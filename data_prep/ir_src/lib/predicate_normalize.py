"""
predicate_normalize.py
======================
Normalize the predicate IR produced by Stage 1 (build_unified_ir).

Fixes two structural inconsistencies documented in UNIFIED_IR_SCHEMA.md:

1. **field** nodes may be ``{type:"field", ...}`` or ``{type:"identifier", ...}``
   → always normalized to ``{type:"field", value: <name>}``.

2. **operator** may be a structured object ``{type:"operator", value:"EQ"}``
   or a raw string ``"="`` / ``">="`` / etc.
   → always normalized to a plain string using canonical names
     (``"EQ"``, ``"NE"``, ``"GT"``, ``"GE"``, ``"LT"``, ``"LE"``, …).

The module also provides helpers to walk the tree, collect leaf predicates,
and count nodes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── operator canonicalization map ──────────────────────────────────────

_OP_CANON: Dict[str, str] = {
    "=":   "EQ",
    "==":  "EQ",
    "!=":  "NE",
    ">":   "GT",
    ">=":  "GE",
    "<":   "LT",
    "<=":  "LE",
}


def normalize_operator(op: Any) -> str:
    """Return a canonical operator string."""
    if isinstance(op, dict) and op.get("type") == "operator":
        raw = op.get("value", "")
    elif isinstance(op, str):
        raw = op
    else:
        return str(op)
    return _OP_CANON.get(raw, raw)


def normalize_field(field: Any) -> Dict[str, str]:
    """Return ``{type: "field", value: <name>}``."""
    if isinstance(field, dict):
        return {"type": "field", "value": field.get("value", "")}
    return {"type": "field", "value": str(field)}


# ── tree normalization ─────────────────────────────────────────────────

def normalize_predicate_ir(node: Any) -> Any:
    """Recursively normalize a predicate IR tree *in place* and return it.

    - ExprNode  → recurse into children
    - PredicateNode → normalize field + operator
    - Anything else → pass through unchanged
    """
    if not isinstance(node, dict):
        return node

    ntype = node.get("type")

    if ntype == "expr":
        node["children"] = [
            normalize_predicate_ir(c) for c in node.get("children", [])
        ]
        return node

    if ntype == "predicate":
        node["field"] = normalize_field(node.get("field"))
        node["operator"] = normalize_operator(node.get("operator"))
        return node

    # Unknown node kind — return as-is (e.g. macro_call, call_predicate)
    return node


# ── tree walking helpers ───────────────────────────────────────────────

def collect_predicates(node: Any) -> List[Dict[str, Any]]:
    """Return a flat list of all leaf predicate nodes in *node*."""
    if not isinstance(node, dict):
        return []
    ntype = node.get("type")
    if ntype == "predicate":
        return [node]
    if ntype == "expr":
        out: List[Dict[str, Any]] = []
        for child in node.get("children", []):
            out.extend(collect_predicates(child))
        return out
    return []


def count_nodes(node: Any) -> int:
    """Count total nodes (expr + predicate) in a predicate IR tree."""
    if not isinstance(node, dict):
        return 0
    ntype = node.get("type")
    if ntype == "predicate":
        return 1
    if ntype == "expr":
        return 1 + sum(count_nodes(c) for c in node.get("children", []))
    return 0


def is_empty(node: Any) -> bool:
    """Return True if *node* is None or has zero predicates."""
    if node is None:
        return True
    return count_nodes(node) == 0
