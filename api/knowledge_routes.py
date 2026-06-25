from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# 全局 KnowledgeBase 实例由 server.py 注入
_kb_instance = None


def set_knowledge_base(kb) -> None:
    """由 server.py 启动时注入 KnowledgeBase 实例。"""
    global _kb_instance
    _kb_instance = kb


def _get_kb():
    if _kb_instance is None:
        raise HTTPException(status_code=503, detail="知识库未初始化")
    return _kb_instance


# ── 数据模型 ──

class KnowledgeEntry(BaseModel):
    app_package: str
    knowledge_type: str
    content: str
    metadata: dict[str, Any] = {}


class SearchRequest(BaseModel):
    query: str
    app_package: str = ""
    knowledge_type: str = ""
    top_k: int = 10


# ── API ──

@router.get("/count")
def get_count():
    """获取知识库总条数。"""
    kb = _get_kb()
    return {"status": "success", "count": kb.count}


@router.post("/search")
def search_knowledge(req: SearchRequest):
    """语义搜索知识库。"""
    kb = _get_kb()
    results = kb.query(
        req.query,
        app_package=req.app_package,
        knowledge_type=req.knowledge_type,
        top_k=req.top_k,
    )
    return {"status": "success", "results": results, "total": len(results)}


@router.get("/list")
def list_knowledge(
    app_package: str = Query(default=""),
    knowledge_type: str = Query(default=""),
    query: str = Query(default="*"),
    top_k: int = Query(default=50, le=200),
):
    """列出知识（默认返回最多 50 条，支持过滤）。"""
    kb = _get_kb()
    # 用一个宽泛 query 拉取数据
    q = query if query and query != "*" else (app_package or knowledge_type or "知识")
    results = kb.query(
        q,
        app_package=app_package,
        knowledge_type=knowledge_type,
        top_k=top_k,
    )
    return {"status": "success", "items": results, "total": len(results)}


@router.post("")
def add_knowledge(entry: KnowledgeEntry):
    """手动新增一条知识。"""
    kb = _get_kb()
    from data.knowledge import UIKnowledge
    knowledge = UIKnowledge(
        app_package=entry.app_package,
        knowledge_type=entry.knowledge_type,
        content=entry.content,
        metadata=entry.metadata,
    )
    kb.save_knowledge(knowledge)
    return {"status": "success", "message": "知识已添加"}


@router.delete("")
def delete_knowledge(
    app_package: str = Query(default=""),
    knowledge_type: str = Query(default=""),
):
    """按条件删除知识（app_package 和/或 knowledge_type 过滤）。"""
    if not app_package and not knowledge_type:
        raise HTTPException(status_code=400, detail="至少提供 app_package 或 knowledge_type 之一")
    kb = _get_kb()
    filter_dict: dict[str, str] = {}
    if app_package:
        filter_dict["app_package"] = app_package
    if knowledge_type:
        filter_dict["knowledge_type"] = knowledge_type
    deleted = kb.backend.delete(filter_dict)
    return {"status": "success", "deleted": deleted}


@router.get("/types")
async def get_knowledge_types():
    """返回所有知识类型枚举（前端下拉用）。"""
    return {
        "status": "success",
        "types": [
            {"value": "experience", "label": "操作经验"},
            {"value": "curated_rule", "label": "人工知识"},
        ],
    }
