# Evolution-of-Log-Based-Detection-Rules

This repository accompanies the paper [**Evolution of Log-Based Detection Rules in Public Repositories**](https://arxiv.org/abs/2605.05383). It includes analysis notebooks, prepared datasets, and the data-preparation pipeline used to derive the rule evolution corpus.

## Interactive Demo *(under development)*

We are building a public interactive tool for exploring the rule evolution data — letting anyone search for a detection rule by name, view its revision timeline, and compare any two versions side by side. The demo is hosted at [elena6918.github.io/rule-explorer](https://elena6918.github.io/rule-explorer/) and is currently under active development. Features and data coverage will expand as the work progresses.

---

The easiest way to get started is to use the prepared data shared on Google Drive. That path lets you reproduce the results reported in the paper immediately and gives you a concrete starting point for understanding the data format before touching the full pipeline.

## Quick Start

### 1. Create a Python environment

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Download the prepared data

Prepared pipeline outputs are hosted on Google Drive:

**[https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing](https://drive.google.com/drive/folders/1jdDMVvuRV3cT0WwRK91-R-AHfpUxXvzU?usp=sharing)**

Download these folders and place them under `data_prep/`:

| Google Drive folder | Local path |
|---|---|
| `build_data/` | `data_prep/build_data/` |
| `ir_data/` | `data_prep/ir_data/` |
| `align_data/` | `data_prep/align_data/` |
| `llm_data/` | `data_prep/llm_data/` |

This is the recommended setup path.
- It is the fastest way to reproduce the results reported in the paper.
- It helps you inspect the dataset and intermediate formats directly.


## Reproducing the Paper Results

All reported figures and tables can be reproduced from the notebooks in `analysis/scripts/`. With the four downloaded data folders in place, the notebooks can be run directly without rebuilding the data pipeline.

| Notebook | Paper outputs |
|---|---|
| `dataset.ipynb` | Table 1 — dataset overview |
| `temporal.ipynb` | Figure 2, Figure 3, Table 2 — temporal trends and cohorts |
| `structural.ipynb` | Figure 4, Figure 5, Table 4, Table 5 — structural rule changes |
| `llm.ipynb` | Table 7, Table 8, Table 9 — LLM-based intent analysis and validation |

## Understanding the Data

The prepared data directories correspond to successive stages of the pipeline:

| Directory | What it contains |
|---|---|
| `data_prep/build_data/` | Rule lineage construction outputs and versioned rule records |
| `data_prep/ir_data/` | Parsed rule representations and predicate-graph IR |
| `data_prep/align_data/` | Pairwise rule alignments and trajectory summaries |
| `data_prep/llm_data/` | Prompt inputs and collected LLM annotation outputs |

For most readers, starting with these prepared outputs is the best way to learn the data layout and study design.

## Generating Your Own Analysis from Rule Repository Snapshots

If you want to regenerate the dataset yourself from snapshots of the upstream rule repositories, see the READMEs in the corresponding pipeline folders:

- [data_prep/build_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/build_src/README.md)
- [data_prep/ir_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/ir_src/README.md)
- [data_prep/align_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/align_src/README.md)
- [data_prep/llm_src/README.md](/Users/elena/Desktop/Evolution-of-Log-Based-Detection-Rules/data_prep/llm_src/README.md)

Those guides are the right place for details on rebuilding pipeline stages, working from repository snapshots, and generating new derived outputs.

## Notes

- `llm_data/` should be treated as provided data for reproduction; re-running LLM annotation may not produce identical outputs.
- The repository has been structured so newcomers can start from prepared data first, then move into the pipeline only if they want to generate new analyses.
