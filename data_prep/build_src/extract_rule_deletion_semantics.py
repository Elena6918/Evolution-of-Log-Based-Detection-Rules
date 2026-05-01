#!/usr/bin/env python3
"""
Repo-level deletion semantics extractor (grouped by commit).

Groups deletions by deleted_in_commit so one record represents a batch edit.
Must be run from inside (or with CWD set to) the target upstream git repo,
or git commands must be invoked with -C <repo_root>.

Usage:
    python extract_rule_deletion_semantics.py --repo-type sigma
    python extract_rule_deletion_semantics.py --repo-type ssc

Input:
    lineage_metadata_patched_{sigma|ssc}.json

Output:
    rule_deletion_semantics_{sigma|ssc}.json
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import RepoConfig


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
    )
    ap.add_argument(
        "--input", default=None,
        help="Override input file (default: patched lineage for the repo-type)",
    )
    return ap.parse_args()


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def git(repo_root: Path, cmd: list) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root)] + cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def get_commit_metadata(repo_root: Path, commit_hash: str):
    fmt = "%H%n%an%n%ad%n%s%n%b"
    out = git(repo_root, ["show", "-s", f"--format={fmt}", commit_hash])
    lines = out.splitlines()
    if len(lines) < 4:
        return None
    return {
        "hash":    lines[0],
        "author":  lines[1],
        "date":    lines[2],
        "subject": lines[3],
        "body":    "\n".join(lines[4:]).strip(),
    }


def get_paths_touched(repo_root: Path, commit_hash: str) -> list:
    out = git(repo_root, ["show", "--name-only", "--pretty=format:", commit_hash])
    return [p for p in out.splitlines() if p.strip()]


def classify_reason(subject: str, body: str) -> str:
    text = f"{subject}\n{body}".lower()
    if "remove" in text or "removing" in text or "delete" in text or "fp" in text:
        return "remove"
    if "rename" in text or "normalization" in text:
        return "rename"
    if "duplicate" in text or "dedup" in text:
        return "deduplicate"
    if "deprecated" in text or "obsolete" in text:
        return "deprecate"
    if "cleanup" in text or "refactor" in text:
        return "refactor"
    return "other"


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    args = parse_args()
    cfg  = RepoConfig(args.repo_type)

    input_path  = Path(args.input) if args.input else cfg.path("patched")
    output_path = cfg.path("deletions")
    repo_root   = cfg.repo_root

    with open(input_path, "r") as f:
        data = json.load(f)

    deleted = [
        e for e in data
        if (not e.get("exists_in_head")) and e.get("deleted_in_commit")
    ]

    print(f"Processing {len(deleted)} deleted-rule entries...")

    by_commit: dict = defaultdict(list)
    for e in deleted:
        by_commit[e["deleted_in_commit"]].append(e)

    print(f"Found {len(by_commit)} unique deletion commits (batch groups).")

    results = []
    skipped_meta = 0

    for commit_hash, entries in by_commit.items():
        meta = get_commit_metadata(repo_root, commit_hash)
        if not meta:
            skipped_meta += 1
            continue

        touched_paths = get_paths_touched(repo_root, commit_hash)

        lineage_ids    = []
        canonical_names = []
        deleted_rules  = []

        for e in entries:
            lineage_ids.append(e.get("lineage_id"))
            canonical_names.append(e.get("canonical_name"))
            deleted_rules.append({
                "lineage_id":    e.get("lineage_id"),
                "canonical_name": e.get("canonical_name"),
                "deleted_date":  e.get("deleted_date"),
                "all_paths":     e.get("all_paths") or e.get("paths"),
                "all_ids":       e.get("all_ids"),
            })

        lineage_ids_sorted    = sorted({x for x in lineage_ids if x})
        canonical_names_sorted = sorted({x for x in canonical_names if x})
        deleted_rules_sorted  = sorted(
            deleted_rules,
            key=lambda r: ((r.get("lineage_id") or ""), (r.get("canonical_name") or "")),
        )

        results.append({
            "commit": {
                "hash":    commit_hash,
                "subject": meta["subject"],
                "author":  meta["author"],
                "date":    meta["date"],
                "body":    meta["body"],
                "deletion_reason_hint": classify_reason(meta["subject"], meta["body"]),
            },
            "batch": {
                "count":           len(entries),
                "lineage_ids":     lineage_ids_sorted,
                "canonical_names": canonical_names_sorted,
            },
            "touched_paths":  touched_paths,
            "deleted_rules":  deleted_rules_sorted,
        })

    results.sort(key=lambda r: (r["commit"]["date"], r["commit"]["hash"]))

    cfg.build_data.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    if skipped_meta:
        print(f"Warning: skipped {skipped_meta} commits due to missing metadata.")
    print(f"Wrote {len(results)} grouped deletion semantics to {output_path}")


if __name__ == "__main__":
    main()
