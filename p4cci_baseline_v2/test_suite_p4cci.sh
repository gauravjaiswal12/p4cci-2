#!/usr/bin/env bash
# =============================================================================
# P4CCI -- 30-Run Statistical Test Suite (matches KBCS test_suite.sh structure)
# =============================================================================
# Runs the experiment N times (default 30), each for DURATION seconds.
# After each run, calls collect_metrics.py to parse iperf logs and append CSV.
#
# Usage:
#   sudo ./test_suite_p4cci.sh                                   # defaults
#   sudo ./test_suite_p4cci.sh --topo dumbbell --mode p4cci      # dumbbell + P4CCI
#   sudo ./test_suite_p4cci.sh --topo cross --mode baseline      # cross + FIFO
#   sudo ./test_suite_p4cci.sh --topo dumbbell --mode baseline    # dumbbell + FIFO
#   sudo ./test_suite_p4cci.sh --runs 10 --duration 30           # quick test
#
# Output CSV files (in results/):
#   p4cci_dumbbell_results.csv     -- P4CCI mode, dumbbell topology
#   p4cci_cross_results.csv        -- P4CCI mode, cross topology
#   baseline_dumbbell_results.csv  -- Baseline (FIFO), dumbbell topology
#   baseline_cross_results.csv     -- Baseline (FIFO), cross topology
# =============================================================================

set -euo pipefail

# ---- Defaults ----
RUNS=30
DURATION=60
MODE="p4cci"          # "p4cci" or "baseline"
TOPO="dumbbell"       # "dumbbell" or "cross"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
RESULTS_DIR="${SCRIPT_DIR}/results"

# ---- Parse arguments ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)     MODE="$2";     shift 2 ;;
        --topo)     TOPO="$2";     shift 2 ;;
        --runs)     RUNS="$2";     shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        *)          echo "[WARN] Unknown arg: $1"; shift ;;
    esac
done

# ---- Select topology script ----
if [[ "$TOPO" == "cross" ]]; then
    TOPO_SCRIPT="${SCRIPT_DIR}/topology_cross.py"
else
    TOPO_SCRIPT="${SCRIPT_DIR}/topology_4flow.py"
fi

CSV_FILE="${RESULTS_DIR}/${MODE}_${TOPO}_results.csv"

mkdir -p "${LOG_DIR}" "${RESULTS_DIR}"

# Delete old CSV to start fresh (avoid mixing old+new data)
rm -f "${CSV_FILE}"

# Load TCP kernel modules
sudo modprobe tcp_bbr      2>/dev/null || true
sudo modprobe tcp_vegas    2>/dev/null || true
sudo modprobe tcp_illinois 2>/dev/null || true

echo ""
echo "=================================================================="
echo "  P4CCI Statistical Test Suite"
echo "=================================================================="
echo "  Topology  : ${TOPO}"
echo "  Mode      : ${MODE}"
echo "  Runs      : ${RUNS}"
echo "  Duration  : ${DURATION}s per run"
echo "  Script    : ${TOPO_SCRIPT}"
echo "  CSV       : ${CSV_FILE}"
echo "  Total est : ~$((RUNS * (DURATION + 30)))s"
echo "=================================================================="
echo ""

for run in $(seq 1 "${RUNS}"); do
    echo ""
    echo "------------------------------------------------------------"
    echo "  RUN ${run} / ${RUNS}  |  ${TOPO}  |  ${MODE}  |  $(date '+%H:%M:%S')"
    echo "------------------------------------------------------------"

    RUN_LOG_DIR="${LOG_DIR}/run_${run}"
    RUN_LOG="${LOG_DIR}/run_${run}.log"
    mkdir -p "${RUN_LOG_DIR}"

    # Step 1: Clean Mininet
    echo "  [1/3] Cleaning Mininet..."
    sudo mn -c > /dev/null 2>&1 || true
    sudo killall -9 simple_switch 2>/dev/null || true
    sleep 2

    # Step 2: Run topology (self-contained: creates switches, installs rules, runs traffic)
    echo "  [2/3] Running ${TOPO} topology (~${DURATION}s)..."
    sudo python3 "${TOPO_SCRIPT}" \
        --mode "${MODE}" \
        --duration "${DURATION}" \
        --log-dir "${RUN_LOG_DIR}" \
        2>&1 | tee "${RUN_LOG}"

    # Step 3: Collect metrics from iperf logs
    echo "  [3/3] Collecting metrics..."
    python3 "${SCRIPT_DIR}/collect_metrics.py" \
        --run      "${run}" \
        --topo     "${TOPO}" \
        --mode     "${MODE}" \
        --duration "${DURATION}" \
        --log-dir  "${RUN_LOG_DIR}" \
        2>&1 | tee -a "${RUN_LOG}"

    # Cleanup
    sudo mn -c > /dev/null 2>&1 || true
    sudo killall -9 simple_switch 2>/dev/null || true
    sleep 3

    echo "  [OK] Run ${run} complete."
done

echo ""
echo "=================================================================="
echo "  ALL ${RUNS} RUNS COMPLETE"
echo "  Results: ${CSV_FILE}"
echo "=================================================================="
echo ""

echo "--- CSV Preview ---"
head -5 "${CSV_FILE}" 2>/dev/null || echo "(no data)"
echo ""

echo "To compare with KBCS, copy this CSV to kbcs_v2/results/ and run:"
echo "  python3 analyze_results.py"
