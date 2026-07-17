from __future__ import annotations

import json

from data.relational import SqliteBackend


def test_record_and_read_token_metrics(tmp_path):
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
        exact_click_rate=0.9,
        fuzzy_click_rate=0.05,
        input_tokens=50000,
        output_tokens=3000,
        total_tokens=53000,
        cached_input_tokens=48000,
        llm_token_calls=20,
    )
    detail = db.get_test_run(run_id)
    assert detail is not None
    assert detail["llm_call_count"] == 20
    assert abs(detail["exact_click_rate"] - 0.9) < 1e-9
    assert abs(detail["fuzzy_click_rate"] - 0.05) < 1e-9
    assert detail["input_tokens"] == 50000
    assert detail["output_tokens"] == 3000
    assert detail["total_tokens"] == 53000
    assert detail["cached_input_tokens"] == 48000
    assert detail["llm_token_calls"] == 20
    listing = db.list_test_runs(limit=5)
    assert len(listing) >= 1
    assert "llm_call_count" in listing[0]
    assert "exact_click_rate" in listing[0]
    assert "input_tokens" in listing[0]
    assert "llm_token_calls" in listing[0]
