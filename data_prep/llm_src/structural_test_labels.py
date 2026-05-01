#!/usr/bin/env python3
"""
Helpers for generating structural test sets without relying on any LLM outputs.

This mirrors the structural detection stack used in analysis/scripts/structural.ipynb:
  - non-noop steps from align_data/all_steps_{repo}.jsonl
  - structural-op detections from analysis/scripts/.cache/struct_ops_{repo}.pkl
    or recomputed from pgir_{repo}_nonempty.jsonl when the cache is missing
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
ALIGN_DATA = REPO_ROOT / "data_prep" / "align_data"
IR_DATA = REPO_ROOT / "data_prep" / "ir_data"
STRUCT_CACHE_DIR = REPO_ROOT / "analysis" / "scripts" / ".cache"


def load_nonnoop_steps(repo: str, align_data: Path = ALIGN_DATA) -> list[dict[str, Any]]:
    path = align_data / f"all_steps_{repo}.jsonl"
    steps: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("is_noop", True):
                steps.append(rec)
    return steps


def needed_pgir_keys(steps: list[dict[str, Any]]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for step in steps:
        lid = step["lineage_id"]
        keys.add((lid, step["version_a"]))
        keys.add((lid, step["version_b"]))
    return keys


def run_detection_cached(repo: str, steps: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    cache_path = STRUCT_CACHE_DIR / f"struct_ops_{repo}.pkl"
    if cache_path.exists():
        with cache_path.open("rb") as f:
            cached = pickle.load(f)
        if isinstance(cached, dict) and "results" in cached:
            return cached["results"]
        return cached

    from structural_ops_helpers import load_pgir_index_selective, detect_structural_ops_for_pair

    pgir_path = IR_DATA / f"pgir_{repo}_nonempty.jsonl"
    pgir_index = load_pgir_index_selective(pgir_path, needed_pgir_keys(steps))

    results: list[dict[str, Any] | None] = []
    for step in steps:
        lid = step["lineage_id"]
        obj_a = pgir_index.get((lid, step["version_a"]))
        obj_b = pgir_index.get((lid, step["version_b"]))
        if obj_a is None or obj_b is None:
            results.append(None)
            continue
        try:
            det = detect_structural_ops_for_pair(obj_a, obj_b)
            det["lineage_id"] = lid
            det["version_a"] = step["version_a"]
            det["version_b"] = step["version_b"]
            results.append(det)
        except Exception as exc:
            results.append({
                "error": str(exc),
                "lineage_id": lid,
                "version_a": step["version_a"],
                "version_b": step["version_b"],
            })

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as f:
        pickle.dump(results, f)
    return results


def infer_structural_test_labels(step: dict[str, Any], det: dict[str, Any]) -> list[str]:
    """
    Derive the seven curation labels requested for test sampling from the
    structural notebook's PG-IR detections plus change_counts.
    """
    ops = det.get("ops") or {}
    cc = step.get("change_counts") or {}
    evidence = det.get("evidence") or {}

    pred_insertions = int(cc.get("pred_insertion", 0) or 0)
    pred_deletions = int(cc.get("pred_deletion", 0) or 0)
    substitution_like = (
        int(cc.get("pred_value_shift", 0) or 0)
        + int(cc.get("pred_field_shift", 0) or 0)
        + int(cc.get("pred_op_shift", 0) or 0)
    )

    new_and_branch = (
        int(evidence.get("n_new_and_with_preds", 0) or 0) > 0
        or int(evidence.get("n_new_and_ops", 0) or 0) > 0
    )
    removed_and_branch = (
        int(evidence.get("n_removed_and_with_preds", 0) or 0) > 0
        or int(evidence.get("n_removed_and_ops", 0) or 0) > 0
    )
    new_or_branch = (
        int(evidence.get("n_new_or_with_preds", 0) or 0) > 0
        or int(evidence.get("n_new_or_ops", 0) or 0) > 0
    )
    removed_or_branch = (
        int(evidence.get("n_removed_or_with_preds", 0) or 0) > 0
        or int(evidence.get("n_removed_or_ops", 0) or 0) > 0
    )

    labels: list[str] = []

    if ops.get("PRED_UPDATE", False) and pred_insertions > 0:
        labels.append("value_addition")
    if ops.get("PRED_UPDATE", False) and pred_deletions > 0:
        labels.append("value_removal")
    if ops.get("PRED_UPDATE", False) and (substitution_like > 0 or (pred_insertions == 0 and pred_deletions == 0)):
        labels.append("value_modification")

    if ops.get("AND_ADD", False) or (ops.get("BRANCH_ADD", False) and new_and_branch):
        labels.append("required_condition_added")
    if ops.get("AND_REMOVE", False) or (ops.get("BRANCH_REMOVE", False) and removed_and_branch):
        labels.append("required_condition_removed")
    if ops.get("OR_ADD", False) or (ops.get("BRANCH_ADD", False) and new_or_branch):
        labels.append("alternative_added")
    if ops.get("OR_REMOVE", False) or (ops.get("BRANCH_REMOVE", False) and removed_or_branch):
        labels.append("alternative_removed")

    return labels


def build_structural_candidates(repo: str) -> list[dict[str, Any]]:
    steps = load_nonnoop_steps(repo)
    det_results = run_detection_cached(repo, steps)
    det_by_key: dict[tuple[str, int, int], dict[str, Any]] = {}
    for det in det_results:
        if det is None or "error" in det:
            continue
        key = (det["lineage_id"], det["version_a"], det["version_b"])
        det_by_key[key] = det

    rows: list[dict[str, Any]] = []
    for step in steps:
        det = det_by_key.get((step["lineage_id"], step["version_a"], step["version_b"]))
        if det is None:
            continue
        labels = infer_structural_test_labels(step, det)
        if not labels:
            continue
        active_ops = [op for op, is_on in (det.get("ops") or {}).items() if is_on]
        rows.append({
            "lineage_id": step["lineage_id"],
            "repo": repo,
            "version_a": step["version_a"],
            "version_b": step["version_b"],
            "rule_canonical": step.get("rule_canonical", ""),
            "d_step": float(step.get("d_step", 0.0) or 0.0),
            "change_counts": step.get("change_counts", {}),
            "pgir_ops": active_ops,
            "pgir_labels": labels,
            "det_evidence": det.get("evidence", {}),
        })
    return rows
