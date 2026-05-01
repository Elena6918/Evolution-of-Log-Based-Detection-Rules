#!/usr/bin/env bash
# ============================================================
# align_src/run_pipeline.sh
#
# Alignment pipeline: computes PGIR alignment + distance trajectories.
#
#   Input  : ir_data/pgir_{repo}_nonempty.jsonl  (from ir_src pipeline)
#   Output : align_data/all_trajectories_{repo}.jsonl
#            align_data/all_steps_{repo}.jsonl
#
# Usage:
#   ./run_pipeline.sh                    # run both repos
#   ./run_pipeline.sh sigma              # sigma only
#   ./run_pipeline.sh ssc                # ssc only
#   ./run_pipeline.sh sigma                      # default: polarity gate on
#   ./run_pipeline.sh sigma --no-hard-gate-polarity
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

_tty() { [ -t 1 ]; }
green()  { _tty && printf '\033[0;32m%s\033[0m\n' "$*" || echo "$*"; }
yellow() { _tty && printf '\033[0;33m%s\033[0m\n' "$*" || echo "$*"; }
red()    { _tty && printf '\033[0;31m%s\033[0m\n' "$*" || echo "$*"; }
bold()   { _tty && printf '\033[1m%s\033[0m\n'    "$*" || echo "$*"; }

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

_elapsed() {
    local start=$1 end
    end=$(date +%s)
    local s=$(( end - start ))
    printf '%dm%02ds' $(( s / 60 )) $(( s % 60 ))
}

run_stage() {
    local label="$1"; shift
    local script="$1"; shift
    bold "  [$label] $script"
    local t0
    t0=$(date +%s)
    "$PYTHON" "$SCRIPT_DIR/$script" "$@"
    green "  -> done in $(_elapsed "$t0")"
    echo
}

run_repo() {
    local repo="$1"
    shift
    local repo_upper
    repo_upper="$(upper "$repo")"

    bold "==============================="
    bold " ${repo_upper} align pipeline"
    bold "==============================="
    echo

    local t0
    t0=$(date +%s)

    run_stage "1" export_align_trajectories.py --repo-type "$repo" 

    green "${repo_upper} align pipeline complete in $(_elapsed "$t0")"
    echo
}

run_sigma() { run_repo sigma; }
run_ssc()   { run_repo ssc;   }

TARGET="${1:-both}"
EXTRA_ARGS=("${@:2}")

case "$TARGET" in
    sigma) run_repo sigma ;;
    ssc)   run_repo ssc ;;
    both)  run_repo sigma; run_repo ssc;;
    *)
        red "Unknown target: $TARGET"
        echo "Usage: $0 [sigma|ssc|both] [extra export_align_trajectories.py args]"
        exit 1
        ;;
esac

bold "All done."
