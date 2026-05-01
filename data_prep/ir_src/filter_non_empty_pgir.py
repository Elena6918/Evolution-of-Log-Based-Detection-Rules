#!/usr/bin/env python3
"""
filter_non_empty_pgir.py
========================
Stage 3b — ir_src pipeline.

Reads  : data_prep/ir_data/pgir_{repo}_nonempty.jsonl  (Stage 3a output)
Writes : data_prep/ir_data/pgir_{repo}_filtered.jsonl

Filters the non-empty PGIR records, removing low-quality entries where the
predicate graph contains only noise.  Current filter rules:

1. **Placeholder-only**: all predicates are ``_raw CONTAINS "__SUBSEARCH__"``
2. **Wildcard-only**: all predicates are ``_raw CONTAINS "*"``
3. **ir_success=false**: records forwarded from Stage 1 failures
   (these already have predicate_count=0 so normally land in _empty,
   but guard against edge cases)

Records that pass all filters are written to the output.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HERE      = Path(__file__).resolve().parent
_DATA_PREP = _HERE.parent
_IR_DATA   = _DATA_PREP / "ir_data"

# ── noise predicates ───────────────────────────────────────────────────

_NOISE_VALUES = frozenset({"__SUBSEARCH__", "*"})


def _is_noise_predicate(pred: Dict[str, Any]) -> bool:
    """Return True if *pred* is a low-signal ``_raw CONTAINS <noise>``."""
    field = pred.get("field", {})
    fname = field.get("value", "") if isinstance(field, dict) else ""
    if fname != "_raw":
        return False
    op = pred.get("operator", "")
    if op not in ("CONTAINS", "contains"):
        return False
    val = pred.get("value", {})
    vval = val.get("value", "") if isinstance(val, dict) else str(val)
    return vval in _NOISE_VALUES


def _is_noise_only(predicates: List[Dict[str, Any]]) -> bool:
    """Return True if every predicate in the list is noise."""
    if not predicates:
        return True
    return all(_is_noise_predicate(p) for p in predicates)


# ── main ───────────────────────────────────────────────────────────────

def filter_pgir(
    repo_type: str,
    *,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    num_entries: Optional[int] = None,
    quiet: bool = False,
) -> None:
    in_path  = input_path  or _IR_DATA / f"pgir_{repo_type}_nonempty.jsonl"
    out_path = output_path or _IR_DATA / f"pgir_{repo_type}_filtered.jsonl"

    if not in_path.exists():
        logger.error("Input file not found: %s", in_path)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("repo_type : %s", repo_type)
    logger.info("input     : %s", in_path)
    logger.info("output    : %s", out_path)

    kept = dropped_noise = dropped_fail = skipped = 0

    with in_path.open("r", encoding="utf-8") as f_in, \
         out_path.open("w", encoding="utf-8") as f_out:

        for line_no, raw_line in enumerate(f_in, 1):
            if num_entries and line_no > num_entries:
                break

            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                logger.warning("line %d: JSON decode error: %s", line_no, exc)
                skipped += 1
                continue

            # Filter 1: ir_success
            if not record.get("ir_success", False):
                dropped_fail += 1
                continue

            # Filter 2: noise-only predicates
            preds = record.get("predicates", [])
            if _is_noise_only(preds):
                dropped_noise += 1
                continue

            f_out.write(raw_line + "\n")
            kept += 1

            if not quiet and line_no % 10000 == 0:
                logger.info(
                    "  [line %d] kept=%d noise=%d fail=%d",
                    line_no, kept, dropped_noise, dropped_fail,
                )

    total = kept + dropped_noise + dropped_fail
    print("\n" + "=" * 60)
    print(f"PGIR FILTER SUMMARY  [{repo_type.upper()}]")
    print("=" * 60)
    print(f"  Lines read         : {total + skipped}")
    print(f"  Kept               : {kept}")
    print(f"  Dropped (noise)    : {dropped_noise}")
    print(f"  Dropped (ir fail)  : {dropped_fail}")
    print(f"  Skipped            : {skipped}")
    if total > 0:
        print(f"  Keep rate          : {100.0 * kept / total:.1f}%")
    print("=" * 60)
    print(f"\nOutput: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Stage 3b (ir_src): filter non-empty PGIR records."
    )
    ap.add_argument("--repo-type", required=True, choices=["sigma", "ssc"])
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
    filter_pgir(
        args.repo_type,
        input_path=Path(args.input)   if args.input  else None,
        output_path=Path(args.output) if args.output else None,
        num_entries=args.num_entries,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
