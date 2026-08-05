import re, pathlib

for fname in ['agents/nodes.py', 'agents/llm_runtime.py', 'tools/__init__.py', 'tools/context.py']:
    t = pathlib.Path(fname).read_text(encoding='utf-8', errors='ignore')
    for i, line in enumerate(t.splitlines(), 1):
        if re.search(r'parse_evidence|result_evidence', line):
            print(f'{fname}:{i}: {line.strip()[:120]}')
