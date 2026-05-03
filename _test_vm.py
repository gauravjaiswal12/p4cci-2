"""Deploy and test the fixed cross topology."""
import paramiko, sys, os, time
sys.stdout.reconfigure(encoding='utf-8')

VM_DIR = '/home/p4/p4cci_baseline_v2'
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'p4cci_baseline_v2')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('localhost', port=2222, username='p4', password='p4', timeout=10)
sftp = ssh.open_sftp()

print('Uploading fixed cross topology...')
sftp.put(os.path.join(LOCAL, 'topology_cross.py'), f'{VM_DIR}/topology_cross.py')
sftp.close()

# Clean
print('Cleaning...')
sin, sout, serr = ssh.exec_command('sudo mn -c 2>/dev/null; sudo killall -9 simple_switch 2>/dev/null', timeout=15)
sout.read()
time.sleep(4)

# Run cross topology test (short 20s)
print('Running cross topology test (20s)...')
print('=' * 60)
cmd = (
    f'cd {VM_DIR} && '
    f'sudo python3 topology_cross.py --mode p4cci --duration 20 '
    f'--log-dir /tmp/cross_test 2>&1'
)
sin, sout, serr = ssh.exec_command(cmd, timeout=180)
output = sout.read().decode('utf-8', 'replace')
# Print just the important parts
for line in output.splitlines():
    if any(x in line.lower() for x in ['ping', 'error', 'fail', 'unreachable', 'route', 'rules', 'traffic', 'server', 'flow', 'log', 'stop']):
        print(line)

print('\n--- Collecting metrics ---')
cmd2 = (
    f'cd {VM_DIR} && rm -f results/p4cci_cross_results.csv && '
    f'python3 collect_metrics.py --run 1 --topo cross --mode p4cci '
    f'--duration 20 --log-dir /tmp/cross_test 2>&1'
)
sin, sout, serr = ssh.exec_command(cmd2, timeout=30)
print(sout.read().decode('utf-8', 'replace'))

# Also show the raw iperf log for h1
print('\n--- h1 iperf log ---')
sin, sout, serr = ssh.exec_command('cat /tmp/cross_test/h1_cubic.txt 2>/dev/null', timeout=10)
print(sout.read().decode('utf-8', 'replace'))

ssh.close()
print('DONE')
