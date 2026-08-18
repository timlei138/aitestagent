import re, sys

out = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace')
path = 'logs/runs/165957_test-20260817_165957_langchain.log'
lines = open(path, 'r', encoding='utf-8', errors='ignore').read().splitlines()
out.write('total=' + str(len(lines)) + '\n')
kws = ['DONE:', 'ABORT:', 'toggled', 'FAIL', 'Traceback', 'stop', 'Stop',
       'conclude', 'final', 'verdict', 'exhaust', 'TERMINATE', 'reached max',
       'v1', 'v2', 'reporter', 'reporter_node']
seen = set()
for i, l in enumerate(lines):
    if any(k in l for k in kws) and l not in seen:
        seen.add(l)
        out.write('L%d: %s\n' % (i + 1, l))
