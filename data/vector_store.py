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
        self, query: str, filter: dict[str, str] | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """相似度搜索，返回 [{content, metadata, score}, ...]。"""
        ...

    @abstractmethod
    def delete(self, filter: dict[str, str]) -> int:
        """按条件删除知识，返回删除条数。"""
        ...

    @abstractmethod
    def count(self) -> int:
        """返回知识总数。"""
        ...


class MemoryBackend(VectorStoreBackend):
    """纯内存实现 —— 无 embedding 依赖，仅做关键词匹配。"""

    def __init__(self):
        self._items: list[dict[str, Any]] = []

    def add(self, content: str, metadata: dict[str, Any]) -> None:
        self._items.append({"content": content, "metadata": dict(metadata)})

    def search(self, query: str, filter: dict[str, str] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        candidates = self._filtered(filter)
        query_lower = query.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in candidates:
            score = 0
            content_lower = item["content"].lower()
            for word in query_lower.split():
                if word in content_lower:
                    score += 1
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: -x[0])
        return [{"content": s["content"], "metadata": s["metadata"], "score": float(sc)}
                for sc, s in scored[:top_k]]

    def delete(self, filter: dict[str, str]) -> int:
        before = len(self._items)
        self._items = [i for i in self._items if not self._match(i, filter)]
        return before - len(self._items)

    def count(self) -> int:
        return len(self._items)

    def _filtered(self, filter: dict[str, str] | None) -> list[dict[str, Any]]:
        if not filter:
            return list(self._items)
        return [i for i in self._items if self._match(i, filter)]

    def _match(self, item: dict, filter: dict[str, str]) -> bool:
        return all(
            str(item.get("metadata", {}).get(k, "")).lower() == str(v).lower()
            for k, v in filter.items()
        )


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

    def _to_chroma_filter(self, filter: dict[str, str]) -> dict[str, Any]:
        """将多 key 简单过滤转为 ChromaDB 兼容的 $and 格式。"""
        if len(filter) <= 1:
            return filter
        return {"$and": [{k: v} for k, v in filter.items()]}

    def search(self, query: str, filter: dict[str, str] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"k": top_k}
        if filter:
            kwargs["filter"] = self._to_chroma_filter(filter)
        results = self._store.similarity_search_with_score(query, **kwargs)
        return [
            {"content": doc.page_content, "metadata": doc.metadata, "score": round(float(score), 4)}
            for doc, score in results
        ]

    def delete(self, filter: dict[str, str]) -> int:
        where = self._to_chroma_filter(filter)
        ids = self._store.get(where=where).get("ids", [])
        if ids:
            self._store.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        return self._store._collection.count()

    @property
    def store(self):
        """返回底层 Chroma 实例（用于 as_retriever 等高级操作）。"""
        return self._store
