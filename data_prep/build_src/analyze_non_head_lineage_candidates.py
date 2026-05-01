"""
analyze_non_head_lineage_candidates.py
=======================================
For every lineage where exists_in_head=false, find candidate lineages that
might represent a continuation, renaming, or merge of the deleted lineage.

Supports both Sigma and SSC repos via --repo-type sigma|ssc.

ANALYSIS NOTES
--------------
Strong evidence (high-confidence signals):
  - Shared ID: a UUID appearing in both lineages is near-definitive.  UUIDs
    are supposed to be unique per detection rule; reuse across lineages means
    the two lineages describe the same logical rule at different points in time.
  - Exact normalized basename match: after lowercasing, stripping extensions,
    and collapsing separators, an identical stem almost always means the same
    rule file under a different directory or format.
  - Shared historical path: if a file path that appears in lineage A's commits
    also appears in lineage B's commits, the two lineages share physical history.

Medium evidence:
  - High fuzzy basename similarity (>=0.80): catches minor renames, added/
    removed prefixes such as "ssa___", plural changes, token reordering.
  - Close temporal gap (<=30 days between last_commit of source and
    first_commit of candidate): consistent with a rename/migration event.
  - Rename-keyword in commit subjects ("renamed", "converted", "migrated", etc.)

Weak evidence:
  - Path-token overlap ratio >=0.5 (same directory tokens but different stem).
  - Temporal gap <=90 days with any other signal present.

Outputs:
  build_data/non_head_lineage_candidate_matches_{repo}.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make lib/ importable regardless of working directory
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.config import RepoConfig
from lib.scoring import (
    MAX_CANDIDATES,
    build_indexes,
    coarse_label,
    gather_candidate_lids,
    score_pair,
    source_meta,
)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def is_known_deleted(lineage: dict) -> bool:
    return bool(lineage.get("deleted_in_commit") or lineage.get("deleted_date"))


def analyze(
    lineages: list[dict],
    *,
    skip_known_deleted_no_candidates: bool = False,
) -> tuple[list[dict], int]:
    """
    For each non-head lineage, gather and rank candidates.
    Returns a list of result dicts and a skipped-count.
    """
    indexes = build_indexes(lineages)
    by_lid = indexes["by_lid"]

    non_head = [e for e in lineages if not e.get("exists_in_head", True)]
    print(f"  Non-head lineages: {len(non_head)}", flush=True)

    results: list[dict] = []
    skipped_known_deleted = 0

    for source in non_head:
        src_lid = source["lineage_id"]
        smeta = source_meta(source)
        cand_lids = gather_candidate_lids(smeta, src_lid, indexes)

        scored: list[tuple[float, dict, list[dict]]] = []
        for cid in cand_lids:
            cand = by_lid.get(cid)
            if cand is None:
                continue
            sc, ev = score_pair(smeta, cand)
            if sc > 0:
                scored.append((sc, cand, ev))

        scored.sort(key=lambda x: -x[0])
        top = scored[:MAX_CANDIDATES]

        if skip_known_deleted_no_candidates and not top and is_known_deleted(source):
            skipped_known_deleted += 1
            continue

        candidates_out: list[dict] = []
        for sc, cand, ev in top:
            candidates_out.append({
                "lineage_id": cand["lineage_id"],
                "canonical_name": cand.get("canonical_name"),
                "exists_in_head": cand.get("exists_in_head"),
                "first_commit_date": cand.get("first_commit_date"),
                "last_commit_date": cand.get("last_commit_date"),
                "score": round(sc, 1),
                "label": coarse_label(sc),
                "evidence": ev,
            })

        results.append({
            "source_lineage_id": src_lid,
            "source_canonical_name": source.get("canonical_name"),
            "source_all_ids": sorted(source.get("all_ids") or []),
            "source_last_commit_date": source.get("last_commit_date"),
            "candidates": candidates_out,
            "best_score": round(top[0][0], 1) if top else 0.0,
            "best_label": coarse_label(top[0][0]) if top else "no_evidence",
        })

    return results, skipped_known_deleted


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_json(results: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)


def print_summary(results: list[dict], skipped_known_deleted: int = 0) -> None:
    total   = len(results)
    strong  = sum(1 for r in results if r["best_label"] == "strong_candidate")
    possible = sum(1 for r in results if r["best_label"] == "possible_candidate")
    weak    = sum(1 for r in results if r["best_label"] == "weak_candidate")
    no_ev   = sum(1 for r in results if r["best_label"] == "no_evidence")

    id_based = 0
    name_only = 0
    for r in results:
        has_id = any(
            any(e["signal"] == "shared_id" for e in c["evidence"])
            for c in r["candidates"]
        )
        has_name = any(
            any(e["signal"] in ("exact_basename_match", "fuzzy_basename") for e in c["evidence"])
            for c in r["candidates"]
        )
        if has_id:
            id_based += 1
        elif has_name:
            name_only += 1

    print("\n" + "=" * 60)
    print("NON-HEAD LINEAGE CANDIDATE ANALYSIS SUMMARY")
    print("=" * 60)
    print(f"  Result rows emitted              : {total}")
    print(f"  Known-deleted/no-candidate skipped: {skipped_known_deleted}")
    print(f"  Strong candidates found           : {strong}")
    print(f"  Possible candidates               : {possible}")
    print(f"  Weak candidates                   : {weak}")
    print(f"  No evidence at all                : {no_ev}")
    print(f"  With ID-based evidence            : {id_based}")
    print(f"  Name/path evidence only (no ID)   : {name_only}")
    print("=" * 60)

    strong_results = sorted(
        [r for r in results if r["best_label"] == "strong_candidate"],
        key=lambda r: -r["best_score"],
    )
    if strong_results:
        print("\nTop strong candidates (sample, up to 5):")
        for r in strong_results[:5]:
            best = r["candidates"][0]
            print(f"  {r['source_lineage_id']} -> {best['lineage_id']}  score={best['score']}")
            print(f"    src : {r['source_canonical_name']}")
            print(f"    cand: {best['canonical_name']}")
            sigs = [e["signal"] for e in best["evidence"]]
            print(f"    signals: {', '.join(sigs)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Analyze non-head lineage candidates for continuation/rename matches."
    )
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
        help="Which repo to analyze (sigma or ssc).",
    )
    ap.add_argument(
        "--input", default=None,
        help="Override input lineage JSON path (default: build_data/lineage_metadata_{repo}.json).",
    )
    ap.add_argument(
        "--output-json", default=None,
        help="Override output JSON path.",
    )
    ap.add_argument(
        "--skip-known-deleted-no-candidates",
        action="store_true",
        help="Omit known-deleted non-head lineages when no continuation candidate is found.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = RepoConfig(args.repo_type)

    in_path  = Path(args.input)        if args.input       else cfg.path("stage3_out")
    out_json = Path(args.output_json)  if args.output_json else cfg.path("non_head_candidates")

    if not in_path.exists():
        print(f"ERROR: input file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {in_path} ...", flush=True)
    with open(in_path, encoding="utf-8") as f:
        lineages: list[dict] = json.load(f)
    print(f"  {len(lineages)} lineages loaded.", flush=True)

    print("Building indexes ...", flush=True)
    from lib.scoring import build_indexes as _bi
    idxs = _bi(lineages)
    print(
        f"  id_index: {len(idxs['id_index'])}  "
        f"basename_index: {len(idxs['basename_index'])}  "
        f"path_index: {len(idxs['path_index'])}",
        flush=True,
    )

    print("Analyzing non-head lineages ...", flush=True)
    results, skipped_known_deleted = analyze(
        lineages,
        skip_known_deleted_no_candidates=args.skip_known_deleted_no_candidates,
    )

    print(f"Writing JSON -> {out_json} ...", flush=True)
    write_json(results, out_json)

    print_summary(results, skipped_known_deleted)


if __name__ == "__main__":
    main()
