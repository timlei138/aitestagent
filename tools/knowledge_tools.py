"""知识查询类工具（RAG 操作经验 / 人工规则 / 元素身份）。

从 tools/__init__.py 拆出（重构 T4），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import hashlib
import re

from tools.context import get_tool_context

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


@tool
def request_knowledge(intent: str) -> str:
    """Request reviewed semantic constraints, negative knowledge, and hints for the current app."""
    from tools import _capture_page_id  # 延迟 import 避免加载期循环依赖

    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = ctx.device.current_app().get("package", "")

    # 埋点：统计 RAG 查询次数
    ctx._rag_query_count = int(getattr(ctx, "_rag_query_count", 0) or 0) + 1

    # 缓存键含 page_signature_hash，页面变化自动失效
    try:
        page_sig = _capture_page_id(ctx) or "unknown"
    except Exception:
        page_sig = "unknown"
    _cache = getattr(ctx, "_rag_query_cache", None)
    if _cache is None:
        _cache = {}
        ctx._rag_query_cache = _cache
    query_norm = (intent or "").strip().lower()
    run_tag = getattr(ctx, "_run_tag", "") or ""
    cache_key = f"{run_tag}|{package}|{query_norm}|{page_sig}"
    if cache_key in _cache:
        return _cache[cache_key]

    grouped = ctx.knowledge_base.query_semantic_knowledge(package, top_k=5)
    available = sum(len(items) for items in grouped.values())
    ctx._rag_same_app_count = (
        int(getattr(ctx, "_rag_same_app_count", 0) or 0) + available
    )
    if not available:
        ctx._rag_empty_hit_count = int(getattr(ctx, "_rag_empty_hit_count", 0) or 0) + 1

    headings = {
        "constraint": "## 执行约束",
        "negative_knowledge": "## 已知禁止/失败模式",
        "semantic_hint": "## 语义提示（不得覆盖验收）",
    }
    parts = [
        headings[knowledge_type] + "\n" + "\n".join(f"- {item}" for item in items)
        for knowledge_type, items in grouped.items()
        if items
    ]
    result = "\n\n".join(parts) if parts else f"未找到 '{intent}' 的已审核知识"
    _cache[cache_key] = result
    return result


def _query_tokens(query_lower: str) -> set[str]:
    """把 query 切成可匹配的 token，中英文都work（R6 修复）。

    旧实现用 `.split()` 按空格分词——中文没有空格，整句变成一个 token，
    `if 整句 in content` 几乎永不命中 → relevance 恒 0 → rerank 形同虚设。
    现改为：英文按词、中文按**字符 2-gram**（外加单字兜底），让中文查询也有真实信号。
    """
    q = (query_lower or "").strip()
    tokens: set[str] = set()
    # 英文/数字词（长度 >= 2）
    for w in re.findall(r"[a-z0-9_]{2,}", q):
        tokens.add(w)
    # 中文字符 2-gram（无空格分词的通用做法）
    cjk = re.findall(r"[\u4e00-\u9fff]", q)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    # 兜底：query 只有单个中文字时，用单字
    if not tokens and cjk:
        tokens.update(cjk)
    return tokens


def _experience_relevance(entry: dict, query_lower: str) -> int:
    """轻量语义相关性：content 中命中的 query token 数（中英文通用）。"""
    content = str(entry.get("content", "") or "").lower()
    if not content:
        return 0
    tokens = _query_tokens(query_lower)
    if not tokens:
        return 0
    return sum(1 for t in tokens if t in content)


@tool
def query_locator_knowledge(alias: str, app_package: str = "") -> str:
    """Query current-page locator knowledge from previous successful clicks."""
    ctx = get_tool_context()
    package = app_package or ctx.device.current_app().get("package", "")
    sig = ""
    try:
        sig = ctx.perceiver.screen_signature()[:16] if ctx.perceiver else ""
    except Exception:
        pass
    # P6.3: 延迟导入避免循环依赖 (tools -> graph -> tools)
    db = None
    try:
        from agents.graph import _relational_db as _gdb

        db = _gdb
    except ImportError:
        pass
    if db is None:
        from data import create_relational_db
        from config import TestConfig

        db = create_relational_db(TestConfig())

    try:
        rows = db.query_locator_knowledge(package, alias=alias, page_signature=sig)
        if not rows:
            return f"No known locator for '{alias}' on {package}"
        lines = [f"Known locators for '{alias}' on {package}:"]
        for r in rows:
            lines.append(
                f"  rid={r['resource_id']} class={r['class_name']} "
                f"role={r['role']} region={r['region']} "
                f"successes={r['success_count']} failures={r['failure_count']}"
            )
        return chr(10).join(lines)
    except Exception as exc:
        return f"Locator knowledge query failed: {exc}"
