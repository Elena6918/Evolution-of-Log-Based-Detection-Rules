#!/usr/bin/env python3
"""
score_pgir_between_two.py
=========================
Debug utility: align and score two PGIR records from a JSONL file,
then print a human-readable breakdown.

Usage
-----
# By 1-based non-empty line number (simplest):
python score_pgir_between_two.py \\
    --jsonl ../../data_prep/ir_data/pgir_ssc_nonempty.jsonl \\
    --line-a 42 --line-b 43

# By lineage_id + version_index:
python score_pgir_between_two.py \\
    --jsonl ../../data_prep/ir_data/pgir_ssc_nonempty.jsonl \\
    --lineage-a lineage_00123 --version-a 3 \\
    --lineage-b lineage_00123 --version-b 4

# Save raw JSON output alongside the printed summary:
python score_pgir_between_two.py \\
    --jsonl ../../data_prep/ir_data/pgir_ssc_nonempty.jsonl \\
    --line-a 42 --line-b 43 --out /tmp/score.json

Config overrides (all optional):
  Alignment : --theta-op-support, --theta-op-coverage, --lambda-ctx,
               --field-mismatch-penalty, --max-fuzzy-cands,
               --hard-gate-polarity, --no-hard-gate-polarity, --debug-scope
  Distance  : --pred-insert-cost, --pred-delete-cost,
               --op-insert-cost, --op-delete-cost, --op-update-cost,
               --pred-field-shift, --pred-op-shift, --pred-value-shift,
               --no-subtree-root-accounting, --no-cap-update-by-del-ins
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace, is_dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pgir_align import (
    AlignConfig,
    DistanceConfig,
    TNode,
    build_canonical_tree,
    align_boolean_ast,
    compute_distance_for_pair_from_trees,
    iter_jsonl_nonempty_with_idx1,
    iter_pred_leaf_labels,
    is_pred_label,
    is_op_label,
)


# =============================================================================
# Entry selection
# =============================================================================

def select_by_line(path: Path, line_num: int) -> Tuple[int, Dict[str, Any]]:
    """Return the record at 1-based non-empty-line position *line_num*."""
    for idx1, obj in iter_jsonl_nonempty_with_idx1(path):
        if idx1 == line_num:
            return idx1, obj
    raise ValueError(
        f"Line {line_num} not found in {path} "
        f"(file has fewer than {line_num} non-empty lines)"
    )


def select_by_lineage_version(
    path: Path,
    lineage_id: str,
    version_index: Optional[int],
) -> Tuple[int, Dict[str, Any]]:
    """Return the record matching *lineage_id* (and optionally *version_index*)."""
    matches: list[Tuple[int, Dict[str, Any]]] = []
    for idx1, obj in iter_jsonl_nonempty_with_idx1(path):
        if obj.get("lineage_id") != lineage_id:
            continue
        if version_index is not None and obj.get("version_index") != version_index:
            continue
        matches.append((idx1, obj))

    if not matches:
        vstr = f" v{version_index}" if version_index is not None else ""
        raise ValueError(f"No record found for lineage={lineage_id}{vstr} in {path}")
    if len(matches) > 1:
        vstr = f" v{version_index}" if version_index is not None else ""
        raise ValueError(
            f"{len(matches)} records match lineage={lineage_id}{vstr}; "
            f"use --version-a/--version-b to disambiguate"
        )
    return matches[0]


def select_entry(
    path: Path,
    line: Optional[int],
    lineage: Optional[str],
    version: Optional[int],
    side_label: str,
) -> Tuple[int, Dict[str, Any]]:
    """Resolve selector arguments to (line_num, record)."""
    if line is not None:
        return select_by_line(path, line)
    if lineage is not None:
        return select_by_lineage_version(path, lineage, version)
    raise ValueError(
        f"Selector for side {side_label!r}: supply --line-{side_label} "
        f"or --lineage-{side_label} [--version-{side_label}]"
    )


# =============================================================================
# Pretty printing
# =============================================================================

SEP  = "─" * 72
SEP2 = "═" * 72


def _tree_str(node: TNode, indent: int = 0, max_depth: int = 8) -> str:
    """Compact recursive tree string, truncated beyond max_depth."""
    prefix = "  " * indent
    if indent >= max_depth:
        return prefix + "...\n"
    lines = [f"{prefix}{node.label}\n"]
    for ch in node.children:
        lines.append(_tree_str(ch, indent + 1, max_depth))
    return "".join(lines)


def print_record_summary(label: str, line_num: int, obj: Dict[str, Any],
                          tree: TNode) -> None:
    print(f"\n{SEP}")
    print(f"  RECORD {label}  (line {line_num})")
    print(SEP)
    for field in ("lineage_id", "rule_canonical", "repo",
                  "version_index", "commit_hash", "commit_date"):
        if field in obj:
            print(f"  {field:<20}: {obj[field]}")
    print(f"  {'predicate_count':<20}: {obj.get('predicate_count', '?')}")

    preds = iter_pred_leaf_labels(tree)
    print(f"\n  Predicates ({len(preds)}):")
    for p in preds:
        print(f"    {p}")

    print(f"\n  Canonical tree:")
    print(_tree_str(tree, indent=2))


def print_alignment(alignment: Dict[str, Any]) -> None:
    print(f"\n{SEP}")
    print(f"  ALIGNMENT")
    print(SEP)
    print(f"  nodes_a      : {alignment['nodes_a']}")
    print(f"  nodes_b      : {alignment['nodes_b']}")
    print(f"  matched      : {alignment['matched_count']}")
    print(f"  unmatched_a  : {len(alignment.get('unmatched_a', []))}")
    print(f"  unmatched_b  : {len(alignment.get('unmatched_b', []))}")

    matches = alignment.get("matches", [])
    if matches:
        print(f"\n  Matched pairs ({len(matches)}):")
        pred_matches = [m for m in matches if m.get("kind") == "PRED"]
        op_matches   = [m for m in matches if m.get("kind") == "OP"]

        if pred_matches:
            print(f"  [PRED — {len(pred_matches)} pairs]")
            for m in pred_matches:
                same = m["a_label"] == m["b_label"]
                marker = "=" if same else "~"
                print(f"    [{m['phase']:<22}] {m['a_label']}")
                if not same:
                    print(f"    {' ' * 26}→ {m['b_label']}")

        if op_matches:
            print(f"  [OP   — {len(op_matches)} pairs]")
            for m in op_matches:
                same = m["a_label"] == m["b_label"]
                print(f"    [{m['phase']:<22}] {m['a_label']}"
                      + (f" → {m['b_label']}" if not same else "")
                      + f"  (support={m.get('support', '?'):.3f},"
                        f" cov_i={m.get('coverage_i', '?'):.2f},"
                        f" cov_j={m.get('coverage_j', '?'):.2f})")

    ua = alignment.get("unmatched_a", [])
    ub = alignment.get("unmatched_b", [])
    if ua:
        print(f"\n  Unmatched in A (deleted / {len(ua)}):")
        for n in ua:
            print(f"    DEL  {n['label']}")
    if ub:
        print(f"\n  Unmatched in B (inserted / {len(ub)}):")
        for n in ub:
            print(f"    INS  {n['label']}")


def print_distance(dist: Dict[str, Any]) -> None:
    print(f"\n{SEP}")
    print(f"  DISTANCE")
    print(SEP)
    print(f"  total        : {dist['total']:.4f}")

    bd = dist.get("breakdown", {})
    print(f"\n  Breakdown:")
    for k, v in bd.items():
        bar = "█" * int(round(v * 10))
        print(f"    {k:<20}: {v:7.4f}  {bar}")

    cc = dist.get("change_counts", {})
    if cc:
        print(f"\n  Change counts:")
        for k, v in cc.items():
            if v:
                print(f"    {k:<28}: {v}")

    ccd = dist.get("change_cost_breakdown", {})
    if ccd:
        print(f"\n  Per-predicate update costs:")
        for entry in ccd:
            if isinstance(entry, dict):
                a_lbl = entry.get("a_label", "?")
                b_lbl = entry.get("b_label", "?")
                cost  = entry.get("cost", 0.0)
                cnts  = entry.get("counts", {})
                print(f"    cost={cost:.4f}  {a_lbl}")
                print(f"    {'':>10}→ {b_lbl}")
                if cnts:
                    shifts = {k: v for k, v in cnts.items() if v}
                    if shifts:
                        print(f"    {'':>10}   shifts: {shifts}")

    sizes = dist.get("sizes", {})
    if sizes:
        print(f"\n  Node accounting:")
        for k, v in sizes.items():
            print(f"    {k:<20}: {v}")


def print_configs(acfg: AlignConfig, dcfg: DistanceConfig) -> None:
    print(f"\n{SEP}")
    print("  CONFIGS  (non-default values only)")
    print(SEP)
    defaults_a = AlignConfig()
    defaults_d = DistanceConfig()
    diff_a = {k: v for k, v in asdict(acfg).items()
               if v != getattr(defaults_a, k)}
    diff_d = {k: v for k, v in asdict(dcfg).items()
               if v != getattr(defaults_d, k)}
    if diff_a:
        print(f"  AlignConfig   : {diff_a}")
    else:
        print(f"  AlignConfig   : (all defaults)")
    if diff_d:
        print(f"  DistanceConfig: {diff_d}")
    else:
        print(f"  DistanceConfig: (all defaults)")


# =============================================================================
# CLI
# =============================================================================

def _cfg_replace(cfg, **kwargs):
    if not is_dataclass(cfg):
        raise TypeError(f"Expected dataclass, got {type(cfg)}")
    return replace(cfg, **kwargs)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Align and score two PGIR records; print a detailed breakdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--jsonl", required=True, type=Path,
                    help="Path to a pgir_{repo}_nonempty.jsonl file.")

    # ── Selectors ────────────────────────────────────────────────────────────
    grp_a = ap.add_argument_group("record A selector (use one)")
    grp_a.add_argument("--line-a", type=int, default=None,
                       help="1-based non-empty line number for record A.")
    grp_a.add_argument("--lineage-a", type=str, default=None,
                       help="lineage_id for record A.")
    grp_a.add_argument("--version-a", type=int, default=None,
                       help="version_index for record A (use with --lineage-a).")

    grp_b = ap.add_argument_group("record B selector (use one)")
    grp_b.add_argument("--line-b", type=int, default=None,
                       help="1-based non-empty line number for record B.")
    grp_b.add_argument("--lineage-b", type=str, default=None,
                       help="lineage_id for record B.")
    grp_b.add_argument("--version-b", type=int, default=None,
                       help="version_index for record B (use with --lineage-b).")

    # ── AlignConfig overrides ─────────────────────────────────────────────────
    grp_ac = ap.add_argument_group("alignment config overrides")
    grp_ac.add_argument("--theta-op-support",      type=float, default=None)
    grp_ac.add_argument("--theta-op-coverage",     type=float, default=None)
    grp_ac.add_argument("--lambda-ctx",            type=float, default=None)
    grp_ac.add_argument("--field-mismatch-penalty",type=float, default=None)
    grp_ac.add_argument("--max-fuzzy-cands",       type=int,   default=None)
    grp_ac.add_argument("--hard-gate-polarity",    action="store_true",
                        help="Disallow predicate matches across different CTX polarity (default).")
    grp_ac.add_argument("--no-hard-gate-polarity", action="store_true",
                        help="Allow predicate matches across different CTX polarity.")
    grp_ac.add_argument("--debug-scope", action="store_true",
                        help="Print scope-matching debug traces to stderr.")

    # ── DistanceConfig overrides ──────────────────────────────────────────────
    grp_dc = ap.add_argument_group("distance config overrides")
    grp_dc.add_argument("--pred-insert-cost",  type=float, default=None)
    grp_dc.add_argument("--pred-delete-cost",  type=float, default=None)
    grp_dc.add_argument("--op-insert-cost",    type=float, default=None)
    grp_dc.add_argument("--op-delete-cost",    type=float, default=None)
    grp_dc.add_argument("--op-update-cost",    type=float, default=None)
    grp_dc.add_argument("--pred-field-shift",  type=float, default=None)
    grp_dc.add_argument("--pred-op-shift",     type=float, default=None)
    grp_dc.add_argument("--pred-value-shift",  type=float, default=None)
    grp_dc.add_argument("--no-subtree-root-accounting", action="store_true")
    grp_dc.add_argument("--no-cap-update-by-del-ins",   action="store_true")

    ap.add_argument("--out", type=Path, default=None,
                    help="Optional path to write the raw JSON result.")
    ap.add_argument("--json-only", action="store_true",
                    help="Print raw JSON only (suppress the human-readable output).")

    return ap.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.jsonl.exists():
        print(f"[ERROR] File not found: {args.jsonl}", file=sys.stderr)
        sys.exit(1)

    # ── Build configs ─────────────────────────────────────────────────────────
    acfg = AlignConfig()
    if args.theta_op_support      is not None:
        acfg = _cfg_replace(acfg, theta_op_support=args.theta_op_support)
    if args.theta_op_coverage     is not None:
        acfg = _cfg_replace(acfg, theta_op_coverage=args.theta_op_coverage)
    if args.lambda_ctx            is not None:
        acfg = _cfg_replace(acfg, lambda_ctx=args.lambda_ctx)
    if args.field_mismatch_penalty is not None:
        acfg = _cfg_replace(acfg, field_mismatch_penalty=args.field_mismatch_penalty)
    if args.max_fuzzy_cands       is not None:
        acfg = _cfg_replace(acfg, max_fuzzy_cands=args.max_fuzzy_cands)
    if args.hard_gate_polarity:
        acfg = _cfg_replace(acfg, hard_gate_polarity=True)
    if args.no_hard_gate_polarity:
        acfg = _cfg_replace(acfg, hard_gate_polarity=False)

    dcfg = DistanceConfig()
    if args.pred_insert_cost  is not None:
        dcfg = _cfg_replace(dcfg, pred_insert_cost=args.pred_insert_cost)
    if args.pred_delete_cost  is not None:
        dcfg = _cfg_replace(dcfg, pred_delete_cost=args.pred_delete_cost)
    if args.op_insert_cost    is not None:
        dcfg = _cfg_replace(dcfg, op_insert_cost=args.op_insert_cost)
    if args.op_delete_cost    is not None:
        dcfg = _cfg_replace(dcfg, op_delete_cost=args.op_delete_cost)
    if args.op_update_cost    is not None:
        dcfg = _cfg_replace(dcfg, op_update_cost=args.op_update_cost)
    if args.pred_field_shift  is not None:
        dcfg = _cfg_replace(dcfg, pred_field_shift=args.pred_field_shift)
    if args.pred_op_shift     is not None:
        dcfg = _cfg_replace(dcfg, pred_op_shift=args.pred_op_shift)
    if args.pred_value_shift  is not None:
        dcfg = _cfg_replace(dcfg, pred_value_shift=args.pred_value_shift)
    if args.no_subtree_root_accounting:
        dcfg = _cfg_replace(dcfg, charge_unmatched_by_subtree_roots=False)
    if args.no_cap_update_by_del_ins:
        dcfg = _cfg_replace(dcfg, cap_update_by_del_ins=False)

    # ── Load records ──────────────────────────────────────────────────────────
    line_a, obj_a = select_entry(
        args.jsonl,
        line=args.line_a, lineage=args.lineage_a, version=args.version_a,
        side_label="a",
    )
    line_b, obj_b = select_entry(
        args.jsonl,
        line=args.line_b, lineage=args.lineage_b, version=args.version_b,
        side_label="b",
    )

    # ── Build canonical trees ─────────────────────────────────────────────────
    ta = build_canonical_tree(obj_a)
    tb = build_canonical_tree(obj_b)

    # ── Align ─────────────────────────────────────────────────────────────────
    alignment = align_boolean_ast(ta, tb, acfg, debug_scope=args.debug_scope)

    # ── Score ─────────────────────────────────────────────────────────────────
    dist = compute_distance_for_pair_from_trees(ta, tb, alignment, dcfg)

    # ── Assemble result dict ──────────────────────────────────────────────────
    def _rec_summary(line_num, obj):
        return {
            "line": line_num,
            "lineage_id":    obj.get("lineage_id"),
            "rule_canonical": obj.get("rule_canonical"),
            "repo":          obj.get("repo"),
            "version_index": obj.get("version_index"),
            "commit_hash":   obj.get("commit_hash"),
            "commit_date":   obj.get("commit_date"),
            "predicate_count": obj.get("predicate_count"),
        }

    result = {
        "a":         _rec_summary(line_a, obj_a),
        "b":         _rec_summary(line_b, obj_b),
        "alignment": alignment,
        "distance":  dist,
        "align_config":    asdict(acfg),
        "distance_config": asdict(dcfg),
    }

    # ── Output ────────────────────────────────────────────────────────────────
    if args.json_only:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n{SEP2}")
        print(f"  PGIR SCORE  {args.jsonl.name}")
        print(SEP2)
        print_record_summary("A", line_a, obj_a, ta)
        print_record_summary("B", line_b, obj_b, tb)
        print_alignment(alignment)
        print_distance(dist)
        print_configs(acfg, dcfg)
        print(f"\n{SEP2}\n")

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2))
        print(f"[saved] {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
