import sys
out = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace')
path = 'logs/runs/165957_test-20260817_165957_langchain.log'
lines = open(path, 'r', encoding='utf-8', errors='ignore').read().splitlines()

# 1) 最后几条 AI 消息（content）
print('=== 最后出现的 AI content / output 行 ===')
ai_blocks = []
for i, l in enumerate(lines):
    low = l.lower()
    if '"role": "ai"' in l or 'role=ai' in low or 'AI:' in l:
        ai_blocks.append((i, l))
for i, l in ai_blocks[-6:]:
    print('L%d: %s' % (i + 1, l[:400]))

# 2) 搜索 max turns / budget / exhausted 相关
print('\n=== 限制/预算相关 ===')
for i, l in enumerate(lines):
    if any(k in l for k in ['max_turns', 'max_agent_turns', 'budget', 'MAX_TURNS',
                            'exhaust', 'iteration', 'iterations', 'reached max',
                            'tool_calls_total', 'max_tool_calls']):
        print('L%d: %s' % (i + 1, l[:300]))

# 3) 搜索 agent_node 输出 / reporter 输入
print('\n=== reporter 之前的关键节点 ===')
for i, l in enumerate(lines):
    if 'agent_node' in l or 'reporter' in l or 'planner' in l or 'plan_review' in l:
        if any(k in l for k in ['Entering', 'Exiting', 'output', 'graph']):
            print('L%d: %s' % (i + 1, l[:300]))
