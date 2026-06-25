"""RAG 旧数据清理脚本。

执行：python scripts/cleanup_old_knowledge.py
执行前请确认 storage/knowledge 路径正确。
"""
from __future__ import annotations

import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.vector_store import ChromaBackend

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    backend = ChromaBackend(persist_dir="storage/knowledge")
    total_before = backend.count()
    logger.info("清理前总数: %d", total_before)

    # 1. 删除所有旧类型
    old_types = [
        "verified_plan", "navigation_path", "page_structure",
        "test_experience", "app_precondition", "global_knowledge",
    ]
    for old in old_types:
        try:
            n = backend.delete({"knowledge_type": old})
            if n:
                logger.info("删除 %s: %d 条", old, n)
        except Exception as e:
            logger.warning("删除 %s 失败: %s", old, e)

    # 2. 删除垃圾 experience（page="" 或 action="agent"）
    for label, where in [
        ("experience page=''", {"knowledge_type": "experience", "page": ""}),
        ("experience action='agent'", {"knowledge_type": "experience", "action": "agent"}),
    ]:
        try:
            n = backend.delete(where)
            if n:
                logger.info("删除 %s: %d 条", label, n)
        except Exception as e:
            logger.warning("删除 %s 失败: %s", label, e)

    total_after = backend.count()
    logger.info("清理后总数: %d (删除 %d 条)", total_after, total_before - total_after)


if __name__ == "__main__":
    main()
