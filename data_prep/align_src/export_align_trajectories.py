#!/usr/bin/env python3
"""
export_align_trajectories.py
============================
Alignment pipeline — align_src.

Reads  : data_prep/ir_data/pgir_{repo}_nonempty.jsonl  (Stage 3b output)
Writes : data_prep/align_data/all_trajectories_{repo}.jsonl
         data_prep/align_data/all_steps_{repo}.jsonl

For each lineage (grouped by lineage_id, ordered by version_index):
  1. Build a canonical boolean AST (TNode) per version.
  2. Compute pairwise adjacent-step alignment + distance.
  3. Emit one step row per adjacent pair (k, k+1).
  4. Emit one trajectory summary row per lineage.

Output schemas
--------------
all_steps_{repo}.jsonl — one row per adjacent version pair:
  lineage_id, rule_canonical, repo
  version_a, version_b          (1-based version_index values)
  d_step                        (total distance)
  dist_breakdown                (delete/insert/update sub-costs)
  change_counts                 (pred_field_shift, pred_op_shift, ...)
  align_summary                 (matched_count, unmatched_a/b counts)
  nodes_a, nodes_b              (tree size)
  sig_a, sig_b                  (tree signature hashes)
  is_noop                       (sig_a == sig_b or d == 0)
  delta_nodes

all_trajectories_{repo}.jsonl — one row per lineage with >= 2 versions:
  lineage_id, rule_canonical, repo
  n_versions
  sizes                         (first/last/mean tree node count)
  steps                         (count/noop_count/noop_frac/max/median/shock_ratio)
  dist                          (cum/net/excess/ratio + net_breakdown + net_align_summary)
  endpoint_anchor_count
  endpoint_fuzzy_match_count
  endpoint_best_value_similarity
  endpoint_field_overlap
  reverts                       (repeat_count/aba_count)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pgir_align import (
    AlignConfig,
    DistanceConfig,
    TNode,
    build_canonical_tree,
    align_boolean_ast,
    compute_distance_for_pair_from_trees,
    endpoint_anchor_and_fuzzy_counts,
    endpoint_best_value_similarity,
    global_field_overlap,
    iter_pred_leaf_labels,
)

_DATA_PREP = _HERE.parent
_IR_DATA    = _DATA_PREP / "ir_data"
_ALIGN_DATA = _DATA_PREP / "align_data"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def stable_tree_signature(root: TNode) -> str:
    def ser(n: TNode) -> str:
        if not n.children:
            return n.label
        return n.label + "(" + ",".join(ser(c) for c in n.children) + ")"
    s = ser(root)
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def count_nodes(root: TNode) -> int:
    stack = [root]
    n = 0
    while stack:
        cur = stack.pop()
        n += 1
        stack.extend(getattr(cur, "children", None) or [])
    return n


def safe_median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    return float(xs[0]) if len(xs) == 1 else float(statistics.median(xs))


def count_repeats(sig_seq: List[str]) -> int:
    seen: Dict[str, int] = {}
    repeats = 0
    for s in sig_seq:
        seen[s] = seen.get(s, 0) + 1
        if seen[s] >= 2:
            repeats += 1
    return repeats


def count_aba(sig_seq: List[str]) -> int:
    return sum(
        1 for i in range(len(sig_seq) - 2)
        if sig_seq[i] == sig_seq[i + 2] and sig_seq[i] != sig_seq[i + 1]
    )


# ---------------------------------------------------------------------------
# Tree cache (keyed by lineage_id + version_index)
# ---------------------------------------------------------------------------

class TreeCache:
    def __init__(self):
        self._tree: Dict[Tuple[str, int], TNode] = {}
        self._nodes: Dict[Tuple[str, int], int] = {}
        self._sig: Dict[Tuple[str, int], str] = {}

    def get(self, rec: Dict[str, Any]) -> Tuple[TNode, int, str]:
        key = (rec["lineage_id"], rec["version_index"])
        if key not in self._tree:
            t = build_canonical_tree(rec)
            self._tree[key] = t
            self._nodes[key] = count_nodes(t)
            self._sig[key] = stable_tree_signature(t)
        return self._tree[key], self._nodes[key], self._sig[key]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_lineage(
    lineage_id: str,
    rule_canonical: str,
    repo: str,
    versions: List[Dict[str, Any]],
    cache: TreeCache,
    align_cfg: AlignConfig,
    dist_cfg: DistanceConfig,
    *,
    eps: float,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Analyze one lineage trajectory.

    Returns (trajectory_summary, step_rows).
    trajectory_summary is None when there are fewer than 2 versions.
    """
    if len(versions) < 2:
        return None, []

    trees:    List[TNode] = []
    nodes_:   List[int]   = []
    sigs:     List[str]   = []
    v_idxs:   List[int]   = []

    for v in versions:
        t, n, s = cache.get(v)
        trees.append(t)
        nodes_.append(n)
        sigs.append(s)
        v_idxs.append(v["version_index"])

    n_versions = len(versions)
    steps: List[float] = []
    step_rows: List[Dict[str, Any]] = []

    # Adjacent step distances
    for i in range(n_versions - 1):
        ta = trees[i]
        tb = trees[i + 1]
        alignment = align_boolean_ast(ta, tb, align_cfg)
        dist = compute_distance_for_pair_from_trees(ta, tb, alignment, dist_cfg)
        d = float(dist["total"])
        steps.append(d)
        step_rows.append({
            "lineage_id":    lineage_id,
            "rule_canonical": rule_canonical,
            "repo":          repo,
            "version_a":     v_idxs[i],
            "version_b":     v_idxs[i + 1],
            "d_step":        d,
            "dist_breakdown":  dist.get("breakdown", {}),
            "change_counts":   dist.get("change_counts", {}),
            "align_summary": {
                "nodes_a":       alignment.get("nodes_a"),
                "nodes_b":       alignment.get("nodes_b"),
                "matched_count": alignment.get("matched_count"),
                "unmatched_a":   len(alignment.get("unmatched_a") or []),
                "unmatched_b":   len(alignment.get("unmatched_b") or []),
            },
            "nodes_a":     nodes_[i],
            "nodes_b":     nodes_[i + 1],
            "sig_a":       sigs[i],
            "sig_b":       sigs[i + 1],
            "is_noop":     (sigs[i] == sigs[i + 1]) or (d == 0.0),
            "delta_nodes": int(nodes_[i + 1] - nodes_[i]),
        })

    cum = float(sum(steps))

    # Net distance: first → last version
    align_net = align_boolean_ast(trees[0], trees[-1], align_cfg)
    dist_net  = compute_distance_for_pair_from_trees(trees[0], trees[-1], align_net, dist_cfg)
    net = float(dist_net["total"])

    endpoint_anchor_count, endpoint_fuzzy_count = endpoint_anchor_and_fuzzy_counts(align_net)
    endpoint_best_sim  = endpoint_best_value_similarity(trees[0], trees[-1])
    endpoint_field_ovl = global_field_overlap(trees[0], trees[-1])

    excess     = float(cum - net)
    ratio      = float(cum / max(net, eps))
    max_step   = float(max(steps)) if steps else 0.0
    med_step   = float(safe_median(steps))
    shock      = float(max_step / max(med_step, eps)) if steps else 0.0
    noop_steps = sum(1 for r in step_rows if r["is_noop"])

    summary = {
        "lineage_id":    lineage_id,
        "rule_canonical": rule_canonical,
        "repo":          repo,
        "n_versions":    n_versions,
        "sizes": {
            "first": nodes_[0],
            "last":  nodes_[-1],
            "mean":  float(sum(nodes_) / max(1, len(nodes_))),
        },
        "steps": {
            "count":      len(steps),
            "noop_count": int(noop_steps),
            "noop_frac":  float(noop_steps / max(1, len(steps))),
            "max":        max_step,
            "median":     med_step,
            "shock_ratio": shock,
        },
        "dist": {
            "cum":    cum,
            "net":    net,
            "excess": excess,
            "ratio":  ratio,
            "net_breakdown":    dist_net.get("breakdown", {}),
            "net_change_counts": dist_net.get("change_counts", {}),
            "net_align_summary": {
                "nodes_a":       align_net.get("nodes_a"),
                "nodes_b":       align_net.get("nodes_b"),
                "matched_count": align_net.get("matched_count"),
                "unmatched_a":   len(align_net.get("unmatched_a") or []),
                "unmatched_b":   len(align_net.get("unmatched_b") or []),
            },
        },
        "endpoint_anchor_count":          endpoint_anchor_count,
        "endpoint_fuzzy_match_count":     endpoint_fuzzy_count,
        "endpoint_best_value_similarity": endpoint_best_sim,
        "endpoint_field_overlap":         endpoint_field_ovl,
        "reverts": {
            "repeat_count": int(count_repeats(sigs)),
            "aba_count":    int(count_aba(sigs)),
        },
    }
    return summary, step_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def export_trajectories(
    repo_type: str,
    *,
    input_path: Optional[Path] = None,
    out_trajectories: Optional[Path] = None,
    out_steps: Optional[Path] = None,
    min_versions: int = 2,
    eps: float = 1e-6,
    align_cfg: Optional[AlignConfig] = None,
    dist_cfg: Optional[DistanceConfig] = None,
    quiet: bool = False,
    progress_every: int = 250,
) -> None:
    in_path   = input_path      or _IR_DATA    / f"pgir_{repo_type}_nonempty.jsonl"
    traj_path = out_trajectories or _ALIGN_DATA / f"all_trajectories_{repo_type}.jsonl"
    step_path = out_steps        or _ALIGN_DATA / f"all_steps_{repo_type}.jsonl"

    if not in_path.exists():
        print(f"[ERROR] Input file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    traj_path.parent.mkdir(parents=True, exist_ok=True)
    step_path.parent.mkdir(parents=True, exist_ok=True)

    if align_cfg is None:
        align_cfg = AlignConfig()
    if dist_cfg is None:
        dist_cfg = DistanceConfig()

    # 1) Load and group by lineage_id
    if not quiet:
        print(f"Loading {in_path} …", flush=True)

    lineage_groups: Dict[str, List[Dict[str, Any]]] = {}
    loaded = skipped = 0
    with in_path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            lid = rec.get("lineage_id")
            if not lid:
                skipped += 1
                continue
            if rec.get("predicate_count", 0) == 0:
                skipped += 1
                continue
            lineage_groups.setdefault(lid, []).append(rec)
            loaded += 1

    if not quiet:
        print(
            f"Loaded {loaded} records, skipped {skipped}, {len(lineage_groups)} lineages",
            flush=True,
        )

    # Sort each group by version_index
    for lid in lineage_groups:
        lineage_groups[lid].sort(key=lambda r: r.get("version_index", 0))

    # 2) Analyze
    cache = TreeCache()
    all_summaries:   List[Dict[str, Any]] = []
    n_steps_written  = 0
    n_too_short      = 0
    total_lineages   = len(lineage_groups)

    if not quiet:
        print(f"Analyzing {total_lineages} lineages …", flush=True)

    with step_path.open("w") as f_steps:
        for idx, (lid, versions) in enumerate(lineage_groups.items(), start=1):
            if len(versions) < min_versions:
                n_too_short += 1
                if not quiet and progress_every > 0 and (
                    idx == total_lineages or idx % progress_every == 0
                ):
                    print(
                        f"  progress: {idx}/{total_lineages} lineages "
                        f"({n_steps_written} step rows written so far)"
                    , flush=True)
                continue

            rec0 = versions[0]
            summary, step_rows = analyze_lineage(
                lineage_id    = lid,
                rule_canonical = rec0.get("rule_canonical", ""),
                repo          = rec0.get("repo", repo_type),
                versions      = versions,
                cache         = cache,
                align_cfg     = align_cfg,
                dist_cfg      = dist_cfg,
                eps           = eps,
            )
            if summary is None:
                n_too_short += 1
                continue

            all_summaries.append(summary)
            for r in step_rows:
                f_steps.write(json.dumps(r, ensure_ascii=False) + "\n")
                n_steps_written += 1

            if not quiet and progress_every > 0 and (
                idx == total_lineages or idx % progress_every == 0
            ):
                print(
                    f"  progress: {idx}/{total_lineages} lineages "
                    f"({n_steps_written} step rows written so far)"
                , flush=True)

    # 3) Write trajectories
    with traj_path.open("w") as f_traj:
        for s in all_summaries:
            f_traj.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"\n{'=' * 60}", flush=True)
    print(f"ALIGN TRAJECTORIES  [{repo_type.upper()}]", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"  Lineages loaded   : {len(lineage_groups)}", flush=True)
    print(f"  Too short (<{min_versions})    : {n_too_short}", flush=True)
    print(f"  Trajectories out  : {len(all_summaries)}", flush=True)
    print(f"  Steps out         : {n_steps_written}", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"\nTrajectories : {traj_path}", flush=True)
    print(f"Steps        : {step_path}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Alignment pipeline: compute PGIR trajectories and step distances."
    )
    ap.add_argument("--repo-type", required=True, choices=["sigma", "ssc"])
    ap.add_argument("--input",            default=None, help="Override input JSONL path.")
    ap.add_argument("--out-trajectories", default=None, help="Override trajectories output path.")
    ap.add_argument("--out-steps",        default=None, help="Override steps output path.")
    ap.add_argument("--min-versions", type=int, default=2,
                    help="Minimum versions per lineage (default: 2).")
    ap.add_argument("--eps", type=float, default=1e-6)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--progress-every",
        type=int,
        default=250,
        help="Print progress every N analyzed lineages (default: 250, 0 disables).",
    )

    # AlignConfig overrides
    ap.add_argument("--theta-op-support",  type=float, default=None)
    ap.add_argument("--theta-op-coverage", type=float, default=None)
    ap.add_argument("--lambda-ctx",        type=float, default=None)
    ap.add_argument("--max-fuzzy-cands",   type=int,   default=None)
    ap.add_argument(
        "--hard-gate-polarity",
        action="store_true",
        help="Disallow predicate matches across different CTX polarity (default).",
    )
    ap.add_argument(
        "--no-hard-gate-polarity",
        action="store_true",
        help="Allow predicate matches across different CTX polarity.",
    )

    # DistanceConfig overrides
    ap.add_argument("--op-update-cost",    type=float, default=None)
    ap.add_argument("--pred-value-shift",  type=float, default=None)

    return ap.parse_args()


