import sys
out = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace')
path = 'logs/runs/165957_test-20260817_165957_langchain.log'
lines = open(path, 'r', encoding='utf-8', errors='ignore').read().splitlines()

# 找含有 assert_behavior_effect FAIL 之后的 LLM 输出（即 AI 最后一次 content）
# 关键：Text参数或 "content":"..." 紧跟工具调用后
print('=== 含 toggled FAIL 之后 200 行内出现的内容片段（AI 决策）===')
for i, l in enumerate(lines):
    if 'toggled' in l and 'FAIL' in l:
        start = i
        # 往后找下一个 'AI:' 或 'content' 或 'role":"ai"'
        for j in range(i+1, min(i+300, len(lines))):
            if '"role": "ai"' in lines[j] or 'AI:' in lines[j] or 'finish_reason' in lines[j]:
                print('L%d: %s' % (j+1, lines[j][:600]))
                break
        break

# 另外：直接找最后一条 AI message 的 content（在 reporter 之前）
print('\n=== 在 L2560 reporter 之前所有 AI content 块 ===')
for i in range(len(lines)-1, -1, -1):
    l = lines[i]
    if ('"role": "ai"' in l) or ('AI:' in l):
        # 打印这一行及前后
        lo = max(0, i-0)
        print('L%d: %s' % (i+1, l[:800]))
        # 只取最后3条
