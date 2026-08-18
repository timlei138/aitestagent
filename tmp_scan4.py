import sys, json
out = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace')
path = 'logs/runs/165957_test-20260817_165957_langchain.log'
text = open(path, 'r', encoding='utf-8', errors='ignore').read()

# 找所有 'AI:' 开头且紧跟非 System 的片段（即 agent 实际回复）
import re
# 日志里 agent 回复行形如:  AI: 文本...  或  AI: 文本 [{'name':...}]
for m in re.finditer(r'^AI:\s*(.*)$', text, re.MULTILINE):
    seg = m.group(1)
    # 跳过太长的 system 行（通常不带工具调用且超长）
    if len(seg) < 5:
        continue
    # 只打印含决策性内容的
    if any(k in seg for k in ['toggled', 'v0', 'v1', 'v2', 'terminate', '结论', '完成', '失败', '通过', '验证', 'assert', 'click']):
        out.write('AI>>> ' + seg[:500] + '\n')
        out.write('-' * 60 + '\n')

# 找 evaluator / loop 结束原因
out.write('\n=== evaluator / loop 结束相关 ===\n')
for m in re.finditer(r'(EVALUATOR_TERMINAL|loop_break|MAX_TURNS|MAX_TOOL|NO_PROGRESS|TOGGLE_LOOP|evaluator.*verdict|verdict.*passed|verdict.*failed|all clauses|terminate_run)', text):
    s = max(0, m.start()-80)
    e = min(len(text), m.end()+80)
    snippet = text[s:e].replace('\n', ' ')
    out.write(snippet + '\n---\n')
