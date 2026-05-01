#!/usr/bin/env python3
"""
split_pgir_by_predicate_graph.py
================================
Stage 3a — ir_src pipeline.

Reads  : data_prep/ir_data/pgir_{repo}.jsonl         (Stage 2 output)
Writes : data_prep/ir_data/pgir_{repo}_empty.jsonl
         data_prep/ir_data/pgir_{repo}_nonempty.jsonl

Splits PGIR records into two files based on whether the record's
predicate_graph is empty (null / zero predicates) or non-empty.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HERE      = Path(__file__).resolve().parent
_DATA_PREP = _HERE.parent
_IR_DATA   = _DATA_PREP / "ir_data"


def split_pgir(
    repo_type: str,
    *,
    input_path: Optional[Path] = None,
    output_empty: Optional[Path] = None,
    output_nonempty: Optional[Path] = None,
    num_entries: Optional[int] = None,
    quiet: bool = False,
) -> None:
    in_path = input_path    or _IR_DATA / f"pgir_{repo_type}.jsonl"
    e_path  = output_empty  or _IR_DATA / f"pgir_{repo_type}_empty.jsonl"
    ne_path = output_nonempty or _IR_DATA / f"pgir_{repo_type}_nonempty.jsonl"

    if not in_path.exists():
        logger.error("Input file not found: %s", in_path)
        sys.exit(1)

    e_path.parent.mkdir(parents=True, exist_ok=True)
    ne_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("repo_type : %s", repo_type)
    logger.info("input     : %s", in_path)
    logger.info("empty     : %s", e_path)
    logger.info("nonempty  : %s", ne_path)

    n_empty = n_nonempty = skipped = 0

    with in_path.open("r", encoding="utf-8") as f_in, \
         e_path.open("w", encoding="utf-8") as f_empty, \
         ne_path.open("w", encoding="utf-8") as f_nonempty:

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

            pred_count = record.get("predicate_count", 0)

            if pred_count > 0:
                f_nonempty.write(raw_line + "\n")
                n_nonempty += 1
            else:
                f_empty.write(raw_line + "\n")
                n_empty += 1

            if not quiet and line_no % 10000 == 0:
                logger.info(
                    "  [line %d] nonempty=%d empty=%d",
                    line_no, n_nonempty, n_empty,
                )

    total = n_empty + n_nonempty
    print("\n" + "=" * 60)
    print(f"PGIR SPLIT SUMMARY  [{repo_type.upper()}]")
    print("=" * 60)
    print(f"  Lines read   : {total + skipped}")
    print(f"  Non-empty    : {n_nonempty}")
    print(f"  Empty        : {n_empty}")
    print(f"  Skipped      : {skipped}")
    if total > 0:
        print(f"  Non-empty %  : {100.0 * n_nonempty / total:.1f}%")
    print("=" * 60)
    print(f"\nEmpty    : {e_path}")
    print(f"Non-empty: {ne_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Stage 3a (ir_src): split PGIR into empty / non-empty."
    )
    ap.add_argument("--repo-type", required=True, choices=["sigma", "ssc"])
    ap.add_argument("--input", default=None)
    ap.add_argument("--output-empty", default=None)
    ap.add_argument("--output-nonempty", default=None)
    ap.add_argument("-n", "--num-entries", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = _parse_args()
    split_pgir(
        args.repo_type,
        input_path=Path(args.input) if args.input else None,
        output_empty=Path(args.output_empty) if args.output_empty else None,
        output_nonempty=Path(args.output_nonempty) if args.output_nonempty else None,
        num_entries=args.num_entries,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
