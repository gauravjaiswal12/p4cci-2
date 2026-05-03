"""Test baseline mode"""
import paramiko, sys, os
sys.stdout.reconfigure(encoding='utf-8')

VM_DIR = '/home/p4/p4cci_baseline_v2'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('localhost', port=2222, username='p4', password='p4', timeout=10)

print('Running BASELINE test...')
cmd = (
    f'cd {VM_DIR} && '
    f'sudo python3 topology_4flow.py --mode baseline --duration 20 '
    f'--log-dir /tmp/rate_test_base 2>&1'
)
sin, sout, serr = ssh.exec_command(cmd, timeout=120)
print(sout.read().decode('utf-8', 'replace'))

cmd2 = (
    f'cd {VM_DIR} && '
    f'python3 collect_metrics.py --run 1 --topo dumbbell --mode baseline '
    f'--duration 20 --log-dir /tmp/rate_test_base 2>&1'
)
sin, sout, serr = ssh.exec_command(cmd2, timeout=30)
print(sout.read().decode('utf-8', 'replace'))

ssh.close()
