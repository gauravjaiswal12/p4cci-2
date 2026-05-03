#!/usr/bin/env python3
"""
P4CCI 4-flow Dumbbell Topology  (2-switch, shared bottleneck)
─────────────────────────────────────────────────────────────
Mirrors the KBCS dumbbell design so that results are directly comparable.

  h1(cubic)  ──┐                         ┌── h5 (receiver)
  h2(bbr)    ──┼── s1 ──(bottleneck)── s2 ──┼── h6 (receiver)
  h3(vegas)  ──┤       3 Mbps link       ├── h7 (receiver)
  h4(illinois)─┘                         └── h8 (receiver)

ALL four flows share port 5 on s1, creating real congestion.
In P4CCI mode, flows are classified and assigned to priority queues
on that shared port.  In baseline mode, they share FIFO (queue 0).
"""

import os, sys, argparse, tempfile
from time import sleep

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.log import setLogLevel, info, debug, error
from mininet.link import TCLink
from mininet.cli import CLI

# ---------- P4 imports (absolute VM paths) ----------
sys.path.insert(0, '/home/p4/tutorials/utils')
sys.path.insert(0, '/home/p4/src/behavioral-model/mininet')
sys.path.insert(0, '/home/p4/src/mininet')

from p4_mininet import P4Switch, P4Host


# ── P4SwitchPQ: adds '--priority-queues 3' after the BMv2 target separator ───

class P4SwitchPQ(P4Switch):
    """P4Switch subclass that injects '-- --priority-queues 3'."""

    def start(self, controllers):
        """Start with priority-queue support (BMv2 target option)."""
        info("Starting P4 switch %s.\n" % self.name)
        args = [self.sw_path]
        for port, intf in self.intfs.items():
            if not intf.IP():
                args.extend(['-i', str(port) + '@' + intf.name])
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


# ── Topology ───────────────────────────────────────────────────────────────────
#
#   h1 ─ s1:p1          s2:p1 ─ h5
#   h2 ─ s1:p2          s2:p2 ─ h6
#   h3 ─ s1:p3   s1:p5──s2:p5   s2:p3 ─ h7
#   h4 ─ s1:p4  (bottleneck)    s2:p4 ─ h8
#

