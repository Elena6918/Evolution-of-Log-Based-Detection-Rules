#!/usr/bin/env bash
# ============================================================
# run_pipeline.sh
#
# Runs the lineage-metadata build pipeline for sigma, ssc, or both.
# Outputs land in data_prep/build_data/ with per-repo suffixes.
#
# Both repos share the same pipeline:
#   1  build_rename_metadata           -> lineage_metadata_raw_{repo}.json
#   2  build_semantic_lineage_metadata -> lineage_metadata_{repo}.json
#   3  merge_non_head_lineages         -> lineage_metadata_final_{repo}.json
#   4  build_lineage_spl_per_rule      -> rule_lineages_{repo}/*.json
#
# Usage:
#   ./run_pipeline.sh           # run both repos
#   ./run_pipeline.sh sigma     # sigma only
#   ./run_pipeline.sh ssc       # ssc only
# ============================================================

set -euo pipefail

BUILD_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

# ---- colour helpers (silent if not a tty) ----------------------
_tty() { [ -t 1 ]; }
green()  { _tty && printf '\033[0;32m%s\033[0m\n' "$*" || echo "$*"; }
yellow() { _tty && printf '\033[0;33m%s\033[0m\n' "$*" || echo "$*"; }
red()    { _tty && printf '\033[0;31m%s\033[0m\n' "$*" || echo "$*"; }
bold()   { _tty && printf '\033[1m%s\033[0m\n'    "$*" || echo "$*"; }

upper() {
    printf '%s' "$1" | tr '[:lower:]' '[:upper:]'
}

# ---- timing helper ---------------------------------------------
_elapsed() {
    local start=$1 end
    end=$(date +%s)
    local s=$(( end - start ))
    printf '%dm%02ds' $(( s / 60 )) $(( s % 60 ))
}

# ---- run one stage ---------------------------------------------
run_stage() {
    local label="$1"; shift
    local script="$1"; shift
    # remaining args are passed through to the script

    bold "  [$label]"
    local t0
    t0=$(date +%s)

    "$PYTHON" "$BUILD_SRC/$script" "$@"

    green "  -> done in $(_elapsed "$t0")"
    echo
}

# ====================================================================
# Shared pipeline (same structure for both repos)
#
#   Stage 1  build_rename_metadata               -> lineage_metadata_raw_{repo}.json
#   Stage 2  build_semantic_lineage_metadata     -> lineage_metadata_{repo}.json
#   Stage 3  merge_non_head_lineages             -> lineage_metadata_final_{repo}.json
#                                                   lineage_final_report_{repo}.json
#   Stage 4  build_lineage_spl_per_rule          -> rule_lineages_{repo}/*.json
#            (Sigma: requires `sigma` CLI; SSC: git show + disk fallback)
#   Stage 5  build_rule_versions                 -> rule_versions_{repo}.jsonl
#            (version-level filter + SPL normalize; SSC: macro expansion)
# ====================================================================
run_repo() {
    local repo="$1"
    local repo_upper
    repo_upper="$(upper "$repo")"

    bold "==============================="
    bold " ${repo_upper} pipeline"
    bold "==============================="
    echo

    local t0
    t0=$(date +%s)

    # run_stage "1"  build_rename_metadata.py               --repo-type "$repo"
    # run_stage "2"  build_semantic_lineage_metadata.py     --repo-type "$repo"
    # run_stage "3"  merge_non_head_lineages.py             --repo-type "$repo"
    run_stage "4"  build_lineage_spl_per_rule.py          --repo-type "$repo"
    run_stage "5"  build_rule_versions.py                 --repo-type "$repo"

    green "${repo_upper} pipeline complete in $(_elapsed "$t0")"
    echo
}

run_sigma() { run_repo sigma; }
run_ssc()   { run_repo ssc;   }

# ====================================================================
# Entry point
# ====================================================================
TARGET="${1:-both}"

case "$TARGET" in
    sigma)
        run_sigma
        ;;
    ssc)
        run_ssc
        ;;
    both)
        run_sigma
        run_ssc
        ;;
    *)
        red "Unknown target: $TARGET"
        echo "Usage: $0 [sigma|ssc|both]"
        exit 1
        ;;
esac

bold "All done."
