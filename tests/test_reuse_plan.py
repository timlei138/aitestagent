"""Tests for v3: 复用计划重跑 / 用例管理功能。"""

from __future__ import annotations

import json

from data.relational import SqliteBackend


def test_reuse_plan_new_table_created(tmp_path):
    """验证 test_cases 表在新建数据库时正确创建。"""
    db = SqliteBackend(str(tmp_path / "test.db"))
    
    # 验证 test_cases 表存在
    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='test_cases'"
    )
    assert cursor.fetchone() is not None
    
    # 验证表结构包含必要字段
    cursor = db._conn.execute("PRAGMA table_info(test_cases)")
    cols = {r[1] for r in cursor.fetchall()}
    assert "id" in cols
    assert "name" in cols
    assert "goal_json" in cols
    assert "user_request" in cols
    assert "app_package" in cols


def test_reuse_plan_new_columns_in_test_runs(tmp_path):
    """验证 test_runs 表新列在新建数据库时正确创建。"""
    db = SqliteBackend(str(tmp_path / "test.db"))
    
    # 验证新列存在
    cursor = db._conn.execute("PRAGMA table_info(test_runs)")
    cols = {r[1] for r in cursor.fetchall()}
    assert "goal_json" in cols
    assert "run_type" in cols
    assert "source_run_id" in cols
    
    # 验证默认值正确
    run_id = "run-defaults-1"
    db.record_test_run(
        run_id=run_id,
        user_request="u",
        app_package="pkg",
        app_name="app",
        status="success",
        conclusion="DONE: ok",
        steps=[],
        duration_seconds=1.0,
        execution_status="completed",
        test_verdict="passed",
        verification_json=json.dumps([], ensure_ascii=False),
        llm_call_count=1,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        cached_input_tokens=0,
        llm_token_calls=1,
    )
    
    detail = db.get_test_run(run_id)
    assert detail["goal_json"] == "{}"
    assert detail["run_type"] == "normal"
    assert detail["source_run_id"] is None


def test_create_test_case(tmp_path):
    """验证 test_cases CRUD 操作。"""
    db = SqliteBackend(str(tmp_path / "test.db"))
    
    # 创建用例
    case_id = db.create_test_case(
        name="WIFI 开关验证",
        user_request="验证WIFI开关功能",
        app_package="com.android.settings",
        app_name="Settings",
        goal_json=json.dumps({
            "goal": "验证WIFI开关可以从关到开",
            "app_package": "com.android.settings",
            "app_name": "Settings",
            "target_pages": ["设置主页"],
            "verification": ["WIFI开关可从关到开"],
            "hints": ["点击WLAN进入设置"],
        }, ensure_ascii=False),
    )
    assert case_id is not None
    
    # 查询单个用例
    case = db.get_test_case(case_id)
    assert case is not None
    assert case["name"] == "WIFI 开关验证"
    assert case["user_request"] == "验证WIFI开关功能"
    assert "WIFI开关可以从关到开" in case["goal_json"]
    
    # 更新用例
    updated = db.update_test_case(case_id, {"name": "WIFI 开关验证 v2"})
    assert updated
    
    updated_case = db.get_test_case(case_id)
    assert updated_case["name"] == "WIFI 开关验证 v2"
    
    # 查询列表
    cases = db.list_test_cases()
    assert len(cases) >= 1
    assert cases[0]["name"] == "WIFI 开关验证 v2"
    
    # 删除用例
    deleted = db.delete_test_case(case_id)
    assert deleted
    
    case = db.get_test_case(case_id)
    assert case is None


def test_record_case_run(tmp_path):
    """验证 record_case_run 更新 last_run_status 和 last_run_at。"""
    db = SqliteBackend(str(tmp_path / "test.db"))
    
    # 创建用例
    case_id = db.create_test_case(
        name="测试用例",
        user_request="测试",
        app_package="pkg",
        app_name="app",
        goal_json="{}",
    )
    
    # 记录运行状态
    db.record_case_run(case_id, "completed/passed", "2026-07-22T10:00:00")
    
    case = db.get_test_case(case_id)
    assert case["last_run_status"] == "completed/passed"
    assert case["last_run_at"] == "2026-07-22T10:00:00"


def test_list_test_runs_includes_new_columns(tmp_path):
    """验证 list_test_runs 返回新列。"""
    db = SqliteBackend(str(tmp_path / "test.db"))
    
    run_id = "run-test-1"
    db.record_test_run(
        run_id=run_id,
        user_request="u",
        app_package="pkg",
        app_name="app",
        status="success",
        conclusion="DONE: ok",
        steps=[],
        duration_seconds=1.0,
        execution_status="completed",
        test_verdict="passed",
        verification_json=json.dumps([], ensure_ascii=False),
        llm_call_count=1,
        goal_json='{"goal":"test"}',
        run_type="rerun",
        source_run_id="source-123",
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        cached_input_tokens=0,
        llm_token_calls=1,
    )
    
    runs = db.list_test_runs(limit=10)
    assert len(runs) >= 1
    assert runs[0]["goal_json"] == '{"goal":"test"}'
    assert runs[0]["run_type"] == "rerun"
    assert runs[0]["source_run_id"] == "source-123"


def test_get_test_run_includes_new_columns(tmp_path):
    """验证 get_test_run 返回新列。"""
    db = SqliteBackend(str(tmp_path / "test.db"))
    
    run_id = "run-test-2"
    db.record_test_run(
        run_id=run_id,
        user_request="u",
        app_package="pkg",
        app_name="app",
        status="success",
        conclusion="DONE: ok",
        steps=[],
        duration_seconds=1.0,
        execution_status="completed",
        test_verdict="passed",
        verification_json=json.dumps([], ensure_ascii=False),
        llm_call_count=1,
        goal_json='{"goal":"test2"}',
        run_type="normal",
        source_run_id=None,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        cached_input_tokens=0,
        llm_token_calls=1,
    )
    
    detail = db.get_test_run(run_id)
    assert detail["goal_json"] == '{"goal":"test2"}'
    assert detail["run_type"] == "normal"
    assert detail["source_run_id"] is None
