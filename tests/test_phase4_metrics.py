from __future__ import annotations

from agents.run_trace import compute_resolution_metrics
from data.relational import SqliteBackend


def _click(resolution_type: str, *, req: str = "", res: str = "", fuzzy: bool = False) -> dict:
    return {
        "name": "click",
        "match_mode": "element" if resolution_type == "semantic" else "resource_id",
        "resolution_type": resolution_type,
        "requested_label": req,
        "resolved_label": res,
        "fuzzy_match": fuzzy,
    }


def test_compute_resolution_metrics_basic():
    tool_log = [
        _click("exact"),
        _click("exact"),
        _click("semantic"),
        {"name": "scroll"},  # 非 click 不计数
    ]
    m = compute_resolution_metrics(tool_log)
    assert m == {
        "exact_resolution_count": 2,
        "semantic_resolution_count": 1,
        "label_mismatch_count": 0,
    }


def test_compute_resolution_metrics_label_mismatch():
    # 双方非空且不一致 → 计 mismatch；一方为空 → 不计。
    tool_log = [
        _click("exact", req="登录", res="登入", fuzzy=True),
        _click("semantic", req="", res="提交", fuzzy=True),
        _click("exact", req="确认", res="确认"),
    ]
    m = compute_resolution_metrics(tool_log)
    assert m["label_mismatch_count"] == 1
    assert m["exact_resolution_count"] == 2
    assert m["semantic_resolution_count"] == 1


def test_compute_resolution_metrics_equal_labels_not_mismatch():
    # 请求 label 与命中 label 完全相等 → 不应计为 mismatch。
    tool_log = [
        {"name": "click", "resolution_type": "exact", "requested_label": "WLAN 开关", "resolved_label": "WLAN 开关"},
        {"name": "click", "resolution_type": "semantic", "requested_label": "提交", "resolved_label": "提交"},
    ]
    m = compute_resolution_metrics(tool_log)
    assert m["exact_resolution_count"] == 1
    assert m["semantic_resolution_count"] == 1
    assert m["label_mismatch_count"] == 0


def test_compute_resolution_metrics_empty():
    assert compute_resolution_metrics([]) == {
        "exact_resolution_count": 0,
        "semantic_resolution_count": 0,
        "label_mismatch_count": 0,
    }
    assert compute_resolution_metrics(None) == {
        "exact_resolution_count": 0,
        "semantic_resolution_count": 0,
        "label_mismatch_count": 0,
    }


def test_record_execution_run_resolution_metrics_roundtrip(tmp_path):
    db = SqliteBackend(str(tmp_path / "p4.db"))
    metrics = {
        "exact_resolution_count": 3,
        "semantic_resolution_count": 1,
        "label_mismatch_count": 2,
    }
    token_usage = {
        "input_tokens": 1234,
        "output_tokens": 567,
        "total_tokens": 1801,
        "cached_input_tokens": 200,
    }
    db.record_execution_run(
        run_id="run-p4-1",
        user_request="验证",
        app_package="com.example.app",
        goal={},
        verification_contract={},
        execution_mode="direct",
        verdict="passed",
        resolution_metrics=metrics,
        duration_seconds=12.5,
        llm_call_count=7,
        token_usage=token_usage,
    )
    run = db.get_execution_run("run-p4-1")
    assert run["resolution_metrics"] == metrics
    assert run["duration_seconds"] == 12.5
    assert run["llm_call_count"] == 7
    assert run["token_usage"] == token_usage

    listed = db.list_execution_runs(limit=5)
    assert listed[0]["resolution_metrics"] == metrics
    assert listed[0]["duration_seconds"] == 12.5
    assert listed[0]["llm_call_count"] == 7
    assert listed[0]["token_usage"] == token_usage
    db._conn.close()


def test_compute_resolution_metrics_non_dict_and_no_resolution_type():
    # 非 dict 元素应被跳过；click 元素没有 resolution_type 时既不计 exact 也不计 semantic。
    tool_log = [
        None,
        "not-a-dict",
        {"name": "click", "requested_label": "a", "resolved_label": "b"},
        {"name": "scroll"},
    ]
    m = compute_resolution_metrics(tool_log)
    assert m == {
        "exact_resolution_count": 0,
        "semantic_resolution_count": 0,
        "label_mismatch_count": 1,
    }


