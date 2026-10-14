#!/usr/bin/env python3
"""
prepare_align_data.py
=====================
Builds pred_diff_{repo}.jsonl — rich predicate-level diff records for every
non-noop adjacent version pair, for use in the interactive demo website.

Two-pass approach:
  Pass 1 (fast): harvest d_step / change_counts / is_noop from the pre-computed
                 align_data/all_steps_{repo}.jsonl (no re-alignment needed).
  Pass 2 (targeted): for each non-noop step, load the two PGIR records and
                 re-run alignment to recover the actual predicate labels
                 (all_steps only stores counts, not labels).

Outputs
-------
  data_prep/align_data/pred_diff_sigma.jsonl
  data_prep/align_data/pred_diff_ssc.jsonl

Each output row (one per non-noop adjacent pair):
  {
    "lineage_id":   str,
    "repo":         str,
    "version_a":    int,          # 1-based version_index of older version
    "version_b":    int,          # 1-based version_index of newer version
    "d_step":       float,        # raw alignment cost (not normalised)
    "counts": {
        "value_shift": int,       # matched preds where only value changed
        "op_shift":    int,       # matched preds where operator changed
        "field_shift": int,       # matched preds where field changed
        "inserted":    int,       # unmatched in B (new predicates)
        "deleted":     int        # unmatched in A (removed predicates)
    },
    "changed": [                  # matched PRED pairs that differ (a_label != b_label)
        {"a": str, "b": str, "diffs": ["field"|"op"|"value"]}
    ],
    "deleted":  [str],            # labels of predicates only in A
    "inserted": [str],            # labels of predicates only in B
    "nc":       int               # count of unchanged matched predicates
  }

Label format (raw canonical, parseable by the frontend):
  P:<field>|<op>|<norm_value_repr>|CTX:POS/NEG

Warnings are printed to stderr for:
  - Non-noop steps where PGIR record is missing for one or both versions
  - Multi-version rules present in the rule index but absent from all_steps
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE      = Path(__file__).resolve().parent
_DATA_PREP = _HERE / "data_prep"
_ALIGN_DATA = _DATA_PREP / "align_data"
_IR_DATA    = _DATA_PREP / "ir_data"

sys.path.insert(0, str(_DATA_PREP / "align_src"))
from pgir_align import (
    AlignConfig,
    DistanceConfig,
    build_canonical_tree,
    align_boolean_ast,
    iter_pred_leaf_labels,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def _split_pred_label(label: str) -> Tuple[str, str, str, str]:
    """Split 'P:field|OP|value_repr|CTX:POS' into (field, op, value_repr, ctx).

    The value_repr part may contain pipe characters inside tuple reprs, so we
    only split on the first two pipes (field and op) then find the CTX suffix.
    """
    assert label.startswith("P:"), f"Not a predicate label: {label!r}"
    body = label[2:]  # strip leading "P:"

    # First pipe splits off field; second splits off op; rest is value|CTX
    first = body.index("|")
    field = body[:first]
    rest = body[first + 1:]

    second = rest.index("|")
    op = rest[:second]
    remainder = rest[second + 1:]

    # CTX suffix is always "|CTX:POS" or "|CTX:NEG" at the very end
    ctx = ""
    for suffix in ("|CTX:POS", "|CTX:NEG"):
        if remainder.endswith(suffix):
            ctx = suffix[1:]  # e.g. "CTX:POS"
            remainder = remainder[: -len(suffix)]
            break

    return field, op, remainder, ctx  # remainder is the norm_value repr


def _label_diffs(label_a: str, label_b: str) -> List[str]:
    """Return list of changed components between two predicate labels."""
    fa, oa, va, ca = _split_pred_label(label_a)
    fb, ob, vb, cb = _split_pred_label(label_b)
    diffs = []
    if fa != fb:
        diffs.append("field")
    if oa != ob:
        diffs.append("op")
    if va != vb or ca != cb:
        diffs.append("value")
    return diffs


# ---------------------------------------------------------------------------
# Per-repo processing
# ---------------------------------------------------------------------------

def _load_pgir_index(repo: str) -> Dict[Tuple[str, int], dict]:
    """Load pgir_{repo}_nonempty.jsonl keyed by (lineage_id, version_index)."""
    path = _IR_DATA / f"pgir_{repo}_nonempty.jsonl"
    log.info("Loading PGIR index from %s ...", path.name)
    index: Dict[Tuple[str, int], dict] = {}
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            key = (r["lineage_id"], r["version_index"])
            index[key] = r
    log.info("  %d records loaded (unique lineage-version pairs)", len(index))
    return index


def _load_steps(repo: str) -> List[dict]:
    """Load all_steps_{repo}.jsonl and return as list."""
    path = _ALIGN_DATA / f"all_steps_{repo}.jsonl"
    log.info("Loading steps from %s ...", path.name)
    rows = []
    with open(path) as fh:
        for line in fh:
            rows.append(json.loads(line))
    log.info("  %d total steps loaded", len(rows))
    return rows


def _process_repo(repo: str, align_cfg: AlignConfig) -> int:
    """Process one repo. Returns number of warnings emitted."""
    out_path = _ALIGN_DATA / f"pred_diff_{repo}.jsonl"
    warn_count = 0

    steps = _load_steps(repo)
    pgir_index = _load_pgir_index(repo)

    noop_count = sum(1 for s in steps if s["is_noop"])
    non_noop = [s for s in steps if not s["is_noop"]]
    log.info(
        "%s: %d steps (%d noop, %d non-noop to process)",
        repo, len(steps), noop_count, len(non_noop),
    )

    written = 0
    missing_pgir: List[str] = []

    with open(out_path, "w") as fout:
        for i, step in enumerate(non_noop):
            if i % 500 == 0:
                log.info("  %s: %d / %d non-noop steps processed ...", repo, i, len(non_noop))

            lid   = step["lineage_id"]
            va    = step["version_a"]
            vb    = step["version_b"]
            cc    = step.get("change_counts", {})

            # Look up PGIR records for both sides
            rec_a = pgir_index.get((lid, va))
            rec_b = pgir_index.get((lid, vb))

            if rec_a is None or rec_b is None:
                msg = (
                    f"MISSING PGIR  repo={repo}  lineage={lid}  "
                    f"version_a={va} (found={rec_a is not None})  "
                    f"version_b={vb} (found={rec_b is not None})"
                )
                log.warning(msg)
                missing_pgir.append(msg)
                warn_count += 1
                continue

            # Build trees and align
            try:
                ta = build_canonical_tree(rec_a)
                tb = build_canonical_tree(rec_b)
                alignment = align_boolean_ast(ta, tb, align_cfg)
            except Exception as exc:
                log.warning(
                    "ALIGN ERROR  repo=%s  lineage=%s  va=%d  vb=%d  error=%s",
                    repo, lid, va, vb, exc,
                )
                warn_count += 1
                continue

            # Separate PRED-kind matches from OP-kind
            changed: List[dict] = []
            matched: List[str] = []  # unchanged predicate labels (a == b)

            for m in alignment.get("matches", []):
                if m.get("kind") != "PRED":
                    continue
                la, lb = m["a_label"], m["b_label"]
                if la == lb:
                    matched.append(la)
                else:
                    diffs = _label_diffs(la, lb)
                    changed.append({"a": la, "b": lb, "diffs": diffs})

            deleted  = [u["label"] for u in alignment.get("unmatched_a", [])
                        if u["label"].startswith("P:")]
            inserted = [u["label"] for u in alignment.get("unmatched_b", [])
                        if u["label"].startswith("P:")]

            row = {
                "lineage_id": lid,
                "repo":       repo,
                "version_a":  va,
                "version_b":  vb,
                "d_step":     step["d_step"],
                "counts": {
                    "value_shift": cc.get("pred_value_shift", 0),
                    "op_shift":    cc.get("pred_op_shift",    0),
                    "field_shift": cc.get("pred_field_shift", 0),
                    "inserted":    cc.get("pred_insertion",   0),
                    "deleted":     cc.get("pred_deletion",    0),
                },
                "changed":  changed,
                "matched":  matched,   # full labels of unchanged matched predicates
                "deleted":  deleted,
                "inserted": inserted,
                # nc is len(matched) — derived by consumer, not stored separately
            }
            fout.write(json.dumps(row) + "\n")
            written += 1

    log.info("%s: wrote %d pred_diff records to %s", repo, written, out_path.name)
    if missing_pgir:
        log.warning(
            "%s: %d non-noop steps had missing PGIR records — "
            "these steps will have no predicate diff in the demo:",
            repo, len(missing_pgir),
        )
        for msg in missing_pgir:
            log.warning("  %s", msg)

    return warn_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        choices=["sigma", "ssc", "both"],
        default="both",
        help="Which repo to process (default: both)",
    )
    parser.add_argument(
        "--no-hard-gate-polarity",
        action="store_true",
        default=False,
        help="Disable polarity hard-gating in alignment (pass through to AlignConfig)",
    )
    args = parser.parse_args()

    _ALIGN_DATA.mkdir(parents=True, exist_ok=True)

    align_cfg = AlignConfig(
        hard_gate_polarity=not args.no_hard_gate_polarity,
    )

    repos = ["sigma", "ssc"] if args.repo == "both" else [args.repo]
    total_warnings = 0
    for repo in repos:
        log.info("=== Processing repo: %s ===", repo)
        total_warnings += _process_repo(repo, align_cfg)

    if total_warnings:
        log.warning("Finished with %d total warnings — review output above.", total_warnings)
        sys.exit(1)
    else:
        log.info("Done. No warnings.")


if __name__ == "__main__":
    main()
