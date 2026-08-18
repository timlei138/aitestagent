from __future__ import annotations

from data.knowledge import KnowledgeBase, UIKnowledge


class _InMemoryVectorBackend:
    """Minimal vector backend for unit tests."""

    def __init__(self):
        self._docs: list[dict] = []

    def add(self, content: str, metadata: dict) -> None:
        self._docs.append({"content": content, "metadata": dict(metadata)})

    def search(self, query: str, filter: dict | None = None, top_k: int = 5) -> list[dict]:
        # Naive substring match for deterministic tests.
        results = []
        for doc in self._docs:
            md = doc["metadata"]
            if filter:
                match = True
                for key, value in filter.items():
                    if md.get(key) != value:
                        match = False
                        break
                if not match:
                    continue
            score = 1.0 if query in doc["content"] else 0.0
            results.append({**doc, "score": score})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def get_by_metadata(self, where: dict, limit: int = 50) -> list[dict]:
        return self.search("", where, limit)

    def count(self) -> int:
        return len(self._docs)

    def delete(self, filter: dict) -> None:
        pass

    def delete_by_ids(self, ids: list) -> None:
        pass


def test_save_task_plan_summary_and_query():
    kb = KnowledgeBase(_InMemoryVectorBackend())
    kb.save_task_plan_summary(
        app_package="com.example.app",
        plan_id="plan-1",
        summary="app=com.example.app | 新建课程表 | 设置时长{MINUTE}分钟",
        verification_fingerprint="vf-1",
        user_request_template="设置时长为{MINUTE}分钟",
        parameter_slots=[
            {"name": "MINUTE", "type": "duration", "unit": "minute", "value": 50}
        ],
        environment_key="env-1",
    )

    results = kb.query_task_plan_summaries(
        app_package="com.example.app", query="新建课程表"
    )
    assert len(results) == 1
    assert results[0]["metadata"]["plan_id"] == "plan-1"
    assert results[0]["metadata"]["knowledge_type"] == "task_plan_summary"


def test_query_task_plan_summaries_filters_by_app_package():
    kb = KnowledgeBase(_InMemoryVectorBackend())
    kb.save_task_plan_summary(
        app_package="com.a.app",
        plan_id="plan-a",
        summary="app=com.a.app | 任务A",
    )
    kb.save_task_plan_summary(
        app_package="com.b.app",
        plan_id="plan-b",
        summary="app=com.b.app | 任务B",
    )

    results = kb.query_task_plan_summaries(app_package="com.a.app", query="任务")
    assert len(results) == 1
    assert results[0]["metadata"]["plan_id"] == "plan-a"


def test_semantic_knowledge_query_excludes_task_plan_summary():
    kb = KnowledgeBase(_InMemoryVectorBackend())
    kb.save_task_plan_summary(
        app_package="com.example.app",
        plan_id="plan-1",
        summary="新建课程表",
    )
    kb.save_knowledge(
        UIKnowledge(
            app_package="com.example.app",
            knowledge_type="semantic_hint",
            content="使用 vision_tap",
            metadata={},
        )
    )
    grouped = kb.query_semantic_knowledge("com.example.app")
    assert "task_plan_summary" not in grouped
    assert grouped["semantic_hint"] == ["使用 vision_tap"]
