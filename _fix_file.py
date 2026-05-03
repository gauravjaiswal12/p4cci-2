"""Fix cross topology: add gateway ARP entries."""
import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r'e:\Research Methodology\Project-Implementation\baseline_p4cci\p4cci_baseline_v2\topology_cross.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# The problem: gateway IPs (10.0.X.10) have no ARP entries.
# When a host sends to a different subnet, it sends to the gateway IP.
# The kernel ARPs for the gateway but the P4 switch can't respond to ARP.
# Fix: add static ARP entries for the gateway IPs.

old_block = """    # Default gateways (matching KBCS topology.json)
    for i in range(1, 5):
        net.get(f'h{i}').cmd(f'route add default gw 10.0.1.10 dev eth0')
    for i in range(5, 9):
        net.get(f'h{i}').cmd(f'route add default gw 10.0.2.10 dev eth0')
    net.get('h9').cmd('route add default gw 10.0.3.10 dev eth0')
    net.get('h10').cmd('route add default gw 10.0.3.10 dev eth0')
    net.get('h11').cmd('route add default gw 10.0.4.10 dev eth0')
    net.get('h12').cmd('route add default gw 10.0.4.10 dev eth0')"""

new_block = """    # Gateway ARP entries — the P4 switch cannot respond to ARP requests,
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
    net.get('h12').cmd('route add default gw 10.0.4.10 dev eth0')"""

if old_block in content:
    content = content.replace(old_block, new_block)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('SUCCESS: Added gateway ARP entries.')
else:
    print('ERROR: Could not find target block.')
