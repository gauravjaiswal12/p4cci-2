#!/usr/bin/env python3
"""
evaluate.py — Standalone P4CCI metrics evaluator.

Computes and reports:
  1. Jain Fairness Index (JFI)         J = (Σxi)² / (n·Σxi²)
  2. Link Utilization                  U = Σthroughput / link_capacity
  3. Throughput Deviation              σ/μ per flow, averaged

Usage examples:
    # Demo with synthetic data (no logs required):
    python3 evaluate.py --demo

    # Evaluate from iperf3 JSON logs produced by topology.py:
    python3 evaluate.py --cubic /tmp/cubic_baseline.json --bbr /tmp/bbr_baseline.json

    # Compare baseline vs p4cci runs:
    python3 evaluate.py \\
        --cubic-baseline /tmp/cubic_baseline.json \\
        --bbr-baseline   /tmp/bbr_baseline.json  \\
        --cubic-p4cci    /tmp/cubic_p4cci.json   \\
        --bbr-p4cci      /tmp/bbr_p4cci.json

    # Evaluate from plain text iperf3 logs:
    python3 evaluate.py --cubic /tmp/cubic_flow.log --bbr /tmp/bbr_flow.log
"""

import os
import sys
import json
import math
import argparse
from collections import defaultdict


# -----------------------------------------------------------------------------
# Pure-Python math utilities
# -----------------------------------------------------------------------------

def _mean(data):
    return sum(data) / len(data) if data else 0.0

def _std(data):
    if len(data) < 2:
        return 0.0
    mu = _mean(data)
    return math.sqrt(sum((x - mu) ** 2 for x in data) / len(data))

def _median(data):
    s = sorted(data)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 != 0 else (s[mid-1] + s[mid]) / 2.0


# -----------------------------------------------------------------------------
# Metric computations (paper Section VI)
# -----------------------------------------------------------------------------

def compute_jain_fairness(throughputs):
    """
    Jain's Fairness Index: J(x1,...,xn) = (Σxi)² / (n · Σxi²)

    Range: [1/n, 1.0] where 1.0 = perfect fairness.
    Paper target (P4CCI with separation): JFI ≈ 0.99
    Paper baseline (no separation):       JFI ≈ 0.70

    Args:
        throughputs: list of per-flow average throughput values (any unit, consistent)
    """
    n = len(throughputs)
    if n == 0:
        return 0.0
    sum_x  = sum(throughputs)
    sum_x2 = sum(x**2 for x in throughputs)
    if sum_x2 == 0:
        return 1.0
    return (sum_x ** 2) / (n * sum_x2)


def compute_link_utilization(throughputs, link_capacity_mbps=1000.0):
    """
    Link Utilization: U = Σthroughput / link_capacity

    Range: [0, 1] where 1.0 = 100% utilization.
    Paper target (P4CCI): U ≈ 0.95 (95%)

    Args:
        throughputs: list of per-flow Mbps values
        link_capacity_mbps: bottleneck link capacity (default 1000 Mbps = 1 Gbps)
    """
    if link_capacity_mbps <= 0:
        return 0.0
    return min(1.0, sum(throughputs) / link_capacity_mbps)


def compute_throughput_deviation(timeseries_dict):
    """
    Throughput Deviation: mean of (σ/μ) per flow across all time intervals.

    Lower = more stable throughput (less oscillation).
    CUBIC under BBR competition has high deviation; after separation it drops.

    Args:
        timeseries_dict: {flow_name: [mbps_interval_0, mbps_interval_1, ...]}
    Returns:
        dict with per-flow CoV and aggregate mean
    """
    results = {}
    per_flow_cov = []

    for flow_name, series in timeseries_dict.items():
        series = [v for v in series if v >= 0]  # drop negatives
        if not series:
            results[flow_name] = 0.0
            continue
        mu  = _mean(series)
        std = _std(series)
        cov = std / mu if mu > 0 else 0.0
        results[flow_name] = cov
        per_flow_cov.append(cov)

    results['_aggregate'] = _mean(per_flow_cov)
    return results


# -----------------------------------------------------------------------------
# iperf3 log parsers
# -----------------------------------------------------------------------------

