"""Pull P4CCI CSVs into kbcs_v2/results/ locally."""
import paramiko, sys, os
sys.stdout.reconfigure(encoding='utf-8')

VM_RESULTS = '/home/p4/kbcs_v2/results'
LOCAL_DIR = r'e:\Research Methodology\Project-Implementation\kbcs_v2\results'
os.makedirs(LOCAL_DIR, exist_ok=True)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('localhost', port=2222, username='p4', password='p4', timeout=10)
sftp = ssh.open_sftp()

files = sftp.listdir(VM_RESULTS)
csv_files = sorted([f for f in files if f.endswith('.csv')])

for f in csv_files:
    remote = f'{VM_RESULTS}/{f}'
    local = os.path.join(LOCAL_DIR, f)
    sftp.get(remote, local)
    with open(local, 'r') as fh:
        rows = len(fh.readlines()) - 1
    print(f'  {f:40s} → {rows} runs')

sftp.close()
ssh.close()
print(f'\nAll CSVs saved to: {LOCAL_DIR}')
