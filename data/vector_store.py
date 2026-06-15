from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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
    """ChromaDB 向量存储实现。"""

    def __init__(
        self,
        persist_dir: str = "storage/knowledge",
        embedding_model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        from langchain_chroma import Chroma
        from langchain_openai import OpenAIEmbeddings

        embeddings = OpenAIEmbeddings(model=embedding_model, api_key=api_key, base_url=base_url)
        self._store = Chroma(
            collection_name="app_knowledge",
            embedding_function=embeddings,
            persist_directory=persist_dir,
        )

    def add(self, content: str, metadata: dict[str, Any]) -> None:
        from langchain_core.documents import Document
        self._store.add_documents([Document(page_content=content, metadata=metadata)])

    def search(self, query: str, filter: dict[str, str] | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"k": top_k}
        if filter:
            kwargs["filter"] = filter
        results = self._store.similarity_search_with_score(query, **kwargs)
        return [
            {"content": doc.page_content, "metadata": doc.metadata, "score": round(float(score), 4)}
            for doc, score in results
        ]

    def delete(self, filter: dict[str, str]) -> int:
        ids = self._store.get(where=filter).get("ids", [])
        if ids:
            self._store.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        return self._store._collection.count()

    @property
    def store(self):
        """返回底层 Chroma 实例（用于 as_retriever 等高级操作）。"""
        return self._store
