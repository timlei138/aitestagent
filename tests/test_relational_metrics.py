from __future__ import annotations

import json

from data.relational import SqliteBackend


def test_record_and_read_tool_call_400_metrics(tmp_path):
    db = SqliteBackend(str(tmp_path / "metrics.db"))
    run_id = "run-metrics-1"
    db.record_test_run(
        run_id=run_id,
        user_request="u",
        app_package="pkg",
        app_name="app",
        status="success",
        conclusion="DONE: ok",
        steps=[],
        duration_seconds=1.2,
        execution_status="completed",
        test_verdict="passed",
        verification_json=json.dumps([], ensure_ascii=False),
        llm_call_count=20,
        tool_call_400_count=1,
        tool_call_400_rate=0.05,
    )
    detail = db.get_test_run(run_id)
    assert detail is not None
    assert detail["llm_call_count"] == 20
    assert detail["tool_call_400_count"] == 1
    assert abs(detail["tool_call_400_rate"] - 0.05) < 1e-9
    listing = db.list_test_runs(limit=5)
    assert len(listing) >= 1
    assert "llm_call_count" in listing[0]
    assert "tool_call_400_count" in listing[0]
    assert "tool_call_400_rate" in listing[0]
