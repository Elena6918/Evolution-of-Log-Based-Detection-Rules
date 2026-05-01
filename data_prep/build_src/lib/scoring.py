"""
lib/scoring.py
==============
Shared scoring constants, normalization helpers, and candidate-scoring logic
used by both analyze_non_head_lineage_candidates.py and merge_non_head_lineages.py.

Keeping these here avoids duplicating ~150 lines of identical code across two scripts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------
SCORE_SHARED_ID        = 100   # near-definitive
SCORE_EXACT_BASENAME   = 60    # strong — same stem, different directory/format
SCORE_SHARED_HIST_PATH = 50    # strong — shared physical file path in commits
SCORE_FUZZY_HIGH       = 30    # fuzzy similarity >= HIGH_FUZZY_THRESH
SCORE_FUZZY_MED        = 15    # fuzzy similarity >= MED_FUZZY_THRESH
SCORE_TEMPORAL_CLOSE   = 20    # gap <= CLOSE_GAP_DAYS
SCORE_TEMPORAL_MEDIUM  = 10    # gap <= MEDIUM_GAP_DAYS
SCORE_COMMIT_KEYWORD   = 10    # rename/convert keyword in any commit subject
SCORE_PATH_TOKEN_OVERLAP = 10  # path-token overlap ratio >= TOKEN_OVERLAP_THRESH

HIGH_FUZZY_THRESH   = 0.85
MED_FUZZY_THRESH    = 0.70
CLOSE_GAP_DAYS      = 30
MEDIUM_GAP_DAYS     = 90
TOKEN_OVERLAP_THRESH = 0.50
MAX_CANDIDATES      = 5        # top-N kept per source lineage (analysis only)

# Thresholds for coarse label
STRONG_THRESHOLD   = 80
POSSIBLE_THRESHOLD = 30
WEAK_THRESHOLD     = 10

MERGE_SCORE_THRESHOLD = 80     # minimum score to add a merge edge

RENAME_KEYWORDS: frozenset[str] = frozenset({
    "convert", "converted", "conversion",
    "rename", "renamed", "renames",
    "move", "moved", "moves",
    "migrate", "migrated", "migration",
    "replace", "replaced", "replaces",
    "split", "splits",
    "merge", "merged",
    "deprecate", "deprecated",
    "cleanup", "refactor", "refactored",
    "promote", "promoted",
})

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date(s: str | None) -> datetime | None:
    """Parse ISO-8601 date string tolerantly, always returning UTC datetime."""
    if not s:
        return None
    s = s.strip()
    # Normalize numeric tz offset (+05:30 → +0530)
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def gap_days(end: datetime | None, start: datetime | None) -> float | None:
    """Return (start - end) in days; negative means candidate started before source ended."""
    if end is None or start is None:
        return None
    return (start - end).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Name / path normalization
# ---------------------------------------------------------------------------

_SEP_RE = re.compile(r"[-_\s]+")
_NOISE_TOKENS: frozenset[str] = frozenset({
    "ssa", "detection", "detections", "yml", "yaml", "json", "deprecated",
    "converted", "escu", "searches", "stories", "baselines",
    "test", "tests", "rules",
})


def normalize_basename(path: str) -> str:
    """
    Lower-case, strip extension, collapse separators, remove common noise
    prefix tokens (e.g. 'ssa___') to get a canonical stem for comparison.
    """
    base = Path(path).stem.lower()
    # Remove leading SSA prefix pattern: 'ssa___'
    base = re.sub(r"^ssa_{2,}", "", base)
    return _SEP_RE.sub(" ", base).strip()


def path_tokens(path: str) -> frozenset[str]:
    """All directory and stem tokens from a path, minus noise."""
    tokens: set[str] = set()
    for part in Path(path).parts:
        for tok in _SEP_RE.split(Path(part).stem.lower()):
            if tok and tok not in _NOISE_TOKENS:
                tokens.add(tok)
    return frozenset(tokens)


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Entry metadata helpers
# ---------------------------------------------------------------------------

def all_paths_for(entry: dict) -> list[str]:
    """Collect all unique paths associated with a lineage entry."""
    paths: list[str] = list(entry.get("all_paths") or [])
    canon = entry.get("canonical_name")
    if canon and canon not in paths:
        paths.append(canon)
    seen = set(paths)
    for commit in (entry.get("commits") or []):
        p = commit.get("path_used")
        if p and p not in seen:
            paths.append(p)
            seen.add(p)
    return paths


def source_meta(entry: dict) -> dict:
    """Pre-compute scoring metadata for one lineage entry."""
    all_ids: set[str] = set(entry.get("all_ids") or [])
    for c in (entry.get("commits") or []):
        uid = c.get("id")
        if uid:
            all_ids.add(uid)

    apaths = all_paths_for(entry)
    norm_basenames = [normalize_basename(p) for p in apaths]
    path_token_sets = [path_tokens(p) for p in apaths]
    subjects = [
        c.get("subject", "").lower()
        for c in (entry.get("commits") or [])
        if c.get("subject")
    ]

    return {
        "all_ids": all_ids,
        "all_paths_set": set(apaths),
        "all_paths": apaths,
        "norm_basenames": norm_basenames,
        "path_token_sets": path_token_sets,
        "subjects": subjects,
        "last_commit_date": parse_date(entry.get("last_commit_date")),
    }


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def build_indexes(lineages: list[dict]) -> dict[str, Any]:
    """
    Build lookup indexes to avoid O(N²) scans.

    Returns a dict with:
      id_index       : id_str   -> list[lineage_id]
      basename_index : norm_stem -> list[lineage_id]
      path_index     : path_str  -> list[lineage_id]
      by_lid         : lineage_id -> entry dict
    """
    id_index: dict[str, list[str]] = defaultdict(list)
    basename_index: dict[str, list[str]] = defaultdict(list)
    path_index: dict[str, list[str]] = defaultdict(list)
    by_lid: dict[str, dict] = {}

    for entry in lineages:
        lid = entry["lineage_id"]
        by_lid[lid] = entry

        for uid in (entry.get("all_ids") or []):
            if uid and lid not in id_index[uid]:
                id_index[uid].append(lid)
        for commit in (entry.get("commits") or []):
            uid = commit.get("id")
            if uid and lid not in id_index[uid]:
                id_index[uid].append(lid)

        for p in all_paths_for(entry):
            nb = normalize_basename(p)
            if nb and lid not in basename_index[nb]:
                basename_index[nb].append(lid)
            if lid not in path_index[p]:
                path_index[p].append(lid)

    return {
        "id_index": dict(id_index),
        "basename_index": dict(basename_index),
        "path_index": dict(path_index),
        "by_lid": by_lid,
    }


# ---------------------------------------------------------------------------
# Candidate gathering (index-first to avoid O(N²) full scan)
# ---------------------------------------------------------------------------

def gather_candidate_lids(src_meta: dict, src_lid: str, indexes: dict) -> set[str]:
    """Use indexes to narrow the candidate pool for one source lineage."""
    cands: set[str] = set()

    for uid in src_meta["all_ids"]:
        for lid in indexes["id_index"].get(uid, []):
            if lid != src_lid:
                cands.add(lid)

    for nb in src_meta["norm_basenames"]:
        if nb:
            for lid in indexes["basename_index"].get(nb, []):
                if lid != src_lid:
                    cands.add(lid)

    for p in src_meta["all_paths_set"]:
        for lid in indexes["path_index"].get(p, []):
            if lid != src_lid:
                cands.add(lid)

    return cands


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_pair(src_meta: dict, cand: dict) -> tuple[float, list[dict]]:
    """
    Score a candidate lineage against pre-computed source metadata.
    Returns (score, evidence_list).
    """
    score = 0.0
    evidence: list[dict] = []

    cand_all_ids: set[str] = set(cand.get("all_ids") or [])
    for c in (cand.get("commits") or []):
        uid = c.get("id")
        if uid:
            cand_all_ids.add(uid)

    cand_all_paths = all_paths_for(cand)
    cand_paths_set = set(cand_all_paths)
    cand_norm_basenames = {normalize_basename(p) for p in cand_all_paths}

    # A — shared UUID
    shared_ids = src_meta["all_ids"] & cand_all_ids
    if shared_ids:
        score += SCORE_SHARED_ID
        evidence.append({"signal": "shared_id", "ids": sorted(shared_ids)})

    # B — exact normalized basename
    src_norm_set = set(src_meta["norm_basenames"])
    exact = src_norm_set & cand_norm_basenames
    if exact:
        score += SCORE_EXACT_BASENAME
        evidence.append({"signal": "exact_basename_match", "basenames": sorted(exact)})

    # C — fuzzy basename (only when no exact match)
    best_fuzzy_val = 0.0
    best_pair = ("", "")
    if not exact:
        for src_nb in src_meta["norm_basenames"]:
            for cand_nb in cand_norm_basenames:
                f = fuzzy(src_nb, cand_nb)
                if f > best_fuzzy_val:
                    best_fuzzy_val = f
                    best_pair = (src_nb, cand_nb)
        if best_fuzzy_val >= HIGH_FUZZY_THRESH:
            score += SCORE_FUZZY_HIGH
            evidence.append({
                "signal": "fuzzy_basename",
                "similarity": round(best_fuzzy_val, 3),
                "src": best_pair[0],
                "cand": best_pair[1],
            })
        elif best_fuzzy_val >= MED_FUZZY_THRESH:
            score += SCORE_FUZZY_MED
            evidence.append({
                "signal": "fuzzy_basename",
                "similarity": round(best_fuzzy_val, 3),
                "src": best_pair[0],
                "cand": best_pair[1],
            })

        # Path-token overlap only when fuzzy match is not strong
        if best_fuzzy_val < MED_FUZZY_THRESH:
            best_overlap = 0.0
            for src_toks in src_meta["path_token_sets"]:
                for cp in cand_all_paths:
                    cand_toks = path_tokens(cp)
                    if src_toks and cand_toks:
                        ov = len(src_toks & cand_toks) / max(len(src_toks | cand_toks), 1)
                        if ov > best_overlap:
                            best_overlap = ov
            if best_overlap >= TOKEN_OVERLAP_THRESH:
                score += SCORE_PATH_TOKEN_OVERLAP
                evidence.append({"signal": "path_token_overlap", "ratio": round(best_overlap, 3)})

    # D — temporal continuity
    cand_first = parse_date(cand.get("first_commit_date"))
    g = gap_days(src_meta["last_commit_date"], cand_first)
    if g is not None:
        if abs(g) <= CLOSE_GAP_DAYS:
            score += SCORE_TEMPORAL_CLOSE
            evidence.append({"signal": "temporal_gap_days", "gap": round(g, 1)})
        elif abs(g) <= MEDIUM_GAP_DAYS:
            score += SCORE_TEMPORAL_MEDIUM
            evidence.append({"signal": "temporal_gap_days", "gap": round(g, 1)})

    # E — commit keyword
    cand_subjects = [
        c.get("subject", "").lower() for c in (cand.get("commits") or [])
    ]
    all_subjects = src_meta["subjects"] + cand_subjects
    found_kw: set[str] = set()
    for subj in all_subjects:
        found_kw |= set(re.findall(r"\b\w+\b", subj)) & RENAME_KEYWORDS
    if found_kw:
        score += SCORE_COMMIT_KEYWORD
        evidence.append({"signal": "commit_keyword", "keywords": sorted(found_kw)})

    # F — shared historical path
    shared_paths = src_meta["all_paths_set"] & cand_paths_set
    if shared_paths:
        score += SCORE_SHARED_HIST_PATH
        evidence.append({"signal": "shared_path", "paths": sorted(shared_paths)})

    return score, evidence


# ---------------------------------------------------------------------------
# Coarse label
# ---------------------------------------------------------------------------

def coarse_label(score: float) -> str:
    if score >= STRONG_THRESHOLD:
        return "strong_candidate"
    if score >= POSSIBLE_THRESHOLD:
        return "possible_candidate"
    if score >= WEAK_THRESHOLD:
        return "weak_candidate"
    return "no_evidence"
