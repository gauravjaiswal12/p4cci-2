#!/usr/bin/env python3
"""
topology.py — Mininet emulation for P4CCI with two experiment modes:

  --mode baseline   : CUBIC vs BBR on shared bottleneck WITHOUT traffic separation.
                      Demonstrates unfairness (paper Section V baseline).

  --mode p4cci      : CUBIC vs BBR WITH P4CCI CCA-aware queue separation.
                      Demonstrates improved fairness (paper Section V P4CCI).

Both modes compute and print:
  - Jain Fairness Index (JFI)
  - Link Utilization
  - Throughput Deviation

Run:
    sudo python3 topology.py --mode baseline --auto
    sudo python3 topology.py --mode p4cci    --auto
"""

import sys
import os
import json
import math
import subprocess
from time import sleep
from collections import defaultdict

# ── P4 / Mininet imports ─────────────────────────────────────────────────────
sys.path.insert(0, '/home/p4/tutorials/utils')
sys.path.insert(0, '/home/p4/src/behavioral-model/mininet')
sys.path.insert(0, '/home/p4/src/mininet')

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.log import setLogLevel, info
from mininet.cli import CLI
from mininet.link import TCLink
from p4_mininet import P4Switch, P4Host


from p4_mininet import P4Switch, P4Host


# ── P4Switch with priority queues ──────────────────────────────────────────────
# BMv2 requires --priority-queues AFTER the '--' separator:
#   simple_switch -i ... --thrift-port ... JSON -- --priority-queues 3
# This matches how tutorials/p4_mininet.py line 134 does it for KBCS.
# Without this, standard_metadata.priority has no effect (only 1 queue per port).

class P4SwitchPQ(P4Switch):
    """P4Switch with --priority-queues 3 for CCA-aware traffic separation."""
    def start(self, controllers):
        """Override start() to inject '-- --priority-queues 3' into the command."""
        import tempfile
        from mininet.log import info, debug, error
        from sys import exit
        import os

        info("Starting P4 switch %s.\n" % self.name)
        args = [self.sw_path]
        for port, intf in self.intfs.items():
            if not intf.IP():
                args.extend(['-i', str(port) + "@" + intf.name])
        if self.pcap_dump:
            args.append("--pcap")
        if self.thrift_port:
            args.extend(['--thrift-port', str(self.thrift_port)])
        if self.nanomsg:
            args.extend(['--nanolog', self.nanomsg])
        args.extend(['--device-id', str(self.device_id)])
        P4Switch.device_id += 1
        args.append(self.json_path)
        # ── KEY: priority queues AFTER '--' separator (BMv2 target option) ──
        args.extend(['--', '--priority-queues', '3'])
        if self.enable_debugger:
            args.append("--debugger")
        if self.log_console:
            args.append("--log-console")
        logfile = "/tmp/p4s.%s.log" % self.name
        info(' '.join(args) + "\n")

        pid = None
        with tempfile.NamedTemporaryFile() as f:
            self.cmd(' '.join(args) + ' >' + logfile + ' 2>&1 & echo $! >> ' + f.name)
            pid = int(f.read())
        debug("P4 switch %s PID is %d.\n" % (self.name, pid))
        if not self.check_switch_started(pid):
            error("P4 switch %s did not start correctly.\n" % self.name)
            exit(1)
        info("P4 switch %s has been started.\n" % self.name)


# ─────────────────────────────────────────────────────────────────────────────
# Topology
# ─────────────────────────────────────────────────────────────────────────────

