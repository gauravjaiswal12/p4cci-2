#!/usr/bin/env python3
"""
verify_metrics.py — Quick verification that all P4CCI metric functions are correct.
Run: python verify_metrics.py
No external dependencies required.
"""
import sys
sys.path.insert(0, '.')

from evaluate import compute_jain_fairness, compute_link_utilization, compute_throughput_deviation
from controller import (compute_jain_fairness as ctrl_jfi,
                        compute_link_utilization as ctrl_util,
                        compute_throughput_deviation as ctrl_dev,
                        print_metrics_table)

print("=" * 55)
print("  P4CCI Metric Verification")
print("=" * 55)

# ---- BASELINE: CUBIC starved by BBR ----
b_cubic_ts  = [22.4, 25.6, 20.6, 7.8, 7.3, 7.1, 14.5, 0.0, 15.1, 7.3]
b_bbr_ts    = [5.9, 18.0, 20.3, 11.3, 11.1, 11.3, 11.3, 11.1, 11.3, 11.3]
b_cubic_avg = sum(b_cubic_ts) / len(b_cubic_ts)
b_bbr_avg   = sum(b_bbr_ts)   / len(b_bbr_ts)

b_jfi  = compute_jain_fairness([b_cubic_avg, b_bbr_avg])
b_util = compute_link_utilization([b_cubic_avg, b_bbr_avg], 1000.0)
b_dev  = compute_throughput_deviation({'CUBIC': b_cubic_ts, 'BBR': b_bbr_ts})

print()
print("BASELINE (No Separation):")
print("  CUBIC avg:", round(b_cubic_avg, 2), "Mbps")
print("  BBR   avg:", round(b_bbr_avg,   2), "Mbps")
print("  Jain Fairness Index :", round(b_jfi,  4))
print("  Link Utilization    :", round(b_util * 100, 1), "%")
print("  Throughput Deviation:", round(b_dev['_aggregate'], 4))

# ---- P4CCI: Fair queue separation ----
p_cubic_ts  = [470, 480, 475, 478, 472, 476, 479, 481, 474, 477]
p_bbr_ts    = [478, 476, 480, 482, 475, 479, 477, 480, 476, 482]
p_cubic_avg = sum(p_cubic_ts) / len(p_cubic_ts)
p_bbr_avg   = sum(p_bbr_ts)   / len(p_bbr_ts)

p_jfi  = compute_jain_fairness([p_cubic_avg, p_bbr_avg])
p_util = compute_link_utilization([p_cubic_avg, p_bbr_avg], 1000.0)
p_dev  = compute_throughput_deviation({'CUBIC': p_cubic_ts, 'BBR': p_bbr_ts})

print()
print("P4CCI (With Separation):")
print("  CUBIC avg:", round(p_cubic_avg, 2), "Mbps")
print("  BBR   avg:", round(p_bbr_avg,   2), "Mbps")
print("  Jain Fairness Index :", round(p_jfi,  4))
print("  Link Utilization    :", round(p_util * 100, 1), "%")
print("  Throughput Deviation:", round(p_dev['_aggregate'], 4))

# ---- Comparison ----
print()
print("-" * 55)
print("  Comparison:")
print("  JFI:  baseline=" + str(round(b_jfi, 4)) +
      "  p4cci=" + str(round(p_jfi, 4)) +
      "  improvement=+" + str(round(p_jfi - b_jfi, 4)))
print("  Util: baseline=" + str(round(b_util*100, 1)) + "%" +
      "  p4cci=" + str(round(p_util*100, 1)) + "%" +
      "  improvement=+" + str(round((p_util - b_util)*100, 1)) + "%")
print("  Dev:  baseline=" + str(round(b_dev['_aggregate'], 4)) +
      "  p4cci=" + str(round(p_dev['_aggregate'], 4)) +
      "  reduction=" + str(round(b_dev['_aggregate'] - p_dev['_aggregate'], 4)))

# ---- Cross-check controller vs evaluate module ----
print()
print("-" * 55)
print("  Cross-module consistency check:")
r1 = ctrl_jfi([b_cubic_avg, b_bbr_avg])
r2 = ctrl_jfi([p_cubic_avg, p_bbr_avg])
ok1 = abs(r1 - b_jfi) < 0.0001
ok2 = abs(r2 - p_jfi) < 0.0001
print("  JFI (evaluate vs controller): " + ("MATCH" if ok1 and ok2 else "MISMATCH"))

r3 = ctrl_util([b_cubic_avg, b_bbr_avg], 1000.0)
ok3 = abs(r3 - b_util) < 0.001
print("  Util check: " + ("MATCH" if ok3 else "MISMATCH"))

# ---- Use controller print_metrics_table ----
print()
print("Controller print_metrics_table (Baseline):")
print_metrics_table('Baseline', [b_cubic_avg, b_bbr_avg],
                    {'CUBIC': b_cubic_ts, 'BBR': b_bbr_ts}, 1000.0)

print("Controller print_metrics_table (P4CCI):")
print_metrics_table('P4CCI', [p_cubic_avg, p_bbr_avg],
                    {'CUBIC (Q1)': p_cubic_ts, 'BBR (Q2)': p_bbr_ts}, 1000.0)

print()
all_ok = ok1 and ok2 and ok3
print("=" * 55)
print("  RESULT: " + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
print("=" * 55)
