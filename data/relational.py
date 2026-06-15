from __future__ import annotations

import sqlite3
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class RelationalBackend(ABC):
    """关系型数据库抽象接口。可替换为 SQLite / PostgreSQL / MySQL 等实现。"""

    @abstractmethod
    def execute(self, sql: str, params: tuple = ()) -> Any:
        """执行 SQL 并返回 cursor。"""
        ...

    @abstractmethod
    def insert(self, table: str, data: dict[str, Any]) -> int:
        """插入一行数据，返回 rowid。"""
        ...

    @abstractmethod
    def select(
        self, table: str, where: dict[str, Any] | None = None,
        order_by: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询数据，返回行字典列表。"""
        ...

    @abstractmethod
    def upsert(self, table: str, data: dict[str, Any], key: str) -> int:
        """插入或更新（按 key 列唯一）。"""
        ...

    @abstractmethod
    def count(self, table: str, where: dict[str, Any] | None = None) -> int:
        """统计行数。"""
        ...


class SqliteBackend(RelationalBackend):
    """SQLite 实现。自动建表。"""

    def __init__(self, db_path: str = "storage/test_history.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS test_runs (
                id TEXT PRIMARY KEY,
                user_request TEXT,
                app_package TEXT,
                app_name TEXT,
                status TEXT,
                conclusion TEXT,
                steps_json TEXT,
                duration_seconds REAL,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS human_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                step_index INTEGER,
                question TEXT,
                decision TEXT,
                created_at TEXT,
                FOREIGN KEY (run_id) REFERENCES test_runs(id)
            );

            CREATE TABLE IF NOT EXISTS test_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                app_package TEXT,
                yaml_path TEXT,
                steps_count INTEGER,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS element_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_package TEXT NOT NULL,
                page_signature TEXT NOT NULL,
                alias TEXT NOT NULL,
                resource_id TEXT,
                class_name TEXT,
                role TEXT,
                region TEXT,
                text_hint TEXT,
                candidates_count INTEGER DEFAULT 1,
                click_count INTEGER DEFAULT 1,
                last_used_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(app_package, page_signature, alias)
            );
        """)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self._conn.execute(sql, params)

    def insert(self, table: str, data: dict[str, Any]) -> int:
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})"
        cursor = self._conn.execute(sql, tuple(data.values()))
        self._conn.commit()
        return cursor.lastrowid or 0

    def select(self, table: str, where: dict[str, Any] | None = None,
               order_by: str = "", limit: int = 100) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table}"
        params: tuple = ()
        if where:
            clauses = " AND ".join(f"{k} = ?" for k in where)
            sql += f" WHERE {clauses}"
            params = tuple(where.values())
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def upsert(self, table: str, data: dict[str, Any], key: str) -> int:
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        updates = ", ".join(f"{k} = excluded.{k}" for k in data if k != key)
        sql = (f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
               f"ON CONFLICT({key}) DO UPDATE SET {updates}")
        cursor = self._conn.execute(sql, tuple(data.values()))
        self._conn.commit()
        return cursor.lastrowid or 0

    def count(self, table: str, where: dict[str, Any] | None = None) -> int:
        sql = f"SELECT COUNT(*) FROM {table}"
        params: tuple = ()
        if where:
            clauses = " AND ".join(f"{k} = ?" for k in where)
            sql += f" WHERE {clauses}"
            params = tuple(where.values())
        return self._conn.execute(sql, params).fetchone()[0]

    def record_test_run(self, run_id: str, user_request: str, app_package: str,
                        app_name: str, status: str, conclusion: str,
                        steps: list[dict], duration_seconds: float = 0) -> None:
        """快捷方法：记录一次测试执行。"""
        self.upsert("test_runs", {
            "id": run_id,
            "user_request": user_request,
            "app_package": app_package,
            "app_name": app_name,
            "status": status,
            "conclusion": str(conclusion)[:2000],
            "steps_json": json.dumps(steps, ensure_ascii=False),
            "duration_seconds": duration_seconds,
            "created_at": datetime.now().isoformat(),
        }, key="id")

    def record_human_decision(self, run_id: str, step_index: int,
                              question: str, decision: str) -> None:
        """快捷方法：记录一次人工决策。"""
        self.insert("human_decisions", {
            "run_id": run_id,
            "step_index": step_index,
            "question": question[:500],
            "decision": decision,
            "created_at": datetime.now().isoformat(),
        })

    def list_test_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """查询最近的测试执行记录列表。"""
        rows = self._conn.execute(
            "SELECT id, user_request, app_package, status, conclusion, "
            "steps_json, duration_seconds, created_at FROM test_runs "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            steps = json.loads(d.pop("steps_json", "[]") or "[]")
            pass_count = sum(1 for s in steps if s.get("status") == "success")
            fail_count = len(steps) - pass_count
            d["pass_count"] = pass_count
            d["fail_count"] = fail_count
            d["total_steps"] = len(steps)
            result.append(d)
        return result

    def get_test_run(self, run_id: str) -> dict[str, Any] | None:
        """查询单次测试执行的完整报告。"""
        row = self._conn.execute(
            "SELECT id, user_request, app_package, app_name, status, conclusion, "
            "steps_json, duration_seconds, created_at FROM test_runs WHERE id = ?",
            (run_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        steps = json.loads(d.pop("steps_json", "[]") or "[]")
        pass_count = sum(1 for s in steps if s.get("status") == "success")
        d["steps"] = steps
        d["pass_count"] = pass_count
        d["fail_count"] = len(steps) - pass_count
        d["total_steps"] = len(steps)
        return d

    # ── 元素身份 ──

    def save_element_identity(self, app_package: str, page_signature: str,
                              alias: str, resource_id: str = "", class_name: str = "",
                              role: str = "", region: str = "", text_hint: str = "",
                              candidates_count: int = 1) -> None:
        """保存或更新元素身份映射。click_count 递增。"""
        existing = self.select("element_identities", {
            "app_package": app_package, "page_signature": page_signature, "alias": alias,
        }, limit=1)
        now = datetime.now().isoformat()
        if existing:
            row = existing[0]
            self.insert("element_identities", {
                "app_package": app_package, "page_signature": page_signature, "alias": alias,
                "resource_id": resource_id or row.get("resource_id", ""),
                "class_name": class_name or row.get("class_name", ""),
                "role": role or row.get("role", ""),
                "region": region or row.get("region", ""),
                "text_hint": text_hint or row.get("text_hint", ""),
                "candidates_count": candidates_count,
                "click_count": int(row.get("click_count", 0)) + 1,
                "last_used_at": now,
                "updated_at": now,
                "created_at": row.get("created_at", now),
            })
        else:
            self.insert("element_identities", {
                "app_package": app_package, "page_signature": page_signature, "alias": alias,
                "resource_id": resource_id, "class_name": class_name,
                "role": role, "region": region, "text_hint": text_hint,
                "candidates_count": candidates_count, "click_count": 1,
                "last_used_at": now, "created_at": now, "updated_at": now,
            })

    def query_element_identity(self, app_package: str, alias: str,
                               page_signature: str = "") -> list[dict[str, Any]]:
        """查询元素身份映射。可限定页面签名。"""
        if page_signature:
            rows = self.select("element_identities", {
                "app_package": app_package, "page_signature": page_signature, "alias": alias,
            }, limit=1)
            if rows: return rows
        return self.select("element_identities", {
            "app_package": app_package, "alias": alias,
        }, order_by="click_count DESC", limit=3)

    def list_element_identities(self, app_package: str = "",
                                limit: int = 50) -> list[dict[str, Any]]:
        """列出元素身份映射。"""
        if app_package:
            return self.select("element_identities", {"app_package": app_package},
                              order_by="click_count DESC", limit=limit)
        return self.select("element_identities", order_by="click_count DESC", limit=limit)

    def list_test_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """列出最近测试执行记录。"""
        return self.select("test_runs", order_by="created_at DESC", limit=limit)