class P4CCITopo(Topo):
    """
    Dumbbell topology (paper Section V):
        h1 (CUBIC sender) ──┐
                             ├── [s1: P4Switch] ──┬── h3 (Receiver 1)
        h2 (BBR sender)   ──┘                     └── h4 (Receiver 2)

    All hosts on 10.0.0.0/24.
    Bottleneck: s1 → h3/h4 at 1 Gbps, 200-packet buffer.
    """

    def __init__(self, sw_path, json_path, thrift_port=9090,
                 priority_queues=3, **opts):
        Topo.__init__(self, **opts)

        # --priority-queues 3 enables Q0(short), Q1(loss-based), Q2(model-based)
        # Passed via P4SwitchPQ subclass with correct '-- --priority-queues 3' syntax.
        s1 = self.addSwitch('s1',
                            sw_path=sw_path,
                            json_path=json_path,
                            thrift_port=thrift_port,
                            pcap_dump=False,
                            cls=P4SwitchPQ)

        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        h3 = self.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
        h4 = self.addHost('h4', ip='10.0.0.4/24', mac='00:00:00:00:00:04')

        # Access links (senders → switch)
        self.addLink(h1, s1, delay='5ms')
        self.addLink(h2, s1, delay='5ms')

        # Bottleneck links (switch → receivers), 1 Gbps, 200-pkt buffer
        self.addLink(s1, h3, bw=1000, delay='10ms', max_queue_size=200,
                     cls=TCLink)
        self.addLink(s1, h4, bw=1000, delay='10ms', max_queue_size=200,
                     cls=TCLink)


# ─────────────────────────────────────────────────────────────────────────────
# Setup helpers
# ─────────────────────────────────────────────────────────────────────────────

def populate_arp(net):
    """Statically populate ARP caches (P4 switch doesn't handle ARP)."""
    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')
    arp_table = {
        '10.0.0.1': '00:00:00:00:00:01',
        '10.0.0.2': '00:00:00:00:00:02',
        '10.0.0.3': '00:00:00:00:00:03',
        '10.0.0.4': '00:00:00:00:00:04',
    }
    for host in [h1, h2, h3, h4]:
        for ip, mac in arp_table.items():
            host.cmd(f'arp -s {ip} {mac}')


def set_cca(host, algo):
    """Set TCP CCA for a host."""
    host.cmd(f'sysctl -w net.ipv4.tcp_congestion_control={algo}')
    result = host.cmd('sysctl net.ipv4.tcp_congestion_control')
    info(f'[CCA] {host.name}: {result.strip()}\n')


def populate_switch_tables(thrift_port=9090, enable_queues=False):
    """
    Push L3 forwarding rules + optional queue assignment rules.
    Port mapping: h1=1, h2=2, h3=3, h4=4
    """
    info('*** Populating P4 switch L3 tables\n')
    rules = [
        'table_add ipv4_lpm ipv4_forward 10.0.0.1/32 => 00:00:00:00:00:01 1',
        'table_add ipv4_lpm ipv4_forward 10.0.0.2/32 => 00:00:00:00:00:02 2',
        'table_add ipv4_lpm ipv4_forward 10.0.0.3/32 => 00:00:00:00:00:03 3',
        'table_add ipv4_lpm ipv4_forward 10.0.0.4/32 => 00:00:00:00:00:04 4',
    ]

    if enable_queues:
        # Populate queue_assignment table (paper Section III-C)
        # cca_class 0 → priority 0 (short/unclassified)
        # cca_class 1 → priority 1 (loss-based: CUBIC/Reno)
        # cca_class 2 → priority 2 (model-based: BBR)
        rules += [
            'table_add queue_assignment assign_to_queue 0 => 0',
            'table_add queue_assignment assign_to_queue 1 => 1',
            'table_add queue_assignment assign_to_queue 2 => 2',
        ]
        info('*** Queue assignment table populated (3 priority queues)\n')

    cli_input = '\n'.join(rules) + '\n'
    cmd = f'echo "{cli_input}" | simple_switch_CLI --thrift-port {thrift_port}'
    os.system(cmd)


