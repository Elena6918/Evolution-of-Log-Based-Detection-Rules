"""
ir_builder.py
=============
Core SPL → Unified IR logic extracted from
security_content/tree_analysis/build_unified_ir.py.

Public API
----------
  make_builder(grammar_name="boolean_expr.lark", quiet=False) -> UnifiedIRBuilder
      Load grammar from lib/ and return a ready-to-use builder.

  UnifiedIRBuilder.build_from_text(spl_text: str) -> dict
      Convert a single SPL string to a Unified IR dict.

The grammar files (boolean_expr.lark, boolean_expr_old.lark, spl2cst.lark)
live alongside this module in lib/.  spl_bool_fallback and spl_pipeline_io_infer
are imported from the same package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Set

from lark import Transformer, Tree, Token, Lark

from .spl_pipeline_io_infer import infer_io, infer_io_ext          # noqa: F401
from .spl_bool_fallback import parse_boolean_expr_fallback, strip_subsearches, strip_trailing_spl_commands


# ============================================================
# 0) Boolean grammar utilities
# ============================================================

OP_TOKENS = {
    "EQ", "NE", "NEQ",
    "GT", "GE", "LT", "LE",
    "LIKE", "CONTAINS",
    "STARTSWITH", "ENDSWITH",
    "MATCHES",
}

BOOL_CUE_RE = re.compile(
    r"""
    \b(AND|OR|NOT|IN|LIKE|MATCHES|CONTAINS|STARTSWITH|ENDSWITH)\b
    | (=|!=|>=|<=|<|>)
    | \(
    """,
    re.IGNORECASE | re.VERBOSE
)

_SUBSEARCH_FIELD_RE = r'(?:`[^`]+`|"(?:\\.|[^"\\])+"|[A-Za-z_][A-Za-z0-9_.:{}\-]*)'


# ============================================================
# 1) CST-ish IR transformer for boolean_expr.lark parse trees
# ============================================================

class ToRawIR(Transformer):
    KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}

    OP_NORMALIZE = {
        "=": "EQ",
        "==": "EQ",
        "!=": "NE",
        ">": "GT",
        ">=": "GE",
        "<": "LT",
        "<=": "LE",
        "LIKE": "LIKE",
        "CONTAINS": "CONTAINS",
        "STARTSWITH": "STARTSWITH",
        "ENDSWITH": "ENDSWITH",
        "MATCHES": "MATCHES_REGEX",
    }

    def __default__(self, data, children, meta):
        if len(children) == 1:
            return children[0]
        return children

    def ESCAPED_STRING(self, t):
        s = str(t)[1:-1]
        if "*" in s or "?" in s:
            return {"type": "value", "subtype": "wildcard", "value": s}
        if any(x in s for x in ["\\d", ".", "^", "$", "(", ")", "+", "?"]):
            return {"type": "value", "subtype": "regex_candidate", "value": s}
        return {"type": "value", "subtype": "string", "value": s}

    def SIMPLE_QUOTED_FIELD(self, t):
        return {"type": "field", "value": str(t)[1:-1]}

    def BACKTICK_FIELD(self, t):
        return {"type": "field", "value": str(t)[1:-1]}

    def BARE_WILDCARD(self, t):
        return {"type": "value", "subtype": "wildcard", "value": str(t)}

    def NUMBER(self, t):
        raw = str(t)
        if "." in raw:
            return {"type": "value", "subtype": "float", "value": float(raw)}
        return {"type": "value", "subtype": "int", "value": int(raw)}

    def CNAME(self, t):
        text = str(t)
        low = text.lower()
        if low in self.KEYWORDS:
            if low == "true":
                return {"type": "value", "subtype": "bool", "value": True}
            if low == "false":
                return {"type": "value", "subtype": "bool", "value": False}
            if low == "null":
                return {"type": "value", "subtype": "null", "value": None}
            return {"type": "keyword", "value": low}
        return {"type": "identifier", "value": text}

    def OPERATOR(self, t):
        raw = str(t).upper()
        return {"type": "operator", "value": self.OP_NORMALIZE.get(raw, raw)}

    # Predicates
    def predicate_full(self, items):
        field, op, val = items
        if isinstance(val, dict) and val.get("type") == "identifier":
            val = {"type": "value", "subtype": "raw_identifier", "value": val["value"]}
        return {"type": "predicate", "field": field, "operator": op, "value": val}

    def predicate_in_list(self, items):
        field = items[0]
        values = items[-1]
        normalized = []
        for v in values:
            if isinstance(v, dict) and v.get("type") == "identifier":
                normalized.append({"type": "value", "subtype": "raw_identifier", "value": v["value"]})
            else:
                normalized.append(v)
        return {
            "type": "predicate",
            "field": field,
            "operator": {"type": "operator", "value": "IN"},
            "value": normalized,
        }

    def value_item(self, items):
        return items[0] if items else None

    def value_list(self, items):
        return list(items)

    def predicate_literal_only(self, items):
        val = items[0]
        return {
            "type": "predicate",
            "field": {"type": "field", "value": "_raw"},
            "operator": {"type": "operator", "value": "CONTAINS"},
            "value": val,
        }

    # Boolean
    def OR(self, _): return None
    def AND(self, _): return None
    def NOT(self, _): return None
    def IN(self, _): return None
    def implicit_and(self, _): return None

    def not_expr_node(self, items):
        return {"type": "expr", "op": "NOT", "children": items[1:]}

    def and_expr(self, items):
        c = [x for x in items if x is not None]
        if len(c) == 1:
            return c[0]
        return {"type": "expr", "op": "AND", "children": c}

    def or_expr(self, items):
        c = [x for x in items if x is not None]
        if len(c) == 1:
            return c[0]
        return {"type": "expr", "op": "OR", "children": c}

    def group_expr(self, items):
        return items[0] if items else None

    def expr(self, items):
        return items[0] if items else None


raw_ir_transformer = ToRawIR()


def ir_to_builtin(obj):
    if isinstance(obj, Tree):
        return {"type": obj.data, "children": [ir_to_builtin(c) for c in obj.children]}
    if isinstance(obj, Token):
        return obj.value
    if isinstance(obj, dict):
        return {k: ir_to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [ir_to_builtin(x) for x in obj]
    return obj


# ============================================================
# 2) SPL2/SSA "program mode" helpers
# ============================================================

_SSA_PROGRAM_CUE_RE = re.compile(r"(?im)^\s*\$?[A-Za-z_]\w*\s*=\s*(?:$|\||from\b)")
_SSA_LEADING_PIPE_CUE_RE = re.compile(r"(?m)^\s*\|\s*from\b")
_SSA_INTO_CUE_RE = re.compile(r"(?i)\binto\s+[A-Za-z_]\w*\s*\(")

_SPL2_ASSIGN_HDR_RE = re.compile(r"(?is)^\s*(?P<var>\$?[A-Za-z_]\w*)\s*=\s*(?P<body>.*)$")
_SPL2_ASSIGN_FROM_RE = re.compile(r"(?ix)^\s*\$?[A-Za-z_]\w*\s*=\s*\|?\s*from\b")
_SPL2_ASSIGN_ANY_RE = re.compile(r"(?ix)^\s*\$?[A-Za-z_]\w*\s*=\s*(?:$|\|.*|from\b.*)")


def looks_like_spl2_program(s: str) -> bool:
    s = s or ""
    return bool(
        _SSA_PROGRAM_CUE_RE.search(s)
        or _SSA_LEADING_PIPE_CUE_RE.search(s)
        or _SSA_INTO_CUE_RE.search(s)
    )


def is_spl2_assignment(stage0: str) -> bool:
    return _SPL2_ASSIGN_ANY_RE.match((stage0 or "").strip()) is not None


def is_spl2_assign_from(stage: str) -> bool:
    return _SPL2_ASSIGN_FROM_RE.match((stage or "").strip()) is not None


def split_statements(program: str) -> List[str]:
    """
    Split SPL2/SSA programs into semicolon-separated statements,
    but avoid splitting inside quotes or regex /.../ literals.
    """
    s = program or ""
    out: List[str] = []
    buf: List[str] = []
    in_s = in_d = False
    in_regex = False
    esc = False

    i = 0
    while i < len(s):
        ch = s[i]

        if esc:
            buf.append(ch)
            esc = False
            i += 1
            continue

        if ch == "\\":
            buf.append(ch)
            esc = True
            i += 1
            continue

        if not in_d and ch == "'" and not in_regex:
            in_s = not in_s
            buf.append(ch)
            i += 1
            continue

        if not in_s and ch == '"' and not in_regex:
            in_d = not in_d
            buf.append(ch)
            i += 1
            continue

        # very lightweight /.../ tracking outside quotes
        if not in_s and not in_d and ch == "/":
            in_regex = not in_regex
            buf.append(ch)
            i += 1
            continue

        if ch == ";" and not in_s and not in_d and not in_regex:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def strip_optional_assignment(stmt: str) -> Tuple[Optional[str], str]:
    m = _SPL2_ASSIGN_HDR_RE.match(stmt or "")
    if not m:
        return None, (stmt or "").strip()
    return m.group("var"), (m.group("body") or "").strip()


def normalize_leading_pipe(s: str) -> str:
    return re.sub(r"^\s*\|\s*", "", s or "").strip()


# ============================================================
# 3) Quote-aware pipeline splitting
# ============================================================

_RESCUE_PIPE_CMD_RE = re.compile(
    r"""(?ix)
    ^
    (?:from|where|search|eval|into|stats|tstats|eventstats|streamstats|timechart|chart|
       rename|fields|table|lookup|inputlookup|rex|spath|convert|sort|dedup|head|tail|
       join|append|appendpipe|union|map|macro|first_time_event)\b
    """
)


def _heuristic_split_on_command_pipes(s: str) -> List[str]:
    """
    Rescue splitter for malformed SSA/SPL2 snippets that end with an unmatched
    quote and swallow later real pipeline stages.

    It deliberately ignores quote state and only splits on pipes whose
    subsequent token clearly looks like a command keyword.
    """
    stages: List[str] = []
    buf: List[str] = []
    bracket_depth = 0
    escape = False

    i = 0
    n = len(s)
    while i < n:
        ch = s[i]

        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue

        if ch == "\\":
            buf.append(ch)
            escape = True
            i += 1
            continue

        if ch == "[":
            bracket_depth += 1
            buf.append(ch)
            i += 1
            continue

        if ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
            buf.append(ch)
            i += 1
            continue

        if ch == "|" and bracket_depth == 0:
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            tail = s[j:]
            if _RESCUE_PIPE_CMD_RE.match(tail or ""):
                stage = "".join(buf).strip()
                if stage:
                    stages.append(stage)
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stages.append(tail)
    return stages

def _strip_outer_wrapper_quotes(s: str) -> Tuple[str, bool]:
    t = (s or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        quote = t[0]
        esc = False
        # Only unwrap when the opening quote's first real close is the final
        # character. Otherwise this is just ordinary quoted content at both ends
        # of the SPL, not an outer wrapper around the entire expression.
        for i in range(1, len(t) - 1):
            ch = t[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == quote:
                return t, False
        return t[1:-1].strip(), True
    return t, False


_QUOTED_PREDICATE_START_RE = re.compile(
    r"""
    ^\s*
    (?P<q>["'])
    (?:\\.|(?! (?P=q) ).)+
    (?P=q)
    \s*
    (?:
        =|!=|==|>=|<=|<|>
        |\bIN\b
        |\bLIKE\b
        |\bCONTAINS\b
        |\bSTARTSWITH\b
        |\bENDSWITH\b
        |\bMATCHES\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _normalize_embedded_pipeline_wrapper(s: str) -> Tuple[str, bool]:
    """
    Repair malformed wrapper quotes around an entire embedded pipeline.

    Some SSC rows contain a leading quote before a pipe-led pipeline but no
    matching trailing quote, for example:

        "| tstats ... | convert ... | MACRO=foo

    A smaller malformed variant also appears in SSC where a root search is
    prefixed with a stray single quote before an opening parenthesis, e.g.:

        '(source=foo OR source=bar) EventCode=7 | stats count

    If left untouched, quote-aware splitting hides all pipes and the whole line
    collapses into one `_raw CONTAINS` predicate. Only unwrap when the content
    clearly looks like embedded SPL.
    """
    t = (s or "").strip()
    if not t:
        return t, False

    if len(t) >= 2 and t[0] in ("'", '"') and t[0] != t[-1]:
        inner = t[1:].lstrip()
        # Some malformed SSC rows start with a stray single quote before a
        # parenthesized root search. Handle that exact shape before any regex
        # or quote-aware splitting, because both can get dragged into the slow
        # path by the unmatched leading quote.
        if t[0] == "'" and inner.startswith("("):
            return inner, True
        # Do not treat a normal quoted-field predicate start as a wrapper, e.g.
        #   "c-uri"="..."
        #   "quoted.field" IN ("a", "b")
        if _QUOTED_PREDICATE_START_RE.match(t):
            return t, False
        stages, meta = split_pipeline(inner)
        if meta.get("real_pipe_count", 0) >= 1 and len(stages) >= 2:
            return inner, True

    # Balanced outer quotes can also wrap an entire SPL pipeline, even when the
    # inner content legitimately contains the same quote character in function
    # arguments or string literals. Detect that by checking the unwrapped text
    # for real pipeline segmentation instead of requiring quote-free content.
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        if not _QUOTED_PREDICATE_START_RE.match(t):
            inner = t[1:-1].strip()
            stages, meta = split_pipeline(inner)
            if meta.get("real_pipe_count", 0) >= 1 and len(stages) >= 2:
                return inner, True

    inner, stripped = _strip_outer_wrapper_quotes(t)
    if stripped:
        stages, meta = split_pipeline(inner)
        if meta.get("real_pipe_count", 0) >= 1 and len(stages) >= 2:
            return inner, True

    return t, False


_SEARCH_STAR_SUFFIX_RE = re.compile(r"(?is)\s+\bsearch\s+\*\s*$")


def strip_redundant_search_star(stage: str) -> str:
    """Drop trailing 'search *' which is a no-op after macro expansion."""
    s = (stage or "").strip()
    return _SEARCH_STAR_SUFFIX_RE.sub("", s).strip()


def extract_trailing_bracket_block(stage: str) -> Tuple[str, str]:
    """
    If stage ends with a trailing [...] block, return (base, payload_inside).
    Quote-aware and bracket-depth aware.
    Otherwise return (stage, "").
    """
    s = (stage or "").rstrip()
    if not s.endswith("]"):
        return stage, ""

    in_s = in_d = False
    esc = False
    depth = 0

    i = len(s) - 1
    while i >= 0:
        ch = s[i]
        if esc:
            esc = False
            i -= 1
            continue
        if ch == "\\":
            esc = True
            i -= 1
            continue

        if ch == "'" and not in_d:
            in_s = not in_s
            i -= 1
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            i -= 1
            continue

        if not in_s and not in_d:
            if ch == "]":
                depth += 1
            elif ch == "[":
                depth -= 1
                if depth == 0:
                    base = s[:i].rstrip()
                    payload = s[i + 1 : -1].strip()
                    return base, payload

        i -= 1

    return stage, ""


def split_pipeline(spl: str) -> Tuple[List[str], Dict[str, Any]]:
    s0 = (spl or "").strip()
    if not s0:
        return [], {"mode": "empty", "pipe_count": 0, "stage_count": 0, "stripped_wrapper": False}

    s = s0
    pipe_count = s.count("|")

    stages: List[str] = []
    buf: List[str] = []

    in_squote = False
    in_dquote = False
    escape = False
    bracket_depth = 0
    in_regex = False
    in_regex_char_class = False
    in_block_comment = False
    real_pipe_count = 0    # pipes seen while outside ALL delimiters (quotes/brackets/regex/comments)
    empty_stage_count = 0  # real pipes whose preceding buffer was empty (e.g. leading | or || in middle)

    def _prev_nonspace(buf_: List[str]) -> str:
        for k in range(len(buf_) - 1, -1, -1):
            if not buf_[k].isspace():
                return buf_[k]
        return ""

    i = 0
    n = len(s)
    while i < n:
        ch = s[i]

        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue

        if ch == "\\":
            buf.append(ch)
            escape = True
            i += 1
            continue

        if not in_squote and not in_dquote and not in_regex:
            # Block-comment detection (/* ... */) is intentionally disabled.
            # Traditional SPL does not use C-style block comments, and the
            # /* sequence appears frequently in file-path globs and URL
            # patterns (e.g.  uri=/*/data/ui/views/*).  Treating these as
            # block comments swallows all subsequent pipe characters.
            pass

        if in_block_comment:
            buf.append(ch)
            i += 1
            continue

        if ch == "'" and not in_dquote and not in_regex:
            in_squote = not in_squote
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not in_squote and not in_regex:
            in_dquote = not in_dquote
            buf.append(ch)
            i += 1
            continue

        if not in_squote and not in_dquote and not in_regex:
            if ch == "[":
                bracket_depth += 1
                buf.append(ch)
                i += 1
                continue
            if ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
                buf.append(ch)
                i += 1
                continue

        if not in_squote and not in_dquote and bracket_depth == 0 and ch == "/":
            if in_regex:
                if in_regex_char_class:
                    buf.append(ch)
                    i += 1
                    continue
                in_regex = False
                buf.append(ch)
                i += 1
                continue

            prev = _prev_nonspace(buf)

            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            nxt = s[j] if j < n else ""

            regexish_next = nxt in ("(", "[", "\\", "^", ".", "?", "*")
            if nxt == "(" and j + 2 < n and s[j:j+3].lower() == "(?i":
                regexish_next = True

            if prev in ("", "(", ",", "=", " ", "\t", ":") and regexish_next:
                in_regex = True
                buf.append(ch)
                i += 1
                continue

        if in_regex:
            if ch == "[" and not in_regex_char_class:
                in_regex_char_class = True
                buf.append(ch)
                i += 1
                continue
            if ch == "]" and in_regex_char_class:
                in_regex_char_class = False
                buf.append(ch)
                i += 1
                continue

        if ch == "|" and not in_squote and not in_dquote and bracket_depth == 0 and not in_regex:
            real_pipe_count += 1
            stage = "".join(buf).strip()
            if stage:
                stages.append(stage)
            else:
                empty_stage_count += 1
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stages.append(tail)

    mode = "quote_bracket_regex_comment_aware"

    # Fallback: only applies when there were genuinely unquoted/unbracketed pipes
    # that the splitter should have produced enough stages for but somehow didn't.
    #
    # Guard 1 — real_pipe_count (not raw pipe_count): prevents clobbering correct
    #   single-stage results where all pipes were inside quoted strings
    #   (e.g.  field="value|with|pipes"  → 0 real pipes → no fallback needed).
    #
    # Guard 2 — pipe-led adjustment: for SPL that begins with '|', the leading
    #   pipe increments real_pipe_count but produces no stage (empty pre-pipe text
    #   is discarded).  So N real pipes yield N stages, not N+1.  Without this
    #   adjustment the fallback would fire on every pipe-led rule even when the
    #   quote-aware result is perfectly correct.
    #
    # Guard 3 — empty_stage_count: consecutive pipes (|| or | |) and trailing empty
    #   stages are silently discarded by the state machine.  Each such discard
    #   reduces the number of output stages by 1.  Subtracting empty_stage_count
    #   from min_expected prevents the fallback from firing for SPL that genuinely
    #   contains empty pipeline segments.
    is_pipe_led = s.startswith("|")
    min_expected = real_pipe_count + (0 if is_pipe_led else 1) - empty_stage_count

    if real_pipe_count > 0 and len(stages) < min_expected:
        if (not in_squote) and (not in_dquote) and (bracket_depth == 0) and (not in_regex) and (not in_block_comment):
            naive = [seg.strip() for seg in s.split("|") if seg.strip()]
            if len(naive) > 1:
                stages = naive
                mode = "fallback_naive"

    if (
        mode == "quote_bracket_regex_comment_aware"
        and in_dquote
        and pipe_count > real_pipe_count
    ):
        rescued = _heuristic_split_on_command_pipes(s)
        if len(rescued) > len(stages):
            stages = rescued
            mode = "command_pipe_rescue"
            real_pipe_count = max(real_pipe_count, len(stages) - (0 if s.startswith("|") else 1))

    meta = {
        "mode": mode,
        "pipe_count": pipe_count,
        "real_pipe_count": real_pipe_count,
        "empty_stage_count": empty_stage_count,
        "stage_count": len(stages),
        "ended_in_squote": in_squote,
        "ended_in_dquote": in_dquote,
        "ended_in_bracket": bracket_depth != 0,
        "ended_in_regex": in_regex,
        "ended_in_block_comment": in_block_comment,
    }
    return stages, meta


def drop_garbage_prelude_stages(stages: List[str]) -> List[str]:
    if not stages:
        return stages

    def is_garbage(s: str) -> bool:
        t = (s or "").strip()
        if not t:
            return True
        if " " not in t and len(t) <= 3 and re.fullmatch(r"[A-Za-z]+", t):
            return True
        if re.fullmatch(r"[|,;]+", t):
            return True
        return False

    def strong_cmd(s: str) -> bool:
        c = detect_cmd(s)
        return c in {"tstats", "search", "where", "sourcetype", "index", "from", "into"}

    out = list(stages)
    while len(out) >= 2 and is_garbage(out[0]) and strong_cmd(out[1]):
        out.pop(0)
    return out


def split_spl_regions(spl: str) -> Tuple[str, List[str], Dict[str, Any]]:
    """Split SPL into (base_search_text, pipe_stages, seg_meta)."""
    s = (spl or "").strip()
    if not s:
        return "", [], {
            "mode": "empty", "pipe_count": 0, "stage_count": 0,
            "stripped_wrapper": False, "pipe_led": False,
        }

    pipe_led = s.startswith("|")
    all_stages, seg_meta = split_pipeline(s)
    seg_meta["pipe_led"] = pipe_led

    if pipe_led:
        return "", list(all_stages), seg_meta

    if not all_stages:
        return "", [], seg_meta

    return all_stages[0], list(all_stages[1:]), seg_meta


# ============================================================
# 4) Command detection
# ============================================================

def detect_cmd(stage: str) -> str:
    s = (stage or "").lstrip("|").strip()

    if is_spl2_assign_from(s):
        return "from"

    if re.match(r"^MACRO\s*=", s, flags=re.IGNORECASE):
        return "macro"

    if s.startswith("`"):
        return "search"

    if re.match(r"(?ix)^\s*into\b", s):
        return "into"

    if re.match(r"(?i)^\s*sourcetype\s*:", s):
        return "unknown"

    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\b", s)
    if not m:
        return "unknown"

    tok = m.group(1)
    j = m.end(1)
    while j < len(s) and s[j].isspace():
        j += 1

    if j < len(s) and s[j] == "=":
        return "unknown"

    return tok.lower()


# ============================================================
# 5) Root-search heuristics
# ============================================================

ROOT_ALLOWED_CMDS = {"search", "where"}

EXPLICIT_STAGE0_CMDS = {
    "search", "where", "tstats", "from", "into", "eval", "rex", "regex",
    "stats", "eventstats", "streamstats", "timechart", "chart",
    "table", "fields", "rename", "sort", "dedup",
    "head", "tail", "top", "rare", "bin", "bucket",
    "lookup", "inputlookup", "outputlookup",
    "join", "append", "appendcols", "appendpipe", "transaction",
    "apply", "union", "convert", "map", "multisearch",
    "foreach", "format", "replace", "fillnull", "mvexpand",
    "makemv", "nomv", "spath", "fieldformat", "summary",
    "macro",
}

NEVER_HAS_PREDICATE_CMDS = {
    "foreach", "format", "fieldformat", "replace",
    "makemv", "nomv", "mvexpand", "spath",
    "outputlookup", "multisearch",
    "transaction",
}

IMPLICIT_SEARCH_CUE_RE = re.compile(
    r"""(?ix)
    ^\s*
    (?:`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_.:-]*)
    \s*(?:=|!=|>=|<=|<|>)\s*
    (?:`[^`]+`|"[^"]+"|[A-Za-z0-9_.:/\\\-\*\?]+)
    (?:\s+(?:`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_.:-]*)\s*(?:=|!=|>=|<=|<|>)\s*(?:`[^`]+`|"[^"]+"|[A-Za-z0-9_.:/\\\-\*\?]+))*
    \s*$
    """
)

BASESEARCH_BOOL_RE = re.compile(r"(?i)\b(AND|OR|NOT)\b")


def looks_like_predicate_expr(stage0: str) -> bool:
    s = (stage0 or "").strip()
    if not s:
        return False
    if is_spl2_assignment(s):
        return False
    cmd = detect_cmd(s)
    if cmd in EXPLICIT_STAGE0_CMDS:
        return False
    return BOOL_CUE_RE.search(s) is not None


def looks_like_implicit_root_search(stage0: str) -> bool:
    s = (stage0 or "").strip()
    if not s:
        return False
    if is_spl2_assignment(s):
        return False
    cmd = detect_cmd(s)
    if cmd in EXPLICIT_STAGE0_CMDS:
        return False
    if not any(op in s for op in ("!=", ">=", "<=", "=", ">", "<")):
        return False
    return IMPLICIT_SEARCH_CUE_RE.match(s) is not None


def looks_like_implicit_boolean_root(stage0: str) -> bool:
    s = (stage0 or "").strip()
    if not s:
        return False
    if is_spl2_assignment(s):
        return False
    cmd = detect_cmd(s)
    if cmd in EXPLICIT_STAGE0_CMDS:
        return False
    if not any(op in s for op in ("!=", ">=", "<=", "=", ">", "<")):
        return False
    if not (BASESEARCH_BOOL_RE.search(s) or "(" in s or ")" in s):
        return False
    return True


def looks_like_bare_boolean(stage0: str) -> bool:
    s = (stage0 or "").strip()
    if not s:
        return False
    if is_spl2_assignment(s):
        return False
    cmd = detect_cmd(s)
    if cmd in ROOT_ALLOWED_CMDS:
        return True
    if cmd != "unknown":
        return False
    return BOOL_CUE_RE.search(s) is not None


_LITERAL_OP_RE = re.compile(r"(?:!=|>=|<=|=|>|<)")
_LITERAL_BOOL_KW_RE = re.compile(
    r"\b(?:AND|OR|NOT|IN|LIKE|MATCHES|CONTAINS|STARTSWITH|ENDSWITH)\b",
    re.IGNORECASE,
)


def _looks_like_literal_search_term(s: str) -> bool:
    if not s or not s.strip():
        return False
    if _LITERAL_OP_RE.search(s):
        return False
    if _LITERAL_BOOL_KW_RE.search(s):
        return False
    cmd = detect_cmd(s)
    if cmd in EXPLICIT_STAGE0_CMDS:
        return False
    if not re.search(r"""[\w"'*?]""", s):
        return False
    return True


def _extract_literal_value(s: str) -> Tuple[str, str]:
    t = s.strip()
    if len(t) >= 2 and t[0] in ('"', "'") and t[-1] == t[0]:
        t = t[1:-1]
    subtype = "wildcard" if ("*" in t or "?" in t) else "string"
    return t, subtype


# ============================================================
# 6) Layer classification helpers
# ============================================================

AGG_CMDS = {
    "stats", "tstats", "timechart", "chart", "top", "rare",
    "eventstats", "streamstats", "transaction", "xyseries", "geostats",
    "sitestats", "anomalydetection", "bucket", "bin",
}

FILTER_CMDS = {"where", "search", "regex"}

XFORM_HINTS = {
    "eval", "rex", "lookup", "inputlookup", "outputlookup", "spath",
    "mvexpand", "makemv", "nomv", "convert", "replace",
    "rename", "fields", "table", "dedup", "sort", "head", "tail",
    "join", "append", "appendcols", "appendpipe",
    "foreach", "map", "multisearch", "format", "apply",
    "iplocation", "fillnull",
    "macro", "append", "appendpipe", "map",
    "from",
    "into",
    "union"
}


def _stage_type(c: dict) -> str:
    return (c.get("type") or "unknown").lower()


def _first_index(cmds, pred):
    for i, c in enumerate(cmds):
        if pred(c):
            return i
    return None


def _is_agg(c): return _stage_type(c) in AGG_CMDS
def _is_filter(c): return _stage_type(c) in FILTER_CMDS


def _is_transform_boundary(c):
    t = _stage_type(c)
    if t in ("base_search",):
        return False
    if t in AGG_CMDS:
        return False
    if t in FILTER_CMDS:
        return False
    return (t in XFORM_HINTS) or (t == "unknown")


def _is_noop_wildcard_pred(pred: dict) -> bool:
    """Return True for the no-op  _raw CONTAINS "*"  predicate.

    ``search *``  is syntactically valid but semantically matches every
    event. Representing it with  ``contributes_to_filter=True``  misleads
    downstream consumers into believing a real filter is present.
    """
    if not isinstance(pred, dict) or pred.get("type") != "predicate":
        return False
    field = pred.get("field", {})
    if not isinstance(field, dict) or field.get("value") != "_raw":
        return False
    op = pred.get("operator", {})
    op_val = op.get("value") if isinstance(op, dict) else op
    if op_val != "CONTAINS":
        return False
    val = pred.get("value", {})
    if not isinstance(val, dict):
        return False
    return val.get("subtype") == "wildcard" and val.get("value") == "*"


def _explicit_search_noop_reason(expr: str) -> Optional[str]:
    text = (expr or "").strip()
    if text == "*":
        return "explicit_search_match_all"
    if text.lower() == "x":
        return "explicit_search_placeholder"
    return None


def _literal_search_noop_reason(literal_val: str) -> Optional[str]:
    text = (literal_val or "").strip()
    if text == "*":
        return "literal_search_match_all"
    if text.lower() == "x":
        return "literal_search_placeholder"
    return None


# ============================================================
# 7) Unified IR builder
# ============================================================

class UnifiedIRBuilder:
    SEARCH_RE = re.compile(r"^\s*search\s+(.+)$", re.DOTALL | re.IGNORECASE)
    WHERE_RE  = re.compile(r"^\s*where\s+(.+)$",  re.DOTALL | re.IGNORECASE)
    TSTATS_WHERE_RE = re.compile(r"(?is)\btstats\b.*?\bwhere\b(?P<expr>.*?)(?=\bby\b|$)")
    TSTATS_FROM_RE  = re.compile(r"(?i)\bfrom\s+datamodel\s*=\s*([^\s]+)")
    TSTATS_BY_RE    = re.compile(r"(?i)\bby\b(?P<fields>.+)$", re.DOTALL)
    SUBSEARCH_IN_RE = re.compile(
        rf'(?is)^\s*(?P<field>{_SUBSEARCH_FIELD_RE})\s+IN\s*$'
    )
    SUBSEARCH_NOT_IN_RE = re.compile(
        rf'(?is)^\s*NOT\s+(?P<field>{_SUBSEARCH_FIELD_RE})\s+IN\s*$'
    )
    SUBSEARCH_EQ_RE = re.compile(
        rf'(?is)^\s*(?P<field>{_SUBSEARCH_FIELD_RE})\s*=\s*$'
    )
    SUBSEARCH_NE_RE = re.compile(
        rf'(?is)^\s*(?P<field>{_SUBSEARCH_FIELD_RE})\s*!=\s*$'
    )
    UNCLOSED_SUBSEARCH_EQ_RE = re.compile(
        rf'(?is)^(?P<prefix>.*?)(?P<field>{_SUBSEARCH_FIELD_RE})\s*=\s*\[(?P<sub>.+)$'
    )
    UNCLOSED_SUBSEARCH_NE_RE = re.compile(
        rf'(?is)^(?P<prefix>.*?)(?P<field>{_SUBSEARCH_FIELD_RE})\s*!=\s*\[(?P<sub>.+)$'
    )
    INPUTLOOKUP_SUBSEARCH_RE = re.compile(
        r"(?is)^\s*\|?\s*inputlookup\b\s+(?:append\s*=\s*\S+\s+)?(?P<src>[A-Za-z0-9_.:-]+)\b"
    )

    @staticmethod
    def _predicate_meta(source: str, confidence: str = "full") -> Dict[str, Any]:
        return {"source": source, "confidence": confidence}

    EVAL_RE = re.compile(r"^\s*eval\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.DOTALL | re.IGNORECASE)
    REX_FIELD = re.compile(r'field\s*=\s*([A-Za-z_][A-Za-z0-9_.-]*)', re.IGNORECASE)
    REX_CAP = re.compile(r'\(\?<([A-Za-z_][A-Za-z0-9_.]*)>', re.IGNORECASE)
    REX_SED_MODE = re.compile(r"(?i)\bmode\s*=\s*sed\b")

    APPEND_RE = re.compile(r"(?is)^\s*append\s*\[(?P<sub>.*)\]\s*$")
    APPENDPIPE_RE = re.compile(r"(?is)^\s*appendpipe\s*\[(?P<sub>.*)\]\s*$")

    MAP_SEARCH_RE = re.compile(r'''(?is)^\s*map\b(?P<body>.*)$''')
    MAP_SEARCH_KV_RE = re.compile(r'''(?is)\bsearch\s*=\s*("(?P<dq>(?:\\.|[^"\\])*)"|'(?P<sq>(?:\\.|[^'\\])*)')''')

    UNION_RE = re.compile(r"(?is)^\s*union\b(?P<args>.*)$")

    _JOIN_OPT_KV_RE = re.compile(
        r"(?ix)\b(?:type|usetime|earlier|overwrite|max|keepsingle|delim)\s*=\s*\S+"
    )
    _JOIN_FIELD_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.{}\-:]*\b")
    FROM_ANY_RE = re.compile(r"(?is)^\s*from\s+(?P<src>.+?)\s*$")
    ASSIGN_FROM_ANY_RE = re.compile(r"(?is)^\s*(?P<var>\$[A-Za-z_]\w*)\s*=\s*\|?\s*from\s+(?P<src>.+?)\s*$")
    _BOOL_OP_RE = re.compile(r"\b(?:AND|OR|NOT)\b", re.IGNORECASE)
    _FALLBACK_FIRST_EXPR_LEN = 4000
    _FALLBACK_FIRST_BOOL_OPS = 256

    def __init__(self, boolean_parser: Lark, quiet: bool = False):
        self.boolean_parser = boolean_parser
        self.quiet = quiet

    def parse_boolean_expr(self, expr: str) -> Optional[Dict[str, Any]]:
        expr = (expr or "").strip()
        if not expr:
            return None
        pred_subsearch = self._parse_subsearch_membership_expr(expr)
        if pred_subsearch is not None:
            return pred_subsearch
        pred_unclosed_subsearch = self._parse_unclosed_subsearch_membership_expr(expr)
        if pred_unclosed_subsearch is not None:
            return pred_unclosed_subsearch
        expr = self._rewrite_inputlookup_subsearch_clauses(expr)
        pred_inputlookup_subsearch = self._parse_inputlookup_subsearch_expr(expr)
        if pred_inputlookup_subsearch is not None:
            return pred_inputlookup_subsearch
        # Strip bracket-delimited subsearches [| inputlookup ... | fields ...]
        # so their internal SPL tokens don't become _raw CONTAINS predicates.
        expr = strip_subsearches(expr)
        # Truncate at leaked SPL pipeline commands (missing-pipe malformed SPL).
        expr = strip_trailing_spl_commands(expr)
        # Giant OR/AND chains are cheap for the fallback parser but can create
        # oversized parse structures in the grammar parser.
        if (
            len(expr) >= self._FALLBACK_FIRST_EXPR_LEN
            or len(self._BOOL_OP_RE.findall(expr)) >= self._FALLBACK_FIRST_BOOL_OPS
        ):
            pred = parse_boolean_expr_fallback(expr)
            if pred is not None:
                return pred
        try:
            tree = self.boolean_parser.parse(expr)
            raw = raw_ir_transformer.transform(tree)
            return ir_to_builtin(raw)
        except Exception:
            pass
        return parse_boolean_expr_fallback(expr)

    def _normalize_predicate_field(self, raw_field: str) -> Dict[str, Any]:
        field = (raw_field or "").strip()
        if len(field) >= 2 and field[0] == field[-1] and field[0] in {'"', '`'}:
            field = field[1:-1]
        return {"type": "field", "value": field}

    def _parse_subsearch_membership_expr(self, expr: str) -> Optional[Dict[str, Any]]:
        base, sub = extract_trailing_bracket_block(expr)
        if not sub:
            return None

        sub_ir = self._parse_subsearch_ir(sub)
        if not sub_ir:
            return None

        base = (base or "").strip()
        m_not = self.SUBSEARCH_NOT_IN_RE.match(base)
        if m_not:
            inner = {
                "type": "predicate",
                "field": self._normalize_predicate_field(m_not.group("field")),
                "operator": {"type": "operator", "value": "IN_SUBSEARCH"},
                "subsearch": sub_ir,
            }
            return {"type": "expr", "op": "NOT", "children": [inner]}

        m = self.SUBSEARCH_IN_RE.match(base)
        if m:
            return {
                "type": "predicate",
                "field": self._normalize_predicate_field(m.group("field")),
                "operator": {"type": "operator", "value": "IN_SUBSEARCH"},
                "subsearch": sub_ir,
            }

        m_eq = self.SUBSEARCH_EQ_RE.match(base)
        if m_eq:
            return {
                "type": "predicate",
                "field": self._normalize_predicate_field(m_eq.group("field")),
                "operator": {"type": "operator", "value": "EQ_SUBSEARCH"},
                "subsearch": sub_ir,
            }

        m_ne = self.SUBSEARCH_NE_RE.match(base)
        if m_ne:
            inner = {
                "type": "predicate",
                "field": self._normalize_predicate_field(m_ne.group("field")),
                "operator": {"type": "operator", "value": "EQ_SUBSEARCH"},
                "subsearch": sub_ir,
            }
            return {"type": "expr", "op": "NOT", "children": [inner]}

        return None

    def _merge_prefix_with_subsearch_predicate(
        self,
        prefix: str,
        sub_pred: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        prefix = (prefix or "").strip()
        if not prefix:
            return sub_pred

        prefix_pred = self.parse_boolean_expr(prefix)
        if prefix_pred is None:
            return None

        return {"type": "expr", "op": "AND", "children": [prefix_pred, sub_pred]}

    def _parse_unclosed_subsearch_membership_expr(self, expr: str) -> Optional[Dict[str, Any]]:
        text = (expr or "").strip()
        if not text or text.count("[") <= text.count("]"):
            return None

        m_ne = self.UNCLOSED_SUBSEARCH_NE_RE.match(text)
        if m_ne:
            sub_ir = self._parse_subsearch_ir(m_ne.group("sub"))
            if not sub_ir:
                return None
            inner = {
                "type": "predicate",
                "field": self._normalize_predicate_field(m_ne.group("field")),
                "operator": {"type": "operator", "value": "EQ_SUBSEARCH"},
                "subsearch": sub_ir,
            }
            wrapped = {"type": "expr", "op": "NOT", "children": [inner]}
            return self._merge_prefix_with_subsearch_predicate(m_ne.group("prefix"), wrapped)

        m_eq = self.UNCLOSED_SUBSEARCH_EQ_RE.match(text)
        if not m_eq:
            return None

        sub_ir = self._parse_subsearch_ir(m_eq.group("sub"))
        if not sub_ir:
            return None

        pred = {
            "type": "predicate",
            "field": self._normalize_predicate_field(m_eq.group("field")),
            "operator": {"type": "operator", "value": "EQ_SUBSEARCH"},
            "subsearch": sub_ir,
        }
        return self._merge_prefix_with_subsearch_predicate(m_eq.group("prefix"), pred)

    def _inputlookup_src_predicate(self, src: str) -> Dict[str, Any]:
        return {
            "type": "predicate",
            "field": self._normalize_predicate_field("inputlookup"),
            "operator": {"type": "operator", "value": "EQ"},
            "value": {"type": "value", "subtype": "raw_identifier", "value": src},
        }

    def _rewrite_inputlookup_subsearch_clauses(self, expr: str) -> str:
        s = expr or ""
        if not s or "[" not in s:
            return s

        out: list[str] = []
        buf: list[str] = []
        depth = 0
        in_s = in_d = esc = False
        in_regex = False
        in_regex_char_class = False

        def _prev_nonspace(chars: list[str]) -> str:
            for ch in reversed(chars):
                if not ch.isspace():
                    return ch
            return ""

        n = len(s)
        for i, ch in enumerate(s):
            if esc:
                if depth == 0:
                    out.append(ch)
                else:
                    buf.append(ch)
                esc = False
                continue

            if ch == "\\":
                if depth == 0:
                    out.append(ch)
                else:
                    buf.append(ch)
                esc = True
                continue

            if ch == "'" and not in_d:
                in_s = not in_s
                if depth == 0:
                    out.append(ch)
                else:
                    buf.append(ch)
                continue

            if ch == '"' and not in_s:
                in_d = not in_d
                if depth == 0:
                    out.append(ch)
                else:
                    buf.append(ch)
                continue

            if not in_s and not in_d:
                if ch == "/":
                    if in_regex:
                        if in_regex_char_class:
                            if depth == 0:
                                out.append(ch)
                            else:
                                buf.append(ch)
                            continue
                        if depth == 0:
                            out.append(ch)
                        else:
                            buf.append(ch)
                        in_regex = False
                        continue

                    prev = _prev_nonspace(out if depth == 0 else buf)
                    j = i + 1
                    while j < n and s[j].isspace():
                        j += 1
                    nxt = s[j] if j < n else ""
                    regexish_next = nxt in ("(", "[", "\\", "^", ".", "?", "*")
                    if nxt == "(" and j + 2 < n and s[j:j + 3].lower() == "(?i":
                        regexish_next = True

                    if prev in ("", "(", ",", "=", " ", "\t", ":") and regexish_next:
                        if depth == 0:
                            out.append(ch)
                        else:
                            buf.append(ch)
                        in_regex = True
                        continue

            if in_regex:
                if ch == "[" and not in_regex_char_class:
                    in_regex_char_class = True
                elif ch == "]" and in_regex_char_class:
                    in_regex_char_class = False
                if depth == 0:
                    out.append(ch)
                else:
                    buf.append(ch)
                continue

            if not in_s and not in_d and ch == "[":
                if depth == 0:
                    buf = []
                else:
                    buf.append(ch)
                depth += 1
                continue

            if not in_s and not in_d and ch == "]" and depth > 0:
                depth -= 1
                if depth == 0:
                    body = "".join(buf)
                    m = self.INPUTLOOKUP_SUBSEARCH_RE.match(body)
                    if m:
                        src = (m.group("src") or "").strip()
                        if src:
                            out.append(f" inputlookup={src} ")
                        else:
                            out.append("[")
                            out.append(body)
                            out.append("]")
                    else:
                        out.append("[")
                        out.append(body)
                        out.append("]")
                    buf = []
                else:
                    buf.append(ch)
                continue

            if depth == 0:
                out.append(ch)
            else:
                buf.append(ch)

        if depth > 0:
            out.append("[")
            out.append("".join(buf))

        return "".join(out)

    def _parse_inputlookup_subsearch_expr(self, expr: str) -> Optional[Dict[str, Any]]:
        base, sub = extract_trailing_bracket_block(expr)
        if not sub:
            return None

        m = self.INPUTLOOKUP_SUBSEARCH_RE.match(sub)
        if not m:
            return None

        src = (m.group("src") or "").strip()
        if not src:
            return None

        pred = self._inputlookup_src_predicate(src)
        base = (base or "").strip()
        if not base:
            return pred

        if re.fullmatch(r"(?is)NOT", base):
            return {"type": "expr", "op": "NOT", "children": [pred]}

        return self._merge_prefix_with_subsearch_predicate(base, pred)

    _FUNC_CALL_FIELD_RE = re.compile(r"^[A-Za-z_]\w*\s*\((.+)\)$", re.DOTALL)

    def find_fields(self, ir: Any) -> Set[str]:
        fields: Set[str] = set()

        def _unwrap_func_call_field(f: str) -> str:
            m = self._FUNC_CALL_FIELD_RE.match(f.strip())
            if not m:
                return f
            first_tok = re.match(r"[A-Za-z_][A-Za-z0-9_.:-]*", m.group(1).strip())
            if first_tok:
                return first_tok.group(0)
            return f

        def rec(x):
            if isinstance(x, dict):
                if x.get("type") == "predicate":
                    f = x.get("field")
                    if isinstance(f, dict) and f.get("value"):
                        fields.add(_unwrap_func_call_field(f["value"]))
                    elif isinstance(f, str) and f:
                        fields.add(_unwrap_func_call_field(f))
                for v in x.values():
                    rec(v)
            elif isinstance(x, list):
                for y in x:
                    rec(y)

        rec(ir)
        return fields

    # ---------------- command parsers ----------------

    def parse_search(self, stage: str) -> Dict[str, Any]:
        m = self.SEARCH_RE.match(stage)
        expr = m.group(1).strip() if m else ""
        noop_reason = _explicit_search_noop_reason(expr)
        pred = self.parse_boolean_expr(expr) if expr and not noop_reason else None
        result: Dict[str, Any] = {
            "raw": stage,
            "type": "search",
            "clauses": {"where": {"raw": expr, "predicate_ir": pred}} if expr else {},
            "inputs": list(self.find_fields(pred)) if pred else [],
            "outputs": [],
            "layer": "L1",
            "predicate_ir": pred,
            "info": {},
        }
        if noop_reason:
            result["info"] = {
                "source": "explicit_search_noop",
                "noop_reason": noop_reason,
                "note": f'Explicit "{expr}" search treated as match-all/no-op search stage.',
            }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("explicit_filter")
        return result

    def parse_where(self, stage: str) -> Dict[str, Any]:
        m = self.WHERE_RE.match(stage)
        expr = m.group(1).strip() if m else ""
        pred = self.parse_boolean_expr(expr) if expr else None
        result: Dict[str, Any] = {
            "raw": stage,
            "type": "where",
            "clauses": {"where": {"raw": expr, "predicate_ir": pred}} if expr else {},
            "inputs": list(self.find_fields(pred)) if pred else [],
            "outputs": [],
            "layer": "L1",
            "predicate_ir": pred,
            "info": {},
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("explicit_filter")
        return result

    def parse_eval(self, stage: str) -> Dict[str, Any]:
        ins, outs, io_meta = infer_io_ext(stage, "eval")
        return {
            "raw": stage,
            "type": "eval",
            "inputs": ins or [],
            "outputs": outs or [],
            "layer": "L2",
            "predicate_ir": None,
            "info": {},
            "io_meta": io_meta,
        }

    @staticmethod
    def _unquote_simple(x: str) -> str:
        t = (x or "").strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
            return t[1:-1]
        return t

    @classmethod
    def _extract_rex_pattern(cls, stage: str) -> Optional[str]:
        raw = (stage or "").strip()
        m = re.match(r"(?is)^\s*rex\b(?P<body>.*)$", raw)
        body = (m.group("body") if m else "").strip()
        if not body:
            return None

        pat_re = r"""(?:/(?:\\.|[^/\\])+/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""

        body_wo_field = re.sub(
            r"""(?is)\bfield\s*=\s*(?:[A-Za-z_][A-Za-z0-9_.-]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""",
            " ",
            body,
            count=1,
        )
        body_wo_opts = re.sub(
            r"""(?is)\b(?:max_match|offset_field|mode)\s*=\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)""",
            " ",
            body_wo_field,
        ).strip()

        m_pat = re.search(rf"(?is)(?P<p>{pat_re})", body_wo_opts)
        if not m_pat:
            return None

        pat = m_pat.group("p")
        if len(pat) >= 2 and pat[0] == "/" and pat[-1] == "/":
            return pat[1:-1]
        return cls._unquote_simple(pat)

    def parse_rex(self, stage: str) -> Dict[str, Any]:
        inputs, outputs = [], []
        m = self.REX_FIELD.search(stage)
        if m:
            inputs.append(m.group(1))
        outputs = self.REX_CAP.findall(stage)

        pred = None
        if not self.REX_SED_MODE.search(stage):
            source_field = inputs[0] if inputs else "_raw"
            pattern = self._extract_rex_pattern(stage)
            if pattern and len(outputs) == 1:
                subtype = "wildcard" if ("*" in pattern or "?" in pattern) else "regex_candidate"
                pred = {
                    "type": "predicate",
                    "field": {"type": "field", "value": outputs[0]},
                    "operator": {"type": "operator", "value": "MATCHES_REGEX"},
                    "value": {"type": "value", "subtype": subtype, "value": pattern},
                    "source_field": {"type": "field", "value": source_field},
                }

        result = {
            "raw": stage,
            "type": "rex",
            "inputs": inputs or ["_raw"],
            "outputs": outputs,
            "layer": "L2",
            "predicate_ir": pred,
            "info": {},
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("derived_field_predicate")
        return result

    @staticmethod
    def _needed_upstream_inputs(cmd: Dict[str, Any], needed_outputs: Set[str]) -> Set[str]:
        inputs = list(cmd.get("inputs") or [])
        outputs = list(cmd.get("outputs") or [])
        if not needed_outputs:
            return set()

        t = _stage_type(cmd)
        if t in {"table", "fields", "rename", "rex"} and outputs:
            mapped_inputs: Set[str] = set()
            for inp, out in zip(inputs, outputs):
                if out in needed_outputs:
                    mapped_inputs.add(inp)
            if mapped_inputs:
                return mapped_inputs

        if outputs and (set(outputs) & needed_outputs):
            return set(inputs)

        return set()

    def parse_tstats(self, stage: str) -> Dict[str, Any]:
        raw = stage or ""
        pred = None
        where_raw = None

        m_where = self.TSTATS_WHERE_RE.search(raw)
        if m_where:
            where_raw = (m_where.group("expr") or "").strip()
            if where_raw:
                pred = self.parse_boolean_expr(where_raw)

        m_from = self.TSTATS_FROM_RE.search(raw)
        dataset_ref = m_from.group(1) if m_from else None

        m_by = self.TSTATS_BY_RE.search(raw)
        by_raw = (m_by.group("fields") or "").strip() if m_by else ""
        by_fields = [
            f for f in re.split(r"[\s,]+", by_raw)
            if f and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:{}\-]*", f)
        ] if by_raw else []

        clauses: Dict[str, Any] = {}
        if dataset_ref:
            clauses["source"] = {"dataset_ref": dataset_ref}
        if m_where:
            clauses["where"] = {"raw": where_raw, "predicate_ir": pred}
        if by_fields:
            clauses["by"] = by_fields

        ins, outs, io_meta = infer_io_ext(raw, "tstats")

        pred_inputs = list(self.find_fields(pred)) if pred else []
        if pred_inputs:
            ins = pred_inputs

        pred_meta = self._predicate_meta(
            "embedded_where",
            confidence="full" if pred else "partial",
        ) if m_where else None

        result: Dict[str, Any] = {
            "raw": raw,
            "type": "tstats",
            "clauses": clauses,
            "inputs": ins or [],
            "outputs": outs or [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": {"has_embedded_where": bool(m_where)},
            "io_meta": io_meta,
        }
        if pred_meta:
            result["predicate_meta"] = pred_meta
        return result

    def parse_regex(self, stage: str) -> Dict[str, Any]:
        raw = stage or ""
        s = raw.strip()

        m = re.match(r"(?is)^\s*regex\b(?P<body>.*)$", s)
        body = (m.group("body") if m else "").strip()

        def unquote_simple(x: str) -> str:
            t = (x or "").strip()
            if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
                return t[1:-1]
            return t

        def strip_regex_delims(p: str) -> str:
            t = (p or "").strip()
            if len(t) >= 2 and t[0] == "/" and t[-1] == "/":
                return t[1:-1]
            return unquote_simple(t)

        FIELD_RE = r"[A-Za-z_][A-Za-z0-9_.:{}\-]*"
        PAT_RE = r"""(?:/(?:\\.|[^/\\])+/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""

        field: Optional[str] = None
        pat: Optional[str] = None
        op_value = "MATCHES_REGEX"

        m_fieldopt = re.search(rf"(?is)\bfield\s*=\s*(?P<f>{FIELD_RE}|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')", body)
        if m_fieldopt:
            # Only treat this as the "field=<fieldname>" keyword option when there is
            # additional text remaining that can serve as the regex pattern.
            # If body_wo_fieldopt is empty the captured value IS the pattern and the
            # word before "=" is the actual field name — fall through to m_feq below.
            body_wo_fieldopt = (body[:m_fieldopt.start()] + " " + body[m_fieldopt.end():]).strip()
            if not body_wo_fieldopt:
                m_fieldopt = None   # e.g. `regex field=".*foo.*"` — "field" is the field name

        if m_fieldopt:
            field = unquote_simple(m_fieldopt.group("f"))
            body_wo_fieldopt = (body[:m_fieldopt.start()] + " " + body[m_fieldopt.end():]).strip()
            m_pat = re.search(rf"(?is)(?P<p>{PAT_RE})", body_wo_fieldopt)
            if m_pat:
                pat = strip_regex_delims(m_pat.group("p"))
            else:
                pat = strip_regex_delims(body_wo_fieldopt) if body_wo_fieldopt else ""
        else:
            m_fcmp = re.match(rf"(?is)^\s*(?P<f>{FIELD_RE})\s*(?P<op>!=|=)\s*(?P<rest>.*)$", body)
            if m_fcmp:
                field = m_fcmp.group("f").strip()
                rest = (m_fcmp.group("rest") or "").strip()
                if m_fcmp.group("op") == "!=":
                    op_value = "NOT_MATCHES_REGEX"
                m_pat = re.search(rf"(?is)(?P<p>{PAT_RE})", rest)
                if m_pat:
                    pat = strip_regex_delims(m_pat.group("p"))
                else:
                    pat = strip_regex_delims(rest) if rest else ""
            else:
                field = "_raw"
                m_pat = re.search(rf"(?is)(?P<p>{PAT_RE})", body)
                if m_pat:
                    pat = strip_regex_delims(m_pat.group("p"))
                else:
                    pat = strip_regex_delims(body) if body else ""

        field = field or "_raw"
        pat = pat or ""

        if pat:
            subtype = "wildcard" if ("*" in pat or "?" in pat) else "regex_candidate"
            pred: Optional[Dict[str, Any]] = {
                "type": "predicate",
                "field": {"type": "field", "value": field},
                "operator": {"type": "operator", "value": op_value},
                "value": {"type": "value", "subtype": subtype, "value": pat},
            }
        else:
            pred = None

        inputs = [field] if field else []

        result: Dict[str, Any] = {
            "raw": raw,
            "type": "regex",
            "inputs": inputs,
            "outputs": [],
            "layer": "L1",
            "predicate_ir": pred,
            "info": {},
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("explicit_filter")
        return result

    def split_macro_stage(self, stage: str) -> Tuple[Optional[str], str]:
        _MACRO_STAGE_RE = re.compile(
            r"""(?is)^\s*MACRO\s*=\s*(?P<name>[^\s]+)(?:\s+(?P<tail>.*?))?\s*$"""
        )
        s = (stage or "").strip()
        m = _MACRO_STAGE_RE.match(s)
        if not m:
            return None, s
        return m.group("name"), (m.group("tail") or "").strip()

    def parse_macro(self, stage: str) -> Dict[str, Any]:
        macro_name, tail = self.split_macro_stage(stage)

        pred = None
        pred_source = None
        inputs: List[str] = []

        if tail:
            if looks_like_implicit_boolean_root(tail):
                pred = self.parse_boolean_expr(tail)
                pred_source = "macro_embedded_boolean"
            elif looks_like_implicit_root_search(tail):
                pred = self.parse_boolean_expr(tail)
                pred_source = "macro_embedded_kv"
            elif looks_like_bare_boolean(tail):
                pred = self.parse_boolean_expr(tail)
                pred_source = "macro_embedded_bare_boolean"
            elif tail and looks_like_predicate_expr(tail):
                pred = self.parse_boolean_expr(tail)
                pred_source = "macro_embedded_filter"

            if pred is not None:
                inputs = list(self.find_fields(pred))

        result: Dict[str, Any] = {
            "raw": stage,
            "type": "macro",
            "inputs": inputs,
            "outputs": [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": {
                "macro_name": macro_name,
                "tail": tail if tail else None,
            },
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta(pred_source or "macro_embedded_filter", "full")
        return result

    def parse_into(self, stage: str) -> Dict[str, Any]:
        return {
            "raw": stage,
            "type": "into",
            "inputs": [],
            "outputs": [],
            "layer": "L3",
            "predicate_ir": None,
            "info": {},
        }

    def parse_from(self, stage: str) -> Dict[str, Any]:
        raw = stage or ""
        s = raw.strip()

        binding = None
        src = None

        m = self.ASSIGN_FROM_ANY_RE.match(s)
        if m:
            binding = m.group("var")
            src = (m.group("src") or "").strip()
        else:
            m2 = self.FROM_ANY_RE.match(s)
            if m2:
                src = (m2.group("src") or "").strip()

        source_kind = None
        if src:
            if src.startswith("$"):
                source_kind = "binding_ref"
            elif src.endswith(")"):
                source_kind = "function_call"
            else:
                source_kind = "dataset"

        info = {}
        if binding:
            info["binding"] = binding
        if src:
            info["source"] = src
        if source_kind:
            info["source_kind"] = source_kind

        io_meta = {
            "scope": "source",
            "parse_confidence": "full",
            "outputs_dynamic": False,
            "input_pattern": None,
            "dataset_ref": src if source_kind == "dataset" else None,
            "schema_preserving": source_kind == "binding_ref",
        }

        return {
            "raw": raw,
            "type": "from",
            "inputs": [],
            "outputs": [],
            "layer": "L2",
            "predicate_ir": None,
            "info": info,
            "io_meta": io_meta,
        }

    def _parse_subsearch_ir(self, sub: str) -> Optional[Dict[str, Any]]:
        sub = (sub or "").strip()
        if not sub:
            return None
        return self.build_from_text(sub)

    def _subsearch_predicate(self, sub_ir: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not sub_ir:
            return None
        rs = sub_ir.get("root_search") or None
        if rs and isinstance(rs, dict):
            return rs.get("predicate_ir")
        return None

    def _collect_all_inputs(self, ir_obj: Optional[Dict[str, Any]]) -> List[str]:
        if not ir_obj:
            return []
        acc = set()
        rs = ir_obj.get("root_search")
        if isinstance(rs, dict):
            for x in rs.get("inputs", []) or []:
                acc.add(x)
        for c in ir_obj.get("pipeline", []) or []:
            if isinstance(c, dict):
                for x in c.get("inputs", []) or []:
                    acc.add(x)
        return sorted(acc)

    def _join_time_bucket_transform(self, sub_ir: Optional[Dict[str, Any]]) -> Optional[str]:
        if not sub_ir:
            return None
        for stage in sub_ir.get("pipeline", []) or []:
            if not isinstance(stage, dict):
                continue
            if (stage.get("type") or "").lower() != "tstats":
                continue
            raw = str(stage.get("raw", ""))
            m = re.search(r"(?i)\bspan\s*=\s*([^\s,\]]+)", raw)
            if not m:
                return None
            span = m.group(1).strip().strip('"').strip("'")
            safe = re.sub(r"[^A-Za-z0-9_]+", "_", span).strip("_")
            if not safe:
                return None
            return f"bucket_{safe}"
        return None

    def _join_tuple_side(self, side: str, keys: List[str], time_transform: Optional[str]) -> str:
        items: List[str] = []
        for key in keys:
            if key == "_time" and time_transform:
                items.append(f"{time_transform}(_time)")
            else:
                items.append(key)
        if len(items) == 1:
            return f"{side}.{items[0]}"
        return f"{side}.(" + ",".join(items) + ")"

    def _join_exists_predicate(
        self,
        keys: List[str],
        *,
        time_transform: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not keys:
            return None
        return {
            "type": "predicate",
            "field": self._normalize_predicate_field(
                self._join_tuple_side("left", keys, time_transform)
            ),
            "operator": {"type": "operator", "value": "EXISTS_MATCH"},
            "value": {
                "type": "value",
                "subtype": "raw_identifier",
                "value": self._join_tuple_side("right", keys, time_transform),
            },
        }

    def parse_append(self, stage: str) -> Dict[str, Any]:
        m = self.APPEND_RE.match(stage or "")
        sub = m.group("sub").strip() if m else ""
        sub_ir = self._parse_subsearch_ir(sub) if sub else None

        pred = self._collect_subsearch_filter_predicate(sub_ir)

        info: Dict[str, Any] = {
            "subsearch_root_type": (sub_ir.get("root_search") or {}).get("type") if sub_ir else None,
            "subsearch_stage_count": len((sub_ir.get("pipeline") or [])) if sub_ir else 0,
            "branch_summary": self._branch_pipeline_summary(sub_ir),
        }

        result: Dict[str, Any] = {
            "raw": stage,
            "type": "append",
            "inputs": self._collect_all_inputs(sub_ir),
            "outputs": [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": info,
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("derived_branch_summary", "partial")
        return result

    def parse_join(self, stage: str) -> Dict[str, Any]:
        raw = stage or ""
        body = re.sub(r"(?is)^\s*join\b", "", raw).strip()

        header, sub_raw = extract_trailing_bracket_block(body)
        header = (header or "").strip()
        sub_raw = (sub_raw or "").strip()

        sub_ir = self._parse_subsearch_ir(sub_raw) if sub_raw else None
        branch_pred = self._collect_subsearch_filter_predicate(sub_ir)
        branch_summary = self._branch_pipeline_summary(sub_ir)

        options: Dict[str, Any] = {}
        recognized_option_names = {
            "type", "max", "usetime", "earlier", "overwrite", "keepsingle", "delim"
        }

        option_spans: List[Tuple[int, int]] = []
        for m in re.finditer(
            r'(?ix)\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(".*?"|\'.*?\'|[^\s,]+)',
            header,
        ):
            k = m.group(1).lower()
            v = m.group(2).strip()
            if k in recognized_option_names:
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                if k == "max":
                    try:
                        options[k] = int(v)
                    except ValueError:
                        options[k] = v
                else:
                    options[k] = v
                option_spans.append((m.start(), m.end()))

        if option_spans:
            buf = []
            last = 0
            for a, b in option_spans:
                buf.append(header[last:a])
                buf.append(" ")
                last = b
            buf.append(header[last:])
            keys_region = "".join(buf)
        else:
            keys_region = header

        keys_region = re.sub(r"\s+", " ", keys_region).strip()
        key_candidates = re.split(r"[\s,]+", keys_region) if keys_region else []
        keys = [
            tok for tok in key_candidates
            if tok and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:{}\-]*", tok)
        ]

        join_type = str(options.get("type") or "").lower()
        affects_match_set = join_type in {"", "inner"}
        time_transform = self._join_time_bucket_transform(sub_ir) if "_time" in keys else None
        join_pred = self._join_exists_predicate(keys, time_transform=time_transform) if affects_match_set else None
        pred = self._and_ir(join_pred, branch_pred) if affects_match_set else None

        inputs = list(dict.fromkeys(keys + (self._collect_all_inputs(sub_ir) if sub_ir else [])))

        clauses: Dict[str, Any] = {"keys": keys}
        if options:
            clauses["options"] = options
        if sub_ir is not None:
            clauses["subsearch"] = {
                "root_type": (sub_ir.get("root_search") or {}).get("type") if sub_ir else None,
                "stage_count": len((sub_ir.get("pipeline") or [])) if sub_ir else 0,
                "has_predicate": pred is not None,
            }

        result: Dict[str, Any] = {
            "raw": raw,
            "type": "join",
            "clauses": clauses,
            "inputs": inputs,
            "outputs": [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": {
                "join_type": options.get("type"),
                "join_keys": keys,
                "join_affects_match_set": affects_match_set,
                "join_time_transform": time_transform,
                "join_predicate_ir": join_pred,
                "branch_predicate_ir": branch_pred,
                "subsearch_root_type": (sub_ir.get("root_search") or {}).get("type") if sub_ir else None,
                "subsearch_stage_count": len((sub_ir.get("pipeline") or [])) if sub_ir else 0,
                "branch_summary": branch_summary,
            },
            "io_meta": {
                "scope": "specific" if keys else "unknown",
                "parse_confidence": "full" if keys else "partial",
                "outputs_dynamic": True,
                "input_pattern": None,
                "dataset_ref": None,
                "schema_preserving": False,
            },
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("derived_branch_summary", "partial")
        return result

    def parse_appendpipe(self, stage: str) -> Dict[str, Any]:
        m = self.APPENDPIPE_RE.match(stage or "")
        sub = m.group("sub").strip() if m else ""
        sub_ir = self._parse_subsearch_ir(sub) if sub else None

        pred = self._collect_subsearch_filter_predicate(sub_ir)

        result: Dict[str, Any] = {
            "raw": stage,
            "type": "appendpipe",
            "inputs": self._collect_all_inputs(sub_ir),
            "outputs": [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": {
                "subpipeline_stage_count": len((sub_ir.get("pipeline") or [])) if sub_ir else 0,
                "branch_summary": self._branch_pipeline_summary(sub_ir),
            },
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("derived_branch_summary", "partial")
        return result

    def _split_top_level_commas(self, s: str) -> List[str]:
        s = (s or "").strip()
        if not s:
            return []

        out: List[str] = []
        buf: List[str] = []
        in_squote = False
        in_dquote = False
        escape = False
        bracket_depth = 0
        in_regex = False

        def prev_nonspace() -> str:
            for k in range(len(buf) - 1, -1, -1):
                if not buf[k].isspace():
                    return buf[k]
            return ""

        i = 0
        n = len(s)
        while i < n:
            ch = s[i]

            if escape:
                buf.append(ch); escape = False; i += 1; continue
            if ch == "\\":
                buf.append(ch); escape = True; i += 1; continue
            if ch == "'" and not in_dquote and not in_regex:
                in_squote = not in_squote; buf.append(ch); i += 1; continue
            if ch == '"' and not in_squote and not in_regex:
                in_dquote = not in_dquote; buf.append(ch); i += 1; continue

            if not in_squote and not in_dquote:
                if ch == "[":
                    bracket_depth += 1; buf.append(ch); i += 1; continue
                if ch == "]" and bracket_depth > 0:
                    bracket_depth -= 1; buf.append(ch); i += 1; continue

            if not in_squote and not in_dquote and bracket_depth == 0 and ch == "/":
                if in_regex:
                    in_regex = False; buf.append(ch); i += 1; continue
                prev = prev_nonspace()
                j = i + 1
                while j < n and s[j].isspace():
                    j += 1
                nxt = s[j] if j < n else ""
                regexish_next = nxt in ("(", "[", "\\", "^", ".", "?", "*")
                if nxt == "(" and j + 2 < n and s[j:j+3].lower() == "(?i":
                    regexish_next = True
                if prev in ("", "(", ",", "=", " ", "\t", ":") and regexish_next:
                    in_regex = True; buf.append(ch); i += 1; continue

            if ch == "," and not in_squote and not in_dquote and bracket_depth == 0 and not in_regex:
                item = "".join(buf).strip()
                if item:
                    out.append(item)
                buf = []
                i += 1
                continue

            buf.append(ch)
            i += 1

        tail = "".join(buf).strip()
        if tail:
            out.append(tail)
        return out

    def _strip_outer_brackets(self, s: str) -> Tuple[str, bool]:
        t = (s or "").strip()
        if t.startswith("[") and t.endswith("]"):
            return t[1:-1].strip(), True
        return t, False

    def parse_union(self, stage: str) -> Dict[str, Any]:
        m = self.UNION_RE.match(stage)
        args = (m.group("args") if m else "").strip()
        items = self._split_top_level_commas(args)

        branches: List[Dict[str, Any]] = []
        branch_preds: List[Dict[str, Any]] = []
        b = 1

        for it in items:
            raw_item = it.strip()
            if not raw_item:
                continue
            inner, is_bracket = self._strip_outer_brackets(raw_item)
            if is_bracket:
                sub_ir = self.build_from_text(inner)
                bp = self._collect_subsearch_filter_predicate(sub_ir)
                if bp:
                    branch_preds.append(bp)
                branches.append({
                    "branch_id": f"B{b}",
                    "kind": "subsearch",
                    "has_pipeline": bool(sub_ir.get("pipeline")),
                    "predicate_ir": bp,
                    "pipeline_summary": self._branch_pipeline_summary(sub_ir),
                })
                b += 1
                continue
            branches.append({"branch_id": f"B{b}", "kind": "dataset", "dataset_ref": raw_item})
            b += 1

        pred: Optional[Dict[str, Any]] = None
        if len(branch_preds) == 1:
            pred = branch_preds[0]
        elif len(branch_preds) > 1:
            pred = {"type": "expr", "op": "OR", "children": branch_preds}

        info: Dict[str, Any] = {"branches": branches}
        if not branches and args:
            info["warning"] = "union args present but no branches parsed"

        all_binding_refs = bool(branches) and all(
            br.get("kind") == "dataset" and (br.get("dataset_ref") or "").startswith("$")
            for br in branches
        )
        io_meta = {
            "scope": "unknown",
            "parse_confidence": "partial",
            "outputs_dynamic": False,
            "input_pattern": None,
            "dataset_ref": None,
            "schema_preserving": all_binding_refs,
        }

        result: Dict[str, Any] = {
            "raw": stage,
            "type": "union",
            "inputs": [],
            "outputs": [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": info,
            "io_meta": io_meta,
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("derived_branch_summary", "partial")
        return result

    def parse_map(self, stage: str) -> Dict[str, Any]:
        m = self.MAP_SEARCH_RE.match(stage or "")
        body = (m.group("body") if m else "") or ""

        sub = ""
        mk = self.MAP_SEARCH_KV_RE.search(body)
        if mk:
            sub = (mk.group("dq") if mk.group("dq") is not None else mk.group("sq")) or ""
            sub = sub.strip()

        sub_ir = self._parse_subsearch_ir(sub) if sub else None
        pred = self._collect_subsearch_filter_predicate(sub_ir)

        result: Dict[str, Any] = {
            "raw": stage,
            "type": "map",
            "inputs": self._collect_all_inputs(sub_ir),
            "outputs": [],
            "layer": "L2",
            "predicate_ir": pred,
            "info": {
                "map_subsearch_stage_count": len((sub_ir.get("pipeline") or [])) if sub_ir else 0,
                "branch_summary": self._branch_pipeline_summary(sub_ir),
            },
        }
        if pred is not None:
            result["predicate_meta"] = self._predicate_meta("derived_branch_summary", "partial")
        return result

    def parse_foreach(self, stage: str) -> Dict[str, Any]:
        ins, outs, io_meta = infer_io_ext(stage, "foreach")
        return {
            "raw": stage,
            "type": "foreach",
            "inputs": ins or [],
            "outputs": outs or [],
            "layer": "L2",
            "predicate_ir": None,
            "info": {},
            "io_meta": io_meta,
        }

    def parse_unknown(self, stage: str, cmd_guess: str) -> Dict[str, Any]:
        raw_stage = stage or ""
        s = strip_redundant_search_star(raw_stage)

        if cmd_guess in NEVER_HAS_PREDICATE_CMDS:
            ins, outs, io_meta = infer_io_ext(raw_stage, cmd_guess)
            return {
                "raw": raw_stage,
                "type": cmd_guess,
                "inputs": ins or [],
                "outputs": outs or [],
                "layer": "L2",
                "predicate_ir": None,
                "info": {"note": "never_has_predicate"},
                "io_meta": io_meta,
            }

        if is_spl2_assignment(s):
            return {
                "raw": raw_stage,
                "type": cmd_guess,
                "inputs": [],
                "outputs": [],
                "layer": "L2",
                "predicate_ir": None,
                "info": {"note": "spl2_assignment_suppressed"},
            }

        base, sub = extract_trailing_bracket_block(s)
        if sub and re.match(r"(?ix)^\s*search\b", sub):
            pred_base = None
            if looks_like_implicit_boolean_root(base) or looks_like_implicit_root_search(base) or looks_like_bare_boolean(base):
                pred_base = self.parse_boolean_expr(base)

            sub_ir = self._parse_subsearch_ir(sub)

            ins = set()
            if pred_base:
                ins |= set(self.find_fields(pred_base))
            if sub_ir:
                for x in (sub_ir.get("root_search") or {}).get("inputs", []) or []:
                    ins.add(x)
                for c in sub_ir.get("pipeline", []) or []:
                    for x in (c.get("inputs", []) or []):
                        ins.add(x)
                    for x in (c.get("outputs", []) or []):
                        ins.add(x)

            return {
                "raw": raw_stage,
                "type": "search",
                "inputs": sorted(ins),
                "outputs": [],
                "layer": "L1",
                "predicate_ir": pred_base,
                "info": {
                    "source": "implicit_search_with_subsearch",
                    "base": base,
                    "subsearch": sub,
                    "subsearch_stage_count": len((sub_ir.get("pipeline") or [])) if sub_ir else 0,
                },
            }

        pred = None
        source = None

        if looks_like_implicit_boolean_root(s):
            pred = self.parse_boolean_expr(s)
            source = "implicit_unknown_boolean"
        elif looks_like_implicit_root_search(s):
            pred = self.parse_boolean_expr(s)
            source = "implicit_unknown_kv"

        if pred:
            return {
                "raw": raw_stage,
                "type": "search",
                "inputs": list(self.find_fields(pred)),
                "outputs": [],
                "layer": "L1",
                "predicate_ir": pred,
                "info": {"source": source},
            }

        return {
            "raw": raw_stage,
            "type": cmd_guess,
            "inputs": [],
            "outputs": [],
            "layer": "L2",
            "predicate_ir": None,
            "info": {},
        }

    def _parse_base_search(self, base_text: str) -> Optional[Dict[str, Any]]:
        s = (base_text or "").strip()
        if not s:
            return None
        if is_spl2_assignment(s):
            return None

        stage0 = strip_redundant_search_star(s)
        stage0_base, stage0_sub = extract_trailing_bracket_block(stage0)

        cmd_at_start = detect_cmd(stage0_base)
        if cmd_at_start == "search":
            st = self.parse_search(stage0)
            if st.get("predicate_ir") is not None or st.get("info", {}).get("source") == "explicit_search_noop":
                return {
                    "raw": s, "type": "search",
                    "inputs": st.get("inputs", []), "outputs": [],
                    "layer": "L1", "predicate_ir": st["predicate_ir"],
                    **(
                        {"predicate_meta": self._predicate_meta("root_search")}
                        if st.get("predicate_ir") is not None else {}
                    ),
                    "info": (
                        {"source": "explicit_search_base"}
                        if st.get("predicate_ir") is not None
                        else dict(st.get("info", {}))
                    ),
                }
        elif cmd_at_start == "where":
            st = self.parse_where(stage0)
            if st.get("predicate_ir") is not None:
                return {
                    "raw": s, "type": "search",
                    "inputs": st.get("inputs", []), "outputs": [],
                    "layer": "L1", "predicate_ir": st["predicate_ir"],
                    "predicate_meta": self._predicate_meta("root_search"),
                    "info": {"source": "explicit_where_base"},
                }
        elif cmd_at_start == "regex":
            st = self.parse_regex(stage0)
            if st.get("predicate_ir") is not None:
                return {
                    "raw": s, "type": "search",
                    "inputs": st.get("inputs", []), "outputs": [],
                    "layer": "L1", "predicate_ir": st["predicate_ir"],
                    "predicate_meta": self._predicate_meta("root_search"),
                    "info": {"source": "explicit_regex_base"},
                }

        macro_name, macro_tail = self.split_macro_stage(stage0_base)
        if macro_name and macro_tail:
            pred0 = None
            source = None
            if looks_like_implicit_boolean_root(macro_tail):
                pred0 = self.parse_boolean_expr(macro_tail)
                source = "macro_prefixed_stage0_boolean"
            elif looks_like_implicit_root_search(macro_tail):
                pred0 = self.parse_boolean_expr(macro_tail)
                source = "macro_prefixed_stage0_kv"
            elif looks_like_bare_boolean(macro_tail):
                pred0 = self.parse_boolean_expr(macro_tail)
                source = "macro_prefixed_stage0_bare_boolean"
            elif looks_like_predicate_expr(macro_tail):
                pred0 = self.parse_boolean_expr(macro_tail)
                source = "macro_prefixed_stage0_predicate_expr"
            if pred0:
                return {
                    "raw": stage0, "type": "search",
                    "inputs": list(self.find_fields(pred0)), "outputs": [],
                    "layer": "L1", "predicate_ir": pred0,
                    "predicate_meta": self._predicate_meta("root_search"),
                    "info": {"source": source, "macro_name": macro_name},
                }

        pred0 = None
        source = None
        if stage0_sub:
            pred0 = self.parse_boolean_expr(stage0)
            if pred0 is not None:
                source = "predicate_expr_with_subsearch_base"

        if pred0 is None and looks_like_implicit_boolean_root(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "implicit_stage0_boolean"
        elif pred0 is None and looks_like_implicit_root_search(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "implicit_stage0"
        elif pred0 is None and looks_like_bare_boolean(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "pre_pipe_stage0"
        elif pred0 is None and looks_like_predicate_expr(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "predicate_expr_base"

        if pred0:
            return {
                "raw": s, "type": "search",
                "inputs": list(self.find_fields(pred0)), "outputs": [],
                "layer": "L1", "predicate_ir": pred0,
                "predicate_meta": self._predicate_meta("root_search"),
                "info": {"source": source},
            }

        if _looks_like_literal_search_term(stage0_base):
            literal_val, literal_subtype = _extract_literal_value(stage0_base)
            if literal_val:
                noop_reason = _literal_search_noop_reason(literal_val)
                if noop_reason:
                    return {
                        "raw": s, "type": "search",
                        "inputs": [], "outputs": [],
                        "layer": "L1", "predicate_ir": None,
                        "info": {
                            "source": "literal_search_noop",
                            "noop_reason": noop_reason,
                            "note": f'Literal "{literal_val}" search treated as match-all/no-op placeholder.',
                        },
                    }
                pred_ir: Dict[str, Any] = {
                    "type": "predicate",
                    "field": {"type": "field", "value": "_raw"},
                    "operator": {"type": "operator", "value": "CONTAINS"},
                    "value": {"type": "value", "subtype": literal_subtype, "value": literal_val},
                }
                return {
                    "raw": s, "type": "search",
                    "inputs": ["_raw"], "outputs": [],
                    "layer": "L1", "predicate_ir": pred_ir,
                    "predicate_meta": self._predicate_meta("root_search"),
                    "info": {"source": "literal_base_search"},
                }

        return None

    def parse_command(self, stage: str) -> Dict[str, Any]:
        cmd = detect_cmd(stage)
        parse_fn_name = f"parse_{cmd}"
        safe = (
            cmd != "unknown"
            and parse_fn_name != "parse_command"
            and hasattr(self, parse_fn_name)
        )
        if cmd == "unknown" or not safe:
            st = self.parse_unknown(stage, cmd_guess=cmd)
        else:
            fn = getattr(self, parse_fn_name)
            st = fn(stage)
            st.setdefault("type", cmd)

        need_ins = not st.get("inputs")
        need_outs = not st.get("outputs")

        if need_ins or need_outs or "io_meta" not in st:
            ins, outs, io_meta = infer_io_ext(st.get("raw", stage), st.get("type", cmd))
            if need_ins and ins:
                st["inputs"] = ins
            if need_outs and outs:
                st["outputs"] = outs
            if "io_meta" not in st:
                st["io_meta"] = io_meta

        return st

    def _and_ir(self, a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not a:
            return b
        if not b:
            return a

        def flatten_and(x):
            if isinstance(x, dict) and x.get("type") == "expr" and x.get("op") == "AND":
                return list(x.get("children") or [])
            return [x]

        children = []
        children.extend(flatten_and(a))
        children.extend(flatten_and(b))
        return {"type": "expr", "op": "AND", "children": children}

    def _branch_pipeline_summary(self, sub_ir: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not sub_ir:
            return []

        BRANCH_CMDS = {"append", "appendpipe", "union", "map", "multisearch"}
        summary: List[Dict[str, Any]] = []

        rs = sub_ir.get("root_search")
        if isinstance(rs, dict):
            summary.append({
                "type": rs.get("type", "base_search"),
                "inputs": rs.get("inputs") or [],
                "outputs": rs.get("outputs") or [],
                "layer": rs.get("layer", "L1"),
                "predicate_ir": rs.get("predicate_ir"),
                "contributes_to_filter": rs.get("contributes_to_filter", True),
            })

        for stage in (sub_ir.get("pipeline") or []):
            if not isinstance(stage, dict):
                continue
            t = (stage.get("type") or "unknown").lower()
            layer = stage.get("layer", "L2")
            if t in BRANCH_CMDS:
                summary.append({
                    "type": t,
                    "inputs": stage.get("inputs") or [],
                    "outputs": stage.get("outputs") or [],
                    "layer": layer,
                    "has_nested_branch": True,
                    "contributes_to_filter": stage.get("contributes_to_filter", False),
                })
                continue
            entry: Dict[str, Any] = {
                "type": t,
                "inputs": stage.get("inputs") or [],
                "outputs": stage.get("outputs") or [],
                "layer": layer,
                "contributes_to_filter": stage.get("contributes_to_filter", False),
            }
            if layer == "L1" and stage.get("predicate_ir") is not None:
                entry["predicate_ir"] = stage["predicate_ir"]
            summary.append(entry)

        return summary

    def _collect_subsearch_filter_predicate(self, sub_ir: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not sub_ir:
            return None

        combined = None

        rs = sub_ir.get("root_search")
        if isinstance(rs, dict) and rs.get("predicate_ir"):
            combined = self._and_ir(combined, rs["predicate_ir"])

        for c in (sub_ir.get("pipeline") or []):
            if not isinstance(c, dict):
                continue
            if not c.get("predicate_ir"):
                continue
            t = (c.get("type") or "").lower()
            pm_source = (c.get("predicate_meta") or {}).get("source", "")
            if pm_source == "derived_branch_summary":
                continue
            if t in FILTER_CMDS or t == "base_search" or pm_source == "embedded_where":
                combined = self._and_ir(combined, c["predicate_ir"])

        return combined

    def classify_layers(self, cmds: List[Dict[str, Any]], root: Optional[Dict[str, Any]]):
        if root:
            root["layer"] = "L1"
            root["contributes_to_filter"] = root.get("predicate_ir") is not None
            if root.get("contributes_to_filter"):
                root["ctf_reason"] = "direct_predicate"
            else:
                root.pop("ctf_reason", None)
            # §5.8 / explicit placeholder searches: not real filters
            if (
                _is_noop_wildcard_pred(root.get("predicate_ir"))
                or (root.get("info") or {}).get("source") in {"explicit_search_noop", "literal_search_noop"}
            ):
                root["contributes_to_filter"] = False
                root["ctf_reason"] = "noop_search"

        if not cmds:
            return

        PRESENTATION_ALWAYS_L3 = {
            "table", "fields", "rename", "sort", "dedup",
            "head", "tail", "convert", "fieldformat", "format",
        }

        for c in cmds:
            t = _stage_type(c)
            c["contributes_to_filter"] = False
            c.pop("ctf_reason", None)

            if t in PRESENTATION_ALWAYS_L3:
                c["layer"] = "L3"
                continue

            if t == "base_search":
                c["layer"] = "L1"
                if c.get("predicate_ir") is not None:
                    c["contributes_to_filter"] = True
                    c["ctf_reason"] = "direct_predicate"
                continue

            if t in FILTER_CMDS:
                c["layer"] = "L1"
                if c.get("predicate_ir") is not None:
                    c["contributes_to_filter"] = True
                    c["ctf_reason"] = "direct_predicate"
                continue

            pm_source = (c.get("predicate_meta") or {}).get("source")
            if c.get("predicate_ir") is not None and pm_source != "derived_field_predicate":
                c["contributes_to_filter"] = True
                c["ctf_reason"] = "direct_predicate"

            if t in AGG_CMDS:
                c["layer"] = "L2"
                continue

            if t in {"into"}:
                c["layer"] = "L3"
                continue

            c["layer"] = "L2"

        last_pred_i = None
        for i, c in enumerate(cmds):
            t = _stage_type(c)
            if t == "base_search" or t in FILTER_CMDS or c.get("predicate_ir") is not None:
                last_pred_i = i

        PRESENTATION_CMDS = {
            "table", "fields", "rename", "sort", "dedup", "head", "tail",
            "convert", "fieldformat", "format",
        }
        if last_pred_i is not None:
            for i in range(last_pred_i + 1, len(cmds)):
                t = _stage_type(cmds[i])
                if t in PRESENTATION_CMDS:
                    cmds[i]["layer"] = "L3"

        filter_fields: Set[str] = set()

        for cmd in cmds:
            pm_source = (cmd.get("predicate_meta") or {}).get("source")
            is_direct_filter = (
                cmd.get("predicate_ir") is not None and pm_source != "derived_field_predicate"
            )
            if is_direct_filter:
                if not cmd.get("contributes_to_filter"):
                    cmd["contributes_to_filter"] = True
                    cmd["ctf_reason"] = "direct_predicate"
                filter_fields |= set(cmd.get("inputs", []))

        if root and root.get("inputs"):
            filter_fields |= set(root["inputs"])

        changed = True
        while changed:
            changed = False
            for cmd in reversed(cmds):
                outs = set(cmd.get("outputs", []))
                needed_outputs = outs & filter_fields
                if needed_outputs:
                    if not cmd["contributes_to_filter"]:
                        cmd["contributes_to_filter"] = True
                        cmd["ctf_reason"] = "field_dependency"
                        changed = True
                    filter_fields |= self._needed_upstream_inputs(cmd, needed_outputs)

        changed = True
        while changed:
            changed = False
            for i, cmd in enumerate(cmds):
                if cmd.get("contributes_to_filter"):
                    continue
                io_meta = cmd.get("io_meta") or {}
                if not cmd.get("outputs") and io_meta.get("schema_preserving"):
                    if any(cmds[j].get("contributes_to_filter") for j in range(i + 1, len(cmds))):
                        cmd["contributes_to_filter"] = True
                        cmd["ctf_reason"] = "schema_passthrough"
                        changed = True

        for c in cmds:
            if c.get("contributes_to_filter") and c.get("layer") == "L3":
                c["layer"] = "L2"

        if root and root.get("contributes_to_filter") and root.get("layer") == "L3":
            root["layer"] = "L2"

    # ---------------- program mode build ----------------

    def build_from_spl2_program(self, program_text: str) -> Dict[str, Any]:
        statements = split_statements(program_text)

        all_cmds: List[Dict[str, Any]] = []
        stmt_roots: List[Dict[str, Any]] = []

        for stmt in statements:
            binding, body = strip_optional_assignment(stmt)
            body = normalize_leading_pipe(body)

            stages, _seg = split_pipeline(body)
            stages = drop_garbage_prelude_stages(stages)
            cmds = [self.parse_command(strip_redundant_search_star(st)) for st in stages]

            if binding:
                for c in cmds:
                    c.setdefault("info", {})
                    c["info"]["spl2_binding"] = binding

            root_stmt = self._maybe_normalize_statement_stage0_as_base_search(
                body=body, stages=stages, cmds=cmds, stmt_binding=binding,
            )
            if root_stmt:
                stmt_roots.append(root_stmt)

            all_cmds.extend(cmds)

        root = None
        self.classify_layers(all_cmds, root)

        return {
            "has_pipeline": True if all_cmds else False,
            "pipeline": all_cmds,
            "root_search": None,
            "_seg": {
                "mode": "spl2_program",
                "statement_count": len(statements),
                "stage_count": len(all_cmds),
                "statement_root_count": len(stmt_roots),
            },
        }

    def _maybe_normalize_statement_stage0_as_base_search(
        self,
        body: str,
        stages: List[str],
        cmds: List[Dict[str, Any]],
        stmt_binding: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not stages or not cmds:
            return None

        stage0 = strip_redundant_search_star((stages[0] or "").strip())
        stage0_base, stage0_sub = extract_trailing_bracket_block(stage0)

        if is_spl2_assignment(stage0):
            return None

        pred0 = None
        source = None
        if stage0_sub:
            pred0 = self.parse_boolean_expr(stage0)
            if pred0 is not None:
                source = "spl2_stmt_subsearch_predicate"

        if pred0 is None and looks_like_implicit_boolean_root(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "spl2_stmt_implicit_stage0_boolean"
        elif pred0 is None and looks_like_implicit_root_search(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "spl2_stmt_implicit_stage0_kv"
        elif pred0 is None and looks_like_bare_boolean(stage0_base):
            pred0 = self.parse_boolean_expr(stage0_base)
            source = "spl2_stmt_bare_boolean"

        if not pred0:
            return None

        root = {
            "raw": stage0,
            "type": "search",
            "inputs": list(self.find_fields(pred0)),
            "outputs": [],
            "layer": "L1",
            "predicate_ir": pred0,
            "info": {"source": source},
        }
        if stmt_binding:
            root.setdefault("info", {})
            root["info"]["spl2_binding"] = stmt_binding

        cmds[0]["type"] = "base_search"
        cmds[0]["layer"] = "L1"
        cmds[0]["predicate_ir"] = pred0
        cmds[0]["inputs"] = list(self.find_fields(pred0))
        cmds[0].setdefault("info", {})
        cmds[0]["info"]["normalized_as_root_search"] = True
        cmds[0]["info"]["normalized_root_source"] = source
        if stmt_binding:
            cmds[0]["info"]["spl2_binding"] = stmt_binding

        return root

    # ---------------- normal build ----------------

    def build_from_text(self, spl_text: str) -> Dict[str, Any]:
        spl_text, stripped_embedded_wrapper = _normalize_embedded_pipeline_wrapper(spl_text)
        if looks_like_spl2_program(spl_text):
            ir = self.build_from_spl2_program(spl_text)
            if stripped_embedded_wrapper:
                ir.setdefault("_seg", {})["stripped_wrapper"] = True
            return ir

        base_text, pipe_stage_strs, seg_meta = split_spl_regions(spl_text)
        if stripped_embedded_wrapper:
            seg_meta["stripped_wrapper"] = True
        pipe_stage_strs = drop_garbage_prelude_stages(pipe_stage_strs)

        cmds = [
            self.parse_command(strip_redundant_search_star(st))
            for st in pipe_stage_strs
        ]

        root = self._parse_base_search(base_text)

        if root is None and base_text.strip() and pipe_stage_strs:
            base_cmd = self.parse_command(strip_redundant_search_star(base_text.strip()))
            cmds = [base_cmd] + cmds

        if root is None and cmds:
            first = cmds[0]
            if first.get("type") in {"search", "where"} and first.get("predicate_ir"):
                root = {
                    "raw": first.get("raw", "(root search)"),
                    "type": "search",
                    "inputs": first.get("inputs", []),
                    "outputs": [],
                    "layer": "L1",
                    "predicate_ir": first.get("predicate_ir"),
                    "predicate_meta": self._predicate_meta("root_search"),
                    "info": {"source": first.get("type")},
                }
                cmds = cmds[1:]

        self.classify_layers(cmds, root)

        return {
            "has_pipeline": bool(cmds),
            "pipeline": cmds,
            "root_search": root,
            "_seg": seg_meta,
        }


# ============================================================
# 8) Factory
# ============================================================

_LIB_DIR = Path(__file__).resolve().parent


def make_builder(
    grammar_name: str = "boolean_expr.lark",
    quiet: bool = False,
) -> UnifiedIRBuilder:
    """
    Load the Lark grammar from lib/ and return a ready-to-use UnifiedIRBuilder.

    grammar_name: filename of a .lark file in lib/ (default: boolean_expr.lark)
    quiet:        suppress per-entry warnings from the builder
    """
    grammar_path = _LIB_DIR / grammar_name
    grammar_text = grammar_path.read_text(encoding="utf-8")
    # LALR uses substantially less memory than Earley on the large OR-heavy
    # predicates in this dataset, while keeping the same fallback path when
    # grammar parsing fails.
    boolean_parser = Lark(grammar_text, start="expr", parser="lalr")
    return UnifiedIRBuilder(boolean_parser=boolean_parser, quiet=quiet)
