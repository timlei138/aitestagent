from __future__ import annotations

from agents.rag_context import retrieve_knowledge


def test_retrieve_knowledge_task_plan_returns_relational_plan(monkeypatch):
    class FakeDB:
        def find_matching_execution_plan(
            self, app_package, query="", verification_fingerprint="", environment_key="", task_signature=None
        ):
            assert (app_package, query) == ("com.example.app", "创建课程表")
            return {"plan_id": "plan-1", "plan_trust": "candidate", "actions": []}

    monkeypatch.setattr("agents.graph._relational_db", FakeDB())

    result = retrieve_knowledge(
        app_package="com.example.app",
        purpose="task_plan",
        query="创建课程表",
        verification_fingerprint="fp",
        environment_key="env",
    )
    assert result["source"] == "relational"
    assert result["items"][0]["plan_id"] == "plan-1"


def test_retrieve_knowledge_task_plan_empty_when_no_db(monkeypatch):
    monkeypatch.setattr("agents.graph._relational_db", None)
    result = retrieve_knowledge(app_package="com.example.app", purpose="task_plan")
    assert result["source"] == "none"
    assert result["items"] == []


def test_retrieve_knowledge_rejects_unknown_purpose():
    try:
        retrieve_knowledge(app_package="com.example.app", purpose="nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError")
