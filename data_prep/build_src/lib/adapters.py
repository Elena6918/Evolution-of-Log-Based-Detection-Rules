#!/usr/bin/env python3
"""
Per-repo adapters for rule-file detection and ID extraction.

  SigmaAdapter  -- YAML only; top-level 'id' field
  SSCAdapter    -- YAML + JSON; 'id' (YAML) or 'search_id'/'id' (JSON)

Usage:
    from lib.config import RepoConfig
    from lib.adapters import get_adapter

    cfg = RepoConfig("sigma")
    adapter = get_adapter(cfg)
    adapter.is_rule_file("rules/windows/proc_creation_win_foo.yml")  # True
    adapter.extract_id("rules/windows/foo.yml", yaml_text)           # "abc-123-..."
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

import yaml

if TYPE_CHECKING:
    from lib.config import RepoConfig


class RepoAdapter:
    def __init__(self, cfg: "RepoConfig") -> None:
        self.cfg = cfg

    def is_rule_file(self, path: str) -> bool:
        raise NotImplementedError

    def extract_id(self, path: str, text: str) -> Optional[str]:
        raise NotImplementedError


class SigmaAdapter(RepoAdapter):
    """
    Recognises YAML files under rules/ and legacy flat roots.
    Extracts the top-level 'id' field (str, int, or list[0]).
    """

    def is_rule_file(self, path: str) -> bool:
        if not path.endswith((".yml", ".yaml")):
            return False
        # Any directory starting with "rules" (rules/, rules-emerging-threats/, …)
        top = path.split("/", 1)[0]
        if top.startswith("rules"):
            return True
        # Legacy flat roots (pre-rules/ era)
        return path.startswith(self.cfg.rule_dir_prefixes)

    def extract_id(self, path: str, text: str) -> Optional[str]:
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                v = data.get("id")
                if isinstance(v, list) and v:
                    return str(v[0])
                if isinstance(v, (str, int)):
                    return str(v)
        except Exception:
            pass
        return None


class SSCAdapter(RepoAdapter):
    """
    Recognises YAML and JSON files under known Splunk Security Content roots.
    For JSON: prefers 'search_id', falls back to 'id'.
    For YAML: uses 'id' (same as Sigma).
    """

    def is_rule_file(self, path: str) -> bool:
        if not path.endswith((".yml", ".yaml", ".json")):
            return False
        for prefix in self.cfg.rule_dir_prefixes:
            if path.startswith(prefix):
                return True
        return False

    def extract_id(self, path: str, text: str) -> Optional[str]:
        try:
            if path.endswith(".json"):
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for k in ("search_id", "id"):
                        v = obj.get(k)
                        if isinstance(v, (str, int)):
                            return str(v)
                return None
            if path.endswith((".yml", ".yaml")):
                obj = yaml.safe_load(text)
                if isinstance(obj, dict):
                    v = obj.get("id")
                    if isinstance(v, list) and v:
                        return str(v[0])
                    if isinstance(v, (str, int)):
                        return str(v)
        except Exception:
            pass
        return None


def get_adapter(cfg: "RepoConfig") -> RepoAdapter:
    if cfg.repo_type == "sigma":
        return SigmaAdapter(cfg)
    if cfg.repo_type == "ssc":
        return SSCAdapter(cfg)
    raise ValueError(f"No adapter registered for repo_type: {cfg.repo_type!r}")
