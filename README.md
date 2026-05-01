# Evolution-of-Log-Based-Detection-Rules
research artifact for double-blinded review

## Environment

- Python: 3.14.2
- OS: tested on macOS
- Package manager: pip

## Data

Large files (rule repository bundles and generated pipeline outputs) are hosted on Google Drive:
**[https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)**

For artifact review, we recommend using the pre-built pipeline outputs from Google Drive rather than re-running `data_prep/build_src/`. In particular:

- Use the uploaded `build_data/` as the starting point for reproduction.
- Also download the later-stage outputs `ir_data/`, `align_data/`, and `llm_data/`.
- Only run `build_src/` if you specifically want to inspect or partially rebuild the raw lineage construction pipeline.

## Setup

### 1. Python environment

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Download the pre-built pipeline data (recommended)

For the clearest artifact-review path, download the following folders from the [Google Drive folder](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing) and place them under `data_prep/`:

| Google Drive folder | Local path | Required for review? | Notes |
|---|---|---|---|
| `build_data/` | `data_prep/build_data/` | Yes | Recommended starting point |
| `ir_data/` | `data_prep/ir_data/` | Yes | Later pipeline output |
| `align_data/` | `data_prep/align_data/` | Yes | Later pipeline output |
| `llm_data/` | `data_prep/llm_data/` | Yes | Must be downloaded; do not regenerate |

With those folders in place, you can proceed directly to the notebooks in `analysis/scripts/` and reproduce the paper outputs without rebuilding the data-prep pipeline.

### 3. Run the IR pipeline (`ir_src/`)

Parses the SPL in each rule version into a Predicate-Graph IR (PG-IR) used by all downstream analysis.

**Input:** `data_prep/build_data/rule_versions_{repo}.jsonl`

```bash
cd data_prep/ir_src
./run_pipeline.sh           # both repos
./run_pipeline.sh sigma     # sigma only
./run_pipeline.sh ssc       # ssc only
```

Outputs land in `data_prep/ir_data/`:

| Stage | Script | Output |
|---|---|---|
| 1 | `build_unified_ir` | `unified_ir_{repo}.jsonl` — SPL parsed to Unified IR |
| 2 | `build_pgir_from_ir` | `pgir_{repo}.jsonl` — Unified IR lifted to Predicate-Graph IR |
| 3 | `split_pgir_by_predicate_graph` | `pgir_{repo}_empty.jsonl` / `pgir_{repo}_nonempty.jsonl` |

### 4. Run the alignment pipeline (`align_src/`)

Computes pairwise alignment and edit-distance trajectories between consecutive rule versions.

**Input:** `data_prep/ir_data/pgir_{repo}_nonempty.jsonl`

```bash
cd data_prep/align_src
./run_pipeline.sh           # both repos
./run_pipeline.sh sigma     # sigma only
./run_pipeline.sh ssc       # ssc only
```

Outputs land in `data_prep/align_data/`:

| Output | Description |
|---|---|
| `all_steps_{repo}.jsonl` | One row per adjacent version pair — alignment, distance breakdown, change counts |
| `all_trajectories_{repo}.jsonl` | One row per lineage — cumulative distance, shock ratio, revert counts, endpoint similarity |

### 5. LLM annotation (`llm_src/`)

Generates prompts for consecutive rule-version pairs and collects structured LLM responses used for intention labelling.

**Input:** `data_prep/build_data/rule_versions_{repo}.jsonl`

| Script | Purpose |
|---|---|
| `prepare_llm_prompts.py` | Generate prompts for all non-noop pairs → `llm_data/{repo}/prompts.jsonl` |
| `prepare_llm_prompts_from_pair_manifest.py` | Generate prompts for an explicit curated pair list |
| `run_llm.py` | Send prompts to an OpenAI-compatible API → `llm_data/{repo}/results.jsonl` |
| `make_entries_from_audit.py` | Build pair manifests from audit results for targeted re-runs |
| `structural_test_labels.py` | Helper: derive structural ground-truth labels without LLM (used in analysis) |

> **LLM outputs are not reproducible.** Re-running `run_llm.py` will not produce identical results due to the non-deterministic nature of LLMs, potential model updates, and non-trivial API cost. **Use the provided `llm_data/` from Google Drive directly.**

### 6. Optional: restore rule snapshots and run the build pipeline (`build_src/`)

