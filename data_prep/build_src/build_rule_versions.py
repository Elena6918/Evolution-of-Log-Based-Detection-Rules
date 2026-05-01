#!/usr/bin/env python3
"""
build_rule_versions.py
=======================
Stage 5a: Filter, normalize, and version-index the per-commit SPL extracted
by Stage 4 (build_lineage_spl_per_rule.py).

Filtering is VERSION-LEVEL (per commit), not rule-level.  A rule with N commits
where some have extraction failures still yields its remaining valid versions.

  SSC  — SPL is native; apply macro expansion using a per-commit disk cache
         under macro_data/macro_cache_by_commit/, then validate expanded SPL.
         Warm-up runs automatically on the first invocation; subsequent runs
         detect the populated cache index and skip the scan.
  Sigma — SPL is sigma-converted; validate that conversion succeeded; apply
          whitespace normalization only (no macros).

Drop reasons recorded in the filter log:
  parse_failed          -- Stage 4 parse_success=False or error field present
  null_spl              -- spl field is None or empty
  spl_failed            -- spl_success=False from Stage 4
  sigma_convert_failed  -- SPL contains sigma convert failure prefix
  invalid_spl_{reason}  -- post-expansion SPL fails validity check (SSC)

Outputs
--------
  build_data/rule_versions_{repo}.jsonl      -- one line per valid version
  build_data/version_filter_log_{repo}.jsonl -- one line per dropped commit

Version JSONL schema (one JSON object per line)
------------------------------------------------
  lineage_id      : lineage identifier
  rule_canonical  : canonical repo-relative file path
  repo            : "sigma" | "ssc"
  version_index   : 1-based index, contiguous over valid versions within this rule
  original_rank   : 1-based position among ALL commits (including filtered ones)
                    A gap between version_index and original_rank tells you how many
                    commits were filtered between two valid versions.
  commit_hash     : git SHA
  commit_date     : ISO-8601 commit date
  path_used       : repo-relative file path at this commit
  rule_id         : UUID extracted from the file content
  spl             : final normalized (and macro-expanded for SSC) SPL string
  spl_source      : "native" | "sigma_convert"
  detection_block : raw detection logic before normalization
                    (Sigma: YAML detection block; SSC: raw SPL before expansion)
  logsource_block : logsource context
                    (Sigma: YAML logsource block; SSC: data_source string)
  macro_stats     : SSC only -- {expanded, fallback, unresolved}; null for Sigma
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.config import RepoConfig
from lib.spl_normalize import (
    MacroCache,
    normalize_spl_sigma,
    normalize_spl_ssc,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date sorting helper
# ---------------------------------------------------------------------------

def _parse_date_key(s: Optional[str]) -> datetime:
    """Parse commit_date for sorting; unknown dates sort to the beginning."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Per-commit processing
# ---------------------------------------------------------------------------

def _commit_base_drop_reason(commit: dict) -> Optional[str]:
    """
    Return a drop reason if the commit should be skipped before any SPL
    normalization, or None if the commit looks structurally OK to process.
    """
    if "error" in commit:
        return "parse_failed"
    if commit.get("parse_success") is not True:
        return "parse_failed"
    if not commit.get("spl_success"):
        return "spl_failed"
    if not commit.get("spl"):
        return "null_spl"
    return None


def _process_commit_ssc(
    commit: dict,
    macro_cache: MacroCache,
) -> tuple[Optional[dict], Optional[str], dict]:
    """
    Process one SSC commit: macro-expand the SPL and validate.

    Returns (version_fields, drop_reason, macro_stats).
    version_fields is None when drop_reason is set.
    """
    base_reason = _commit_base_drop_reason(commit)
    if base_reason:
        return None, base_reason, {}

    raw_spl = commit["spl"]  # already extracted in Stage 4
    macro_map = macro_cache.get(commit.get("commit_hash", ""))
    normalized, reason, stats = normalize_spl_ssc(raw_spl, macro_map)

    if reason:
        return None, reason, stats

    return {
        "spl": normalized,
        "detection_block": commit.get("detection_block"),   # raw (= raw SPL for SSC)
        "logsource_block": commit.get("logsource_block"),
        "spl_source": commit.get("spl_source", "native"),
        "macro_stats": stats,
    }, None, stats