class P4CCI4FlowTopo(Topo):
    def __init__(self, sw_path, json_path, thrift_port=9090, **opts):
        Topo.__init__(self, **opts)

        # Switch 1 (sender side) — thrift port 9090
        s1 = self.addSwitch('s1',
                            sw_path=sw_path,
                            json_path=json_path,
                            thrift_port=thrift_port,
                            pcap_dump=False,
                            cls=P4SwitchPQ)

        # Switch 2 (receiver side) — thrift port 9091
        s2 = self.addSwitch('s2',
                            sw_path=sw_path,
                            json_path=json_path,
                            thrift_port=thrift_port + 1,
                            pcap_dump=False,
                            cls=P4SwitchPQ)

        # 8 hosts
        for i in range(1, 9):
            self.addHost(f'h{i}', ip=f'10.0.0.{i}/24',
                         mac=f'00:00:00:00:00:0{i}')

        # Sender links: h1-h4 ──> s1 ports 1-4
        for i in range(1, 5):
            self.addLink(f'h{i}', 's1', delay='5ms')

        # Receiver links: h5-h8 ──> s2 ports 1-4  (added FIRST so they get ports 1-4)
        for i in range(5, 9):
            self.addLink(f'h{i}', 's2', delay='5ms')

        # Bottleneck link: s1:p5 ──> s2:p5  (shared by ALL 4 flows)
        # Added LAST so both switches assign it to port 5.
        self.addLink('s1', 's2', delay='5ms',
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

    # h3: VEGAS (delay-based -- classified as Loss-based per paper Section III-F,
    # since the paper only defines two classes: Loss-based and Model-based(BBR))
    h3 = net.get('h3')
    h3.cmd('modprobe tcp_vegas 2>/dev/null')
    h3.cmd('sysctl -w net.ipv4.tcp_congestion_control=vegas')
    info('[CCA] h3: vegas (Loss-based queue -- delay-based CCA)\n')

    # h4: ILLINOIS (loss-based)
    h4 = net.get('h4')
    h4.cmd('modprobe tcp_illinois 2>/dev/null')
    h4.cmd('sysctl -w net.ipv4.tcp_congestion_control=illinois')
    info('[CCA] h4: illinois (Loss-based queue)\n')


# ── Switch configuration ──────────────────────────────────────────────────────

def install_rules(thrift_s1, thrift_s2, mode):
    """Install forwarding rules on BOTH switches."""
    info('*** Installing P4 switch rules...\n')

    # ── S1 forwarding rules ──
    # Hosts h1-h4 are directly attached to s1 ports 1-4.
    # Hosts h5-h8 are reachable via the bottleneck link on port 5.
    s1_rules = []
    for i in range(1, 5):
        s1_rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.0.{i}/32 => '
            f'00:00:00:00:00:0{i} {i}'
        )
    for i in range(5, 9):
        # All receiver-bound traffic goes out port 5 (the shared bottleneck)
        s1_rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.0.{i}/32 => '
            f'00:00:00:00:00:0{i} 5'
        )

    # ── S2 forwarding rules ──
    # Port mapping: h5=s2:p1, h6=s2:p2, h7=s2:p3, h8=s2:p4, bottleneck=s2:p5
    s2_rules = []
    for i, port in [(5, 1), (6, 2), (7, 3), (8, 4)]:
        s2_rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.0.{i}/32 => '
            f'00:00:00:00:00:0{i} {port}'
        )
    for i in range(1, 5):
        # Return traffic (ACKs) goes back through port 5 (bottleneck)
        s2_rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.0.{i}/32 => '
            f'00:00:00:00:00:0{i} 5'
        )

    # ── P4CCI mode: add CCA classification + queue assignment on S1 ──
    if mode == 'p4cci':
        # Queue assignment table (class -> priority queue)
        s1_rules.append('table_add queue_assignment assign_to_queue 0 => 0')
        s1_rules.append('table_add queue_assignment assign_to_queue 1 => 1')
        s1_rules.append('table_add queue_assignment assign_to_queue 2 => 2')

        # CCA classification - ternary match: srcIP dstIP srcPort dstPort
        # CUBIC (h1) -> class 1 (Loss-based)
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.1&&&0xffffffff 10.0.0.5&&&0xffffffff '
            '0&&&0 5001&&&0xffff => 1 10'
        )
        # BBR (h2) -> class 2 (Model-based)
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.2&&&0xffffffff 10.0.0.6&&&0xffffffff '
            '0&&&0 5002&&&0xffff => 2 20'
        )
        # VEGAS (h3) -> class 1 (Loss-based queue)
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.3&&&0xffffffff 10.0.0.7&&&0xffffffff '
            '0&&&0 5003&&&0xffff => 1 30'
        )
        # ILLINOIS (h4) -> class 1 (Loss-based)
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.0.4&&&0xffffffff 10.0.0.8&&&0xffffffff '
            '0&&&0 5004&&&0xffff => 1 40'
        )

    # Push rules to S1
    cli_input = '\n'.join(s1_rules) + '\n'
    cmd = f'echo "{cli_input}" | simple_switch_CLI --thrift-port {thrift_s1}'
    os.system(cmd)

    # Push rules to S2
    cli_input = '\n'.join(s2_rules) + '\n'
    cmd = f'echo "{cli_input}" | simple_switch_CLI --thrift-port {thrift_s2}'
    os.system(cmd)

    # ── Rate limit the bottleneck: s1 port 5 only ──
    # This is the ONLY egress port that ALL 4 flows share.
    # By setting the rate on the port (without specifying a queue), BMv2 limits
    # the entire port to 250 pps (~3 Mbps). The priority queues (Q0, Q1, Q2) 
    # will automatically compete for this shared 3 Mbps capacity.
    info('*** Applying bottleneck rate limit on s1 port 5 (250 pps ~ 3 Mbps)...\n')
    rl_cmd = f'echo "set_queue_rate 250 5" | simple_switch_CLI --thrift-port {thrift_s1}'
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

    thrift_s1 = args.thrift_port
    thrift_s2 = args.thrift_port + 1

    topo = P4CCI4FlowTopo(sw_path=sw_path, json_path=json_path,
                           thrift_port=thrift_s1)

    net = Mininet(topo=topo, host=P4Host, switch=P4Switch,
                  link=TCLink, controller=None)
    net.start()
    sleep(2)

    configure_hosts(net)
    install_rules(thrift_s1, thrift_s2, args.mode)
    sleep(2)

    info('\n*** Ping test...\n')
    net.pingAll()

    run_traffic(net, args.duration, args.log_dir)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
