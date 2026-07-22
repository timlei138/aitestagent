from __future__ import annotations

import re
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
        self,
        table: str,
        where: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int = 100,
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

    def __init__(self, db_path: str = ""):
        import app_paths

        db_path = db_path or app_paths.DB_PATH_STR
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
                created_at TEXT,
                execution_status TEXT DEFAULT '',
                test_verdict TEXT DEFAULT '',
                verification_json TEXT DEFAULT '[]',
                llm_call_count INTEGER DEFAULT 0,
                click_count INTEGER DEFAULT 0,
                fuzzy_click_count INTEGER DEFAULT 0,
                ambiguous_count INTEGER DEFAULT 0,
                exact_click_count INTEGER DEFAULT 0,
                exact_click_rate REAL DEFAULT 0,
                fuzzy_click_rate REAL DEFAULT 0,
                rag_query_count INTEGER DEFAULT 0,
                rag_same_app_ratio REAL DEFAULT 0,
                rag_empty_hit_rate REAL DEFAULT 0,
                rag_cross_app_used_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cached_input_tokens INTEGER DEFAULT 0,
                llm_token_calls INTEGER DEFAULT 0,
                goal_json TEXT NOT NULL DEFAULT '{}',
                run_type TEXT NOT NULL DEFAULT 'normal',
                source_run_id TEXT
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
                screen_width INTEGER DEFAULT 0,
                screen_height INTEGER DEFAULT 0,
                bounds_json TEXT DEFAULT '',
                UNIQUE(app_package, page_signature, alias)
            );

            CREATE TABLE IF NOT EXISTS test_cases (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_run_id TEXT,
                user_request TEXT,
                app_package TEXT,
                app_name TEXT,
                goal_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                last_run_status TEXT NOT NULL DEFAULT '',
                last_run_at TEXT NOT NULL DEFAULT ''
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

    def select(
        self,
        table: str,
        where: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
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
        sql = (
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT({key}) DO UPDATE SET {updates}"
        )
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

    def record_test_run(
        self,
        run_id: str,
        user_request: str,
        app_package: str,
        app_name: str,
        status: str,
        conclusion: str,
        steps: list[dict],
        duration_seconds: float = 0,
        execution_status: str = "",
        test_verdict: str = "",
        verification_json: str = "[]",
        llm_call_count: int = 0,
        click_count: int = 0,
        fuzzy_click_count: int = 0,
        ambiguous_count: int = 0,
        exact_click_count: int = 0,
        exact_click_rate: float = 0.0,
        fuzzy_click_rate: float = 0.0,
        rag_query_count: int = 0,
        rag_same_app_ratio: float = 0.0,
        rag_empty_hit_rate: float = 0.0,
        rag_cross_app_used_count: int = 0,
        # O1: token 消耗观测
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cached_input_tokens: int = 0,
        llm_token_calls: int = 0,
        # v3: 用例管理 / 复跑
        goal_json: str = "{}",
        run_type: str = "normal",
        source_run_id: str | None = None,
    ) -> None:
        """快捷方法：记录一次测试执行。"""
        self.upsert(
            "test_runs",
            {
                "id": run_id,
                "user_request": user_request,
                "app_package": app_package,
                "app_name": app_name,
                "status": status,
                "conclusion": str(conclusion)[:2000],
                "steps_json": json.dumps(steps, ensure_ascii=False),
                "duration_seconds": duration_seconds,
                "execution_status": execution_status,
                "test_verdict": test_verdict,
                "verification_json": verification_json,
                "llm_call_count": llm_call_count,
                "click_count": click_count,
                "fuzzy_click_count": fuzzy_click_count,
                "ambiguous_count": ambiguous_count,
                "exact_click_count": exact_click_count,
                "exact_click_rate": exact_click_rate,
                "fuzzy_click_rate": fuzzy_click_rate,
                "rag_query_count": rag_query_count,
                "rag_same_app_ratio": rag_same_app_ratio,
                "rag_empty_hit_rate": rag_empty_hit_rate,
                "rag_cross_app_used_count": rag_cross_app_used_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cached_input_tokens": cached_input_tokens,
                "llm_token_calls": llm_token_calls,
                "goal_json": goal_json,
                "run_type": run_type,
                "source_run_id": source_run_id,
                "created_at": datetime.now().isoformat(),
            },
            key="id",
        )

    def record_human_decision(
        self, run_id: str, step_index: int, question: str, decision: str
    ) -> None:
        """快捷方法：记录一次人工决策。"""
        self.insert(
            "human_decisions",
            {
                "run_id": run_id,
                "step_index": step_index,
                "question": question[:500],
                "decision": decision,
                "created_at": datetime.now().isoformat(),
            },
        )

    def list_test_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """查询最近的测试执行记录列表。"""
        rows = self._conn.execute(
            "SELECT id, user_request, app_package, status, conclusion, "
            "steps_json, duration_seconds, created_at, "
            "execution_status, test_verdict, "
            "llm_call_count, "
            "click_count, fuzzy_click_count, ambiguous_count, exact_click_count, "
            "exact_click_rate, fuzzy_click_rate, "
            "rag_query_count, rag_same_app_ratio, rag_empty_hit_rate, "
            "rag_cross_app_used_count, "
            "input_tokens, output_tokens, total_tokens, cached_input_tokens, llm_token_calls, "
            "COALESCE(goal_json,'{}') AS goal_json, "
            "COALESCE(run_type,'normal') AS run_type, "
            "source_run_id "
            "FROM test_runs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            steps = json.loads(d.pop("steps_json", "[]") or "[]")
            pass_count = sum(
                1 for s in steps if s.get("status") in ("success", "continue")
            )
            fail_count = sum(1 for s in steps if s.get("status") == "fail")
            d["pass_count"] = pass_count
            d["fail_count"] = fail_count
            d["total_steps"] = len(steps)
            if not d.get("execution_status"):
                d["execution_status"] = (
                    "completed" if d["status"] == "success" else "error"
                )
            if not d.get("test_verdict"):
                d["test_verdict"] = (
                    "passed" if d["status"] == "success" else "inconclusive"
                )
            d["llm_call_count"] = int(d.get("llm_call_count", 0) or 0)
            d["click_count"] = int(d.get("click_count", 0) or 0)
            d["fuzzy_click_count"] = int(d.get("fuzzy_click_count", 0) or 0)
            d["ambiguous_count"] = int(d.get("ambiguous_count", 0) or 0)
            d["exact_click_count"] = int(d.get("exact_click_count", 0) or 0)
            d["exact_click_rate"] = float(d.get("exact_click_rate", 0) or 0)
            d["fuzzy_click_rate"] = float(d.get("fuzzy_click_rate", 0) or 0)
            d["input_tokens"] = int(d.get("input_tokens", 0) or 0)
            d["output_tokens"] = int(d.get("output_tokens", 0) or 0)
            d["total_tokens"] = int(d.get("total_tokens", 0) or 0)
            d["cached_input_tokens"] = int(d.get("cached_input_tokens", 0) or 0)
            d["llm_token_calls"] = int(d.get("llm_token_calls", 0) or 0)
            d["goal_json"] = d.get("goal_json") or "{}"
            d["run_type"] = d.get("run_type") or "normal"
            # source_run_id 保持为 None 或字符串
            if d.get("source_run_id") is None:
                d["source_run_id"] = None
            result.append(d)
        return result

    def get_test_run(self, run_id: str) -> dict[str, Any] | None:
        """查询单次测试执行的完整报告。"""
        row = self._conn.execute(
            "SELECT id, user_request, app_package, app_name, status, conclusion, "
            "steps_json, duration_seconds, created_at, "
            "execution_status, test_verdict, verification_json, "
            "llm_call_count, "
            "click_count, fuzzy_click_count, ambiguous_count, exact_click_count, "
            "exact_click_rate, fuzzy_click_rate, "
            "rag_query_count, rag_same_app_ratio, rag_empty_hit_rate, "
            "rag_cross_app_used_count, "
            "input_tokens, output_tokens, total_tokens, cached_input_tokens, llm_token_calls, "
            "COALESCE(goal_json,'{}') AS goal_json, "
            "COALESCE(run_type,'normal') AS run_type, "
            "source_run_id "
            "FROM test_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        steps = json.loads(d.pop("steps_json", "[]") or "[]")
        pass_count = sum(1 for s in steps if s.get("status") in ("success", "continue"))
        fail_count = sum(1 for s in steps if s.get("status") == "fail")
        d["steps"] = steps
        d["pass_count"] = pass_count
        d["fail_count"] = fail_count
        d["total_steps"] = len(steps)
        if not d.get("execution_status"):
            d["execution_status"] = "completed" if d["status"] == "success" else "error"
        if not d.get("test_verdict"):
            d["test_verdict"] = "passed" if d["status"] == "success" else "inconclusive"
        d["llm_call_count"] = int(d.get("llm_call_count", 0) or 0)
        d["click_count"] = int(d.get("click_count", 0) or 0)
        d["fuzzy_click_count"] = int(d.get("fuzzy_click_count", 0) or 0)
        d["ambiguous_count"] = int(d.get("ambiguous_count", 0) or 0)
        d["exact_click_count"] = int(d.get("exact_click_count", 0) or 0)
        d["exact_click_rate"] = float(d.get("exact_click_rate", 0) or 0)
        d["fuzzy_click_rate"] = float(d.get("fuzzy_click_rate", 0) or 0)
        d["input_tokens"] = int(d.get("input_tokens", 0) or 0)
        d["output_tokens"] = int(d.get("output_tokens", 0) or 0)
        d["total_tokens"] = int(d.get("total_tokens", 0) or 0)
        d["cached_input_tokens"] = int(d.get("cached_input_tokens", 0) or 0)
        d["llm_token_calls"] = int(d.get("llm_token_calls", 0) or 0)
        d["goal_json"] = d.get("goal_json") or "{}"
        d["run_type"] = d.get("run_type") or "normal"
        # source_run_id 保持为 None 或字符串，不强制转为空字符串
        if d.get("source_run_id") is None:
            d["source_run_id"] = None
        verification_results = json.loads(d.pop("verification_json", "[]") or "[]")
        if isinstance(verification_results, list):
            for item in verification_results:
                if isinstance(item, dict) and item.get("screenshot"):
                    item["screenshot"] = str(item["screenshot"]).replace("\\", "/")
        d["verification_results"] = verification_results
        return d

    def delete_test_run(self, run_id: str) -> bool:
        """删除单次测试运行及其关联人工决策记录。"""
        row = self._conn.execute(
            "SELECT id FROM test_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return False
        self._conn.execute("DELETE FROM human_decisions WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM test_runs WHERE id = ?", (run_id,))
        self._conn.commit()
        return True

    # ── 元素身份 ──

    def save_element_identity(
        self,
        app_package: str,
        page_signature: str,
        alias: str,
        resource_id: str = "",
        class_name: str = "",
        role: str = "",
        region: str = "",
        text_hint: str = "",
        bounds_json: str = "",
        screen_width: int = 0,
        screen_height: int = 0,
        candidates_count: int = 1,
    ) -> None:
        """保存或更新元素身份映射。click_count 递增。"""
        existing = self.select(
            "element_identities",
            {
                "app_package": app_package,
                "page_signature": page_signature,
                "alias": alias,
            },
            limit=1,
        )
        now = datetime.now().isoformat()
        if existing:
            row = existing[0]
            self.insert(
                "element_identities",
                {
                    "app_package": app_package,
                    "page_signature": page_signature,
                    "alias": alias,
                    "resource_id": resource_id or row.get("resource_id", ""),
                    "class_name": class_name or row.get("class_name", ""),
                    "role": role or row.get("role", ""),
                    "region": region or row.get("region", ""),
                    "text_hint": text_hint or row.get("text_hint", ""),
                    "bounds_json": bounds_json or row.get("bounds_json", ""),
                    "screen_width": screen_width or row.get("screen_width", 0),
                    "screen_height": screen_height or row.get("screen_height", 0),
                    "candidates_count": candidates_count,
                    "click_count": int(row.get("click_count", 0)) + 1,
                    "last_used_at": now,
                    "updated_at": now,
                    "created_at": row.get("created_at", now),
                },
            )
        else:
            self.insert(
                "element_identities",
                {
                    "app_package": app_package,
                    "page_signature": page_signature,
                    "alias": alias,
                    "resource_id": resource_id,
                    "class_name": class_name,
                    "role": role,
                    "region": region,
                    "text_hint": text_hint,
                    "bounds_json": bounds_json,
                    "screen_width": screen_width,
                    "screen_height": screen_height,
                    "candidates_count": candidates_count,
                    "click_count": 1,
                    "last_used_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def query_element_identity(
        self,
        app_package: str,
        alias: str,
        page_signature: str = "",
        target_screen: tuple[int, int] = (0, 0),
    ) -> list[dict[str, Any]]:
        """查询元素身份映射。如果提供 target_screen, 自动将历史 bounds 换算为当前屏幕坐标。"""
        if page_signature:
            rows = self.select(
                "element_identities",
                {
                    "app_package": app_package,
                    "page_signature": page_signature,
                    "alias": alias,
                },
                limit=1,
            )
            if rows:
                return [
                    _convert_bounds(r, target_screen) for r in self._apply_expiry(rows)
                ]
        rows = self.select(
            "element_identities",
            {
                "app_package": app_package,
                "alias": alias,
            },
            order_by="click_count DESC",
            limit=3,
        )
        return [_convert_bounds(r, target_screen) for r in self._apply_expiry(rows)]

    def _apply_expiry(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """过期降权：>30天的记录 click_count 视为 1。"""
        for row in rows:
            updated = row.get("updated_at", "")
            if updated:
                try:
                    days_old = (datetime.now() - datetime.fromisoformat(updated)).days
                    if days_old > 30:
                        row["click_count"] = min(row.get("click_count", 1), 1)
                except Exception:
                    pass
        return rows

    def list_element_identities(
        self, app_package: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """列出元素身份映射。"""
        if app_package:
            return self.select(
                "element_identities",
                {"app_package": app_package},
                order_by="click_count DESC",
                limit=limit,
            )
        return self.select(
            "element_identities", order_by="click_count DESC", limit=limit
        )

    def find_successful_plan(
        self, app_package: str, user_request: str
    ) -> dict[str, Any] | None:
        """查找最近一次有成功步骤的测试执行, 返回其结果。
        用于 replay_mode: 全量回归时复用历史计划（从第 1 步开始）。
        匹配时对 user_request 规范化，提高命中率。
        """
        normalized = _normalize_request(user_request)
        # 先精确匹配原始文本
        sql = (
            "SELECT id, status, steps_json, created_at "
            "FROM test_runs WHERE app_package = ? AND user_request = ? "
            "ORDER BY created_at DESC LIMIT 10"
        )
        rows = self._conn.execute(sql, (app_package, user_request)).fetchall()
        # 精确匹配失败时，用规范化后的文本模糊匹配
        if not rows:
            all_runs = self._conn.execute(
                "SELECT id, status, steps_json, created_at, user_request "
                "FROM test_runs WHERE app_package = ? "
                "ORDER BY created_at DESC LIMIT 50",
                (app_package,),
            ).fetchall()
            rows = [
                r
                for r in all_runs
                if _normalize_request(dict(r).get("user_request", "")) == normalized
            ]
        if not rows:
            return None
        # 找最近的有最多成功步骤的记录
        best = None
        best_success = -1
        for row in rows:
            d = dict(row)
            steps = json.loads(d.get("steps_json", "[]") or "[]")
            success_count = sum(1 for s in steps if s.get("status") == "success")
            if success_count > best_success:
                best_success = success_count
                best = {
                    "run_id": d["id"],
                    "steps": steps,
                    "success_count": success_count,
                    "total_count": len(steps),
                }
        return best

    # ── v3: 用例管理 CRUD ──

    def create_test_case(
        self,
        name: str,
        user_request: str = "",
        app_package: str = "",
        app_name: str = "",
        goal_json: str = "{}",
        source_run_id: str | None = None,
    ) -> str:
        """创建用例。返回 case_id。"""
        import uuid

        case_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat()
        self.insert(
            "test_cases",
            {
                "id": case_id,
                "name": name,
                "source_run_id": source_run_id,
                "user_request": user_request,
                "app_package": app_package,
                "app_name": app_name,
                "goal_json": goal_json,
                "created_at": now,
                "last_run_status": "",
                "last_run_at": "",
            },
        )
        return case_id

    def list_test_cases(self, q: str = "") -> list[dict[str, Any]]:
        """查询用例列表，支持 ?q= 名称模糊过滤。"""
        if q:
            rows = self._conn.execute(
                "SELECT * FROM test_cases WHERE name LIKE ? ORDER BY created_at DESC",
                (f"%{q}%",),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM test_cases ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_test_case(self, case_id: str) -> dict[str, Any] | None:
        """查询单条用例。"""
        row = self._conn.execute(
            "SELECT * FROM test_cases WHERE id = ?", (case_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_test_case(self, case_id: str, data: dict[str, Any]) -> bool:
        """编辑用例计划。data 可含 name / goal_json / user_request / app_package / app_name。"""
        allowed = {"name", "goal_json", "user_request", "app_package", "app_name"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [case_id]
        self._conn.execute(
            f"UPDATE test_cases SET {sets} WHERE id = ?", values
        )
        self._conn.commit()
        return True

    def delete_test_case(self, case_id: str) -> bool:
        """删除单条用例。"""
        row = self._conn.execute(
            "SELECT id FROM test_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if not row:
            return False
        self._conn.execute("DELETE FROM test_cases WHERE id = ?", (case_id,))
        self._conn.commit()
        return True

    def batch_delete_test_cases(self, ids: list[str]) -> int:
        """批量删除用例。返回删除数量。"""
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        cursor = self._conn.execute(
            f"DELETE FROM test_cases WHERE id IN ({placeholders})", ids
        )
        self._conn.commit()
        return cursor.rowcount

    def record_case_run(self, case_id: str, status: str, at: str = "") -> None:
        """更新用例的最后运行状态和时间。"""
        now = at or datetime.now().isoformat()
        self._conn.execute(
            "UPDATE test_cases SET last_run_status = ?, last_run_at = ? WHERE id = ?",
            (status, now, case_id),
        )
        self._conn.commit()


# ── 模块级辅助函数 ──


def _normalize_request(text: str) -> str:
    """规范化 user_request，提高精确匹配命中率。
    处理：去标点、统一空格、去前后缀冗余词、小写。
    例: '打开WLAN设置， 开启开关' -> '打开wlan设置 开启开关'
    """
    # 去标点符号
    text = re.sub(r"[\uff0c\u3002\uff01\uff1f\u3001,.!?;\uff1b]", " ", text)
    # 统一空格 + strip
    text = re.sub(r"\s+", " ", text).strip()
    # 去掉常见前缀冗余词
    text = re.sub(r"^(请|帮我|帮忙)", "", text).strip()
    return text.lower()


def _convert_bounds(
    row: dict[str, Any], target_screen: tuple[int, int]
) -> dict[str, Any]:
    """将历史 bounds 按百分比换算到目标屏幕尺寸。

    注意：Android 布局不是简单等比缩放，状态栏/导航栏高度不同会导致线性换算偏差。
    因此百分比 bounds 只作为第 3 优先级 fallback，并在输出中标记置信度。
    """
    result = dict(row)
    bounds_json = result.get("bounds_json", "")
    src_w = result.get("screen_width", 0)
    src_h = result.get("screen_height", 0)
    tgt_w, tgt_h = target_screen

    if bounds_json and src_w > 0 and src_h > 0 and tgt_w > 0 and tgt_h > 0:
        try:
            b = json.loads(bounds_json)
            # 计算百分比
            pct = {
                "x1_pct": round(b["x1"] / src_w * 100, 2),
                "y1_pct": round(b["y1"] / src_h * 100, 2),
                "x2_pct": round(b["x2"] / src_w * 100, 2),
                "y2_pct": round(b["y2"] / src_h * 100, 2),
            }
            # 换算到目标屏幕
            converted = {
                "x1": int(pct["x1_pct"] / 100 * tgt_w),
                "y1": int(pct["y1_pct"] / 100 * tgt_h),
                "x2": int(pct["x2_pct"] / 100 * tgt_w),
                "y2": int(pct["y2_pct"] / 100 * tgt_h),
            }
            # 置信度评估：屏幕比例差异越大，置信度越低
            aspect_src = src_w / src_h
            aspect_tgt = tgt_w / tgt_h
            confidence = (
                "high"
                if abs(aspect_src - aspect_tgt) < 0.1
                else ("medium" if abs(aspect_src - aspect_tgt) < 0.3 else "low")
            )
            result["bounds_pct"] = pct
            result["bounds_converted"] = converted
            result["bounds_confidence"] = confidence
        except Exception:
            pass
    return result
