"""
lib/spl_normalize.py
=====================
SPL normalization, validation, and SSC macro expansion.

Shared utilities (both repos):
  preclean_spl(s)           -- strip fenced blocks, normalize whitespace
  invalid_spl_reason(s)     -- detect structural invalidity (SSC-oriented)
  is_valid_sigma_spl(s)     -- True if sigma convert succeeded

SSC-specific:
  MacroCache                -- per-commit macro definition cache (disk + LRU memory)
  expand_macros_ssc(s, m)   -- two-pass macro expansion + MACRO=... rewrite
  normalize_spl_ssc(s, m)   -- preclean → expand → validate; returns (spl, reason, stats)

Sigma-specific:
  normalize_spl_sigma(s)    -- validate conversion success + preclean; returns (spl, reason)
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Shared regex constants
# ---------------------------------------------------------------------------

_TRIPLE_TICK_RX       = re.compile(r"```.*?```", re.DOTALL)
_INVOC_RX             = re.compile(r"`\s*(?P<name>[A-Za-z0-9_.:-]+)\s*(?:\((?P<args>[^`]*)\))?\s*`")
_PLACEHOLDER_RX       = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\$")
_ARG_SPLIT_RX         = re.compile(r',(?=(?:[^"]*"[^"]*")*[^"]*$)')
_STRING_LIT_RX        = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_DANGLING_BOOL_PAREN  = re.compile(r"(?i)\b(?:and|or)\s*\)")
_DANGLING_BOOL_PIPE   = re.compile(r"(?i)\b(?:and|or)\s*\|")
_DANGLING_BOOL_END    = re.compile(r"(?i)\b(?:and|or)\s*$")
_PLACEHOLDER_MARKER   = re.compile(r"(?i)\b(?:UPDATE_SPL|TODO|TBD|PLACEHOLDER)\b")

# Sigma convert failure prefixes (set by Stage 4)
_SIGMA_FAIL_PREFIXES = (
    "# sigma convert failed:",
    "# no detection block",
    "# error:",
)


# ---------------------------------------------------------------------------
# Shared: pre-cleaning and validation
# ---------------------------------------------------------------------------

def preclean_spl(s: str) -> str:
    """
    Remove non-SPL annotation blocks and normalize whitespace.
    - Strips ``` fenced blocks
    - Removes --finding_report-- markers (SSC-specific artifact)
    - Collapses all whitespace (tabs, newlines) into single spaces
    """
    if not s:
        return s
    out = _TRIPLE_TICK_RX.sub(" ", s)
    out = out.replace("--finding_report--", "")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"[\t\n]+", " ", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _strip_string_literals(s: str) -> str:
    """Replace quoted strings with '' to avoid false positives in pattern checks."""
    return _STRING_LIT_RX.sub('""', s)


def invalid_spl_reason(spl: str) -> Optional[str]:
    """
    Check for structural SPL invalidity after macro expansion.
    Primarily relevant for SSC (Sigma validity is handled by is_valid_sigma_spl).

    Returns a short reason string, or None if the SPL appears valid.
      leftover_backtick          -- unresolved macro invocation remains
      dangling_bool_before_paren -- "AND )" or "OR )" pattern
      dangling_bool_before_pipe  -- "AND |" or "OR |" pattern
      dangling_bool_at_end       -- query ends with AND/OR
      placeholder_marker         -- literal TODO / TBD / PLACEHOLDER token
    """
    if not spl:
        return None
    code = _strip_string_literals(spl)
    if "`" in code:
        return "leftover_backtick"
    if _DANGLING_BOOL_PAREN.search(code):
        return "dangling_bool_before_paren"
    if _DANGLING_BOOL_PIPE.search(code):
        return "dangling_bool_before_pipe"
    if _DANGLING_BOOL_END.search(code):
        return "dangling_bool_at_end"
    if _PLACEHOLDER_MARKER.search(code):
        return "placeholder_marker"
    return None


def is_valid_sigma_spl(spl: Optional[str]) -> bool:
    """
    True if spl is a real converted SPL string (not a sigma convert failure message
    written by Stage 4).
    """
    if not spl or not spl.strip():
        return False
    stripped = spl.lstrip()
    return not any(stripped.startswith(p) for p in _SIGMA_FAIL_PREFIXES)


# ---------------------------------------------------------------------------
# SSC: macro argument binding
# ---------------------------------------------------------------------------

def _split_args(argstr: Optional[str]) -> list[str]:
    """Split macro invocation arguments, respecting double-quoted strings."""
    if not argstr or not argstr.strip():
        return []
    parts = _ARG_SPLIT_RX.split(argstr.strip())
    return [p.strip() for p in parts if p.strip()]


