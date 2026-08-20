"""分析 192037 日志：1/3/5周 不可选验证失败上下文。"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
PATH = "logs/runs/192037_test-20260820_192037_langchain.log"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip(line):
    return ANSI.sub("", line)


lines = [strip(l) for l in open(PATH, encoding="utf-8")]


def meaningful(s):
    return re.sub(r"\s+", " ", s).strip()


print("=== assert_behavior_effect 相关（含 disabled/enabled/selected/toast）===")
for i, l in enumerate(lines):
    low = l.lower()
    if ("assert_behavior" in low) or ("disabled" in low) or ("周" in l) or ("toast" in low) or ("selected" in low):
        m = meaningful(l)
        if len(m) > 300:
            m = m[:300] + "..."
        if m:
            print(f"  [L{i}] {m}")

print("\n=== 最终输出（最后 50 非空行）===")
nonempty = [l for l in lines if l.strip()]
for l in nonempty[-50:]:
    m = meaningful(l)
    if len(m) > 240:
        m = m[:240] + "..."
    if m:
        print("  ", m)
