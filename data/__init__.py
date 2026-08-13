from __future__ import annotations

import logging

from config import TestConfig
from data.vector_store import VectorStoreBackend, ChromaBackend
from data.relational import RelationalBackend, SqliteBackend

logger = logging.getLogger(__name__)


def create_vector_store(config: TestConfig) -> VectorStoreBackend:
    """创建使用默认本地 ONNX embedding 的 Chroma 向量库。"""
    try:
        return ChromaBackend(persist_dir=config.rag_persist_dir)
    except Exception as exc:
        raise RuntimeError(
            f"ChromaDB 初始化失败: {exc}. "
            "Hint: onnx 模型请放到 %LOCALAPPDATA%\\AiAgentTest\\models\\bge-large-zh-onnx。"
        ) from exc


def create_relational_db(config: TestConfig) -> RelationalBackend:
    """工厂：根据配置创建关系型数据库后端。"""
    return SqliteBackend(db_path=config.db_path)


__all__ = [
    "VectorStoreBackend", "ChromaBackend",
    "RelationalBackend", "SqliteBackend",
    "create_vector_store", "create_relational_db",
]
