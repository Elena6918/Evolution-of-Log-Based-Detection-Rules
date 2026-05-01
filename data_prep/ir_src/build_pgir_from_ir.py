#!/usr/bin/env python3
"""
build_pgir_from_ir.py
=====================
Stage 2 — ir_src pipeline.

Reads  : data_prep/ir_data/unified_ir_{repo}.jsonl   (Stage 1 output)
Writes : data_prep/ir_data/pgir_{repo}.jsonl

Transforms each Unified IR record into a Predicate-Graph IR (PGIR) record by:

1. Normalizing every predicate node (fix field/identifier duality,
   operator string/object duality).
2. Collecting predicate sources from root_search and every pipeline stage
   that carries a predicate_ir.
3. Merging all sources into a single combined predicate graph (AND of all
   stage predicates).
4. Emitting a flat predicate list for easy downstream querying.

Output schema (one JSON object per line)
-----------------------------------------
Identity (pass-through):
  lineage_id, rule_canonical, repo, version_index, original_rank,
  commit_hash, commit_date, rule_id, spl_source, spl

PGIR fields:
  predicate_graph : dict | null  -- combined normalized predicate tree
  predicates      : list         -- flat list of leaf PredicateNode dicts
  predicate_count : int          -- len(predicates)
  sources         : list         -- per-source metadata
  stage_count     : int          -- total pipeline stages in the IR
  ir_success      : bool         -- forwarded from Stage 1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.predicate_normalize import (
    normalize_predicate_ir,
    collect_predicates,
    is_empty,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DATA_PREP  = _HERE.parent
_IR_DATA    = _DATA_PREP / "ir_data"

_PASSTHROUGH_FIELDS = (
    "lineage_id",
    "rule_canonical",
    "repo",
    "version_index",
    "original_rank",
    "commit_hash",
    "commit_date",
    "rule_id",
    "spl_source",
    "spl",
)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _is_macro_placeholder_predicate(node: Any) -> bool:
    """Return True for placeholder leaf predicates of the form ``MACRO = ...``."""
    if not isinstance(node, dict) or node.get("type") != "predicate":
        return False

    field = node.get("field")
    operator = node.get("operator")

    field_name = str(field.get("value", "")) if isinstance(field, dict) else str(field or "")
    op_name = str(operator.get("value", "")) if isinstance(operator, dict) else str(operator or "")

    return field_name == "MACRO" and op_name in {"=", "==", "EQ"}


def _strip_macro_placeholder_predicates(
    node: Any,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Drop ``MACRO = ...`` leaves and simplify the resulting boolean tree."""
    if not isinstance(node, dict):
        return None, []

    ntype = node.get("type")

    if ntype == "predicate":
        if _is_macro_placeholder_predicate(node):
            return None, [node]
        return node, []

    if ntype != "expr":
        return node, []

    kept_children: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for child in node.get("children", []):
        child_new, child_dropped = _strip_macro_placeholder_predicates(child)
        dropped.extend(child_dropped)
        if child_new is not None:
            kept_children.append(child_new)

    if not kept_children:
        return None, dropped

    if node.get("op") == "NOT":
        return {
            "type": "expr",
            "op": "NOT",
            "children": kept_children[:1],
        }, dropped

    if len(kept_children) == 1:
        return kept_children[0], dropped

    return {
        "type": "expr",
        "op": node.get("op"),
        "children": kept_children,
    }, dropped


