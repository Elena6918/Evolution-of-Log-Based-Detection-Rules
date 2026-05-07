# `ir_src`

This directory contains the IR parsing stage of the pipeline. It takes the versioned rule records produced by [data_prep/build_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/build_src/README.md) and converts each rule version's SPL into structured intermediate representations used by the downstream analysis.

For most users, the easiest path is to start from the prepared `ir_data/` on Google Drive. If you want to regenerate the IR artifacts yourself, start from the prepared `build_data/` and run the scripts here.

## Recommended Path

Google Drive provides prepared outputs for this stage:

- `data_prep/ir_data/`

Drive folder:

**[https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)**

If your goal is to reproduce the paper results or inspect the parsed rule representations, download `ir_data/` directly and place it under `data_prep/`.

If you want to regenerate this stage yourself, the recommended starting point is still the prepared `data_prep/build_data/` from Google Drive rather than rebuilding the earlier `build_src` stage from repository snapshots.

## What This Stage Does

The IR pipeline turns each versioned rule into progressively more structured representations:

- It parses each rule version's SPL into a Unified IR that preserves the rule identity metadata from the build stage.
- It converts that Unified IR into a Predicate-Graph IR, which makes the rule's filtering logic explicit as predicate nodes and boolean structure.
- It separates entries with meaningful predicate graphs from entries whose parsed representation is empty.

These outputs are used downstream for structural comparison, edit-distance alignment, and later validation against the LLM-based annotations.

## Inputs

The main input to this stage is:

- `data_prep/build_data/rule_versions_sigma.jsonl`
- `data_prep/build_data/rule_versions_ssc.jsonl`

These files are produced by stage 5 of `build_src`.

Each input row corresponds to one valid rule version and includes the normalized SPL string that will be parsed here.

## Pipeline Stages

The `run_pipeline.sh` runs three steps for each repository:

| Stage | Script | Main output |
|---|---|---|
| 1 | `build_unified_ir.py` | `data_prep/ir_data/unified_ir_{repo}.jsonl` |
| 2 | `build_pgir_from_ir.py` | `data_prep/ir_data/pgir_{repo}.jsonl` |
| 3 | `split_pgir_by_predicate_graph.py` | `data_prep/ir_data/pgir_{repo}_empty.jsonl`, `data_prep/ir_data/pgir_{repo}_nonempty.jsonl` |

`repo` is one of `sigma` or `ssc`.

More concretely, the stages do the following:

- Stage 1 reads `rule_versions_{repo}.jsonl`, parses each SPL string, and writes one JSONL record per rule version with the original identity fields plus a Unified IR representation. Parse failures are preserved with `ir_success=false` and an error field.
- Stage 2 reads the Unified IR output and extracts the filtering logic into a Predicate-Graph IR. It normalizes predicate structure, collects predicate sources across the rule's search pipeline, combines them into a single boolean graph, and emits both the graph and a flat predicate list.
- Stage 3 splits the Predicate-Graph IR output into two files: one where the parsed rule has a non-empty predicate graph and one where it does not. In practice, most downstream structural analysis uses the non-empty subset.

## Running the IR Pipeline

`run_pipeline.sh` is the main entrypoint for this stage.

Run both repositories:

```bash
cd data_prep/ir_src
./run_pipeline.sh
```

Run a single repository:

```bash
cd data_prep/ir_src
./run_pipeline.sh sigma
./run_pipeline.sh ssc
```

The script reads from `data_prep/build_data/` and writes outputs into `data_prep/ir_data/`.

## Optional Helper Script

This directory also contains:

- `filter_non_empty_pgir.py`

That script reads `pgir_{repo}_nonempty.jsonl` and removes low-signal entries whose predicate graphs contain only placeholder or wildcard noise. It is useful for targeted cleanup experiments, but it is not currently part of the `run_pipeline.sh`.

## Outputs

The main outputs of this stage are:

- `data_prep/ir_data/unified_ir_sigma.jsonl`
- `data_prep/ir_data/unified_ir_ssc.jsonl`
- `data_prep/ir_data/pgir_sigma.jsonl`
- `data_prep/ir_data/pgir_ssc.jsonl`
- `data_prep/ir_data/pgir_sigma_empty.jsonl`
- `data_prep/ir_data/pgir_sigma_nonempty.jsonl`
- `data_prep/ir_data/pgir_ssc_empty.jsonl`
- `data_prep/ir_data/pgir_ssc_nonempty.jsonl`

The most important downstream output is usually `pgir_{repo}_nonempty.jsonl`, which is the input expected by the alignment stage.

## Downstream Use

The outputs from this directory feed directly into the next stage documented in:

- [data_prep/align_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/align_src/README.md)

If you need to understand where the input `rule_versions_{repo}.jsonl` files come from, see:

- [data_prep/build_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/build_src/README.md)