def test_summarize_by_mode(tmp_path):
    db = SqliteBackend(str(tmp_path / "p4-summary.db"))

    # direct 模式：passed，2 次 exact 解析，1 次转换，postcond 命中 2。
    db.record_execution_run(
        run_id="d1", user_request="r", app_package="p",
        goal={}, verification_contract={}, execution_mode="direct",
        verdict="passed",
        resolution_metrics={
            "exact_resolution_count": 2,
            "semantic_resolution_count": 0,
            "label_mismatch_count": 0,
        },
        duration_seconds=10.0, llm_call_count=4,
    )
    db.record_action_events("d1", [
        {"action_index": 0, "tool_name": "click", "status": "success"},
        {"action_index": 1, "tool_name": "click", "status": "success"},
    ])
    db.record_mode_transition_events("d1", [
        {"from_mode": "Bootstrapping", "to_mode": "Direct"},
    ])
    db.record_evidence_events("d1", [{"verification_key": "v0", "status": "PASS"}])

    # explore 模式：failed，无 evidence（证据不完整），1 次 semantic 解析。
    db.record_execution_run(
        run_id="e1", user_request="r", app_package="p",
        goal={}, verification_contract={}, execution_mode="explore",
        verdict="failed",
        resolution_metrics={
            "exact_resolution_count": 0,
            "semantic_resolution_count": 1,
            "label_mismatch_count": 1,
        },
        duration_seconds=30.0, llm_call_count=20,
    )
    db.record_action_events("e1", [
        {"action_index": 0, "tool_name": "click", "status": "fail"},
    ])

    summary = db.summarize_by_mode()
    assert set(summary.keys()) == {"direct", "explore"}

    d = summary["direct"]
    assert d["run_count"] == 1
    assert d["pass_count"] == 1
    assert d["pass_rate"] == 1.0
    assert d["avg_duration_seconds"] == 10.0
    assert d["total_llm_calls"] == 4
    assert d["avg_llm_calls"] == 4.0
    assert d["mode_transition_count"] == 1
    assert d["postcond_hit_count"] == 2
    assert d["evidence_complete_rate"] == 1.0
    assert d["resolution_metrics"]["exact_resolution_count"] == 2
    assert d["resolution_metrics"]["semantic_resolution_count"] == 0

    e = summary["explore"]
    assert e["pass_rate"] == 0.0
    assert e["avg_duration_seconds"] == 30.0
    assert e["total_llm_calls"] == 20
    assert e["mode_transition_count"] == 0
    assert e["postcond_hit_count"] == 0
    assert e["evidence_complete_rate"] == 0.0
    assert e["resolution_metrics"]["semantic_resolution_count"] == 1
    assert e["resolution_metrics"]["label_mismatch_count"] == 1
    db._conn.close()


def test_summarize_by_mode_multi_run_aggregation(tmp_path):
    db = SqliteBackend(str(tmp_path / "p4-multi.db"))
    # direct 模式 3 条 run：2 passed，1 failed；耗时 10/20/30，llm 调用 4/6/8。
    for i, (verdict, duration, llm, exact, semantic, mismatch) in enumerate([
        ("passed", 10.0, 4, 2, 0, 0),
        ("passed", 20.0, 6, 1, 1, 0),
        ("failed", 30.0, 8, 0, 0, 1),
    ], start=1):
        db.record_execution_run(
            run_id=f"d{i}", user_request="r", app_package="p",
            goal={}, verification_contract={}, execution_mode="direct",
            verdict=verdict,
            resolution_metrics={
                "exact_resolution_count": exact,
                "semantic_resolution_count": semantic,
                "label_mismatch_count": mismatch,
            },
            duration_seconds=duration, llm_call_count=llm,
        )
        # 每条 run 都带 2 次 success action + 1 次 transition + evidence。
        db.record_action_events(f"d{i}", [
            {"action_index": 0, "tool_name": "click", "status": "success"},
            {"action_index": 1, "tool_name": "click", "status": "success"},
        ])
        db.record_mode_transition_events(f"d{i}", [
            {"from_mode": "Bootstrapping", "to_mode": "Direct"},
        ])
        db.record_evidence_events(f"d{i}", [{"verification_key": "v", "status": "PASS"}])

    summary = db.summarize_by_mode()
    d = summary["direct"]
    assert d["run_count"] == 3
    assert d["pass_count"] == 2
    assert d["pass_rate"] == round(2 / 3, 4)
    assert d["avg_duration_seconds"] == round(60.0 / 3, 2)
    assert d["total_llm_calls"] == 18
    assert d["avg_llm_calls"] == round(18 / 3, 2)
    assert d["mode_transition_count"] == 3
    assert d["postcond_hit_count"] == 6
    assert d["evidence_complete_rate"] == 1.0
    assert d["resolution_metrics"]["exact_resolution_count"] == 3
    assert d["resolution_metrics"]["semantic_resolution_count"] == 1
    assert d["resolution_metrics"]["label_mismatch_count"] == 1
    # Token 聚合：每条 run 没有 token_usage，所以都是 0。
    assert d["token_usage"]["total_tokens"] == 0
    db._conn.close()