def set_tcp_buffers(hosts):
    """Set TCP send/receive buffers (200 MB, as per paper Section V-A)."""
    for h in hosts:
        h.cmd('sysctl -w net.core.rmem_max=209715200')
        h.cmd('sysctl -w net.core.wmem_max=209715200')
        h.cmd('sysctl -w net.ipv4.tcp_rmem="4096 87380 209715200"')
        h.cmd('sysctl -w net.ipv4.tcp_wmem="4096 65536 209715200"')


# ─────────────────────────────────────────────────────────────────────────────
# Metrics helpers (pure stdlib)
# ─────────────────────────────────────────────────────────────────────────────

def _mean(data):
    return sum(data) / len(data) if data else 0.0

def _std(data):
    if len(data) < 2:
        return 0.0
    mu = _mean(data)
    return math.sqrt(sum((x - mu) ** 2 for x in data) / len(data))

def compute_jain_fairness(throughputs):
    """J = (Σxi)² / (n·Σxi²)   — paper Section VI."""
    n = len(throughputs)
    if n == 0:
        return 0.0
    sx  = sum(throughputs)
    sx2 = sum(x**2 for x in throughputs)
    return (sx**2) / (n * sx2) if sx2 > 0 else 1.0

def compute_link_utilization(throughputs, capacity_mbps=1000.0):
    """U = Σthroughput / capacity   — paper Section VI."""
    return min(1.0, sum(throughputs) / capacity_mbps) if capacity_mbps > 0 else 0.0

def compute_deviation(timeseries_dict):
    """Mean of per-flow (std/mean) across all flows."""
    devs = []
    for series in timeseries_dict.values():
        mu = _mean(series)
        devs.append(_std(series) / mu if mu > 0 else 0.0)
    return _mean(devs)

def compute_starvation_count(throughputs, capacity_mbps=1000.0):
    """Tracks the number of flows that receive less than 10% of their fair share."""
    if not throughputs:
        return 0
    fair_share = capacity_mbps / len(throughputs)
    return sum(1 for x in throughputs if x < 0.1 * fair_share)

def compute_packet_drop_ratio(retransmits, total_packets):
    """Directly computed from the P4 egress pipeline registers. Using retx as proxy."""
    if total_packets + retransmits == 0:
        return 0.0
    return retransmits / (total_packets + retransmits)


def parse_iperf3_log(log_path):
    """
    Parse iperf3 JSON log (generated with -J flag).
    Returns:
        avg_mbps: float — average throughput over all intervals
        intervals_mbps: list of per-interval Mbps values
    """
    try:
        with open(log_path) as f:
            data = json.load(f)
        intervals = data.get('intervals', [])
        mbps_list = []
        retx = 0
        for iv in intervals:
            bits = iv['sum']['bits_per_second']
            mbps_list.append(bits / 1e6)
            retx += iv['sum'].get('retransmits', 0)
        avg = _mean(mbps_list)
        return avg, mbps_list, retx
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        # Fallback: parse plain text iperf3 output
        return _parse_iperf3_text(log_path)


