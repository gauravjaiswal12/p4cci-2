#!/usr/bin/env python3
"""
controller.py — Control-plane logic for P4CCI.

Pipeline (paper Section IV):
  1. Collect per-flow BIF time-series from P4 digest.
  2. MAD-based outlier rejection (Robust Z-score, threshold=3.5).
  3. Z-normalization.
  4. Classify via FCN (if PyTorch available) or CV heuristic fallback.
  5. Push ACL rule to P4 switch (cca_classification table) via simple_switch_CLI.

Evaluation metrics (paper Section VI):
  - Jain Fairness Index: J = (Σxi)² / (n·Σxi²)
  - Link Utilization:    U = Σthroughput / link_capacity
  - Throughput Deviation: σ / μ of per-flow throughputs
"""

import sys
import os
import math
import subprocess
import argparse
from collections import defaultdict

# -- Constants from paper -----------------------------------------------------
SEQ_LENGTH    = 20       # L=20 BIF samples per window (paper Section IV-A)
MAD_THRESHOLD = 3.5      # Robust Z-score rejection cutoff (paper Section IV-A)
THRIFT_PORT   = 9090
SWITCH_CMD    = 'simple_switch_CLI'

# Per-flow BIF sample buffer: {(src_ip, dst_ip, src_port, dst_port): [bif...]}
flow_bif_buffer = defaultdict(list)

# -- Try to load trained FCN model --------------------------------------------
FCN_MODEL = None
try:
    from fcn_model import load_model, predict_single, TORCH_AVAILABLE, SEQ_LENGTH as FCN_SEQ_LEN
    if TORCH_AVAILABLE:
        _model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fcn_model.pth')
        if os.path.exists(_model_path):
            FCN_MODEL = load_model(_model_path)
            print(f"[Controller] FCN model loaded from {_model_path}")
        else:
            print("[Controller] No trained FCN found — will use CV heuristic. Run fcn_model.py to train.")
    else:
        print("[Controller] PyTorch unavailable — using CV heuristic classifier.")
except ImportError:
    TORCH_AVAILABLE = False
    print("[Controller] fcn_model.py not found — using CV heuristic classifier.")


# -----------------------------------------------------------------------------
# Pure-Python math helpers
# -----------------------------------------------------------------------------

def _median(data):
    s = sorted(data)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 != 0 else (s[mid - 1] + s[mid]) / 2.0

def _mean(data):
    return sum(data) / len(data)

def _std(data):
    mu = _mean(data)
    return math.sqrt(sum((x - mu) ** 2 for x in data) / len(data))


# -----------------------------------------------------------------------------
# Preprocessing (paper Section IV-A)
# -----------------------------------------------------------------------------

def mad_outlier_rejection(time_series):
    """
    Robust Z-score outlier rejection using Median Absolute Deviation (MAD).
    Formula: M_i = 0.6745 * (x_i - x̂) / MAD ; reject where |M_i| > 3.5
    (Paper Section IV-A, equation 1)
    """
    if len(time_series) < 2:
        return time_series[:]

    median = _median(time_series)
    mad    = _median([abs(x - median) for x in time_series])

    if mad == 0:
        return time_series[:]

    filtered = [x for x in time_series
                if abs(0.6745 * (x - median) / mad) <= MAD_THRESHOLD]
    return filtered if filtered else time_series[:]


def z_normalization(time_series):
    """Z-score normalization: x' = (x - μ) / σ  (paper Section IV-A, step 2)"""
    mu    = _mean(time_series)
    sigma = _std(time_series)
    if sigma == 0:
        return [0.0] * len(time_series)
    return [(x - mu) / sigma for x in time_series]


def pad_or_truncate(ts, length, fill_val=0.0):
    """Pad with fill_val or truncate to exactly `length` samples."""
    if len(ts) < length:
        ts = ts + [fill_val] * (length - len(ts))
    return ts[:length]