def _normalize_and_filter_predicate_ir(
    predicate_ir: Any,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Normalize a predicate tree, then remove placeholder macro predicates."""
    pir = normalize_predicate_ir(predicate_ir)
    return _strip_macro_placeholder_predicates(pir)


def _build_source_record(source_meta: Dict[str, Any], predicate_ir: Any) -> Dict[str, Any]:
    """Attach filtered predicate metadata to one source entry."""
    pir, dropped_macro_preds = _normalize_and_filter_predicate_ir(predicate_ir)
    preds = collect_predicates(pir) if pir else []

    source = dict(source_meta)
    source["predicate_ir"] = pir
    source["predicate_count"] = len(preds)
    source["dropped_macro_predicate_count"] = len(dropped_macro_preds)
    return source

def _extract_sources(ir: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Walk root_search + pipeline and collect every predicate source."""
    sources: List[Dict[str, Any]] = []

    # root_search
    rs = ir.get("root_search")
    if rs and rs.get("predicate_ir"):
        sources.append(_build_source_record({
            "origin": "root_search",
            "stage_type": rs.get("type", "search"),
            "layer": rs.get("layer"),
        }, rs["predicate_ir"]))

    # pipeline stages
    for idx, stage in enumerate(ir.get("pipeline", [])):
        pir_raw = stage.get("predicate_ir")
        if not pir_raw or not stage.get("contributes_to_filter", False):
            join_branch_pred = ((stage.get("info") or {}).get("branch_predicate_ir"))
            if (
                stage.get("type") == "join"
                and not pir_raw
                and join_branch_pred
            ):
                sources.append(_build_source_record({
                    "origin": "pipeline_join_branch",
                    "stage_index": idx,
                    "stage_type": stage.get("type", "unknown"),
                    "layer": stage.get("layer"),
                    "join_type": ((stage.get("info") or {}).get("join_type")),
                    "join_affects_match_set": ((stage.get("info") or {}).get("join_affects_match_set")),
                }, join_branch_pred))
            continue
        sources.append(_build_source_record({
            "origin": "pipeline",
            "stage_index": idx,
            "stage_type": stage.get("type", "unknown"),
            "layer": stage.get("layer"),
        }, pir_raw))

    return sources


def _merge_predicate_graphs(sources: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Combine all source predicate_ir trees into one AND-rooted tree.

    If there is exactly one source, return its tree directly.
    If there are zero, return None.
    """
    trees = [s["predicate_ir"] for s in sources if s.get("predicate_ir")]
    if not trees:
        return None
    if len(trees) == 1:
        return trees[0]
    return {
        "type": "expr",
        "op": "AND",
        "children": trees,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_pgir(
    repo_type: str,
    *,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    num_entries: Optional[int] = None,
    quiet: bool = False,
) -> None:
    in_path  = input_path  or _IR_DATA / f"unified_ir_{repo_type}.jsonl"
    out_path = output_path or _IR_DATA / f"pgir_{repo_type}.jsonl"

    if not in_path.exists():
        logger.error("Input file not found: %s", in_path)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("repo_type : %s", repo_type)
    logger.info("input     : %s", in_path)
    logger.info("output    : %s", out_path)

    ok = err = skipped = 0
    empty_pred = nonempty_pred = 0

    with in_path.open("r", encoding="utf-8") as f_in, \
         out_path.open("w", encoding="utf-8") as f_out:

        for line_no, raw_line in enumerate(f_in, 1):
            if num_entries and line_no > num_entries:
                break

            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                logger.warning("line %d: JSON decode error: %s", line_no, exc)
                skipped += 1
                continue

            ir_success = entry.get("ir_success", False)
            ir = entry.get("ir")

            record: dict = {f: entry.get(f) for f in _PASSTHROUGH_FIELDS}
            record["ir_success"] = ir_success

            if not ir_success or not ir:
                record["predicate_graph"] = None
                record["predicates"] = []
                record["predicate_count"] = 0
                record["sources"] = []
                record["stage_count"] = 0
                err += 1
            else:
                sources = _extract_sources(ir)
                graph = _merge_predicate_graphs(sources)
                preds = collect_predicates(graph) if graph else []

                # Strip predicate_ir from sources to avoid duplicating bulk
                sources_meta = []
                for s in sources:
                    s_copy = {k: v for k, v in s.items() if k != "predicate_ir"}
                    sources_meta.append(s_copy)

                record["predicate_graph"] = graph
                record["predicates"] = preds
                record["predicate_count"] = len(preds)
                record["sources"] = sources_meta
                record["stage_count"] = len(ir.get("pipeline", []))
                ok += 1

                if is_empty(graph):
                    empty_pred += 1
                else:
                    nonempty_pred += 1

            f_out.write(json.dumps(record, ensure_ascii=False) + "\n")

            if not quiet and line_no % 10000 == 0:
                logger.info(
                    "  [line %d] ok=%d err=%d skipped=%d",
                    line_no, ok, err, skipped,
                )

    total = ok + err
    print("\n" + "=" * 60)
    print(f"PGIR BUILD SUMMARY  [{repo_type.upper()}]")
    print("=" * 60)
    print(f"  Lines read            : {total + skipped}")
    print(f"  Records processed     : {total}")
    print(f"  IR success (Stage 1)  : {ok}")
    print(f"  IR errors (forwarded) : {err}")
    print(f"  Skipped               : {skipped}")
    print(f"  Non-empty predicate   : {nonempty_pred}")
    print(f"  Empty predicate       : {empty_pred}")
    if total > 0:
        print(f"  Non-empty rate        : {100.0 * nonempty_pred / total:.1f}%")
    print("=" * 60)
    print(f"\nOutput: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Stage 2 (ir_src): build PGIR from Unified IR."
    )
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
    )
    ap.add_argument("--input", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("-n", "--num-entries", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = _parse_args()
    build_pgir(
        args.repo_type,
        input_path=Path(args.input)   if args.input  else None,
        output_path=Path(args.output) if args.output else None,
        num_entries=args.num_entries,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
