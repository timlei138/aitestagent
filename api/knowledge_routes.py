from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
SEMANTIC_KNOWLEDGE_TYPES = {"constraint", "negative_knowledge", "semantic_hint"}
# 任务规划摘要也走知识库（由 save_task_plan_summary 写入），删除/列出时需放行。
ALLOWED_KNOWLEDGE_TYPES = SEMANTIC_KNOWLEDGE_TYPES | {"task_plan_summary"}

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


class KnowledgeUpdate(BaseModel):
    """编辑知识：优先通过旧条目 ID 精确替换；无 ID 时退化为严格唯一匹配。"""

    old_entry_id: str = ""
    old_content: str
    old_app_package: str = ""
    old_knowledge_type: str = ""
    new_entry: KnowledgeEntry


class SearchRequest(BaseModel):
    query: str
    app_package: str = ""
    knowledge_type: str = ""
    top_k: int = 10


def _save_entry(kb, entry: KnowledgeEntry) -> None:
    from data.knowledge import UIKnowledge

    if entry.knowledge_type not in SEMANTIC_KNOWLEDGE_TYPES:
        raise HTTPException(status_code=400, detail="不支持的 knowledge_type")

    knowledge = UIKnowledge(
        app_package=entry.app_package,
        knowledge_type=entry.knowledge_type,
        content=entry.content,
        metadata=entry.metadata,
    )
    kb.save_knowledge(knowledge)


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
    try:
        results = kb.query(
            req.query,
            app_package=req.app_package,
            knowledge_type=req.knowledge_type,
            top_k=req.top_k,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    try:
        results = kb.list_entries(
            app_package=app_package,
            knowledge_type=knowledge_type,
            top_k=top_k,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if query and query != "*":
        q = query.strip().lower()
        results = [r for r in results if q in str(r.get("content", "")).lower()]
    return {"status": "success", "items": results, "total": len(results)}


@router.post("")
def add_knowledge(entry: KnowledgeEntry):
    """手动新增一条已审核 v2 语义知识。"""
    kb = _get_kb()
    _save_entry(kb, entry)
    return {"status": "success", "message": "知识已添加"}


@router.put("")
def update_knowledge(req: KnowledgeUpdate):
    """编辑知识：删除旧条目后新增新条目。"""
    kb = _get_kb()

    delete_id = req.old_entry_id.strip()
    if not delete_id:
        where: dict[str, Any] = {}
        if req.old_app_package:
            where["app_package"] = req.old_app_package
        if req.old_knowledge_type:
            where["knowledge_type"] = req.old_knowledge_type
        candidates = kb.backend.get_by_metadata(where, limit=200)
        matched = [
            r for r in candidates if str(r.get("content", "") or "") == req.old_content
        ]
        if not matched:
            raise HTTPException(status_code=404, detail="未找到匹配的知识条目")
        if len(matched) > 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "检测到多条同内容记录，已拒绝更新以避免误改。"
                    "请刷新列表并使用 entry_id 精确更新。"
                ),
            )
        delete_id = str(matched[0].get("id", "") or "").strip()
        if not delete_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "当前记录缺少可用 entry_id，已拒绝更新以避免误改。"
                    "请刷新列表后重试。"
                ),
            )

    deleted = kb.backend.delete_by_ids([delete_id])
    if deleted == 0:
        raise HTTPException(status_code=404, detail="旧知识条目不存在或已被删除")

    _save_entry(kb, req.new_entry)
    return {"status": "success", "message": "知识已更新"}


@router.delete("")
def delete_knowledge(
    app_package: str = Query(default=""),
    knowledge_type: str = Query(default=""),
    content: str = Query(default=""),
    entry_id: str = Query(default=""),
):
    """删除知识。
    - 单条删除：优先使用 entry_id；否则使用 app_package+knowledge_type+content 精确匹配。
    - 批量删除：仅提供 app_package 和/或 knowledge_type。
    """
    kb = _get_kb()
    if knowledge_type and knowledge_type not in ALLOWED_KNOWLEDGE_TYPES:
        raise HTTPException(status_code=400, detail="不支持的 knowledge_type")
    if entry_id:
        deleted = kb.backend.delete_by_ids([entry_id])
        return {"status": "success", "deleted": deleted}
    if content:
        where: dict[str, Any] = {}
        if app_package:
            where["app_package"] = app_package
        if knowledge_type:
            where["knowledge_type"] = knowledge_type
        candidates = kb.backend.get_by_metadata(where, limit=200)
        matched = [
            r for r in candidates if str(r.get("content", "") or "") == str(content)
        ]
        if not matched:
            raise HTTPException(status_code=404, detail="未找到匹配的知识条目")
        if len(matched) > 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "检测到多条同内容记录，已拒绝删除以避免误删。"
                    "请先刷新列表并使用 entry_id 精确删除。"
                ),
            )
        first_id = str(matched[0].get("id", "") or "")
        if first_id:
            deleted = kb.backend.delete_by_ids([first_id])
            return {"status": "success", "deleted": deleted}
        raise HTTPException(
            status_code=409,
            detail=(
                "当前记录缺少可用 entry_id，已拒绝条件删除以避免误删。"
                "请刷新列表后重试。"
            ),
        )
    if not app_package and not knowledge_type:
        raise HTTPException(
            status_code=400, detail="至少提供 app_package 或 knowledge_type 之一"
        )
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
            {"value": "constraint", "label": "执行约束"},
            {"value": "negative_knowledge", "label": "已知禁止/失败模式"},
            {"value": "semantic_hint", "label": "语义提示"},
        ],
    }
