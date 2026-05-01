#!/usr/bin/env python3
"""
prepare_llm_prompts_from_pair_manifest.py
=========================================
Generate prompts for an explicit pair manifest rather than by walking the full
corpus adjacency. This keeps the experiment subset exact and also writes the
corresponding raw rule_versions endpoint records for inspection/reuse.

Input entries:
    A JSON array of objects containing either:
      {lineage_id, repo, version_a, version_b}
    or, when --repo is passed:
      {lineage_id, version_a, version_b}

    The script also accepts curated test-set rows such as
    data_prep/llm_data/test/test.json, as long as each row includes the
    required pair-identifying fields above. Any extra metadata fields are
    ignored.

Typical usage:
    python prepare_llm_prompts_from_pair_manifest.py \
      --entries data_prep/llm_data/test/agreed_changed_entries.json

    python prepare_llm_prompts_from_pair_manifest.py \
      --entries data_prep/llm_data/test/test.json

Outputs by default:
    data_prep/llm_data/test/pair_manifest_prompts.jsonl
    data_prep/llm_data/test/rule_versions_sigma_subset.jsonl
    data_prep/llm_data/test/rule_versions_ssc_subset.jsonl

Prompt content is specific to the pair-manifest workflow.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from prepare_llm_prompts import (
    BUILD_DATA,
    LLM_DATA,
    load_versions_index,
    make_commit_obj,
)


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


PAIR_MANIFEST_PROMPT_TEMPLATE = """You are analyzing predicate-level logic changes between two detection rule versions. Predicate logic is known to have changed. Characterize it using the schema below.

## Output (strict JSON)
{
  "from_commit": "<hash>",
  "to_commit": "<hash>",
  "match_set_direction": "broader | narrower | mixed | unclear",
  "predicate_modified_present": bool,
  "predicate_added": bool,
  "predicate_removed": bool,
  "summary": "<one sentence: observable logic change>",
  "rationale_label": "coverage_expansion | false_positive_reduction | mixed_tradeoff | insufficient_evidence",
  "rationale_confidence": "high | medium | low",
  "rationale_support": "<brief explanation grounded only in the observed pair>"
}

## Definitions
- predicate_modified_present: An existing predicate is rewritten in-place (field, operator, or value changed)
- predicate_added: Any new predicate introduced into the logic (including AND constraints or OR branches)
- predicate_removed: Any predicate removed from the logic (including AND constraints or OR branches)

## Rules
- Analyze predicate logic only (conditions, values, Boolean structure)
- Ignore formatting, macros, output fields, and pipeline mechanics
- Do not infer intent beyond the pair
- match_set_direction: broader = matches more; narrower = matches fewer; mixed = both; unclear = cannot determine
- rationale_label: be conservative; use insufficient_evidence if intent is ambiguous

## Example
Commit A:
(CommandLine contains whoami/systeminfo/&cd&echo) OR (CommandLine contains net AND user) OR (CommandLine contains cd AND /d) OR (CommandLine contains ping AND -n)

Commit B:
(Image endswith whoami.exe/systeminfo.exe) OR (Image endswith net.exe/net1.exe AND CommandLine contains user) OR (CommandLine contains cd AND /d) OR (Image endswith ping.exe AND CommandLine contains -n) OR (CommandLine contains &cd&echo)

Output:
{
  "from_commit": "A", "to_commit": "B",
  "match_set_direction": "mixed",
  "predicate_modified_present": true,
  "predicate_added": true,
  "predicate_removed": true,
  "summary": "Rewrites command-line predicates into executable-based conditions, adds new constraints, and introduces additional alternatives.",
  "rationale_label": "mixed_tradeoff",
  "rationale_confidence": "medium",
  "rationale_support": "The rule replaces broad command-line checks with more specific executable-based conditions while adding new alternatives."
}

## Now analyze:

### Commit A
__COMMIT_A__

