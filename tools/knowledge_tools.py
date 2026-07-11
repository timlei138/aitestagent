"""知识查询类工具（RAG 操作经验 / 人工规则 / 元素身份）。

从 tools/__init__.py 拆出（重构 T4），仅移动代码、不改逻辑。
"""

from __future__ import annotations

import hashlib

from tools.context import get_tool_context

try:
    from langchain_core.tools import tool
except Exception:

    def tool(func=None, *args, **kwargs):
        def wrapper(f):
            return f

        return wrapper(func) if func else wrapper


@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """Query operation experience and curated rules for the given app."""
    from tools import _capture_page_id  # 延迟 import 避免加载期循环依赖

    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = app_package or ctx.device.current_app().get("package", "")

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
    query_norm = (query or "").strip().lower()
    run_tag = getattr(ctx, "_run_tag", "") or ""
    cache_key = f"{run_tag}|{package}|{query_norm}|{page_sig}"
    if cache_key in _cache:
        return _cache[cache_key]

    parts = []
    # 并行召回：质量 + 语义双路，合并 rerank
    strong = ctx.knowledge_base.query_experience(package, query, top_k=10) if package else []
    semantic = ctx.knowledge_base.query(
        query, app_package=package, knowledge_type="experience", top_k=5
    )

    seen = set()
    merged = []
    for r in strong + semantic:
        rid_val = str(r.get("id", "") or "")
        if rid_val:
            key = rid_val
        else:
            key = hashlib.sha1(
                str(r.get("content", "") or "").strip().lower().encode("utf-8")
            ).hexdigest()[:12]
        if key not in seen:
            seen.add(key)
            merged.append(r)

    if merged and query.strip():
        merged.sort(
            key=lambda r: _experience_relevance(r, query.strip().lower()), reverse=True
        )
        merged = merged[:5]

    # RAG 来源标记（用于观测）
    n_same = sum(1 for r in merged if (r.get("metadata", {}) or {}).get("app_package", "") == package)
    ctx._rag_same_app_count = int(getattr(ctx, "_rag_same_app_count", 0) or 0) + n_same
    ctx._rag_cross_app_count = int(getattr(ctx, "_rag_cross_app_count", 0) or 0) + (len(merged) - n_same)
    if not merged:
        ctx._rag_empty_hit_count = int(getattr(ctx, "_rag_empty_hit_count", 0) or 0) + 1

    if merged:
        parts.append("## 操作经验")
        parts.extend(f"- {r['content']}" for r in merged)

    rule_text = ctx.knowledge_base.query_curated_rules(package, top_k=3)
    if rule_text:
        parts.append("## 人工知识")
        parts.append(rule_text)

    result = "\n".join(parts) if parts else f"未找到 '{query}' 的相关知识"
    _cache[cache_key] = result
    return result


def _experience_relevance(entry: dict, query_lower: str) -> int:
    """轻量语义相关性：content 中 query 词命中数。"""
    content = str(entry.get("content", "") or "").lower()
    words = [w for w in query_lower.split() if len(w) >= 2]
    if not words:
        return 0
    return sum(1 for w in words if w in content)


@tool
def query_element_identity(alias: str, app_package: str = "") -> str:
    """Query stored element identities for given alias. Returns resource_id, class, role, region from previous successful clicks."""
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
        rows = db.query_element_identity(package, alias, sig)
        if not rows:
            return f"No known identity for '{alias}' on {package}"
        lines = [f"Known identities for '{alias}' on {package}:"]
        for r in rows:
            lines.append(
                f"  rid={r['resource_id']} class={r['class_name']} "
                f"role={r['role']} region={r['region']} "
                f"clicks={r['click_count']} candidates={r['candidates_count']}"
            )
        return chr(10).join(lines)
    except Exception as exc:
        return f"Element identity query failed: {exc}"
