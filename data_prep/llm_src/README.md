# `llm_src`

This directory contains the prompt-generation and API-calling scripts used for the LLM-based rule-pair annotation part of the project.

Unlike the earlier pipeline stages, these scripts are provided mainly for reference and experimentation. For reproducing the results reported in the paper, you should use the prepared `llm_data/` from Google Drive rather than re-running LLM calls yourself.

## Recommended Path

Google Drive provides prepared outputs for this stage:

- `data_prep/llm_data/`

Drive folder:

**[https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)**

If your goal is to reproduce the paper results, inspect the LLM annotations, or understand the prompt and result formats, download `llm_data/` directly and place it under `data_prep/`.

## Important Caveat

Re-running the LLM stage is not expected to reproduce the paper results 1:1.

- You must configure your own API access.
- You are responsible for any API cost.
- Model behavior can change over time.
- Responses are non-deterministic even when the prompt format is fixed.

Because of those factors, this directory should be treated as a reference implementation for how prompts were prepared and collected, not as a deterministic reproduction path.

## What This Stage Does

The scripts in this folder support an LLM annotation workflow over adjacent rule-version pairs.

- They prepare prompts from versioned rule records in `build_data/`.
- They submit those prompts to an OpenAI-compatible chat-completions API.
- They collect structured JSON responses describing observable predicate-level logic changes and inferred rationale labels.
- They support curated subsets, retries, and audit-oriented helper workflows.

These outputs are used in the paper's LLM analysis and validation sections, but they should be interpreted alongside the structural outputs from the earlier pipeline stages.

## Inputs

The main input to prompt generation is:

- `data_prep/build_data/rule_versions_sigma.jsonl`
- `data_prep/build_data/rule_versions_ssc.jsonl`

These files come from [data_prep/build_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/build_src/README.md).

Some helper scripts also use:

- `data_prep/align_data/all_steps_{repo}.jsonl`
- `data_prep/ir_data/pgir_{repo}_nonempty.jsonl`
- curated JSON manifests under `data_prep/llm_data/test/` or repo-specific `llm_data/{repo}/`

## Main Scripts

There is no single `run_pipeline.sh` in this directory. Instead, the workflow is split into a few scripts depending on whether you want full-corpus prompts, curated subsets, or API execution.

### `prepare_llm_prompts.py`

Generates prompt records from adjacent version pairs in `rule_versions_{repo}.jsonl`.

Typical usage:

```bash
cd data_prep/llm_src

python prepare_llm_prompts.py --repo sigma
python prepare_llm_prompts.py --repo ssc
python prepare_llm_prompts.py --repo both
```

This writes prompt JSONL files such as:

- `data_prep/llm_data/sigma/prompts.jsonl`
- `data_prep/llm_data/ssc/prompts.jsonl`

Each output record contains pair metadata plus a rendered prompt for one adjacent rule-version pair.

### `run_llm.py`

Sends prompt records through an OpenAI-compatible API and writes structured results.

Typical usage:

```bash
cd data_prep/llm_src

python run_llm.py \
  --input ../llm_data/sigma/prompts.jsonl \
  --outfile ../llm_data/sigma/results.jsonl \
  --model gpt-4o-mini
```

This script requires user-managed API configuration. In practice, that means:

- setting your own API key, typically through `OPENAI_API_KEY`
- choosing your own model
- accepting the associated API cost
- understanding that reruns may yield different outputs

The script targets an OpenAI-compatible chat-completions endpoint and also supports options such as dry runs, retries, aggregation, and prompt-size guards.

### `prepare_llm_prompts_from_pair_manifest.py`

Generates prompts for an explicit list of version pairs instead of walking the whole corpus.

Typical usage:

```bash
cd data_prep/llm_src

python prepare_llm_prompts_from_pair_manifest.py \
  --entries ../llm_data/test/test.json
```

This is useful when you want to evaluate or re-run a curated subset exactly, and it also writes the corresponding raw `rule_versions` endpoint rows for inspection.

## Helper Scripts

This directory also includes several helper utilities for targeted experiments and auditing.

### `make_entries_from_audit.py`

Builds a minimal pair-manifest JSON file from an `audit_results.jsonl` file, typically keeping pairs where the structural and LLM labels agree that predicate logic changed.

This is useful when you want to create a smaller follow-up set for targeted re-runs.

### `structural_test_labels.py`

Builds structural labels for candidate test pairs without making any LLM calls. It uses the structural outputs from the alignment and IR stages to derive labels such as value additions, value removals, and alternative additions.

This is useful when you want a structure-based comparison set or want to inspect pair categories before spending API budget.

## Suggested Workflow

If you want to experiment with the LLM stage yourself, a reasonable workflow is:

1. Start from the provided `build_data/`, and optionally the provided `align_data/` and `ir_data/`.
2. Generate prompts with `prepare_llm_prompts.py` or `prepare_llm_prompts_from_pair_manifest.py`.
3. Inspect a sample of the prompt JSONL manually before making any API calls.
4. Configure your own API key and choose a model you are comfortable paying for.
5. Run `run_llm.py` on a small subset first.
6. Scale up only if the output format and cost are acceptable for your use case.

For most readers, step 3 is already enough to understand how the annotation setup works.

## Outputs

Common outputs in `data_prep/llm_data/` include:

- `llm_data/{repo}/prompts.jsonl`
- `llm_data/{repo}/results.jsonl`
- `llm_data/test/pair_manifest_prompts.jsonl`
- `llm_data/test/rule_versions_sigma_subset.jsonl`
- `llm_data/test/rule_versions_ssc_subset.jsonl`

Prompt files contain the rendered prompt text for each selected pair. Result files contain the original pair metadata plus:

- `model`
- `llm_result`
- `llm_error`

## Reproduction Guidance

For reproducing the paper:

- use the provided `data_prep/llm_data/`
- do not expect API reruns to match the released outputs exactly
- treat these scripts as documentation of the workflow rather than a deterministic pipeline stage

If you need the upstream versioned rule inputs, see:

- [data_prep/build_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/build_src/README.md)

If you want to compare the LLM outputs against structural signals, see:

- [data_prep/align_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/align_src/README.md)
- [data_prep/ir_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/ir_src/README.md)
