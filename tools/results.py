"""L1：工具返回契约。

统一「状态码 + 人类可读消息 + 机器可读证据」的返回格式，让**代码按字段读取**，
而不是靠散落各处的子串 grep 去反推语义（后者一旦文案变动就静默失效）。

约定的返回字符串格式：
    "<STATUS>: <message>"                      # 无证据
    "<STATUS>: <message> || k1=v1; k2=v2"      # 带机器可读证据（证据分隔符为 ' || '）

STATUS 取自有限、稳定的词表（收敛型契约，符合 §0）：
    OK / NOT_FOUND / AMBIGUOUS / ERROR / NEEDS_HUMAN / PASS / FAIL

设计要点：
- 证据分隔符用 ' || '，与 click 既有的 ' | '（页面快照/开关状态拼接）区分，避免冲突。
- parse_* 系列对「旧格式/无前缀」的历史返回保持宽容（返回空），调用方可回退到旧启发式，
  因此本契约可**增量**落地，不要求一次性改完所有工具。
"""

from __future__ import annotations

from typing import Any

OK = "OK"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
ERROR = "ERROR"
NEEDS_HUMAN = "NEEDS_HUMAN"
PASS = "PASS"
FAIL = "FAIL"

# 顺序：更长/更具体的前缀在前，避免 ERROR 抢先匹配（此处无重叠，仍保持稳定顺序）
_STATUSES = (OK, NOT_FOUND, AMBIGUOUS, NEEDS_HUMAN, ERROR, PASS, FAIL)

_EVIDENCE_SEP = " || "

# click 成功日志中 strategy= 字段属于「兜底」策略的取值（稳定枚举，替代 grep "fallback"）
FALLBACK_STRATEGIES = frozenset(
    {
        "text-fallback",
        "known-rid-fallback",
        "pct-bounds-fallback",
        "rid-fallback",
    }
)


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def make_result(status: str, message: str = "", evidence: dict | None = None) -> str:
    """构造契约字符串。message 为给 LLM/人看的自然语言；evidence 为机器可读键值。"""
    base = f"{status}: {message}".rstrip() if message else status
    if evidence:
        kv = "; ".join(
            f"{k}={_fmt(v)}" for k, v in evidence.items() if v not in (None, "")
        )
        if kv:
            base = f"{base}{_EVIDENCE_SEP}{kv}"
    return base


def parse_status(output: Any) -> str:
    """读取返回字符串开头的规范状态码；旧格式/无前缀返回 ""。"""
    s = str(output or "").lstrip()
    for tok in _STATUSES:
        if s == tok or s.startswith(tok + ":") or s.startswith(tok + _EVIDENCE_SEP):
            return tok
    return ""


def parse_evidence(output: Any) -> dict[str, str]:
    """解析 ' || k=v; k=v' 证据段；无证据返回 {}。"""
    s = str(output or "")
    if _EVIDENCE_SEP not in s:
        return {}
    tail = s.split(_EVIDENCE_SEP, 1)[1]
    ev: dict[str, str] = {}
    for pair in tail.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            k = k.strip()
            if k:
                ev[k] = v.strip()
    return ev


def strategy_of(output: Any) -> str:
    """从 click 成功日志中提取 strategy= 取值（无则 ""）。"""
    import re

    m = re.search(r"strategy=([A-Za-z0-9_\-]+)", str(output or ""))
    return m.group(1) if m else ""


def is_fallback_output(output: Any) -> bool:
    """判断 click 结果是否走了兜底策略：优先按 strategy= 枚举判定，
    回退到旧的 'fallback' 子串启发式（兼容未迁移的返回）。"""
    strat = strategy_of(output)
    if strat:
        return strat in FALLBACK_STRATEGIES
    return "fallback" in str(output or "").lower()
