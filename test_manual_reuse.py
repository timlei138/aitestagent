#!/usr/bin/env python
"""手动测试脚本：验证 v3 复用计划重跑功能。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import shutil

from agents.orchestrator import TestOrchestrator
from config import TestConfig


def cleanup_dir(path: str):
    """安全清理目录（处理 Windows 文件锁问题）。"""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def test_orchestrator_reuse_plan():
    """测试 orchestrator 复用计划功能。"""
    print("\n=== 测试 1: Orchestrator 复用计划 ===")
    
    tmpdir = tempfile.mkdtemp()
    try:
        db_path = Path(tmpdir) / "test.db"
        
        # 创建 orchestrator
        config = TestConfig.from_yaml("config.yaml")
        orchestrator = TestOrchestrator(config)
        
        # 测试 busy-guard
        print("\n1.1 测试 busy-guard:")
        result1 = orchestrator.start(
            user_request="测试1",
            app_package="com.android.settings",
        )
        print(f"  第一次运行: status={result1.get('status')}")
        
        result2 = orchestrator.start(
            user_request="测试2",
            app_package="com.android.settings",
        )
        print(f"  第二次运行(等待中): status={result2.get('status')}")
        
        # 测试复跑
        print("\n1.2 测试复跑 (reuse_plan=True):")
        goal_description = {
            "goal": "测试目标",
            "app_package": "com.android.settings",
            "app_name": "Settings",
            "target_pages": ["设置主页"],
            "verification": ["验证成功"],
            "hints": ["测试提示"],
        }
        result = orchestrator.start(
            user_request="复跑测试",
            app_package="com.android.settings",
            goal_description=goal_description,
            reuse_plan=True,
            run_type="rerun",
            source_run_id="test-source-123",
        )
        print(f"  复跑结果: status={result.get('status')}, thread_id={result.get('thread_id')}")
        
    finally:
        cleanup_dir(tmpdir)


def test_database_schema():
    """测试数据库表结构。"""
    print("\n=== 测试 2: 数据库表结构 ===")
    
    from data.relational import SqliteBackend
    
    tmpdir = tempfile.mkdtemp()
    try:
        db_path = Path(tmpdir) / "test.db"
        db = SqliteBackend(str(db_path))
        
        # 检查 test_runs 表
        print("\n2.1 test_runs 表结构:")
        cursor = db._conn.execute("PRAGMA table_info(test_runs)")
        cols = {r[1] for r in cursor.fetchall()}
        print(f"  列: {sorted(cols)}")
        
        required_cols = {"goal_json", "run_type", "source_run_id"}
        missing = required_cols - cols
        if missing:
            print(f"  ❌ 缺失列: {missing}")
        else:
            print(f"  ✅ 所有必需列都存在")
        
        # 检查 test_cases 表
        print("\n2.2 test_cases 表结构:")
        cursor = db._conn.execute("PRAGMA table_info(test_cases)")
        cols = {r[1] for r in cursor.fetchall()}
        print(f"  列: {sorted(cols)}")
        
        required_cols = {"id", "name", "goal_json", "user_request", "app_package"}
        missing = required_cols - cols
        if missing:
            print(f"  ❌ 缺失列: {missing}")
        else:
            print(f"  ✅ 所有必需列都存在")
        
    finally:
        cleanup_dir(tmpdir)


def test_database_crud():
    """测试数据库 CRUD 操作。"""
    print("\n=== 测试 3: 数据库 CRUD ===")
    
    from data.relational import SqliteBackend
    
    tmpdir = tempfile.mkdtemp()
    try:
        db_path = Path(tmpdir) / "test.db"
        db = SqliteBackend(str(db_path))
        
        # 创建用例
        print("\n3.1 创建用例:")
        case_id = db.create_test_case(
            name="测试用例",
            user_request="测试请求",
            app_package="pkg",
            app_name="App",
            goal_json=json.dumps({
                "goal": "测试目标",
                "target_pages": ["页面1"],
                "verification": ["验证1"],
            }, ensure_ascii=False),
        )
        print(f"  创建成功: case_id={case_id}")
        
        # 查询用例
        print("\n3.2 查询用例:")
        case = db.get_test_case(case_id)
        print(f"  查询成功: name={case['name']}, goal_json={case['goal_json'][:50]}...")
        
        # 列出用例
        print("\n3.3 列出用例:")
        cases = db.list_test_cases()
        print(f"  共 {len(cases)} 个用例")
        
        # 更新用例
        print("\n3.4 更新用例:")
        updated = db.update_test_case(case_id, {"name": "更新后的用例"})
        print(f"  更新成功: {updated}")
        
        # 记录运行
        print("\n3.5 记录运行:")
        db.record_case_run(case_id, "completed/passed", "2026-07-22T10:00:00")
        case = db.get_test_case(case_id)
        print(f"  运行状态: last_run_status={case['last_run_status']}")
        
        # 删除用例
        print("\n3.6 删除用例:")
        deleted = db.delete_test_case(case_id)
        print(f"  删除成功: {deleted}")
        
        case = db.get_test_case(case_id)
        print(f"  查询删除后的结果: {case}")
        
    finally:
        cleanup_dir(tmpdir)


if __name__ == "__main__":
    print("=" * 60)
    print("v3 复用计划重跑功能手动测试")
    print("=" * 60)
    
    try:
        test_database_schema()
        test_database_crud()
        test_orchestrator_reuse_plan()
        
        print("\n" + "=" * 60)
        print("✅ 所有测试完成！")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
