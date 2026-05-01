# spl_bool_fallback.py
from __future__ import annotations

import re
from typing import List, Optional, Dict, Any, Tuple


# ============================================================
# Fallback boolean parser (NO Lark): robust + fast for failures
# Emits SAME IR schema as ToRawIR
# ============================================================

_FB_BOOL_OPS = {"OR", "AND", "NOT"}

# Keep operator normalization local (avoid dependency on ToRawIR)
_OP_NORMALIZE = {
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
    "MATCHES_REGEX": "MATCHES_REGEX",
    "NOT_MATCHES_REGEX": "NOT_MATCHES_REGEX",
    "IS_NULL": "IS_NULL",
    "IS_NOT_NULL": "IS_NOT_NULL",
}

_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:{}\-]*$")
# Also match quoted field names like "api.operation"
_QUOTED_FIELD_RE = re.compile(r'^"(?!\*)(?:\\.|[^"\\])+"$')
# Match bare field, quoted field, or function-call field before IN
_IN_RE = re.compile(
    r'^(?P<field>[A-Za-z_][A-Za-z0-9_.:{}\-]*|"(?:\\.|[^"\\])+"'
    r'|[A-Za-z_]\w*\s*\([^)]*\))\s+(?i:IN)\s*\((?P<body>.*)\)\s*$',
    re.DOTALL,
)


_FUNC_FIELD_RE = re.compile(r'^[A-Za-z_]\w*\s*\(.+\)$', re.DOTALL)
_NUMERIC_RE = re.compile(r'^-?\.?\d+(?:\.\d+)?$')


def _is_field_token(t: str) -> bool:
    """Check if a token looks like a field name, quoted field, or function call."""
    return bool(_FIELD_RE.match(t) or _QUOTED_FIELD_RE.match(t) or _FUNC_FIELD_RE.match(t))


def _is_comparable_lhs(t: str) -> bool:
    """Check if a token can appear on the left side of a comparison operator.
    Includes field names, function calls, and numeric literals (e.g. .25)."""
    return _is_field_token(t) or bool(_NUMERIC_RE.match(t))


def _unquote_field(t: str) -> str:
    """Strip outer quotes from a quoted field token, if present."""
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1]
    return t

# SQL-style null-check syntax: <field> IS NOT NULL / <field> IS NULL
# Must be matched before tokenization so that "IS", "NOT", "NULL" are not treated
# as separate boolean/literal atoms.
_RE_IS_NOT_NULL = re.compile(
    r'\b([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s+IS\s+NOT\s+NULL\b',
    re.IGNORECASE,
)
_RE_IS_NULL = re.compile(
    r'\b([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s+IS\s+NULL\b',
    re.IGNORECASE,
)

# --- PRED_FOV machinery (function-compare atom collapsing) ---
_PRED_FOV_RE = re.compile(r"^PRED_FOV\((?P<body>.*)\)$")

