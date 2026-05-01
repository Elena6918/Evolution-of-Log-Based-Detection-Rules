#!/usr/bin/env python3
"""
Centralized path and repository configuration for the unified data_prep pipeline.

All paths are derived from this file's location so the pipeline works regardless
of the caller's working directory.

Usage:
    from lib.config import RepoConfig
    cfg = RepoConfig("sigma")   # or "ssc"
    cfg.repo_root               -> Path  (rules_repo/sigma or rules_repo/splunk_sc)
    cfg.build_data              -> Path  (data_prep/build_data/)
    cfg.path("stage1_out")      -> Path  (build_data/lineage_metadata_raw_sigma.json)
"""

from pathlib import Path
from typing import Optional

# data_prep/build_src/lib/config.py  ->  ../../../  ->  Evolution-of-Detection-Rules/
_EVOLUTION_ROOT = Path(__file__).resolve().parents[3]
_BUILD_DATA = _EVOLUTION_ROOT / "data_prep" / "build_data"

# Per-repo configuration. Each key under a repo-type entry names a pipeline
# artifact by its filename inside build_data/.
_CONFIGS: dict = {
    "sigma": {
        "repo_root": _EVOLUTION_ROOT / "rules_repo" / "sigma",
        # Directories that contain Sigma rule files (git-path prefixes)
        "rule_dir_prefixes": (
            "rules/",
            "rules-emerging-threats/",
            "rules-threat-hunting/",
            "rules-compliance/",
            "rules-dfir/",
            "rules-placeholder/",
            "deprecated/",
            "unsupported/",
            # Legacy flat roots (pre-rules/ era)
            "windows/",
            "linux/",
            "web/",
            "tests/",
            "tools/",
            "other/",
        ),
        "file_exts": (".yml", ".yaml"),
        "suffix": "_sigma",
        # ---- Pipeline artifact filenames ----
        # Stage 1
        "stage1_out":     "lineage_metadata_raw_sigma.json",
        # Stage 1b (sigma-only: split_overmerged_lineage.py)
        "stage1b_out":    "lineage_metadata_split_sigma.json",
        # Stage 2 (build_lineage_metadata_merge.py)
        "stage2_in":      "lineage_metadata_split_sigma.json",   # output of stage 1b
        "stage2_out":     "lineage_metadata_merge_sigma.json",
        "stage2_log":     "lineage_merge_log_sigma.json",
        # Stage 2b (sigma-only: build_lineage_metadata_merge_stage2.py)
        "stage2b_out":    "lineage_metadata_merge_da_sigma.json",
        "stage2b_log":    "lineage_merge_da_log_sigma.json",
        # Stage 2 (build_semantic_lineage_metadata.py)
        # Both repos now share the same raw -> semantic path (no intermediate merge stages).
        "stage3_in":      "lineage_metadata_raw_sigma.json",
        "stage3_out":     "lineage_metadata_sigma.json",
        "stage3_splits":  "lineage_split_relationships_sigma.json",
        # Experiment: run Sigma raw lineage through the SSC-style raw -> semantic path
        "experiment_raw_stage3_out":    "lineage_metadata_sigma_experiment.json",
        "experiment_raw_stage3_splits": "lineage_split_relationships_sigma_experiment.json",
        "experiment_raw_stage3_compare": "lineage_metadata_raw_semantic_sigma_compare.json",
        # Stage 3 — non-head lineage candidate analysis + merge
        "non_head_candidates":     "non_head_lineage_candidate_matches_sigma.json",
        "non_head_candidates_csv": "non_head_lineage_candidate_matches_sigma.csv",
        "lineage_final":           "lineage_metadata_final_sigma.json",
        "lineage_final_report":    "lineage_final_report_sigma.json",
        # Patch application
        "patched":        "lineage_metadata_patched_sigma.json",
        "patch_log":      "manual_patch_apply_log_sigma.json",
        "patches":        "manual_patches_sigma.jsonl",
        # Stage 4 — per-rule SPL lineage extraction
        "spl_log":        "build_lineage_spl_per_rule_sigma.log",
        # Stage 5a — version filtering + normalization
        "rule_versions":       "rule_versions_sigma.jsonl",
        "version_filter_log":  "version_filter_log_sigma.jsonl",
        # Aux outputs
        "deletions":      "rule_deletion_semantics_sigma.json",
        "rename_split_log": "rename_split_log_sigma.json",
    },

    "ssc": {
        "repo_root": _EVOLUTION_ROOT / "rules_repo" / "splunk_sc",
        # Directories that contain Splunk Security Content rule files
        "rule_dir_prefixes": (
            "detections/",
            "investigations/",
            "baselines/",
            "escu/searches/",
            "deprecated/",
            "removed/baselines/",
            "removed/detections/",
            "removed/investigations/",
            "tests/",
            "ssa_detections/",
            "ba_detections/",
            "converted_detections/",
        ),
        "file_exts": (".yml", ".yaml", ".json"),
        "suffix": "_ssc",
        # ---- Pipeline artifact filenames ----
        # Stage 1
        "stage1_out":     "lineage_metadata_raw_ssc.json",
        # Stage 1b: SSC has no split stage; this key is unused but kept for symmetry
        "stage1b_out":    "lineage_metadata_split_ssc.json",
        # Stage 2 — SSC skips the split stage, so merge reads from raw directly
        "stage2_in":      "lineage_metadata_raw_ssc.json",
        "stage2_out":     "lineage_metadata_merge_ssc.json",
        "stage2_log":     "lineage_merge_log_ssc.json",
        # Stage 2b: SSC has no DA-merge stage; these keys are unused but kept for symmetry
        "stage2b_out":    "lineage_metadata_merge_da_ssc.json",
        "stage2b_log":    "lineage_merge_da_log_ssc.json",
        # Stage 2 — SSC goes raw -> semantic (skipping split/merge/DA stages)
        "stage3_in":      "lineage_metadata_raw_ssc.json",
        "stage3_out":     "lineage_metadata_ssc.json",
        "stage3_splits":  "lineage_split_relationships_ssc.json",
        # Stage 3 — non-head lineage candidate analysis + merge
        "non_head_candidates":     "non_head_lineage_candidate_matches_ssc.json",
        "non_head_candidates_csv": "non_head_lineage_candidate_matches_ssc.csv",
        "lineage_final":           "lineage_metadata_final_ssc.json",
        "lineage_final_report":    "lineage_final_report_ssc.json",
        # Patch application
        "patched":        "lineage_metadata_patched_ssc.json",
        "patch_log":      "manual_patch_apply_log_ssc.json",
        "patches":        "manual_patches_ssc.jsonl",
        # Stage 4 — per-rule SPL lineage extraction
        "spl_log":        "build_lineage_spl_per_rule_ssc.log",
        # Stage 5a — version filtering + normalization
        "rule_versions":       "rule_versions_ssc.jsonl",
        "version_filter_log":  "version_filter_log_ssc.jsonl",
        # Aux outputs
        "deletions":      "rule_deletion_semantics_ssc.json",
        "rename_split_log": "rename_split_log_ssc.json",
    },
}


