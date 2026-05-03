"""Upload updated analyze_results.py to VM."""
import paramiko, sys, os
sys.stdout.reconfigure(encoding='utf-8')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('localhost', port=2222, username='p4', password='p4', timeout=10)
sftp = ssh.open_sftp()

local = r'e:\Research Methodology\Project-Implementation\kbcs_v2\analyze_results.py'
remote = '/home/p4/kbcs_v2/analyze_results.py'
print(f'Uploading analyze_results.py...')
sftp.put(local, remote)
sftp.close()
ssh.close()
print('DONE')
