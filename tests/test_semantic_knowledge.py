from __future__ import annotations

import pytest

from data.knowledge import KnowledgeBase, UIKnowledge


class _FakeBackend:
    def __init__(self):
        self.records: list[dict] = []

    def add(self, content: str, metadata: dict):
        self.records.append(
            {
                "id": f"r{len(self.records) + 1}",
                "content": content,
                "metadata": dict(metadata),
            }
        )

    def search(self, query: str, filter: dict | None = None, top_k: int = 5):
        del query
        return [
            {"content": row["content"], "metadata": row["metadata"], "score": 0.0}
            for row in self.get_by_metadata(filter or {}, limit=top_k)
        ]

    def count(self) -> int:
        return len(self.records)

    def get_by_metadata(self, where: dict, limit: int = 50):
        return [
            {"id": row["id"], "content": row["content"], "metadata": row["metadata"]}
            for row in self.records
            if all(
                row["metadata"].get(key, "") == value for key, value in where.items()
            )
        ][:limit]


def _save(kb: KnowledgeBase, app_package: str, knowledge_type: str, content: str):
    kb.save_knowledge(
        UIKnowledge(
            app_package=app_package,
            knowledge_type=knowledge_type,
            content=content,
            metadata={},
        )
    )


def test_semantic_knowledge_returns_only_current_app_and_universal_entries():
    kb = KnowledgeBase(_FakeBackend())
    _save(kb, "", "constraint", "通用约束")
    _save(kb, "com.zui.gallery", "semantic_hint", "图库提示")
    _save(kb, "com.zui.calculator", "semantic_hint", "计算器提示")

    grouped = kb.query_semantic_knowledge("com.zui.gallery", top_k=10)

    assert grouped["constraint"] == ["通用约束"]
    assert grouped["semantic_hint"] == ["图库提示"]
    assert "计算器提示" not in grouped["semantic_hint"]


def test_global_knowledge_defaults_to_universal_scope():
    backend = _FakeBackend()
    kb = KnowledgeBase(backend)

    _save(kb, "", "constraint", "通用约束")

    assert backend.records[0]["metadata"]["scope"] == "universal"


@pytest.mark.parametrize("knowledge_type", ["experience", "curated_rule", "unknown"])
def test_legacy_knowledge_types_are_rejected(knowledge_type: str):
    kb = KnowledgeBase(_FakeBackend())

    with pytest.raises(ValueError):
        _save(kb, "com.zui.gallery", knowledge_type, "旧知识")
