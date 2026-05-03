#!/usr/bin/env bash
# =============================================================================
# test_suite_p4cci.sh — 10-run statistical evaluation for P4CCI
#
# Each run:
#   1. Clean Mininet
#   2. Run topology_4flow.py (self-contained: rules + traffic + wait)
#   3. Collect metrics from iperf log files
#
# Usage:
#   chmod +x test_suite_p4cci.sh
#   sudo ./test_suite_p4cci.sh [--mode p4cci] [--runs 10] [--duration 60]
# =============================================================================

set -euo pipefail

RUNS=10
DURATION=60
MODE="p4cci"
TOPOLOGY="dumbbell"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
RESULTS_DIR="${SCRIPT_DIR}/results"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)     MODE="$2";     shift 2 ;;
        --runs)     RUNS="$2";     shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        *)          echo "[WARN] Unknown arg: $1"; shift ;;
    esac
done

mkdir -p "${LOG_DIR}" "${RESULTS_DIR}"

# Delete old CSV so we start fresh
rm -f "${RESULTS_DIR}/p4cci_statistical_results.csv"

echo "======================================================================"
echo "  P4CCI Statistical Test Suite"
echo "======================================================================"
echo "  Mode     : ${MODE}"
echo "  Runs     : ${RUNS}"
echo "  Duration : ${DURATION}s per run"
echo "  CSV      : ${RESULTS_DIR}/p4cci_statistical_results.csv"
echo "======================================================================"
echo ""

for run in $(seq 1 "${RUNS}"); do
    echo ""
    echo "────────────────────────────────────────────────────────────────"
    echo "  RUN ${run} / ${RUNS}  |  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "────────────────────────────────────────────────────────────────"

    RUN_LOG_DIR="${LOG_DIR}/run_${run}"
    RUN_LOG="${LOG_DIR}/run_${run}.log"

    # Step 1: Clean Mininet
    echo "  [1/3] Cleaning Mininet..."
    mn -c 2>/dev/null || true
    sleep 2

    # Step 2: Run topology (self-contained: installs rules, runs traffic, waits)
    echo "  [2/3] Running topology_4flow.py (this will take ~${DURATION}s)..."
    sudo python3 "${SCRIPT_DIR}/topology_4flow.py" \
        --mode "${MODE}" \
        --duration "${DURATION}" \
        --log-dir "${RUN_LOG_DIR}" \
        2>&1 | tee "${RUN_LOG}"

    # Step 3: Collect metrics from the iperf log files
    echo "  [3/3] Collecting metrics..."
    python3 "${SCRIPT_DIR}/collect_metrics.py" \
        --run      "${run}" \
        --duration "${DURATION}" \
        --mode     "${MODE}" \
        --topology "${TOPOLOGY}" \
        --log-dir  "${RUN_LOG_DIR}" \
        2>&1 | tee -a "${RUN_LOG}"

    echo "  ✓ Run ${run} complete."
done

echo ""
echo "======================================================================"
echo "  ALL ${RUNS} RUNS COMPLETE"
echo "  Results: ${RESULTS_DIR}/p4cci_statistical_results.csv"
echo "======================================================================"
echo ""
cat "${RESULTS_DIR}/p4cci_statistical_results.csv"
