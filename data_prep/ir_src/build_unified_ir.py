#!/usr/bin/env python3
"""
build_unified_ir.py
====================
Stage 1 — ir_src pipeline.

Reads  : data_prep/build_data/rule_versions_{repo}.jsonl
           (output of build_rule_versions.py, Stage 5a of the build pipeline)
Writes : data_prep/ir_data/unified_ir_{repo}.jsonl

Each input line is one valid rule version.  This script parses the SPL string
into a Unified IR and forwards all identity fields unchanged so downstream
stages never need to re-join with the build pipeline outputs.

Output schema (one JSON object per line)
-----------------------------------------
Identity (pass-through from rule_versions):
  lineage_id      : str
  rule_canonical  : str     -- canonical repo-relative path
  repo            : "sigma" | "ssc"
  version_index   : int     -- contiguous 1-based index over valid versions
  original_rank   : int     -- 1-based position among ALL commits
  commit_hash     : str
  commit_date     : str     -- ISO-8601
  rule_id         : str | null
  spl_source      : str     -- "native" | "sigma_convert"
  spl             : str     -- the SPL that was parsed

IR fields:
  ir              : dict | null  -- Unified IR (null when ir_success=false)
  ir_success      : bool
  ir_error        : str | null   -- error message when ir_success=false
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.ir_builder import make_builder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DATA_PREP = _HERE.parent          # data_prep/
_BUILD_DATA = _DATA_PREP / "build_data"
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
# Main
# ---------------------------------------------------------------------------

def build_unified_ir(
    repo_type: str,
    *,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    grammar: str = "boolean_expr.lark",
    num_entries: Optional[int] = None,
    quiet: bool = False,
) -> None:
    in_path  = input_path  or _BUILD_DATA / f"rule_versions_{repo_type}.jsonl"
    out_path = output_path or _IR_DATA    / f"unified_ir_{repo_type}.jsonl"

    if not in_path.exists():
        logger.error("Input file not found: %s", in_path)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("repo_type : %s", repo_type)
    logger.info("input     : %s", in_path)
    logger.info("output    : %s", out_path)
    logger.info("grammar   : %s", grammar)

    builder = make_builder(grammar_name=grammar, quiet=quiet)

    ok = err = skipped = 0

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

            spl = entry.get("spl")
            if not spl or not isinstance(spl, str) or not spl.strip():
                skipped += 1
                continue

            # Build the pass-through identity dict first
            record: dict = {f: entry.get(f) for f in _PASSTHROUGH_FIELDS}

            # Parse the SPL into Unified IR
            try:
                ir = builder.build_from_text(spl.strip())
                record["ir"]         = ir
                record["ir_success"] = True
                record["ir_error"]   = None
                ok += 1
            except Exception as exc:
                record["ir"]         = None
                record["ir_success"] = False
                record["ir_error"]   = str(exc)
                err += 1

            f_out.write(json.dumps(record, ensure_ascii=False) + "\n")

            if not quiet and line_no % 5000 == 0:
                logger.info("  [line %d] ok=%d err=%d skipped=%d",
                            line_no, ok, err, skipped)

    # Summary
    total = ok + err
    print("\n" + "=" * 60)
    print(f"UNIFIED IR SUMMARY  [{repo_type.upper()}]")
    print("=" * 60)
    print(f"  Lines read          : {ok + err + skipped}")
    print(f"  Versions parsed     : {total}")
    print(f"  IR success          : {ok}")
    print(f"  IR errors           : {err}")
    print(f"  Skipped (no SPL)    : {skipped}")
    if total > 0:
        print(f"  Success rate        : {100.0 * ok / total:.1f}%")
    print("=" * 60)
    print(f"\nOutput: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Stage 1 (ir_src): parse SPL → Unified IR for each rule version."
    )
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
        help="Which repo to process.",
    )
    ap.add_argument(
        "--input", default=None,
        help="Override input JSONL path (default: build_data/rule_versions_{repo}.jsonl).",
    )
    ap.add_argument(
        "--output", default=None,
        help="Override output JSONL path (default: ir_data/unified_ir_{repo}.jsonl).",
    )
    ap.add_argument(
        "--grammar", default="boolean_expr.lark",
        help="Grammar filename in lib/ (default: boolean_expr.lark).",
    )
    ap.add_argument(
        "-n", "--num-entries", type=int, default=None,
        help="Limit to first N input lines (for testing).",
    )
    ap.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress logging.",
    )
    return ap.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = _parse_args()
    build_unified_ir(
        args.repo_type,
        input_path=Path(args.input)   if args.input  else None,
        output_path=Path(args.output) if args.output else None,
        grammar=args.grammar,
        num_entries=args.num_entries,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
