from __future__ import annotations

import logging
import os as _os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# ═══ 模块加载时检测本地缓存，避免每次启动 ~40 次 HTTP HEAD 验证 ═══
_cache_dir = _os.path.join(
    _os.path.expanduser("~"), ".cache", "huggingface", "hub",
    "models--BAAI--bge-large-zh-v1.5"
)
if _os.path.isdir(_cache_dir):
    _os.environ.setdefault("HF_HUB_OFFLINE", "1")
    logger.info("HF model cached locally, network checks disabled")


class VectorStoreBackend(ABC):
    """向量存储抽象接口。可替换为 Chroma / Qdrant / Pinecone 等实现。"""

    @abstractmethod
    def add(self, content: str, metadata: dict[str, Any]) -> None:
        """添加一条知识到向量库。"""
        ...

    @abstractmethod
    def search(
        self, query: str, filter: dict[str, Any] | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """相似度搜索，返回 [{content, metadata, score}, ...]。"""
        ...

    @abstractmethod
    def delete(self, filter: dict[str, Any]) -> int:
        """按条件删除知识，返回删除条数。"""
        ...

    @abstractmethod
    def count(self) -> int:
        """返回知识总数。"""
        ...

    @abstractmethod
    def get_by_metadata(self, where: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
        """按 metadata 精确过滤获取，返回格式对齐 search()。"""
        ...


class ChromaBackend(VectorStoreBackend):
    """ChromaDB 向量存储实现。支持 HuggingFace 本地 embedding 和 OpenAI 远程 embedding。"""

    def __init__(
        self,
        persist_dir: str = "storage/knowledge",
        embedding_provider: str = "huggingface",   # huggingface | openai
        embedding_model: str = "BAAI/bge-large-zh-v1.5",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        from langchain_chroma import Chroma

        if embedding_provider == "huggingface":
            from langchain_huggingface import HuggingFaceEmbeddings
            embeddings = HuggingFaceEmbeddings(
                model_name=embedding_model,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
        else:
            # OpenAI 远程 embedding: 需要 API Key
            from langchain_openai import OpenAIEmbeddings
            embeddings = OpenAIEmbeddings(
                model=embedding_model, api_key=api_key, base_url=base_url
            )

        self._store = Chroma(
            collection_name="app_knowledge",
            embedding_function=embeddings,
            persist_directory=persist_dir,
        )

    def add(self, content: str, metadata: dict[str, Any]) -> None:
        from langchain_core.documents import Document
        self._store.add_documents([Document(page_content=content, metadata=metadata)])

    def _to_chroma_filter(self, filter: dict[str, Any]) -> dict[str, Any]:
        """将多 key 简单过滤转为 ChromaDB 兼容的 $and / $or 格式。

        关键：当 filter 同时含普通字段（如 app_package）和复合操作符（$or/$and）时，
        ChromaDB 要求必须用 $and 包裹，不能直接透传 flat dict。
        例如 {"app_package":"x", "$or":[...]} 必须转为 {"$and":[{"app_package":"x"},{"$or":[...]}]}
        """
        has_compound = "$or" in filter or "$and" in filter
        if not has_compound:
            if len(filter) <= 1:
                return filter
            return {"$and": [{k: v} for k, v in filter.items()]}

        # 有 $or/$and + 普通字段 → 需要包成 $and
        plain = {k: v for k, v in filter.items() if not k.startswith("$")}
        compound = {k: v for k, v in filter.items() if k.startswith("$")}
        parts = [{k: v} for k, v in plain.items()] + [{k: v} for k, v in compound.items()]
        if len(parts) == 1:
            return parts[0]
        return {"$and": parts}

    def search(self, query: str, filter: dict[str, Any] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"k": top_k}
        if filter:
            kwargs["filter"] = self._to_chroma_filter(filter)
        results = self._store.similarity_search_with_score(query, **kwargs)
        return [
            {"content": doc.page_content, "metadata": doc.metadata, "score": round(float(score), 4)}
            for doc, score in results
        ]

    def delete(self, filter: dict[str, Any]) -> int:
        where = self._to_chroma_filter(filter)
        ids = self._store.get(where=where).get("ids", [])
        if ids:
            self._store.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        return self._store._collection.count()

    def get_by_metadata(self, where: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
        """按 metadata 精确过滤获取，返回格式对齐 search()。"""
        try:
            raw = self._store.get(
                where=where, limit=limit,
                include=["documents", "metadatas"],
            )
        except Exception:
            logger.exception("get_by_metadata failed")
            return []
        return [
            {"content": doc, "metadata": meta, "score": 1.0}
            for doc, meta in zip(raw.get("documents", []), raw.get("metadatas", []))
        ]

    @property
    def store(self):
        """返回底层 Chroma 实例（用于 as_retriever 等高级操作）。"""
        return self._store