_RE_FUNC_BOOL = re.compile(
    r"""
    (?P<lhs>\bmatch_regex\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*/(?:\\\/|[^/])*/[A-Za-z]*\s*\))
    \s*(?P<op>=|!=|==)\s*
    (?P<rhs>true|false)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Boolean function patterns: isnull/isnotnull with optional = true/false comparison
_RE_ISNOTNULL_BOOL = re.compile(
    r'\bisnotnull\s*\(\s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*\)(?:\s*(?:==|=)\s*(true|false))?',
    re.IGNORECASE,
)
_RE_ISNULL_BOOL = re.compile(
    r'\bisnull\s*\(\s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*\)(?:\s*(?:==|=)\s*(true|false))?',
    re.IGNORECASE,
)
# like(field, "pattern") SPL function form
_RE_LIKE_FUNC = re.compile(
    r'\blike\s*\(\s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*,\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')\s*\)',
    re.IGNORECASE,
)
_RE_LIKE_FUNC_UNQUOTED = re.compile(
    r'\blike\s*\(\s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*,\s*([^,\)]+?)\s*\)',
    re.IGNORECASE,
)
# match(field, "pattern") / match(field, /pattern/) with optional = true|false
_RE_MATCH_FUNC = re.compile(
    r"""
    \bmatch\s*\(
        \s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*,
        \s*(
            /(?:\\\/|[^/])*/[A-Za-z]*
            |
            "(?:\\.|[^"\\])*"
            |
            '(?:\\.|[^'\\])*'
        )\s*
    \)
    (?:\s*(==|=|!=)\s*(true|false))?
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
# Standalone match_regex(field, /pattern/) without = true/false (SPL2 boolean usage)
_RE_MATCH_REGEX_ATOM = re.compile(
    r'^match_regex\s*\(\s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*,\s*/(.+)/[A-Za-z]*\s*\)$',
    re.IGNORECASE | re.DOTALL,
)
_RE_MATCH_ATOM = re.compile(
    r"""
    ^match\s*\(
        \s*([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*,
        \s*(
            /(?:\\\/|[^/])*/[A-Za-z]*
            |
            "(?:\\.|[^"\\])*"
            |
            '(?:\\.|[^'\\])*'
        )\s*
    \)$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# If you want: extend this cue in the caller to decide whether to attempt fallback.
BOOL_CUE_RE = re.compile(
    r"""
    \b(AND|OR|NOT|IN|LIKE|MATCHES|CONTAINS|STARTSWITH|ENDSWITH)\b
    | (=|!=|>=|<=|<|>)
    | \(
    """,
    re.IGNORECASE | re.VERBOSE
)


def strip_subsearches(expr: str) -> str:
    """
    Remove bracket-delimited subsearch blocks  ``[...]``  from a boolean
    expression before tokenization.

    SPL subsearches like  ``[| inputlookup foo | fields bar]``  produce
    spurious  ``_raw CONTAINS``  predicates for every token inside the
    brackets (``inputlookup``, ``rename``, ``as``, ``fields``, ``|``, …).

    This function strips the *entire* ``[...]`` block (including nested
    brackets) so the boolean parser never sees it.  A placeholder atom
    ``__SUBSEARCH__``  is left in its place so we don't accidentally merge
    the tokens that surrounded the brackets.
    """
    out: list[str] = []
    depth = 0
    in_s = in_d = esc = False
    in_regex = False
    in_regex_char_class = False

    def _prev_nonspace_out() -> str:
        for ch in reversed(out):
            if not ch.isspace():
                return ch
        return ""

    s = expr or ""
    n = len(s)
    for i, ch in enumerate(s):
        if esc:
            if depth == 0:
                out.append(ch)
            esc = False
            continue
        if ch == "\\":
            esc = True
            if depth == 0:
                out.append(ch)
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
            if depth == 0:
                out.append(ch)
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            if depth == 0:
                out.append(ch)
            continue

        if not in_s and not in_d:
            if ch == "/":
                if in_regex:
                    if in_regex_char_class:
                        if depth == 0:
                            out.append(ch)
                        continue
                    if depth == 0:
                        out.append(ch)
                    in_regex = False
                    continue

                prev = _prev_nonspace_out()
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
                    in_regex = True
                    continue

        if in_regex:
            if ch == "[" and not in_regex_char_class:
                in_regex_char_class = True
                if depth == 0:
                    out.append(ch)
                continue
            if ch == "]" and in_regex_char_class:
                in_regex_char_class = False
                if depth == 0:
                    out.append(ch)
                continue

        if not in_s and not in_d and not in_regex:
            if ch == "[":
                if depth == 0:
                    out.append("__SUBSEARCH__")
                depth += 1
                continue
            if ch == "]":
                depth -= 1
                if depth < 0:
                    depth = 0
                continue

        if depth == 0:
            out.append(ch)

    return "".join(out)


# SPL pipeline commands that should NEVER appear as boolean atoms.
# If found outside quotes at the start of a whitespace-delimited token,
# everything from that point onward is treated as a leaked pipeline
# stage and truncated from the boolean expression.
_SPL_COMMANDS = frozenset({
    "stats", "eventstats", "streamstats", "mstats", "tstats",
    "eval", "where", "search",
    "table", "fields", "rename", "sort", "dedup", "head", "tail",
    "top", "rare", "chart", "timechart", "transaction",
    "join", "append", "appendpipe", "collect",
    "lookup", "inputlookup", "outputlookup",
    "rex", "regex", "replace", "convert",
    "fillnull", "filldown", "multisearch", "foreach",
    "return", "format", "map", "mvexpand", "makemv", "mvcombine",
    "iplocation", "geostats", "outputcsv", "inputcsv",
    "rest", "metadata", "abstract", "addinfo", "addtotals",
    "bucket", "bin", "cluster", "delta", "diff", "erex",
    "eventcount", "extract", "loadjob", "localop",
    "multireport", "overlap", "predict", "rangemap",
    "reltime", "require", "reverse", "run",
    "savedsearch", "script", "scrub", "sendemail",
    "set", "setfields", "strcat", "tags",
    "typeahead", "typelearner", "typer", "union", "uniq",
    "untable", "xyseries", "xmlkv", "xpath",
})

# Regex: a bare SPL command keyword at a token boundary, not preceded by
# a comparison operator (so  ``field=stats``  is not falsely matched).
_RE_SPL_CMD_BOUNDARY = re.compile(
    r'(?<![=<>!"\w.])(?:' + "|".join(re.escape(c) for c in sorted(_SPL_COMMANDS, key=len, reverse=True)) + r')(?=\s|$)',
    re.IGNORECASE,
)


def strip_trailing_spl_commands(expr: str) -> str:
    """
    Truncate a boolean expression at the first unquoted SPL pipeline
    command keyword.

    When SPL is malformed (missing pipe), the boolean expression may
    contain leaked pipeline stages like:
        ``Message="*Delete*" stats count min(_time) as firstTime by host``

    This function detects the first bare SPL command keyword (``stats``)
    outside of quotes and returns only the text before it:
        ``Message="*Delete*"``
    """
    out = expr or ""
    in_s = in_d = esc = False
    i = 0
    while i < len(out):
        ch = out[i]
        if esc:
            esc = False; i += 1; continue
        if ch == "\\":
            esc = True; i += 1; continue
        if ch == "'" and not in_d:
            in_s = not in_s; i += 1; continue
        if ch == '"' and not in_s:
            in_d = not in_d; i += 1; continue
        if not in_s and not in_d:
            m = _RE_SPL_CMD_BOUNDARY.match(out, i)
            if m:
                # Make sure the matched word is a standalone token, not part
                # of a dotted field (e.g. ``Processes.search`` should NOT match).
                if i == 0 or out[i - 1].isspace():
                    # Don't truncate if the previous non-whitespace character is
                    # a comparison operator — the keyword is likely a VALUE,
                    # not a leaked SPL command (e.g. ``action= run``).
                    prev_nws = ""
                    for k in range(i - 1, -1, -1):
                        if not out[k].isspace():
                            prev_nws = out[k]
                            break
                    if prev_nws not in ("=", ">", "<", "!"):
                        return out[:i].strip()
        i += 1
    return out


def _escape_pred_fov_delim(s: str) -> str:
    s = s or ""
    return s.replace("\\", r"\\").replace("|", r"\|")


def _unescape_pred_fov_delim(s: str) -> str:
    out: List[str] = []
    src = s or ""
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\" and i + 1 < n and src[i + 1] in {"\\", "|"}:
            out.append(src[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_regex_literal_delims(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == "/" and "/" in s[1:]:
        end = s.rfind("/")
        if end > 0:
            return s[1:end]
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def collapse_where_atoms(expr: str) -> str:
    """
    Best-effort canonicalization for fallback parsing:
    - Collapse <field> IS NOT NULL / <field> IS NULL into PRED_FOV(...)
    - Collapse isnotnull(field) / isnull(field) [= true/false] into PRED_FOV(...)
    - Collapse like(field, "pattern") into PRED_FOV(...)
    - Collapse match(field, "pattern"|/pattern/) [= true/false] into PRED_FOV(...)
    - Collapse match_regex(field, /.../)=true|false into PRED_FOV(...) with proper field extraction
    - Collapse <ident> op null into PRED_FOV(...)
    """
    out = expr or ""

    # field IS NOT NULL → PRED_FOV(field|IS_NOT_NULL|true)
    # Must be processed before IS NULL to avoid partial overlap.
    def _sub_is_not_null(m: re.Match) -> str:
        return f"PRED_FOV({_escape_pred_fov_delim(m.group(1))}|IS_NOT_NULL|true)"

    out = _RE_IS_NOT_NULL.sub(_sub_is_not_null, out)

    # field IS NULL → PRED_FOV(field|IS_NULL|true)
    def _sub_is_null(m: re.Match) -> str:
        return f"PRED_FOV({_escape_pred_fov_delim(m.group(1))}|IS_NULL|true)"

    out = _RE_IS_NULL.sub(_sub_is_null, out)

    # isnotnull(field) [= true/false] → PRED_FOV(field|IS_NOT_NULL|true) or IS_NULL
    def _sub_isnotnull(m: re.Match) -> str:
        field = m.group(1)
        cmp = (m.group(2) or "true").lower()
        op = "IS_NULL" if cmp == "false" else "IS_NOT_NULL"
        return f"PRED_FOV({_escape_pred_fov_delim(field)}|{op}|true)"

    out = _RE_ISNOTNULL_BOOL.sub(_sub_isnotnull, out)

    # isnull(field) [= true/false] → PRED_FOV(field|IS_NULL|true) or IS_NOT_NULL
    def _sub_isnull(m: re.Match) -> str:
        field = m.group(1)
        cmp = (m.group(2) or "true").lower()
        op = "IS_NOT_NULL" if cmp == "false" else "IS_NULL"
        return f"PRED_FOV({_escape_pred_fov_delim(field)}|{op}|true)"

    out = _RE_ISNULL_BOOL.sub(_sub_isnull, out)

    # like(field, "pattern") → PRED_FOV(field|LIKE|"pattern")
    def _sub_like(m: re.Match) -> str:
        field = m.group(1)
        pattern = m.group(2)
        return f"PRED_FOV({_escape_pred_fov_delim(field)}|LIKE|{_escape_pred_fov_delim(pattern)})"

    out = _RE_LIKE_FUNC.sub(_sub_like, out)

    # Malformed like(field, %pattern%") / like(field, %pattern%) style args.
    # These appear in SSA rules where one quote is missing around the pattern.
    def _sub_like_unquoted(m: re.Match) -> str:
        field = m.group(1)
        pattern = (m.group(2) or "").strip()
        if not pattern:
            return m.group(0)
        if pattern[0] in {'"', "'"}:
            return m.group(0)
        normalized = pattern.strip()
        if normalized.endswith('"') or normalized.endswith("'"):
            normalized = normalized[:-1].rstrip()
        if not any(ch in normalized for ch in ("%", "*", "?", ":", "/", ".", "\\", "-")):
            return m.group(0)
        quoted = '"' + normalized.replace('"', '\\"') + '"'
        return f"PRED_FOV({_escape_pred_fov_delim(field)}|LIKE|{_escape_pred_fov_delim(quoted)})"

    out = _RE_LIKE_FUNC_UNQUOTED.sub(_sub_like_unquoted, out)

    # match(field, "pattern"|/pattern/) [= true/false]
    # → PRED_FOV(field|MATCHES_REGEX|pattern) or NOT_MATCHES_REGEX
    def _sub_match(m: re.Match) -> str:
        field = m.group(1)
        pattern = _strip_regex_literal_delims(m.group(2))
        cmp_op = m.group(3)
        cmp_val = (m.group(4) or "true").lower()
        negate = cmp_val == "false" or cmp_op == "!="
        op = "NOT_MATCHES_REGEX" if negate else "MATCHES_REGEX"
        return (
            f"PRED_FOV({_escape_pred_fov_delim(field)}|{op}|"
            f"{_escape_pred_fov_delim(pattern)})"
        )

    out = _RE_MATCH_FUNC.sub(_sub_match, out)

    # match_regex(field, /pattern/) = true/false
    # → PRED_FOV(field|MATCHES_REGEX|pattern) or PRED_FOV(field|NOT_MATCHES_REGEX|pattern)
    def _sub_match_regex(m: re.Match) -> str:
        lhs = m.group("lhs")
        negate = m.group("rhs").strip().lower() == "false"
        m2 = re.match(
            r'match_regex\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*/(.+)/[A-Za-z]*\s*\)',
            lhs.strip(), re.IGNORECASE | re.DOTALL,
        )
        if m2:
            field = m2.group(1)
            pattern = m2.group(2)
            op = "NOT_MATCHES_REGEX" if negate else "MATCHES_REGEX"
            return f"PRED_FOV({_escape_pred_fov_delim(field)}|{op}|{_escape_pred_fov_delim(pattern)})"
        # Fallback: preserve old behavior for unrecognized forms
        lhs_clean = re.sub(r"\s+", " ", lhs).strip()
        op = "="
        rhs = m.group("rhs").lower()
        return f"PRED_FOV({_escape_pred_fov_delim(lhs_clean)}|{op}|{rhs})"

    out = _RE_FUNC_BOOL.sub(_sub_match_regex, out)

    def _sub_null(m: re.Match) -> str:
        lhs = m.group(1)
        op = m.group(2)
        if op == "==":
            op = "="
        rhs = m.group(3).lower()
        return f"PRED_FOV({_escape_pred_fov_delim(lhs)}|{op}|{rhs})"

    out = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.:{}\-]*)\s*(==|=|!=)\s*(null)\b",
        _sub_null,
        out,
        flags=re.IGNORECASE,
    )
    return out


def _fb_tok_quote_aware(s: str) -> List[str]:
    out: List[str] = []
    buf: List[str] = []
    in_s = False
    in_d = False
    esc = False

    func_paren_depth = 0        # for IDENT(...)
    atom_paren_depth = 0        # for wildcard atoms like *GetCurrent()*

    def flush():
        nonlocal buf
        if buf:
            t = "".join(buf).strip()
            if t:
                u = t.upper()
                # Normalize boolean ops AND/OR/NOT to uppercase; also normalize
                # IN so that mixed-case variants ("In", "in") match _fb_merge_in_lists.
                if u in _FB_BOOL_OPS or u == "IN":
                    out.append(u)
                else:
                    out.append(t)
            buf = []

    def buf_text():
        return "".join(buf)

    for ch in (s or "").strip():
        if esc:
            buf.append(ch); esc = False; continue
        if ch == "\\":
            buf.append(ch); esc = True; continue

        if ch == "'" and not in_d:
            in_s = not in_s; buf.append(ch); continue
        if ch == '"' and not in_s:
            in_d = not in_d; buf.append(ch); continue

        if not in_s and not in_d:
            if func_paren_depth > 0 or atom_paren_depth > 0:
                if ch == "(":
                    if func_paren_depth > 0:
                        func_paren_depth += 1
                    else:
                        atom_paren_depth += 1
                elif ch == ")":
                    if func_paren_depth > 0:
                        func_paren_depth -= 1
                    else:
                        atom_paren_depth -= 1
                buf.append(ch)
                continue

            if ch.isspace():
                flush()
                continue

            if ch == "(":
                cur = buf_text().strip()

                if buf and ("*" in cur or "?" in cur):
                    buf.append(ch)
                    atom_paren_depth = 1
                    continue

                if cur and cur.upper() in _FB_BOOL_OPS_U:
                    flush()
                    out.append("(")
                    continue

                if cur and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cur):
                    buf.append(ch)
                    func_paren_depth = 1
                    continue

                flush()
                out.append("(")
                continue

            if ch == ")":
                flush()
                out.append(")")
                continue

        buf.append(ch)

    flush()
    return out


def _fb_split_glued_bool_ops(tokens: List[str]) -> List[str]:
    """
    Split boolean keywords that are glued to adjacent atoms.

    Examples:
      PRED_FOV(process_exec|LIKE|"%.mdb%")OR  ->  [PRED_FOV(...), OR]
    """
    out: List[str] = []
    for t in tokens:
        if t in ("AND", "OR", "NOT", "(", ")"):
            out.append(t)
            continue
        if t.endswith("OR") and len(t) > 2 and (t[-3] == ")" or t[-3] == '"' or t[-3] == "'"):
            out.append(t[:-2])
            out.append("OR")
            continue
        if t.endswith("AND") and len(t) > 3 and (t[-4] == ")" or t[-4] == '"' or t[-4] == "'"):
            out.append(t[:-3])
            out.append("AND")
            continue
        out.append(t)
    return out


def _fb_merge_in_lists(tokens: List[str]) -> List[str]:
    out: List[str] = []
    i = 0

    while i < len(tokens):
        if i + 1 < len(tokens) and _is_field_token(tokens[i]):
            m_inline = re.match(r'^(?i:IN)\((?P<body>.*)\)$', tokens[i + 1], re.DOTALL)
            if m_inline:
                field = _unquote_field(tokens[i])
                body = (m_inline.group("body") or "").strip()
                out.append(f"{field} IN ({body})")
                i += 2
                continue

        if (
            i + 2 < len(tokens)
            and _is_field_token(tokens[i])
            and tokens[i + 1] == "IN"
            and tokens[i + 2] == "("
        ):
            field = _unquote_field(tokens[i])
            i += 3

            body_parts: List[str] = []
            depth = 1
            while i < len(tokens):
                cur = tokens[i]
                if cur == "(":
                    depth += 1
                    body_parts.append(cur)
                elif cur == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                    body_parts.append(cur)
                else:
                    body_parts.append(cur)
                i += 1

            merged = f"{field} IN ({' '.join(body_parts).strip()})"
            out.append(merged)
            continue

        out.append(tokens[i])
        i += 1

    return out


def _fb_merge_macro_invocations(tokens: List[str]) -> List[str]:
    out: List[str] = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]
        if (
            i + 1 < len(tokens)
            and re.match(r'^(?i:MACRO)\s*=\s*[A-Za-z_][A-Za-z0-9_]*$', tok)
            and tokens[i + 1] == "("
        ):
            head = tok
            i += 2
            depth = 1
            body_parts: List[str] = []

            while i < len(tokens):
                cur = tokens[i]
                if cur == "(":
                    depth += 1
                    body_parts.append(cur)
                elif cur == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                    body_parts.append(cur)
                else:
                    body_parts.append(cur)
                i += 1

            body = " ".join(body_parts).strip()
            out.append(f"{head}({body})")
            continue

        out.append(tok)
        i += 1

    return out


_FB_OPS_SET: set = {"=", "==", "!=", ">=", "<=", ">", "<"}
_FB_TEXT_OPS_SET: set = {"LIKE", "MATCHES", "CONTAINS", "STARTSWITH", "ENDSWITH"}
_FB_BOOL_OPS_U: set = {"OR", "AND", "NOT", "IN"}
# Arithmetic operators that can appear between two field/number terms on the LHS
# of a comparison.  "/" is intentionally excluded to avoid conflicting with the
# regex-literal detector in split_pipeline.
_ARITH_OPS_SET: set = {"-", "+", "%"}
_ARITH_EXPR_OPS_SET: set = {"-", "+", "%", "*", "/"}


_RE_TRAILING_OP = re.compile(
    r'^(?P<field>[A-Za-z_][A-Za-z0-9_.:{}\-]*)(?P<op>==|!=|>=|<=|=|>|<)$'
)

# Match a token that *begins* with a comparison operator followed by a non-empty
# right-hand side, e.g.  "!=console.amazonaws.com"  or  ">=10".
# This happens when SPL has no space between the operator and the value:
#   userAgent !=console.amazonaws.com
# → tokenizer produces ["userAgent", "!=console.amazonaws.com"]
_RE_LEADING_OP = re.compile(r'^(?P<op>==|!=|>=|<=|=|>|<)(?P<rhs>\S.*)$')


def _fb_split_leading_ops(tokens: List[str]) -> List[str]:
    """
    Split tokens that start with a comparison operator fused to the value.

    ``!=console.amazonaws.com``  →  ``["!=", "console.amazonaws.com"]``

    This is the mirror of _fb_split_trailing_ops: trailing-op handles
    ``field=`` (operator stuck to LHS), leading-op handles ``!=value``
    (operator stuck to RHS).  After splitting, _fb_merge_field_op_value
    can recognize the [field, op, value] triplet normally.
    """
    out: List[str] = []
    for t in tokens:
        if t in _FB_OPS_SET:
            out.append(t)
            continue
        m = _RE_LEADING_OP.match(t)
        if m:
            out.append(m.group("op"))
            out.append(m.group("rhs"))
        else:
            out.append(t)
    return out


def _fb_merge_split_bang_eq(tokens: List[str]) -> List[str]:
    """
    Merge a lone ``!`` immediately followed by ``=`` back into ``!=``.

    When SPL has ``Account_Name ! = "value"`` (spaces around both ``!``
    and ``=``), the tokenizer produces ``["Account_Name", "!", "=", ...]``.
    This pass re-fuses ``["!", "="]`` → ``["!="]`` before the comparison
    merge pass runs.
    """
    out: List[str] = []
    for t in tokens:
        if out and out[-1] == "!" and t == "=":
            out[-1] = "!="
        else:
            out.append(t)
    return out


def _fb_strip_as_aliases(tokens: List[str]) -> List[str]:
    """
    Remove  ``field AS alias``  rename-syntax triplets from the token stream.

    When SPL has::

        search islibrary = True process_name AS ImageLoaded

    the ``process_name AS ImageLoaded`` fragment is a field-rename hint, not
    a boolean predicate.  Without this pass, ``AS`` and ``ImageLoaded``
    become spurious  ``_raw CONTAINS``  atoms.

    For each occurrence of an atom followed by the ``AS`` keyword and
    another atom, the whole triplet is dropped (the leading field atom is
    consumed so that it doesn't become a bare _raw CONTAINS either).
    """
    out: List[str] = []
    i = 0
    skip_next = False
    while i < len(tokens):
        if skip_next:
            skip_next = False
            i += 1
            continue
        if (
            i + 2 < len(tokens)
            and tokens[i + 1].upper() == "AS"
            and tokens[i] not in ("(", ")", "AND", "OR", "NOT", "IN")
            and tokens[i + 2] not in ("(", ")", "AND", "OR", "NOT", "IN")
        ):
            # Drop the entire "field AS alias" triplet
            i += 3
            continue
        out.append(tokens[i])
        i += 1
    return out


def _looks_like_fused_predicate_atom(t: str) -> bool:
    t = (t or "").strip()
    if not t:
        return False
    if _PRED_FOV_RE.match(t) or _IN_RE.match(t) or _RE_MATCH_ATOM.match(t) or _RE_MATCH_REGEX_ATOM.match(t):
        return True
    if re.match(
        r'^(?P<field>[A-Za-z_][A-Za-z0-9_.:{}\-]*|"(?:\\.|[^"\\])+"|[A-Za-z_]\w*\s*\(.+\))'
        r'\s+(?P<op>LIKE|MATCHES|CONTAINS|STARTSWITH|ENDSWITH)\s+.+$',
        t,
        re.IGNORECASE | re.DOTALL,
    ):
        return True
    in_s = in_d = esc = False
    for i, ch in enumerate(t):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            continue
        if not in_s and not in_d:
            for op in ("!=", ">=", "<=", "==", "=", ">", "<"):
                if t.startswith(op, i):
                    left = t[:i].strip()
                    right = t[i + len(op):].strip()
                    return _is_comparable_lhs(left) and right != ""
    return False


def _fb_strip_trailing_field_list(tokens: List[str]) -> List[str]:
    """
    Drop a trailing bare field list after one or more real predicates.

    Example:
      Processes.process_name=certutil.exe Processes.process="* -exportPFX *"
      Processes.dest Processes.user Processes.process_id

    The suffix is output-column syntax leaked into the boolean parser, not a
    free-text search. If we keep it, implicit-AND turns each field token into a
    spurious ``_raw CONTAINS`` predicate.
    """
    if len(tokens) < 3:
        return tokens

    start = len(tokens)
    while start > 0 and _is_field_token(tokens[start - 1]):
        start -= 1

    suffix = tokens[start:]
    prefix = tokens[:start]
    if len(suffix) < 2:
        return tokens
    if not any("." in tok or ":" in tok for tok in suffix):
        return tokens
    if not any(_looks_like_fused_predicate_atom(tok) for tok in prefix):
        return tokens
    if any(tok in ("(", ")") or tok.upper() in _FB_BOOL_OPS_U for tok in suffix):
        return tokens
    return prefix


def _fb_split_trailing_ops(tokens: List[str]) -> List[str]:
    """
    Split tokens that have a trailing operator fused to the field name.

    When SPL has  ``field= value``  (space AFTER the operator only), the
    tokenizer produces  ``["field=", "value"]``.  This pass splits
    ``"field="``  into  ``["field", "="]``  so that
    ``_fb_merge_field_op_value`` can later merge  ``[field, =, value]``
    back into a single atom.
    """
    out: List[str] = []
    for t in tokens:
        m = _RE_TRAILING_OP.match(t)
        if m:
            out.append(m.group("field"))
            out.append(m.group("op"))
        else:
            out.append(t)
    return out


def _fb_merge_field_op_value(tokens: List[str]) -> List[str]:
    """
    Merge whitespace-split  [field, op, value]  triplets back into single atoms.

    The tokenizer splits on whitespace, so  ``Message = "*foo*"``  becomes
    ``["Message", "=", '"*foo*"']`` — three tokens.  ``_fb_insert_implicit_and``
    then inserts AND between them, turning the predicate into three separate
    ``_raw CONTAINS`` literals.  This pass re-fuses the triplets so that
    ``_fb_atom_to_predicate`` can recognise them as  field=value  predicates.

    Rules for merging:
    - tokens[i]   must match _FIELD_RE  (a valid field identifier)
    - tokens[i+1] must be a comparison operator  (=, !=, >=, <=, >, <)
    - tokens[i+2] must NOT be a boolean keyword or another operator
    - If tokens[i+2] is "(", consume through matching ")" to handle
      arithmetic expressions like  ``count > (avg + stdev)``
    """
    out: List[str] = []
    i = 0

    def _collect_expr_term(start: int) -> Tuple[Optional[str], int]:
        if start >= len(tokens):
            return None, start
        if tokens[start] == "(":
            depth = 1
            j = start + 1
            parts = ["("]
            while j < len(tokens) and depth > 0:
                t = tokens[j]
                parts.append(t)
                if t == "(":
                    depth += 1
                elif t == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                return "".join(parts), j
            return None, start
        return tokens[start], start + 1

    def _collect_arith_rhs(start: int) -> Tuple[Optional[str], int]:
        expr, j = _collect_expr_term(start)
        if expr is None:
            return None, start
        parts = [expr]
        while j + 1 < len(tokens) and tokens[j] in _ARITH_EXPR_OPS_SET:
            op = tokens[j]
            rhs_term, next_j = _collect_expr_term(j + 1)
            if rhs_term is None:
                break
            parts.append(op)
            parts.append(rhs_term)
            j = next_j
        return "".join(parts), j

    while i < len(tokens):
        # Parenthesized arithmetic LHS: (field1-field2) > expr
        if tokens[i] == "(":
            lhs_expr, j = _collect_expr_term(i)
            if (
                lhs_expr is not None
                and j < len(tokens)
                and tokens[j] in _FB_OPS_SET
            ):
                lhs_inner = lhs_expr[1:-1] if lhs_expr.startswith("(") and lhs_expr.endswith(")") else lhs_expr
                if lhs_inner and lhs_inner.upper() not in _FB_BOOL_OPS_U and all(op not in lhs_inner.upper() for op in (" AND ", " OR ")):
                    rhs_expr, end = _collect_arith_rhs(j + 1)
                    if rhs_expr is not None:
                        out.append(f"{lhs_inner}{tokens[j]}{rhs_expr}")
                        i = end
                        continue

        # Check for arithmetic LHS:  term1 arith_op term2 comp_op value
        # e.g.  LastPowerOnTime - LastPowerOffTime > 2592000
        # Merge into a single fused atom so _fb_atom_to_predicate can parse it.
        # _FIELD_RE already includes "-" in its character class, so the fused
        # "term1-term2" string is treated as a valid comparable LHS.
        if (
            i + 4 < len(tokens)
            and _is_comparable_lhs(tokens[i])
            and tokens[i + 1] in _ARITH_OPS_SET
            and _is_comparable_lhs(tokens[i + 2])
            and tokens[i + 3] in _FB_OPS_SET
        ):
            lhs = f"{tokens[i]}{tokens[i + 1]}{tokens[i + 2]}"
            comp_op = tokens[i + 3]
            rhs_expr, end = _collect_arith_rhs(i + 4)
            if rhs_expr is not None and rhs_expr not in _FB_BOOL_OPS_U and rhs_expr not in ("(", ")"):
                out.append(f"{lhs}{comp_op}{rhs_expr}")
                i = end
                continue
            # Fall through if value slot is a bool-op or unbalanced paren

        # Check for comparison operators  (=, !=, >=, etc.)
        if (
            i + 2 < len(tokens)
            and _is_comparable_lhs(tokens[i])
            and tokens[i + 1] in _FB_OPS_SET
        ):
            # Unquote the field name before fusing:  "api.name"="val" → api.name=val
            field = _unquote_field(tokens[i])
            rhs_expr, end = _collect_arith_rhs(i + 2)
            if (
                rhs_expr is not None
                and rhs_expr.upper() not in _FB_BOOL_OPS_U
                and rhs_expr not in ("(", ")")
            ):
                out.append(field + tokens[i + 1] + rhs_expr)
                i = end
            else:
                out.append(tokens[i])
                i += 1

        # Check for text operators  (LIKE, MATCHES, CONTAINS, etc.)
        elif (
            i + 2 < len(tokens)
            and _is_comparable_lhs(tokens[i])
            and tokens[i + 1].upper() in _FB_TEXT_OPS_SET
            and tokens[i + 2] not in ("(", ")")
            and tokens[i + 2].upper() not in _FB_BOOL_OPS_U
        ):
            field = _unquote_field(tokens[i])
            op = tokens[i + 1].upper()
            val_tok = tokens[i + 2]
            out.append(f"{field} {op} {val_tok}")
            i += 3
        else:
            out.append(tokens[i])
            i += 1
    return out


def _fb_insert_implicit_and(tokens: List[str]) -> List[str]:
    def is_atom(t: str) -> bool:
        return t not in ("(", ")") and t.upper() not in _FB_BOOL_OPS

    out: List[str] = []
    for t in tokens:
        if not out:
            out.append(t)
            continue
        prev = out[-1]
        if (is_atom(prev) or prev == ")") and (is_atom(t) or t == "(" or t == "NOT"):
            if prev != "NOT" and t not in ("AND", "OR"):
                out.append("AND")
        out.append(t)
    return out


class _FBParseError(Exception):
    pass


_FB_PRECEDENCE = {"OR": 1, "AND": 2}


def _fb_parse_ast(tokens: List[str]) -> Any:
    i = 0

    def peek() -> Optional[str]:
        return tokens[i] if i < len(tokens) else None

    def consume(expected: Optional[str] = None) -> str:
        nonlocal i
        if i >= len(tokens):
            raise _FBParseError("Unexpected end")
        t = tokens[i]
        if expected is not None and t != expected:
            raise _FBParseError(f"Expected {expected}, got {t}")
        i += 1
        return t

    def parse_primary() -> Any:
        t = peek()
        if t is None:
            raise _FBParseError("Unexpected end")
        if t == "(":
            consume("(")
            node = parse_bp(0)
            if peek() != ")":
                raise _FBParseError("Missing ')'")
            consume(")")
            return node
        if t == "NOT":
            consume("NOT")
            return ("NOT", parse_primary())
        consume()
        return ("ATOM", t)

    def parse_bp(min_bp: int) -> Any:
        left = parse_primary()
        while True:
            op = peek()
            if op not in ("AND", "OR"):
                break
            lbp = _FB_PRECEDENCE[op]
            rbp = lbp + 1
            if lbp < min_bp:
                break
            consume()
            right = parse_bp(rbp)
            left = (op, left, right)
        return left

    node = parse_bp(0)
    if i != len(tokens):
        raise _FBParseError(f"Unconsumed tokens: {tokens[i:]}")
    return node


def _fb_mk_field(name: str) -> Dict[str, Any]:
    return {"type": "field", "value": name}


def _fb_mk_op(raw: str) -> Dict[str, Any]:
    raw_u = (raw or "").upper()
    op_val = _OP_NORMALIZE.get(raw_u, _OP_NORMALIZE.get(raw, raw_u))
    return {"type": "operator", "value": op_val}


def _fb_value_from_raw(v: str) -> Any:
    v = (v or "").strip()

    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        inner = v[1:-1]
        if "*" in inner or "?" in inner:
            return {"type": "value", "subtype": "wildcard", "value": inner}
        if any(x in inner for x in ["\\d", ".", "^", "$", "(", ")", "+", "?"]):
            return {"type": "value", "subtype": "regex_candidate", "value": inner}
        return {"type": "value", "subtype": "string", "value": inner}

    low = v.lower()
    if low == "true":
        return {"type": "value", "subtype": "bool", "value": True}
    if low == "false":
        return {"type": "value", "subtype": "bool", "value": False}
    if low == "null":
        return {"type": "value", "subtype": "null", "value": None}

    if re.fullmatch(r"-?\d+(?:\.\d+)?", v):
        if "." in v:
            return {"type": "value", "subtype": "float", "value": float(v)}
        return {"type": "value", "subtype": "int", "value": int(v)}

    if "*" in v or "?" in v:
        return {"type": "value", "subtype": "wildcard", "value": v}
    if v == "*":
        return {"type": "value", "subtype": "wildcard", "value": "*"}

    return {"type": "value", "subtype": "raw_identifier", "value": v}


def _fb_literal_only_pred(atom: str) -> Dict[str, Any]:
    return {
        "type": "predicate",
        "field": {"type": "field", "value": "_raw"},
        "operator": {"type": "operator", "value": "CONTAINS"},
        "value": _fb_value_from_raw(atom),
    }


def _fb_split_csv_quote_aware(s: str) -> List[str]:
    items: List[str] = []
    buf: List[str] = []
    in_s = False
    in_d = False
    esc = False

    for ch in (s or ""):
        if esc:
            buf.append(ch); esc = False; continue
        if ch == "\\":
            buf.append(ch); esc = True; continue
        if ch == "'" and not in_d:
            in_s = not in_s; buf.append(ch); continue
        if ch == '"' and not in_s:
            in_d = not in_d; buf.append(ch); continue

        if ch == "," and not in_s and not in_d:
            it = "".join(buf).strip()
            if it:
                items.append(it)
            buf = []
        else:
            buf.append(ch)

    it = "".join(buf).strip()
    if it:
        items.append(it)

    if in_s or in_d:
        naive = [x.strip() for x in (s or "").split(",")]
        return [x for x in naive if x]

    return items


def _split_pred_fov(body: str) -> Optional[List[str]]:
    parts: List[str] = []
    buf: List[str] = []
    s = body or ""
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and s[i + 1] in {"|", "\\"}:
            buf.append(s[i + 1])
            i += 2
            continue
        if ch == "|":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    if len(parts) != 3:
        return None
    return [_unescape_pred_fov_delim(p.strip()) for p in parts]


def _fb_atom_to_predicate(atom: str) -> Dict[str, Any]:
    a = (atom or "").strip()
    if not a:
        return _fb_literal_only_pred(a)

    # Standalone match(field, "pattern"|/pattern/)
    m_match = _RE_MATCH_ATOM.match(a)
    if m_match:
        field = m_match.group(1)
        pattern = _strip_regex_literal_delims(m_match.group(2))
        return {
            "type": "predicate",
            "field": _fb_mk_field(field),
            "operator": _fb_mk_op("MATCHES_REGEX"),
            "value": {"type": "value", "subtype": "regex", "value": pattern},
        }

    # Standalone match_regex(field, /pattern/) — SPL2 boolean usage without = true/false
    m_mr = _RE_MATCH_REGEX_ATOM.match(a)
    if m_mr:
        field = m_mr.group(1)
        pattern = m_mr.group(2)
        return {
            "type": "predicate",
            "field": _fb_mk_field(field),
            "operator": _fb_mk_op("MATCHES_REGEX"),
            "value": {"type": "value", "subtype": "regex", "value": pattern},
        }

    m_pf = _PRED_FOV_RE.match(a)
    if m_pf:
        parts = _split_pred_fov(m_pf.group("body"))
        if parts:
            lhs, op, rhs = parts
            if op in {"MATCHES_REGEX", "NOT_MATCHES_REGEX"}:
                rhs_val = {"type": "value", "subtype": "regex", "value": rhs}
            else:
                rhs_val = _fb_value_from_raw(rhs)
            return {
                "type": "predicate",
                "field": _fb_mk_field(lhs),
                "operator": _fb_mk_op(op),
                "value": rhs_val,
            }

    m_in = _IN_RE.match(a)
    if m_in:
        field = m_in.group("field")
        body = m_in.group("body")
        vals_raw = _fb_split_csv_quote_aware(body)
        if len(vals_raw) == 1 and re.search(r"\s", vals_raw[0]):
            vals_raw = [x for x in re.split(r"\s+", vals_raw[0].strip()) if x]
        vals = [_fb_value_from_raw(x) for x in vals_raw]

        return {
            "type": "predicate",
            "field": _fb_mk_field(field),
            "operator": {"type": "operator", "value": "IN"},
            "value": vals,
        }

    # field op value: scan for first op outside quotes
    in_s = in_d = esc = False
    op_pos = None
    op_txt = None
    OPS = ["!=", ">=", "<=", "==", "=", ">", "<"]
    i = 0
    while i < len(a):
        ch = a[i]
        if esc:
            esc = False; i += 1; continue
        if ch == "\\":
            esc = True; i += 1; continue
        if ch == "'" and not in_d:
            in_s = not in_s; i += 1; continue
        if ch == '"' and not in_s:
            in_d = not in_d; i += 1; continue
        if not in_s and not in_d:
            for op in OPS:
                if a.startswith(op, i):
                    op_pos = i
                    op_txt = op
                    break
            if op_pos is not None:
                break
        i += 1

    if op_pos is not None and op_txt is not None:
        left = a[:op_pos].strip()
        right = a[op_pos + len(op_txt):].strip()
        if _is_comparable_lhs(left) and right != "":
            return {
                "type": "predicate",
                "field": _fb_mk_field(_unquote_field(left)),
                "operator": _fb_mk_op(op_txt),
                "value": _fb_value_from_raw(right),
            }

    # Text operators: field LIKE/MATCHES/CONTAINS/etc. value
    m_text_op = re.match(
        r'^(?P<field>.+?)\s+(?P<op>LIKE|MATCHES|CONTAINS|STARTSWITH|ENDSWITH)\s+(?P<val>.+)$',
        a, re.IGNORECASE,
    )
    if m_text_op:
        left = m_text_op.group("field").strip()
        op = m_text_op.group("op").upper()
        right = m_text_op.group("val").strip()
        if _is_comparable_lhs(left) and right:
            return {
                "type": "predicate",
                "field": _fb_mk_field(_unquote_field(left)),
                "operator": _fb_mk_op(op),
                "value": _fb_value_from_raw(right),
            }

    return _fb_literal_only_pred(a)


def _fb_ast_to_ir(ast: Any) -> Dict[str, Any]:
    kind = ast[0]
    if kind == "ATOM":
        return _fb_atom_to_predicate(ast[1])
    if kind == "NOT":
        return {"type": "expr", "op": "NOT", "children": [_fb_ast_to_ir(ast[1])]}
    if kind in ("AND", "OR"):
        op = kind
        children: List[Dict[str, Any]] = []

        def collect(n):
            if n[0] == op:
                collect(n[1])
                collect(n[2])
            else:
                children.append(_fb_ast_to_ir(n))

        collect(ast)
        if len(children) == 1:
            return children[0]
        return {"type": "expr", "op": op, "children": children}

    raise ValueError(f"Unknown node: {ast}")


def _fb_balance_parens(tokens: List[str]) -> List[str]:
    opens = sum(1 for t in tokens if t == "(")
    closes = sum(1 for t in tokens if t == ")")
    if opens == closes:
        return tokens

    out = list(tokens)
    if opens > closes:
        out.extend([")"] * (opens - closes))
        return out

    extra = closes - opens
    i = len(out) - 1
    while i >= 0 and extra > 0:
        if out[i] == ")":
            out.pop(i)
            extra -= 1
        i -= 1
    return out


def parse_boolean_expr_fallback(expr: str) -> Optional[Dict[str, Any]]:
    """
    Public entry point.
    Returns:
      - dict IR on success
      - None on failure / non-boolean-ish inputs
    """
    expr = (expr or "").strip()
    if not expr:
        return None

    if not BOOL_CUE_RE.search(expr):
        return None

    try:
        expr_fb = strip_subsearches(expr)           # remove [...] subsearch blocks
        expr_fb = strip_trailing_spl_commands(expr_fb)  # truncate at leaked SPL commands
        expr_fb = collapse_where_atoms(expr_fb)
        toks = _fb_tok_quote_aware(expr_fb)
        toks = _fb_split_glued_bool_ops(toks)       # split atoms like PRED_FOV(...)OR
        toks = [t for t in toks if t != "__SUBSEARCH__"]  # 5.1: drop placeholder tokens
        toks = _fb_split_trailing_ops(toks)       # split  "field="  → ["field", "="]
        toks = _fb_split_leading_ops(toks)        # 5.6a: split "!=value" → ["!=", "value"]
        toks = _fb_merge_split_bang_eq(toks)      # 5.6b: merge ["!", "="] → ["!="]
        toks = _fb_strip_as_aliases(toks)         # 5.4: drop "field AS alias" triplets
        toks = _fb_merge_field_op_value(toks)     # re-fuse split  field op value  triplets
        toks = _fb_merge_macro_invocations(toks)
        toks = _fb_merge_in_lists(toks)
        toks = _fb_strip_trailing_field_list(toks)  # 5.3: drop leaked output field lists
        toks = _fb_balance_parens(toks)
        toks = _fb_insert_implicit_and(toks)
        ast = _fb_parse_ast(toks)
        return _fb_ast_to_ir(ast)
    except Exception:
        return None
