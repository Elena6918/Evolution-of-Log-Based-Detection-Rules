"""
structural_ops_helpers.py
=========================
Helpers for structural_patterns_analysis in analysis/scripts/structural.ipynb.

Adapted from temporal_analysis/structural_ops_helpers.py with the following changes:
  - Uses new pgir_align.py (from data_prep/align_src/) supporting new PGIR format.
  - parse_pred_label returns a 4-tuple (field, op, norm_value, ctx) — PROV removed.
  - ByteOffsetIndex replaced by load_pgir_index / load_pgir_index_selective for
    dict-based lookup by (lineage_id, version_index).
  - Pattern labels renamed to match paper: PURE_EXPANSION, PURE_CONTRACTION,
    MIXED, RESTRUCTURING_ONLY (PRED_VALUE_UPDATE_ONLY unchanged).

Public API
----------
  load_pgir_index(path)            -> {(lineage_id, version_index): record}
  load_pgir_index_selective(path, needed_keys) -> same, only for requested keys
  detect_structural_ops_for_pair(obj_a, obj_b, cfg) -> detection result dict
  sequence_metrics(op_sequence)   -> lineage-level aggregate metrics
  assign_pattern_labels(metrics)  -> list of pattern label strings
  tree_structure_summary(root)    -> structural stats dict
  format_tree_compact(node)       -> human-readable tree string
  pred_multiset(root)             -> Counter of (field, op, value_tag)
  ALL_PRIMITIVE_OPS, CORE_OPS, OP_* constants
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Wire up pgir_align (from data_prep/align_src/)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from pgir_align import (
    AlignConfig,
    TNode,
    align_boolean_ast,
    ancestors_ops,
    build_canonical_tree,
    descendants_of_type,
    index_tree,
    is_op_label,
    is_pred_label,
    parse_pred_label,
    tree_signature,
)

# ---------------------------------------------------------------------------
# Primitive operation name constants
# ---------------------------------------------------------------------------
OP_OR_ADD             = "OR_ADD"
OP_OR_REMOVE           = "OR_REMOVE"
OP_AND_ADD            = "AND_ADD"
OP_AND_REMOVE             = "AND_REMOVE"
OP_PRED_UPDATE           = "PRED_UPDATE"
OP_PRED_SCOPE_REASSIGN   = "PRED_SCOPE_REASSIGN"
OP_PRED_CONTEXT_RELABEL  = "PRED_CONTEXT_RELABEL"
OP_BRANCH_ADD          = "BRANCH_ADD"
OP_BRANCH_REMOVE         = "BRANCH_REMOVE"

# Fine-grained branch ops that distinguish OR vs AND branch type.
# These are supplementary flags emitted by detect_structural_ops_for_pair
# in addition to (not replacing) BRANCH_ADD / BRANCH_REMOVE.
# Semantic direction:
#   BRANCH_OR_ADD     → broadening  (new OR branch = extra detection path)
#   BRANCH_AND_ADD    → narrowing   (new AND branch = extra required condition)
#   BRANCH_OR_REMOVE  → narrowing   (drop OR branch = lose detection path)
#   BRANCH_AND_REMOVE → broadening  (drop AND branch = fewer required conditions)
OP_BRANCH_OR_ADD      = "BRANCH_OR_ADD"
OP_BRANCH_AND_ADD     = "BRANCH_AND_ADD"
OP_BRANCH_OR_REMOVE   = "BRANCH_OR_REMOVE"
OP_BRANCH_AND_REMOVE  = "BRANCH_AND_REMOVE"

FINE_GRAINED_DIRECTIONAL_OPS = [
    OP_OR_ADD,
    OP_OR_REMOVE,
    OP_AND_ADD,
    OP_AND_REMOVE,
    OP_BRANCH_AND_ADD,
    OP_BRANCH_OR_ADD,
    OP_BRANCH_AND_REMOVE,
    OP_BRANCH_OR_REMOVE,
]

FINE_GRAINED_REVISION_OPS = [
    *FINE_GRAINED_DIRECTIONAL_OPS,
    OP_PRED_UPDATE,
    OP_PRED_SCOPE_REASSIGN,
]

ALL_PRIMITIVE_OPS = [
    OP_OR_ADD, OP_OR_REMOVE,
    OP_AND_ADD, OP_AND_REMOVE,
    OP_PRED_SCOPE_REASSIGN, OP_PRED_CONTEXT_RELABEL, OP_PRED_UPDATE,
    OP_BRANCH_ADD, OP_BRANCH_REMOVE,
]

# Core structural ops (excludes PRED_UPDATE which is a value-level / L1 op)
CORE_OPS = [
    OP_OR_ADD, OP_OR_REMOVE,
    OP_AND_ADD, OP_AND_REMOVE,
    OP_BRANCH_ADD, OP_BRANCH_REMOVE,
    OP_PRED_SCOPE_REASSIGN, OP_PRED_CONTEXT_RELABEL,
]

# Pattern labels used in assign_pattern_labels / structural evolution analysis
PAT_PURE_EXPANSION        = "PURE_EXPANSION"
PAT_PURE_CONTRACTION      = "PURE_CONTRACTION"
PAT_MIXED                 = "MIXED"
PAT_RESTRUCTURING_ONLY    = "RESTRUCTURING_ONLY"
PAT_PRED_VALUE_UPDATE_ONLY = "PRED_VALUE_UPDATE_ONLY"
PAT_NO_STRUCTURAL_EDITS   = "NO_STRUCTURAL_EDITS"
# Legacy fallback label kept for backward compatibility with older notebooks.
# Under the paper-final definitions, these cases should now be folded into MIXED.
PAT_COMPLEX_UNCLASSIFIED  = "COMPLEX_UNCLASSIFIED"

PATTERN_ORDER = [
    PAT_PURE_EXPANSION,
    PAT_PURE_CONTRACTION,
    PAT_MIXED,
    PAT_RESTRUCTURING_ONLY,
    PAT_PRED_VALUE_UPDATE_ONLY,
]


# ---------------------------------------------------------------------------
# PGIR record loaders (replace ByteOffsetIndex)
# ---------------------------------------------------------------------------

def load_pgir_index(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Load all records from a pgir_{repo}_nonempty.jsonl into a dict.

    Keys are (lineage_id, version_index) for O(1) lookup.
    Memory note: loads the full file; use load_pgir_index_selective when
    only a subset of records is needed.
    """
    idx: Dict[Tuple[str, int], Dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = (rec["lineage_id"], rec["version_index"])
            idx[key] = rec
    return idx


def load_pgir_index_selective(
    path: Path,
    needed_keys: Set[Tuple[str, int]],
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Load only PGIR records whose (lineage_id, version_index) is in needed_keys.

    Much more memory-efficient than load_pgir_index when only a subset
    of versions is needed (e.g. just the A and B sides of non-noop steps).
    """
    idx: Dict[Tuple[str, int], Dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = (rec["lineage_id"], rec["version_index"])
            if key in needed_keys:
                idx[key] = rec
    return idx


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def nearest_or_and_ancestor(
    nid: int,
    parent: Dict[int, Optional[int]],
    id2node: Dict[int, TNode],
    matched_nids: Set[int],
) -> Tuple[Optional[str], Optional[int], bool]:
    """Walk up from nid to find the nearest O:AND or O:OR ancestor.

    Returns (ancestor_label, ancestor_id, is_matched) where:
      - ancestor_label : 'O:AND', 'O:OR', or None (root context)
      - ancestor_id    : integer node ID, or None
      - is_matched     : True if ancestor has a cross-tree match (always True for root)
    """
    cur = parent.get(nid)
    while cur is not None:
        lbl = id2node[cur].label
        if lbl in ("O:AND", "O:OR"):
            return lbl, cur, (cur in matched_nids)
        cur = parent.get(cur)
    return None, None, True   # root-level conjunctive context


def nearest_boolean_scope(
    nid: int,
    parent: Dict[int, Optional[int]],
    id2node: Dict[int, TNode],
) -> Tuple[str, Optional[int]]:
    """Return the nearest enclosing Boolean scope in {O:AND, O:OR, ROOT}."""
    cur = parent.get(nid)
    while cur is not None:
        lbl = id2node[cur].label
        if lbl in ("O:AND", "O:OR"):
            return lbl, cur
        cur = parent.get(cur)
    return "ROOT", None


def count_pred_leaves_in_subtree(
    root_nid: int,
    children: Dict[int, List[int]],
    id2node: Dict[int, TNode],
) -> int:
    """Count predicate leaf nodes in the subtree rooted at root_nid."""
    return len(descendants_of_type(root_nid, children, id2node, want_pred_leaf=True))


def tree_structure_summary(root: TNode) -> Dict[str, Any]:
    """Compute structural statistics for a canonical tree."""
    id2node, height, parent, children, _ = index_tree(root)
    depth: Dict[int, int] = {}
    for nid in id2node:
        d, cur = 0, nid
        while parent.get(cur) is not None:
            d += 1
            cur = parent[cur]
        depth[nid] = d

    n_preds = sum(1 for nid, n in id2node.items() if is_pred_label(n.label) and not n.children)
    n_or  = sum(1 for n in id2node.values() if n.label == "O:OR")
    n_and = sum(1 for n in id2node.values() if n.label == "O:AND")
    n_not = sum(1 for n in id2node.values() if n.label == "O:NOT")
    or_fanouts  = [len(children[nid]) for nid, n in id2node.items() if n.label == "O:OR"]
    and_fanouts = [len(children[nid]) for nid, n in id2node.items() if n.label == "O:AND"]
    depths = list(depth.values())

    return {
        "total_nodes": len(id2node),
        "n_preds": n_preds,
        "n_or": n_or,
        "n_and": n_and,
        "n_not": n_not,
        "max_or_fanout":  max(or_fanouts)  if or_fanouts  else 0,
        "max_and_fanout": max(and_fanouts) if and_fanouts else 0,
        "mean_or_fanout": sum(or_fanouts) / len(or_fanouts) if or_fanouts else 0.0,
        "max_depth": max(depths) if depths else 0,
    }


def format_tree_compact(node: TNode, indent: int = 0) -> str:
    """Return a compact human-readable representation of a TNode tree."""
    prefix = "  " * indent
    lbl = node.label
    if lbl.startswith("P:"):
        parsed = parse_pred_label(lbl)
        if parsed:
            f, o, nv, ctx = parsed   # 4-tuple (no PROV)
            val_str = repr(nv[1]) if isinstance(nv, tuple) and len(nv) == 2 else repr(nv)
            ctx_str = f"[{ctx}]" if ctx else ""
            return f"{prefix}PRED {f} {o} {val_str}{ctx_str}\n"
        return f"{prefix}{lbl}\n"
    lines = f"{prefix}{lbl}\n"
    for ch in node.children:
        lines += format_tree_compact(ch, indent + 1)
    return lines


def pred_multiset(root: TNode) -> Counter:
    """Return a multiset of (field, op, value_tag) for all pred leaves."""
    id2node, _, _, children, _ = index_tree(root)
    result: Counter = Counter()
    for nid, n in id2node.items():
        if is_pred_label(n.label) and not n.children:
            parsed = parse_pred_label(n.label)
            if parsed:
                f, o, nv, _ = parsed   # 4-tuple
                vt = nv[0] if isinstance(nv, tuple) and len(nv) == 2 else "U"
                result[(f, str(o).upper(), vt)] += 1
    return result


# ---------------------------------------------------------------------------
# Primary detection function
# ---------------------------------------------------------------------------

def detect_structural_ops_for_pair(
    obj_a: Dict[str, Any],
    obj_b: Dict[str, Any],
    cfg: Optional[AlignConfig] = None,
) -> Dict[str, Any]:
    """Detect primitive structural operations for one adjacent-version pair.

    Parameters
    ----------
    obj_a, obj_b : PGIR records (from pgir_{repo}_nonempty.jsonl).
                   Each must have a 'predicate_graph' field.
    cfg          : AlignConfig (uses default if None).

    Returns
    -------
    dict with keys:
      ops           : {op_name: bool}
      evidence      : raw evidence counters
      align_summary : matched/unmatched node counts
      tree_a, tree_b: tree_structure_summary dicts
      sig_a, sig_b  : tree signatures
    """
    if cfg is None:
        cfg = AlignConfig()

    ta = build_canonical_tree(obj_a)
    tb = build_canonical_tree(obj_b)
    alignment = align_boolean_ast(ta, tb, cfg)

    id2a, ha, pa, ca, _ = index_tree(ta)
    id2b, hb, pb, cb, _ = index_tree(tb)

    matched_a: Set[int] = {m["a_id"] for m in alignment["matches"]}
    matched_b: Set[int] = {m["b_id"] for m in alignment["matches"]}
    pred_match_map_a_to_b: Dict[int, int] = {
        m["a_id"]: m["b_id"] for m in alignment["matches"] if m.get("kind") == "PRED"
    }
    pred_match_map_b_to_a: Dict[int, int] = {v: k for k, v in pred_match_map_a_to_b.items()}

    unmatched_a: List[Dict] = alignment["unmatched_a"]
    unmatched_b: List[Dict] = alignment["unmatched_b"]

    # ── Evidence counters ────────────────────────────────────────────────────
    ev: Dict[str, int] = {
        "n_pred_added_under_existing_or":  0,
        "n_pred_added_under_existing_and": 0,
        "n_pred_added_at_root":            0,
        "n_pred_added_under_new_scope":    0,
        "n_pred_removed_from_existing_or":  0,
        "n_pred_removed_from_existing_and": 0,
        "n_pred_removed_at_root":           0,
        "n_pred_removed_from_old_scope":    0,
        "n_new_or_ops":  0, "n_new_and_ops": 0, "n_new_not_ops": 0,
        "n_new_or_with_preds":  0, "n_new_and_with_preds": 0,
        "n_removed_or_ops":  0, "n_removed_and_ops": 0, "n_removed_not_ops": 0,
        "n_removed_or_with_preds":  0, "n_removed_and_with_preds": 0,
        "n_pred_update": 0,
        "n_pred_scope_reassign": 0,
        "n_pred_context_relabel": 0,
        "n_pred_field_shift": 0,
        "n_pred_op_shift": 0,
        "n_pred_value_shift": 0,
    }

    # ── Unmatched B nodes (insertions) ───────────────────────────────────────
    for item in unmatched_b:
        nid, lbl = item["id"], item["label"]
        node = id2b[nid]
        if is_pred_label(lbl) and not node.children:
            anc_lbl, _, anc_is_matched = nearest_or_and_ancestor(nid, pb, id2b, matched_b)
            if anc_is_matched:
                if anc_lbl == "O:OR":
                    ev["n_pred_added_under_existing_or"] += 1
                elif anc_lbl == "O:AND":
                    ev["n_pred_added_under_existing_and"] += 1
                else:
                    ev["n_pred_added_at_root"] += 1
            else:
                ev["n_pred_added_under_new_scope"] += 1
        elif is_op_label(lbl):
            if lbl == "O:OR":   ev["n_new_or_ops"]  += 1
            elif lbl == "O:AND": ev["n_new_and_ops"] += 1
            elif lbl == "O:NOT": ev["n_new_not_ops"] += 1

    # ── Unmatched A nodes (deletions) ────────────────────────────────────────
    for item in unmatched_a:
        nid, lbl = item["id"], item["label"]
        node = id2a[nid]
        if is_pred_label(lbl) and not node.children:
            anc_lbl, _, anc_is_matched = nearest_or_and_ancestor(nid, pa, id2a, matched_a)
            if anc_is_matched:
                if anc_lbl == "O:OR":
                    ev["n_pred_removed_from_existing_or"] += 1
                elif anc_lbl == "O:AND":
                    ev["n_pred_removed_from_existing_and"] += 1
                else:
                    ev["n_pred_removed_at_root"] += 1
            else:
                ev["n_pred_removed_from_old_scope"] += 1
        elif is_op_label(lbl):
            if lbl == "O:OR":   ev["n_removed_or_ops"]  += 1
            elif lbl == "O:AND": ev["n_removed_and_ops"] += 1
            elif lbl == "O:NOT": ev["n_removed_not_ops"] += 1

    # ── Scope identity map (matched + inferred-preserved scopes) ─────────────
    matched_scope_map_a_to_b: Dict[int, int] = {
        m["a_id"]: m["b_id"] for m in alignment["matches"] if m.get("kind") == "OP"
    }
    unmatched_op_a = [item["id"] for item in unmatched_a if is_op_label(item["label"])]
    unmatched_op_b = [item["id"] for item in unmatched_b if is_op_label(item["label"])]

    def matched_pred_desc_a(op_id):
        preds = descendants_of_type(op_id, ca, id2a, want_pred_leaf=True)
        return tuple(sorted(pred_match_map_a_to_b[p] for p in preds if p in pred_match_map_a_to_b))

    def matched_pred_desc_b(op_id):
        preds = descendants_of_type(op_id, cb, id2b, want_pred_leaf=True)
        return tuple(sorted(p for p in preds if p in pred_match_map_b_to_a))

    used_scope_pair_b: Set[int] = set()
    preserved_a: Set[int] = set()
    preserved_b: Set[int] = set()

    def total_pred_desc_a(op_id: int) -> int:
        return len(descendants_of_type(op_id, ca, id2a, want_pred_leaf=True))

    def total_pred_desc_b(op_id: int) -> int:
        return len(descendants_of_type(op_id, cb, id2b, want_pred_leaf=True))

    for a_id in unmatched_op_a:
        a_desc = matched_pred_desc_a(a_id)
        if not a_desc:
            continue
        a_lbl = id2a[a_id].label
        for b_id in unmatched_op_b:
            if b_id in used_scope_pair_b:
                continue
            b_desc = matched_pred_desc_b(b_id)
            if b_desc != a_desc:
                continue

            # Preserve same-label wrappers as before.
            same_label_preserved = id2b[b_id].label == a_lbl

            # Also preserve AND↔OR wrapper rewrites when the scope is otherwise
            # unchanged: every predicate descendant is matched, so the only
            # unmatched structural difference is the boolean operator itself.
            relabel_only_preserved = (
                id2a[a_id].label in ("O:AND", "O:OR")
                and id2b[b_id].label in ("O:AND", "O:OR")
                and total_pred_desc_a(a_id) == len(a_desc)
                and total_pred_desc_b(b_id) == len(b_desc)
            )

            if same_label_preserved or relabel_only_preserved:
                matched_scope_map_a_to_b[a_id] = b_id
                used_scope_pair_b.add(b_id)
                preserved_a.add(a_id)
                preserved_b.add(b_id)
                break

    # Reclassify unmatched predicate leaf additions/removals using both
    # alignment-matched scopes and preserved scopes. Without this, predicates
    # added under a preserved OR/AND wrapper are incorrectly treated as being
    # under a wholly new scope, which suppresses OR_ADD / AND_ADD signals.
    ev["n_pred_added_under_existing_or"] = 0
    ev["n_pred_added_under_existing_and"] = 0
    ev["n_pred_added_at_root"] = 0
    ev["n_pred_added_under_new_scope"] = 0
    ev["n_pred_removed_from_existing_or"] = 0
    ev["n_pred_removed_from_existing_and"] = 0
    ev["n_pred_removed_at_root"] = 0
    ev["n_pred_removed_from_old_scope"] = 0

    effective_matched_b = matched_b | preserved_b
    effective_matched_a = matched_a | preserved_a

    for item in unmatched_b:
        nid, lbl = item["id"], item["label"]
        node = id2b[nid]
        if is_pred_label(lbl) and not node.children:
            anc_lbl, _, anc_is_matched = nearest_or_and_ancestor(
                nid, pb, id2b, effective_matched_b
            )
            if anc_is_matched:
                if anc_lbl == "O:OR":
                    ev["n_pred_added_under_existing_or"] += 1
                elif anc_lbl == "O:AND":
                    ev["n_pred_added_under_existing_and"] += 1
                else:
                    ev["n_pred_added_at_root"] += 1
            else:
                ev["n_pred_added_under_new_scope"] += 1

    for item in unmatched_a:
        nid, lbl = item["id"], item["label"]
        node = id2a[nid]
        if is_pred_label(lbl) and not node.children:
            anc_lbl, _, anc_is_matched = nearest_or_and_ancestor(
                nid, pa, id2a, effective_matched_a
            )
            if anc_is_matched:
                if anc_lbl == "O:OR":
                    ev["n_pred_removed_from_existing_or"] += 1
                elif anc_lbl == "O:AND":
                    ev["n_pred_removed_from_existing_and"] += 1
                else:
                    ev["n_pred_removed_at_root"] += 1
            else:
                ev["n_pred_removed_from_old_scope"] += 1

    def unmatched_pred_desc_b(op_id):
        preds = descendants_of_type(op_id, cb, id2b, want_pred_leaf=True)
        return sum(1 for p in preds if p not in matched_b)

    def unmatched_pred_desc_a(op_id):
        preds = descendants_of_type(op_id, ca, id2a, want_pred_leaf=True)
        return sum(1 for p in preds if p not in matched_a)

    for op_id in unmatched_op_b:
        if op_id in preserved_b:
            continue
        if count_pred_leaves_in_subtree(op_id, cb, id2b) == 0:
            continue
        if unmatched_pred_desc_b(op_id) == 0:
            continue
        parent_id = pb.get(op_id)
        if parent_id in unmatched_op_b and parent_id not in preserved_b:
            continue
        lbl = id2b[op_id].label
        if lbl == "O:OR":   ev["n_new_or_with_preds"]  += 1
        elif lbl == "O:AND": ev["n_new_and_with_preds"] += 1

    for op_id in unmatched_op_a:
        if op_id in preserved_a:
            continue
        if count_pred_leaves_in_subtree(op_id, ca, id2a) == 0:
            continue
        if unmatched_pred_desc_a(op_id) == 0:
            continue
        parent_id = pa.get(op_id)
        if parent_id in unmatched_op_a and parent_id not in preserved_a:
            continue
        lbl = id2a[op_id].label
        if lbl == "O:OR":   ev["n_removed_or_with_preds"]  += 1
        elif lbl == "O:AND": ev["n_removed_and_with_preds"] += 1

    # ── Matched predicate pair analysis ──────────────────────────────────────
    for m in alignment["matches"]:
        if m.get("kind") != "PRED":
            continue
        a_lbl, b_lbl = m["a_label"], m["b_label"]

        if a_lbl != b_lbl:
            ev["n_pred_update"] += 1
            pa_parsed = parse_pred_label(a_lbl)
            pb_parsed = parse_pred_label(b_lbl)
            if pa_parsed and pb_parsed:
                fa, oa, va, _ = pa_parsed   # 4-tuple (no PROV)
                fb, ob, vb, _ = pb_parsed
                if fa != fb:
                    ev["n_pred_field_shift"] += 1
                if str(oa).upper() != str(ob).upper():
                    ev["n_pred_op_shift"] += 1
                if va != vb:
                    ev["n_pred_value_shift"] += 1

        scope_lbl_a, scope_id_a = nearest_boolean_scope(m["a_id"], pa, id2a)
        scope_lbl_b, scope_id_b = nearest_boolean_scope(m["b_id"], pb, id2b)

        if scope_id_a is None and scope_id_b is None:
            same_scope = True
        elif scope_id_a is None or scope_id_b is None:
            same_scope = False
        else:
            same_scope = matched_scope_map_a_to_b.get(scope_id_a) == scope_id_b

        if not same_scope:
            ev["n_pred_scope_reassign"] += 1
        elif scope_lbl_a != scope_lbl_b:
            ev["n_pred_context_relabel"] += 1

    # ── Derive primitive-op flags ─────────────────────────────────────────────
    n_new_with_preds     = ev["n_new_or_with_preds"]     + ev["n_new_and_with_preds"]
    n_removed_with_preds = ev["n_removed_or_with_preds"] + ev["n_removed_and_with_preds"]

    n_unmatched_pred_a = sum(
        1 for item in unmatched_a
        if is_pred_label(item["label"]) and not id2a[item["id"]].children
    )
    n_unmatched_pred_b = sum(
        1 for item in unmatched_b
        if is_pred_label(item["label"]) and not id2b[item["id"]].children
    )

    ops: Dict[str, bool] = {
        OP_OR_ADD:           ev["n_pred_added_under_existing_or"]  > 0,
        OP_OR_REMOVE:         ev["n_pred_removed_from_existing_or"] > 0,
        OP_AND_ADD:          (ev["n_pred_added_under_existing_and"] + ev["n_pred_added_at_root"]) > 0,
        OP_AND_REMOVE:           (ev["n_pred_removed_from_existing_and"] + ev["n_pred_removed_at_root"]) > 0,
        OP_PRED_SCOPE_REASSIGN: ev["n_pred_scope_reassign"] > 0,
        OP_PRED_CONTEXT_RELABEL: ev["n_pred_context_relabel"] > 0,
        OP_PRED_UPDATE:         ev["n_pred_update"] > 0,
        OP_BRANCH_ADD:          n_new_with_preds > 0,
        OP_BRANCH_REMOVE:       n_removed_with_preds > 0,
        # Fine-grained branch ops: distinguish OR vs AND branch type
        OP_BRANCH_OR_ADD:      ev["n_new_or_with_preds"]     > 0,
        OP_BRANCH_AND_ADD:     ev["n_new_and_with_preds"]    > 0,
        OP_BRANCH_OR_REMOVE:   ev["n_removed_or_with_preds"] > 0,
        OP_BRANCH_AND_REMOVE:  ev["n_removed_and_with_preds"]> 0,
    }

    return {
        "ops":          ops,
        "evidence":     ev,
        "align_summary": {
            "nodes_a":          alignment["nodes_a"],
            "nodes_b":          alignment["nodes_b"],
            "matched_count":    alignment["matched_count"],
            "unmatched_a":      len(unmatched_a),
            "unmatched_b":      len(unmatched_b),
            "n_unmatched_pred_a": n_unmatched_pred_a,
            "n_unmatched_pred_b": n_unmatched_pred_b,
        },
        "tree_a": tree_structure_summary(ta),
        "tree_b": tree_structure_summary(tb),
        "sig_a":  tree_signature(ta),
        "sig_b":  tree_signature(tb),
    }


def active_directional_ops(
    detection: Dict[str, Any],
    op_order: Optional[List[str]] = None,
) -> List[str]:
    """Return the active fine-grained directional structural ops in display order."""
    if op_order is None:
        op_order = FINE_GRAINED_DIRECTIONAL_OPS
    ops = detection.get("ops", {})
    return [op for op in op_order if ops.get(op, False)]


# ---------------------------------------------------------------------------
# Lineage-level sequence metrics
# ---------------------------------------------------------------------------

def sequence_metrics(op_sequence: List[Dict[str, bool]]) -> Dict[str, Any]:
    """Compute sequence-level metrics for a lineage's primitive-op history.

    Parameters
    ----------
    op_sequence : ordered list of per-step op dicts (one dict per non-noop step,
                  keys are primitive-op names, values are bool flags).

    Returns
    -------
    Metrics dict suitable for assign_pattern_labels.
    """
    n = len(op_sequence)
    if n == 0:
        return {"n_structural_steps": 0}

    counts = {op: sum(1 for s in op_sequence if s.get(op, False)) for op in ALL_PRIMITIVE_OPS}

    expansion   = counts[OP_OR_ADD]   + counts[OP_AND_ADD]   + counts[OP_BRANCH_ADD]
    contraction = counts[OP_OR_REMOVE] + counts[OP_AND_REMOVE]    + counts[OP_BRANCH_REMOVE]
    n_restructure = (
        counts[OP_BRANCH_ADD] + counts[OP_BRANCH_REMOVE]
        + counts[OP_PRED_SCOPE_REASSIGN] + counts[OP_PRED_CONTEXT_RELABEL]
    )

    regimes = []
    for s in op_sequence:
        exp = s.get(OP_OR_ADD, False) or s.get(OP_AND_ADD, False) or s.get(OP_BRANCH_ADD, False)
        con = s.get(OP_OR_REMOVE, False) or s.get(OP_AND_REMOVE, False) or s.get(OP_BRANCH_REMOVE, False)
        if exp and not con:
            regimes.append("expand")
        elif con and not exp:
            regimes.append("contract")
        elif exp and con:
            regimes.append("mixed")
        else:
            regimes.append("other")

    oscillation_count = sum(
        1 for i in range(1, len(regimes))
        if regimes[i] != regimes[i - 1]
        and regimes[i] in ("expand", "contract")
        and regimes[i - 1] in ("expand", "contract")
    )

    return {
        "n_structural_steps":        n,
        "op_counts":                 counts,
        "expansion":                 expansion,
        "contraction":               contraction,
        "net_balance":               expansion - contraction,
        "n_restructure_events":      n_restructure,
        "oscillation_count":         oscillation_count,
        "regimes":                   regimes,
        "is_monotonic_expand":       expansion > 0 and contraction == 0,
        "is_monotonic_contract":     contraction > 0 and expansion == 0,
        "is_mixed":                  expansion > 0 and contraction > 0,
        "restructure_dominated":     n_restructure > 0 and (expansion + contraction) == 0,
        "is_pred_update_only":       (
            counts[OP_PRED_UPDATE] > 0
            and all(not v for k, v in counts.items() if k != OP_PRED_UPDATE)
        ),
    }


def assign_pattern_labels(metrics: Dict[str, Any]) -> List[str]:
    """Assign structural evolution pattern labels (overlapping) to a lineage.

    Pattern names (paper-final):
      PURE_EXPANSION          expansion with no contraction (reorganization allowed)
      PURE_CONTRACTION        contraction with no expansion (reorganization allowed)
      MIXED                   both expansion and contraction (reorganization allowed)
      RESTRUCTURING_ONLY      scope-level changes; zero expansion and contraction
      PRED_VALUE_UPDATE_ONLY  only PRED_UPDATE; no structural ops

    A lineage receives all applicable labels; assign_dominant_pattern picks one.
    """
    labels: List[str] = []
    if metrics.get("n_structural_steps", 0) == 0:
        return [PAT_NO_STRUCTURAL_EDITS]

    if metrics.get("is_pred_update_only"):
        labels.append(PAT_PRED_VALUE_UPDATE_ONLY)
    if metrics.get("restructure_dominated"):
        labels.append(PAT_RESTRUCTURING_ONLY)
    if metrics.get("is_monotonic_expand") and not metrics.get("restructure_dominated"):
        labels.append(PAT_PURE_EXPANSION)
    if metrics.get("is_monotonic_contract") and not metrics.get("restructure_dominated"):
        labels.append(PAT_PURE_CONTRACTION)
    if metrics.get("is_mixed") and not metrics.get("restructure_dominated"):
        labels.append(PAT_MIXED)
    if not labels:
        # Paper-final taxonomy has no residual "other" bucket: any lineage with
        # both expansion and contraction belongs to MIXED, even if reorganization
        # also occurs. Keep MIXED as the defensive fallback for any unforeseen
        # non-monotonic combination that reaches this point.
        labels.append(PAT_MIXED)

    return labels


def assign_dominant_pattern(metrics: Dict[str, Any]) -> str:
    """Assign a single dominant pattern label using priority order.

    Priority: PURE_EXPANSION / PURE_CONTRACTION > MIXED
              > RESTRUCTURING_ONLY > PRED_VALUE_UPDATE_ONLY
    """
    labels = assign_pattern_labels(metrics)
    priority = [
        PAT_PURE_EXPANSION, PAT_PURE_CONTRACTION,
        PAT_MIXED, PAT_RESTRUCTURING_ONLY, PAT_PRED_VALUE_UPDATE_ONLY,
        PAT_NO_STRUCTURAL_EDITS,
    ]
    for pat in priority:
        if pat in labels:
            return pat
    return PAT_MIXED
