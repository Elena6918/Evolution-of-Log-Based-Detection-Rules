# `align_src`

This directory contains the structural alignment stage of the pipeline. It takes the non-empty Predicate-Graph IR records produced by [data_prep/ir_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/ir_src/README.md) and computes how each rule changes from one version to the next.

For most users, the easiest path is to start from the prepared `align_data/` on Google Drive. If you want to regenerate the alignment outputs yourself, start from the prepared `ir_data/` and run the script here.

## Recommended Path

Google Drive provides prepared outputs for this stage:

- `data_prep/align_data/`

Drive folder:

**[https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)**

If your goal is to reproduce the paper results or inspect structural change summaries directly, download `align_data/` and place it under `data_prep/`.

If you want to regenerate this stage, the recommended input is the prepared `data_prep/ir_data/` from Google Drive rather than rebuilding the earlier stages first.

## What This Stage Does

The alignment pipeline measures structural evolution between adjacent versions of the same rule lineage.

- It reads the non-empty PGIR records for each repository.
- It converts each rule version into a canonical boolean tree representation.
- It aligns adjacent versions within each lineage and computes structural edit-distance style change scores.
- It summarizes both per-step changes and whole-lineage trajectories.

This stage is what makes it possible to quantify questions like how much a rule changed, whether revisions are mostly small or large, whether a lineage contains reversions, and how cumulative change compares to net endpoint change.

## Inputs

The main input to this stage is:

- `data_prep/ir_data/pgir_sigma_nonempty.jsonl`
- `data_prep/ir_data/pgir_ssc_nonempty.jsonl`

These files are produced by the IR stage and contain one JSONL record per rule version with a non-empty predicate graph.

## Pipeline Script

The `run_pipeline.sh` runs one main step per repository:

| Step | Script | Outputs |
|---|---|---|
| 1 | `export_align_trajectories.py` | `data_prep/align_data/all_steps_{repo}.jsonl`, `data_prep/align_data/all_trajectories_{repo}.jsonl` |

`repo` is one of `sigma` or `ssc`.

Although the shell script is short, `export_align_trajectories.py` performs the full batch alignment workflow for this stage.

## What `export_align_trajectories.py` Computes

For each lineage, the script groups versions by `lineage_id`, orders them by `version_index`, and then:

- Builds a canonical boolean AST for each version from its predicate graph.
- Aligns each adjacent pair of versions.
- Computes a structural distance score and a change-count breakdown for each adjacent pair.
- Emits one row per adjacent version pair into `all_steps_{repo}.jsonl`.
- Computes one trajectory summary per lineage and writes it to `all_trajectories_{repo}.jsonl`.

The output is split into two complementary views:

- `all_steps_{repo}.jsonl` is the fine-grained view. Each row describes one adjacent version pair and includes distance, change breakdown, alignment summary, tree sizes, signatures, and whether the step is effectively a no-op.
- `all_trajectories_{repo}.jsonl` is the lineage-level view. Each row summarizes an entire rule lineage, including version count, cumulative versus net change, maximum and median step size, shock ratio, endpoint similarity, field overlap, and simple revert statistics.

## Running the Alignment Pipeline

`run_pipeline.sh` is the main entrypoint for this stage.

Run both repositories:

```bash
cd data_prep/align_src
./run_pipeline.sh
```

Run a single repository:

```bash
cd data_prep/align_src
./run_pipeline.sh sigma
./run_pipeline.sh ssc
```

The script reads from `data_prep/ir_data/` and writes outputs into `data_prep/align_data/`.

The pipeline also accepts extra arguments that are forwarded to `export_align_trajectories.py`, which is useful if you want to experiment with alignment settings such as polarity gating.

## Helper Utilities

This directory also contains a few supporting scripts:

- `pgir_align.py`: the core alignment and distance-scoring library used by the batch exporter.
- `score_pgir_between_two.py`: a debug utility for inspecting the alignment and distance between two specific PGIR records.
- `structural_ops_helpers.py`: helper utilities used by the alignment logic.

These are mainly for development, debugging, or targeted experiments rather than standard reproduction.

### Inspecting Sample Alignments with `score_pgir_between_two.py`

`score_pgir_between_two.py` is the easiest way to look inside the alignment process for one pair of rule versions. It prints a human-readable summary of:

- the two selected PGIR records
- the canonical predicate-tree representation for each side
- which predicates and operators were matched
- which nodes were treated as insertions or deletions
- the final distance score and its breakdown

This is useful when you want a more concrete feel for what the aligner considers a match, a modification, a structural insertion, or a deletion.

The script reads from a `pgir_{repo}_nonempty.jsonl` file and lets you select records in two ways.

Select by non-empty line number:

```bash
cd data_prep/align_src

python score_pgir_between_two.py \
  --jsonl ../ir_data/pgir_ssc_nonempty.jsonl \
  --line-a 42 \
  --line-b 43
```

This is the fastest option when you are just browsing examples in the file.

Select by lineage and version index:

```bash
cd data_prep/align_src

python score_pgir_between_two.py \
  --jsonl ../ir_data/pgir_ssc_nonempty.jsonl \
  --lineage-a <lineage_id> --version-a 3 \
  --lineage-b <lineage_id> --version-b 4
```

This is usually the better option when you want to inspect an adjacent pair from a known lineage.

If you want to save the raw alignment result in addition to the printed summary:

```bash
cd data_prep/align_src

python score_pgir_between_two.py \
  --jsonl ../ir_data/pgir_ssc_nonempty.jsonl \
  --line-a 42 \
  --line-b 43 \
  --out /tmp/score.json
```

Some practical ways to use it:

- Pick two adjacent versions from the same lineage to see how a normal revision is aligned.
- Compare a pair with a low `d_step` from `all_steps_{repo}.jsonl` against a pair with a high `d_step` to build intuition for what the distance metric is capturing.
- Inspect no-op or near-no-op pairs to see why two versions are treated as structurally equivalent.
- Experiment with flags such as `--no-hard-gate-polarity` when you want to understand how alignment settings affect matching behavior.

When reading the output, the most useful sections are usually:

- `Predicates`: shows the leaf predicates extracted from each canonical tree.
- `Canonical tree`: shows the boolean structure actually being aligned.
- `Matched pairs`: shows which predicates or operators were aligned across the two versions.
- `Unmatched in A` and `Unmatched in B`: shows which parts of the earlier or later version were treated as deletions or insertions.
- `Distance`: shows the total score and the contribution from different edit types.

For exploratory work, a good workflow is:

1. Find an interesting pair in `data_prep/align_data/all_steps_{repo}.jsonl`.
2. Locate the corresponding lineage and version indices.
3. Run `score_pgir_between_two.py` on that pair.
4. Compare the printed alignment with the numeric `d_step`, `change_counts`, and `dist_breakdown` fields in `all_steps_{repo}.jsonl`.

## Outputs

The main outputs of this stage are:

- `data_prep/align_data/all_steps_sigma.jsonl`
- `data_prep/align_data/all_steps_ssc.jsonl`
- `data_prep/align_data/all_trajectories_sigma.jsonl`
- `data_prep/align_data/all_trajectories_ssc.jsonl`

In practice:

- Use `all_steps_{repo}.jsonl` when you want to study individual revisions.
- Use `all_trajectories_{repo}.jsonl` when you want lineage-level summaries and aggregate analyses.

## Downstream Use

These outputs are consumed directly by the analysis notebooks in `analysis/scripts/`.

If you need to regenerate the PGIR inputs first, see:

- [data_prep/ir_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/ir_src/README.md)
