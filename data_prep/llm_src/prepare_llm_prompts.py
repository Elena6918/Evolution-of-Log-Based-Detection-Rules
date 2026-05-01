#!/usr/bin/env python3
"""
prepare_llm_prompts.py
======================
Generate LLM-ready prompt records for detection-rule version-pair diffs.

Data source:
    data_prep/build_data/rule_versions_{repo}.jsonl

Output (one JSONL record per consecutive version pair):
    data_prep/llm_data/{repo}/prompts.jsonl       -- full corpus mode
    data_prep/llm_data/test/prompts.jsonl         -- curated-entries mode

Each output record:
    {lineage_id, repo, rule_canonical,
     version_a, version_b, commit_a, commit_b,
     prompt}

Usage:
    # Full corpus (all non-noop pairs)
    python prepare_llm_prompts.py --repo sigma
    python prepare_llm_prompts.py --repo ssc
    python prepare_llm_prompts.py --repo both

    # Curated test list  →  llm_data/test/prompts.jsonl
    python prepare_llm_prompts.py --entries ../llm_data/test/test.json

    # Limit / dry-run
    python prepare_llm_prompts.py --repo sigma --limit 200
    python prepare_llm_prompts.py --repo sigma --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ── Repo root resolution (works whether called from anywhere) ──────────────
_HERE      = Path(__file__).resolve().parent          # data_prep/llm_src/
_REPO_ROOT = _HERE.parent.parent                      # repo root
BUILD_DATA = _REPO_ROOT / "data_prep" / "build_data"
LLM_DATA   = _REPO_ROOT / "data_prep" / "llm_data"

# ── Prompt template (constant instructions) ────────────────────────────────
# The two ~~~json blocks at the end are filled in per pair.
PROMPT_INSTRUCTIONS = """\
You are analyzing how a detection rule's predicate-level logic changed between two adjacent commits.

Your task is to compare the two rule versions and return a structured JSON description of:
(1) whether the matched event set changed,
(2) how it changed at an observable level, and
(3) a conservative inferred rationale.

Important constraints:
- Focus only on predicate-level detection logic: conditions, keywords, values, Boolean structure, and other changes that affect which events match.
- Ignore formatting, comments, metadata, field ordering, whitespace, and other presentation-only changes.
- Do not use repository history, commit history beyond this pair, or external assumptions about developer intent.
- Do not infer reverts, rollbacks, or long-term maintenance behavior from a single pair.
- Be conservative: if the pair does not strongly support an inferred rationale, use "insufficient_evidence".

Return JSON strictly in this format:

{
  "from_commit": "<hash>",
  "to_commit": "<hash>",
  "predicate_logic_changed": true,
  "match_set_direction": "broader | narrower | mixed | equivalent | unclear",
  "observable_edit_kinds": [
    "value_added",
    "value_removed",
    "value_modified",
    "conjunct_added",
    "conjunct_removed",
    "disjunct_added",
    "disjunct_removed",
    "branch_added",
    "branch_removed",
    "restructure_only"
  ],
  "evidence": {
    "added_items": ["<item>", "..."],
    "removed_items": ["<item>", "..."],
    "modified_items": [
      {
        "before": "<old item>",
        "after": "<new item>"
      }
    ]
  },
  "summary": "<one-sentence description of the observable logic change>",
  "rationale_label": "coverage_expansion | false_positive_reduction | mixed_tradeoff | logic_correction | refactor_or_normalization | insufficient_evidence",
  "rationale_confidence": "high | medium | low",
  "rationale_support": "<brief explanation grounded only in the observed rule-pair changes>"
}

Definitions:

1. predicate_logic_changed
- true only if the set of matched events changed or likely changed.
- false if the rule is logically equivalent and only presentation or non-semantic structure changed.
- If uncertain, prefer true only when there is a concrete observable reason.

2. match_set_direction
- "broader": the newer rule matches a broader set or superset of events.
- "narrower": the newer rule matches a more restricted set or subset of events.
- "mixed": some changes broaden while others narrow.
- "equivalent": no meaningful matched-set change.
- "unclear": cannot determine direction from the pair alone.

3. observable_edit_kinds
Use only edit kinds directly supported by the pair:
- "value_added": a new keyword, literal, or value was added within an existing selection or predicate set
- "value_removed": a keyword, literal, or value was removed
- "value_modified": one value was changed into another
- "conjunct_added": a new required condition was added under AND-like logic
- "conjunct_removed": a required condition was removed
- "disjunct_added": a new alternative was added under OR-like logic
- "disjunct_removed": an alternative was removed
- "branch_added": a larger Boolean subexpression or grouped condition was introduced
- "branch_removed": a larger Boolean subexpression or grouped condition was deleted
- "restructure_only": regrouping or reorganization without clear matched-set change

