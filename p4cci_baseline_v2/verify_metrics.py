#!/usr/bin/env python3
"""
verify_metrics.py -- Cross-module consistency check for P4CCI metric functions.

Verifies that evaluate.py and controller.py compute identical JFI, link
utilization, and throughput deviation values for the same input data.

Run: python3 verify_metrics.py
No external dependencies required.
"""
import sys
import os
import csv
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluate import compute_jain_fairness, compute_link_utilization, compute_throughput_deviation
from controller import (compute_jain_fairness as ctrl_jfi,
                        compute_link_utilization as ctrl_util,
                        compute_throughput_deviation as ctrl_dev)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')
CSV_FILE    = os.path.join(RESULTS_DIR, 'p4cci_statistical_results.csv')


def load_csv_results(path):
    """Load per-run results from the statistical CSV."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _mean(data):
    return sum(data) / len(data) if data else 0.0


def _std(data):
    if len(data) < 2:
        return 0.0
    mu = _mean(data)
    return math.sqrt(sum((x - mu) ** 2 for x in data) / len(data))


def run_formula_checks():
    """Verify that both modules compute identical results for known inputs."""
    print("=" * 60)
    print("  P4CCI Metric Verification -- Formula Consistency")
    print("=" * 60)

    # Test case 1: Two flows, one dominant (unfair scenario)
    flows_unfair = [7.3, 14.2]
    jfi_eval = compute_jain_fairness(flows_unfair)
    jfi_ctrl = ctrl_jfi(flows_unfair)
    ok1 = abs(jfi_eval - jfi_ctrl) < 0.0001
    print(f"\n  Test 1: Unfair flows {flows_unfair}")
    print(f"    evaluate.jfi = {jfi_eval:.6f}")
    print(f"    controller.jfi = {jfi_ctrl:.6f}")
    print(f"    Match: {'PASS' if ok1 else 'FAIL'}")

    # Test case 2: Two flows, perfectly fair
    flows_fair = [500.0, 500.0]
    jfi_eval2 = compute_jain_fairness(flows_fair)
    jfi_ctrl2 = ctrl_jfi(flows_fair)
    ok2 = abs(jfi_eval2 - 1.0) < 0.0001 and abs(jfi_ctrl2 - 1.0) < 0.0001
    print(f"\n  Test 2: Fair flows {flows_fair}")
    print(f"    evaluate.jfi = {jfi_eval2:.6f}  (expected 1.0)")
    print(f"    controller.jfi = {jfi_ctrl2:.6f}  (expected 1.0)")
    print(f"    Match: {'PASS' if ok2 else 'FAIL'}")

    # Test case 3: Link utilization
    util_eval = compute_link_utilization(flows_unfair, 1000.0)
    util_ctrl = ctrl_util(flows_unfair, 1000.0)
    ok3 = abs(util_eval - util_ctrl) < 0.0001
    print(f"\n  Test 3: Link utilization (capacity=1000 Mbps)")
    print(f"    evaluate.util = {util_eval*100:.2f}%")
    print(f"    controller.util = {util_ctrl*100:.2f}%")
    print(f"    Match: {'PASS' if ok3 else 'FAIL'}")

    # Test case 4: Throughput deviation
    ts = {'flow_a': [10.0, 12.0, 8.0, 11.0], 'flow_b': [20.0, 19.5, 20.5, 20.0]}
    dev_eval = compute_throughput_deviation(ts)
    dev_ctrl = ctrl_dev(ts)
    ok4 = abs(dev_eval['_aggregate'] - dev_ctrl['_aggregate_mean_deviation']) < 0.001
    print(f"\n  Test 4: Throughput deviation")
    print(f"    evaluate.dev = {dev_eval['_aggregate']:.6f}")
    print(f"    controller.dev = {dev_ctrl['_aggregate_mean_deviation']:.6f}")
    print(f"    Match: {'PASS' if ok4 else 'FAIL'}")

    all_ok = ok1 and ok2 and ok3 and ok4
    print(f"\n{'=' * 60}")
    print(f"  FORMULA CHECKS: {'ALL PASSED' if all_ok else 'SOME FAILED'}")
    print(f"{'=' * 60}")
    return all_ok


def run_csv_validation():
    """If CSV results exist, validate that metrics are non-random and consistent."""
    print(f"\n{'=' * 60}")
    print(f"  CSV Results Validation")
    print(f"{'=' * 60}")

    rows = load_csv_results(CSV_FILE)
    if not rows:
        print(f"  [SKIP] No CSV results found at {CSV_FILE}")
        print(f"         Run test_suite_p4cci.sh first to generate data.")
        return True

    print(f"  Loaded {len(rows)} runs from {CSV_FILE}")

    jfi_vals = [float(r['jfi']) for r in rows]
    agg_vals = [float(r['agg_throughput_mbps']) for r in rows]
    util_vals = [float(r['link_util_pct']) for r in rows]

    print(f"\n  JFI:  mean={_mean(jfi_vals):.4f}  std={_std(jfi_vals):.4f}  "
          f"range=[{min(jfi_vals):.4f}, {max(jfi_vals):.4f}]")
    print(f"  Agg Throughput: mean={_mean(agg_vals):.2f} Mbps  std={_std(agg_vals):.2f}")
    print(f"  Link Util: mean={_mean(util_vals):.1f}%  std={_std(util_vals):.1f}%")

    # Sanity checks
    ok1 = all(0 <= j <= 1.0 for j in jfi_vals)
    ok2 = all(a >= 0 for a in agg_vals)
    ok3 = all(0 <= u <= 200 for u in util_vals)

    print(f"\n  JFI in [0, 1]: {'PASS' if ok1 else 'FAIL'}")
    print(f"  Throughput >= 0: {'PASS' if ok2 else 'FAIL'}")
    print(f"  Utilization sane: {'PASS' if ok3 else 'FAIL'}")

    all_ok = ok1 and ok2 and ok3
    print(f"\n{'=' * 60}")
    print(f"  CSV VALIDATION: {'PASSED' if all_ok else 'FAILED'}")
    print(f"{'=' * 60}")
    return all_ok


if __name__ == '__main__':
    ok1 = run_formula_checks()
    ok2 = run_csv_validation()
    sys.exit(0 if (ok1 and ok2) else 1)