def _normalize_definition(defn: str) -> str:
    """Collapse all whitespace in a macro definition to single spaces."""
    return " ".join(str(defn).split())


def _bind_args(defn: str, args: list[str]) -> tuple[str, bool]:
    """
    Substitute invocation args into $placeholder$ slots in a macro definition.

    Returns (expanded_text, success).  success=False when the number of supplied
    args does not match the number of distinct placeholders — in that case the
    original definition is returned unchanged for caller to handle as a fallback.
    """
    defn = _normalize_definition(defn)
    placeholders: list[str] = []
    seen: set[str] = set()
    for m in _PLACEHOLDER_RX.finditer(defn):
        key = m.group(1)
        if key not in seen:
            placeholders.append(key)
            seen.add(key)

    if not placeholders:
        return defn, True  # concrete definition; args are ignored

    if len(args) != len(placeholders):
        return defn, False  # mismatch; caller decides how to handle

    out = defn
    for key, val in zip(placeholders, args):
        out = out.replace(f"${key}$", val)
    return out, True


# ---------------------------------------------------------------------------
# SSC: MacroCache — per-commit disk-backed + in-memory LRU
# ---------------------------------------------------------------------------

class MacroCache:
    """
    Per-commit cache of SSC macro definitions.

    Cache layout (matches security_content/tree_analysis/macro_cache.py):
      cache_dir/                           (data_prep/macro_data/macro_cache_by_commit/)
        {commit_hash}.json                 -- flat {macro_name: definition_str} map
        macro_cache_index.json             -- {commit_hash: {path, n_macros}} index

    On first access for a commit, macros are read from the git tree under the
    'macros/' directory and saved to `cache_dir/{commit_hash}.json`.  The index
    file is kept in sync so it's easy to audit what's been fetched.

    Subsequent accesses (same process or across runs) use the disk copy.
    An in-memory LRU keeps recently accessed commits warm to avoid repeated
    disk reads during a single invocation.

    Missing macros are NEVER a reason to drop an SPL entry — the expansion
    pass rewrites any unresolved `name(...)` invocation to `MACRO=name(...)`
    so the SPL remains valid and no backtick is left behind.
    """

    _MACRO_DIRS = ("macros",)          # directories to search inside the SSC repo
    _INDEX_FILE = "macro_cache_index.json"

    def __init__(
        self,
        repo_root: Path,
        cache_dir: Path,
        lru_size: int = 512,
    ) -> None:
        self.repo_root = repo_root
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._mem: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._lru_size = lru_size
        # Load or initialise the index
        self._index: dict[str, dict] = self._load_index()

    # ── public interface ──────────────────────────────────────────────────────

    def get(self, commit_hash: str) -> dict[str, str]:
        """Return {macro_name: definition} for the commit, from cache or git."""
        if not commit_hash:
            return {}

        # Memory hit
        if commit_hash in self._mem:
            self._mem.move_to_end(commit_hash)
            return self._mem[commit_hash]

        # Disk hit (check index first for speed, then fall back to file existence)
        disk = self._cache_path(commit_hash)
        if commit_hash in self._index or disk.exists():
            try:
                macros: dict[str, str] = json.loads(disk.read_text(encoding="utf-8"))
                self._put_mem(commit_hash, macros)
                return macros
            except Exception:
                pass  # corrupt cache; rebuild below

        # Build from git, persist to disk + index
        macros = self._load_from_git(commit_hash)
        try:
            disk.write_text(json.dumps(macros, ensure_ascii=False), encoding="utf-8")
            self._index[commit_hash] = {
                "path": disk.name,
                "n_macros": len(macros),
            }
            self._save_index()
        except Exception:
            pass  # non-fatal; just won't persist this run
        self._put_mem(commit_hash, macros)
        return macros

    def warm(self, commit_hashes: list[str]) -> int:
        """
        Pre-populate the cache for a list of commits.
        Returns the number of commits that required a git fetch (cache miss).
        """
        misses = 0
        for h in commit_hashes:
            if h and h not in self._mem and h not in self._index and not self._cache_path(h).exists():
                misses += 1
            if h:
                self.get(h)
        return misses

    # ── internals ────────────────────────────────────────────────────────────

    def _cache_path(self, commit_hash: str) -> Path:
        return self.cache_dir / f"{commit_hash}.json"

    @property
    def _index_path(self) -> Path:
        return self.cache_dir / self._INDEX_FILE

    def _load_index(self) -> dict[str, dict]:
        """Load the index file, or return an empty dict if it doesn't exist."""
        try:
            if self._index_path.exists():
                return json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_index(self) -> None:
        """Persist the index to disk (best-effort)."""
        try:
            self._index_path.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _put_mem(self, commit_hash: str, macros: dict[str, str]) -> None:
        if commit_hash in self._mem:
            self._mem.move_to_end(commit_hash)
        else:
            self._mem[commit_hash] = macros
            if len(self._mem) > self._lru_size:
                self._mem.popitem(last=False)

    def _load_from_git(self, commit_hash: str) -> dict[str, str]:
        """Read all macro definitions from the repo tree at commit_hash."""
        import yaml  # lazy import; only needed for SSC

        macros: dict[str, str] = {}
        for macro_dir in self._MACRO_DIRS:
            r = subprocess.run(
                ["git", "-C", str(self.repo_root),
                 "ls-tree", "-r", "--name-only", commit_hash, macro_dir],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if r.returncode != 0 or not r.stdout.strip():
                continue

            for rel_path in r.stdout.splitlines():
                rel_path = rel_path.strip()
                if not rel_path.endswith((".yml", ".yaml")):
                    continue
                sr = subprocess.run(
                    ["git", "-C", str(self.repo_root), "show", f"{commit_hash}:{rel_path}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                if sr.returncode != 0 or not sr.stdout.strip():
                    continue
                try:
                    obj = yaml.safe_load(sr.stdout)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                name = obj.get("name")
                defn = obj.get("definition")
                if name and defn is not None:
                    macros[str(name).strip()] = _normalize_definition(str(defn))

        return macros


# ---------------------------------------------------------------------------
# SSC: macro expansion pipeline
# ---------------------------------------------------------------------------

def expand_macros_ssc(
    spl: str,
    macro_map: dict[str, str],
) -> tuple[str, dict]:
    """
    Two-pass macro expansion for SSC SPL.

    Pass 1: expand known macros inline (substituting $placeholder$ variables).
            On arg-count mismatch, rewrite as MACRO=name(...) immediately.
    Pass 2: rewrite any remaining unresolved `name` or `name(args)` invocations
            as MACRO=name(...) tokens so no backticks remain.

    Returns (expanded_spl, stats) where stats = {expanded, fallback, unresolved}.
    """
    stats = {"expanded": 0, "fallback": 0, "unresolved": 0}

    def _repl_known(m: re.Match) -> str:
        name = m.group("name")
        args = _split_args(m.group("args"))
        if name in macro_map:
            expanded, ok = _bind_args(macro_map[name], args)
            if ok:
                stats["expanded"] += 1
                return expanded
            else:
                stats["fallback"] += 1
                return f"MACRO={name}({', '.join(args)})" if args else f"MACRO={name}"
        return m.group(0)  # leave for pass 2

    out = _INVOC_RX.sub(_repl_known, spl)

    def _repl_unresolved(m: re.Match) -> str:
        name = m.group("name")
        args = _split_args(m.group("args"))
        stats["unresolved"] += 1
        return f"MACRO={name}({', '.join(args)})" if args else f"MACRO={name}"

    out = _INVOC_RX.sub(_repl_unresolved, out)

    # Normalize pipe spacing and collapse extra whitespace
    out = re.sub(r"\s+\|\s+", " | ", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out, stats


# ---------------------------------------------------------------------------
# Top-level normalization entry points
# ---------------------------------------------------------------------------

def normalize_spl_ssc(
    spl: Optional[str],
    macro_map: dict[str, str],
) -> tuple[Optional[str], Optional[str], dict]:
    """
    Full SSC normalization pipeline: preclean → macro expand → validate.

    Returns (normalized_spl, drop_reason, stats).
    drop_reason is None if the SPL passes all checks.
    stats = {expanded, fallback, unresolved} from macro expansion.
    """
    if not spl or not spl.strip():
        return spl, "null_spl", {}

    cleaned = preclean_spl(spl)
    expanded, stats = expand_macros_ssc(cleaned, macro_map)
    reason = invalid_spl_reason(expanded)
    if reason:
        return expanded, f"invalid_spl_{reason}", stats
    return expanded, None, stats


def normalize_spl_sigma(spl: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Sigma normalization: validate that sigma convert succeeded, then preclean.

    Returns (normalized_spl, drop_reason).
    drop_reason is None if the SPL is valid.
    """
    if not spl or not spl.strip():
        return spl, "null_spl"
    if not is_valid_sigma_spl(spl):
        return spl, "sigma_convert_failed"
    return preclean_spl(spl), None
