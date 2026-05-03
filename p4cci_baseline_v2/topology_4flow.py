#!/usr/bin/env python3
"""
topology_4flow.py — Self-contained 4-flow P4CCI evaluation topology.

This script handles EVERYTHING in one process:
  1. Start Mininet with 4 senders + 4 receivers
  2. Install forwarding rules via simple_switch_CLI
  3. Apply BMv2 queue rate limiting (250 pps ≈ 3 Mbps)
  4. Start iperf servers and clients
  5. Wait for iperf to fully complete
  6. Stop Mininet and exit

Topology:
    h1 (CUBIC) ──┐                       ┌── h5 (Receiver 1)
    h2 (BBR)   ──┤                       ├── h6 (Receiver 2)
    h3 (CUBIC) ──┼── [s1: P4Switch] ──── ┼── h7 (Receiver 3)
    h4 (BBR)   ──┘                       └── h8 (Receiver 4)

Run:
    sudo python3 topology_4flow.py --mode p4cci --duration 60 --log-dir logs/run_1
"""

import sys, os, argparse
from time import sleep

sys.path.insert(0, '/home/p4/tutorials/utils')
sys.path.insert(0, '/home/p4/src/behavioral-model/mininet')
sys.path.insert(0, '/home/p4/src/mininet')

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.log import setLogLevel, info
from mininet.link import TCLink
from p4_mininet import P4Switch, P4Host


# ── Topology ───────────────────────────────────────────────────────────────────

class P4CCI4FlowTopo(Topo):
    def __init__(self, sw_path, json_path, thrift_port=9090, **opts):
        Topo.__init__(self, **opts)

        s1 = self.addSwitch('s1',
                            sw_path=sw_path,
                            json_path=json_path,
                            thrift_port=thrift_port,
                            pcap_dump=False)

        for i in range(1, 9):
            self.addHost(f'h{i}', ip=f'10.0.0.{i}/24',
                         mac=f'00:00:00:00:00:0{i}')

        # Sender links (ports 1-4)
        for i in range(1, 5):
            self.addLink(f'h{i}', 's1', delay='5ms')

        # Receiver links (ports 5-8), with queue buffer
        for i in range(5, 9):
            self.addLink('s1', f'h{i}', delay='5ms',
                         max_queue_size=200, cls=TCLink)


# ── Host setup ─────────────────────────────────────────────────────────────────

def configure_hosts(net):
    hosts = [net.get(f'h{i}') for i in range(1, 9)]

    for h in hosts:
        h.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        h.cmd('sysctl -w net.core.rmem_max=209715200')
        h.cmd('sysctl -w net.core.wmem_max=209715200')
        h.cmd('sysctl -w net.ipv4.tcp_rmem="4096 87380 209715200"')
        h.cmd('sysctl -w net.ipv4.tcp_wmem="4096 65536 209715200"')

    # Static ARP
    arp_table = {f'10.0.0.{i}': f'00:00:00:00:00:0{i}' for i in range(1, 9)}
    for h in hosts:
        for ip, mac in arp_table.items():
            h.cmd(f'arp -s {ip} {mac}')

    # CCA assignment matching KBCS reference
    # h1=CUBIC, h2=BBR, h3=VEGAS, h4=ILLINOIS
    
    # h1: CUBIC
    net.get('h1').cmd('sysctl -w net.ipv4.tcp_congestion_control=cubic')
    info('[CCA] h1: cubic (Loss-based)\n')

    # h2: BBR
    h2 = net.get('h2')
    h2.cmd('modprobe tcp_bbr 2>/dev/null')
    h2.cmd('sysctl -w net.ipv4.tcp_congestion_control=bbr')
    info('[CCA] h2: bbr (Model-based)\n')

    # h3: VEGAS
    h3 = net.get('h3')
    h3.cmd('modprobe tcp_vegas 2>/dev/null')
    h3.cmd('sysctl -w net.ipv4.tcp_congestion_control=vegas')
    info('[CCA] h3: vegas (Delay-based)\n')

    # h4: ILLINOIS
    h4 = net.get('h4')
    h4.cmd('modprobe tcp_illinois 2>/dev/null')
    h4.cmd('sysctl -w net.ipv4.tcp_congestion_control=illinois')
    info('[CCA] h4: illinois (Loss-based)\n')


# ── Switch configuration ──────────────────────────────────────────────────────

