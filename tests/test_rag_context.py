from __future__ import annotations

import agents.graph
import agents.rag_context as rag_context
import tools.context
from agents.plan_extractor import verification_fingerprint


class _FakeVectorBackend:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def search(self, query: str, filter: dict | None = None, top_k: int = 5) -> list[dict]:
        results = []
        for doc in self._docs:
            md = doc.get("metadata", {})
            if filter:
                match = True
                for key, value in filter.items():
                    if md.get(key) != value:
                        match = False
                        break
                if not match:
                    continue
            results.append(doc)
        return results[:top_k]


class _FakeKnowledgeBase:
    def __init__(self, docs: list[dict]):
        self._backend = _FakeVectorBackend(docs)

    def query_task_plan_summaries(
        self, app_package: str, query: str, top_k: int = 5
    ) -> list[dict]:
        where = {"knowledge_type": "task_plan_summary"}
        if app_package:
            where["app_package"] = app_package
        return self._backend.search(query, where, top_k)


class _FakeRelationalDB:
    def __init__(self, plans: dict[str, dict]):
        self._plans = plans

    def get_full_execution_plan(self, plan_id: str):
        return self._plans.get(plan_id)

    def find_matching_execution_plan(self, *args, **kwargs):
        return None


def _fake_tool_context(kb):
    class Ctx:
        knowledge_base = kb
    return Ctx()


def _setup(kb, db):
    tools.context._CONTEXT = _fake_tool_context(kb)
    agents.graph._relational_db = db


def _teardown():
    tools.context._CONTEXT = None
    agents.graph._relational_db = None


def test_retrieve_knowledge_task_plan_vector_recall_and_filter():
    contract = {
        "status": "approved",
        "verifications": [
            {
                "statement": "时长设置生效",
                "clauses": [{"claim": "显示50分钟", "channels": ["ui_text"]}],
            }
        ],
    }
    slots = [
        {"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 50, "original": "50分钟"}
    ]
    templated_fp = verification_fingerprint(contract, slots)
    full_plan = {
        "plan_id": "plan-1",
        "environment_key": "env-a",
        "task_signature": {
            "user_request_template": "设置时长为{lesson_duration}",
            "action_semantics": ["点击时长"],
            "verification_semantics": ["时长设置生效"],
            "parameter_slots": slots,
            "verification_fingerprint": templated_fp,
        },
    }
    kb = _FakeKnowledgeBase(
        [
            {
                "content": "app=com.example.app | 设置时长{lesson_duration}分钟",
                "metadata": {
                    "app_package": "com.example.app",
                    "knowledge_type": "task_plan_summary",
                    "plan_id": "plan-1",
                },
            }
        ]
    )
    db = _FakeRelationalDB({"plan-1": full_plan})

    _setup(kb, db)
    try:
        query_signature = {
            "user_request_template": "设置时长为{lesson_duration}",
            "action_semantics": ["点击时长"],
            "verification_semantics": ["时长设置生效"],
            "parameter_slots": slots,
            "verification_fingerprint": templated_fp,
        }
        result = rag_context.retrieve_knowledge(
            app_package="com.example.app",
            purpose="task_plan",
            query="设置时长为50分钟",
            verification_fingerprint=templated_fp,
            environment_key="env-a",
            task_signature=query_signature,
        )
        assert result["source"] == "vector_then_relational"
        assert len(result["items"]) == 1
        assert result["items"][0]["plan_id"] == "plan-1"
    finally:
        _teardown()


def test_retrieve_knowledge_task_plan_rejects_incompatible_vector_candidate():
    contract = {
        "status": "approved",
        "verifications": [
            {
                "statement": "时长设置生效",
                "clauses": [{"claim": "显示50分钟", "channels": ["ui_text"]}],
            }
        ],
    }
    plan_slots = [
        {"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 50, "original": "50分钟"}
    ]
    templated_fp = verification_fingerprint(contract, plan_slots)
    full_plan = {
        "plan_id": "plan-1",
        "environment_key": "env-a",
        "task_signature": {
            "user_request_template": "设置时长为{lesson_duration}",
            "action_semantics": ["点击时长"],
            "verification_semantics": ["时长设置生效"],
            "parameter_slots": plan_slots,
            "verification_fingerprint": templated_fp,
        },
    }
    kb = _FakeKnowledgeBase(
        [
            {
                "content": "app=com.example.app | 设置时长{lesson_duration}分钟",
                "metadata": {
                    "app_package": "com.example.app",
                    "knowledge_type": "task_plan_summary",
                    "plan_id": "plan-1",
                },
            }
        ]
    )
    db = _FakeRelationalDB({"plan-1": full_plan})

    _setup(kb, db)
    try:
        query_signature = {
            "user_request_template": "设置时长为{lesson_duration}",
            "action_semantics": ["点击时长"],
            "verification_semantics": ["时长设置生效"],
            "parameter_slots": [
                {"name": "lesson_duration", "type": "duration", "unit": "minute", "value": 54}
            ],
            "verification_fingerprint": templated_fp,
        }
        result = rag_context.retrieve_knowledge(
            app_package="com.example.app",
            purpose="task_plan",
            query="设置时长为54分钟",
            verification_fingerprint=templated_fp,
            environment_key="env-a",
            task_signature=query_signature,
        )
        assert result["items"] == []
    finally:
        _teardown()
