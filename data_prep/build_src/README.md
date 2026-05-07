# `build_src`

This directory contains the first stage of the data pipeline: constructing rule lineages and versioned rule records from snapshots of the upstream rule repositories.

For most users, the recommended path is still to start from the prepared data on Google Drive rather than rebuilding this stage from scratch.

## Recommended Path

Google Drive provides the outputs needed to work with this stage directly:

- `data_prep/build_data/`
- `data_prep/rule_lineages_sigma/`
- `data_prep/rule_lineages_ssc/`

Drive folder:

**[https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)**

If you only want to run the later parts of the build pipeline or inspect the intermediate data layout, download those folders and place them in the matching locations under `data_prep/`.

Google Drive also provides bundle files for the upstream rule repositories. Those bundles are the easiest way to populate `rules_repo/` if you want to run this stage yourself.

## Reproducibility Caveat

The Google Drive bundles are useful for rebuilding the pipeline, but they do not reproduce the paper results 1:1.

- The study used working repository snapshots as of April 10, 2026.
- The distributed bundles were packed later, on April 30, 2026, from repositories pinned to the April 10, 2026 commits.
- During that gap, some upstream branches were removed.
- As a result, the bundle-based rebuild is very close, but not identical, to the exact version history used for the paper.

The `rule_lineages_sigma/` and `rule_lineages_ssc/` folders shared on Google Drive are included as reference because they were generated from our earlier working submodules pulled on April 10, 2026, not from the later April 30 bundle packaging.

## Preparing `rules_repo/`

The scripts in this folder expect these local repositories:

```text
rules_repo/sigma
rules_repo/splunk_sc
```

You can prepare them in one of three ways.

### Option 1. Use the provided bundles

Download these files from Google Drive into `rules_repo/`:

```text
rules_repo/sigma.bundle
rules_repo/splunk_sc.bundle
```

Then initialize the local repositories from those bundles:

```bash
git clone rules_repo/sigma.bundle rules_repo/sigma -b snapshot
git clone rules_repo/splunk_sc.bundle rules_repo/splunk_sc -b snapshot
```

If you prefer to use the repository's submodule wiring, place the bundle files in `rules_repo/` first and then run:

```bash
git submodule update --init --recursive
```

Because `.gitmodules` points at those local bundle files, submodule initialization works only after the bundle files are already present on disk.

### Option 2. Clone the upstream repositories directly and checkout target commits

If you want to work from fresh upstream clones instead of bundles:

```bash
git clone https://github.com/SigmaHQ/sigma.git rules_repo/sigma
git clone https://github.com/splunk/security_content.git rules_repo/splunk_sc
```

Then checkout the commits you want to analyze:

```bash
git -C rules_repo/sigma checkout <sigma_commit>
git -C rules_repo/splunk_sc checkout <splunk_sc_commit>
```

This path is appropriate if you want to study a different snapshot than the one used in the paper.

### Option 3. Generate your own bundles at specific commits

If you want portable local snapshots, create branches at the target commits and bundle them:

```bash
git -C /path/to/sigma switch --detach <sigma_commit>
git -C /path/to/sigma branch -f snapshot <sigma_commit>
git -C /path/to/sigma bundle create /path/to/this-repo/rules_repo/sigma.bundle snapshot

git -C /path/to/security_content switch --detach <ssc_commit>
git -C /path/to/security_content branch -f snapshot <ssc_commit>
git -C /path/to/security_content bundle create /path/to/this-repo/rules_repo/splunk_sc.bundle snapshot
```

Then clone from those bundles into `rules_repo/`:

```bash
git clone rules_repo/sigma.bundle rules_repo/sigma -b snapshot
git clone rules_repo/splunk_sc.bundle rules_repo/splunk_sc -b snapshot
```

## Pipeline Stages

This build stage produces the lineage and version-history artifacts consumed by the later pipeline stages.

| Stage | Script | Main output |
|---|---|---|
| 1 | `build_rename_metadata.py` | `data_prep/build_data/lineage_metadata_raw_{repo}.json` |
| 2 | `build_semantic_lineage_metadata.py` | `data_prep/build_data/lineage_metadata_{repo}.json` |
| 3 | `merge_non_head_lineages.py` | `data_prep/build_data/lineage_metadata_final_{repo}.json` |
| 4 | `build_lineage_spl_per_rule.py` | `data_prep/rule_lineages_{repo}/` |
| 5 | `build_rule_versions.py` | `data_prep/build_data/rule_versions_{repo}.jsonl` |

`repo` is one of `sigma` or `ssc`.

More concretely, the stages do the following:

- Stage 1 traces rule-file history from git and builds a raw lineage graph using rename and file-history evidence only. This stage is intentionally noisy but lossless: it collects candidate rule histories without yet deciding which paths belong to the same semantic rule lineage.
- Stage 2 converts the raw file-history output into semantic lineage metadata. This step resolves repository-specific rule semantics and produces cleaner lineage candidates that are closer to the rule evolution units used in the paper.
- Stage 3 merges non-head and otherwise fragmented lineage candidates into the final lineage representation used downstream. The result is the canonical lineage metadata file for each repository.
- Stage 4 walks through each finalized lineage and extracts the per-version rule content. For Sigma, it reads the historical YAML and converts the `detection` logic to SPL with the `sigma` CLI. For Splunk Security Content, it extracts native SPL directly from the historical rule files.
- Stage 5 filters and normalizes the per-lineage histories into one versioned record stream per repository. These `rule_versions_{repo}.jsonl` files are the main inputs to the downstream IR, alignment, and LLM stages.

## Runtime Notes

- Stage 1 is by far the slowest part of the build and takes about 3 hours when run for both repositories.
- The later stages are substantially shorter.
- Rebuilding `rule_lineages_sigma/` from scratch requires the `sigma` CLI because Sigma rules are converted to SPL in stage 4.
- If you only need downstream analysis, it is usually better to reuse the prepared outputs from Google Drive.

## Running the Build

### End-to-end pipeline

`run_pipeline.sh` is the main end-to-end entrypoint for this build stage. It runs the full sequence of stages for one repository or both repositories.

```bash
cd data_prep/build_src

./run_pipeline.sh
```

This runs the build pipeline for both repositories:

- `sigma`
- `ssc`

To run only one repository:

```bash
cd data_prep/build_src

./run_pipeline.sh sigma
./run_pipeline.sh ssc
```

The script executes the same five-stage structure described above and writes outputs into `data_prep/build_data/` plus the per-repo `data_prep/rule_lineages_{repo}/` directories.

## Outputs

The main outputs of this directory are:

- `data_prep/build_data/lineage_metadata_raw_sigma.json`
- `data_prep/build_data/lineage_metadata_raw_ssc.json`
- `data_prep/build_data/lineage_metadata_sigma.json`
- `data_prep/build_data/lineage_metadata_ssc.json`
- `data_prep/build_data/lineage_metadata_final_sigma.json`
- `data_prep/build_data/lineage_metadata_final_ssc.json`
- `data_prep/rule_lineages_sigma/`
- `data_prep/rule_lineages_ssc/`
- `data_prep/build_data/rule_versions_sigma.jsonl`
- `data_prep/build_data/rule_versions_ssc.jsonl`

These outputs feed directly into the later pipeline stages documented in:

- [data_prep/ir_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/ir_src/README.md)
- [data_prep/align_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/align_src/README.md)
- [data_prep/llm_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/llm_src/README.md)