def install_rules(thrift_port, mode):
    """Install forwarding rules and (optionally) queue assignment via CLI."""
    info('*** Installing P4 switch rules...\n')

    rules = []
    # L3 forwarding for all 8 hosts
    for i in range(1, 9):
        rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.0.{i}/32 => '
            f'00:00:00:00:00:0{i} {i}'
        )

    if mode == 'p4cci':
        # Queue assignment table (class → priority queue)
        rules.append('table_add queue_assignment assign_to_queue 0 => 0')
        rules.append('table_add queue_assignment assign_to_queue 1 => 1')
        rules.append('table_add queue_assignment assign_to_queue 2 => 2')

        # CCA classification — ternary match syntax (no spaces around &&&):
        # table_add <table> <action> <key1> <key2> <key3> <key4> => <action_data> <priority>
        #
        # CUBIC (h1) -> class 1 (Loss-based)
        rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.1&&&0xffffffff 10.0.0.5&&&0xffffffff '
            '0&&&0 5001&&&0xffff => 1 10'
        )
        # BBR (h2) -> class 2 (Model-based)
        rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.2&&&0xffffffff 10.0.0.6&&&0xffffffff '
            '0&&&0 5002&&&0xffff => 2 20'
        )
        # VEGAS (h3) -> class 2 (Delay-based)
        rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.3&&&0xffffffff 10.0.0.7&&&0xffffffff '
            '0&&&0 5003&&&0xffff => 2 30'
        )
        # ILLINOIS (h4) -> class 1 (Loss-based)
        rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.4&&&0xffffffff 10.0.0.8&&&0xffffffff '
            '0&&&0 5004&&&0xffff => 1 40'
        )

    cli_input = '\n'.join(rules) + '\n'
    cmd = f'echo "{cli_input}" | simple_switch_CLI --thrift-port {thrift_port}'
    os.system(cmd)

    # Apply rate limits on bottleneck (egress) ports 5-8
    info('*** Applying queue rate limits (250 pps ≈ 3 Mbps)...\n')
    for port in range(5, 9):
        rl_cmd = f'echo "set_queue_rate 250 {port}" | simple_switch_CLI --thrift-port {thrift_port}'
        os.system(rl_cmd + ' 2>/dev/null')

    info('*** Rules and rate limits installed.\n')


# ── Traffic test ───────────────────────────────────────────────────────────────

def run_traffic(net, duration, log_dir):
    """Run 4 iperf flows using sendCmd for true parallel execution."""
    os.makedirs(log_dir, exist_ok=True)

    flows = [
        ('h1', 'h5', '10.0.0.5', 5001, 'cubic'),
        ('h2', 'h6', '10.0.0.6', 5002, 'bbr'),
        ('h3', 'h7', '10.0.0.7', 5003, 'vegas'),
        ('h4', 'h8', '10.0.0.8', 5004, 'illinois'),
    ]

    # Start iperf servers (background &, NOT -D)
    info('*** Starting iperf servers...\n')
    for _, recv_name, _, port, _ in flows:
        recv = net.get(recv_name)
        recv.cmd(f'iperf -s -p {port} > /dev/null 2>&1 &')
    sleep(3)

    # Verify servers are listening
    all_ok = True
    for _, recv_name, _, port, _ in flows:
        recv = net.get(recv_name)
        check = recv.cmd(f'ss -tlnp | grep {port}')
        if str(port) in check:
            info(f'    {recv_name}:{port} ✓\n')
        else:
            info(f'    {recv_name}:{port} ✗ — retrying...\n')
            recv.cmd(f'iperf -s -p {port} > /dev/null 2>&1 &')
            all_ok = False
    if not all_ok:
        sleep(3)

    # Launch all clients using sendCmd (TRUE parallel — no blocking)
    info(f'*** Launching {len(flows)} flows for {duration}s...\n')
    senders = []
    for send_name, _, dst_ip, port, cca in flows:
        sender = net.get(send_name)
        log_path = os.path.join(log_dir, f'{send_name}_{cca}.txt')
        # sendCmd sends command and returns immediately (non-blocking)
        sender.sendCmd(
            f'iperf -c {dst_ip} -t {duration} -p {port} -P 1 '
            f'> {log_path} 2>&1'
        )
        senders.append((sender, send_name, cca))

    # Wait for ALL iperf clients to finish (waitOutput blocks until done)
    info(f'*** Waiting for all flows to complete...\n')
    for sender, send_name, cca in senders:
        try:
            sender.waitOutput(verbose=False)
            info(f'    {send_name} ({cca}) finished.\n')
        except Exception as e:
            info(f'    {send_name} ({cca}) error: {e}\n')

    sleep(2)

    # Kill servers
    for _, recv_name, _, port, _ in flows:
        net.get(recv_name).cmd('pkill iperf 2>/dev/null')

    # Print log file sizes for verification
    info('*** Traffic complete. Log files:\n')
    for send_name, _, _, _, cca in flows:
        log_path = os.path.join(log_dir, f'{send_name}_{cca}.txt')
        size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        info(f'    {log_path}: {size} bytes\n')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='P4CCI 4-flow dumbbell topology')
    parser.add_argument('--mode',        choices=['baseline', 'p4cci'], default='p4cci')
    parser.add_argument('--duration',    type=int, default=60)
    parser.add_argument('--thrift-port', type=int, default=9090)
    parser.add_argument('--log-dir',     type=str, default='/tmp/p4cci_run')
    args = parser.parse_args()

    sw_path   = 'simple_switch'
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'build', 'p4cci_switch.json')

    if not os.path.exists(json_path):
        print(f'[ERROR] P4 JSON not found: {json_path}')
        print('Compile: p4c --target bmv2 --arch v1model p4cci_switch.p4 -o build/')
        sys.exit(1)

    topo = P4CCI4FlowTopo(sw_path=sw_path, json_path=json_path,
                           thrift_port=args.thrift_port)

    net = Mininet(topo=topo, host=P4Host, switch=P4Switch,
                  link=TCLink, controller=None)
    net.start()
    sleep(2)

    configure_hosts(net)
    install_rules(args.thrift_port, args.mode)
    sleep(2)

    info('\n*** Ping test...\n')
    net.pingAll()

    run_traffic(net, args.duration, args.log_dir)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
