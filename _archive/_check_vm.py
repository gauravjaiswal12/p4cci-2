"""Debug cross topology - check ping and run log."""
import paramiko, sys
sys.stdout.reconfigure(encoding='utf-8')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('localhost', port=2222, username='p4', password='p4', timeout=10)

cmds = [
    # Check the full run log for ping results and errors
    "grep -A5 'Ping test' /home/p4/p4cci_baseline_v2/logs/run_5.log 2>/dev/null",
    # Check for any error in the log
    "grep -i 'error\\|fail\\|ICMP\\|No route\\|timed out' /home/p4/p4cci_baseline_v2/logs/run_5.log 2>/dev/null | head -20",
    # Check iperf log content
    "cat /home/p4/p4cci_baseline_v2/logs/run_5/h1_cubic.txt 2>/dev/null",
]

for cmd in cmds:
    print(f'\n>>> {cmd}')
    sin, sout, serr = ssh.exec_command(cmd, timeout=10)
    print(sout.read().decode('utf-8', 'replace').strip())

ssh.close()
print('\nDONE')