def preprocess(raw_bif):
    """
    Full preprocessing pipeline (paper Section IV-A):
      1. MAD outlier rejection
      2. Pad/truncate to SEQ_LENGTH=20
      3. Z-normalization
    """
    ts = mad_outlier_rejection(raw_bif)
    fill = _median(ts) if ts else 0.0
    ts   = pad_or_truncate(ts, SEQ_LENGTH, fill_val=fill)
    ts   = z_normalization(ts)
    return ts


# -----------------------------------------------------------------------------
# Classification
# -----------------------------------------------------------------------------

def cv_classify(ts_normalized):
    """
    Fallback CV-based classifier when FCN unavailable.
    CUBIC (loss-based): sawtooth -> high σ/mean
    BBR   (model-based): paced   -> low  σ/mean

    Returns: 0 = Loss-based, 1 = Model-based
    """
    sigma = _std(ts_normalized)
    mu    = abs(_mean(ts_normalized)) + 1e-9  # avoid div-by-zero
    cv    = sigma / mu
    # CV threshold: from paper Fig. 4 / Section IV-C empirical analysis
    return 0 if cv > 0.15 else 1


def classify_flow(flow_key, raw_bif):
    """
    Full preprocessing + classification pipeline.
    Uses FCN if available, falls back to CV heuristic.

    Returns: (prediction: int, label: str, confidence: float)
      prediction: 0 = Loss-based, 1 = Model-based
    """
    ts_norm = preprocess(raw_bif)

    if FCN_MODEL is not None:
        # FCN path (paper-accurate)
        pred, confidence = predict_single(FCN_MODEL, raw_bif, seq_len=SEQ_LENGTH)
        method = 'FCN'
    else:
        # CV heuristic fallback
        pred       = cv_classify(ts_norm)
        confidence = abs(_std(ts_norm))
        method     = 'CV heuristic'

    label_str = 'Loss-based (CUBIC/Reno)' if pred == 0 else 'Model-based (BBR)'
    sigma = _std(ts_norm)
    mu    = _mean(ts_norm)

    print(f"[+] Flow {flow_key}")
    print(f"    BIF samples: {len(raw_bif)}  |  std={sigma:.4f}  |  mean={mu:.4f}")
    print(f"    Classifier: {method}  ->  {label_str}  (conf={confidence:.3f})")
    return pred, label_str, confidence


# -----------------------------------------------------------------------------
# Evaluation Metrics (paper Section VI)
# -----------------------------------------------------------------------------

def compute_jain_fairness(throughputs):
    """
    Jain's Fairness Index: J = (Σxi)² / (n · Σxi²)
    (Paper Section VI, equation 2)

    Args:
        throughputs: list of per-flow throughput values (Mbps or any consistent unit)
    Returns:
        float in [1/n, 1] where 1.0 = perfect fairness
    """
    n = len(throughputs)
    if n == 0:
        return 0.0
    sum_x  = sum(throughputs)
    sum_x2 = sum(x ** 2 for x in throughputs)
    if sum_x2 == 0:
        return 1.0
    return (sum_x ** 2) / (n * sum_x2)


def compute_link_utilization(throughputs, link_capacity_mbps=1000.0):
    """
    Link Utilization: U = Σthroughput / link_capacity
    (Paper Section VI)

    Args:
        throughputs: list of per-flow throughput values (Mbps)
        link_capacity_mbps: bottleneck link capacity in Mbps (default=1000)
    Returns:
        float in [0, 1] where 1.0 = full utilization
    """
    if link_capacity_mbps == 0:
        return 0.0
    return min(1.0, sum(throughputs) / link_capacity_mbps)


def compute_throughput_deviation(throughput_timeseries):
    """
    Normalized throughput deviation: σ/μ per flow, then mean across flows.
    Lower = more stable throughput over time.

    Args:
        throughput_timeseries: dict {flow_key: [mbps_t0, mbps_t1, ...]}
          OR list of flow throughput lists
    Returns:
        dict with per-flow and aggregate deviation
    """
    if isinstance(throughput_timeseries, dict):
        flows_data = list(throughput_timeseries.values())
        flow_keys  = list(throughput_timeseries.keys())
    else:
        flows_data = throughput_timeseries
        flow_keys  = [f'flow_{i}' for i in range(len(flows_data))]

    results = {}
    deviations = []
    for key, series in zip(flow_keys, flows_data):
        if len(series) < 2:
            dev = 0.0
        else:
            mu  = _mean(series)
            std = _std(series)
            dev = std / mu if mu > 0 else 0.0
        results[str(key)] = dev
        deviations.append(dev)

    results['_aggregate_mean_deviation'] = _mean(deviations) if deviations else 0.0
    return results