def _process_commit_sigma(
    commit: dict,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Process one Sigma commit: validate conversion success + whitespace normalize.

    Returns (version_fields, drop_reason).
    """
    base_reason = _commit_base_drop_reason(commit)
    if base_reason:
        return None, base_reason

    normalized, reason = normalize_spl_sigma(commit.get("spl"))
    if reason:
        return None, reason

    return {
        "spl": normalized,
        "detection_block": commit.get("detection_block"),
        "logsource_block": commit.get("logsource_block"),
        "spl_source": commit.get("spl_source", "sigma_convert"),
        "macro_stats": None,
    }, None


# ---------------------------------------------------------------------------
# Per-rule processing
# ---------------------------------------------------------------------------

def _process_rule(
    data: dict,
    repo_type: str,
    macro_cache: Optional[MacroCache],
    versions_buf: list[dict],
    filter_buf: list[dict],
) -> dict:
    """
    Process all commits for one rule file.

    Assigns version_index (contiguous, 1-based) to valid commits.
    Returns a small stats dict for the summary report.
    """
    lineage_id    = data.get("lineage_id", "")
    rule_canonical = data.get("rule_canonical", "")
    repo          = data.get("repo", repo_type)

    raw_commits: list[dict] = data.get("commits") or []

    # Sort by commit_date ascending so version_index reflects chronological order
    commits_sorted = sorted(
        raw_commits,
        key=lambda c: _parse_date_key(c.get("commit_date") or c.get("date")),
    )

    version_index = 0
    kept = 0
    dropped = 0

    for original_rank, commit in enumerate(commits_sorted, start=1):
        chash   = commit.get("commit_hash") or commit.get("hash", "")
        cdate   = commit.get("commit_date") or commit.get("date", "")
        path    = commit.get("path_used", "")
        rule_id = commit.get("rule_id")

        if repo_type == "ssc":
            fields, reason, _ = _process_commit_ssc(commit, macro_cache)  # type: ignore[arg-type]
        else:
            fields, reason = _process_commit_sigma(commit)

        if reason:
            filter_buf.append({
                "lineage_id":    lineage_id,
                "rule_canonical": rule_canonical,
                "original_rank": original_rank,
                "commit_hash":   chash,
                "commit_date":   cdate,
                "path_used":     path,
                "drop_reason":   reason,
            })
            dropped += 1
            continue

        version_index += 1
        kept += 1

        versions_buf.append({
            "lineage_id":     lineage_id,
            "rule_canonical": rule_canonical,
            "repo":           repo,
            "version_index":  version_index,
            "original_rank":  original_rank,
            "commit_hash":    chash,
            "commit_date":    cdate,
            "path_used":      path,
            "rule_id":        rule_id,
            **fields,
        })

    return {"kept": kept, "dropped": dropped, "total": len(commits_sorted)}


# ---------------------------------------------------------------------------
# Macro cache warm-up (SSC only)
# ---------------------------------------------------------------------------

def _collect_commit_hashes(rule_files: list[Path]) -> list[str]:
    """Scan all rule files and collect unique commit hashes for cache warm-up."""
    hashes: set[str] = set()
    for p in rule_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for c in data.get("commits") or []:
                h = c.get("commit_hash") or c.get("hash")
                if h:
                    hashes.add(h)
        except Exception:
            pass
    return sorted(hashes)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _cache_already_populated(cache_dir: Path) -> bool:
    """
    Return True when the macro cache directory already contains a populated
    index file from a previous run.  Used to auto-skip warm-up.
    """
    index = cache_dir / MacroCache._INDEX_FILE
    if not index.exists():
        return False
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        return isinstance(data, dict) and len(data) > 0
    except Exception:
        return False


def build_rule_versions(
    repo_type: str,
    *,
    input_dir: Optional[Path] = None,
    output_versions: Optional[Path] = None,
    output_filter_log: Optional[Path] = None,
    num_rules: Optional[int] = None,
) -> None:
    cfg = RepoConfig(repo_type)

    in_dir         = input_dir or cfg.rule_lineages_dir
    out_versions   = output_versions   or cfg.path("rule_versions")
    out_filter_log = output_filter_log or cfg.path("version_filter_log")

    out_versions.parent.mkdir(parents=True, exist_ok=True)
    out_filter_log.parent.mkdir(parents=True, exist_ok=True)

    logger.info("repo_type      : %s", repo_type)
    logger.info("input_dir      : %s", in_dir)
    logger.info("output_versions: %s", out_versions)
    logger.info("output_filter  : %s", out_filter_log)

    if not in_dir.exists():
        logger.error("Input directory not found: %s", in_dir)
        sys.exit(1)

    rule_files = sorted(in_dir.glob("*.json"))
    if num_rules:
        rule_files = rule_files[:num_rules]
    logger.info("Rule files to process: %d", len(rule_files))

    # ── SSC: build macro cache ────────────────────────────────────────────────
    macro_cache: Optional[MacroCache] = None
    if repo_type == "ssc":
        cache_dir = cfg.macro_cache_dir
        assert cache_dir is not None
        macro_cache = MacroCache(cfg.repo_root, cache_dir)

        if _cache_already_populated(cache_dir):
            logger.info(
                "Macro cache already populated (%s entries in index); skipping warm-up.",
                len(macro_cache._index),
            )
        else:
            logger.info("Macro cache not found; running warm-up ...")
            all_hashes = _collect_commit_hashes(rule_files)
            logger.info("  %d unique commits found; warming cache ...", len(all_hashes))
            misses = macro_cache.warm(all_hashes)
            logger.info("  Warm-up complete: %d cache misses fetched from git.", misses)

    # ── Process rules ────────────────────────────────────────────────────────
    total_rules      = 0
    rules_with_data  = 0
    rules_all_dropped = 0
    total_kept       = 0
    total_dropped    = 0

    versions_buf: list[dict] = []
    filter_buf:   list[dict] = []

    for rule_file in rule_files:
        try:
            data = json.loads(rule_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read %s: %s", rule_file.name, e)
            continue

        total_rules += 1
        if not data.get("commits"):
            continue
        rules_with_data += 1

        stats = _process_rule(data, repo_type, macro_cache, versions_buf, filter_buf)

        total_kept    += stats["kept"]
        total_dropped += stats["dropped"]
        if stats["kept"] == 0:
            rules_all_dropped += 1

        if total_rules % 500 == 0:
            logger.info("  [%d rules] %d versions kept, %d dropped so far",
                        total_rules, total_kept, total_dropped)

    # ── Write outputs ────────────────────────────────────────────────────────
    logger.info("Writing %d version records -> %s", len(versions_buf), out_versions)
    with out_versions.open("w", encoding="utf-8") as f:
        for rec in versions_buf:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    logger.info("Writing %d filter log records -> %s", len(filter_buf), out_filter_log)
    with out_filter_log.open("w", encoding="utf-8") as f:
        for rec in filter_buf:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"VERSION FILTER SUMMARY  [{repo_type.upper()}]")
    print("=" * 60)
    print(f"  Rule files scanned          : {total_rules}")
    print(f"  Rules with commit data       : {rules_with_data}")
    print(f"  Rules with ≥1 valid version  : {rules_with_data - rules_all_dropped}")
    print(f"  Rules entirely dropped       : {rules_all_dropped}")
    print(f"  Total commits processed      : {total_kept + total_dropped}")
    print(f"  Valid versions kept          : {total_kept}")
    print(f"  Commits dropped              : {total_dropped}")
    if total_kept + total_dropped > 0:
        pct = 100.0 * total_kept / (total_kept + total_dropped)
        print(f"  Keep rate                    : {pct:.1f}%")
    print("=" * 60)
    print(f"\nOutputs:")
    print(f"  Versions    : {out_versions}")
    print(f"  Filter log  : {out_filter_log}")
    if repo_type == "ssc" and macro_cache:
        print(f"  Macro cache : {cfg.macro_cache_dir}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Stage 5: Filter and normalize per-commit SPL; "
            "assign version indices; write rule_versions_{repo}.jsonl."
        )
    )
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
        help="Which repo to process.",
    )
    ap.add_argument(
        "--input-dir", default=None,
        help="Override input directory (default: data_prep/rule_lineages_{repo}/).",
    )
    ap.add_argument(
        "--output-versions", default=None,
        help="Override output versions JSONL path.",
    )
    ap.add_argument(
        "--output-filter-log", default=None,
        help="Override output filter log JSONL path.",
    )
    ap.add_argument(
        "-n", "--num-rules", type=int, default=None,
        help="Limit to first N rule files (for testing).",
    )
    return ap.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = parse_args()
    build_rule_versions(
        args.repo_type,
        input_dir=Path(args.input_dir) if args.input_dir else None,
        output_versions=Path(args.output_versions) if args.output_versions else None,
        output_filter_log=Path(args.output_filter_log) if args.output_filter_log else None,
        num_rules=args.num_rules,
    )


if __name__ == "__main__":
    main()