def main() -> None:
    args = _parse_args()

    align_cfg = AlignConfig()
    if args.theta_op_support  is not None:
        align_cfg = AlignConfig(**{**align_cfg.__dict__, "theta_op_support":  args.theta_op_support})
    if args.theta_op_coverage is not None:
        align_cfg = AlignConfig(**{**align_cfg.__dict__, "theta_op_coverage": args.theta_op_coverage})
    if args.lambda_ctx        is not None:
        align_cfg = AlignConfig(**{**align_cfg.__dict__, "lambda_ctx":        args.lambda_ctx})
    if args.max_fuzzy_cands   is not None:
        align_cfg = AlignConfig(**{**align_cfg.__dict__, "max_fuzzy_cands":   args.max_fuzzy_cands})
    if args.hard_gate_polarity:
        align_cfg = AlignConfig(**{**align_cfg.__dict__, "hard_gate_polarity": True})
    if args.no_hard_gate_polarity:
        align_cfg = AlignConfig(**{**align_cfg.__dict__, "hard_gate_polarity": False})

    dist_cfg = DistanceConfig()
    if args.op_update_cost   is not None:
        dist_cfg = DistanceConfig(**{**dist_cfg.__dict__, "op_update_cost":   args.op_update_cost})
    if args.pred_value_shift is not None:
        dist_cfg = DistanceConfig(**{**dist_cfg.__dict__, "pred_value_shift": args.pred_value_shift})

    export_trajectories(
        args.repo_type,
        input_path=Path(args.input)            if args.input            else None,
        out_trajectories=Path(args.out_trajectories) if args.out_trajectories else None,
        out_steps=Path(args.out_steps)         if args.out_steps        else None,
        min_versions=args.min_versions,
        eps=args.eps,
        align_cfg=align_cfg,
        dist_cfg=dist_cfg,
        quiet=args.quiet,
        progress_every=args.progress_every,
    )


if __name__ == "__main__":
    main()