### Commit B
__COMMIT_B__"""

def format_pair_manifest_prompt(commit_a: dict, commit_b: dict) -> str:
    """Render the pair-manifest prompt with the predicate-change schema."""
    return PAIR_MANIFEST_PROMPT_TEMPLATE.replace(
        "__COMMIT_A__",
        json.dumps(commit_a, indent=2),
    ).replace(
        "__COMMIT_B__",
        json.dumps(commit_b, indent=2),
    )


def load_entries(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"entries file must contain a JSON list: {path}")
    return data


def normalize_entries(entries: list[dict], default_repo: str | None) -> list[dict]:
    normalized: list[dict] = []
    required_fields = ("lineage_id", "version_a", "version_b")

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry at index {idx} must be a JSON object")

        missing = [field for field in required_fields if field not in entry]
        if missing:
            raise ValueError(
                f"entry at index {idx} is missing required field(s): {', '.join(missing)}"
            )

        repo = entry.get("repo", default_repo)
        if repo is None:
            raise ValueError(
                f"entry at index {idx} is missing 'repo'; pass --repo or include repo in each entry"
            )
        normalized.append({
            "lineage_id": entry["lineage_id"],
            "repo": repo,
            "version_a": entry["version_a"],
            "version_b": entry["version_b"],
        })
    return normalized


def build_repo_indices(entries: list[dict], build_data: Path) -> dict[str, dict[str, list[dict]]]:
    repos = sorted({entry["repo"] for entry in entries})
    return {repo: load_versions_index(repo, build_data) for repo in repos}


def collect_pair_records(
    entries: list[dict],
    build_data: Path,
    max_lines: int,
) -> tuple[list[dict], dict[str, list[dict]]]:
    indices = build_repo_indices(entries, build_data)

    prompt_records: list[dict] = []
    raw_subset_rows: dict[str, list[dict]] = {}
    raw_seen: dict[str, set[tuple[str, int]]] = {}

    for entry in entries:
        lid = entry["lineage_id"]
        repo = entry["repo"]
        va_i = entry["version_a"]
        vb_i = entry["version_b"]

        versions = indices.get(repo, {}).get(lid)
        if versions is None:
            print(
                f"  WARNING: lineage {lid!r} not found in {repo} index — skipped.",
                file=sys.stderr,
            )
            continue

        by_vi = {r["version_index"]: r for r in versions}
        va = by_vi.get(va_i)
        vb = by_vi.get(vb_i)
        if va is None or vb is None:
            print(
                f"  WARNING: {lid} v{va_i}→v{vb_i} missing in {repo} — skipped.",
                file=sys.stderr,
            )
            continue

        commit_a = make_commit_obj(va, max_lines)
        commit_b = make_commit_obj(vb, max_lines)
        prompt = format_pair_manifest_prompt(commit_a, commit_b)

        record = {
            "lineage_id": lid,
            "repo": repo,
            "rule_canonical": va.get("rule_canonical", "") or vb.get("rule_canonical", ""),
            "version_a": va_i,
            "version_b": vb_i,
            "commit_a": va.get("commit_hash", ""),
            "commit_b": vb.get("commit_hash", ""),
            "prompt": prompt,
        }

        prompt_records.append(record)

        raw_subset_rows.setdefault(repo, [])
        raw_seen.setdefault(repo, set())
        for ver_rec in (va, vb):
            key = (ver_rec["lineage_id"], ver_rec["version_index"])
            if key in raw_seen[repo]:
                continue
            raw_subset_rows[repo].append(ver_rec)
            raw_seen[repo].add(key)

    prompt_records.sort(key=lambda r: (r["repo"], r["lineage_id"], r["version_a"], r["version_b"]))
    for repo in raw_subset_rows:
        raw_subset_rows[repo].sort(key=lambda r: (r["lineage_id"], r["version_index"]))

    return prompt_records, raw_subset_rows


def write_prompt_jsonl(records: list[dict], outfile: Path) -> None:
    outfile.parent.mkdir(parents=True, exist_ok=True)
    with outfile.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def write_raw_subset_jsonl(raw_subset_rows: dict[str, list[dict]], outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for repo, rows in sorted(raw_subset_rows.items()):
        path = outdir / f"rule_versions_{repo}_subset.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare prompts from an explicit pair manifest and emit matching raw rule_versions rows."
    )
    parser.add_argument(
        "--entries",
        required=True,
        help=(
            "Path to a JSON pair list or curated test.json with lineage_id/version_a/"
            "version_b and optionally repo."
        ),
    )
    parser.add_argument(
        "--repo",
        choices=["sigma", "ssc"],
        default=None,
        help="Default repo to apply when entries omit the repo field.",
    )
    parser.add_argument(
        "--outfile",
        default=str(LLM_DATA / "test" / "pair_manifest_prompts.jsonl"),
        help="Prompt JSONL output path.",
    )
    parser.add_argument(
        "--raw-outdir",
        default=str(LLM_DATA / "test"),
        help="Directory for raw rule_versions_{repo}_subset.jsonl outputs.",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=80,
        help="Max lines per detection block before truncation (default: 80).",
    )
    parser.add_argument(
        "--build-data",
        default=str(BUILD_DATA),
        help=f"Override build_data directory (default: {BUILD_DATA}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary only; do not write outputs.",
    )
    args = parser.parse_args()

    entries_path = Path(args.entries)
    build_data = Path(args.build_data)
    outfile = Path(args.outfile)
    raw_outdir = Path(args.raw_outdir)

    if not entries_path.exists():
        parser.error(f"entries file not found: {entries_path}")

    raw_entries = load_entries(entries_path)
    entries = normalize_entries(raw_entries, args.repo)
    print(f"Loaded {len(entries):,} pair-manifest entries from {entries_path}")

    prompt_records, raw_subset_rows = collect_pair_records(entries, build_data, args.max_lines)

    print(f"Resolved {len(prompt_records):,} prompt pairs")
    for repo in sorted(raw_subset_rows):
        print(f"  {repo}: {len(raw_subset_rows[repo]):,} raw version rows")

    if args.dry_run:
        return

    write_prompt_jsonl(prompt_records, outfile)
    print(f"Wrote {len(prompt_records):,} prompt records to {outfile}")

    written_raw = write_raw_subset_jsonl(raw_subset_rows, raw_outdir)
    for path in written_raw:
        print(f"Wrote raw subset to {path}")


if __name__ == "__main__":
    main()
