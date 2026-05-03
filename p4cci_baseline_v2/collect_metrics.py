#!/usr/bin/env python3
"""
collect_metrics.py -- Unified metric collector for P4CCI experiments.

Parses per-flow iperf (v2) log files, computes metrics matching the KBCS v2
CSV format (except karma fields), and appends one row per run.

Metrics computed (same formulas as KBCS collect_metrics.py):
  - Jain's Fairness Index (JFI)   J = (sum_x)^2 / (n * sum_x2)
  - Aggregate Throughput (Mbps)
  - Link Utilization (%)          agg_throughput / bottleneck_capacity
  - Packet Drop Ratio (PDR %)     estimated from retransmissions

Topologies supported:
  - dumbbell: 4 flows (h1-h4 -> h5-h8), bottleneck = 3 Mbps
  - cross:    8 flows (h1-h8 -> h9-h12), bottleneck = 6 Mbps (2 x 3 Mbps links)

Usage (called by test_suite_p4cci.sh):
    python3 collect_metrics.py --run 1 --topo dumbbell --mode p4cci \\
        --duration 60 --log-dir logs/run_1
"""

import os
import re
import csv
import argparse
from datetime import datetime

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'results')

# Bottleneck capacity (250 pps * 1500 B * 8 ~ 3 Mbps per link)
CAPACITY_DUMBBELL = 3.0    # 1 bottleneck link
CAPACITY_CROSS    = 6.0    # 2 bottleneck links per ingress switch

# Flow definitions per topology
DUMBBELL_FLOWS = [
    ('h1', 'cubic'),
    ('h2', 'bbr'),
    ('h3', 'vegas'),
    ('h4', 'illinois'),
]

CROSS_FLOWS = [
    ('h1', 'cubic'),
    ('h2', 'bbr'),
    ('h3', 'vegas'),
    ('h4', 'illinois'),
    ('h5', 'cubic'),
    ('h6', 'bbr'),
    ('h7', 'vegas'),
    ('h8', 'illinois'),
]


# ---- Metric formulas (matching KBCS) ----------------------------------------

def jain_fairness(values):
    """J = (sum_x)^2 / (n * sum_x^2)  --  same as KBCS collect_metrics.py."""
    active = [x for x in values if x > 0]
    n = len(active)
    if n == 0:
        return 0.0
    sum_x  = sum(active)
    sum_x2 = sum(x * x for x in active)
    return (sum_x ** 2) / (n * sum_x2) if sum_x2 > 0 else 1.0


def packet_drop_ratio(retransmits, fwd_bytes):
    """PDR = drop_bytes / (fwd_bytes + drop_bytes) * 100  --  matches KBCS."""
    drop_bytes = retransmits * 1500
    total = fwd_bytes + drop_bytes
    return (drop_bytes / total * 100.0) if total > 0 else 0.0


# ---- iperf v2 log parser -----------------------------------------------------

def parse_iperf_log(log_path):
    """
    Parse iperf v2 plain-text output.
    Returns (avg_mbps, retransmits).

    Example summary line:
      [  1]  0.0-60.0 sec  215 MBytes  30.1 Mbits/sec
    """
    if not os.path.exists(log_path):
        return 0.0, 0

    try:
        with open(log_path) as f:
            content = f.read()
    except Exception:
        return 0.0, 0

    if not content.strip():
        return 0.0, 0

    lines = content.strip().splitlines()

    # Check for connection errors
    if 'Connection refused' in content and 'connected with' not in content:
        return 0.0, 0

    # Find throughput -- last line with "bits/sec"
    mbps = 0.0
    for line in reversed(lines):
        m = re.search(r'([\d.]+)\s+(K|M|G)bits/sec', line)
        if m:
            val = float(m.group(1))
            unit = m.group(2)
            if unit == 'K':
                val /= 1000.0     # Kbits -> Mbits
            elif unit == 'G':
                val *= 1000.0     # Gbits -> Mbits
            mbps = val
            break

    # iperf v2 doesn't report retransmits by default
    retransmits = 0

    return mbps, retransmits


# ---- Main collection logic ---------------------------------------------------

