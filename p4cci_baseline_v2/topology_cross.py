#!/usr/bin/env python3
"""
topology_cross.py -- P4CCI cross-topology experiment (matches KBCS v2 cross).

Topology (identical to KBCS kbcs-topo/topology.json):

    h1 (CUBIC)   --+             +-- h9  (Receiver)
    h2 (BBR)     --+-- S1 --+---+-- h10 (Receiver)
    h3 (VEGAS)   --+         \  +---------- S3
    h4 (ILLINOIS)--+          X
    h5 (CUBIC)   --+         / +---------- S4
    h6 (BBR)     --+-- S2 --+---+-- h11 (Receiver)
    h7 (VEGAS)   --+             +-- h12 (Receiver)
    h8 (ILLINOIS)--+

    Bottleneck links: S1-S3, S1-S4, S2-S3, S2-S4 (250 pps each ~ 3 Mbps)
    Total bottleneck capacity: 2 links per ingress switch = 6 Mbps

CCAs: h1=CUBIC, h2=BBR, h3=VEGAS, h4=ILLINOIS,
      h5=CUBIC, h6=BBR, h7=VEGAS, h8=ILLINOIS

IPs match KBCS exactly:
    Senders:   h1-h4 = 10.0.1.1-4,  h5-h8 = 10.0.2.1-4
    Receivers: h9-h10 = 10.0.3.1-2, h11-h12 = 10.0.4.1-2

Run:
    sudo python3 topology_cross.py --mode p4cci --duration 60 --log-dir logs/run_1
    sudo python3 topology_cross.py --mode baseline --duration 60 --log-dir logs/run_1
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


# ---- P4Switch with priority queues ------------------------------------------
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
        # -- KEY: priority queues AFTER '--' separator (BMv2 target option) --
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


# ---- Topology ---------------------------------------------------------------

class P4CCICrossTopo(Topo):
    """Cross topology: 4 switches, 8 senders, 4 receivers."""

    def __init__(self, sw_path, json_path, **opts):
        Topo.__init__(self, **opts)

        # --priority-queues 3 enables Q0(short), Q1(loss-based), Q2(model-based)
        # Passed via P4SwitchPQ subclass with correct '-- --priority-queues 3' syntax.
        s1 = self.addSwitch('s1', sw_path=sw_path, json_path=json_path,
                            thrift_port=9090, pcap_dump=False, cls=P4SwitchPQ)
        s2 = self.addSwitch('s2', sw_path=sw_path, json_path=json_path,
                            thrift_port=9091, pcap_dump=False, cls=P4SwitchPQ)
        s3 = self.addSwitch('s3', sw_path=sw_path, json_path=json_path,
                            thrift_port=9092, pcap_dump=False, cls=P4SwitchPQ)
        s4 = self.addSwitch('s4', sw_path=sw_path, json_path=json_path,
                            thrift_port=9093, pcap_dump=False, cls=P4SwitchPQ)

        # ---- Senders (h1-h8) ----
        # Subnet 10.0.1.0/24 on S1
        for i in range(1, 5):
            h = self.addHost(f'h{i}', ip=f'10.0.1.{i}/24',
                             mac=f'08:00:00:00:01:0{i}')
            self.addLink(h, s1, delay='5ms')       # S1 ports 1-4

        # Subnet 10.0.2.0/24 on S2
        for i in range(5, 9):
            h = self.addHost(f'h{i}', ip=f'10.0.2.{i-4}/24',
                             mac=f'08:00:00:00:02:0{i-4}')
            self.addLink(h, s2, delay='5ms')       # S2 ports 1-4

        # ---- Cross links (bottleneck) ----
        self.addLink(s1, s3, delay='5ms', max_queue_size=200, cls=TCLink)  # S1-p5 <-> S3-p1
        self.addLink(s1, s4, delay='5ms', max_queue_size=200, cls=TCLink)  # S1-p6 <-> S4-p1
        self.addLink(s2, s3, delay='5ms', max_queue_size=200, cls=TCLink)  # S2-p5 <-> S3-p2
        self.addLink(s2, s4, delay='5ms', max_queue_size=200, cls=TCLink)  # S2-p6 <-> S4-p2

        # ---- Receivers ----
        # Subnet 10.0.3.0/24 on S3
        h9  = self.addHost('h9',  ip='10.0.3.1/24', mac='08:00:00:00:03:01')
        h10 = self.addHost('h10', ip='10.0.3.2/24', mac='08:00:00:00:03:02')
        self.addLink(h9,  s3, delay='5ms')         # S3-p3
        self.addLink(h10, s3, delay='5ms')         # S3-p4

        # Subnet 10.0.4.0/24 on S4
        h11 = self.addHost('h11', ip='10.0.4.1/24', mac='08:00:00:00:04:01')
        h12 = self.addHost('h12', ip='10.0.4.2/24', mac='08:00:00:00:04:02')
        self.addLink(h11, s4, delay='5ms')         # S4-p3
        self.addLink(h12, s4, delay='5ms')         # S4-p4


# ---- Host configuration -----------------------------------------------------

def configure_hosts(net):
    """Set CCAs, ARP tables, and sysctl tuning for all 12 hosts."""
    all_hosts = [net.get(f'h{i}') for i in range(1, 13)]

    # Sysctl tuning (match KBCS)
    for h in all_hosts:
        h.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        h.cmd('sysctl -w net.core.rmem_max=209715200')
        h.cmd('sysctl -w net.core.wmem_max=209715200')
        h.cmd('sysctl -w net.ipv4.tcp_rmem="4096 87380 209715200"')
        h.cmd('sysctl -w net.ipv4.tcp_wmem="4096 65536 209715200"')

    # Static ARP for all hosts across all subnets
    arp_table = {}
    for i in range(1, 5):
        arp_table[f'10.0.1.{i}'] = f'08:00:00:00:01:0{i}'
    for i in range(1, 5):
        arp_table[f'10.0.2.{i}'] = f'08:00:00:00:02:0{i}'
    arp_table['10.0.3.1'] = '08:00:00:00:03:01'
    arp_table['10.0.3.2'] = '08:00:00:00:03:02'
    arp_table['10.0.4.1'] = '08:00:00:00:04:01'
    arp_table['10.0.4.2'] = '08:00:00:00:04:02'

    for h in all_hosts:
        for ip, mac in arp_table.items():
            h.cmd(f'arp -s {ip} {mac}')

    # Gateway ARP entries — the P4 switch cannot respond to ARP requests,
    # so we must manually tell each host the MAC for its gateway IP.
    # These gateway MACs are arbitrary; the switch rewrites dst MAC anyway.
    gw_arp = {
        '10.0.1.10': '08:00:00:00:01:00',  # S1 "virtual gateway"
        '10.0.2.10': '08:00:00:00:02:00',  # S2 "virtual gateway"
        '10.0.3.10': '08:00:00:00:03:00',  # S3 "virtual gateway"
        '10.0.4.10': '08:00:00:00:04:00',  # S4 "virtual gateway"
    }
    for h in all_hosts:
        for ip, mac in gw_arp.items():
            h.cmd(f'arp -s {ip} {mac}')

    # Default gateways (matching KBCS topology.json)
    for i in range(1, 5):
        net.get(f'h{i}').cmd(f'route add default gw 10.0.1.10 dev eth0')
    for i in range(5, 9):
        net.get(f'h{i}').cmd(f'route add default gw 10.0.2.10 dev eth0')
    net.get('h9').cmd('route add default gw 10.0.3.10 dev eth0')
    net.get('h10').cmd('route add default gw 10.0.3.10 dev eth0')
    net.get('h11').cmd('route add default gw 10.0.4.10 dev eth0')
    net.get('h12').cmd('route add default gw 10.0.4.10 dev eth0')

    # CCA assignments (identical to KBCS)
    cca_map = {
        'h1': 'cubic', 'h2': 'bbr', 'h3': 'vegas', 'h4': 'illinois',
        'h5': 'cubic', 'h6': 'bbr', 'h7': 'vegas', 'h8': 'illinois',
    }
    for hname, cca in cca_map.items():
        h = net.get(hname)
        if cca in ('bbr', 'vegas', 'illinois'):
            h.cmd(f'modprobe tcp_{cca} 2>/dev/null')
        h.cmd(f'sysctl -w net.ipv4.tcp_congestion_control={cca}')
        info(f'[CCA] {hname}: {cca}\n')


# ---- Switch rules ------------------------------------------------------------

def install_rules(mode):
    """Install forwarding + (optionally) classification rules on all 4 switches."""
    info('*** Installing P4 switch rules...\n')

    # ---- S1 (thrift 9090): ingress for h1-h4 ----
    s1_rules = []
    # Local forwarding (h1-h4 on ports 1-4)
    for i in range(1, 5):
        s1_rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.1.{i}/32 => '
            f'08:00:00:00:01:0{i} {i}'
        )
    # Route to S3 (port 5) for subnet 10.0.3.0/24
    s1_rules.append('table_add ipv4_lpm ipv4_forward 10.0.3.0/24 => 08:00:00:00:03:00 5')
    # Route to S4 (port 6) for subnet 10.0.4.0/24
    s1_rules.append('table_add ipv4_lpm ipv4_forward 10.0.4.0/24 => 08:00:00:00:04:00 6')

    # ---- S2 (thrift 9091): ingress for h5-h8 ----
    s2_rules = []
    for i in range(1, 5):
        s2_rules.append(
            f'table_add ipv4_lpm ipv4_forward 10.0.2.{i}/32 => '
            f'08:00:00:00:02:0{i} {i}'
        )
    s2_rules.append('table_add ipv4_lpm ipv4_forward 10.0.3.0/24 => 08:00:00:00:03:00 5')
    s2_rules.append('table_add ipv4_lpm ipv4_forward 10.0.4.0/24 => 08:00:00:00:04:00 6')

    # ---- S3 (thrift 9092): egress for h9-h10 ----
    s3_rules = []
    s3_rules.append('table_add ipv4_lpm ipv4_forward 10.0.1.0/24 => 08:00:00:00:01:00 1')
    s3_rules.append('table_add ipv4_lpm ipv4_forward 10.0.2.0/24 => 08:00:00:00:02:00 2')
    s3_rules.append('table_add ipv4_lpm ipv4_forward 10.0.3.1/32 => 08:00:00:00:03:01 3')
    s3_rules.append('table_add ipv4_lpm ipv4_forward 10.0.3.2/32 => 08:00:00:00:03:02 4')

    # ---- S4 (thrift 9093): egress for h11-h12 ----
    s4_rules = []
    s4_rules.append('table_add ipv4_lpm ipv4_forward 10.0.1.0/24 => 08:00:00:00:01:00 1')
    s4_rules.append('table_add ipv4_lpm ipv4_forward 10.0.2.0/24 => 08:00:00:00:02:00 2')
    s4_rules.append('table_add ipv4_lpm ipv4_forward 10.0.4.1/32 => 08:00:00:00:04:01 3')
    s4_rules.append('table_add ipv4_lpm ipv4_forward 10.0.4.2/32 => 08:00:00:00:04:02 4')

    # ---- P4CCI mode: add CCA classification + queue assignment ----
    if mode == 'p4cci':
        # Queue assignment rules (all switches)
        qa_rules = [
            'table_add queue_assignment assign_to_queue 0 => 0',
            'table_add queue_assignment assign_to_queue 1 => 1',
            'table_add queue_assignment assign_to_queue 2 => 2',
        ]

        # S1 classification: h1=CUBIC(1), h2=BBR(2), h3=VEGAS(1), h4=ILLINOIS(1)
        s1_rules += qa_rules
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.1.1&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 1 10'
        )
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.1.2&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 2 20'
        )
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.1.3&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 1 30'
        )
        s1_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.1.4&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 1 40'
        )

        # S2 classification: h5=CUBIC(1), h6=BBR(2), h7=VEGAS(1), h8=ILLINOIS(1)
        s2_rules += qa_rules
        s2_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.2.1&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 1 10'
        )
        s2_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.2.2&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 2 20'
        )
        s2_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.2.3&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 1 30'
        )
        s2_rules.append(
            'table_add cca_classification set_cca_class '
            '10.0.2.4&&&0xffffffff 0&&&0 0&&&0 0&&&0 => 1 40'
        )

        # S3 and S4 also get queue assignment (for egress queue steering)
        s3_rules += qa_rules
        s4_rules += qa_rules

    # Push rules to each switch
    for port, rules in [(9090, s1_rules), (9091, s2_rules),
                        (9092, s3_rules), (9093, s4_rules)]:
        cli_input = '\n'.join(rules) + '\n'
        cmd = f'echo "{cli_input}" | simple_switch_CLI --thrift-port {port}'
        os.system(cmd + ' 2>/dev/null')

    # Rate limit bottleneck ports (S1 p5/p6, S2 p5/p6)
    # By setting the rate on the port (without specifying a queue), BMv2 limits
    # the entire port to 250 pps (~3 Mbps). The priority queues (Q0, Q1, Q2) 
    # will automatically compete for this shared 3 Mbps capacity.
    info('*** Applying bottleneck rate limits (250 pps ~ 3 Mbps per link)...\n')
    for port in [9090, 9091]:
        for p in [5, 6]:
            rl_cmd = f'echo "set_queue_rate 250 {p}" | simple_switch_CLI --thrift-port {port}'
            os.system(rl_cmd + ' 2>/dev/null')
    
    info('*** Rules and rate limits installed.\n')


# ---- Traffic -----------------------------------------------------------------

# 8 flows matching KBCS cross topology traffic pattern
FLOWS = [
    ('h1', 'h9',  '10.0.3.1', 5001, 'cubic'),
    ('h2', 'h9',  '10.0.3.1', 5002, 'bbr'),
    ('h3', 'h10', '10.0.3.2', 5003, 'vegas'),
    ('h4', 'h10', '10.0.3.2', 5004, 'illinois'),
    ('h5', 'h11', '10.0.4.1', 5005, 'cubic'),
    ('h6', 'h11', '10.0.4.1', 5006, 'bbr'),
    ('h7', 'h12', '10.0.4.2', 5007, 'vegas'),
    ('h8', 'h12', '10.0.4.2', 5008, 'illinois'),
]


def run_traffic(net, duration, log_dir):
    """Run 8 iperf flows using sendCmd for true parallel execution."""
    os.makedirs(log_dir, exist_ok=True)

    # Start iperf servers on receivers (one per port)
    info('*** Starting iperf servers...\n')
    receiver_ports = {}
    for _, recv_name, _, port, _ in FLOWS:
        if (recv_name, port) not in receiver_ports:
            recv = net.get(recv_name)
            recv.cmd(f'iperf -s -p {port} > /dev/null 2>&1 &')
            receiver_ports[(recv_name, port)] = True
    sleep(3)

    # Verify servers
    for (recv_name, port) in receiver_ports:
        recv = net.get(recv_name)
        check = recv.cmd(f'ss -tlnp | grep {port}')
        if str(port) in check:
            info(f'    {recv_name}:{port} OK\n')
        else:
            info(f'    {recv_name}:{port} RETRY\n')
            recv.cmd(f'iperf -s -p {port} > /dev/null 2>&1 &')
    sleep(2)

    # Launch all 8 clients in parallel (sendCmd is non-blocking)
    info(f'*** Launching {len(FLOWS)} flows for {duration}s...\n')
    senders = []
    for send_name, _, dst_ip, port, cca in FLOWS:
        sender = net.get(send_name)
        log_path = os.path.join(log_dir, f'{send_name}_{cca}.txt')
        sender.sendCmd(
            f'iperf -c {dst_ip} -t {duration} -p {port} -P 1 '
            f'> {log_path} 2>&1'
        )
        senders.append((sender, send_name, cca))

    # Wait for all to complete
    info(f'*** Waiting for all flows to complete...\n')
    for sender, send_name, cca in senders:
        try:
            sender.waitOutput(verbose=False)
            info(f'    {send_name} ({cca}) finished.\n')
        except Exception as e:
            info(f'    {send_name} ({cca}) error: {e}\n')
    sleep(2)

    # Kill servers
    for recv_name in ['h9', 'h10', 'h11', 'h12']:
        net.get(recv_name).cmd('pkill iperf 2>/dev/null')

    # Print log file sizes
    info('*** Traffic complete. Log files:\n')
    for send_name, _, _, _, cca in FLOWS:
        log_path = os.path.join(log_dir, f'{send_name}_{cca}.txt')
        size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        info(f'    {log_path}: {size} bytes\n')


# ---- Main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='P4CCI cross-topology experiment')
    parser.add_argument('--mode',     choices=['baseline', 'p4cci'], default='p4cci')
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--log-dir',  type=str, default='/tmp/p4cci_cross_run')
    args = parser.parse_args()

    sw_path   = 'simple_switch'
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'build', 'p4cci_switch.json')

    if not os.path.exists(json_path):
        print(f'[ERROR] P4 JSON not found: {json_path}')
        print('Compile: p4c --target bmv2 --arch v1model p4cci_switch.p4 -o build/')
        sys.exit(1)

    topo = P4CCICrossTopo(sw_path=sw_path, json_path=json_path)

    net = Mininet(topo=topo, host=P4Host, switch=P4Switch,
                  link=TCLink, controller=None)
    net.start()
    sleep(3)

    configure_hosts(net)
    install_rules(args.mode)
    sleep(2)

    info('\n*** Ping test (sample)...\n')
    h1, h9 = net.get('h1', 'h9')
    result = h1.cmd('ping -c 2 10.0.3.1')
    info(result)

    run_traffic(net, args.duration, args.log_dir)

    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
