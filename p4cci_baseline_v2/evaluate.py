#!/usr/bin/env python3
"""
evaluate.py - Standalone P4CCI metrics evaluator.

Computes and reports:
  1. Jain Fairness Index (JFI)         J = (Sumxi)Â² / (nÂ·SumxiÂ²)
  2. Link Utilization                  U = Sumthroughput / link_capacity
  3. Throughput Deviation              std/mean per flow, averaged

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
    Jain's Fairness Index: J(x1,...,xn) = (Sumxi)Â² / (n Â· SumxiÂ²)

    Range: [1/n, 1.0] where 1.0 = perfect fairness.
    Paper target (P4CCI with separation): JFI â‰ˆ 0.99
    Paper baseline (no separation):       JFI â‰ˆ 0.70

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
    Link Utilization: U = Sumthroughput / link_capacity

    Range: [0, 1] where 1.0 = 100% utilization.
    Paper target (P4CCI): U â‰ˆ 0.95 (95%)

    Args:
        throughputs: list of per-flow Mbps values
        link_capacity_mbps: bottleneck link capacity (default 1000 Mbps = 1 Gbps)
    """
    if link_capacity_mbps <= 0:
        return 0.0
    return min(1.0, sum(throughputs) / link_capacity_mbps)


def compute_throughput_deviation(timeseries_dict):
    """
    Throughput Deviation: mean of (std/mean) per flow across all time intervals.

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


def compute_starvation_count(throughputs, link_capacity_mbps=1000.0):
    """
    Starvation Count: Tracks flows receiving < 10% of their fair share.
    """
    n = len(throughputs)
    if n == 0:
        return 0
    fair_share = link_capacity_mbps / n
    threshold = 0.10 * fair_share
    return sum(1 for t in throughputs if t < threshold)


def compute_packet_drop_ratio(drops, total_packets):
    """
    Packet Drop Ratio: Drops / (Total Packets + Drops).
    Normally computed from P4 egress pipeline registers. Here we use retransmissions as a proxy.
    """
    if total_packets + drops == 0:
        return 0.0
    return drops / (total_packets + drops)


def compute_throughput_rates(forwarded_bytes_ts, interval_sec=1.0):
    """
    Throughput / Flow Rates (Mbps): Derived from forwarded_bytes telemetry register.
    """
    return [(b * 8) / (interval_sec * 1e6) for b in forwarded_bytes_ts]


# -----------------------------------------------------------------------------
# iperf3 log parsers
# -----------------------------------------------------------------------------

def parse_iperf3_json(log_path):
    """
    Parse iperf3 JSON output (generated with `iperf3 -J` flag).

    Returns:
        avg_mbps: float - overall average throughput
        intervals_mbps: list of float - per-interval throughput (Mbps)
        retransmits: int - total retransmissions
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
    starvation = compute_starvation_count(avg_mbps, link_capacity_mbps)
    
    # Estimate total packets for Drop Ratio calculation (assuming ~1500B MTU)
    total_retx = sum(d['retx'] for d in flow_data.values())
    duration_sec = len(list(ts_dict.values())[0]) * 5 if ts_dict and list(ts_dict.values())[0] else 60
    total_bits = sum(avg_mbps) * 1e6 * duration_sec
    estimated_total_packets = total_bits / (1500 * 8)
    drop_ratio = compute_packet_drop_ratio(total_retx, estimated_total_packets)

    print(f"\n{'='*60}")
    print(f"  Scenario: {scenario_name}")
    print(f"{'='*60}")

    for name, d in flow_data.items():
        print(f"  {name:<20} avg={d['avg']:>8.2f} Mbps  retx={d['retx']:>5}")

    print(f"{'-'*60}")
    total = sum(avg_mbps)
    print(f"  {'Total throughput':<20} {total:>8.2f} Mbps / {link_capacity_mbps:.0f} Mbps capacity")
    print(f"{'-'*60}")
    print(f"  Jain Fairness Index  : {jfi:.4f}   {'[OK]' if jfi >= 0.95 else '[!] '} (target >= 0.95)")
    print(f"  Link Efficiency/Util : {util*100:.1f}%   {'[OK]' if util >= 0.90 else '[!] '} (target >= 90%)")
    print(f"  Starvation Count     : {starvation}      {'[OK]' if starvation == 0 else '[!] '} (target = 0)")
    print(f"  Throughput Deviation : {dev['_aggregate']:.4f}   {'[OK]' if dev['_aggregate'] <= 0.15 else '[!] '} (target <= 0.15)")
    print(f"  Packet Drop Ratio    : {drop_ratio*100:.4f}%")
    print()
    print("  Per-flow throughput deviation (std/mean):")
    for name, cov in dev.items():
        if name != '_aggregate':
            print(f"    {name:<20}: {cov:.4f}")
    print(f"{'='*60}")

    return {'jfi': jfi, 'utilization': util, 'deviation': dev['_aggregate'],
            'total_mbps': total, 'starvation': starvation, 'drop_ratio': drop_ratio}


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
        ('Link Efficiency (%)', 'utilization_pct', '{:.1f}%'),
        ('Starvation Count', 'starvation', '{:.0f}'),
        ('Packet Drop Ratio (%)', 'drop_ratio_pct', '{:.4f}%'),
        ('Throughput Deviation', 'deviation', '{:.4f}'),
        ('Total Throughput (Mbps)', 'total_mbps', '{:.1f}'),
    ]

    # Add derived metrics views
    for k, v in results.items():
        v['utilization_pct'] = v['utilization'] * 100
        v['drop_ratio_pct'] = v.get('drop_ratio', 0) * 100

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
            
            # For starvation, drop ratio, and deviation, lower is better, so negate for 'improvement'
            if real_key in ['starvation', 'drop_ratio', 'deviation']:
                dv = -dv

            sign = '+' if dv >= 0 else ''
            extra = '%' if 'pct' in key else ''
            # Format nicely
            if real_key == 'starvation':
                print(f"  {sign}{dv:.0f}".rjust(14), end='')
            else:
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
# Demo with synthetic data (DISABLED)
# -----------------------------------------------------------------------------

def run_demo():
    """
    Demonstrate all metrics. Synthetic data was previously used here,
    but has been removed to ensure all metrics reflect actual experimentation.
    Please run this script with real iperf logs to evaluate performance.
    """
    print("=" * 60)
    print("  P4CCI Metrics Evaluator")
    print("=" * 60)
    print("  [INFO] Synthetic demo mode is disabled.")
    print("  To evaluate performance, please provide actual log files:")
    print("    python3 evaluate.py --cubic /tmp/cubic.log --bbr /tmp/bbr.log")
    print("=" * 60)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='P4CCI Metrics Evaluator â€” JFI, Link Utilization, Throughput Deviation',
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