This step is not needed for normal artifact review. We recommend reviewers use the uploaded `data_prep/build_data/` from Google Drive and treat `build_src/` as an optional inspection or rebuild path.

The upstream rule repositories (SigmaHQ/sigma and splunk/security_content) are not stored in this repo. They are provided as git bundle files on Google Drive, preserving the full commit history as of the April 10, 2026 snapshot used in the study.

**Download the bundles from the [Google Drive folder](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)** and place them in `rules_repo/`:
```
rules_repo/sigma.bundle
rules_repo/splunk_sc.bundle
```

Then clone from the bundles:

```bash
git clone rules_repo/sigma.bundle     rules_repo/sigma     -b snapshot
git clone rules_repo/splunk_sc.bundle rules_repo/splunk_sc -b snapshot
```

This gives you two local git repositories with the exact commit history the pipeline was run against:

| Repository | Snapshot commit | Date |
|---|---|---|
| `rules_repo/sigma` | `d4d12bdd` | 2026-04-01 (HEAD as of 2026-04-10 + 17 branches) |
| `rules_repo/splunk_sc` | `aa672c674` | 2026-04-09 (HEAD as of 2026-04-10 + 678 branches) |

```bash
cd data_prep/build_src

./run_pipeline.sh           # run both repositories
./run_pipeline.sh sigma     # sigma only
./run_pipeline.sh ssc       # splunk security content only
```

Outputs land in `data_prep/build_data/`. The five stages are:

1. `build_rename_metadata` -> `lineage_metadata_raw_{repo}.json`
2. `build_semantic_lineage_metadata` -> `lineage_metadata_{repo}.json`
3. `merge_non_head_lineages` -> `lineage_metadata_final_{repo}.json`
4. `build_lineage_spl_per_rule` -> `rule_lineages_{repo}/`
5. `build_rule_versions` -> `rule_versions_{repo}.jsonl`

> **Note on runtime:** Stage 1 (`build_rename_metadata`) traverses the full commit history of both repositories and takes approximately 3 hours to complete end-to-end.

## Reproducing the paper results

The Google Drive folder contains pre-built outputs for all pipeline stages. For a faithful 1:1 reproduction of the results reported in the paper, **download the pre-built data from Google Drive** and place files under the corresponding directories before running any analysis:

| Google Drive folder | Local path | Reproducible from scratch? |
|---|---|---|
| `build_data/` | `data_prep/build_data/` | Mostly — see caveat below |
| `ir_data/` | `data_prep/ir_data/` | Yes, from provided `build_data/` |
| `align_data/` | `data_prep/align_data/` | Yes, from provided `build_data/` |
| `llm_data/` | `data_prep/llm_data/` | No — use provided files |

For artifact review, this is the recommended path: use the provided `build_data/`, `ir_data/`, `align_data/`, and `llm_data/` directly.

If you prefer to re-run `ir_src/` and `align_src/` yourself, start from the provided `build_data/` and the results will be identical to those reported in the paper.

**Why `build_data/` may differ slightly if re-run from scratch:** The bundles were created from a fresh GitHub clone on April 30, 2026 — 20 days after the original April 10 submodules snapshot. During that window, 9 short-lived fix/feature branches were deleted from the `splunk/security_content` repository on GitHub. Those branches contributed 9 unique commits (1,722 commit–rule records in aggregate, dominated by one broad formatting commit) to the original pipeline run that are no longer recoverable. The structural lineage outputs (`lineage_metadata_final_ssc.json`, 3,956 entries) are unaffected; only `rule_versions_ssc.jsonl` sees a minor reduction (~0.8%) in version records.

## Analysis

All figures and tables reported in the paper can be reproduced from the Jupyter notebooks in `analysis/scripts/`. Each notebook is self-contained and reads directly from the pre-built data directories above.

| Notebook | Paper outputs |
|---|---|
| `dataset.ipynb` | Table 1 — dataset overview (lineage counts, active/deleted, commit statistics) |
| `temporal.ipynb` | Figure 2, Figure 3, Table 2 — quarterly rule creation and revision volume, cohort stacking |
| `structural.ipynb` | Figure 4, Figure 5, Table 4, Table 5 — structural operation frequency and co-occurrence |
| `llm.ipynb` | Table 9, 7, 8 - LLM label validation against PG-IR structural signals and intent distribution |
