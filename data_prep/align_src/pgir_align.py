# pgir_align.py
"""
Library: PG-IR boolean AST alignment (GumTree-inspired) + distance scoring.

Importable by:
  - export_align_trajectories.py  (batch / streaming scorer)
  - align_pgir_between_two.py     (thin CLI, two-record comparison)

Design:
  - Keep alignment deterministic and debug-friendly.
  - Scoring re-indexes trees and consumes alignment["matches"].
  - PROV labels have been removed entirely; only field/op/value/context
    are encoded in predicate leaf labels.

Public API:
  AlignConfig, DistanceConfig
  build_canonical_tree(obj)            <- new PGIR format (recursive expr tree)
  align_boolean_ast(ta, tb, cfg)       -> alignment_dict
  compute_distance_for_pair_from_trees(obj_a, obj_b, ta, tb, alignment, dist_cfg)
  iter_jsonl_nonempty_with_idx1(path)  -> iterator
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterator, List, Optional, Tuple

# =============================================================================
# CONFIGS
# =============================================================================

@dataclass(frozen=True)
class AlignConfig:
    # --- Operator scope matching ---
    theta_op_support: float = 0.60
    theta_op_coverage: float = 0.50

    # --- Context effects ---
    lambda_ctx: float = 0.35

    # --- Candidate control ---
    max_fuzzy_cands: int = 50

    # --- Hard gates ---
    hard_gate_polarity: bool = True
    hard_gate_value_tag: bool = True
    hard_gate_op_class: bool = False

    # --- Similarity scoring knobs ---
    exact_op_bonus: float = 0.05
    field_mismatch_penalty: float = 0.15

    # --- Phase 2 requirements ---
    min_support_anchors_per_scope: int = 1


@dataclass(frozen=True)
class DistanceConfig:
    # predicate insert/delete
    pred_insert_cost: float = 1.0
    pred_delete_cost: float = 1.0

    # operator insert/delete (structural)
    op_insert_cost: float = 3.0
    op_delete_cost: float = 3.0

    # operator label update (primitive; no bound)
    op_update_cost: float = 4.5

    # predicate update decomposition
    pred_field_shift: float = 0.2
    pred_op_shift: float = 0.5
    pred_value_shift: float = 0.8

    # accounting toggles
    charge_unmatched_by_subtree_roots: bool = True
    cap_update_by_del_ins: bool = True


# =============================================================================
# Tree node
# =============================================================================

@dataclass
class TNode:
    label: str
    children: List["TNode"] = field(default_factory=list)


def is_pred_label(lbl: str) -> bool:
    return lbl.startswith("P:")


def is_op_label(lbl: str) -> bool:
    return lbl.startswith("O:")


# =============================================================================
# Value normalization
# =============================================================================

def norm_str(s: str) -> str:
    return s


def norm_value(v: Any):
    if v is None:
        return ("N", None)
    if isinstance(v, bool):
        return ("B", bool(v))
    if isinstance(v, (int, float)):
        return ("X", v)
    if isinstance(v, str):
        return ("S", norm_str(v))
    if isinstance(v, list):
        elems = [norm_value(x) for x in v]
        elems.sort(key=lambda t: repr(t))
        return ("L", tuple(elems))
    if isinstance(v, dict):
        items = [(str(k), norm_value(v[k])) for k in sorted(v.keys(), key=lambda x: str(x))]
        return ("D", tuple(items))
    return ("U", repr(v))


def _nv_tag(nv: Any) -> str:
    if isinstance(nv, tuple) and len(nv) == 2 and isinstance(nv[0], str):
        return nv[0]
    return "U"


def _extract_value_for_norm(v: Any) -> Any:
    """Extract the raw Python value from a new-format ValueNode dict.

    Handles:
      - ValueNode  : {"type": "value", "value": <raw>}
      - List of ValueNodes (IN predicates): [{...}, {...}]
      - Raw values (already unwrapped)
    """
    if isinstance(v, dict) and v.get("type") == "value":
        inner = v.get("value")
        if isinstance(inner, list):
            return [_extract_value_for_norm(x) for x in inner]
        return inner
    if isinstance(v, list):
        return [_extract_value_for_norm(x) for x in v]
    return v


# =============================================================================
# Parse predicate label
# =============================================================================

def parse_pred_label(lbl: str) -> Optional[Tuple[str, str, Any, str]]:
    """
    Parse a predicate label:
      P:<field>|<op>|<repr(norm_value)>[|CTX:<ctx>]

    Returns (field, op, nv_obj, ctx_str) or None on parse failure.
    """
    if not is_pred_label(lbl):
        return None
    body = lbl[2:]

    ctx = ""
    if "|CTX:" in body:
        body, ctx_part = body.split("|CTX:", 1)
        ctx = ctx_part

    parts = body.split("|", 2)
    if len(parts) != 3:
        return None
    f, o, nv_repr = parts[0], parts[1], parts[2]
    try:
        nv = ast.literal_eval(nv_repr)
    except Exception:
        nv = ("U", nv_repr)

    return (f, o, nv, ctx)


# =============================================================================
# Build canonical tree from new PGIR format
# =============================================================================

def _new_pred_label(pred_node: Dict[str, Any]) -> str:
    """Build a predicate leaf label from a new-format PredicateNode."""
    f = pred_node.get("field", {})
    fname = f.get("value") if isinstance(f, dict) else str(f)
    op = str(pred_node.get("operator", ""))
    raw_val = _extract_value_for_norm(pred_node.get("value"))
    nv = norm_value(raw_val)
    return f"P:{fname}|{op}|{repr(nv)}"


def _convert_pgir_node(node: Dict[str, Any]) -> TNode:
    """Recursively convert a new-format predicate_graph node to TNode."""
    ntype = node.get("type")
    if ntype == "expr":
        op = node.get("op", "AND").upper()
        children = [_convert_pgir_node(c) for c in node.get("children", [])]
        return TNode(f"O:{op}", children)
    if ntype == "predicate":
        return TNode(_new_pred_label(node), [])
    return TNode("EMPTY")


def build_raw_tree(obj: Dict[str, Any]) -> TNode:
    """Build a raw (non-canonicalized) TNode tree from a new-format PGIR record."""
    pgraph = obj.get("predicate_graph")
    if not pgraph or not isinstance(pgraph, dict):
        return TNode("EMPTY")
    return _convert_pgir_node(pgraph)


def build_canonical_tree(obj: Dict[str, Any]) -> TNode:
    """Build a canonical TNode tree from a new-format PGIR record.

    Steps: convert → flatten associative → sort commutative children →
    annotate polarity context.
    """
    t = build_raw_tree(obj)
    t = flatten_associative(t)
    t = canonicalize_commutative(t)
    t = annotate_polarity_context(t)
    return t


# =============================================================================
# Canonicalization helpers
# =============================================================================

def flatten_associative(node: TNode) -> TNode:
    new_children = [flatten_associative(c) for c in node.children]
    if node.label in ("O:AND", "O:OR"):
        flat: List[TNode] = []
        for ch in new_children:
            if ch.label == node.label:
                flat.extend(ch.children)
            else:
                flat.append(ch)
        return TNode(node.label, flat)
    return TNode(node.label, new_children)


def _pred_alignment_key(lbl: str):
    p = parse_pred_label(lbl)
    if p is None:
        return ("Z", lbl)
    f, o, nv, ctx = p
    return ("P", f, str(o).upper(), nv, ctx)


def _alignment_key(node: TNode):
    if not node.children:
        if node.label.startswith("P:"):
            return _pred_alignment_key(node.label)
        return ("L", node.label)
    child_keys = tuple(sorted((_alignment_key(c) for c in node.children), key=repr))
    return ("O", node.label, child_keys)


def canonicalize_commutative(node: TNode) -> TNode:
    canon_children = [canonicalize_commutative(c) for c in node.children]
    if node.label in ("O:AND", "O:OR"):
        canon_children.sort(key=lambda n: repr(_alignment_key(n)))
    return TNode(node.label, canon_children)


def annotate_polarity_context(root: TNode) -> TNode:
    def rec(node: TNode, neg: bool) -> TNode:
        lbl = node.label
        if lbl.startswith("P:") and not node.children:
            if "|CTX:" in lbl:
                return node
            ctx = "NEG" if neg else "POS"
            return TNode(lbl + "|CTX:" + ctx, [])
        if lbl == "O:NOT":
            return TNode(lbl, [rec(ch, not neg) for ch in node.children])
        return TNode(lbl, [rec(ch, neg) for ch in node.children])
    return rec(root, False)


def tree_to_tuple(node: TNode):
    return (node.label, tuple(tree_to_tuple(ch) for ch in node.children))


def tree_signature(node: TNode) -> str:
    return repr(tree_to_tuple(node))


# =============================================================================
# JSONL selection utilities
# =============================================================================

def iter_jsonl_nonempty_with_idx1(path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
    idx1 = 0
    with path.open() as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            idx1 += 1
            yield idx1, json.loads(line)


def iter_jsonl(path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
    with path.open() as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            yield i, json.loads(line)


# =============================================================================
# Indexing + context
# =============================================================================

def index_tree(
    root: TNode,
) -> Tuple[
    Dict[int, TNode],
    Dict[int, int],
    Dict[int, Optional[int]],
    Dict[int, List[int]],
    List[int],
]:
    """Assign stable integer IDs by preorder traversal; compute height, parent,
    children, postorder."""
    id2node: Dict[int, TNode] = {}
    parent: Dict[int, Optional[int]] = {}
    children: Dict[int, List[int]] = {}

    next_id = 0
    stack: List[Tuple[TNode, Optional[int]]] = [(root, None)]
    preorder_nodes: List[Tuple[int, TNode]] = []

    while stack:
        node, par = stack.pop()
        nid = next_id
        next_id += 1
        id2node[nid] = node
        parent[nid] = par
        children[nid] = []
        preorder_nodes.append((nid, node))
        for ch in reversed(node.children):
            stack.append((ch, nid))

    obj2id = {id(obj): nid for nid, obj in id2node.items()}
    for nid, obj in id2node.items():
        children[nid] = [obj2id[id(ch)] for ch in obj.children]

    postorder: List[int] = []
    height: Dict[int, int] = {}

    def dfs(u: int):
        for v in children[u]:
            dfs(v)
        postorder.append(u)
        height[u] = 0 if not children[u] else 1 + max(height[v] for v in children[u])

    dfs(0)
    return id2node, height, parent, children, postorder


def descendants_of_type(
    root_id: int,
    children: Dict[int, List[int]],
    id2node: Dict[int, TNode],
    *,
    want_pred_leaf: bool,
) -> List[int]:
    out: List[int] = []
    stack = [root_id]
    while stack:
        u = stack.pop()
        n = id2node[u]
        if want_pred_leaf:
            if is_pred_label(n.label) and not n.children:
                out.append(u)
        else:
            if is_op_label(n.label):
                out.append(u)
        stack.extend(children[u])
    return out


def ancestors_ops(
    nid: int,
    parent: Dict[int, Optional[int]],
    id2node: Dict[int, TNode],
) -> List[str]:
    ops: List[str] = []
    cur = parent.get(nid)
    while cur is not None:
        lbl = id2node[cur].label
        if is_op_label(lbl) and lbl != "O:NOT":
            ops.append(lbl)
        cur = parent.get(cur)
    return ops


def ctx_signature(
    nid: int,
    parent: Dict[int, Optional[int]],
    id2node: Dict[int, TNode],
) -> Counter:
    return Counter(ancestors_ops(nid, parent, id2node))


def ctx_penalty(sig_a: Counter, sig_b: Counter) -> float:
    if not sig_a and not sig_b:
        return 0.0
    inter = union = 0
    for k in set(sig_a.keys()) | set(sig_b.keys()):
        inter += min(sig_a.get(k, 0), sig_b.get(k, 0))
        union += max(sig_a.get(k, 0), sig_b.get(k, 0))
    return 0.0 if union == 0 else 1.0 - (inter / union)


def is_descendant_of(
    nid: int,
    ancestor: int,
    parent: Dict[int, Optional[int]],
) -> bool:
    cur: Optional[int] = nid
    while cur is not None:
        if cur == ancestor:
            return True
        cur = parent.get(cur)
    return False


def is_consistent_with_ancestry(
    a_id: int,
    b_id: int,
    M: Dict[int, int],
    parent_a: Dict[int, Optional[int]],
    parent_b: Dict[int, Optional[int]],
) -> bool:
    cur = parent_a.get(a_id)
    while cur is not None:
        if cur in M:
            return is_descendant_of(b_id, M[cur], parent_b)
        cur = parent_a.get(cur)
    return True


def is_structural_op(lbl: str) -> bool:
    return is_op_label(lbl) and lbl not in ("O:NOT",)


def nearest_mapped_structural_op_ancestor(
    nid: int,
    M: Dict[int, int],
    parent: Dict[int, Optional[int]],
    id2node: Dict[int, TNode],
) -> Optional[int]:
    cur = parent.get(nid)
    while cur is not None:
        lbl = id2node[cur].label
        if is_structural_op(lbl) and cur in M:
            return cur
        cur = parent.get(cur)
    return None


def matched_immediate_children_consistent(
    a_op: int,
    b_op: int,
    M: Dict[int, int],
    children_a: Dict[int, List[int]],
    children_b: Dict[int, List[int]],
    id2a: Dict[int, TNode],
    id2b: Dict[int, TNode],
) -> bool:
    """Return True when preserved immediate-child placement is consistent.

    This is intentionally local:
      - already-matched immediate children must stay immediate children under the
        candidate operator match;
      - unmatched added/removed children do not invalidate the operator match.

    The symmetric check is what catches scope reassignment of matched predicates,
    e.g. a predicate moving from being an immediate child of OR to being an
    immediate child of a nested AND.
    """
    invM = {b: a for a, b in M.items()}

    for a_child in children_a[a_op]:
        if a_child not in M:
            continue
        if M[a_child] not in children_b[b_op]:
            return False

    for b_child in children_b[b_op]:
        if b_child not in invM:
            continue
        if invM[b_child] not in children_a[a_op]:
            return False

    return True


# =============================================================================
# Predicate keys + similarity
# =============================================================================

def op_class(op: str) -> str:
    op = (op or "").upper()
    if op in ("EQ", "="):
        return "EQ"
    if op in ("IN", "CONTAINS"):
        return "SET"
    if op in ("REGEX", "MATCH", "LIKE", "MATCHES_REGEX"):
        return "PAT"
    if op in ("NEQ", "NE", "!="):
        return "NEQ"
    return "OTHER"


def pred_key_exact(lbl: str) -> Optional[Tuple[Any, ...]]:
    p = parse_pred_label(lbl)
    if p is None:
        return None
    f, o, nv, ctx = p
    return ("P", f, str(o).upper(), nv, ctx)


def pred_anchor_key(lbl: str) -> Optional[Tuple[Any, ...]]:
    return pred_key_exact(lbl)


def pred_key_coarse(lbl: str) -> Optional[Tuple[Any, ...]]:
    p = parse_pred_label(lbl)
    if p is None:
        return None
    _f, o, nv, ctx = p
    return ("P", op_class(str(o)), _nv_tag(nv))


def pred_key_field_value_tag(lbl: str) -> Optional[Tuple[Any, ...]]:
    """Broader fuzzy bucket for update-style predicate matching.

    This keeps exact field identity and exact normalized value identity, but
    intentionally drops the operator class. It allows pairs such as EQ↔NEQ on
    the same field and same value to be considered candidate matches, so
    downstream accounting can charge a predicate update instead of a
    delete+insert pair.
    """
    p = parse_pred_label(lbl)
    if p is None:
        return None
    f, _o, nv, _ctx = p
    return ("P", f, nv)


def hard_incompatible_pred(a_lbl: str, b_lbl: str, cfg: AlignConfig) -> bool:
    pa = parse_pred_label(a_lbl)
    pb = parse_pred_label(b_lbl)
    if pa is None or pb is None:
        return True
    _fa, oa, va, ctxa = pa
    _fb, ob, vb, ctxb = pb
    if cfg.hard_gate_polarity and (ctxa != ctxb):
        return True
    if cfg.hard_gate_value_tag and (_nv_tag(va) != _nv_tag(vb)):
        return True
    if cfg.hard_gate_op_class and (op_class(str(oa)) != op_class(str(ob))):
        return True
    return False


# =============================================================================
# String / path similarity helpers (module-level, for export metrics)
# =============================================================================

_re_ws = re.compile(r"\s+")
_re_quotes = re.compile(r"[\"']")
_re_seps = re.compile(r"[\\/]+")
_re_drive = re.compile(r"^[a-z]:", re.IGNORECASE)


def _canon_string(s: str) -> str:
    s = str(s).strip().lower()
    s = _re_ws.sub(" ", s)
    s = _re_quotes.sub("", s)
    s = s.replace("\\\\", "\\")
    return s


def _is_pathish(s: str) -> bool:
    s = str(s)
    if "\\" in s or "/" in s:
        return True
    if _re_drive.match(s.strip()):
        return True
    if "*" in s and ("\\" in s or "/" in s):
        return True
    return False


def _canon_pathish(s: str) -> str:
    s = str(s).strip().lower()
    s = _re_ws.sub(" ", s)
    s = _re_quotes.sub("", s)
    s = s.replace("\\\\", "\\")
    s = s.replace("/", "\\")
    parts = [p for p in _re_seps.split(s) if p]
    s = "\\".join(parts)
    if _re_drive.match(s):
        s = s[0].lower() + s[1:]
    return s


def _path_segments(s: str) -> List[str]:
    s = _canon_pathish(s)
    segs = [seg for seg in _re_seps.split(s) if seg]
    out = []
    for seg in segs:
        seg = seg.strip()
        if not seg:
            continue
        if all(ch in "*?" for ch in seg):
            continue
        out.append(seg)
    return out


def _longest_common_prefix(a: List[str], b: List[str]) -> int:
    n = min(len(a), len(b))
    k = 0
    for i in range(n):
        if a[i] != b[i]:
            break
        k += 1
    return k


def _longest_common_suffix(a: List[str], b: List[str]) -> int:
    n = min(len(a), len(b))
    k = 0
    for i in range(1, n + 1):
        if a[-i] != b[-i]:
            break
        k += 1
    return k


def _glob_match_score(pattern: str, text: str) -> float:
    p = _canon_pathish(pattern)
    t = _canon_pathish(text)
    if not any(ch in p for ch in "*?"):
        return 0.0
    if not fnmatch.fnmatchcase(t, p):
        return 0.0
    p_compact = p.replace("\\", "")
    nonwild = sum(1 for ch in p_compact if ch not in "*?")
    total = max(1, len(p_compact))
    return 0.90 + 0.10 * (nonwild / total)


def _path_affix_score(a: str, b: str) -> float:
    A = _path_segments(a)
    B = _path_segments(b)
    if not A or not B:
        return 0.0
    lcp = _longest_common_prefix(A, B)
    lcs = _longest_common_suffix(A, B)
    denom = max(1, min(len(A), len(B)))
    return max(0.75 * (lcs / denom) + 0.25 * (lcp / denom), lcs / denom, lcp / denom)


def string_fuzzy_sim(a: str, b: str) -> float:
    ca, cb = _canon_string(a), _canon_string(b)
    if ca == cb:
        return 1.0
    base = SequenceMatcher(None, ca, cb).ratio()
    if not (_is_pathish(a) or _is_pathish(b)):
        return float(base)
    glob_score = max(_glob_match_score(a, b), _glob_match_score(b, a))
    affix = _path_affix_score(a, b)
    pa, pb = _canon_pathish(a), _canon_pathish(b)
    path_base = SequenceMatcher(None, pa, pb).ratio()
    return float(max(base, path_base, glob_score, affix))


def _unwrap_list_value(nv: Any) -> Optional[List[str]]:
    try:
        tag, payload = nv
        if tag != "L":
            return None
        out = []
        for item in payload:
            itag, ival = item
            if itag != "S":
                return None
            out.append(ival)
        return out
    except Exception:
        return None


def list_string_fuzzy_sim(a_list: List[str], b_list: List[str]) -> float:
    if not a_list or not b_list:
        return 0.0
    if a_list == b_list:
        return 1.0

    # Large IOC-style lists can make the greedy fuzzy matcher extremely slow.
    # For big string sets, exact-overlap coverage is a practical proxy.
    if len(a_list) >= 64 and len(b_list) >= 64:
        set_a = {_canon_string(x) for x in a_list}
        set_b = {_canon_string(x) for x in b_list}
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        if inter == 0:
            return 0.0
        small = min(len(set_a), len(set_b))
        large = max(len(set_a), len(set_b))
        coverage_small = inter / max(1, small)
        alpha = 0.20
        soft_penalty = (1 - alpha) + alpha * (small / max(1, large))
        return float(coverage_small * soft_penalty)

    A = list(a_list)
    B = list(b_list)
    if len(A) > len(B):
        A, B = B, A
    used = set()
    scores = []
    for a in A:
        best_j = None
        best_s = -1.0
        for j, b in enumerate(B):
            if j in used:
                continue
            s = string_fuzzy_sim(a, b)
            if s > best_s:
                best_s = s
                best_j = j
        if best_j is not None:
            used.add(best_j)
            scores.append(best_s)
    if not scores or max(scores) < 0.90:
        return 0.0
    avg = sum(scores) / len(scores)
    coverage_small = len(scores) / len(A)
    alpha = 0.20
    superset_ratio = len(A) / len(B)
    soft_penalty = (1 - alpha) + alpha * superset_ratio
    return float(avg * coverage_small * soft_penalty)


def pred_value_similarity_from_labels(a_lbl: str, b_lbl: str) -> float:
    pa = parse_pred_label(a_lbl)
    pb = parse_pred_label(b_lbl)
    if pa is None or pb is None:
        return 0.0
    _fa, _oa, va, _ctxa = pa
    _fb, _ob, vb, _ctxb = pb
    if va == vb:
        return 1.0
    if _nv_tag(va) == "S" and _nv_tag(vb) == "S":
        return string_fuzzy_sim(va[1], vb[1])
    if _nv_tag(va) == "L" and _nv_tag(vb) == "L":
        la = _unwrap_list_value(va)
        lb = _unwrap_list_value(vb)
        if la is not None and lb is not None:
            return list_string_fuzzy_sim(la, lb)
    return 0.0


def iter_pred_leaf_labels(root: TNode) -> List[str]:
    out = []
    stack = [root]
    while stack:
        n = stack.pop()
        ch = getattr(n, "children", None) or []
        if not ch and isinstance(n.label, str) and n.label.startswith("P:"):
            out.append(n.label)
        else:
            stack.extend(ch)
    return out


def field_of_pred_label(lbl: str) -> Optional[str]:
    p = parse_pred_label(lbl)
    if p is None:
        return None
    f, _o, _nv, _ctx = p
    return None if f is None else str(f)


def endpoint_best_value_similarity(ta: TNode, tb: TNode) -> float:
    A = iter_pred_leaf_labels(ta)
    B = iter_pred_leaf_labels(tb)
    best = 0.0
    for a_lbl in A:
        for b_lbl in B:
            s = pred_value_similarity_from_labels(a_lbl, b_lbl)
            if s > best:
                best = s
                if best >= 0.999:
                    return 1.0
    return float(best)


ANCHOR_PHASES = {"pred_exact_unique", "pred_exact_scoped"}
FUZZY_PHASES = {"pred_fuzzy_principled"}


def endpoint_anchor_and_fuzzy_counts(align: Dict[str, Any]) -> Tuple[int, int]:
    anchors: set = set()
    fuzzies: set = set()
    for m in (align.get("matches") or []):
        if not isinstance(m, dict) or m.get("kind") != "PRED":
            continue
        a_id = m.get("a_id")
        b_id = m.get("b_id")
        if a_id is None or b_id is None:
            continue
        ph = m.get("phase")
        if ph in ANCHOR_PHASES:
            anchors.add((int(a_id), int(b_id)))
        elif ph in FUZZY_PHASES:
            fuzzies.add((int(a_id), int(b_id)))
    return int(len(anchors)), int(len(fuzzies))


def global_field_overlap(ta: TNode, tb: TNode) -> int:
    fa = {f for lbl in iter_pred_leaf_labels(ta) if (f := field_of_pred_label(lbl))}
    fb = {f for lbl in iter_pred_leaf_labels(tb) if (f := field_of_pred_label(lbl))}
    return int(len(fa & fb))


# =============================================================================
# Alignment algorithm
# =============================================================================

def align_boolean_ast(
    ta: TNode,
    tb: TNode,
    cfg: AlignConfig,
    *,
    debug_scope: bool = False,
) -> Dict[str, Any]:

    def match_ops_bottom_up_once(
        *,
        id2a, id2b, ha, hb, pa, pb, ca, cb,
        ops_a, ops_b, M, used_op_b, cfg, match_info, phase_name,
        exact_pred_a, evidence_mode="exact_only",
    ):
        if evidence_mode not in ("exact_only", "all"):
            raise ValueError(f"evidence_mode must be 'exact_only' or 'all'")

        invM = {b: a for a, b in M.items()}
        ops_b_by_label: DefaultDict[str, List[int]] = defaultdict(list)
        for j in ops_b:
            ops_b_by_label[id2b[j].label].append(j)
        for lbl in ops_b_by_label:
            ops_b_by_label[lbl].sort(key=lambda x: (hb[x], x))

        desc_pred_a_cache: Dict[int, List[int]] = {}
        desc_pred_b_cache: Dict[int, List[int]] = {}

        def desc_preds_a(op_id):
            if op_id not in desc_pred_a_cache:
                desc_pred_a_cache[op_id] = descendants_of_type(op_id, ca, id2a, want_pred_leaf=True)
            return desc_pred_a_cache[op_id]

        def desc_preds_b(op_id):
            if op_id not in desc_pred_b_cache:
                desc_pred_b_cache[op_id] = descendants_of_type(op_id, cb, id2b, want_pred_leaf=True)
            return desc_pred_b_cache[op_id]

        def is_evidence_pred(a_pid):
            if evidence_mode == "exact_only":
                return a_pid in exact_pred_a
            return a_pid in M

        for i in sorted(ops_a, key=lambda x: (ha[x], x)):
            if i in M:
                continue
            a_lbl = id2a[i].label
            cands = [j for j in ops_b_by_label.get(a_lbl, []) if j not in used_op_b]
            if not cands:
                continue

            a_desc = desc_preds_a(i)
            mapped_under_i = 0
            mapped_bs_under_i: List[int] = []
            for a_pid in a_desc:
                if is_evidence_pred(a_pid) and a_pid in M:
                    mapped_under_i += 1
                    mapped_bs_under_i.append(M[a_pid])

            if mapped_under_i < cfg.min_support_anchors_per_scope:
                continue

            best_j = None
            best_s = -1.0
            best_height_diff = 0
            best_stats = None

            for j in cands:
                if not is_consistent_with_ancestry(i, j, M, pa, pb):
                    continue
                if not matched_immediate_children_consistent(i, j, M, ca, cb, id2a, id2b):
                    continue
                b_desc = desc_preds_b(j)
                b_desc_set = set(b_desc)
                overlap = sum(1 for b_pid in mapped_bs_under_i if b_pid in b_desc_set)

                mapped_under_j = 0
                for b_pid in b_desc:
                    if b_pid in invM:
                        a_pid = invM[b_pid]
                        if evidence_mode != "exact_only" or a_pid in exact_pred_a:
                            mapped_under_j += 1

                if mapped_under_j < cfg.min_support_anchors_per_scope:
                    continue

                denom = max(1, min(mapped_under_i, mapped_under_j))
                s = overlap / denom
                if s < cfg.theta_op_support:
                    continue

                cov_i = overlap / max(1, mapped_under_i)
                cov_j = overlap / max(1, mapped_under_j)
                if cov_i < cfg.theta_op_coverage or cov_j < cfg.theta_op_coverage:
                    continue

                height_diff = abs(ha[i] - hb[j])
                if (s > best_s) or (
                    s == best_s
                    and (best_j is None or (height_diff, j) < (best_height_diff, best_j))
                ):
                    best_j = j
                    best_s = s
                    best_height_diff = height_diff
                    best_stats = (cov_i, cov_j, mapped_under_i, mapped_under_j, overlap)

            if best_j is None:
                continue

            cov_i, cov_j, mui, muj, ov = best_stats
            M[i] = best_j
            used_op_b.add(best_j)
            match_info.append({
                "a_id": i, "b_id": best_j,
                "a_label": id2a[i].label, "b_label": id2b[best_j].label,
                "kind": "OP", "phase": phase_name,
                "support": best_s, "evidence_mode": evidence_mode,
                "coverage_i": cov_i, "coverage_j": cov_j,
                "mapped_under_i": mui, "mapped_under_j": muj, "overlap": ov,
            })

    id2a, ha, pa, ca, _post_a = index_tree(ta)
    id2b, hb, pb, cb, _post_b = index_tree(tb)

    preds_a = [i for i, n in id2a.items() if is_pred_label(n.label) and not n.children]
    preds_b = [i for i, n in id2b.items() if is_pred_label(n.label) and not n.children]
    ops_a = [i for i, n in id2a.items() if is_op_label(n.label)]
    ops_b = [i for i, n in id2b.items() if is_op_label(n.label)]

    M: Dict[int, int] = {}
    match_info: List[Dict[str, Any]] = []
    used_b: set = set()
    used_op_b: set = set()
    exact_pred_a: set = set()

    # ------------------------------------------------------------------
    # Phase 1: Exact predicate anchors (global unique only)
    # ------------------------------------------------------------------
    idx_exact_b: DefaultDict[Tuple[Any, ...], List[int]] = defaultdict(list)
    for j in preds_b:
        k = pred_key_exact(id2b[j].label)
        if k is not None:
            idx_exact_b[k].append(j)

    idx_exact_a: DefaultDict[Tuple[Any, ...], List[int]] = defaultdict(list)
    for i in preds_a:
        k = pred_key_exact(id2a[i].label)
        if k is not None:
            idx_exact_a[k].append(i)

    unique_keys = [
        k for k in idx_exact_a
        if len(idx_exact_a[k]) == 1 and len(idx_exact_b.get(k, [])) == 1
    ]
    for k in sorted(unique_keys, key=repr):
        i = idx_exact_a[k][0]
        j = idx_exact_b[k][0]
        if j in used_b:
            continue
        M[i] = j
        used_b.add(j)
        exact_pred_a.add(i)
        match_info.append({
            "a_id": i, "b_id": j,
            "a_label": id2a[i].label, "b_label": id2b[j].label,
            "kind": "PRED", "phase": "pred_exact_unique",
        })

    # ------------------------------------------------------------------
    # Phase 2: Operator scope matching (bottom-up)
    # ------------------------------------------------------------------
    match_ops_bottom_up_once(
        id2a=id2a, id2b=id2b, ha=ha, hb=hb, pa=pa, pb=pb, ca=ca, cb=cb,
        ops_a=ops_a, ops_b=ops_b, M=M, used_op_b=used_op_b, cfg=cfg,
        match_info=match_info, phase_name="op_support",
        exact_pred_a=exact_pred_a, evidence_mode="exact_only",
    )

    # ------------------------------------------------------------------
    # Phase 1b: Scope-local exact completion (duplicates)
    # ------------------------------------------------------------------
    matched_ops = [
        (a_id, b_id) for a_id, b_id in M.items()
        if is_op_label(id2a[a_id].label) and is_op_label(id2b[b_id].label)
    ]
    for op_a, op_b in matched_ops:
        A_preds = [p for p in descendants_of_type(op_a, ca, id2a, want_pred_leaf=True) if p not in M]
        B_preds = [p for p in descendants_of_type(op_b, cb, id2b, want_pred_leaf=True) if p not in used_b]
        if not A_preds or not B_preds:
            continue

        by_key_A: DefaultDict[Tuple[Any, ...], List[int]] = defaultdict(list)
        by_key_B: DefaultDict[Tuple[Any, ...], List[int]] = defaultdict(list)
        for pid in A_preds:
            k = pred_key_exact(id2a[pid].label)
            if k is not None:
                by_key_A[k].append(pid)
        for pid in B_preds:
            k = pred_key_exact(id2b[pid].label)
            if k is not None:
                by_key_B[k].append(pid)

        for k in sorted(set(by_key_A) & set(by_key_B), key=repr):
            la = sorted(by_key_A[k])
            lb = sorted(by_key_B[k])
            for t in range(min(len(la), len(lb))):
                a_id = la[t]
                b_id = lb[t]
                if a_id in M or b_id in used_b:
                    continue
                if not is_consistent_with_ancestry(a_id, b_id, M, pa, pb):
                    continue
                M[a_id] = b_id
                used_b.add(b_id)
                exact_pred_a.add(a_id)
                match_info.append({
                    "a_id": a_id, "b_id": b_id,
                    "a_label": id2a[a_id].label, "b_label": id2b[b_id].label,
                    "kind": "PRED", "phase": "pred_exact_scoped",
                })

    # ------------------------------------------------------------------
    # Phase 3: Fuzzy predicate matching
    # ------------------------------------------------------------------
    def val_sim_only(a_lbl: str, b_lbl: str) -> float:
        pa_ = parse_pred_label(a_lbl)
        pb_ = parse_pred_label(b_lbl)
        if pa_ is None or pb_ is None:
            return float("-inf")
        _fa, _oa, va, _ctxa = pa_
        _fb, _ob, vb, _ctxb = pb_
        ta_tag = _nv_tag(va)
        tb_tag = _nv_tag(vb)
        if ta_tag == "S" and tb_tag == "S":
            return string_fuzzy_sim(va[1], vb[1])
        if ta_tag == "L" and tb_tag == "L":
            la = _unwrap_list_value(va)
            lb = _unwrap_list_value(vb)
            if la is not None and lb is not None:
                return list_string_fuzzy_sim(la, lb)
            return float("-inf")
        if ta_tag == "X" and tb_tag == "X":
            if same_field(a_lbl, b_lbl) == 1 and exact_op_match(a_lbl, b_lbl) == 1:
                return 1.0
            return 1.0 if va == vb else float("-inf")
        if ta_tag == "B" and tb_tag == "B":
            # Boolean predicates such as IS_NULL / IS_NOT_NULL often survive
            # field renames unchanged except for the field itself. Treat those
            # as alignable updates when the operator and boolean value still
            # agree, so distance accounting can charge a field shift instead of
            # a delete+insert pair.
            if exact_op_match(a_lbl, b_lbl) == 1 and va == vb:
                return 1.0
        return float("-inf")

    def exact_op_match(a_lbl: str, b_lbl: str) -> int:
        pa_ = parse_pred_label(a_lbl)
        pb_ = parse_pred_label(b_lbl)
        if pa_ is None or pb_ is None:
            return 0
        _fa, oa, _va, _ctxa = pa_
        _fb, ob, _vb, _ctxb = pb_
        return 1 if str(oa).upper() == str(ob).upper() else 0

    def same_field(a_lbl: str, b_lbl: str) -> int:
        pa_ = parse_pred_label(a_lbl)
        pb_ = parse_pred_label(b_lbl)
        if pa_ is None or pb_ is None:
            return 0
        fa, _oa, _va, _ctxa = pa_
        fb, _ob, _vb, _ctxb = pb_
        return 1 if fa == fb else 0

    def in_expected_scope(i: int, j: int, M: Dict[int, int]) -> int:
        a_op = nearest_mapped_structural_op_ancestor(i, M, pa, id2a)
        if a_op is None:
            return 0
        return 1 if is_descendant_of(j, M[a_op], pb) else 0

    remaining_a = [i for i in preds_a if i not in M]
    remaining_b = [j for j in preds_b if j not in used_b]

    idx_coarse_b: DefaultDict[Tuple[Any, ...], List[int]] = defaultdict(list)
    idx_field_value_tag_b: DefaultDict[Tuple[Any, ...], List[int]] = defaultdict(list)
    for j in remaining_b:
        k = pred_key_coarse(id2b[j].label)
        if k is not None:
            idx_coarse_b[k].append(j)
        k_alt = pred_key_field_value_tag(id2b[j].label)
        if k_alt is not None:
            idx_field_value_tag_b[k_alt].append(j)

    ctxA = {i: ctx_signature(i, pa, id2a) for i in preds_a}
    ctxB = {j: ctx_signature(j, pb, id2b) for j in preds_b}

    THETA_VAL_BY_TAG = {
        "S": 0.80, "L": 0.65, "D": 1.0, "B": 1.0,
        "X": 1.0, "N": 1.0, "U": 1.0,
    }

    for i in remaining_a:
        k = pred_key_coarse(id2a[i].label)
        k_alt = pred_key_field_value_tag(id2a[i].label)
        if k is None and k_alt is None:
            continue
        cands_set = set()
        if k is not None:
            cands_set.update(idx_coarse_b.get(k, []))
        if k_alt is not None:
            cands_set.update(idx_field_value_tag_b.get(k_alt, []))
        cands_all = sorted(j for j in cands_set if j not in used_b)[:cfg.max_fuzzy_cands]
        if not cands_all:
            continue

        cands_sf = [j for j in cands_all if same_field(id2a[i].label, id2b[j].label) == 1]
        cands_xf = [j for j in cands_all if same_field(id2a[i].label, id2b[j].label) == 0]

        pa_i = parse_pred_label(id2a[i].label)
        if pa_i is None:
            continue
        _f, _o, va, _ctx = pa_i
        theta_val = THETA_VAL_BY_TAG.get(_nv_tag(va), 1.0)

        def pick_best(cands: List[int]) -> Optional[Tuple[int, Tuple]]:
            best_j = None
            best_key = None
            for j in cands:
                ancestry_bonus = 1 if is_consistent_with_ancestry(i, j, M, pa, pb) else 0
                if hard_incompatible_pred(id2a[i].label, id2b[j].label, cfg):
                    continue
                vs = val_sim_only(id2a[i].label, id2b[j].label)
                if vs < theta_val:
                    continue
                scope_ok = in_expected_scope(i, j, M)
                a_has_scope = nearest_mapped_structural_op_ancestor(i, M, pa, id2a) is not None
                cp = ctx_penalty(ctxA[i], ctxB[j])
                ctx_term = -(cfg.lambda_ctx * cp) if not a_has_scope else 0.0
                key = (vs, scope_ok, ancestry_bonus, ctx_term, exact_op_match(id2a[i].label, id2b[j].label), -j)
                if best_key is None or key > best_key:
                    best_key = key
                    best_j = j
            if best_j is None:
                return None
            return best_j, best_key

        picked = pick_best(cands_sf) or pick_best(cands_xf)
        if picked is None:
            continue

        best_j, best_key = picked
        M[i] = best_j
        used_b.add(best_j)
        match_info.append({
            "a_id": i, "b_id": best_j,
            "a_label": id2a[i].label, "b_label": id2b[best_j].label,
            "kind": "PRED", "phase": "pred_fuzzy_principled",
            "val_sim": best_key[0], "in_scope": bool(best_key[1]),
        })

    # ------------------------------------------------------------------
    # Phase 2b: Operator completion using all evidence
    # ------------------------------------------------------------------
    match_ops_bottom_up_once(
        id2a=id2a, id2b=id2b, ha=ha, hb=hb, pa=pa, pb=pb, ca=ca, cb=cb,
        ops_a=ops_a, ops_b=ops_b, M=M, used_op_b=used_op_b, cfg=cfg,
        match_info=match_info, phase_name="op_completion",
        exact_pred_a=exact_pred_a, evidence_mode="all",
    )

    matched_a = set(M.keys())
    matched_b = set(M.values())
    unmatched_a = sorted(i for i in id2a if i not in matched_a)
    unmatched_b = sorted(j for j in id2b if j not in matched_b)

    return {
        "nodes_a": len(id2a),
        "nodes_b": len(id2b),
        "matched_count": len(M),
        "matches": match_info,
        "unmatched_a": [{"id": i, "label": id2a[i].label} for i in unmatched_a],
        "unmatched_b": [{"id": j, "label": id2b[j].label} for j in unmatched_b],
    }


# =============================================================================
# Distance scoring
# =============================================================================

def _compute_depth(parent: Dict[int, Optional[int]]) -> Dict[int, int]:
    depth: Dict[int, int] = {}
    for nid in parent:
        d = 0
        cur = nid
        while parent.get(cur) is not None:
            d += 1
            cur = parent[cur]
        depth[nid] = d
    return depth


def _subtree_nodes(root_id: int, children: Dict[int, List[int]]) -> List[int]:
    out: List[int] = []
    stack = [root_id]
    while stack:
        u = stack.pop()
        out.append(u)
        stack.extend(children[u])
    return out


def _unmatched_roots(unmatched: set, parent: Dict[int, Optional[int]]) -> List[int]:
    return sorted(u for u in unmatched if parent.get(u) is None or parent[u] not in unmatched)


def _is_pred_leaf(id2node: Dict[int, TNode], nid: int) -> bool:
    n = id2node[nid]
    return is_pred_label(n.label) and not n.children


def _is_op_node(id2node: Dict[int, TNode], nid: int) -> bool:
    return is_op_label(id2node[nid].label)


def _pred_update_cost(a_lbl: str, b_lbl: str, dist_cfg: DistanceConfig) -> float:
    """Cost for a matched predicate leaf pair."""
    if a_lbl == b_lbl:
        return 0.0
    pa = parse_pred_label(a_lbl)
    pb = parse_pred_label(b_lbl)
    if pa is None or pb is None:
        return dist_cfg.pred_delete_cost + dist_cfg.pred_insert_cost
    fa, oa, va, _ctxa = pa
    fb, ob, vb, _ctxb = pb
    cost = 0.0
    if fa != fb:
        cost += dist_cfg.pred_field_shift
    if str(oa).upper() != str(ob).upper():
        cost += dist_cfg.pred_op_shift
    if va != vb:
        cost += dist_cfg.pred_value_shift
    if dist_cfg.cap_update_by_del_ins:
        cost = min(cost, dist_cfg.pred_delete_cost + dist_cfg.pred_insert_cost)
    return cost


def _pred_update_details(a_lbl: str, b_lbl: str, dist_cfg: DistanceConfig) -> Dict[str, Any]:
    """Detailed cost accounting for one matched predicate leaf."""
    zero_counts = {"pred_field_shift": 0, "pred_op_shift": 0, "pred_value_shift": 0}
    zero_costs  = {"pred_field_shift": 0.0, "pred_op_shift": 0.0, "pred_value_shift": 0.0}

    if a_lbl == b_lbl:
        return {"cost": 0.0, "counts": zero_counts.copy(), "costs": zero_costs.copy()}

    pa = parse_pred_label(a_lbl)
    pb = parse_pred_label(b_lbl)
    if pa is None or pb is None:
        return {
            "cost": dist_cfg.pred_delete_cost + dist_cfg.pred_insert_cost,
            "counts": zero_counts.copy(), "costs": zero_costs.copy(),
            "fallback_del_ins_cap": True,
        }

    fa, oa, va, _ctxa = pa
    fb, ob, vb, _ctxb = pb
    counts = zero_counts.copy()
    costs  = zero_costs.copy()

    if fa != fb:
        counts["pred_field_shift"] = 1
        costs["pred_field_shift"] = dist_cfg.pred_field_shift
    if str(oa).upper() != str(ob).upper():
        counts["pred_op_shift"] = 1
        costs["pred_op_shift"] = dist_cfg.pred_op_shift
    if va != vb:
        counts["pred_value_shift"] = 1
        costs["pred_value_shift"] = dist_cfg.pred_value_shift

    cost = sum(costs.values())
    if dist_cfg.cap_update_by_del_ins:
        cost = min(cost, dist_cfg.pred_delete_cost + dist_cfg.pred_insert_cost)
    return {"cost": cost, "counts": counts, "costs": costs}


def _op_update_cost(a_lbl: str, b_lbl: str, dist_cfg: DistanceConfig) -> float:
    return 0.0 if a_lbl == b_lbl else dist_cfg.op_update_cost


def _distance_accounting_from_trees(
    ta: TNode,
    tb: TNode,
    alignment: Dict[str, Any],
    dist_cfg: DistanceConfig,
) -> Dict[str, Any]:
    """Core distance accounting from two indexed canonical trees."""
    id2a, _ha, pa, ca, _post_a = index_tree(ta)
    id2b, _hb, pb, cb, _post_b = index_tree(tb)

    matches = alignment.get("matches", []) or []
    M: Dict[int, int] = {}
    for m in matches:
        ai = int(m["a_id"])
        bi = int(m["b_id"])
        if ai in M and M[ai] != bi:
            raise ValueError(f"Non-functional mapping: a_id {ai} → {M[ai]} and {bi}")
        M[ai] = bi

    matched_a = set(M)
    matched_b = set(M.values())
    unmatched_a = set(id2a) - matched_a
    unmatched_b = set(id2b) - matched_b

    depth_a = _compute_depth(pa)
    depth_b = _compute_depth(pb)

    del_pred = del_op = ins_pred = ins_op = 0.0
    change_counts = {
        "pred_insertion": 0, "pred_deletion": 0,
        "op_insertion": 0, "op_deletion": 0, "op_update": 0,
        "pred_field_shift": 0, "pred_op_shift": 0, "pred_value_shift": 0,
    }
    pred_update_costs = {"pred_field_shift": 0.0, "pred_op_shift": 0.0, "pred_value_shift": 0.0}

    if dist_cfg.charge_unmatched_by_subtree_roots:
        for r in _unmatched_roots(unmatched_a, pa):
            for u in _subtree_nodes(r, ca):
                if u not in unmatched_a:
                    continue
                if _is_pred_leaf(id2a, u):
                    del_pred += dist_cfg.pred_delete_cost
                    change_counts["pred_deletion"] += 1
                elif _is_op_node(id2a, u):
                    del_op += dist_cfg.op_delete_cost
                    change_counts["op_deletion"] += 1
        for r in _unmatched_roots(unmatched_b, pb):
            for u in _subtree_nodes(r, cb):
                if u not in unmatched_b:
                    continue
                if _is_pred_leaf(id2b, u):
                    ins_pred += dist_cfg.pred_insert_cost
                    change_counts["pred_insertion"] += 1
                elif _is_op_node(id2b, u):
                    ins_op += dist_cfg.op_insert_cost
                    change_counts["op_insertion"] += 1
    else:
        for u in unmatched_a:
            if _is_pred_leaf(id2a, u):
                del_pred += dist_cfg.pred_delete_cost
                change_counts["pred_deletion"] += 1
            elif _is_op_node(id2a, u):
                del_op += dist_cfg.op_delete_cost
                change_counts["op_deletion"] += 1
        for u in unmatched_b:
            if _is_pred_leaf(id2b, u):
                ins_pred += dist_cfg.pred_insert_cost
                change_counts["pred_insertion"] += 1
            elif _is_op_node(id2b, u):
                ins_op += dist_cfg.op_insert_cost
                change_counts["op_insertion"] += 1

    upd_pred = upd_op = 0.0
    for a_id, b_id in M.items():
        a_lbl = id2a[a_id].label
        b_lbl = id2b[b_id].label
        if _is_pred_leaf(id2a, a_id) and _is_pred_leaf(id2b, b_id):
            details = _pred_update_details(a_lbl, b_lbl, dist_cfg)
            upd_pred += details["cost"]
            for k, v in details["counts"].items():
                change_counts[k] += int(v)
            for k, v in details["costs"].items():
                pred_update_costs[k] += float(v)
        elif _is_op_node(id2a, a_id) and _is_op_node(id2b, b_id):
            op_cost = _op_update_cost(a_lbl, b_lbl, dist_cfg)
            upd_op += op_cost
            if op_cost != 0.0:
                change_counts["op_update"] += 1
        else:
            upd_pred += dist_cfg.pred_delete_cost + dist_cfg.pred_insert_cost

    total = (del_pred + del_op) + (ins_pred + ins_op) + upd_pred + upd_op

    def _depth_stats(us: set, dm: Dict[int, int]) -> Dict[str, Any]:
        if not us:
            return {"count": 0, "min": None, "max": None, "mean": None}
        ds = [dm[u] for u in us]
        return {"count": len(ds), "min": min(ds), "max": max(ds), "mean": sum(ds) / len(ds)}

    return {
        "total": float(total),
        "breakdown": {
            "delete_pred": float(del_pred),
            "delete_op": float(del_op),
            "insert_pred": float(ins_pred),
            "insert_op": float(ins_op),
            "update_pred": float(upd_pred),
            "update_op": float(upd_op),
        },
        "change_counts": change_counts,
        "change_cost_breakdown": pred_update_costs,
        "sizes": {
            "nodes_a": len(id2a),
            "nodes_b": len(id2b),
            "matched": len(M),
            "unmatched_a": len(unmatched_a),
            "unmatched_b": len(unmatched_b),
        },
        "unmatched_roots": {
            "a": len(_unmatched_roots(unmatched_a, pa)),
            "b": len(_unmatched_roots(unmatched_b, pb)),
        },
        "unmatched_depth": {
            "a": _depth_stats(unmatched_a, depth_a),
            "b": _depth_stats(unmatched_b, depth_b),
        },
        "dist_config": asdict(dist_cfg),
    }


def compute_distance_for_pair_from_trees(
    ta: TNode,
    tb: TNode,
    alignment: Dict[str, Any],
    dist_cfg: Optional[DistanceConfig] = None,
) -> Dict[str, Any]:
    """Compute distance given pre-built canonical trees.

    Signature change from legacy: obj_a / obj_b removed (they were only
    used for PROV grouping, which is no longer computed).
    """
    if dist_cfg is None:
        dist_cfg = DistanceConfig()
    return _distance_accounting_from_trees(ta, tb, alignment, dist_cfg)


def compute_distance_for_pair(
    obj_a: Dict[str, Any],
    obj_b: Dict[str, Any],
    alignment: Dict[str, Any],
    dist_cfg: Optional[DistanceConfig] = None,
) -> Dict[str, Any]:
    """Convenience wrapper — builds canonical trees then scores."""
    if dist_cfg is None:
        dist_cfg = DistanceConfig()
    ta = build_canonical_tree(obj_a)
    tb = build_canonical_tree(obj_b)
    return _distance_accounting_from_trees(ta, tb, alignment, dist_cfg)