def compute_starvation_count(throughputs, link_capacity_mbps=1000.0):
    if not throughputs: return 0
    fair_share = link_capacity_mbps / len(throughputs)
    return sum(1 for t in throughputs if t < 0.10 * fair_share)

def print_metrics_table(scenario_name, throughputs_mbps, throughput_timeseries=None,
                         link_capacity_mbps=1000.0, drop_ratio=0.0):
    """
    Compute and print all three evaluation metrics in a formatted table.

    Args:
        scenario_name: str label (e.g. 'Baseline' or 'P4CCI')
        throughputs_mbps: list of per-flow average throughputs [Mbps]
        throughput_timeseries: dict/list of per-flow throughput over time intervals
        link_capacity_mbps: bottleneck link capacity
    """
    jfi  = compute_jain_fairness(throughputs_mbps)
    util = compute_link_utilization(throughputs_mbps, link_capacity_mbps)
    dev_results = compute_throughput_deviation(throughput_timeseries or
                                               {f'flow_{i}': [t] for i, t in enumerate(throughputs_mbps)})
    agg_dev = dev_results.get('_aggregate_mean_deviation', 0.0)
    starvation = compute_starvation_count(throughputs_mbps, link_capacity_mbps)

    print(f"\n{'='*55}")
    print(f"  Evaluation Metrics - {scenario_name}")
    print(f"{'='*55}")
    print(f"  Jain Fairness Index (JFI):   {jfi:.4f}  (target ~ 0.99)")
    print(f"  Link Efficiency/Util:        {util*100:.1f}%   (target ~ 95%)")
    print(f"  Starvation Count:            {starvation}  (target = 0)")
    print(f"  Throughput Deviation:        {agg_dev:.4f}  (lower = more stable)")
    print(f"  Packet Drop Ratio:           {drop_ratio*100:.4f}%")
    print(f"{'-'*55}")
    for i, tp in enumerate(throughputs_mbps):
        print(f"    Flow {i+1}: {tp:.2f} Mbps")
    total = sum(throughputs_mbps)
    print(f"    Total: {total:.2f} Mbps / {link_capacity_mbps:.0f} Mbps capacity")
    print(f"{'='*55}\n")

    return {'jfi': jfi, 'utilization': util, 'deviation': agg_dev, 'starvation': starvation, 'drop_ratio': drop_ratio}


# -----------------------------------------------------------------------------
# Switch Rule Insertion
# -----------------------------------------------------------------------------

