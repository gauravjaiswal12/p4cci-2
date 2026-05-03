"""
sync_p4cci.py -- Deploy P4CCI baseline to the P4 VM and compile.
"""
import paramiko
import sys
import os
import stat

sys.stdout.reconfigure(encoding='utf-8')

VM_HOST = 'localhost'
VM_PORT = 2222
VM_USER = 'p4'
VM_PASS = 'p4'
VM_DIR  = '/home/p4/p4cci_baseline_v2'

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'p4cci_baseline_v2')

# Files to sync (relative to p4cci_baseline_v2/)
FILES = [
    'p4cci_switch.p4',
    'controller.py',
    'fcn_model.py',
    'fcn_model.pth',
    'generate_dataset.py',
    'topology.py',
    'topology_4flow.py',
    'topology_cross.py',
    'evaluate.py',
    'collect_metrics.py',
    'test_suite_p4cci.sh',
    'verify_metrics.py',
    'Instructions.md',
]

print('=' * 60)
print('  P4CCI Baseline -> VM Deployment')
print('=' * 60)
print(f'  Local dir : {LOCAL_DIR}')
print(f'  VM target : {VM_USER}@{VM_HOST}:{VM_PORT}:{VM_DIR}')
print()

# Connect
print('[1/5] Connecting to VM...')
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect(VM_HOST, port=VM_PORT, username=VM_USER, password=VM_PASS, timeout=10)
    print('  Connected!')
except Exception as e:
    print(f'  FAILED: {e}')
    print('  Make sure the P4 VM is running and SSH port 2222 is forwarded.')
    sys.exit(1)

sftp = ssh.open_sftp()

# Create directories
print('[2/5] Creating directories on VM...')
for d in [VM_DIR, f'{VM_DIR}/build', f'{VM_DIR}/results', f'{VM_DIR}/logs']:
    try:
        sftp.stat(d)
    except FileNotFoundError:
        stdin, stdout, stderr = ssh.exec_command(f'mkdir -p {d}')
        stdout.read()
        print(f'  Created {d}')

# Upload files
print(f'[3/5] Uploading {len(FILES)} files...')
for fname in FILES:
    local_path  = os.path.join(LOCAL_DIR, fname)
    remote_path = f'{VM_DIR}/{fname}'

    if not os.path.exists(local_path):
        print(f'  SKIP (not found): {fname}')
        continue

    sftp.put(local_path, remote_path)
    size = os.path.getsize(local_path)
    print(f'  OK  {fname:>30}  ({size:>10,} bytes)')

sftp.close()

# Make scripts executable
print('[4/5] Setting permissions...')
cmds = [
    f'chmod +x {VM_DIR}/test_suite_p4cci.sh',
    f'chmod +x {VM_DIR}/topology_4flow.py',
    f'chmod +x {VM_DIR}/topology_cross.py',
]
for cmd in cmds:
    stdin, stdout, stderr = ssh.exec_command(cmd)
    stdout.read()
print('  Scripts made executable.')

# Compile P4
print('[5/5] Compiling P4 program on VM...')
compile_cmd = (
    f'cd {VM_DIR} && '
    f'mkdir -p build && '
    f'p4c --target bmv2 --arch v1model p4cci_switch.p4 -o build/ 2>&1'
)
stdin, stdout, stderr = ssh.exec_command(compile_cmd, timeout=60)
output = stdout.read().decode('utf-8', errors='replace')
err    = stderr.read().decode('utf-8', errors='replace')

if output.strip():
    print(f'  stdout: {output.strip()}')
if err.strip():
    print(f'  stderr: {err.strip()}')

# Check if JSON was created
try:
    json_stat = sftp if False else None  # re-open sftp
    sftp2 = ssh.open_sftp()
    json_info = sftp2.stat(f'{VM_DIR}/build/p4cci_switch.json')
    print(f'  OK  build/p4cci_switch.json ({json_info.st_size:,} bytes)')
    sftp2.close()
except Exception as e:
    print(f'  WARNING: Could not verify JSON: {e}')

# Verify listing
print()
print('Verifying VM directory...')
stdin, stdout, stderr = ssh.exec_command(f'ls -la {VM_DIR}/')
listing = stdout.read().decode('utf-8', errors='replace')
print(listing)

stdin, stdout, stderr = ssh.exec_command(f'ls -la {VM_DIR}/build/')
listing = stdout.read().decode('utf-8', errors='replace')
print(listing)

ssh.close()

print('=' * 60)
print('  DEPLOYMENT COMPLETE')
print('=' * 60)
print()
print('To run experiments on the VM, SSH in and execute:')
print(f'  ssh -p 2222 p4@localhost')
print(f'  cd {VM_DIR}')
print()
print('Quick test (3 runs, 30s):')
print(f'  sudo ./test_suite_p4cci.sh --topo dumbbell --mode p4cci --runs 3 --duration 30')
print()
print('Full 30-run suite:')
print(f'  sudo ./test_suite_p4cci.sh --topo dumbbell --mode p4cci --runs 30')
print(f'  sudo ./test_suite_p4cci.sh --topo cross --mode p4cci --runs 30')
print(f'  sudo ./test_suite_p4cci.sh --topo dumbbell --mode baseline --runs 30')
print(f'  sudo ./test_suite_p4cci.sh --topo cross --mode baseline --runs 30')