def parse_iperf3_json(log_path):
    """
    Parse iperf3 JSON output (generated with `iperf3 -J` flag).

    Returns:
        avg_mbps: float — overall average throughput
        intervals_mbps: list of float — per-interval throughput (Mbps)
        retransmits: int — total retransmissions
    """
    try:
        with open(log_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[!] Log file not found: {log_path}")
        return 0.0, [], 0
    except json.JSONDecodeError:
        # Try text fallback
        return parse_iperf3_text(log_path)

    intervals    = data.get('intervals', [])
    mbps_list    = []
    retransmits  = 0

    for iv in intervals:
        try:
            bits = iv['sum']['bits_per_second']
            mbps_list.append(bits / 1e6)
            retransmits += iv['sum'].get('retransmits', 0)
        except KeyError:
            continue

    # Final summary from iperf3 end section
    try:
        end_bits = data['end']['sum_sent']['bits_per_second']
        avg_mbps = end_bits / 1e6
    except (KeyError, TypeError):
        avg_mbps = _mean(mbps_list)

    return avg_mbps, mbps_list, retransmits


def parse_iperf3_text(log_path):
    """
    Fallback: parse plain-text iperf3 output (without -J flag).

    Returns: avg_mbps, intervals_mbps, retransmits
    """
    mbps_list   = []
    retransmits = 0

    try:
        with open(log_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return 0.0, [], 0

    for line in lines:
        parts = line.split()
        for i, p in enumerate(parts):
            if 'Mbits/sec' in p or 'Gbits/sec' in p:
                try:
                    val = float(parts[i - 1])
                    if 'Gbits/sec' in p:
                        val *= 1000.0
                    # Skip the final summary duplicate line
                    if 'sender' not in line and 'receiver' not in line:
                        mbps_list.append(val)
                except (ValueError, IndexError):
                    continue
        # Count retransmits
        if 'Retr' in line or 'retr' in line:
            for p in parts:
                try:
                    retransmits += int(p)
                    break
                except ValueError:
                    continue

    avg = _mean(mbps_list)
    return avg, mbps_list, retransmits


def load_flow_data(log_path):
    """Auto-detect JSON vs text format and parse."""
    if not os.path.exists(log_path):
        print(f"[!] File not found: {log_path}")
        return 0.0, [], 0

    try:
        with open(log_path) as f:
            first_char = f.read(1)
        if first_char == '{':
            return parse_iperf3_json(log_path)
        else:
            return parse_iperf3_text(log_path)
    except Exception as e:
        print(f"[!] Error reading {log_path}: {e}")
        return 0.0, [], 0


# -----------------------------------------------------------------------------
# Report printer
# -----------------------------------------------------------------------------

def print_scenario_report(scenario_name, flow_data, link_capacity_mbps=1000.0):
    """
    Print a full metrics report for one scenario.

    Args:
        scenario_name: str label
        flow_data: dict {flow_name: {'avg': float, 'ts': list, 'retx': int}}
        link_capacity_mbps: float

    Returns:
        dict with computed metric values
    """
    avg_mbps = [d['avg'] for d in flow_data.values()]
    ts_dict  = {name: d['ts'] for name, d in flow_data.items()}

    jfi  = compute_jain_fairness(avg_mbps)
    util = compute_link_utilization(avg_mbps, link_capacity_mbps)
    dev  = compute_throughput_deviation(ts_dict)

    print(f"\n{'='*60}")
    print(f"  Scenario: {scenario_name}")
    print(f"{'='*60}")

    for name, d in flow_data.items():
        print(f"  {name:<20} avg={d['avg']:>8.2f} Mbps  retx={d['retx']:>5}")

    print(f"{'-'*60}")
    total = sum(avg_mbps)
    print(f"  {'Total throughput':<20} {total:>8.2f} Mbps / {link_capacity_mbps:.0f} Mbps capacity")
    print(f"{'-'*60}")
    print(f"  Jain Fairness Index  : {jfi:.4f}   {'[OK]' if jfi >= 0.95 else '[!]️ '} (target ≥ 0.95)")
    print(f"  Link Utilization     : {util*100:.1f}%   {'[OK]' if util >= 0.90 else '[!]️ '} (target ≥ 90%)")
    print(f"  Throughput Deviation : {dev['_aggregate']:.4f}   {'[OK]' if dev['_aggregate'] <= 0.15 else '[!]️ '} (target ≤ 0.15)")
    print()
    print("  Per-flow throughput deviation (σ/μ):")
    for name, cov in dev.items():
        if name != '_aggregate':
            print(f"    {name:<20}: {cov:.4f}")
    print(f"{'='*60}")

    return {'jfi': jfi, 'utilization': util, 'deviation': dev['_aggregate'],
            'total_mbps': total}


def print_comparison(results):
    """Print side-by-side comparison table for multiple scenarios."""
    if len(results) < 2:
        return

    keys = list(results.keys())
    print(f"\n{'='*60}")
    print("  Comparison Summary")
    print(f"{'='*60}")
    print(f"  {'Metric':<30}", end='')
    for k in keys:
        print(f"  {k:>12}", end='')
    print()
    print(f"  {'-'*28}", end='')
    for _ in keys:
        print(f"  {'-'*12}", end='')
    print()

    metrics = [
        ('Jain Fairness Index', 'jfi', '{:.4f}'),
        ('Link Utilization (%)', 'utilization_pct', '{:.1f}%'),
        ('Throughput Deviation', 'deviation', '{:.4f}'),
        ('Total Throughput (Mbps)', 'total_mbps', '{:.1f}'),
    ]

    # Add utilization_pct view
    for k, v in results.items():
        v['utilization_pct'] = v['utilization'] * 100

    for label, key, fmt in metrics:
        print(f"  {label:<30}", end='')
        for k in keys:
            val = results[k].get(key, 0)
            print(f"  {fmt.format(val):>12}", end='')
        print()

    # Delta row (if exactly 2 scenarios)
    if len(keys) == 2:
        a, b = results[keys[0]], results[keys[1]]
        print(f"{'-'*60}")
        print(f"  {'Improvement (-> ' + keys[1] + ')':<30}", end='')
        for label, key, _ in metrics:
            real_key = key.replace('_pct', '')
            dv = b.get(real_key, 0) - a.get(real_key, 0)
            sign = '+' if dv >= 0 else ''
            extra = '%' if 'pct' in key else ''
            print(f"  {sign}{dv:.4f}{extra}".rjust(14), end='')
        print()
    print(f"{'='*60}\n")


# -----------------------------------------------------------------------------
# ASCII chart helper
# -----------------------------------------------------------------------------

def ascii_throughput_chart(timeseries_dict, title='Throughput over Time'):
    """Print a simple ASCII bar chart of per-interval throughputs."""
    print(f"\n  {title}")
    print(f"  {'-'*50}")

    all_vals = [v for ts in timeseries_dict.values() for v in ts]
    max_v = max(all_vals) if all_vals else 1.0
    bar_width = 30

    for name, ts in timeseries_dict.items():
        print(f"  {name}")
        for i, v in enumerate(ts):
            bar_len = int((v / max_v) * bar_width)
            bar = '#' * bar_len
            print(f"    t={i*5:>3}s |{bar:<{bar_width}}| {v:>7.1f} Mbps")
        print()
    print(f"  {'-'*50}")


# -----------------------------------------------------------------------------
# Demo with synthetic data
# -----------------------------------------------------------------------------

def run_demo():
    """
    Demonstrate all metrics using synthetic iperf3 data
    matching the paper's expected results.
    """
    print("=" * 60)
    print("  P4CCI Metrics Demo — Synthetic Data")
    print("=" * 60)
    print("  (Reproducing expected results from paper Section VI)")

    # -- Baseline scenario ---------------------------------------------------
    # CUBIC starved when competing with BBR (paper Fig. 5)
    baseline_cubic_ts = [22.4, 25.6, 20.6, 7.8, 7.3, 7.1, 14.5, 0.0, 15.1, 7.3]
    baseline_bbr_ts   = [5.9, 18.0, 20.3, 11.3, 11.1, 11.3, 11.3, 11.1, 11.3, 11.3]

    baseline_data = {
        'CUBIC (h1->h3, port 5001)': {
            'avg': _mean(baseline_cubic_ts),
            'ts':  baseline_cubic_ts,
            'retx': 839,
        },
        'BBR   (h2->h4, port 5002)': {
            'avg': _mean(baseline_bbr_ts),
            'ts':  baseline_bbr_ts,
            'retx': 1469,
        },
    }

    ascii_throughput_chart(
        {'CUBIC': baseline_cubic_ts, 'BBR': baseline_bbr_ts},
        title='Baseline: CUBIC vs BBR (No Separation)'
    )
    baseline_metrics = print_scenario_report(
        'Baseline — No CCA-Aware Separation',
        baseline_data,
        link_capacity_mbps=1000.0,
    )

    # -- P4CCI scenario ----------------------------------------------------
    # After queue separation, flows share bandwidth fairly (paper Fig. 6)
    p4cci_cubic_ts = [470, 480, 475, 478, 472, 476, 479, 481, 474, 477]
    p4cci_bbr_ts   = [478, 476, 480, 482, 475, 479, 477, 480, 476, 482]

    p4cci_data = {
        'CUBIC  (Q1 — loss-based)': {
            'avg': _mean(p4cci_cubic_ts),
            'ts':  p4cci_cubic_ts,
            'retx': 42,
        },
        'BBR    (Q2 — model-based)': {
            'avg': _mean(p4cci_bbr_ts),
            'ts':  p4cci_bbr_ts,
            'retx': 15,
        },
    }

    ascii_throughput_chart(
        {'CUBIC (Q1)': p4cci_cubic_ts, 'BBR (Q2)': p4cci_bbr_ts},
        title='P4CCI: CUBIC vs BBR (With Queue Separation)'
    )
    p4cci_metrics = print_scenario_report(
        'P4CCI — CCA-Aware Queue Separation',
        p4cci_data,
        link_capacity_mbps=1000.0,
    )

    # -- Comparison --------------------------------------------------------
    print_comparison({'Baseline': baseline_metrics, 'P4CCI': p4cci_metrics})


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='P4CCI Metrics Evaluator — JFI, Link Utilization, Throughput Deviation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--demo', action='store_true',
                        help='Run demo with synthetic data (no logs required)')
    parser.add_argument('--cubic', metavar='LOG',
                        help='iperf3 log for CUBIC flow (JSON or plain text)')
    parser.add_argument('--bbr', metavar='LOG',
                        help='iperf3 log for BBR flow (JSON or plain text)')
    parser.add_argument('--cubic-baseline', metavar='LOG',
                        help='Baseline CUBIC log for comparison mode')
    parser.add_argument('--bbr-baseline', metavar='LOG',
                        help='Baseline BBR log for comparison mode')
    parser.add_argument('--cubic-p4cci', metavar='LOG',
                        help='P4CCI CUBIC log for comparison mode')
    parser.add_argument('--bbr-p4cci', metavar='LOG',
                        help='P4CCI BBR log for comparison mode')
    parser.add_argument('--capacity', type=float, default=1000.0,
                        help='Link capacity in Mbps (default: 1000)')
    args = parser.parse_args()

    # Demo mode
    if args.demo or (not args.cubic and not args.cubic_baseline):
        run_demo()
        sys.exit(0)

    # Single scenario mode
    if args.cubic and args.bbr:
        cubic_avg, cubic_ts, cubic_retx = load_flow_data(args.cubic)
        bbr_avg,   bbr_ts,   bbr_retx   = load_flow_data(args.bbr)
        flow_data = {
            'CUBIC': {'avg': cubic_avg, 'ts': cubic_ts, 'retx': cubic_retx},
            'BBR':   {'avg': bbr_avg,   'ts': bbr_ts,   'retx': bbr_retx},
        }
        ascii_throughput_chart({'CUBIC': cubic_ts, 'BBR': bbr_ts})
        print_scenario_report('Experiment', flow_data, args.capacity)
        sys.exit(0)

    # Comparison mode
    if args.cubic_baseline and args.bbr_baseline and args.cubic_p4cci and args.bbr_p4cci:
        results = {}

        ca, cts, cr = load_flow_data(args.cubic_baseline)
        ba, bts, br = load_flow_data(args.bbr_baseline)
        baseline_data = {
            'CUBIC': {'avg': ca, 'ts': cts, 'retx': cr},
            'BBR':   {'avg': ba, 'ts': bts, 'retx': br},
        }
        ascii_throughput_chart({'CUBIC': cts, 'BBR': bts},
                               title='Baseline Throughput')
        results['Baseline'] = print_scenario_report(
            'Baseline (No Separation)', baseline_data, args.capacity)

        ca, cts, cr = load_flow_data(args.cubic_p4cci)
        ba, bts, br = load_flow_data(args.bbr_p4cci)
        p4cci_data = {
            'CUBIC (Q1)': {'avg': ca, 'ts': cts, 'retx': cr},
            'BBR (Q2)':   {'avg': ba, 'ts': bts, 'retx': br},
        }
        ascii_throughput_chart({'CUBIC (Q1)': cts, 'BBR (Q2)': bts},
                               title='P4CCI Throughput')
        results['P4CCI'] = print_scenario_report(
            'P4CCI (CCA-Aware Separation)', p4cci_data, args.capacity)

        print_comparison(results)
        sys.exit(0)

    parser.print_help()
