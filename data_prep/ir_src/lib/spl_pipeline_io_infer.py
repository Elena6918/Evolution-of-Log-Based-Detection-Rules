#!/usr/bin/env python3
"""
spl_pipeline_io_infer.py

Lightweight, regex-based SPL stage IO inference.

Public API:
  infer_io_ext(raw, stage_type) -> (inputs, outputs, io_meta)
  infer_io(raw, stage_type)     -> (inputs, outputs)   [backward-compat wrapper]

io_meta keys:
  scope            : "specific" | "all_fields" | "source" | "sink" | "unknown" | "pattern"
  parse_confidence : "full" | "partial" | "failed"
  outputs_dynamic  : bool  — outputs exist but can't be enumerated statically
  input_pattern    : str | None  — for foreach: the glob string
  dataset_ref      : str | None  — for tstats/inputlookup/from/into: table/datamodel name
  schema_preserving: bool  — command never adds/removes columns (sort, dedup, head)

Design principles:
- False positives cost more than false negatives. Prefer empty lists over fabricated fields.
- Use structural function detection (token before '(') rather than static stoplists
  for eval and stats, where function names overlap with valid field names.
- Distinguish "knowably empty" (source, sink, dynamic) from "parse failure".
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple, Any


# ─────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────

IOResult = Tuple[List[str], List[str], Dict[str, Any]]


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

FIELD_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*")

# Generic keyword stopwords — used in most parsers.
# Does NOT include function names (those are detected structurally).
STOPWORDS: Set[str] = {
    "as", "by", "over", "from", "into", "output", "outputs", "input", "inputs",
    "where", "search", "eval", "rex", "regex",
    "true", "false", "null",
    "and", "or", "not", "in",
    "span", "timeformat", "format",
    "type", "max", "limit",  # common option-key names in join/dedup/etc.
}

# Stats aggregation functions that appear WITHOUT parentheses in some forms (e.g. "stats count").
# Only used to detect implicit output field names — not as a general stoplist.
STATS_BARE_FUNCS: Set[str] = {"count", "c"}

# Known tstats option tokens (bare words, not kv-pairs) to strip before field extraction.
TSTATS_OPTION_WORDS: Set[str] = {
    "summariesonly", "allow_old_summaries", "prestats",
    "append", "allnum", "fillnull_value",
}


# ─────────────────────────────────────────────
# io_meta factory
# ─────────────────────────────────────────────

def _meta(
    scope: str = "specific",
    confidence: str = "full",
    dyn_out: bool = False,
    pattern: Optional[str] = None,
    dataset: Optional[str] = None,
    schema_preserving: bool = False,
) -> Dict[str, Any]:
    return {
        "scope": scope,
        "parse_confidence": confidence,
        "outputs_dynamic": dyn_out,
        "input_pattern": pattern,
        "dataset_ref": dataset,
        "schema_preserving": schema_preserving,
    }


# ─────────────────────────────────────────────
# Low-level helpers
# ─────────────────────────────────────────────

def _uniq(seq: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _fieldish_tokens(s: str, extra_stop: Optional[Set[str]] = None) -> List[str]:
    """Conservative field-name extraction: identifiers that are not keywords."""
    stop = STOPWORDS | (extra_stop or set())
    toks = FIELD_TOKEN_RE.findall(s or "")
    return _uniq([t for t in toks if t.lower() not in stop and not t[0].isdigit()])


def _func_call_names(s: str) -> Set[str]:
    """Return lowercase names of all identifiers immediately followed by '(' — function calls."""
    return set(m.lower() for m in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", s or ""))


# Matches a function call and captures the text inside its parens (non-nested).
_FUNC_CALL_ARGS_RE = re.compile(r"\b[A-Za-z_]\w*\s*\(([^)]*)\)")


def _agg_arg_fields(pre_by: str) -> Set[str]:
    """Return lowercase set of field tokens found inside aggregation function call parens.

    For 'values(severity) as severity', returns {'severity'}.  This identifies
    identity-alias fields — where the source field name equals the output alias —
    so that _parse_agg_core does not drop them from inputs just because they also
    appear in out_set_lower.

    Note: 'mitreTechniques{}' is canonicalized to 'mitreTechniques' because
    FIELD_TOKEN_RE stops at '{'.  This is intentional — the field name is the part
    before the brace suffix.
    """
    result: Set[str] = set()
    for m in _FUNC_CALL_ARGS_RE.finditer(pre_by):
        for tok in FIELD_TOKEN_RE.findall(m.group(1)):
            low = tok.lower()
            if low not in STOPWORDS:
                result.add(low)
    return result


def _strip_kv_opts(s: str, opt_keys: Set[str]) -> str:
    """Remove known option KV pairs like 'key=value' where key is in opt_keys."""
    pattern = r"(?i)\b(?:" + "|".join(re.escape(k) for k in opt_keys) + r")\s*=\s*\S+"
    return re.sub(pattern, " ", s)


def _strip_words(s: str, words: Set[str]) -> str:
    """Remove exact bare-word matches (case-insensitive)."""
    pattern = r"(?i)\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"
    return re.sub(pattern, " ", s)


def _split_on_by(s: str) -> Tuple[str, str]:
    """Split 'pre_agg by field_list' into (pre_agg, field_list). Returns ('', '') on no match."""
    m = re.search(r"(?i)\bby\b", s)
    if not m:
        return s.strip(), ""
    return s[:m.start()].strip(), s[m.end():].strip()


# ─────────────────────────────────────────────
# Quote / bracket helpers (shared)
# ─────────────────────────────────────────────

def _extract_bracket_payload(raw: str, kw: str) -> str:
    """Extract first [...] payload after keyword. Returns inner text without brackets."""
    s = raw or ""
    m = re.search(rf"(?ix)\b{re.escape(kw)}\b", s)
    if not m:
        return ""
    i = m.end()
    in_s = in_d = esc = False
    while i < len(s):
        ch = s[i]
        if esc:
            esc = False; i += 1; continue
        if ch == "\\":
            esc = True; i += 1; continue
        if ch == "'" and not in_d:
            in_s = not in_s; i += 1; continue
        if ch == '"' and not in_s:
            in_d = not in_d; i += 1; continue
        if not in_s and not in_d and ch == "[":
            break
        i += 1
    if i >= len(s) or s[i] != "[":
        return ""
    i += 1
    start, depth = i, 1
    in_s = in_d = esc = False
    while i < len(s):
        ch = s[i]
        if esc:
            esc = False; i += 1; continue
        if ch == "\\":
            esc = True; i += 1; continue
        if ch == "'" and not in_d:
            in_s = not in_s; i += 1; continue
        if ch == '"' and not in_s:
            in_d = not in_d; i += 1; continue
        if not in_s and not in_d:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return s[start:i].strip()
        i += 1
    return ""


def _split_first_pipe(spl: str) -> Tuple[str, str]:
    """Split SPL into (before_first_pipe, after_first_pipe), quote-aware."""
    s = (spl or "").strip()
    in_s = in_d = esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False; continue
        if ch == "\\":
            esc = True; continue
        if ch == "'" and not in_d:
            in_s = not in_s; continue
        if ch == '"' and not in_s:
            in_d = not in_d; continue
        if ch == "|" and not in_s and not in_d:
            return s[:i].strip(), s[i+1:].strip()
    return s, ""


def _strip_bracket_block(s: str) -> str:
    """Remove first [...] block and everything after it."""
    idx = s.find("[")
    if idx == -1:
        return s
    return s[:idx].strip()


def _strip_leading_search_keyword(s: str) -> str:
    return re.sub(r"(?ix)^\s*search\b", "", s or "").strip()


# ─────────────────────────────────────────────
# Filter commands (search / where / regex)
# IO is driven by predicate_ir upstream — no dedicated parsers needed here.
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# convert
# ─────────────────────────────────────────────

_CONVERT_CALL_RE = re.compile(
    r"\b(?:ctime|strftime|mktime|timeformat|auto|dur2sec|mstime|rmunit|none)\s*\(\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*\)",
    re.IGNORECASE,
)

def parse_convert(raw: str) -> IOResult:
    ins = _CONVERT_CALL_RE.findall(raw or "")
    return _uniq(ins), _uniq(ins), _meta()


# ─────────────────────────────────────────────
# eval
# ─────────────────────────────────────────────

# Matches: optional_comma  lhs_field  =
_EVAL_LHS_RE = re.compile(
    r'(?:^|,)\s*(?:"(?P<lhsq>[^"]+)"|(?P<lhs>[A-Za-z_][A-Za-z0-9_.]*))\s*=(?!=)',
    re.DOTALL,
)
# Extracts string literals so we can mask them before field extraction
_STRING_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')

def parse_eval(raw: str) -> IOResult:
    # Strip leading "eval" command keyword so regex anchors work correctly
    s = re.sub(r"^\s*eval\s+", "", raw or "", flags=re.IGNORECASE)

    # Step 1: collect LHS assignment targets → outputs
    outs: List[str] = []
    for m in _EVAL_LHS_RE.finditer(s):
        lhs = m.group("lhsq") or m.group("lhs") or ""
        if lhs:
            outs.append(lhs)

    if not outs:
        # Can't parse — fallback to empty, mark failed
        return [], [], _meta(confidence="failed")

    # Step 2: build RHS corpus (everything after the first '=')
    # Mask string literals to avoid extracting words inside them
    rhs_start = s.find("=")
    if rhs_start == -1:
        return outs, outs, _meta(confidence="partial")
    rhs = s[rhs_start + 1:]
    masked_rhs = _STRING_LITERAL_RE.sub(" ", rhs)
    # Mask subsequent assignment LHS tokens (e.g. ", user=" → "  ") so that
    # field names which are assignment targets of later comma-separated
    # assignments in the same eval chain are not picked up as field inputs.
    masked_rhs = _EVAL_LHS_RE.sub(" ", masked_rhs)

    # Step 3: structural function detection on RHS
    func_names = _func_call_names(masked_rhs)

    # Eval control-flow keywords that must never become field refs
    EVAL_KEYWORDS = {
        "if", "case", "validate", "in", "and", "or", "not",
        "true", "false", "null", "like",
    }

    # Step 4: collect field refs from masked RHS
    out_set_lower = {o.lower() for o in outs}
    ins: List[str] = []
    for tok in FIELD_TOKEN_RE.findall(masked_rhs):
        low = tok.lower()
        if low in STOPWORDS:
            continue
        if low in func_names:
            continue
        if low in EVAL_KEYWORDS:
            continue
        # digits-only or starts-with-digit → not a field
        if tok[0].isdigit():
            continue
        ins.append(tok)

    # Fields that appear on both LHS and RHS are inputs too (e.g., eval x=x+1)
    return _uniq(ins), _uniq(outs), _meta()


# ─────────────────────────────────────────────
# rex
# ─────────────────────────────────────────────

_REX_FIELD_RE = re.compile(r"(?i)\bfield\s*=\s*([A-Za-z_][A-Za-z0-9_.:-]*)")
_REX_GROUP_RE = re.compile(r"\(\?P?<([A-Za-z_][A-Za-z0-9_]*)>")
_REX_SED_RE   = re.compile(r"(?i)\bmode\s*=\s*sed\b")

def parse_rex(raw: str) -> IOResult:
    s = raw or ""

    # sed mode: in-place substitution — input=output=source field
    if _REX_SED_RE.search(s):
        fm = _REX_FIELD_RE.search(s)
        src = fm.group(1) if fm else "_raw"
        return [src], [src], _meta()

    # Source field
    fm = _REX_FIELD_RE.search(s)
    src = fm.group(1) if fm else "_raw"

    # Named capture groups → outputs
    groups = _REX_GROUP_RE.findall(s)

    confidence = "full" if groups else "partial"
    return [src], _uniq(groups), _meta(confidence=confidence)


# ─────────────────────────────────────────────
# spath
# ─────────────────────────────────────────────

_SPATH_INPUT_RE  = re.compile(r"(?i)\binput\s*=\s*([A-Za-z_][A-Za-z0-9_.:-]*)")
_SPATH_OUTPUT_RE = re.compile(r"(?i)\boutput\s*=\s*([A-Za-z_][A-Za-z0-9_.:-]*)")
_SPATH_PATH_RE   = re.compile(r"(?i)\bpath\s*=\s*(\S+)")
# Positional argument: spath <path_token>  (no input= or output= present)
_SPATH_POS_RE    = re.compile(r"(?i)^\s*spath\s+([A-Za-z_][A-Za-z0-9_.{}\[\]]*)\s*$")

def parse_spath(raw: str) -> IOResult:
    s = raw or ""

    # Explicit input=
    im = _SPATH_INPUT_RE.search(s)
    # Explicit output=
    om = _SPATH_OUTPUT_RE.search(s)

    if im:
        src = im.group(1)
        outs = [om.group(1)] if om else []
        dyn = not bool(om)
        return [src], outs, _meta(dyn_out=dyn, confidence="full" if outs else "partial")

    # Positional form: spath <json_path>
    # The positional token is the JSON path TO EXTRACT, not the input field.
    # Input defaults to _raw; the path token becomes the output field name.
    pm = _SPATH_POS_RE.match(s)
    if pm:
        path_tok = pm.group(1)
        return ["_raw"], [path_tok], _meta(confidence="partial")

    # No input= and no positional — bare spath or path= only
    pm2 = _SPATH_PATH_RE.search(s)
    if pm2:
        path_str = pm2.group(1)
        # last segment of the path as output field name
        out_name = path_str.rstrip("/").split(".")[-1].split("/")[-1]
        return ["_raw"], ([out_name] if out_name and FIELD_TOKEN_RE.fullmatch(out_name) else []), _meta(dyn_out=True, confidence="partial")

    # Bare 'spath' with no args — applies to _raw
    return ["_raw"], [], _meta(dyn_out=True, confidence="partial")


# ─────────────────────────────────────────────
# fillnull
# ─────────────────────────────────────────────

_FILLNULL_OPT_RE = re.compile(
    r"(?ix)\bvalue\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)\b|\b(?:all|field)\s*=\s*[^\s]+"
)

def parse_fillnull(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*fillnull\b", "", raw or "").strip()
    body = _FILLNULL_OPT_RE.sub(" ", s).strip()
    fields = [t for t in re.split(r"[,\s]+", body) if t and FIELD_TOKEN_RE.fullmatch(t)]
    if not fields:
        # All-fields form: fillnull value=0
        return [], [], _meta(scope="all_fields", confidence="full")
    return _uniq(fields), _uniq(fields), _meta()


# ─────────────────────────────────────────────
# mvexpand
# ─────────────────────────────────────────────

_MVEXPAND_RE = re.compile(r"(?ix)^\s*mvexpand\s+([A-Za-z_][A-Za-z0-9_.:-]*)\b")

def parse_mvexpand(raw: str) -> IOResult:
    m = _MVEXPAND_RE.match(raw or "")
    if not m:
        return [], [], _meta(confidence="failed")
    f = m.group(1)
    return [f], [f], _meta()


# ─────────────────────────────────────────────
# makemv
# ─────────────────────────────────────────────

_MAKEMV_OPT_RE = re.compile(
    r"(?ix)\b(?:delim|tokenizer|allowempty|setsv)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)"
)

def parse_makemv(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*makemv\b", "", raw or "").strip()
    body = _MAKEMV_OPT_RE.sub(" ", s).strip()
    toks = [t for t in re.split(r"[,\s]+", body) if t and FIELD_TOKEN_RE.fullmatch(t)]
    if not toks:
        return [], [], _meta(confidence="failed")
    f = toks[-1]  # last positional token is the field
    return [f], [f], _meta()


# ─────────────────────────────────────────────
# replace
# ─────────────────────────────────────────────

_REPLACE_IN_RE = re.compile(r"(?i)\bin\s+(.+)$")

def parse_replace(raw: str) -> IOResult:
    m = _REPLACE_IN_RE.search(raw or "")
    if not m:
        return [], [], _meta(confidence="failed")
    fields = [t for t in re.split(r"[,\s]+", m.group(1).strip())
              if t and FIELD_TOKEN_RE.fullmatch(t)]
    if not fields:
        return [], [], _meta(confidence="failed")
    return _uniq(fields), _uniq(fields), _meta()


# ─────────────────────────────────────────────
# xmlkv
# ─────────────────────────────────────────────

_XMLKV_FIELD_RE = re.compile(r"(?i)\bfield\s*=\s*([A-Za-z_][A-Za-z0-9_.:-]*)")
_XMLKV_POS_RE   = re.compile(r"(?i)^\s*xmlkv\s+([A-Za-z_][A-Za-z0-9_.:-]*)\s*$")

def parse_xmlkv(raw: str) -> IOResult:
    s = raw or ""
    fm = _XMLKV_FIELD_RE.search(s)
    if fm:
        return [fm.group(1)], [], _meta(dyn_out=True, confidence="partial")
    pm = _XMLKV_POS_RE.match(s)
    if pm:
        return [pm.group(1)], [], _meta(dyn_out=True, confidence="partial")
    return ["_raw"], [], _meta(dyn_out=True, confidence="partial")


# ─────────────────────────────────────────────
# iplocation
# ─────────────────────────────────────────────

_IPLOC_RE = re.compile(r"(?ix)^\s*iplocation\s+([A-Za-z_][A-Za-z0-9_.:-]*)\b")

def parse_iplocation(raw: str) -> IOResult:
    m = _IPLOC_RE.match(raw or "")
    if not m:
        return [], [], _meta(dyn_out=True, confidence="partial")
    return [m.group(1)], [], _meta(dyn_out=True, confidence="partial")


# ─────────────────────────────────────────────
# bucket / bin
# ─────────────────────────────────────────────

_BUCKET_RE = re.compile(r"(?ix)^\s*(?:bucket|bin)\b(?P<body>.*)")
_BUCKET_OPT_RE = re.compile(r"(?ix)\b(?:span|bins|start|end|aligntime|minspan)\s*=\s*\S+")

def parse_bucket_bin(raw: str) -> IOResult:
    m = _BUCKET_RE.match(raw or "")
    if not m:
        return [], [], _meta(confidence="failed")
    body = _BUCKET_OPT_RE.sub(" ", m.group("body") or "").strip()
    toks = [t for t in re.split(r"\s+", body) if t and FIELD_TOKEN_RE.fullmatch(t)]
    field = None
    for t in reversed(toks):
        if t.lower() not in STOPWORDS:
            field = t
            break
    if not field:
        return [], [], _meta(confidence="failed")
    return [field], [field], _meta()


# ─────────────────────────────────────────────
# lookup
# ─────────────────────────────────────────────

_LOOKUP_HEAD_RE = re.compile(r"(?ix)^\s*lookup\s+(?P<table>[A-Za-z0-9_.:-]+)(?P<body>.*)")
_LOOKUP_AS_RE   = re.compile(r"(?ix)\b([A-Za-z_][A-Za-z0-9_.:-]*)\s+as\s+([A-Za-z_][A-Za-z0-9_.:-]*)")
_LOOKUP_OUT_RE  = re.compile(r"(?ix)\bOUTPUT(?:NEW)?\b\s*(?P<outs>.+)$")

def parse_lookup(raw: str) -> IOResult:
    mh = _LOOKUP_HEAD_RE.match(raw or "")
    if not mh:
        return [], [], _meta(confidence="failed")
    body = mh.group("body") or ""

    # Inputs: event fields from "lk_field as event_field" pairs
    ins: List[str] = []
    for _lk, ev in _LOOKUP_AS_RE.findall(body):
        ins.append(ev)

    # Outputs: OUTPUT/OUTPUTNEW clause
    outs: List[str] = []
    om = _LOOKUP_OUT_RE.search(body)
    if om:
        for t in re.split(r"[,\s]+", om.group("outs").strip()):
            if t and FIELD_TOKEN_RE.fullmatch(t) and t.lower() not in STOPWORDS:
                outs.append(t)

    # Bare key fields: tokens before any OUTPUT keyword that were NOT part of an 'as' pair
    # and don't look like option keys
    if not ins:
        # Extract body up to OUTPUT keyword
        body_before_out = re.split(r"(?i)\bOUTPUT(?:NEW)?\b", body)[0]
        # Remove all 'lk_field as event_field' patterns
        body_clean = _LOOKUP_AS_RE.sub(" ", body_before_out)
        bare_toks = [t for t in _fieldish_tokens(body_clean)
                     if t.lower() not in {"outputnew", "output"}]
        ins.extend(bare_toks[:3])  # conservative: at most 3 bare key fields

    has_out = bool(outs)
    dyn = not has_out
    conf = "full" if has_out else "partial"
    return _uniq(ins), _uniq(outs), _meta(dyn_out=dyn, confidence=conf)


# ─────────────────────────────────────────────
# rename
# ─────────────────────────────────────────────

_RENAME_BODY_RE = re.compile(r"(?ix)^\s*rename\b\s*(?P<body>.+)$")
_RENAME_PAIR_RE = re.compile(
    r"""(?ix)
    (?:"(?P<srcq>[^"]+)" | (?P<src>[A-Za-z_][A-Za-z0-9_.:-]*))
    \s+(?:as\s+)?
    (?P<dst>[A-Za-z_][A-Za-z0-9_.:-]*)
    """
)

def parse_rename(raw: str) -> IOResult:
    mh = _RENAME_BODY_RE.search(raw or "")
    if not mh:
        return [], [], _meta(confidence="failed")
    body = mh.group("body") or ""

    # Detect wildcard form: rename Processes.* as *
    if "*" in body:
        return [], [], _meta(confidence="partial")

    ins, outs = [], []
    for mm in _RENAME_PAIR_RE.finditer(body):
        src = mm.group("srcq") or mm.group("src")
        dst = mm.group("dst")
        if src and dst and dst.lower() != "as":
            ins.append(src)
            outs.append(dst)
    conf = "full" if ins else "failed"
    return _uniq(ins), _uniq(outs), _meta(confidence=conf)


# ─────────────────────────────────────────────
# table / fields
# ─────────────────────────────────────────────

_TABLE_RE = re.compile(r"(?ix)^\s*(?:table|fields)\s+(?P<body>.+)$")

def parse_table_fields(raw: str) -> IOResult:
    m = _TABLE_RE.match(raw or "")
    if not m:
        return [], [], _meta(confidence="failed")
    body = m.group("body") or ""
    toks = [t.strip() for t in re.split(r"[,\s]+", body) if t.strip()]
    fields = [t for t in toks if FIELD_TOKEN_RE.fullmatch(t)]
    return _uniq(fields), _uniq(fields), _meta()


# ─────────────────────────────────────────────
# convert
# (already defined above — no duplicate needed)
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# sort
# ─────────────────────────────────────────────

_SORT_OPT_RE = re.compile(r"(?ix)\b(?:num|str|ip|auto)\s*\(")  # sort functions

def parse_sort(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*sort\b", "", raw or "").strip()
    # Strip optional row-count integer at the front
    s = re.sub(r"^\s*\d+\s*", "", s)
    # Strip -/+ direction prefixes
    s = re.sub(r"(?<!\w)[+\-]", " ", s)
    # Strip sort-function wrappers: num(field) → field
    # Extract field names from sort functions if present
    func_fields = re.findall(r"(?i)\b(?:num|str|ip|auto)\s*\(\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*\)", s)
    s_clean = _SORT_OPT_RE.sub(" ", s)
    s_clean = re.sub(r"\([^)]*\)", " ", s_clean)  # remove remaining parens
    bare_fields = [t for t in _fieldish_tokens(s_clean)]
    all_fields = _uniq(func_fields + bare_fields)
    return all_fields, [], _meta(schema_preserving=True)


# ─────────────────────────────────────────────
# dedup
# ─────────────────────────────────────────────

_DEDUP_OPT_RE = re.compile(r"(?ix)\b(?:keepevents|keepempty|consecutive)\s*=\s*\S+")

def parse_dedup(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*dedup\b", "", raw or "").strip()
    # Strip optional count integer
    s = re.sub(r"^\s*\d+\s*", "", s)
    # Strip options
    s = _DEDUP_OPT_RE.sub(" ", s)
    fields = [t for t in _fieldish_tokens(s)]
    return fields, [], _meta(schema_preserving=True)


# ─────────────────────────────────────────────
# head / tail
# ─────────────────────────────────────────────

def parse_head_tail(raw: str) -> IOResult:
    return [], [], _meta(schema_preserving=True, confidence="full")


# ─────────────────────────────────────────────
# transaction
# ─────────────────────────────────────────────

_TRANSACTION_OPT_RE = re.compile(
    r"""(?ix)
    \b(?:startswith|endswith|connected|maxspan|maxpause|maxevents|
         keepevents|mvlist|unifyends|delim|fields|startswith_re|endswith_re)
    \s*=\s*(?:"[^"]*"|'[^']*'|\[[^\]]*\]|[^\s]+)
    """
)

def parse_transaction(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*transaction\b", "", raw or "").strip()
    body = _TRANSACTION_OPT_RE.sub(" ", s).strip()
    # Also strip any remaining '( ... )' blocks from startswith/endswith expressions
    body = re.sub(r"\([^)]*\)", " ", body)
    fields = [t for t in _fieldish_tokens(body)]
    if not fields:
        return [], [], _meta(confidence="partial")
    outs = _uniq(fields + ["duration", "eventcount"])
    return fields, outs, _meta(confidence="full")


# ─────────────────────────────────────────────
# join
# ─────────────────────────────────────────────

_JOIN_OPT_RE = re.compile(
    r"(?ix)\b(?:type|usetime|earlier|overwrite|max|keepsingle|delim)\s*=\s*\S+"
)

def parse_join(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*join\b", "", raw or "").strip()
    # Strip options
    s = _JOIN_OPT_RE.sub(" ", s)
    # Remove subsearch bracket block
    s = _strip_bracket_block(s)
    fields = [t for t in _fieldish_tokens(s)]
    conf = "full" if fields else "partial"
    return fields, [], _meta(dyn_out=True, confidence=conf)


# ─────────────────────────────────────────────
# stats / eventstats / streamstats / timechart / chart
# ─────────────────────────────────────────────

_STATS_HEAD_RE = re.compile(
    r"(?ix)^\s*(stats|eventstats|streamstats|timechart|chart)\b(?P<body>.*)"
)
_AS_NAME_RE = re.compile(r"(?ix)\bas\s+([A-Za-z_][A-Za-z0-9_.:-]*)\b")

def _parse_agg_core(
    cmd: str,
    body: str,
) -> Tuple[List[str], List[str], str]:
    """
    Core parsing for stats/eventstats/etc.
    Returns (inputs, outputs, confidence).
    """
    pre_by, by_part = _split_on_by(body)

    # Structural function detection on pre_by
    func_names = _func_call_names(pre_by)

    # Explicit aliases
    explicit_outs = _AS_NAME_RE.findall(pre_by)
    out_set_lower = {o.lower() for o in explicit_outs}

    # Implicit bare agg function names (e.g., bare 'count' with no parens and no alias)
    implicit_outs: List[str] = []
    for tok in FIELD_TOKEN_RE.findall(pre_by):
        low = tok.lower()
        if low in STATS_BARE_FUNCS and low not in func_names and low not in out_set_lower:
            implicit_outs.append(low)
            out_set_lower.add(low)

    all_outs_pre = _uniq(explicit_outs + implicit_outs)

    # By-clause fields
    by_fields = [
        t for t in re.split(r"[,\s]+", by_part)
        if t and FIELD_TOKEN_RE.fullmatch(t) and t.lower() not in STOPWORDS
    ]

    # Inputs: field tokens from pre_by that are NOT function call names and NOT pure aliases.
    # "Pure alias" = alias whose lowercase form does NOT appear as a function argument.
    # Identity-alias fields (e.g. values(severity) as severity) must remain in inputs
    # because they are true inputs to the aggregation even though their name == the alias.
    agg_arg_lower = _agg_arg_fields(pre_by)
    ins: List[str] = []
    for tok in FIELD_TOKEN_RE.findall(pre_by):
        low = tok.lower()
        if low in STOPWORDS:
            continue
        if low in func_names:
            continue
        # Only suppress if it's an alias that is NOT also a function argument.
        if low in out_set_lower and low not in agg_arg_lower:
            continue
        ins.append(tok)
    ins.extend(by_fields)
    ins = [x for x in _uniq(ins) if x.lower() not in out_set_lower or x.lower() in agg_arg_lower]

    # Outputs:
    # - stats / timechart / chart: by-fields also appear in result schema
    # - eventstats / streamstats: additive — by-fields are NOT new outputs
    if cmd in ("stats", "timechart", "chart"):
        all_outs = _uniq(all_outs_pre + by_fields)
    else:
        all_outs = all_outs_pre

    conf = "full" if all_outs else "partial"
    return _uniq(ins), _uniq(all_outs), conf


def parse_stats_like(raw: str) -> IOResult:
    m = _STATS_HEAD_RE.match(raw or "")
    if not m:
        return [], [], _meta(confidence="failed")
    cmd = m.group(1).lower()
    body = (m.group("body") or "").strip()
    ins, outs, conf = _parse_agg_core(cmd, body)
    return ins, outs, _meta(confidence=conf)


# ─────────────────────────────────────────────
# top / rare
# ─────────────────────────────────────────────

_TOP_RARE_HEAD_RE = re.compile(r"(?ix)^\s*(top|rare)\b(?P<body>.*)")
_TOP_RARE_OPT_RE = re.compile(
    r"(?ix)\b(?:limit|showperc|showcount|countfield|percentfield|useother|otherstr)\s*=\s*\S+"
)

def parse_top_rare(raw: str) -> IOResult:
    m = _TOP_RARE_HEAD_RE.match(raw or "")
    if not m:
        return [], [], _meta(confidence="failed")
    body = (m.group("body") or "").strip()
    # Strip options
    body = _TOP_RARE_OPT_RE.sub(" ", body)
    # Strip optional count integer
    body = re.sub(r"^\s*\d+\s*", "", body)

    pre_by, by_part = _split_on_by(body)

    # Field list (what's being counted)
    field_toks = [t for t in _fieldish_tokens(pre_by)]
    # By-clause fields
    by_fields = [
        t for t in re.split(r"[,\s]+", by_part)
        if t and FIELD_TOKEN_RE.fullmatch(t) and t.lower() not in STOPWORDS
    ]

    ins = _uniq(field_toks + by_fields)
    # top/rare always produces count and percent columns; by-fields also in output
    outs = _uniq(field_toks + by_fields + ["count", "percent"])
    return ins, outs, _meta(confidence="full" if ins else "partial")


# ─────────────────────────────────────────────
# tstats  (P0 — critical)
# ─────────────────────────────────────────────

_TSTATS_FROM_RE    = re.compile(r"(?i)\bfrom\s+datamodel\s*=\s*([^\s]+)")
_TSTATS_PRESTATS_RE = re.compile(r"(?i)\bprestats\s*=\s*t(?:rue)?\b")
# All-caps KV pairs are macro substitution placeholders (e.g. MACRO=summariesonly)
_TSTATS_MACRO_KV_RE = re.compile(r"\b[A-Z][A-Z0-9_]*\s*=\s*\S+")

def parse_tstats(raw: str) -> IOResult:
    s = raw or ""

    # Detect prestats=t: in this mode tstats IS a pipeline consumer
    is_prestats = bool(_TSTATS_PRESTATS_RE.search(s))

    # Extract datamodel reference
    dm_match = _TSTATS_FROM_RE.search(s)
    dataset = dm_match.group(1) if dm_match else None

    # Strip: macro KV placeholders + known option bare-words + 'tstats' itself
    clean = re.sub(r"(?ix)^\s*tstats\b", "", s).strip()
    clean = _TSTATS_MACRO_KV_RE.sub(" ", clean)
    clean = _strip_words(clean, TSTATS_OPTION_WORDS)

    # Remove 'from ... where ...' block so datamodel refs don't pollute field lists
    clean = re.sub(r"(?i)\bfrom\b.*?(?=\bby\b|$)", " ", clean, flags=re.DOTALL)

    # Split on 'by'
    pre_by, by_part = _split_on_by(clean)

    # Outputs 1: explicit AS aliases
    explicit_outs = _AS_NAME_RE.findall(pre_by)
    out_set_lower = {o.lower() for o in explicit_outs}

    # Implicit: bare 'count' with no alias
    implicit_outs: List[str] = []
    for tok in FIELD_TOKEN_RE.findall(pre_by):
        low = tok.lower()
        if low in STATS_BARE_FUNCS and low not in _func_call_names(pre_by) and low not in out_set_lower:
            implicit_outs.append(low)
            out_set_lower.add(low)

    # Outputs 2: by-clause fields (they appear in the tstats result schema)
    by_fields_raw = [
        t for t in re.split(r"[,\s]+", by_part)
        if t and FIELD_TOKEN_RE.fullmatch(t) and t.lower() not in STOPWORDS
    ]
    # Strip span= options that may appear in by-clause
    by_fields = [t for t in by_fields_raw if not t.lower().startswith("span")]

    all_outs = _uniq(explicit_outs + implicit_outs + by_fields)

    if is_prestats:
        # prestats mode: tstats consumes the pipeline — treat like eventstats
        func_names = _func_call_names(pre_by)
        agg_arg_lower = _agg_arg_fields(pre_by)
        ins: List[str] = []
        for tok in FIELD_TOKEN_RE.findall(pre_by):
            low = tok.lower()
            if low in STOPWORDS or low in func_names:
                continue
            if low in out_set_lower and low not in agg_arg_lower:
                continue
            ins.append(tok)
        ins.extend(by_fields)
        ins = [x for x in _uniq(ins) if x.lower() not in out_set_lower or x.lower() in agg_arg_lower]
        conf = "full" if all_outs else "partial"
        return ins, all_outs, _meta(scope="specific", confidence=conf, dataset=dataset)

    # Normal tstats: source stage — no pipeline inputs
    conf = "full" if all_outs else "partial"
    return [], all_outs, _meta(scope="source", confidence=conf, dataset=dataset)


# ─────────────────────────────────────────────
# fit (MLTK)
# ─────────────────────────────────────────────

_FIT_INTO_RE  = re.compile(r"(?i)\binto\s+([A-Za-z0-9_]+)")
_FIT_FROM_RE  = re.compile(r"(?i)\bfrom\s+([A-Za-z_][A-Za-z0-9_.:-]*)")
_FIT_OPT_RE   = re.compile(r"(?ix)\b\w+\s*=\s*\S+")  # any kv option

def parse_fit(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*fit\b", "", raw or "").strip()

    # Extract model name (into <name>)
    im = _FIT_INTO_RE.search(s)
    model_name = im.group(1) if im else None
    s_clean = _FIT_INTO_RE.sub(" ", s) if im else s

    # Extract target field (from <field>)
    fm = _FIT_FROM_RE.search(s_clean)
    target = fm.group(1) if fm else None
    s_clean = _FIT_FROM_RE.sub(" ", s_clean) if fm else s_clean

    # Strip options
    s_clean = _FIT_OPT_RE.sub(" ", s_clean)

    # First remaining token is the algorithm name — skip it
    toks = [t for t in _fieldish_tokens(s_clean)]
    feature_fields = toks[1:] if len(toks) > 1 else toks

    if target:
        feature_fields = _uniq(feature_fields + [target])

    return feature_fields, [], _meta(
        dyn_out=True, confidence="partial" if feature_fields else "failed",
        dataset=model_name,
    )


# ─────────────────────────────────────────────
# apply (MLTK)
# ─────────────────────────────────────────────

_APPLY_AS_RE  = re.compile(r"(?i)\bas\s+([A-Za-z_][A-Za-z0-9_.:-]*)")
_APPLY_OPT_RE = re.compile(r"(?ix)\b\w+\s*=\s*\S+")

def parse_apply(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*apply\b", "", raw or "").strip()
    am = _APPLY_AS_RE.search(s)
    outs = [am.group(1)] if am else []
    # First token is model name (metadata, not a field)
    s_clean = _APPLY_AS_RE.sub(" ", s) if am else s
    s_clean = _APPLY_OPT_RE.sub(" ", s_clean)
    toks = _fieldish_tokens(s_clean)
    model_name = toks[0] if toks else None
    dyn = not bool(outs)
    return [], outs, _meta(
        scope="all_fields", dyn_out=dyn,
        confidence="full" if outs else "partial",
        dataset=model_name,
    )


# ─────────────────────────────────────────────
# select (SPL2)
# ─────────────────────────────────────────────

def parse_select(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*select\b", "", raw or "").strip()
    # Remove star (wildcard select)
    if s.strip() == "*":
        return [], [], _meta(scope="all_fields", confidence="full")
    toks = [t.strip() for t in re.split(r"[,\s]+", s) if t.strip()]
    fields = [t for t in toks if FIELD_TOKEN_RE.fullmatch(t)]
    return [], _uniq(fields), _meta(scope="all_fields", confidence="full" if fields else "partial")


# ─────────────────────────────────────────────
# addtotals
# ─────────────────────────────────────────────

_ADDTOTALS_OPT_RE = re.compile(
    r"(?ix)\b(?:row|col|fieldname|labelfield|label)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)"
)

def parse_addtotals(raw: str) -> IOResult:
    s = re.sub(r"(?ix)^\s*addtotals\b", "", raw or "").strip()
    # Extract fieldname= if present (name of the totals column)
    fn_match = re.search(r"(?i)\bfieldname\s*=\s*([A-Za-z_][A-Za-z0-9_.:-]*)", s)
    total_field = fn_match.group(1) if fn_match else "Total"
    body = _ADDTOTALS_OPT_RE.sub(" ", s)
    fields = [t for t in _fieldish_tokens(body)]
    # addtotals sums the listed fields (or all numeric if none listed)
    if fields:
        return fields, _uniq(fields + [total_field]), _meta()
    return [], [total_field], _meta(scope="all_fields")


# ─────────────────────────────────────────────
# inputlookup / outputlookup / from / into
# ─────────────────────────────────────────────

def parse_inputlookup(raw: str) -> IOResult:
    # Table name is metadata; columns are dynamic/unknown
    m = re.search(r"(?i)\binputlookup\s+(?:append\s*=\s*\S+\s+)?([A-Za-z0-9_.:-]+)", raw or "")
    table = m.group(1) if m else None
    return [], [], _meta(scope="source", dyn_out=True, confidence="partial", dataset=table)


def parse_outputlookup(raw: str) -> IOResult:
    m = re.search(r"(?i)\boutputlookup\s+(?:[^\s]+\s*=\s*\S+\s+)*([A-Za-z0-9_.:-]+)", raw or "")
    table = m.group(1) if m else None
    return [], [], _meta(scope="all_fields", confidence="full", dataset=table)


def parse_from_into(raw: str, cmd: str) -> IOResult:
    # Extract datamodel / dataset reference
    m = re.search(r"(?i)\b(?:datamodel|dataset|index)\s*=\s*([^\s]+)", raw or "")
    dataset = m.group(1) if m else None
    if not dataset:
        # bare 'from MyDataset'
        m2 = re.search(rf"(?i)\\b{cmd}\\s+([A-Za-z0-9_.:-]+)", raw or "")
        dataset = m2.group(1) if m2 else None
    scope = "source" if cmd == "from" else "sink"
    return [], [], _meta(scope=scope, confidence="full", dataset=dataset)


# ─────────────────────────────────────────────
# append / appendpipe / map  (branch — IO driven by subsearch, handled upstream)
# ─────────────────────────────────────────────

def parse_append(raw: str) -> Tuple[List[str], List[str]]:
    """Legacy 2-tuple for callers in build_unified_ir.py that bypass infer_io."""
    if not re.match(r"(?ix)^\s*append\b", raw or ""):
        return [], []
    payload = _extract_bracket_payload(raw, "append")
    if not payload:
        return [], []
    base, _ = _split_first_pipe(payload)
    base = _strip_leading_search_keyword(base)
    return _uniq(_fieldish_tokens(base)), []


def parse_appendpipe(raw: str) -> Tuple[List[str], List[str]]:
    if not re.match(r"(?ix)^\s*appendpipe\b", raw or ""):
        return [], []
    payload = _extract_bracket_payload(raw, "appendpipe")
    if not payload:
        return [], []
    return _uniq(_fieldish_tokens(payload)), []


def parse_map(raw: str) -> Tuple[List[str], List[str]]:
    if not re.match(r"(?ix)^\s*map\b", raw or ""):
        return [], []
    m = re.search(r'(?ix)\bsearch\s*=\s*("(?P<dq>(?:\\.|[^"\\])*)"|\'(?P<sq>(?:\\.|[^\'\\])*)\')', raw)
    if not m:
        return [], []
    tpl = m.group("dq") if m.group("dq") is not None else (m.group("sq") or "")
    vars_ = re.findall(r"\$([A-Za-z_][A-Za-z0-9_.:-]*)\$", tpl)
    base, _ = _split_first_pipe(tpl)
    base = _strip_leading_search_keyword(base)
    return _uniq(_fieldish_tokens(base) + vars_), []


# ─────────────────────────────────────────────
# foreach
# ─────────────────────────────────────────────

_FOREACH_OPT_RE = re.compile(r"(?ix)\b(?:fieldstr|matchseg\d+|matchstr)\s*=\s*\S+")
_FOREACH_GLOB_RE = re.compile(r"(?ix)^\s*foreach\s+(?:\w+=\S+\s+)*([^\[]+?)(?:\s*\[|$)")

def parse_foreach(raw: str) -> IOResult:
    # Strip options
    s = _FOREACH_OPT_RE.sub(" ", raw or "")
    m = _FOREACH_GLOB_RE.match(s)
    pattern = m.group(1).strip() if m else None
    if pattern:
        return [], [], _meta(scope="pattern", pattern=pattern,
                             schema_preserving=True, confidence="partial")
    return [], [], _meta(scope="pattern", schema_preserving=True, confidence="failed")


# ─────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────

def infer_io_ext(stage_raw: str, stage_type: str) -> IOResult:
    """
    Full IO inference: returns (inputs, outputs, io_meta).
    io_meta always present; use io_meta['parse_confidence'] to assess reliability.
    """
    t = (stage_type or "unknown").lower().strip()
    s = stage_raw or ""

    # Recover type from raw if 'unknown'
    if t == "unknown":
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\b", s)
        if m:
            t = m.group(1).lower()

    if t == "convert":
        return parse_convert(s)
    if t == "eval":
        return parse_eval(s)
    if t == "rex":
        return parse_rex(s)
    if t == "lookup":
        return parse_lookup(s)
    if t == "rename":
        return parse_rename(s)
    if t in ("table", "fields"):
        return parse_table_fields(s)
    if t == "spath":
        return parse_spath(s)
    if t == "fillnull":
        return parse_fillnull(s)
    if t == "mvexpand":
        return parse_mvexpand(s)
    if t == "makemv":
        return parse_makemv(s)
    if t == "replace":
        return parse_replace(s)
    if t == "xmlkv":
        return parse_xmlkv(s)
    if t == "iplocation":
        return parse_iplocation(s)
    if t in ("bucket", "bin"):
        return parse_bucket_bin(s)
    if t == "tstats":
        return parse_tstats(s)
    if t in ("stats", "eventstats", "streamstats", "timechart", "chart"):
        return parse_stats_like(s)
    if t in ("top", "rare"):
        return parse_top_rare(s)
    if t == "sort":
        return parse_sort(s)
    if t == "dedup":
        return parse_dedup(s)
    if t in ("head", "tail"):
        return parse_head_tail(s)
    if t == "join":
        return parse_join(s)
    if t == "transaction":
        return parse_transaction(s)
    if t == "fit":
        return parse_fit(s)
    if t == "apply":
        return parse_apply(s)
    if t == "select":
        return parse_select(s)
    if t == "addtotals":
        return parse_addtotals(s)
    if t == "foreach":
        return parse_foreach(s)
    if t == "inputlookup":
        return parse_inputlookup(s)
    if t == "outputlookup":
        return parse_outputlookup(s)
    if t == "from":
        return parse_from_into(s, "from")
    if t == "into":
        return parse_from_into(s, "into")

    # Branch commands: IO is handled upstream via subsearch parsing
    if t in ("append", "appendpipe", "union", "map", "multisearch"):
        return [], [], _meta(scope="unknown", confidence="partial")

    # Macro: opaque
    if t == "macro":
        return [], [], _meta(scope="unknown", confidence="failed")

    # format / fieldformat / apply (non-MLTK) / misc presentation
    if t in ("format", "fieldformat"):
        return [], [], _meta(schema_preserving=True, confidence="full")

    # Generic fallback for recognized commands that have no dedicated parser
    if t not in ("unknown",):
        ins = _fieldish_tokens(s)
        # Suppress command-name leak: if first token equals the command name, drop it
        if ins and ins[0].lower() == t:
            ins = ins[1:]
        return ins, [], _meta(confidence="partial")

    return [], [], _meta(scope="unknown", confidence="failed")


def infer_io(stage_raw: str, stage_type: str) -> Tuple[List[str], List[str]]:
    """Backward-compatible 2-tuple wrapper around infer_io_ext."""
    ins, outs, _ = infer_io_ext(stage_raw, stage_type)
    return ins, outs
