#!/usr/bin/env bash
# ============================================================
# ir_src/run_pipeline.sh
#
# IR parsing pipeline: converts rule versions → enriched PG-IR.
#
# Both repos share the same pipeline:
#   Stage 1  build_unified_ir               -> ir_data/unified_ir_{repo}.jsonl
#   Stage 2  build_pgir_from_ir             -> ir_data/pgir_{repo}.jsonl
#   Stage 3a split_pgir_by_predicate_graph  -> ir_data/pgir_{repo}_empty.jsonl
#                                              ir_data/pgir_{repo}_nonempty.jsonl
#   Stage 3b filter_non_empty_pgir          -> ir_data/pgir_{repo}_filtered.jsonl
#   Stage 4  enrich_pgir_semantics_and_prov -> ir_data/pgir_{repo}_enriched.jsonl
#
# Usage:
#   ./run_pipeline.sh           # run both repos
#   ./run_pipeline.sh sigma     # sigma only
#   ./run_pipeline.sh ssc       # ssc only
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

# ---- colour helpers (silent if not a tty) ----------------------
_tty() { [ -t 1 ]; }
green()  { _tty && printf '\033[0;32m%s\033[0m\n' "$*" || echo "$*"; }
yellow() { _tty && printf '\033[0;33m%s\033[0m\n' "$*" || echo "$*"; }
red()    { _tty && printf '\033[0;31m%s\033[0m\n' "$*" || echo "$*"; }
bold()   { _tty && printf '\033[1m%s\033[0m\n'    "$*" || echo "$*"; }

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

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
    # remaining args passed through to the script

    bold "  [$label] $script"
    local t0
    t0=$(date +%s)

    "$PYTHON" "$SCRIPT_DIR/$script" "$@"

    green "  -> done in $(_elapsed "$t0")"
    echo
}

# ====================================================================
# Per-repo pipeline
# ====================================================================
run_repo() {
    local repo="$1"
    local repo_upper
    repo_upper="$(upper "$repo")"

    bold "==============================="
    bold " ${repo_upper} IR pipeline"
    bold "==============================="
    echo

    local t0
    t0=$(date +%s)

    run_stage "1"  build_unified_ir.py               --repo-type "$repo"
    run_stage "2"  build_pgir_from_ir.py             --repo-type "$repo"
    run_stage "3" split_pgir_by_predicate_graph.py  --repo-type "$repo"

    green "${repo_upper} IR pipeline complete in $(_elapsed "$t0")"
    echo
}

run_sigma() { run_repo sigma; }
run_ssc()   { run_repo ssc;   }

# ====================================================================
# Entry point
# ====================================================================
TARGET="${1:-both}"

case "$TARGET" in
    sigma) run_sigma ;;
    ssc)   run_ssc   ;;
    both)  run_sigma; run_ssc ;;
    *)
        red "Unknown target: $TARGET"
        echo "Usage: $0 [sigma|ssc|both]"
        exit 1
        ;;
esac

bold "All done."
