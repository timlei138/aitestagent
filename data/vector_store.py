from __future__ import annotations

import logging
import os as _os
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# ONNX Embedding 适配器（不依赖 PyTorch / sentence-transformers）
# ═══════════════════════════════════════════════════════════════════


class ONNXEmbeddings:
    """基于 ONNX Runtime 的轻量 embedding 适配器。

    完全不调 PyTorch，打包后可减少约 300 MB。
    需要：
    - onnxruntime
    - tokenizers
    - ONNX 模型文件（由 scripts/export_onnx_model.py 导出）
    """

    def __init__(self, model_dir: str, normalize: bool = True):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._normalize = normalize
        model_path = _os.path.join(model_dir, "model.onnx")
        tokenizer_path = _os.path.join(model_dir, "tokenizer.json")

        if not _os.path.isfile(model_path):
            raise FileNotFoundError(
                f"ONNX 模型未找到: {model_path}\n"
                f"请先运行: python scripts/export_onnx_model.py"
            )
        if not _os.path.isfile(tokenizer_path):
            raise FileNotFoundError(f"tokenizer.json 未找到: {tokenizer_path}")

        self._session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_padding()
        self._tokenizer.enable_truncation(max_length=512)

        # 获取模型输入名称
        self._input_names = [inp.name for inp in self._session.get_inputs()]
        logger.info(
            "ONNXEmbeddings loaded: model=%s inputs=%s",
            model_dir,
            self._input_names,
        )

    def _encode_batch(self, texts: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        feed = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = token_type_ids

        outputs = self._session.run(None, feed)
        # outputs[0] = last_hidden_state (batch, seq, hidden)
        hidden = outputs[0]

        # Mean pooling (masked)
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        summed = np.sum(hidden * mask, axis=1)
        count = np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
        embeddings = summed / count

        if self._normalize:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms = np.clip(norms, a_min=1e-12, a_max=None)
            embeddings = embeddings / norms

        return embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 分批处理避免内存爆炸（每批 32 条）
        batch_size = 32
        all_embeds: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vecs = self._encode_batch(batch)
            all_embeds.extend(vecs.tolist())
        return all_embeds

    def embed_query(self, text: str) -> list[float]:
        vec = self._encode_batch([text])
        return vec[0].tolist()


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
    def get_by_metadata(
        self, where: dict[str, Any], limit: int = 50
    ) -> list[dict[str, Any]]:
        """按 metadata 精确过滤获取，返回格式对齐 search()。"""
        ...

    @abstractmethod
    def delete_by_ids(self, ids: list[str]) -> int:
        """按 Chroma 文档 ID 删除，返回删除条数。"""
        ...


class ChromaBackend(VectorStoreBackend):
    """使用默认本地 ONNX embedding 的 ChromaDB 向量存储实现。"""

    def __init__(
        self,
        persist_dir: str = "",
    ):
        import app_paths

        if not persist_dir:
            persist_dir = app_paths.KNOWLEDGE_DIR_STR
        from langchain_chroma import Chroma

        # ONNX Runtime 推理（无 PyTorch 依赖，节省约 300 MB）。
        embeddings = ONNXEmbeddings(model_dir=str(app_paths.ONNX_MODEL_DIR))

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
        parts = [{k: v} for k, v in plain.items()] + [
            {k: v} for k, v in compound.items()
        ]
        if len(parts) == 1:
            return parts[0]
        return {"$and": parts}

    def search(
        self, query: str, filter: dict[str, Any] | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"k": top_k}
        if filter:
            kwargs["filter"] = self._to_chroma_filter(filter)
        try:
            results = self._store.similarity_search_with_score(query, **kwargs)
        except Exception as exc:
            # Chroma 在删除后偶发 "Error finding id" 内部错误，退化到 metadata 读取避免 API 500
            if "Error finding id" in str(exc):
                logger.warning(
                    "Chroma similarity_search failed with missing id, fallback to metadata get"
                )
                raw = self._store.get(
                    where=kwargs.get("filter"),
                    limit=top_k,
                    include=["documents", "metadatas"],
                )
                return [
                    {"content": doc, "metadata": meta, "score": 1.0}
                    for doc, meta in zip(
                        raw.get("documents", []), raw.get("metadatas", [])
                    )
                ]
            raise
        return [
            {
                "content": doc.page_content,
                "metadata": doc.metadata,
                "score": round(float(score), 4),
            }
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

    def get_by_metadata(
        self, where: dict[str, Any], limit: int = 50
    ) -> list[dict[str, Any]]:
        """按 metadata 精确过滤获取，返回格式对齐 search()。"""
        try:
            kwargs: dict[str, Any] = {
                "limit": limit,
                "include": ["documents", "metadatas"],
            }
            if where:
                chroma_where = (
                    self._to_chroma_filter(where) if len(where) > 1 else where
                )
                kwargs["where"] = chroma_where
            raw = self._store.get(**kwargs)
        except Exception:
            logger.exception("get_by_metadata failed")
            return []
        return [
            {"id": _id, "content": doc, "metadata": meta, "score": 1.0}
            for _id, doc, meta in zip(
                raw.get("ids", []), raw.get("documents", []), raw.get("metadatas", [])
            )
        ]

    def delete_by_ids(self, ids: list[str]) -> int:
        cleaned = [i for i in (ids or []) if i]
        if not cleaned:
            return 0
        self._store.delete(ids=cleaned)
        return len(cleaned)

    @property
    def store(self):
        """返回底层 Chroma 实例（用于 as_retriever 等高级操作）。"""
        return self._store
