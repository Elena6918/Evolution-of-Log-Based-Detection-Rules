#!/usr/bin/env python3
"""
make_entries_from_audit.py
==========================
Build a minimal pair-manifest JSON file from audit_results.jsonl.

Default subset:
    pred_agree == True
    pgir_pred_changed == True
    llm_pred_changed == True

Default output rows are minimal:
    {lineage_id, version_a, version_b}

Repo-oriented usage:
    python make_entries_from_audit.py --repo sigma
    python make_entries_from_audit.py --repo ssc

Explicit path usage:
    python make_entries_from_audit.py \
        --audit data_prep/llm_data/sigma/audit_results.jsonl \
        --outfile data_prep/llm_data/sigma/agreed_changed_entries.json \
        --include-repo
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
LLM_DATA = REPO_ROOT / "data_prep" / "llm_data"


def load_audit_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def is_agreed_changed(row: dict) -> bool:
    return (
        row.get("pred_agree") is True
        and row.get("pgir_pred_changed") is True
        and row.get("llm_pred_changed") is True
    )


def make_entry(row: dict, include_repo: bool) -> dict:
    entry = {
        "lineage_id": row["lineage_id"],
        "version_a": row["version_a"],
        "version_b": row["version_b"],
    }
    if include_repo:
        entry["repo"] = row["repo"]
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a minimal pair-manifest JSON from audit_results.jsonl."
    )
    parser.add_argument(
        "--audit",
        default=None,
        help="Path to audit_results.jsonl. If omitted with --repo, uses llm_data/{repo}/audit_results.jsonl.",
    )
    parser.add_argument(
        "--outfile",
        default=None,
        help="Output JSON path. If omitted with --repo, uses llm_data/{repo}/agreed_changed_entries.json.",
    )
    parser.add_argument(
        "--repo",
        choices=["sigma", "ssc"],
        default=None,
        help="Use repo-local llm_data/{repo} paths by default and restrict rows to that repo.",
    )
    parser.add_argument(
        "--include-repo",
        action="store_true",
        help="Include repo in each output entry. Default output is minimal.",
    )
    args = parser.parse_args()

    if args.repo is not None:
        default_audit = LLM_DATA / args.repo / "audit_results.jsonl"
        default_out = LLM_DATA / args.repo / "agreed_changed_entries.json"
    else:
        default_audit = LLM_DATA / "test" / "audit_results.jsonl"
        default_out = LLM_DATA / "test" / "agreed_changed_entries.json"

    audit_path = Path(args.audit) if args.audit else default_audit
    out_path = Path(args.outfile) if args.outfile else default_out

    if not audit_path.exists():
        raise FileNotFoundError(f"audit_results file not found: {audit_path}")

    rows = load_audit_rows(audit_path)
    kept = [row for row in rows if is_agreed_changed(row)]

    if args.repo is not None:
        kept = [row for row in kept if row.get("repo") == args.repo]

    entries = [make_entry(row, include_repo=args.include_repo) for row in kept]
    if args.include_repo:
        entries.sort(key=lambda r: (r["repo"], r["lineage_id"], r["version_a"], r["version_b"]))
    else:
        entries.sort(key=lambda r: (r["lineage_id"], r["version_a"], r["version_b"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)

    print(f"Loaded {len(rows):,} audit rows from {audit_path}")
    print(f"Kept   {len(entries):,} agreed-changed entries")
    if args.repo is not None:
        print(f"Repo   {args.repo}")
    print(f"Wrote entries JSON to {out_path}")


if __name__ == "__main__":
    main()