4. evidence
- Record only concrete items visible in the pair.
- Do not invent hidden semantics.
- Use short strings copied or normalized from the rule logic.

5. rationale_label
Choose conservatively:
- "coverage_expansion": observable changes suggest broader matching
- "false_positive_reduction": observable changes suggest narrower matching
- "mixed_tradeoff": the pair contains both broadening and narrowing edits
- "logic_correction": the pair strongly suggests a repair of mistaken or contradictory logic
- "refactor_or_normalization": representation changed without clear matched-set change
- "insufficient_evidence": rationale cannot be reliably inferred from this pair

6. rationale_confidence
- "high": rationale follows directly from the observable change
- "medium": plausible but not certain
- "low": weak evidence or ambiguity

7. rationale_support
- Explain the rationale using only the observed pair-level edits.
- Do not refer to author intent, history outside the pair, or downstream effects not visible in the rule text.

Now compare the two rule versions below.\
"""


# ── Helpers ────────────────────────────────────────────────────────────────

_MACRO_PLACEHOLDER_STAGE_RX = re.compile(
    r"^\s*MACRO\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+)\s*$"
)
_MACRO_PLACEHOLDER_ASSIGNMENT_RX = re.compile(
    r"\b[\w.]+\s*=\s*MACRO\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_MACRO_PLACEHOLDER_TOKEN_RX = re.compile(
    r"\bMACRO\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+)"
)


def strip_macro_placeholders(block: str) -> str:
    """Remove unresolved ``MACRO=value`` placeholders from SPL-like text."""
    if not block:
        return ""

    cleaned_parts: list[str] = []
    for part in block.split("|"):
        stage = part.strip()
        if not stage:
            continue
        if _MACRO_PLACEHOLDER_STAGE_RX.fullmatch(stage):
            continue

        stage = _MACRO_PLACEHOLDER_ASSIGNMENT_RX.sub(" ", stage)
        stage = _MACRO_PLACEHOLDER_TOKEN_RX.sub(" ", stage)
        stage = re.sub(r"\s{2,}", " ", stage).strip()
        if stage:
            cleaned_parts.append(stage)

    return " | ".join(cleaned_parts)


def truncate_block(block: str, max_lines: int = 80) -> str:
    """Prevent token blow-up for very long detection blocks."""
    if not block:
        return ""
    lines = block.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + "\n# [TRUNCATED]"
    return block


def get_prompt_spl(version_rec: dict) -> str:
    """Prefer normalized SPL for prompts, with detection_block as fallback."""
    return version_rec.get("spl") or version_rec.get("detection_block", "")


def make_commit_obj(version_rec: dict, max_lines: int = 80) -> dict:
    """Extract the commit fields used in the prompt."""
    return {
        "commit_hash":     version_rec.get("commit_hash", ""),
        "detection_block": truncate_block(
            strip_macro_placeholders(get_prompt_spl(version_rec)),
            max_lines,
        ),
    }


def format_prompt(commit_a: dict, commit_b: dict) -> str:
    """Render the full prompt for one version pair."""
    return (
        PROMPT_INSTRUCTIONS
        + "\n\n### Commit A (older)\n~~~json\n"
        + json.dumps(commit_a, indent=2)
        + "\n~~~\n\n### Commit B (newer)\n~~~json\n"
        + json.dumps(commit_b, indent=2)
        + "\n~~~"
    )


# ── Data loading ───────────────────────────────────────────────────────────

def load_versions_index(
    repo: str,
    build_data: Path = BUILD_DATA,
) -> dict[str, list[dict]]:
    """
    Load rule_versions_{repo}.jsonl and return
    {lineage_id: [sorted version records]}.
    """
    path = build_data / f"rule_versions_{repo}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"rule_versions file not found: {path}")

    by_lineage: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            by_lineage[rec["lineage_id"]].append(rec)

    # Sort each lineage by version_index
    for lid in by_lineage:
        by_lineage[lid].sort(key=lambda r: r["version_index"])
    return dict(by_lineage)


# ── Pair generators ────────────────────────────────────────────────────────

def iter_pairs_from_corpus(
    repo: str,
    build_data: Path = BUILD_DATA,
    limit: int | None = None,
):
    """
    Yield (lineage_id, rule_canonical, repo, ver_rec_a, ver_rec_b)
    for every consecutive version pair in the corpus.
    Stops after `limit` pairs if provided.
    """
    index = load_versions_index(repo, build_data)
    count = 0
    for lid, versions in sorted(index.items()):
        for i in range(len(versions) - 1):
            if limit is not None and count >= limit:
                return
            va, vb = versions[i], versions[i + 1]
            yield lid, va.get("rule_canonical", ""), repo, va, vb
            count += 1


def iter_pairs_from_entries(
    entries: list[dict],
    build_data: Path = BUILD_DATA,
):
    """
    Yield (lineage_id, rule_canonical, repo, ver_rec_a, ver_rec_b)
    for each entry in the curated test list.

    Each entry must have: lineage_id, repo, version_a, version_b.
    """
    # Build per-repo indices lazily
    _indices: dict[str, dict] = {}

    def get_index(repo: str) -> dict:
        if repo not in _indices:
            _indices[repo] = load_versions_index(repo, build_data)
        return _indices[repo]

    for entry in entries:
        lid   = entry["lineage_id"]
        repo  = entry["repo"]
        va_i  = entry["version_a"]
        vb_i  = entry["version_b"]

        idx = get_index(repo)
        versions = idx.get(lid)
        if versions is None:
            print(f"  WARNING: lineage {lid!r} not found in {repo} index — skipped.",
                  file=sys.stderr)
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

        yield lid, va.get("rule_canonical", ""), repo, va, vb


# ── Output ─────────────────────────────────────────────────────────────────

def write_prompts(pair_iter, outfile: Path, max_lines: int, dry_run: bool) -> int:
    """
    Consume pair_iter, write prompt records to outfile as JSONL.
    Returns number of records written.
    """
    outfile.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with outfile.open("w", encoding="utf-8") as fout:
        for lid, rule_canonical, repo, va, vb in pair_iter:
            commit_a = make_commit_obj(va, max_lines)
            commit_b = make_commit_obj(vb, max_lines)
            prompt   = format_prompt(commit_a, commit_b)

            record = {
                "lineage_id":     lid,
                "repo":           repo,
                "rule_canonical": rule_canonical,
                "version_a":      va["version_index"],
                "version_b":      vb["version_index"],
                "commit_a":       va.get("commit_hash", ""),
                "commit_b":       vb.get("commit_hash", ""),
                "prompt":         prompt,
            }

            if dry_run:
                print(f"[{repo}] {lid}  v{va['version_index']}→v{vb['version_index']}"
                      f"  {rule_canonical}")
                print(prompt[:600], "...\n")
            else:
                fout.write(json.dumps(record) + "\n")
            count += 1

    return count


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare LLM prompt records for detection-rule version-pair diffs."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--repo",
        choices=["sigma", "ssc", "both"],
        help="Process all version pairs in a corpus.",
    )
    mode.add_argument(
        "--entries",
        metavar="TEST_JSON",
        help="Path to curated test.json; outputs to llm_data/test/prompts.jsonl.",
    )

    parser.add_argument(
        "--outfile",
        metavar="PATH",
        default=None,
        help=(
            "Override output file path. "
            "Default: llm_data/{repo}/prompts.jsonl or llm_data/test/prompts.jsonl."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of pairs to process (corpus mode only).",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=80,
        help="Max lines per detection block before truncation (default: 80).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts to stdout instead of writing to file.",
    )
    parser.add_argument(
        "--build-data",
        metavar="DIR",
        default=str(BUILD_DATA),
        help=f"Override path to build_data directory (default: {BUILD_DATA}).",
    )
    args = parser.parse_args()

    build_data = Path(args.build_data)

    # ── Corpus mode ────────────────────────────────────────────────────────
    if args.repo:
        repos = ["sigma", "ssc"] if args.repo == "both" else [args.repo]
        for repo in repos:
            if args.outfile and len(repos) == 1:
                outfile = Path(args.outfile)
            else:
                outfile = LLM_DATA / repo / "prompts.jsonl"

            print(f"[{repo}] Generating prompts → {outfile}")
            pair_iter = iter_pairs_from_corpus(repo, build_data, args.limit)
            n = write_prompts(pair_iter, outfile, args.max_lines, args.dry_run)
            if not args.dry_run:
                print(f"[{repo}] Wrote {n:,} prompt records to {outfile}")

    # ── Entries mode ───────────────────────────────────────────────────────
    else:
        entries_path = Path(args.entries)
        if not entries_path.exists():
            parser.error(f"entries file not found: {entries_path}")

        with entries_path.open(encoding="utf-8") as f:
            entries = json.load(f)
        print(f"Loaded {len(entries)} entries from {entries_path}")

        outfile = Path(args.outfile) if args.outfile else LLM_DATA / "test" / "prompts.jsonl"
        pair_iter = iter_pairs_from_entries(entries, build_data)
        n = write_prompts(pair_iter, outfile, args.max_lines, args.dry_run)
        if not args.dry_run:
            print(f"Wrote {n:,} prompt records to {outfile}")


if __name__ == "__main__":
    main()
