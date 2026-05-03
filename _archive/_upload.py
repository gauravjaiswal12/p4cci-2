"""Upload BOTH fixed topologies to VM"""
import paramiko, sys, os
sys.stdout.reconfigure(encoding='utf-8')

VM_DIR = '/home/p4/p4cci_baseline_v2'
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'p4cci_baseline_v2')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('localhost', port=2222, username='p4', password='p4', timeout=10)
sftp = ssh.open_sftp()

print('Uploading fixed topology_4flow.py...')
sftp.put(os.path.join(LOCAL, 'topology_4flow.py'), f'{VM_DIR}/topology_4flow.py')

print('Uploading fixed topology_cross.py...')
sftp.put(os.path.join(LOCAL, 'topology_cross.py'), f'{VM_DIR}/topology_cross.py')

sftp.close()
ssh.close()
print('DONE - both files uploaded')
