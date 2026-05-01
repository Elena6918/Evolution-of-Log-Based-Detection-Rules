#!/usr/bin/env python3
"""
Stage 2: Semantic lineage construction with explicit evolution classification
and time-correct canonical naming.

Usage:
    python build_semantic_lineage_metadata.py --repo-type sigma
    python build_semantic_lineage_metadata.py --repo-type ssc

Both repos share the same raw -> semantic pipeline.

Default inputs per repo-type:
    sigma: lineage_metadata_raw_sigma.json
    ssc:   lineage_metadata_raw_ssc.json

Outputs:
    lineage_metadata_{sigma|ssc}.json
    lineage_split_relationships_{sigma|ssc}.json

Key classifications:
    single_or_no_id
    id_introduction
    duplicate_fix_single_survivor
    sequential_id_replacement
    semantic_path_divergence
    parallel_id_overlap
    transient_id_singleton_with_revert
    same_exact_path_multi_id             

Lifecycle policy (canonical-only):
    exists_in_head: True iff canonical_name (latest path_used) exists at HEAD.
    If exists_in_head True  -> deleted_* = None
    If exists_in_head False -> deleted_* from canonical path's deletion commit (git log --follow --diff-filter=D)
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import RepoConfig


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
    )
    ap.add_argument(
        "--input", default=None,
        help="Override input file (default: repo-type-specific stage3_in)",
    )
    ap.add_argument(
        "--output", default=None,
        help="Override lineage output file (default: repo-type-specific stage3_out)",
    )
    ap.add_argument(
        "--splits-output", default=None,
        help="Override split relationship output file (default: repo-type-specific stage3_splits)",
    )
    ap.add_argument(
        "--enable-sc-guards",
        action="store_true",
        help="Enable same_exact_path_multi_id guard (on by default for both repos; flag kept for explicit override).",
    )
    return ap.parse_args()


# =====================================================
# CONSTANTS
# =====================================================

DUP_FIX_RE = re.compile(r"fix.*duplicate.*(id|uuid)", re.IGNORECASE)
REVERT_RE   = re.compile(r"\b(revert|rollback|roll back|undo)\b", re.IGNORECASE)

TRANSIENT_SINGLETON_MAX_COMMITS  = 1
TRANSIENT_NEARBY_TIME_WINDOW     = timedelta(hours=24)
TRANSIENT_NEARBY_INDEX_WINDOW    = 3


# =====================================================
# GIT HELPERS
# =====================================================

def run_git(repo_root: Path, args: Sequence[str]) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def git_path_exists_at_head(repo_root: Path, path: str) -> bool:
    if not path:
        return False
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{path}"],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def git_deletion_info_follow(repo_root: Path, path: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (deleted_commit, deleted_date_iso) or (None, None)."""
    if not path:
        return None, None
    try:
        out = run_git(repo_root, [
            "log", "--follow", "--diff-filter=D", "--format=%H|%cI", "-n", "1", "--", path,
        ])
    except subprocess.CalledProcessError:
        return None, None
    if not out:
        return None, None
    parts = out.split("|", 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


# =====================================================
# GENERIC HELPERS
# =====================================================

def parse_date_loose(s: str) -> datetime:
    if s is None:
        raise ValueError("parse_date_loose got None")
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_z(dt: datetime) -> str:
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    return s


def dedupe_commits(commits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for c in commits:
        key = (c.get("hash"), c.get("path_used"), c.get("id"), c.get("date"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def first_date(commits: List[Dict[str, Any]]) -> datetime:
    return min(parse_date_loose(c["date"]) for c in commits)


def last_date(commits: List[Dict[str, Any]]) -> datetime:
    return max(parse_date_loose(c["date"]) for c in commits)


def canonical_from_commits(commits: List[Dict[str, Any]]) -> Optional[str]:
    if not commits:
        return None
    last = max(commits, key=lambda c: parse_date_loose(c["date"]))
    return last.get("path_used")


def ids_after(commits: List[Dict[str, Any]], cutoff: Optional[datetime]) -> set:
    out = set()
    if cutoff is None:
        return out
    for c in commits:
        if parse_date_loose(c["date"]) > cutoff and c.get("id"):
            out.add(c["id"])
    return out


def ranges_overlap(r1, r2) -> bool:
    return not (r1[1] < r2[0] or r2[1] < r1[0])


def path_families(commits: List[Dict[str, Any]], depth: int = 3) -> Dict[str, set]:
    fam = defaultdict(set)
    for c in commits:
        if c.get("id") and c.get("path_used"):
            parts = c["path_used"].split("/")
            fam[c["id"]].add("/".join(parts[:depth]))
    return fam


def derive_lifecycle_from_git_canonical_only(
    repo_root: Path, canonical_path: Optional[str]
) -> Tuple[bool, Optional[str], Optional[str]]:
    if not canonical_path:
        return False, None, None
    exists = git_path_exists_at_head(repo_root, canonical_path)
    if exists:
        return True, None, None
    dc, dd = git_deletion_info_follow(repo_root, canonical_path)
    return False, dc, dd


# =====================================================
# SSC-SPECIFIC GUARD: same_exact_path_multi_id
# =====================================================

def _exact_paths_by_id(commits: List[Dict[str, Any]]) -> Dict[str, set]:
    out: Dict[str, set] = defaultdict(set)
    for c in commits:
        rid = c.get("id")
        p = c.get("path_used")
        if rid and p:
            out[rid].add(p)
    return out


def all_ids_share_same_exact_paths(
    commits: List[Dict[str, Any]]
) -> Tuple[bool, Dict[str, List[str]]]:
    """
    Returns True iff every ID with at least one path_used has the exact same
    non-empty set of paths. Used as a hard guard against false splits in SSC.
    """
    by_id = _exact_paths_by_id(commits)
    nonempty = {rid: paths for rid, paths in by_id.items() if paths}
    if len(nonempty) <= 1:
        return False, {rid: sorted(paths) for rid, paths in by_id.items()}
    path_sets = list(nonempty.values())
    same = all(path_sets[i] == path_sets[0] for i in range(1, len(path_sets)))
    return same, {rid: sorted(paths) for rid, paths in by_id.items()}


# =====================================================
# TRANSIENT SINGLETON ID DETECTION
# =====================================================

def detect_transient_singleton_id_with_revert(
    commits_sorted: List[Dict[str, Any]],
    id_commits: Dict[str, List[Dict[str, Any]]],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    singletons = [rid for rid, cs in id_commits.items() if len(cs) <= TRANSIENT_SINGLETON_MAX_COMMITS]
    if len(singletons) != 1:
        return False, None

    transient_id = singletons[0]
    transient_commit = id_commits[transient_id][0]
    t_transient = parse_date_loose(transient_commit["date"])

    other_ids = [rid for rid in id_commits.keys() if rid != transient_id]
    if not other_ids:
        return False, None

    other_times = [parse_date_loose(c["date"]) for rid in other_ids for c in id_commits[rid]]
    nearest_dt = min((abs(t - t_transient) for t in other_times), default=None)
    if nearest_dt is None or nearest_dt > TRANSIENT_NEARBY_TIME_WINDOW:
        return False, None

    def same_commit(a, b) -> bool:
        return (
            a.get("hash") == b.get("hash")
            and a.get("date") == b.get("date")
            and a.get("path_used") == b.get("path_used")
            and a.get("id") == b.get("id")
        )

    idx = None
    for i, c in enumerate(commits_sorted):
        if same_commit(c, transient_commit):
            idx = i
            break
    if idx is None:
        for i, c in enumerate(commits_sorted):
            if c.get("hash") == transient_commit.get("hash"):
                idx = i
                break
    if idx is None:
        return False, None

    lo = max(0, idx - TRANSIENT_NEARBY_INDEX_WINDOW)
    hi = min(len(commits_sorted), idx + TRANSIENT_NEARBY_INDEX_WINDOW + 1)
    window = commits_sorted[lo:hi]
    window_text = " ".join((c.get("subject") or "") for c in window)

    if not REVERT_RE.search(window_text):
        return False, None

    def score_survivor(rid: str):
        cs = id_commits[rid]
        return (len(cs), max(parse_date_loose(c["date"]) for c in cs))

    surviving_id = max(other_ids, key=score_survivor)
    info = {
        "surviving_id": surviving_id,
        "transient_id": transient_id,
        "nearest_other_time_delta_seconds": nearest_dt.total_seconds(),
        "transient_commit": {
            "hash": transient_commit.get("hash"),
            "date": transient_commit.get("date"),
            "subject": transient_commit.get("subject"),
            "path_used": transient_commit.get("path_used"),
        },
        "revert_window_subjects": [
            {"hash": c.get("hash"), "date": c.get("date"), "subject": c.get("subject")}
            for c in window if c.get("subject")
        ],
    }
    return True, info


# =====================================================
# CLASSIFICATION
# =====================================================

def analyze_component(component: Dict[str, Any], enable_sc_guards: bool) -> Dict[str, Any]:
    commits = dedupe_commits(component.get("commits", []))
    commits_sorted = sorted(commits, key=lambda c: parse_date_loose(c["date"])) if commits else []

    id_commits: Dict[str, list] = defaultdict(list)
    for c in commits_sorted:
        if c.get("id"):
            id_commits[c["id"]].append(c)

    pre_id_commits = [c for c in commits_sorted if not c.get("id")]
    has_id_introduction = bool(pre_id_commits) and bool(id_commits)

    if len(id_commits) <= 1:
        cls = "id_introduction" if has_id_introduction else "single_or_no_id"
        return {
            "decision": "no_split",
            "classification": cls,
            "id_introduction": has_id_introduction,
            "pre_id_commit_count": len(pre_id_commits),
            "commits": commits_sorted,
        }

    # SSC guard: if all IDs share the exact same path history, do not split.
    if enable_sc_guards:
        same_exact_paths, exact_path_evidence = all_ids_share_same_exact_paths(commits_sorted)
        if same_exact_paths:
            return {
                "decision": "collapse",
                "classification": "same_exact_path_multi_id",
                "exact_paths_by_id": exact_path_evidence,
                "id_introduction": has_id_introduction,
                "pre_id_commit_count": len(pre_id_commits),
                "commits": commits_sorted,
            }

    id_ranges = {rid: (first_date(cs), last_date(cs)) for rid, cs in id_commits.items()}
    ranges = list(id_ranges.values())
    has_overlap = any(
        ranges_overlap(ranges[i], ranges[j])
        for i in range(len(ranges))
        for j in range(i + 1, len(ranges))
    )

    ok, info = detect_transient_singleton_id_with_revert(commits_sorted, id_commits)
    if ok:
        return {
            "decision": "collapse",
            "classification": "transient_id_singleton_with_revert",
            "surviving_id": info["surviving_id"],
            "transient_id": info["transient_id"],
            "evidence": info,
            "id_introduction": has_id_introduction,
            "pre_id_commit_count": len(pre_id_commits),
            "commits": commits_sorted,
            "id_ranges": {
                rid: {"start": id_ranges[rid][0].isoformat(), "end": id_ranges[rid][1].isoformat()}
                for rid in id_ranges
            },
        }

    families  = path_families(commits_sorted)
    fam_sets  = list(families.values())
    disjoint_families = True
    if len(fam_sets) >= 2:
        disjoint_families = all(
            fam_sets[i].isdisjoint(fam_sets[j])
            for i in range(len(fam_sets))
            for j in range(i + 1, len(fam_sets))
        )

    if disjoint_families and has_overlap:
        return {
            "decision": "split",
            "classification": "semantic_path_divergence",
            "ids": list(id_commits.keys()),
            "id_ranges": {
                rid: {"start": id_ranges[rid][0].isoformat(), "end": id_ranges[rid][1].isoformat()}
                for rid in id_ranges
            },
            "path_families": {k: sorted(v) for k, v in families.items()},
            "id_introduction": has_id_introduction,
            "pre_id_commit_count": len(pre_id_commits),
            "commits": commits_sorted,
        }

    if not has_overlap:
        return {
            "decision": "collapse",
            "classification": "sequential_id_replacement",
            "id_ranges": {
                rid: {"start": id_ranges[rid][0].isoformat(), "end": id_ranges[rid][1].isoformat()}
                for rid in id_ranges
            },
            "id_introduction": has_id_introduction,
            "pre_id_commit_count": len(pre_id_commits),
            "commits": commits_sorted,
        }

    fix_commits = [c for c in commits_sorted if DUP_FIX_RE.search(c.get("subject") or "")]
    last_fix_time = max((parse_date_loose(c["date"]) for c in fix_commits), default=None)
    surviving_ids = ids_after(commits_sorted, last_fix_time) if last_fix_time else set()

    if last_fix_time and len(surviving_ids) == 1:
        return {
            "decision": "collapse",
            "classification": "duplicate_fix_single_survivor",
            "surviving_id": next(iter(surviving_ids)),
            "duplicate_fix_commits": [
                {"hash": c.get("hash"), "date": c.get("date"), "subject": c.get("subject")}
                for c in fix_commits
            ],
            "id_introduction": has_id_introduction,
            "pre_id_commit_count": len(pre_id_commits),
            "commits": commits_sorted,
        }

    return {
        "decision": "split",
        "classification": "parallel_id_overlap",
        "ids": list(id_commits.keys()),
        "id_ranges": {
            rid: {"start": id_ranges[rid][0].isoformat(), "end": id_ranges[rid][1].isoformat()}
            for rid in id_ranges
        },
        "id_introduction": has_id_introduction,
        "pre_id_commit_count": len(pre_id_commits),
        "commits": commits_sorted,
    }


# =====================================================
# BUILD LINEAGES
# =====================================================

def build_semantic_lineages(
    *,
    repo_type: str,
    input_path: Optional[Path] = None,
    out_lineage: Optional[Path] = None,
    out_splits: Optional[Path] = None,
    enable_sc_guards: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cfg  = RepoConfig(repo_type)

    input_path   = input_path if input_path else cfg.path("stage3_in")
    out_lineage  = out_lineage if out_lineage else cfg.path("stage3_out")
    out_splits   = out_splits if out_splits else cfg.path("stage3_splits")
    repo_root    = cfg.repo_root
    if enable_sc_guards is None:
        enable_sc_guards = True   # applied to both sigma and ssc

    merged = json.loads(input_path.read_text())

    lineage_out: List[Dict[str, Any]] = []
    split_log:   List[Dict[str, Any]] = []
    lineage_counter = 0

    def new_lineage_id() -> str:
        nonlocal lineage_counter
        lineage_counter += 1
        return f"lineage_{lineage_counter:05d}"

    for comp in merged:
        analysis = analyze_component(comp, enable_sc_guards=enable_sc_guards)
        commits  = analysis["commits"]
        if not commits:
            print({"component_id": comp.get("component_id"), "reason": "no_commits"})
            continue

        cls = analysis["classification"]

        # ---- SEQUENTIAL ID REPLACEMENT ----
        if cls == "sequential_id_replacement":
            lid = new_lineage_id()

            id_first_seen: Dict[str, datetime] = {}
            for c in commits:
                if c.get("id"):
                    id_first_seen.setdefault(c["id"], parse_date_loose(c["date"]))
            ordered_ids = [rid for rid, _ in sorted(id_first_seen.items(), key=lambda x: x[1])]

            all_paths = sorted({c.get("path_used") for c in commits if c.get("path_used")})
            canonical = canonical_from_commits(commits)
            exists_in_head, deleted_in_commit, deleted_date = derive_lifecycle_from_git_canonical_only(repo_root, canonical)

            lineage_out.append({
                "lineage_id": lid,
                "canonical_name": canonical,
                "all_paths": all_paths,
                "all_ids": ordered_ids,
                "commit_count": len(commits),
                "split_from": None,
                "first_commit_date": iso_z(first_date(commits)),
                "last_commit_date": iso_z(last_date(commits)),
                "exists_in_head": exists_in_head,
                "deleted_in_commit": deleted_in_commit,
                "deleted_date": deleted_date,
                "commits": commits,
            })

            split_log.append({
                "component_id": comp.get("component_id"),
                "decision": "no_split",
                "classification": cls,
                "id_introduction": analysis.get("id_introduction", False),
                "pre_id_commit_count": analysis.get("pre_id_commit_count", 0),
                "details": {"id_ranges": analysis.get("id_ranges")},
            })
            continue

        # ---- NO SPLIT / COLLAPSE ----
        if analysis["decision"] in {"no_split", "collapse"}:
            lid = new_lineage_id()

            all_paths = sorted({c.get("path_used") for c in commits if c.get("path_used")})
            canonical = canonical_from_commits(commits)
            exists_in_head, deleted_in_commit, deleted_date = derive_lifecycle_from_git_canonical_only(repo_root, canonical)

            lineage_out.append({
                "lineage_id": lid,
                "canonical_name": canonical,
                "all_paths": all_paths,
                "all_ids": sorted({c["id"] for c in commits if c.get("id")}),
                "commit_count": len(commits),
                "split_from": None,
                "first_commit_date": iso_z(first_date(commits)),
                "last_commit_date": iso_z(last_date(commits)),
                "exists_in_head": exists_in_head,
                "deleted_in_commit": deleted_in_commit,
                "deleted_date": deleted_date,
                "commits": commits,
            })

            log_entry: Dict[str, Any] = {
                "component_id": comp.get("component_id"),
                "decision": "no_split",
                "classification": cls,
                "id_introduction": analysis.get("id_introduction", False),
                "pre_id_commit_count": analysis.get("pre_id_commit_count", 0),
            }

            if cls == "transient_id_singleton_with_revert":
                log_entry["details"] = {
                    "surviving_id": analysis.get("surviving_id"),
                    "transient_id": analysis.get("transient_id"),
                    "evidence": analysis.get("evidence"),
                    "id_ranges": analysis.get("id_ranges"),
                }
            elif cls == "same_exact_path_multi_id":
                log_entry["details"] = {
                    "exact_paths_by_id": analysis.get("exact_paths_by_id"),
                }

            split_log.append(log_entry)
            continue

        # ---- TRUE SPLIT ----
        ancestor_lineage_id: Optional[str] = None

        fanout_time = min(parse_date_loose(r["start"]) for r in analysis["id_ranges"].values())
        ancestor_commits = [c for c in commits if parse_date_loose(c["date"]) < fanout_time]
        ancestor_has_any_id = any(c.get("id") for c in ancestor_commits)

        child_commits_by_id: Dict[str, List[Dict[str, Any]]] = {}
        child_first_time:    Dict[str, datetime] = {}
        child_paths_by_id:   Dict[str, set] = {}

        for rid in analysis["ids"]:
            cs = [c for c in commits if c.get("id") == rid]
            if not cs:
                continue
            child_commits_by_id[rid] = list(cs)
            child_first_time[rid]    = min(parse_date_loose(c["date"]) for c in cs)
            child_paths_by_id[rid]   = {c.get("path_used") for c in cs if c.get("path_used")}

        if ancestor_commits and ancestor_has_any_id:
            ancestor_lineage_id  = new_lineage_id()
            ancestor_paths       = sorted({c.get("path_used") for c in ancestor_commits if c.get("path_used")})
            ancestor_canonical   = canonical_from_commits(ancestor_commits)
            anc_exists, anc_del_commit, anc_del_date = derive_lifecycle_from_git_canonical_only(repo_root, ancestor_canonical)

            lineage_out.append({
                "lineage_id": ancestor_lineage_id,
                "canonical_name": ancestor_canonical,
                "all_paths": ancestor_paths,
                "all_ids": sorted({c["id"] for c in ancestor_commits if c.get("id")}),
                "commit_count": len(ancestor_commits),
                "split_from": None,
                "first_commit_date": iso_z(first_date(ancestor_commits)),
                "last_commit_date": iso_z(last_date(ancestor_commits)),
                "exists_in_head": anc_exists,
                "deleted_in_commit": anc_del_commit,
                "deleted_date": anc_del_date,
                "commits": ancestor_commits,
            })
        elif ancestor_commits:
            # Pre-ID-only ancestor history: attach to children without duplication
            for ac in sorted(ancestor_commits, key=lambda c: parse_date_loose(c["date"])):
                ap = ac.get("path_used")
                assigned: Optional[str] = None

                if ap:
                    matches = [rid for rid, paths in child_paths_by_id.items() if ap in paths]
                    if len(matches) == 1:
                        assigned = matches[0]

                if assigned is None:
                    t = parse_date_loose(ac["date"])
                    candidates = [(rid, abs(ft - t), ft >= t) for rid, ft in child_first_time.items()]
                    candidates.sort(key=lambda x: (not x[2], x[1]))
                    assigned = candidates[0][0] if candidates else None

                if assigned is not None:
                    child_commits_by_id[assigned].append(ac)

        emitted_children = []
        for rid, cs in child_commits_by_id.items():
            if not cs:
                continue
            cs_sorted     = sorted(dedupe_commits(cs), key=lambda c: parse_date_loose(c["date"]))
            child_paths   = sorted({c.get("path_used") for c in cs_sorted if c.get("path_used")})
            child_canonical = canonical_from_commits(cs_sorted)
            exists_in_head, deleted_in_commit, deleted_date = derive_lifecycle_from_git_canonical_only(repo_root, child_canonical)

            child_lid = new_lineage_id()
            emitted_children.append({"id": rid, "lineage_id": child_lid})

            lineage_out.append({
                "lineage_id": child_lid,
                "canonical_name": child_canonical,
                "all_paths": child_paths,
                "all_ids": [rid],
                "commit_count": len(cs_sorted),
                "split_from": ancestor_lineage_id,
                "first_commit_date": iso_z(first_date(cs_sorted)),
                "last_commit_date": iso_z(last_date(cs_sorted)),
                "exists_in_head": exists_in_head,
                "deleted_in_commit": deleted_in_commit,
                "deleted_date": deleted_date,
                "commits": cs_sorted,
            })

        split_log.append({
            "component_id": comp.get("component_id"),
            "decision": "split",
            "classification": cls,
            "ancestor_lineage": ancestor_lineage_id,
            "descendant_ids": analysis["ids"],
            "id_introduction": analysis.get("id_introduction", False),
            "pre_id_commit_count": analysis.get("pre_id_commit_count", 0),
            "evidence": {
                "id_ranges": analysis.get("id_ranges"),
                "path_families": analysis.get("path_families"),
            },
            "emitted_children": emitted_children,
        })

    cfg.build_data.mkdir(parents=True, exist_ok=True)
    out_lineage.parent.mkdir(parents=True, exist_ok=True)
    out_splits.parent.mkdir(parents=True, exist_ok=True)
    out_lineage.write_text(json.dumps(lineage_out, indent=2))
    out_splits.write_text(json.dumps(split_log, indent=2))

    print("Semantic lineage construction complete")
    print(f"  output lineages:   {len(lineage_out)}")
    print(f"  logged decisions:  {len(split_log)}")
    print(f"  lineage file:      {out_lineage}")
    print(f"  splits file:       {out_splits}")

    return lineage_out, split_log


def main():
    args = parse_args()

    build_semantic_lineages(
        repo_type=args.repo_type,
        input_path=Path(args.input) if args.input else None,
        out_lineage=Path(args.output) if args.output else None,
        out_splits=Path(args.splits_output) if args.splits_output else None,
        enable_sc_guards=True if args.enable_sc_guards else None,
    )


if __name__ == "__main__":
    main()