class RepoConfig:
    """
    Holds all configuration for one upstream repo (sigma or ssc).

    Attributes:
        repo_type         -- "sigma" or "ssc"
        repo_root         -- absolute Path to the upstream git repo (inside rules_repo/)
        build_data        -- absolute Path to data_prep/build_data/
        rule_lineages_dir -- absolute Path to data_prep/rule_lineages_{repo_type}/
        rule_dir_prefixes -- tuple of git-path prefixes that identify rule files
        file_exts         -- tuple of file extensions to consider (e.g. (".yml", ".yaml"))
        suffix            -- output filename suffix ("_sigma" or "_ssc")
    """

    def __init__(self, repo_type: str) -> None:
        if repo_type not in _CONFIGS:
            raise ValueError(
                f"Unknown repo_type {repo_type!r}. Choose from: {list(_CONFIGS)}"
            )
        cfg = _CONFIGS[repo_type]
        self.repo_type: str = repo_type
        self.repo_root: Path = cfg["repo_root"]
        self.build_data: Path = _BUILD_DATA
        self.rule_lineages_dir: Path = _BUILD_DATA.parent / f"rule_lineages_{repo_type}"
        self.rule_dir_prefixes: tuple = cfg["rule_dir_prefixes"]
        self.file_exts: tuple = cfg["file_exts"]
        self.suffix: str = cfg["suffix"]
        self._artifacts: dict = cfg

    @property
    def macro_cache_dir(self) -> Optional[Path]:
        """
        Directory for the per-commit macro definition cache (SSC only).
        Layout: data_prep/macro_data/macro_cache_by_commit/{commit_hash}.json
        An index file lives alongside: macro_cache_index.json
        Returns None for Sigma.
        """
        if self.repo_type == "ssc":
            return self.build_data.parent / "macro_data" / "macro_cache_by_commit"
        return None

    def path(self, key: str) -> Path:
        """Return build_data / <filename> for a named pipeline artifact key."""
        try:
            return self.build_data / self._artifacts[key]
        except KeyError:
            raise KeyError(
                f"Unknown artifact key {key!r} for repo_type={self.repo_type!r}. "
                f"Available keys: {[k for k in self._artifacts if not k.startswith('rule_') and k not in ('file_exts', 'suffix', 'repo_root')]}"
            )
