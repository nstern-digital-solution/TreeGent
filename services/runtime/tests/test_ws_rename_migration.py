"""Workspace self-migration across renames (R81) — 3 cases."""
import os
import re
import sys
import tempfile
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / 'app' / 'loop.py')
os.environ['TG_DEV'] = '1'


class S:
    workspace_root = tempfile.mkdtemp(prefix='ws-root-')


src = open(SRC).read()
m = re.search(r'    @staticmethod\n    def _resolve_workspace.*?\n\n    def __init__', src, re.S)
body = m.group(0)
body = body.replace('    @staticmethod\n', '')
body = body.split('\n    def __init__')[0]
# dedent 4 spaces
body = '\n'.join(l[4:] if l.startswith('    ') else l for l in body.splitlines())
ns = {'settings': S}
exec(body, ns)          # defines _resolve_workspace
rw = ns['_resolve_workspace']

root = S.workspace_root
os.makedirs(f'{root}/agent-1')
open(f'{root}/agent-1/SOUL.md', 'w').write('soul of agent-1')
os.makedirs(f'{root}/agent-2')
open(f'{root}/agent-2/SOUL.md', 'w').write('soul of agent-2')

# 1: rename with renamed_from marker
w = rw({'username': 'rosa', 'renamed_from': 'agent-1'})
assert w == f'{root}/rosa' and open(f'{w}/SOUL.md').read() == 'soul of agent-1', w
assert not os.path.exists(f'{root}/agent-1')

# 2: pre-R81 actor — no renamed_from, infra_id dir exists
w2 = rw({'username': 'elio', 'infra_id': 'agent-2'})
assert w2 == f'{root}/elio' and open(f'{w2}/SOUL.md').read() == 'soul of agent-2', w2
assert not os.path.exists(f'{root}/agent-2')

# 3: fresh agent
w3 = rw({'username': 'newbie'})
assert w3 == f'{root}/newbie'

print('workspace migration: 3/3 cases OK')
