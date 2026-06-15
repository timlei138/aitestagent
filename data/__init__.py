from __future__ import annotations

from config import TestConfig
from data.vector_store import VectorStoreBackend, MemoryBackend, ChromaBackend
from data.relational import RelationalBackend, SqliteBackend


def create_vector_store(config: TestConfig) -> VectorStoreBackend:
    """工厂：根据配置创建向量存储后端。"""
    if config.enable_rag and config.api_key:
        try:
            return ChromaBackend(
                persist_dir=config.rag_persist_dir,
                embedding_model=config.embedding_model,
                api_key=config.api_key,
                base_url=config.base_url,
            )
        except Exception:
            pass
    return MemoryBackend()


def create_relational_db(config: TestConfig) -> RelationalBackend:
    """工厂：根据配置创建关系型数据库后端。"""
    return SqliteBackend(db_path=config.db_path)


__all__ = [
    "VectorStoreBackend", "MemoryBackend", "ChromaBackend",
    "RelationalBackend", "SqliteBackend",
    "create_vector_store", "create_relational_db",
]