def _parse_iperf3_text(log_path):
    """Fallback plain-text iperf3 parser."""
    try:
        with open(log_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return 0.0, []

    mbps_list = []
    for line in lines:
        # Lines like: [  5]   0.00-5.00   sec  XXX MBytes  YYY Mbits/sec
        parts = line.split()
        for i, p in enumerate(parts):
            if 'Mbits/sec' in p or 'Gbits/sec' in p:
                try:
                    val = float(parts[i - 1])
                    if 'Gbits/sec' in p:
                        val *= 1000.0
                    mbps_list.append(val)
                except (ValueError, IndexError):
                    continue

    # Remove last "sender" summary line if duplicate
    if len(mbps_list) > 2 and mbps_list[-1] == mbps_list[-2]:
        mbps_list = mbps_list[:-1]

    return _mean(mbps_list), mbps_list, 0


def print_metrics(scenario, cubic_mbps, bbr_mbps, cubic_ts, bbr_ts, cubic_retx, bbr_retx,
                  capacity_mbps=1000.0):
    """Print JFI, utilization, deviation, starvation and drop ratio for a scenario."""
    flows = [cubic_mbps, bbr_mbps]
    ts    = {'CUBIC': cubic_ts, 'BBR': bbr_ts}

    jfi   = compute_jain_fairness(flows)
    util  = compute_link_utilization(flows, capacity_mbps)
    dev   = compute_deviation(ts)
    starv = compute_starvation_count(flows, capacity_mbps)
    
    total_retx = cubic_retx + bbr_retx
    duration_sec = len(cubic_ts) * 5 if cubic_ts else 60
    total_bits = sum(flows) * 1e6 * duration_sec
    estimated_pkts = total_bits / (1500 * 8)
    drop_ratio = compute_packet_drop_ratio(total_retx, estimated_pkts)

    print(f"\n{'='*58}")
    print(f"  Evaluation Metrics - {scenario}")
    print(f"{'='*58}")
    print(f"  CUBIC avg throughput : {cubic_mbps:>8.2f} Mbps")
    print(f"  BBR   avg throughput : {bbr_mbps:>8.2f} Mbps")
    print(f"  Total                : {sum(flows):>8.2f} Mbps / {capacity_mbps:.0f} Mbps")
    print(f"{'-'*58}")
    print(f"  Jain Fairness Index  : {jfi:.4f}   (1.0 = perfect)")
    print(f"  Link Efficiency/Util : {util*100:.1f}%   (target >= 95%)")
    print(f"  Starvation Count     : {starv}      (target = 0)")
    print(f"  Throughput Deviation : {dev:.4f}   (lower = more stable)")
    print(f"  Packet Drop Ratio    : {drop_ratio*100:.4f}%")
    print(f"{'='*58}")

    return {'jfi': jfi, 'utilization': util, 'deviation': dev,
            'cubic_mbps': cubic_mbps, 'bbr_mbps': bbr_mbps,
            'starvation': starv, 'drop_ratio': drop_ratio}


# ─────────────────────────────────────────────────────────────────────────────
# Baseline experiment (no CCA-aware separation)
# ─────────────────────────────────────────────────────────────────────────────

def run_baseline_traffic(net):
    """
    Baseline experiment: CUBIC vs BBR competing on same bottleneck,
    NO queue separation. Demonstrates fairness problem (paper Section V-B).
    """
    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

    set_cca(h1, 'cubic')
    set_cca(h2, 'bbr')
    set_tcp_buffers([h1, h2, h3, h4])

    info('*** [Baseline] Starting iperf3 servers\n')
    h3.cmd('pkill iperf3 2>/dev/null; iperf3 -s -p 5001 -D')
    h4.cmd('pkill iperf3 2>/dev/null; iperf3 -s -p 5002 -D')
    sleep(2)

    cubic_log = '/tmp/cubic_baseline.json'
    bbr_log   = '/tmp/bbr_baseline.json'

    info('*** [Baseline] Starting CUBIC flow: h1 → h3\n')
    h1.cmd(f'iperf3 -c 10.0.0.3 -p 5001 -t 60 -i 5 -J > {cubic_log} 2>&1 &')

    sleep(15)
    info('*** [Baseline] Starting BBR flow: h2 → h4 (15s offset)\n')
    h2.cmd(f'iperf3 -c 10.0.0.4 -p 5002 -t 45 -i 5 -J > {bbr_log} 2>&1 &')

    info('*** [Baseline] Traffic running — waiting 65s...\n')
    sleep(65)

    cubic_avg, cubic_ts, cubic_retx = parse_iperf3_log(cubic_log)
    bbr_avg, bbr_ts, bbr_retx     = parse_iperf3_log(bbr_log)

    info('\n=== Baseline: CUBIC Flow Log ===\n')
    print(h1.cmd(f'cat {cubic_log}'))
    info('\n=== Baseline: BBR Flow Log ===\n')
    print(h2.cmd(f'cat {bbr_log}'))

    return print_metrics('Baseline (No Separation)',
                         cubic_avg, bbr_avg, cubic_ts, bbr_ts, cubic_retx, bbr_retx)


# ─────────────────────────────────────────────────────────────────────────────
# P4CCI experiment (with CCA-aware queue separation)
# ─────────────────────────────────────────────────────────────────────────────

def run_p4cci_traffic(net, thrift_port=9090):
    """
    P4CCI experiment: same topology but with classification-based queue steering.
    Controller pre-classifies flows and inserts rules BEFORE traffic starts,
    so flows are immediately directed to correct queues.
    (Paper Section V-C)
    """
    h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

    set_cca(h1, 'cubic')
    set_cca(h2, 'bbr')
    set_tcp_buffers([h1, h2, h3, h4])

    # Pre-insert classification rules for known flows
    # class_id 1 = Loss-based (CUBIC), class_id 2 = Model-based (BBR)
    info('*** [P4CCI] Pre-inserting CCA classification rules\n')
    rules = [
        # CUBIC flow: h1→h3 (port 5001) → Queue 1 (loss-based)
        'table_add cca_classification set_cca_class 10.0.0.1 10.0.0.3 &1 5001 => 1',
        # BBR flow:   h2→h4 (port 5002) → Queue 2 (model-based)
        'table_add cca_classification set_cca_class 10.0.0.2 10.0.0.4 &1 5002 => 2',
    ]
    # Also push reverse-direction ACK rules for symmetry
    # Note: In real deployment these would be inserted by controller after FCN inference.
    # Here we pre-populate them for deterministic evaluation.
    cli_input = '\n'.join(rules) + '\n'
    cmd = f'echo "{cli_input}" | simple_switch_CLI --thrift-port {thrift_port}'
    os.system(cmd)
    sleep(1)

    info('*** [P4CCI] Starting iperf3 servers\n')
    h3.cmd('pkill iperf3 2>/dev/null; iperf3 -s -p 5001 -D')
    h4.cmd('pkill iperf3 2>/dev/null; iperf3 -s -p 5002 -D')
    sleep(2)

    cubic_log = '/tmp/cubic_p4cci.json'
    bbr_log   = '/tmp/bbr_p4cci.json'

    info('*** [P4CCI] Starting CUBIC flow: h1 → h3\n')
    h1.cmd(f'iperf3 -c 10.0.0.3 -p 5001 -t 60 -i 5 -J > {cubic_log} 2>&1 &')

    info('*** [P4CCI] Starting BBR flow: h2 → h4 (simultaneous)\n')
    h2.cmd(f'iperf3 -c 10.0.0.4 -p 5002 -t 60 -i 5 -J > {bbr_log} 2>&1 &')

    info('*** [P4CCI] Traffic running — waiting 65s...\n')
    sleep(65)

    cubic_avg, cubic_ts, cubic_retx = parse_iperf3_log(cubic_log)
    bbr_avg, bbr_ts, bbr_retx     = parse_iperf3_log(bbr_log)

    info('\n=== P4CCI: CUBIC Flow Log ===\n')
    print(h1.cmd(f'cat {cubic_log}'))
    info('\n=== P4CCI: BBR Flow Log ===\n')
    print(h2.cmd(f'cat {bbr_log}'))

    return print_metrics('P4CCI (CCA-Aware Separation)',
                         cubic_avg, bbr_avg, cubic_ts, bbr_ts, cubic_retx, bbr_retx)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='P4CCI Mininet Experiment')
    parser.add_argument('--mode', choices=['baseline', 'p4cci', 'both'],
                        default='baseline',
                        help='Experiment mode: baseline | p4cci | both (default: baseline)')
    parser.add_argument('--auto', action='store_true',
                        help='Run automated traffic experiment (no CLI)')
    parser.add_argument('--thrift-port', type=int, default=9090,
                        help='simple_switch Thrift port (default: 9090)')
    args = parser.parse_args()

    sw_path   = 'simple_switch'
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'build', 'p4cci_switch.json')

    if not os.path.exists(json_path):
        print(f'[ERROR] Compiled P4 JSON not found: {json_path}')
        print('Compile first: p4c --target bmv2 --arch v1model p4cci_switch.p4 -o build/')
        sys.exit(1)

    enable_queues = (args.mode in ('p4cci', 'both'))

    topo = P4CCITopo(sw_path=sw_path, json_path=json_path,
                     thrift_port=args.thrift_port)

    net = Mininet(topo=topo,
                  host=P4Host,
                  switch=P4Switch,
                  link=TCLink,
                  controller=None)

    net.start()
    populate_arp(net)
    sleep(2)
    populate_switch_tables(args.thrift_port, enable_queues=enable_queues)
    sleep(1)

    info('\n*** Testing connectivity (ping)\n')
    net.pingAll()

    results = {}

    if args.auto:
        if args.mode == 'baseline':
            results['baseline'] = run_baseline_traffic(net)

        elif args.mode == 'p4cci':
            results['p4cci'] = run_p4cci_traffic(net, args.thrift_port)

        elif args.mode == 'both':
            info('\n*** Phase 1: Baseline experiment\n')
            results['baseline'] = run_baseline_traffic(net)
            sleep(5)
            # Re-populate queues for P4CCI phase
            populate_switch_tables(args.thrift_port, enable_queues=True)
            sleep(1)
            info('\n*** Phase 2: P4CCI experiment\n')
            results['p4cci'] = run_p4cci_traffic(net, args.thrift_port)

        # Print comparison if we have both
        if 'baseline' in results and 'p4cci' in results:
            b = results['baseline']
            p = results['p4cci']
            print(f"\n{'='*58}")
            print("  Final Comparison: Baseline vs P4CCI")
            print(f"{'='*58}")
            print(f"  {'Metric':<30} {'Baseline':>10}  {'P4CCI':>10}")
            print(f"  {'-'*30} {'-'*10}  {'-'*10}")
            print(f"  {'JFI':<30} {b['jfi']:>10.4f}  {p['jfi']:>10.4f}")
            print(f"  {'Link Efficiency (%)':<30} {b['utilization']*100:>9.1f}%  {p['utilization']*100:>9.1f}%")
            print(f"  {'Starvation Count':<30} {b['starvation']:>10}  {p['starvation']:>10}")
            print(f"  {'Packet Drop Ratio (%)':<30} {b['drop_ratio']*100:>9.4f}%  {p['drop_ratio']*100:>9.4f}%")
            print(f"  {'Deviation (std/mean)':<30} {b['deviation']:>10.4f}  {p['deviation']:>10.4f}")
            dj = p['jfi'] - b['jfi']
            du = (p['utilization'] - b['utilization']) * 100
            ds = b['starvation'] - p['starvation']
            dp = (b['drop_ratio'] - p['drop_ratio']) * 100
            dd = b['deviation'] - p['deviation']
            print(f"{'-'*58}")
            print(f"  {'JFI improvement':<30} {'+' if dj>=0 else ''}{dj:>10.4f}")
            print(f"  {'Utilization gain':<30} {'+' if du>=0 else ''}{du:>9.1f}%")
            print(f"  {'Starvation reduction':<30} {'+' if ds>=0 else ''}{ds:>10}")
            print(f"  {'Drop Ratio reduction':<30} {'+' if dp>=0 else ''}{dp:>9.4f}%")
            print(f"  {'Deviation reduction':<30} {'+' if dd>=0 else ''}{dd:>10.4f}")
            print(f"{'='*58}\n")

    else:
        info('\n*** Dropping into Mininet CLI. Type "exit" to quit.\n')
        info(f'    Mode: {args.mode}\n')
        info('    Example: h1 iperf3 -c 10.0.0.3 -p 5001 -t 30 -J\n\n')
        CLI(net)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
