"""Fix syntax error in topology_4flow.py"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r'e:\Research Methodology\Project-Implementation\baseline_p4cci\p4cci_baseline_v2\topology_4flow.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# delete lines 257 to 261 (which contains the dangling else and duplicate info line)
del lines[257:262]

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('Fixed syntax error.')