def insert_switch_rule(flow_key, prediction, thrift_port=THRIFT_PORT):
    """
    Pushes an ACL table entry to the P4 switch via simple_switch_CLI.
    cca_class: 1 = Loss-based (CUBIC/Reno), 2 = Model-based (BBR)
    """
    src_ip, dst_ip, src_port, dst_port = flow_key
    class_id = 1 if prediction == 0 else 2

    cli_cmd = (
        f"table_add cca_classification set_cca_class "
        f"{src_ip} {dst_ip} {src_port} {dst_port} => {class_id}"
    )
    print(f"[*] Inserting rule: {cli_cmd}")

    try:
        proc = subprocess.Popen(
            [SWITCH_CMD, '--thrift-port', str(thrift_port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate(
            input=(cli_cmd + '\n').encode(), timeout=5
        )
        if stdout.strip():
            print(f"    CLI: {stdout.decode().strip()}")
    except FileNotFoundError:
        print(f"[!] {SWITCH_CMD} not found — rule not pushed (switch may not be running).")
    except subprocess.TimeoutExpired:
        print("[!] simple_switch_CLI timed out.")
    except Exception as e:
        print(f"[!] CLI error: {e}")


# -----------------------------------------------------------------------------
# BIF digest ingestion
# -----------------------------------------------------------------------------

def process_digest(digest_data):
    """
    Called for each BIF digest from the P4 switch.
    digest_data = (flow_id, bif, src_ip, dst_ip, src_port, dst_port)
    """
    _, bif, src_ip, dst_ip, src_port, dst_port = digest_data
    flow_key = (src_ip, dst_ip, src_port, dst_port)
    flow_bif_buffer[flow_key].append(float(bif))

    count = len(flow_bif_buffer[flow_key])
    if count % 5 == 0:
        print(f"    Collecting: {flow_key} — {count}/{SEQ_LENGTH} samples")

    if count == SEQ_LENGTH:
        pred, label, conf = classify_flow(flow_key, flow_bif_buffer[flow_key])
        insert_switch_rule(flow_key, pred)
        flow_bif_buffer[flow_key] = []   # reset for re-classification


# -----------------------------------------------------------------------------
# Synthetic self-test
# -----------------------------------------------------------------------------

def simulate_bif_stream(flow_key, pattern='cubic', seed=42):
    """Generates synthetic BIF samples for pipeline validation.
    Uses the same BDP-scaled generation logic as generate_dataset.py
    so the FCN sees patterns matching its training distribution."""
    import random
    rng = random.Random(seed)

    print(f"\n{'='*60}")
    print(f"[SIM] Simulating {pattern.upper()} flow: {flow_key}")
    print(f"{'='*60}")

    # Use realistic BDP-scaled BIF values (1 Gbps, 20ms RTT)
    bw_mbps = 1000.0
    rtt_ms  = 20.0
    bdp     = (bw_mbps * 1e6 / 8) * (rtt_ms / 1000.0)   # ~2.5 MB

    bif_vals = []
    for i in range(SEQ_LENGTH):
        if pattern == 'cubic':
            # CUBIC: sawtooth window growth (high variance BIF)
            window_period = 20
            ramp = (i % window_period) + 1
            base = (ramp / window_period) * bdp * 0.8
            bif  = base + rng.gauss(0, base * 0.1 + 500)
        elif pattern == 'reno':
            # Reno: steeper AIMD sawtooth
            window_period = 15
            ramp = (i % window_period) + 1
            base = (ramp / window_period) * bdp * 0.6
            bif  = base + rng.gauss(0, base * 0.12 + 300)
        else:
            # BBR: rate-paced, near-constant BIF at ~1 BDP
            base = bdp * 0.9
            bif  = base + rng.gauss(0, base * 0.02 + 150)
        bif_vals.append(max(0.0, bif))

    for bif in bif_vals:
        process_digest((0, bif, *flow_key))

    return bif_vals


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='P4CCI Controller -- Self-Test')
    parser.add_argument('--classify-only', action='store_true',
                        help='Only run flow classification self-test (no metrics)')
    args = parser.parse_args()

    print("=" * 60)
    print("P4CCI Controller -- Classification Self-Test")
    print("=" * 60)
    print(f"Python version  : {sys.version.split()[0]}")
    classifier_mode = "FCN (PyTorch)" if FCN_MODEL is not None else "CV Heuristic (fallback)"
    print(f"Classifier mode : {classifier_mode}")
    print(f"Sequence length : L={SEQ_LENGTH} BIF samples")
    print()

    # Experiment 1: CUBIC-like flow
    cubic_flow = ('10.0.1.1', '10.0.2.1', 5001, 80)
    simulate_bif_stream(cubic_flow, pattern='cubic')

    # Experiment 2: BBR-like flow
    bbr_flow = ('10.0.1.2', '10.0.2.2', 5002, 80)
    simulate_bif_stream(bbr_flow, pattern='bbr')

    # Experiment 3: Reno-like flow
    reno_flow = ('10.0.1.3', '10.0.2.3', 5003, 80)
    simulate_bif_stream(reno_flow, pattern='reno')

    print("\n[+] Self-test complete.")
    print("    Expected: CUBIC/Reno -> Loss-based, BBR -> Model-based")
    print("\n    To evaluate real experiments, use evaluate.py with iperf3 logs.")

