"""
merge_non_head_lineages.py
==========================
Merges lineage entries that represent the same logical detection rule but were
split into separate lineage objects (typically due to directory moves, format
conversions, or gaps in git rename tracking).

Supports both Sigma and SSC repos via --repo-type sigma|ssc.

STRATEGY
--------
1. Re-score every non-head lineage against all others using the same heuristics
   as analyze_non_head_lineage_candidates.py (shared IDs, basename similarity,
   shared historical paths, temporal proximity, commit keywords).
2. Build an undirected graph: edge (A, B) when score(A->B) >= MERGE_SCORE_THRESHOLD.
3. Run Union-Find to find connected components — handles transitivity automatically.
4. For each multi-member component, merge all entries into one.
5. Write results to lineage_metadata_final_{repo}.json + lineage_final_report_{repo}.json.

MERGE FIELD RULES
-----------------
  lineage_id       : kept from the member with the most-recent last_commit_date
  canonical_name   : path_used from the chronologically latest commit of the winner
  all_paths        : union of all all_paths across all members
  all_ids          : union of all all_ids across all members
  commits          : deduplicated union by hash, sorted ascending by date
  commit_count     : len(merged commits)
  first_commit_date: earliest commit date across all members
  last_commit_date : latest commit date across all members
  exists_in_head   : any(exists_in_head) across members
  deleted_in_commit: None if exists_in_head; else from member with latest deleted_date
  deleted_date     : same rule as deleted_in_commit

MULTI-HEAD CONSTRAINT
---------------------
Merges that would place two exists_in_head=True lineages in the same group are
blocked. The block is enforced transitively via Union-Find: once a group has an
in-head member, it cannot absorb another in-head member via any chain of merges.
If a UUID-conflict sweep (Pass 2) finds two in-head lineages with shared UUIDs,
the edge is blocked and logged in the report for analyst review.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make lib/ importable regardless of working directory
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.config import RepoConfig
from lib.scoring import (
    MERGE_SCORE_THRESHOLD,
    SCORE_SHARED_ID,
    build_indexes,
    gather_candidate_lids,
    isoformat,
    parse_date,
    score_pair,
    source_meta,
    all_paths_for,
)


# ---------------------------------------------------------------------------
# Union-Find (path-compressed, rank-union)
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent: dict[str, str] = {x: x for x in items}
        self.rank: dict[str, int] = {x: 0 for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> bool:
        px, py = self.find(x), self.find(y)
        if px == py:
            return False
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1
        return True

    def union_get_root(self, x: str, y: str) -> str | None:
        """Union x and y; return the new root, or None if already same group."""
        if not self.union(x, y):
            return None
        return self.find(x)

    def groups(self) -> dict[str, list[str]]:
        """Returns {root -> [members]} for all components."""
        result: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            result[self.find(x)].append(x)
        return dict(result)


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def _commit_sort_key(c: dict) -> datetime:
    dt = parse_date(c.get("date") or c.get("author_date"))
    return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)


def merge_group(entries: list[dict]) -> tuple[dict, list[str]]:
    """
    Merge a list of lineage entries into one canonical entry.
    Returns (merged_entry, list_of_absorbed_lineage_ids).

    Ordering contract
    -----------------
    The winner is determined first. All loops over entries process the winner's
    data before any absorbed entry's data, so winner's values always take
    precedence in hash-collision tie-breaking.

    Winner selection: most-recent last_commit_date, tiebreak most commits,
    final tiebreak lexicographically smallest lineage_id.

    canonical_name
    --------------
    Taken from the winner's most-recent commit, so the canonical name always
    reflects the in-head / most-authoritative lineage's path.
    """
    warnings: list[str] = []

    def winner_key(e: dict) -> tuple:
        ld = parse_date(e.get("last_commit_date"))
        ld_ts = ld.timestamp() if ld else 0.0
        cc = e.get("commit_count") or 0
        return (-ld_ts, -cc, e["lineage_id"])

    winner = min(entries, key=winner_key)
    absorbed_entries = [e for e in entries if e["lineage_id"] != winner["lineage_id"]]
    ordered_entries = [winner] + absorbed_entries  # winner-first for tie-breaking

    # ── Commit dedup (winner-first) ─────────────────────────────────────────
    seen_hashes: set[str] = set()
    all_commits: list[dict] = []
    for e in ordered_entries:
        for c in (e.get("commits") or []):
            h = c.get("hash")
            if h and h not in seen_hashes:
                seen_hashes.add(h)
                all_commits.append(c)
            elif not h:
                all_commits.append(c)
    all_commits.sort(key=_commit_sort_key)

    # ── canonical_name from winner's most-recent commit ─────────────────────
    winner_hash_set: set[str] = {
        c["hash"] for c in (winner.get("commits") or []) if c.get("hash")
    }
    canonical_name: str | None = None
    for c in reversed(all_commits):
        if c.get("hash") in winner_hash_set and c.get("path_used"):
            canonical_name = c["path_used"]
            break
    if canonical_name is None:
        for c in reversed(all_commits):
            if c.get("path_used"):
                canonical_name = c["path_used"]
                break

    # ── Union all_paths and all_ids (winner first) ───────────────────────────
    merged_paths: list[str] = []
    seen_paths: set[str] = set()
    merged_ids: list[str] = []
    seen_ids: set[str] = set()
    for e in ordered_entries:
        for p in (e.get("all_paths") or []):
            if p and p not in seen_paths:
                merged_paths.append(p)
                seen_paths.add(p)
        for uid in (e.get("all_ids") or []):
            if uid and uid not in seen_ids:
                merged_ids.append(uid)
                seen_ids.add(uid)
    if canonical_name and canonical_name not in seen_paths:
        merged_paths.append(canonical_name)
        seen_paths.add(canonical_name)

    # ── Dates ────────────────────────────────────────────────────────────────
    commit_dates = [
        parse_date(c.get("date") or c.get("author_date")) for c in all_commits
    ]
    commit_dates_valid = [d for d in commit_dates if d is not None]
    first_date = min(commit_dates_valid) if commit_dates_valid else None
    last_date  = max(commit_dates_valid) if commit_dates_valid else None

    for e in entries:
        fd = parse_date(e.get("first_commit_date"))
        ld = parse_date(e.get("last_commit_date"))
        if fd and (first_date is None or fd < first_date):
            first_date = fd
        if ld and (last_date is None or ld > last_date):
            last_date = ld

    # ── exists_in_head and deletion fields ───────────────────────────────────
    in_head_members = [e for e in entries if e.get("exists_in_head")]
    exists_in_head = len(in_head_members) > 0
    if len(in_head_members) > 1:
        warnings.append(
            f"multi_head_merge: {[e['lineage_id'] for e in in_head_members]} "
            f"all have exists_in_head=True"
        )

    if exists_in_head:
        deleted_in_commit = None
        deleted_date = None
    else:
        best_del_entry = None
        best_del_dt: datetime | None = None
        for e in entries:
            dt = parse_date(e.get("deleted_date"))
            if dt and (best_del_dt is None or dt > best_del_dt):
                best_del_dt = dt
                best_del_entry = e
        deleted_in_commit = best_del_entry.get("deleted_in_commit") if best_del_entry else None
        deleted_date = best_del_entry.get("deleted_date") if best_del_entry else None

    merged = {
        "lineage_id": winner["lineage_id"],
        "canonical_name": canonical_name,
        "all_paths": merged_paths,
        "all_ids": merged_ids,
        "commit_count": len(all_commits),
        "split_from": winner.get("split_from"),
        "first_commit_date": isoformat(first_date),
        "last_commit_date": isoformat(last_date),
        "exists_in_head": exists_in_head,
        "deleted_in_commit": deleted_in_commit,
        "deleted_date": deleted_date,
        "commits": all_commits,
        "_merge_warnings": warnings if warnings else None,
    }
    absorbed = [e["lineage_id"] for e in absorbed_entries]
    return merged, absorbed


def _split_manual_ssc_exception(
    merged_lineages: list[dict],
    report_groups: list[dict],
    total_warnings: list[str],
    by_lid: dict[str, dict],
) -> tuple[list[dict], list[dict], list[str]]:
    """
    Manual analyst exception for SSC lineage_02642.

    The default merge graph correctly links the broader family of
    "attempted_credential_dump_from_registry_via_reg_exe" detections, but the
    final group mixes two distinct analytic IDs:
      - e9fb4a59-c5fb-440a-9f24-191fbc6b2911
      - 14038953-e5f2-4daf-acff-5452062baf03

    Keep the current lineage_02642 branch for the e9fb... lineage and split the
    140389... branch back out into its own merged lineage.
    """
    target_report = next(
        (g for g in report_groups if g["winner_lineage_id"] == "lineage_02642"),
        None,
    )
    if target_report is None:
        return merged_lineages, report_groups, total_warnings

    target_entry = next(
        (e for e in merged_lineages if e["lineage_id"] == "lineage_02642"),
        None,
    )
    if target_entry is None:
        return merged_lineages, report_groups, total_warnings

    all_ids = set(target_entry.get("all_ids") or [])
    expected_ids = {
        "e9fb4a59-c5fb-440a-9f24-191fbc6b2911",
        "14038953-e5f2-4daf-acff-5452062baf03",
    }
    if all_ids != expected_ids:
        return merged_lineages, report_groups, total_warnings

    split_groups = [
        ["lineage_02642", "lineage_04958"],
        [
            "lineage_02543",
            "lineage_02641",
            "lineage_02833",
            "lineage_02949",
            "lineage_04886",
            "lineage_04902",
            "lineage_05255",
        ],
    ]

    new_entries: list[dict] = []
    new_reports: list[dict] = []
    new_warnings = list(total_warnings)

    for member_ids in split_groups:
        entries = [by_lid[lid] for lid in member_ids]
        merged_entry, absorbed = merge_group(entries)
        warnings = merged_entry.pop("_merge_warnings", None) or []
        new_warnings.extend(warnings)
        new_entries.append(merged_entry)
        new_reports.append({
            "winner_lineage_id": merged_entry["lineage_id"],
            "merged_canonical_name": merged_entry["canonical_name"],
            "member_count": len(member_ids),
            "members": sorted(member_ids),
            "absorbed": sorted(absorbed),
            "exists_in_head": merged_entry["exists_in_head"],
            "warnings": warnings,
            "edges": [],
            "manual_exception": "split lineage_02642 by distinct analytic ID",
        })

    merged_lineages = [
        e for e in merged_lineages if e["lineage_id"] != "lineage_02642"
    ] + new_entries
    report_groups = [
        g for g in report_groups if g["winner_lineage_id"] != "lineage_02642"
    ] + new_reports

    target_report = next(
        (g for g in report_groups if g["winner_lineage_id"] == "lineage_02736"),
        None,
    )
    if target_report is None:
        return merged_lineages, report_groups, new_warnings

    target_entry = next(
        (e for e in merged_lineages if e["lineage_id"] == "lineage_02736"),
        None,
    )
    if target_entry is None:
        return merged_lineages, report_groups, new_warnings

    all_paths = target_entry.get("all_paths") or []
    has_previously_seen = any("previously_seen" in p for p in all_paths)
    has_first_time = any("first_time" in p for p in all_paths)
    if not (has_previously_seen and has_first_time):
        return merged_lineages, report_groups, new_warnings

    split_groups = [
        ["lineage_02736", "lineage_03008", "lineage_03176", "lineage_03442"],
        ["lineage_00532", "lineage_01831", "lineage_02236"],
    ]

    second_new_entries: list[dict] = []
    second_new_reports: list[dict] = []
    for member_ids in split_groups:
        entries = [by_lid[lid] for lid in member_ids]
        merged_entry, absorbed = merge_group(entries)
        warnings = merged_entry.pop("_merge_warnings", None) or []
        new_warnings.extend(warnings)
        second_new_entries.append(merged_entry)
        second_new_reports.append({
            "winner_lineage_id": merged_entry["lineage_id"],
            "merged_canonical_name": merged_entry["canonical_name"],
            "member_count": len(member_ids),
            "members": sorted(member_ids),
            "absorbed": sorted(absorbed),
            "exists_in_head": merged_entry["exists_in_head"],
            "warnings": warnings,
            "edges": [],
            "manual_exception": "split lineage_02736 by path family: previously_seen vs first_time",
        })

    merged_lineages = [
        e for e in merged_lineages if e["lineage_id"] != "lineage_02736"
    ] + second_new_entries
    report_groups = [
        g for g in report_groups if g["winner_lineage_id"] != "lineage_02736"
    ] + second_new_reports

    return merged_lineages, report_groups, new_warnings


# ---------------------------------------------------------------------------
# Edge building
# ---------------------------------------------------------------------------

def build_merge_edges(
    lineages: list[dict],
    indexes: dict,
) -> list[tuple[str, str, float, list[dict]]]:
    """
    Score candidate pairs and return edges (lid_a, lid_b, score, evidence)
    for score >= MERGE_SCORE_THRESHOLD.

    Two passes:
      Pass 1 — non-head lineages scored against their candidate pool.
      Pass 2 — UUID-conflict sweep for all lineages (catches in-head vs in-head
               UUID conflicts that Pass 1 misses).
    """
    edges: list[tuple[str, str, float, list[dict]]] = []
    seen: set[frozenset] = set()

    by_lid = indexes["by_lid"]

    # --- Pass 1: non-head lineages -------------------------------------------
    non_head = [e for e in lineages if not e.get("exists_in_head", True)]
    for source in non_head:
        src_lid = source["lineage_id"]
        smeta = source_meta(source)
        cand_lids = gather_candidate_lids(smeta, src_lid, indexes)

        for cand_lid in cand_lids:
            pair = frozenset((src_lid, cand_lid))
            if pair in seen:
                continue
            seen.add(pair)
            cand = by_lid.get(cand_lid)
            if cand is None:
                continue
            sc, ev = score_pair(smeta, cand)
            if sc >= MERGE_SCORE_THRESHOLD:
                edges.append((src_lid, cand_lid, sc, ev))

    # --- Pass 2: UUID-conflict sweep (all head statuses) ---------------------
    for uid, lids in indexes["id_index"].items():
        if len(lids) < 2:
            continue
        for i in range(len(lids)):
            for j in range(i + 1, len(lids)):
                pair = frozenset((lids[i], lids[j]))
                if pair in seen:
                    continue
                seen.add(pair)
                a_entry = by_lid.get(lids[i])
                b_entry = by_lid.get(lids[j])
                if a_entry and b_entry:
                    a_meta = source_meta(a_entry)
                    full_sc, full_ev = score_pair(a_meta, b_entry)
                    edges.append((lids[i], lids[j], full_sc, full_ev))
                else:
                    ev = [{"signal": "shared_id", "ids": [uid]}]
                    edges.append((lids[i], lids[j], float(SCORE_SHARED_ID), ev))

    return edges


# ---------------------------------------------------------------------------
# Main merge pipeline
# ---------------------------------------------------------------------------

def run_merge(lineages: list[dict], repo_type: str) -> tuple[list[dict], dict]:
    """Full merge pipeline. Returns (merged_lineage_list, report_dict)."""
    all_lids = [e["lineage_id"] for e in lineages]
    uf = UnionFind(all_lids)

    print("Building indexes ...", flush=True)
    indexes = build_indexes(lineages)
    print(
        f"  id_index: {len(indexes['id_index'])}  "
        f"basename_index: {len(indexes['basename_index'])}  "
        f"path_index: {len(indexes['path_index'])}",
        flush=True,
    )

    print("Scoring candidates and building merge edges ...", flush=True)
    edges = build_merge_edges(lineages, indexes)
    print(f"  {len(edges)} edges at threshold >= {MERGE_SCORE_THRESHOLD}", flush=True)

    by_lid = indexes["by_lid"]

    # Constrained union: track which group roots already contain an in-head member.
    # A union is allowed only when at most one of the two groups has an in-head member.
    in_head_ids: set[str] = {e["lineage_id"] for e in lineages if e.get("exists_in_head")}
    head_roots: set[str] = set(in_head_ids)  # root == lineage_id initially
    blocked_pairs: list[dict] = []

    for lid_a, lid_b, sc, ev in edges:
        ra, rb = uf.find(lid_a), uf.find(lid_b)
        if ra == rb:
            continue

        if ra in head_roots and rb in head_roots:
            blocked_pairs.append({
                "lid_a": lid_a, "lid_b": lid_b,
                "score": sc, "evidence": ev,
                "reason": "both_groups_have_in_head_member",
            })
            continue

        a_had_head = ra in head_roots
        b_had_head = rb in head_roots
        new_root = uf.union_get_root(lid_a, lid_b)
        head_roots.discard(ra)
        head_roots.discard(rb)
        if a_had_head or b_had_head:
            head_roots.add(new_root)  # type: ignore[arg-type]

    groups = uf.groups()
    multi     = {r: m for r, m in groups.items() if len(m) > 1}
    singletons = {r: m for r, m in groups.items() if len(m) == 1}

    print(
        f"  {len(multi)} merge groups (involving "
        f"{sum(len(m) for m in multi.values())} lineages); "
        f"{len(blocked_pairs)} edge(s) blocked (in-head conflict); "
        f"{len(singletons)} singletons unchanged",
        flush=True,
    )

    # Sanity-check: no group should contain two in-head members
    invariant_violations = 0
    for root, members in multi.items():
        ih = [lid for lid in members if by_lid[lid].get("exists_in_head")]
        if len(ih) > 1:
            print(
                f"  [BUG] Group rooted at {root} has multiple in-head members: {ih}",
                file=sys.stderr,
            )
            invariant_violations += 1
    if invariant_violations == 0:
        print("  Invariant OK: no group contains two in-head members.", flush=True)

    edge_lookup: dict[frozenset, tuple[float, list]] = {
        frozenset((a, b)): (s, ev) for a, b, s, ev in edges
    }

    merged_lineages: list[dict] = []
    report_groups: list[dict] = []
    total_warnings: list[str] = []

    # --- Multi-member groups: merge ──────────────────────────────────────────
    for root, members in multi.items():
        entries = [by_lid[lid] for lid in members]
        group_edges = []
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                ev_data = edge_lookup.get(frozenset((a, b)))
                if ev_data:
                    group_edges.append({
                        "lid_a": a, "lid_b": b,
                        "score": ev_data[0], "evidence": ev_data[1],
                    })
        merged_entry, absorbed = merge_group(entries)
        warnings = merged_entry.pop("_merge_warnings", None) or []
        total_warnings.extend(warnings)
        merged_lineages.append(merged_entry)
        report_groups.append({
            "winner_lineage_id": merged_entry["lineage_id"],
            "merged_canonical_name": merged_entry["canonical_name"],
            "member_count": len(members),
            "members": sorted(members),
            "absorbed": sorted(absorbed),
            "exists_in_head": merged_entry["exists_in_head"],
            "warnings": warnings,
            "edges": group_edges,
        })

    # --- Singleton groups: pass through unchanged ────────────────────────────
    for root, members in singletons.items():
        merged_lineages.append(by_lid[members[0]])

    if repo_type == "ssc":
        merged_lineages, report_groups, total_warnings = _split_manual_ssc_exception(
            merged_lineages, report_groups, total_warnings, by_lid
        )

    merged_lineages.sort(key=lambda e: e["lineage_id"])

    report = {
        "summary": {
            "input_lineage_count": len(lineages),
            "output_lineage_count": len(merged_lineages),
            "merge_groups": len(multi),
            "lineages_absorbed": sum(len(g["absorbed"]) for g in report_groups),
            "blocked_edges": len(blocked_pairs),
            "merge_edges_accepted": len(edges) - len(blocked_pairs),
            "total_warnings": len(total_warnings),
            "threshold_used": MERGE_SCORE_THRESHOLD,
        },
        "warnings": total_warnings,
        "groups": report_groups,
        "blocked_pairs": blocked_pairs,
    }

    return merged_lineages, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Merge non-head lineage entries that represent the same logical rule."
    )
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
        help="Which repo to process (sigma or ssc).",
    )
    ap.add_argument(
        "--input", default=None,
        help="Override input lineage JSON path (default: build_data/lineage_metadata_{repo}.json).",
    )
    ap.add_argument(
        "--output", default=None,
        help="Override output merged lineage JSON path.",
    )
    ap.add_argument(
        "--report", default=None,
        help="Override output merge report JSON path.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = RepoConfig(args.repo_type)

    in_path    = Path(args.input)   if args.input   else cfg.path("stage3_out")
    out_final  = Path(args.output)  if args.output  else cfg.path("lineage_final")
    out_report = Path(args.report)  if args.report  else cfg.path("lineage_final_report")

    if not in_path.exists():
        print(f"ERROR: input not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {in_path} ...", flush=True)
    with open(in_path, encoding="utf-8") as f:
        lineages: list[dict] = json.load(f)
    print(f"  {len(lineages)} lineages loaded.", flush=True)

    merged_lineages, report = run_merge(lineages, args.repo_type)

    out_final.parent.mkdir(parents=True, exist_ok=True)
    out_report.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nWriting final lineage metadata -> {out_final} ...", flush=True)
    with open(out_final, "w", encoding="utf-8") as f:
        json.dump(merged_lineages, f, indent=2, default=str)

    print(f"Writing final report           -> {out_report} ...", flush=True)
    with open(out_report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    s = report["summary"]
    print("\n" + "=" * 60)
    print("FINAL LINEAGE SUMMARY")
    print("=" * 60)
    print(f"  Input lineages          : {s['input_lineage_count']}")
    print(f"  Output lineages         : {s['output_lineage_count']}")
    print(f"  Reduction               : {s['input_lineage_count'] - s['output_lineage_count']}")
    print(f"  Merge groups            : {s['merge_groups']}")
    print(f"  Lineages absorbed       : {s['lineages_absorbed']}")
    print(f"  Edges accepted          : {s['merge_edges_accepted']}")
    print(f"  Edges blocked (in-head) : {s['blocked_edges']}")
    print(f"  Merge warnings          : {s['total_warnings']}")
    print(f"  Score threshold used    : {s['threshold_used']}")
    print("=" * 60)

    if report["warnings"]:
        print("\nWARNINGS (requires analyst review):")
        for w in report["warnings"][:20]:
            print(f"  ! {w}")
        if len(report["warnings"]) > 20:
            print(f"  ... and {len(report['warnings']) - 20} more (see {out_report})")

    print("\nSample merge groups (up to 5):")
    sample = sorted(report["groups"], key=lambda g: -g["member_count"])[:5]
    for g in sample:
        print(f"  {g['winner_lineage_id']}  ({g['member_count']} members)")
        print(f"    canonical: {g['merged_canonical_name']}")
        print(f"    absorbed : {g['absorbed']}")
        if g["warnings"]:
            print(f"    WARNINGS : {g['warnings']}")


if __name__ == "__main__":
    main()
