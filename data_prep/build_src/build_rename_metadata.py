#!/usr/bin/env python3
"""
Stage 1 extractor: build noisy but lossless lineage metadata using ONLY git rename.

This stage emits *evidence*, not interpretation.

Usage:
    python build_rename_metadata.py --repo-type sigma
    python build_rename_metadata.py --repo-type ssc

Output:
    data_prep/build_data/lineage_metadata_raw_sigma.json
    data_prep/build_data/lineage_metadata_raw_ssc.json
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.adapters import get_adapter
from lib.config import RepoConfig

MAX_COMPONENTS = 100_000


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
        help="Which upstream repo to process",
    )
    return ap.parse_args()


# =====================================================
# TIME HANDLING
# =====================================================

def to_utc(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =====================================================
# GIT HELPERS
# =====================================================

def run_git(repo_root: Path, args: list) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_root)] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def get_commit_info(repo_root: Path, commit: str):
    out = run_git(repo_root, ["show", "-s", "--format=%H|%ai|%ci|%s", commit])
    if not out:
        return None
    h, author_raw, committer_raw, subject = out.split("|", 3)
    return {
        "hash": h,
        "date": to_utc(committer_raw),
        "author_date": to_utc(author_raw),
        "subject": subject,
    }


def get_file_at(repo_root: Path, commit: str, path: str) -> str:
    return run_git(repo_root, ["show", f"{commit}:{path}"])


# =====================================================
# STEP 1: BUILD RENAME GRAPH
# =====================================================

def build_rename_graph(repo_root: Path, adapter):
    log = run_git(repo_root, [
        "log", "--all", "--find-renames", "--name-status",
        "--diff-filter=AMDR", "--format=--COMMIT--%H",
    ])

    graph = defaultdict(set)        # undirected rename edges
    all_paths = set()               # all rule-like paths seen
    path_commits = defaultdict(set) # path -> commits where it appears

    current_commit = None

    for line in log.splitlines():
        if line.startswith("--COMMIT--"):
            current_commit = line.split("--COMMIT--", 1)[1]
            continue

        if not line or current_commit is None:
            continue

        parts = line.split("\t")

        # Added / modified / deleted: "A\tpath", "M\tpath", "D\tpath"
        if len(parts) == 2:
            _, p = parts
            if adapter.is_rule_file(p):
                all_paths.add(p)
                path_commits[p].add(current_commit)

        # Rename: "R100\told\tnew"
        elif len(parts) == 3 and parts[0].startswith("R"):
            old, new = parts[1], parts[2]
            if adapter.is_rule_file(old) and adapter.is_rule_file(new):
                graph[old].add(new)
                graph[new].add(old)
                all_paths.update([old, new])
                path_commits[old].add(current_commit)
                path_commits[new].add(current_commit)

    return graph, all_paths, path_commits


# =====================================================
# STEP 2: CONNECTED COMPONENTS
# =====================================================

def connected_components(graph, nodes):
    seen = set()
    components = []

    for n in nodes:
        if n in seen:
            continue
        stack = [n]
        comp = set()
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.add(x)
            for y in graph.get(x, []):
                stack.append(y)
        components.append(sorted(comp))

    return components


# =====================================================
# MAIN EXTRACTION
# =====================================================

def build_raw_metadata(cfg: RepoConfig):
    adapter = get_adapter(cfg)
    repo_root = cfg.repo_root

    graph, all_paths, path_commits = build_rename_graph(repo_root, adapter)
    components = connected_components(graph, all_paths)

    print(f"Found {len(components)} rename-connected components")

    raw_entries = []

    for idx, paths in enumerate(components[:MAX_COMPONENTS], 1):
        commits = sorted({c for p in paths for c in path_commits.get(p, [])})
        if not commits:
            continue

        commit_entries = []
        path_timelines = {}
        id_observations = defaultdict(list)

        for p in paths:
            path_timelines[p] = {
                "first_seen": None,
                "last_seen": None,
                "exists_in_head": (repo_root / p).exists(),
                "deleted_in_commit": None,
                "deleted_date": None,
            }

        for h in commits:
            info = get_commit_info(repo_root, h)
            if not info:
                continue

            # Find one path in this component that exists at this commit.
            for p in paths:
                content = get_file_at(repo_root, h, p)
                if not content:
                    continue

                rid = adapter.extract_id(p, content)

                commit_entries.append({
                    "hash": info["hash"],
                    "date": info["date"],
                    "author_date": info["author_date"],
                    "subject": info["subject"],
                    "path_used": p,
                    "id": rid,
                })

                tl = path_timelines[p]
                tl["first_seen"] = min(tl["first_seen"], info["date"]) if tl["first_seen"] else info["date"]
                tl["last_seen"]  = max(tl["last_seen"],  info["date"]) if tl["last_seen"]  else info["date"]

                if rid:
                    id_observations[rid].append({
                        "commit": info["hash"],
                        "date": info["date"],
                        "path": p,
                    })

                break  # one extant file per commit per component

        # HARD DELETE ONLY (diff-filter=D) per path
        for p in paths:
            out = run_git(repo_root, [
                "log", "-1", "--diff-filter=D", "--format=%H|%ci", "--", p
            ])
            if out:
                h_del, raw = out.split("|", 1)
                path_timelines[p]["deleted_in_commit"] = h_del
                path_timelines[p]["deleted_date"] = to_utc(raw)

        if not commit_entries:
            continue

        raw_entries.append({
            "component_id": f"rename_cc_{idx:06d}",
            "paths": paths,
            "commits": commit_entries,
            "path_timelines": path_timelines,
            "id_observations": id_observations,
        })

        if idx % 100 == 0:
            print(f"[{idx}/{len(components)}] processed")

    output_file = cfg.path("stage1_out")
    cfg.build_data.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(raw_entries, f, indent=2)

    print(f"\nSaved raw lineage evidence → {output_file}")
    print(f"Timestamp: {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    args = parse_args()
    cfg = RepoConfig(args.repo_type)
    build_raw_metadata(cfg)
