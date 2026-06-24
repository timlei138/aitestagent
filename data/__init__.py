from __future__ import annotations

import logging

from config import TestConfig
from data.vector_store import VectorStoreBackend, ChromaBackend
from data.relational import RelationalBackend, SqliteBackend

logger = logging.getLogger(__name__)


def create_vector_store(config: TestConfig) -> VectorStoreBackend:
    """工厂：根据配置创建向量存储后端（仅 ChromaDB，强制要求向量数据库）。"""
    embedding_provider = getattr(config, "embedding_provider", "huggingface")
    if embedding_provider == "openai" and not (getattr(config, "embedding_api_key", None) or config.api_key):
        raise RuntimeError(
            "OpenAI embedding requires api_key. "
            "请在 config.yaml 中配置 embedding_api_key 或 api_key。"
        )
    try:
        return ChromaBackend(
            persist_dir=config.rag_persist_dir,
            embedding_provider=embedding_provider,
            embedding_model=getattr(config, "embedding_model", "BAAI/bge-large-zh-v1.5"),
            api_key=getattr(config, "embedding_api_key", None) or config.api_key,
            base_url=getattr(config, "embedding_base_url", None),
        )
    except Exception as exc:
        raise RuntimeError(
            f"ChromaDB 初始化失败: {exc}. "
            "Hint: 如使用 huggingface, 请 pip install langchain-huggingface sentence-transformers"
        ) from exc


def create_relational_db(config: TestConfig) -> RelationalBackend:
    """工厂：根据配置创建关系型数据库后端。"""
    return SqliteBackend(db_path=config.db_path)


__all__ = [
    "VectorStoreBackend", "ChromaBackend",
    "RelationalBackend", "SqliteBackend",
    "create_vector_store", "create_relational_db",
]