def collect_and_write(run_num, duration_s, mode, topo, log_dir):
    """Parse logs, compute metrics, write CSV row."""

    # Select flow list and capacity based on topology
    if topo == 'cross':
        flow_list = CROSS_FLOWS
        capacity  = CAPACITY_CROSS
    else:
        flow_list = DUMBBELL_FLOWS
        capacity  = CAPACITY_DUMBBELL

    num_flows = len(flow_list)

    # Determine CSV file path
    csv_file = os.path.join(RESULTS_DIR, f'{mode}_{topo}_results.csv')

    print(f'\n[collect_metrics] Run {run_num} | topo={topo} | mode={mode} | '
          f'duration={duration_s}s | flows={num_flows}')
    print(f'  Log directory: {log_dir}')
    print(f'  CSV output:    {csv_file}')

    # Parse each flow's iperf log
    flow_mbps  = []
    flow_fwd   = []   # forwarded bytes (computed from throughput)
    flow_drops = []   # retransmits (proxy for drops)

    for send_name, cca in flow_list:
        log_path = os.path.join(log_dir, f'{send_name}_{cca}.txt')
        mbps, retx = parse_iperf_log(log_path)

        # Compute forwarded bytes from throughput (same approach as reading registers)
        fwd_bytes = int((mbps * 1e6 * duration_s) / 8.0)

        flow_mbps.append(mbps)
        flow_fwd.append(fwd_bytes)
        flow_drops.append(retx)

        status = 'OK' if mbps > 0 else 'MISSING'
        print(f'  {send_name} ({cca:>8}): {mbps:>8.4f} Mbps  '
              f'fwd={fwd_bytes:>12}  drops={retx:>4}  [{status}]')

    # Compute aggregate metrics (matching KBCS formulas exactly)
    agg_mbps   = sum(flow_mbps)
    jfi        = jain_fairness(flow_fwd)   # JFI on bytes, same as KBCS
    link_util  = min((agg_mbps / capacity) * 100.0, 100.0)
    total_fwd  = sum(flow_fwd)
    total_drops = sum(flow_drops)
    pdr        = packet_drop_ratio(total_drops, total_fwd)

    print(f'\n  -- Results --')
    print(f'  JFI              : {jfi:.4f}')
    print(f'  Agg Throughput   : {agg_mbps:.4f} Mbps')
    print(f'  Link Utilization : {link_util:.2f}%')
    print(f'  Packet Drop Ratio: {pdr:.4f}%')

    # ---- Build CSV row (KBCS-compatible format) ----
    row = {
        'run':                run_num,
        'topology':           topo,
        'duration':           duration_s,
        'num_flows':          num_flows,
        'jfi':                round(jfi, 4),
        'agg_throughput_mbps': round(agg_mbps, 4),
        'link_util_pct':      round(link_util, 2),
        'pdr_pct':            round(pdr, 4),
    }

    # Per-flow data (same column names as KBCS)
    for i in range(num_flows):
        row[f'fwd_{i+1}']   = flow_fwd[i]
        row[f'drops_{i+1}'] = flow_drops[i]

    # Write CSV
    os.makedirs(RESULTS_DIR, exist_ok=True)
    fieldnames = list(row.keys())
    file_exists = os.path.exists(csv_file) and os.path.getsize(csv_file) > 0

    with open(csv_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f'\n  Row appended to {csv_file}')
    return row


def main():
    parser = argparse.ArgumentParser(
        description='P4CCI metric collector (KBCS-compatible format)')
    parser.add_argument('--run',      type=int, required=True)
    parser.add_argument('--topo',     type=str, required=True,
                        choices=['dumbbell', 'cross'])
    parser.add_argument('--mode',     type=str, default='p4cci',
                        choices=['baseline', 'p4cci'])
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--log-dir',  type=str, required=True,
                        help='Directory containing h1_cubic.txt, etc.')
    args = parser.parse_args()

    collect_and_write(
        run_num    = args.run,
        duration_s = args.duration,
        mode       = args.mode,
        topo       = args.topo,
        log_dir    = args.log_dir,
    )


if __name__ == '__main__':
    main()
