#!/usr/bin/env python3
"""
build_lineage_spl_per_rule.py
==============================
Stage 4: Generate one LLM-ready JSON file per detection rule, containing the
full commit history with extracted/converted SPL for each version.

Supports --repo-type sigma|ssc.  The extraction logic is repo-specific:

  SSC  — reads YAML/JSON rule files via `git show`; extracts the `search:` field
         (or spec_version-2 nested schemas when `search:` is absent).  A disk
         fallback under cfg.repo_root/missing_spl_versions/ is tried whenever
         `git show` returns empty content.

  Sigma — reads YAML rule files via `git show`; extracts the raw `detection:`
          and `logsource:` YAML blocks; converts them to SPL by calling the
          `sigma convert --target splunk` CLI tool.

Unified per-commit output schema
---------------------------------
  commit_hash        : git commit SHA
  commit_date        : ISO-8601 commit date
  commit_subject     : first line of commit message
  commit_body        : full commit message body
  path_used          : repo-relative file path at this commit
  rule_id            : UUID extracted from the file content
  parse_success      : bool — file was retrieved and parsed
  file_source        : "git" | "disk"  (SSC: disk fallback; Sigma: always "git")
  doc_format         : "yaml" | "json"  (Sigma: always "yaml")
  detection_block    : Sigma — raw YAML detection block
                       SSC   — raw SPL search string (same as spl)
  logsource_block    : Sigma — raw YAML logsource block
                       SSC   — data_source string (or null)
  spl                : final SPL query (Sigma: sigma-converted; SSC: native search field)
  spl_success        : bool — SPL is present and valid
  spl_source         : "sigma_convert" | "native"
  search_name        : SSC: rule display name; Sigma: null
  search_description : SSC: description; Sigma: null
  spec_version       : SSC: spec version integer; Sigma: null
  error              : error string if parse_success=false, else absent

Top-level per-rule file schema
--------------------------------
  rule_canonical  : canonical_name from the lineage entry
  lineage_id      : lineage_id from the lineage entry
  repo            : "sigma" | "ssc"
  rule_metadata   : {all_ids, all_paths, first_commit_date, last_commit_date,
                     commit_count, exists_in_head, deleted_in_commit, deleted_date}
  commits         : list of per-commit dicts (schema above)

Outputs
--------
  data_prep/rule_lineages_{repo}/{rule_stem}.json   — one file per lineage entry
  build_data/build_lineage_spl_per_rule_{repo}.log  — log file
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Set, Tuple

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib.config import RepoConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run_git(repo_root: Path, args: list[str]) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo_root)] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if r.returncode != 0:
        logger.warning("git %s failed: %s", " ".join(args[:4]), r.stderr.strip()[:200])
    return r.stdout


def _get_commit_body(repo_root: Path, chash: str) -> str:
    return _run_git(repo_root, ["show", "-s", "--format=%B", chash]).strip()


def _get_file_content(repo_root: Path, chash: str, path: str) -> str:
    return _run_git(repo_root, ["show", f"{chash}:{path}"])


# ---------------------------------------------------------------------------
# Shared selection helpers
# ---------------------------------------------------------------------------

def load_canonical_name_list(list_file: str) -> Set[str]:
    """
    Load canonical_name values from a newline-delimited file.
    Supports line comments (#) and block comments (/* ... */).
    """
    p = Path(list_file)
    if not p.exists():
        raise FileNotFoundError(f"List file not found: {p}")
    items: Set[str] = set()
    in_block = False
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("/*"):
            in_block = True
            continue
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if line.startswith("#"):
            continue
        items.add(line)
    return items


def filter_lineages(lineages: list, canonical_names: Set[str]) -> list:
    return [e for e in lineages if e.get("canonical_name") in canonical_names]


# ---------------------------------------------------------------------------
# SSC extractor
# ---------------------------------------------------------------------------

class SSCExtractor:
    """
    Extracts SPL from SSC YAML/JSON rule files.

    Handles three file schemas:
      - Standard (spec_version 1/3+): top-level `search:` key
      - spec_version-2 detection:    detect.splunk.correlation_rule
      - spec_version-2 baseline:     baseline.splunk.search
      - spec_version-2 investigation: investigate.splunk

    Falls back to disk copies under `missing_spl_dir` when `git show` returns
    empty content (happens for very old or force-pushed commits).
    """

    def __init__(self, repo_root: Path, missing_spl_dir: Optional[Path] = None) -> None:
        self.repo_root = repo_root
        self.missing_spl_dir = missing_spl_dir or (repo_root / "missing_spl_versions")

    # ── disk fallback ────────────────────────────────────────────────────────

    def _get_from_disk(self, canonical_name: str, chash: str, path: str) -> Optional[str]:
        """
        Look up a pre-saved file from missing_spl_versions/.
        Layout: missing_spl_versions/{canonical_name}/version_{NNNN}_{hash12}/{path}
        """
        rule_dir = self.missing_spl_dir / canonical_name
        if not rule_dir.is_dir():
            return None
        short_hash = chash[:12]
        for vdir in sorted(rule_dir.iterdir()):
            if vdir.is_dir() and vdir.name.endswith(f"_{short_hash}"):
                candidate = vdir / path
                if candidate.exists():
                    try:
                        return candidate.read_text(encoding="utf-8")
                    except Exception as e:
                        logger.warning("disk fallback read error %s: %s", candidate, e)
        return None

    def _get_file(self, chash: str, path: str, canonical_name: str) -> Tuple[str, str]:
        """Return (file_text, source) where source is 'git' or 'disk'."""
        text = _get_file_content(self.repo_root, chash, path)
        if text.strip():
            return text, "git"
        disk = self._get_from_disk(canonical_name, chash, path)
        if disk is not None:
            return disk, "disk"
        return text, "git"  # empty, nothing on disk either

    # ── parsing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_doc(text: str, path: str) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
        """
        Return (obj, fmt, err).  Prefers JSON for .json paths, YAML otherwise,
        with a cross-format fallback in each case.
        """
        t = text.strip()
        if not t:
            return None, None, "empty_text"

        prefer_json = path.lower().endswith(".json")

        def try_json() -> Tuple[Optional[dict], Optional[str]]:
            try:
                obj = json.loads(t)
                return (obj, None) if isinstance(obj, dict) else (None, "top-level not a dict")
            except Exception as e:
                return None, f"JSON parse error: {e}"

        def try_yaml() -> Tuple[Optional[dict], Optional[str]]:
            try:
                obj = yaml.safe_load(t)
                return (obj, None) if isinstance(obj, dict) else (None, "top-level not a dict")
            except Exception as e:
                return None, f"YAML parse error: {e}"

        if prefer_json:
            obj, err = try_json()
            if obj is not None:
                return obj, "json", None
            obj2, err2 = try_yaml()
            if obj2 is not None:
                return obj2, "yaml", f"preferred_json_failed: {err}"
            return None, None, f"{err} | {err2}"
        else:
            obj, err = try_yaml()
            if obj is not None:
                return obj, "yaml", None
            obj2, err2 = try_json()
            if obj2 is not None:
                return obj2, "json", f"preferred_yaml_failed: {err}"
            return None, None, f"{err} | {err2}"

    # ── field extraction ─────────────────────────────────────────────────────

    @staticmethod
    def _as_text(x: Any) -> Optional[str]:
        if x is None:
            return None
        if isinstance(x, str):
            return x
        try:
            return json.dumps(x, ensure_ascii=False)
        except Exception:
            return str(x)

    def _extract_v2_search(self, obj: dict) -> Optional[str]:
        """Extract a search string from spec_version-2 nested schemas."""
        # 1. detect.splunk.correlation_rule
        detect_splunk = (obj.get("detect") or {}).get("splunk") or {}
        if detect_splunk:
            cr = detect_splunk.get("correlation_rule")
            if isinstance(cr, dict) and "search" in cr:
                return self._as_text(cr["search"])
            if isinstance(cr, list):
                for item in cr:
                    if isinstance(item, dict) and "search" in item:
                        return self._as_text(item["search"])
        # 2. baseline.splunk.search
        baseline_splunk = (obj.get("baseline") or {}).get("splunk") or {}
        if baseline_splunk and "search" in baseline_splunk:
            return self._as_text(baseline_splunk["search"])
        # 3. investigate.splunk
        investigate_splunk = (obj.get("investigate") or {}).get("splunk")
        if investigate_splunk:
            if isinstance(investigate_splunk, dict) and "search" in investigate_splunk:
                return self._as_text(investigate_splunk["search"])
            if isinstance(investigate_splunk, list):
                for item in investigate_splunk:
                    if isinstance(item, dict) and "search" in item:
                        return self._as_text(item["search"])
        return None

    def _extract_fields(self, obj: dict) -> dict:
        md = obj.get("metadata") or {}
        # SPL: top-level search, then spec_version-2 fallback
        spl = self._as_text(obj.get("search")) or self._extract_v2_search(obj)
        # logsource / data_source
        logsource = self._as_text(obj.get("data_source") or md.get("data_source"))
        if logsource is None:
            dm = obj.get("data_metadata") or {}
            logsource = self._as_text(dm.get("data_source"))
        return {
            "spl": spl,
            "logsource_block": logsource,
            "search_name": self._as_text(obj.get("search_name") or obj.get("name")),
            "search_description": self._as_text(
                obj.get("search_description") or obj.get("description")
            ),
            "spec_version": obj.get("spec_version"),
        }

    # ── public interface ─────────────────────────────────────────────────────

    def extract_commit(
        self, chash: str, path: str, canonical_name: str, cdate: str, subj: str
    ) -> dict:
        """Return a unified commit dict for one SSC commit."""
        entry: dict = {
            "commit_hash": chash,
            "commit_date": cdate,
            "commit_subject": subj,
            "commit_body": _get_commit_body(self.repo_root, chash),
            "path_used": path,
            "rule_id": None,
            "parse_success": False,
            "file_source": "git",
            "doc_format": None,
            "detection_block": None,
            "logsource_block": None,
            "spl": None,
            "spl_success": False,
            "spl_source": "native",
            "search_name": None,
            "search_description": None,
            "spec_version": None,
        }
        try:
            text, source = self._get_file(chash, path, canonical_name)
            entry["file_source"] = source
            if source == "disk":
                logger.info("  disk fallback used: %s@%s %s", canonical_name, chash[:12], path)

            obj, fmt, perr = self._parse_doc(text, path)
            if obj is None:
                entry["error"] = perr
                return entry

            entry["doc_format"] = fmt
            entry["parse_success"] = True

            # Extract rule_id from the parsed object
            entry["rule_id"] = self._as_text(
                obj.get("id") or (obj.get("metadata") or {}).get("id")
            )

            fields = self._extract_fields(obj)
            entry["spl"] = fields["spl"]
            entry["detection_block"] = fields["spl"]   # same for SSC
            entry["logsource_block"] = fields["logsource_block"]
            entry["spl_success"] = fields["spl"] is not None
            entry["search_name"] = fields["search_name"]
            entry["search_description"] = fields["search_description"]
            entry["spec_version"] = fields["spec_version"]

        except Exception as e:
            entry["error"] = f"exception: {e}"
            logger.error("Error processing %s@%s: %s", canonical_name, chash, e)

        return entry


# ---------------------------------------------------------------------------
# Sigma extractor
# ---------------------------------------------------------------------------

class SigmaExtractor:
    """
    Extracts detection/logsource YAML blocks from Sigma rule files and converts
    them to SPL using the `sigma convert` CLI tool.
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    # ── YAML helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_rule_id(yaml_text: str) -> Optional[str]:
        try:
            data = yaml.safe_load(yaml_text)
            if isinstance(data, dict):
                v = data.get("id")
                if isinstance(v, list) and v:
                    return str(v[0])
                if isinstance(v, (str, int)):
                    return str(v)
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_blocks(yaml_text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract and dedent the `detection:` and `logsource:` YAML blocks.
        Strips the section header line itself; preserves sub-key indentation.
        """
        lines = yaml_text.splitlines()
        det_lines: list[str] = []
        log_lines: list[str] = []
        in_det = in_log = False

        for line in lines:
            if re.match(r"^\s*detection\s*:", line):
                in_det, in_log = True, False
                continue
            elif re.match(r"^\s*logsource\s*:", line):
                in_log, in_det = True, False
                continue
            elif re.match(r"^\S", line):  # new top-level key ends current block
                in_det = in_log = False

            if in_det:
                det_lines.append(line)
            if in_log:
                log_lines.append(line)

        def dedent(block_lines: list[str]) -> Optional[str]:
            if not block_lines:
                return None
            non_empty = [len(l) - len(l.lstrip()) for l in block_lines if l.strip()]
            min_indent = min(non_empty) if non_empty else 0
            return "\n".join(l[min_indent:] for l in block_lines)

        return dedent(det_lines), dedent(log_lines)

    @staticmethod
    def _normalize_condition(detection_block: str) -> str:
        """
        Lowercase boolean keywords (AND/OR/NOT) on `condition:` lines only,
        so we don't accidentally modify string values in rule selections.
        """
        out: list[str] = []
        for line in detection_block.splitlines():
            m = re.match(r"^(\s*condition\s*:\s*)(.*)$", line)
            if not m:
                out.append(line)
                continue
            expr = m.group(2)
            expr = re.sub(r"\bAND\b", "and", expr)
            expr = re.sub(r"\bOR\b",  "or",  expr)
            expr = re.sub(r"\bNOT\b", "not", expr)
            out.append(m.group(1) + expr)
        return "\n".join(out)

    @staticmethod
    def _indent_block(block: str, indent: int = 2) -> str:
        prefix = " " * indent
        return "\n".join(prefix + l if l.strip() else l for l in block.splitlines())

    # ── SPL conversion ────────────────────────────────────────────────────────

    def _convert_to_spl(
        self, detection_block: Optional[str], logsource_block: Optional[str]
    ) -> Tuple[str, bool]:
        if not detection_block:
            return "# no detection block", False

        det = self._normalize_condition(detection_block)
        det = re.sub(r"^\s*detection\s*:\s*", "", det)

        if logsource_block:
            logsource_block = re.sub(r"^\s*logsource\s*:\s*", "", logsource_block)
        if not logsource_block or not logsource_block.strip():
            logsource_block = "product: windows"

        minimal_yaml = (
            "title: temp\n"
            "status: test\n"
            "logsource:\n" + self._indent_block(logsource_block) + "\n"
            "detection:\n" + self._indent_block(det) + "\n"
        )

        p = subprocess.run(
            ["sigma", "convert", "--target", "splunk", "--without-pipeline", "-"],
            input=minimal_yaml,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if p.returncode != 0 or not p.stdout.strip():
            msg = f"# sigma convert failed:\n{p.stderr.strip()}"
            logger.warning("sigma convert failed for block:\n%s", p.stderr.strip()[:200])
            return msg, False
        return p.stdout.strip(), True

    # ── public interface ──────────────────────────────────────────────────────

    def extract_commit(
        self, chash: str, path: str, canonical_name: str, cdate: str, subj: str
    ) -> dict:
        """Return a unified commit dict for one Sigma commit."""
        entry: dict = {
            "commit_hash": chash,
            "commit_date": cdate,
            "commit_subject": subj,
            "commit_body": _get_commit_body(self.repo_root, chash),
            "path_used": path,
            "rule_id": None,
            "parse_success": False,
            "file_source": "git",
            "doc_format": "yaml",
            "detection_block": None,
            "logsource_block": None,
            "spl": None,
            "spl_success": False,
            "spl_source": "sigma_convert",
            "search_name": None,
            "search_description": None,
            "spec_version": None,
        }
        try:
            file_text = _get_file_content(self.repo_root, chash, path)
            entry["rule_id"] = self._extract_rule_id(file_text)
            det_block, log_block = self._extract_blocks(file_text)
            entry["parse_success"] = det_block is not None or file_text.strip() != ""
            entry["detection_block"] = det_block
            entry["logsource_block"] = log_block

            spl, ok = self._convert_to_spl(det_block, log_block)
            entry["spl"] = spl
            entry["spl_success"] = ok

        except Exception as e:
            entry["error"] = f"exception: {e}"
            logger.error("Error processing %s@%s: %s", canonical_name, chash, e)

        return entry


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_per_rule(
    repo_type: str,
    *,
    input_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    num_rules: Optional[int] = None,
    canonical_list_file: Optional[str] = None,
) -> None:
    cfg = RepoConfig(repo_type)

    # ── Resolve paths ────────────────────────────────────────────────────────
    if input_path is None:
        # Prefer the final merged lineage; fall back to semantic output
        final = cfg.path("lineage_final")
        input_path = final if final.exists() else cfg.path("stage3_out")
    if output_dir is None:
        output_dir = cfg.rule_lineages_dir

    log_path = cfg.path("spl_log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Logging ──────────────────────────────────────────────────────────────
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers
               if not isinstance(h, logging.FileHandler)):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(sh)

    logger.info("repo_type   : %s", repo_type)
    logger.info("input       : %s", input_path)
    logger.info("output_dir  : %s", output_dir)

    # ── Load lineage ─────────────────────────────────────────────────────────
    if not input_path.exists():
        logger.error("Input lineage file not found: %s", input_path)
        sys.exit(1)

    lineages: list[dict] = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(lineages, list):
        raise ValueError(f"{input_path} must be a JSON list")
    logger.info("Loaded %d lineage entries.", len(lineages))

    # ── Optional filtering ────────────────────────────────────────────────────
    if canonical_list_file:
        wanted = load_canonical_name_list(canonical_list_file)
        before = len(lineages)
        lineages = filter_lineages(lineages, wanted)
        found = {e.get("canonical_name") for e in lineages}
        missing = sorted(wanted - found)
        logger.info("Selection: %d/%d rules (file: %s)", len(lineages), before, canonical_list_file)
        for m in missing[:50]:
            logger.warning("  missing canonical_name: %s", m)
        if len(missing) > 50:
            logger.warning("  ... (%d more missing entries)", len(missing) - 50)

    if num_rules:
        lineages = lineages[:num_rules]
        logger.info("Testing mode: processing first %d rules.", num_rules)

    logger.info("Total rules to process: %d", len(lineages))

    # ── Build extractor ───────────────────────────────────────────────────────
    if repo_type == "ssc":
        extractor: SSCExtractor | SigmaExtractor = SSCExtractor(cfg.repo_root)
    else:
        extractor = SigmaExtractor(cfg.repo_root)

    # ── Process each rule ────────────────────────────────────────────────────
    ok_count = err_count = skip_count = 0

    for idx, entry in enumerate(lineages, start=1):
        rule_canonical = entry.get("canonical_name")
        if not rule_canonical:
            logger.warning("[%d] Skipping entry missing canonical_name: %s",
                           idx, entry.get("lineage_id"))
            skip_count += 1
            continue

        logger.info("[%d/%d] %s", idx, len(lineages), rule_canonical)

        rule_obj: dict = {
            "rule_canonical": rule_canonical,
            "lineage_id": entry.get("lineage_id"),
            "repo": repo_type,
            "rule_metadata": {
                "all_ids": entry.get("all_ids", []),
                "all_paths": entry.get("all_paths", []),
                "first_commit_date": entry.get("first_commit_date"),
                "last_commit_date": entry.get("last_commit_date"),
                "commit_count": entry.get("commit_count"),
                "exists_in_head": entry.get("exists_in_head"),
                "deleted_in_commit": entry.get("deleted_in_commit"),
                "deleted_date": entry.get("deleted_date"),
            },
            "commits": [],
        }

        for c in entry.get("commits") or []:
            chash = c.get("hash")
            cdate = c.get("date") or c.get("author_date")
            path  = c.get("path_used")
            subj  = c.get("subject", "")

            if not chash or not path:
                logger.warning("Skipping malformed commit in %s: %s", rule_canonical, c)
                continue

            commit_entry = extractor.extract_commit(
                chash, path, rule_canonical, cdate, subj
            )
            # Copy the lineage-level rule_id as fallback when the file-level id is absent
            if not commit_entry.get("rule_id") and c.get("id"):
                commit_entry["rule_id"] = c["id"]

            rule_obj["commits"].append(commit_entry)

        # ── Write output file ────────────────────────────────────────────────
        stem = Path(rule_canonical).name
        stem = re.sub(r"\.(yml|yaml|json)$", ".json", stem, flags=re.IGNORECASE)
        out_path = output_dir / stem

        if out_path.exists():
            logger.info("Skipping existing file: %s", out_path)
            skip_count += 1
            continue

        try:
            out_path.write_text(
                json.dumps(rule_obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            ok_count += 1
        except Exception as e:
            logger.error("Failed to write %s: %s", out_path, e)
            err_count += 1

    logger.info(
        "Done. written=%d  skipped=%d  write_errors=%d  output_dir=%s",
        ok_count, skip_count, err_count, output_dir,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Generate one LLM-ready JSON file per detection rule with full "
            "commit history and extracted/converted SPL."
        )
    )
    ap.add_argument(
        "--repo-type", required=True, choices=["sigma", "ssc"],
        help="Which repo to process (sigma or ssc).",
    )
    ap.add_argument(
        "-n", "--num-rules", type=int, default=None,
        help="Limit to first N rules (useful for testing).",
    )
    ap.add_argument(
        "--canonical-list-file", type=str, default=None,
        help="Path to newline-delimited file of canonical_name values to process.",
    )
    ap.add_argument(
        "--input", type=str, default=None,
        help=(
            "Override input lineage JSON path "
            "(default: lineage_metadata_final_{repo}.json, "
            "falling back to lineage_metadata_{repo}.json)."
        ),
    )
    ap.add_argument(
        "--output-dir", type=str, default=None,
        help="Override output directory (default: data_prep/rule_lineages_{repo}/).",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    build_per_rule(
        args.repo_type,
        input_path=Path(args.input) if args.input else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        num_rules=args.num_rules,
        canonical_list_file=args.canonical_list_file,
    )


if __name__ == "__main__":
    main()