def test_summarize_by_mode_token_aggregation(tmp_path):
    db = SqliteBackend(str(tmp_path / "p4-tokens.db"))
    for i, (input_t, output_t, total_t, cached_t) in enumerate([
        (1000, 500, 1500, 100),
        (2000, 800, 2800, 200),
        (3000, 1200, 4200, 300),
    ], start=1):
        db.record_execution_run(
            run_id=f"t{i}", user_request="r", app_package="p",
            goal={}, verification_contract={}, execution_mode="direct",
            verdict="passed",
            token_usage={
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
                "cached_input_tokens": cached_t,
            },
            duration_seconds=10.0 * i, llm_call_count=i,
        )
    summary = db.summarize_by_mode()
    d = summary["direct"]
    assert d["token_usage"]["total_input_tokens"] == 6000
    assert d["token_usage"]["total_output_tokens"] == 2500
    assert d["token_usage"]["total_tokens"] == 8500
    assert d["token_usage"]["total_cached_input_tokens"] == 600
    assert d["token_usage"]["avg_input_tokens"] == 2000.0
    assert d["token_usage"]["avg_output_tokens"] == round(2500 / 3, 2)
    assert d["token_usage"]["avg_total_tokens"] == round(8500 / 3, 2)
    db._conn.close()


def test_action_events_intent_and_screenshot_roundtrip(tmp_path):
    db = SqliteBackend(str(tmp_path / "p4-action.db"))
    db.record_execution_run(
        run_id="a1", user_request="r", app_package="p",
        goal={}, verification_contract={}, execution_mode="direct",
        verdict="passed",
    )
    db.record_action_events("a1", [
        {
            "action_index": 0,
            "tool_name": "click",
            "tool_input": {},
            "resolved_locator": {},
            "page_before": {},
            "page_after": {},
            "status": "success",
            "intent_text": "点击 WLAN 开关",
            "screenshot_path": "screenshots/a1/step_0.png",
            "execution_mode": "direct",
        },
    ])
    run = db.get_execution_run("a1")
    assert len(run["actions"]) == 1
    assert run["actions"][0]["intent"] == "点击 WLAN 开关"
    assert run["actions"][0]["screenshot"] == "screenshots/a1/step_0.png"
    db._conn.close()


def test_old_db_schema_self_heals(tmp_path):
    """模拟旧 schema 的 DB：先创建不带 Phase 3/4 新列的 execution_runs /
    mode_transition_events，再实例化 SqliteBackend，应能自动补齐列并成功写入。"""
    import sqlite3
    db_path = str(tmp_path / "old_schema.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE execution_runs (
            run_id TEXT PRIMARY KEY,
            user_request TEXT NOT NULL,
            app_package TEXT NOT NULL,
            goal_json TEXT NOT NULL DEFAULT '{}',
            verification_contract_json TEXT NOT NULL DEFAULT '{}',
            execution_mode TEXT NOT NULL DEFAULT 'explore',
            lifecycle_state TEXT NOT NULL DEFAULT 'Bootstrapping',
            plan_id TEXT,
            plan_trust TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT 'unknown',
            terminal_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE mode_transition_events (
            transition_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            from_mode TEXT NOT NULL,
            to_mode TEXT NOT NULL,
            reason TEXT NOT NULL,
            step_index INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    db = SqliteBackend(db_path)
    db.record_execution_run(
        run_id="old-1", user_request="r", app_package="p",
        goal={}, verification_contract={}, execution_mode="direct",
        verdict="passed",
        resolution_metrics={"exact_resolution_count": 1},
        duration_seconds=5.5, llm_call_count=3,
        token_usage={"input_tokens": 100, "output_tokens": 50},
    )
    run = db.get_execution_run("old-1")
    assert run["resolution_metrics"]["exact_resolution_count"] == 1
    assert run["duration_seconds"] == 5.5
    assert run["llm_call_count"] == 3
    assert run["token_usage"]["input_tokens"] == 100
    assert run["token_usage"]["output_tokens"] == 50
    db._conn.close()
