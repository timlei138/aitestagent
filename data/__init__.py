from __future__ import annotations

import logging

from config import TestConfig
from data.vector_store import VectorStoreBackend, MemoryBackend, ChromaBackend
from data.relational import RelationalBackend, SqliteBackend

logger = logging.getLogger(__name__)


def create_vector_store(config: TestConfig) -> VectorStoreBackend:
    """工厂：根据配置创建向量存储后端。
    注意: HuggingFace 模式不需要 api_key，不能用 api_key 作为启用条件。
    """
    # HuggingFace 模式不依赖 api_key； OpenAI 模式需要 api_key
    embedding_provider = getattr(config, "embedding_provider", "huggingface")
    if embedding_provider == "openai" and not (getattr(config, "embedding_api_key", None) or config.api_key):
        logger.warning("OpenAI embedding requires api_key, falling back to MemoryBackend")
        return MemoryBackend()
    try:
        return ChromaBackend(
            persist_dir=config.rag_persist_dir,
            embedding_provider=embedding_provider,
            embedding_model=getattr(config, "embedding_model", "BAAI/bge-large-zh-v1.5"),
            api_key=getattr(config, "embedding_api_key", None) or config.api_key,
            base_url=getattr(config, "embedding_base_url", None),
        )
    except Exception as exc:
        logger.warning(
            "ChromaDB init failed: %s, falling back to MemoryBackend. "
            "Hint: 如使用 huggingface, 请 pip install langchain-huggingface sentence-transformers",
            exc
        )
    return MemoryBackend()


def create_relational_db(config: TestConfig) -> RelationalBackend:
    """工厂：根据配置创建关系型数据库后端。"""
    return SqliteBackend(db_path=config.db_path)


__all__ = [
    "VectorStoreBackend", "MemoryBackend", "ChromaBackend",
    "RelationalBackend", "SqliteBackend",
    "create_vector_store", "create_relational_db",
]
