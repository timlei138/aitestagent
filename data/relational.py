from __future__ import annotations

import sqlite3
import json
import math
import os
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


def _page_facts_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Match only stable page facts that are actually persisted by action events."""
    return bool(expected.get("package") or expected.get("activity")) and all(
        not expected.get(field) or expected.get(field) == actual.get(field)
        for field in ("package", "activity")
    )


def evaluate_action_alignment(
    plan_action: dict[str, Any], event: dict[str, Any]
) -> dict[str, str]:
    """Return aligned, unknown, or deviated from durable plan/action facts."""
    if plan_action.get("tool_name") != event.get("tool_name"):
        return {"alignment": "deviated", "reason": "tool_name_mismatch"}
    expected_input = json.loads(plan_action.get("tool_input_json") or "{}")
    expected_before = json.loads(plan_action.get("precondition_json") or "{}")
    expected_locator = json.loads(plan_action.get("locator_json") or "{}")
    if expected_input and expected_input != (event.get("tool_input") or {}):
        return {"alignment": "deviated", "reason": "tool_input_mismatch"}
    if not expected_before:
        return {"alignment": "unknown", "reason": "missing_precondition_facts"}
    if not _page_facts_match(expected_before, event.get("page_before") or {}):
        return {"alignment": "deviated", "reason": "precondition_mismatch"}
    if expected_locator.get("rid") and expected_locator.get("rid") != (
        event.get("resolved_locator") or {}
    ).get("rid"):
        return {"alignment": "deviated", "reason": "locator_mismatch"}
    if expected_locator.get("path") and expected_locator.get("path") != (
        event.get("resolved_locator") or {}
    ).get("path"):
        return {"alignment": "deviated", "reason": "locator_mismatch"}
    return {"alignment": "aligned", "reason": ""}


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
            CREATE TABLE IF NOT EXISTS execution_plans (
                plan_id TEXT PRIMARY KEY,
                app_package TEXT NOT NULL,
                task_signature_json TEXT NOT NULL,
                entry_contract_json TEXT NOT NULL DEFAULT '{}',
                verification_contract_json TEXT NOT NULL DEFAULT '{}',
                contract_status TEXT NOT NULL DEFAULT 'contract_pending_review',
                plan_trust TEXT NOT NULL DEFAULT 'candidate',
                direct_approved INTEGER NOT NULL DEFAULT 0,
                environment_key TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                quality_score REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plan_actions (
                action_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                action_index INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                tool_input_json TEXT NOT NULL DEFAULT '{}',
                precondition_json TEXT NOT NULL DEFAULT '{}',
                locator_json TEXT NOT NULL DEFAULT '{}',
                postcondition_json TEXT NOT NULL DEFAULT '{}',
                verification_links_json TEXT NOT NULL DEFAULT '[]',
                execution_eligibility TEXT NOT NULL DEFAULT 'guided_only',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                postcondition_pass_count INTEGER NOT NULL DEFAULT 0,
                postcondition_failure_count INTEGER NOT NULL DEFAULT 0,
                timeout_count INTEGER NOT NULL DEFAULT 0,
                not_found_count INTEGER NOT NULL DEFAULT 0,
                quality_score REAL NOT NULL DEFAULT 0,
                last_success_at TEXT,
                last_failed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(plan_id, action_index),
                FOREIGN KEY (plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS locator_knowledge (
                locator_id TEXT PRIMARY KEY,
                app_package TEXT NOT NULL,
                page_signature TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                identity_json TEXT NOT NULL DEFAULT '{}',
                screen_profile TEXT NOT NULL DEFAULT '',
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_runs (
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
                resolution_metrics TEXT NOT NULL DEFAULT '{}',
                duration_seconds REAL NOT NULL DEFAULT 0.0,
                llm_call_count INTEGER NOT NULL DEFAULT 0,
                token_usage_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS action_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                action_index INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                tool_input_json TEXT NOT NULL DEFAULT '{}',
                resolved_locator_json TEXT NOT NULL DEFAULT '{}',
                page_before_json TEXT NOT NULL DEFAULT '{}',
                page_after_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                intent_text TEXT NOT NULL DEFAULT '',
                screenshot_path TEXT NOT NULL DEFAULT '',
                execution_mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES execution_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS evidence_events (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                verification_key TEXT NOT NULL,
                clause_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                tool_call_id TEXT NOT NULL DEFAULT '',
                artifact_ref TEXT NOT NULL DEFAULT '',
                fact_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES execution_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS mode_transition_events (
                transition_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                from_mode TEXT NOT NULL,
                to_mode TEXT NOT NULL,
                reason TEXT NOT NULL,
                step_index INTEGER,
                plan_id TEXT NOT NULL DEFAULT '',
                action_id TEXT NOT NULL DEFAULT '',
                phase_budget REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES execution_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS plan_action_alignment_events (
                alignment_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                action_index INTEGER NOT NULL,
                alignment TEXT NOT NULL,
                deviation_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES execution_runs(run_id),
                FOREIGN KEY (plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS curated_rule_migration_audit (
                source_rule_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                app_package TEXT NOT NULL DEFAULT '',
                reviewed_by TEXT NOT NULL,
                classification TEXT NOT NULL,
                target_rule_id TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_execution_plans_match
                ON execution_plans(app_package, plan_trust, contract_status);
            CREATE INDEX IF NOT EXISTS idx_plan_actions_plan
                ON plan_actions(plan_id, action_index);
            CREATE INDEX IF NOT EXISTS idx_locator_knowledge_page
                ON locator_knowledge(app_package, page_signature);
            CREATE INDEX IF NOT EXISTS idx_execution_runs_plan
                ON execution_runs(plan_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_evidence_events_run
                ON evidence_events(run_id, verification_key, clause_id);
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

    def record_execution_run(
        self,
        run_id: str,
        user_request: str,
        app_package: str,
        goal: dict[str, Any],
        verification_contract: dict[str, Any],
        execution_mode: str = "explore",
        lifecycle_state: str = "Terminal",
        plan_id: str | None = None,
        plan_trust: str = "",
        verdict: str = "inconclusive",
        terminal_reason: str = "",
        resolution_metrics: dict[str, Any] | None = None,
        duration_seconds: float = 0.0,
        llm_call_count: int = 0,
        token_usage: dict[str, Any] | None = None,
    ) -> None:
        """Persist the current run in the v2 execution schema."""
        now = datetime.now().isoformat()
        self.upsert(
            "execution_runs",
            {
                "run_id": run_id,
                "user_request": user_request,
                "app_package": app_package,
                "goal_json": json.dumps(goal or {}, ensure_ascii=False),
                "verification_contract_json": json.dumps(
                    verification_contract or {}, ensure_ascii=False
                ),
                "execution_mode": execution_mode,
                "lifecycle_state": lifecycle_state,
                "plan_id": plan_id,
                "plan_trust": plan_trust,
                "verdict": verdict,
                "terminal_reason": terminal_reason,
                "resolution_metrics": json.dumps(
                    resolution_metrics or {}, ensure_ascii=False
                ),
                "duration_seconds": float(duration_seconds or 0.0),
                "llm_call_count": int(llm_call_count or 0),
                "token_usage_json": json.dumps(
                    token_usage or {}, ensure_ascii=False
                ),
                "created_at": now,
                "updated_at": now,
            },
            key="run_id",
        )

    def record_evidence_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        """Persist current-run evidence with generated IDs for tool events lacking one."""
        for event in events:
            if not isinstance(event, dict):
                continue
            evidence_id = str(event.get("evidence_id", "") or uuid.uuid4())
            created_at = str(event.get("timestamp", "") or datetime.now().isoformat())
            self.upsert(
                "evidence_events",
                {
                    "evidence_id": evidence_id,
                    "run_id": run_id,
                    "verification_key": str(event.get("verification_key", "") or ""),
                    "clause_id": str(event.get("clause_id", "") or ""),
                    "channel": str(event.get("channel", "") or ""),
                    "status": str(event.get("status", "") or ""),
                    "tool_call_id": str(event.get("tool_call_id", "") or ""),
                    "artifact_ref": str(event.get("artifact_ref", "") or ""),
                    "fact_json": json.dumps(
                        event.get("fact", {}) or {}, ensure_ascii=False
                    ),
                    "created_at": created_at,
                },
                key="evidence_id",
            )

    def record_action_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        """Persist tool execution facts for deterministic plan extraction."""
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            self.upsert(
                "action_events",
                {
                    "event_id": str(event.get("event_id", "") or uuid.uuid4()),
                    "run_id": run_id,
                    "action_index": int(event.get("action_index", index) or 0),
                    "tool_name": str(event.get("tool_name", "") or ""),
                    "tool_input_json": json.dumps(
                        event.get("tool_input", {}) or {}, ensure_ascii=False
                    ),
                    "resolved_locator_json": json.dumps(
                        event.get("resolved_locator", {}) or {}, ensure_ascii=False
                    ),
                    "page_before_json": json.dumps(
                        event.get("page_before", {}) or {}, ensure_ascii=False
                    ),
                    "page_after_json": json.dumps(
                        event.get("page_after", {}) or {}, ensure_ascii=False
                    ),
                    "status": str(event.get("status", "") or ""),
                    "intent_text": str(event.get("intent_text", "") or ""),
                    "screenshot_path": str(event.get("screenshot_path", "") or ""),
                    "execution_mode": str(
                        event.get("execution_mode", "explore") or "explore"
                    ),
                    "created_at": str(
                        event.get("timestamp", "") or datetime.now().isoformat()
                    ),
                },
                key="event_id",
            )

    def record_mode_transition_events(
        self, run_id: str, events: list[dict[str, Any]]
    ) -> None:
        """Persist the one-way execution-mode transitions for a run.

        Each event may carry plan_id/action_id/phase_budget so transitions can
        be correlated to the plan/action that triggered them and the phase
        budget consumed at the point of transition.
        """
        for event in events:
            if not isinstance(event, dict):
                continue
            self.upsert(
                "mode_transition_events",
                {
                    "transition_id": str(
                        event.get("transition_id", "") or uuid.uuid4()
                    ),
                    "run_id": run_id,
                    "from_mode": str(event.get("from", "") or ""),
                    "to_mode": str(event.get("to", "") or ""),
                    "reason": str(event.get("reason", "") or ""),
                    "step_index": event.get("step_index"),
                    "plan_id": str(event.get("plan_id", "") or ""),
                    "action_id": str(event.get("action_id", "") or ""),
                    "phase_budget": float(event.get("phase_budget", 0.0) or 0.0),
                    "created_at": str(
                        event.get("timestamp", "") or datetime.now().isoformat()
                    ),
                },
                key="transition_id",
            )

    def record_plan_action_outcomes(
        self,
        plan_id: str,
        events: list[dict[str, Any]],
        *,
        decay_lambda: float = 0.01,
    ) -> None:
        """Update aligned plan-action quality from this run's tool facts."""
        if not plan_id:
            return
        actions = self.select("plan_actions", {"plan_id": plan_id})
        by_index = {int(action["action_index"]): action for action in actions}
        now = datetime.now()
        now_str = now.isoformat()
        for event in events:
            if not isinstance(event, dict):
                continue
            event_index = event.get("action_index", -1)
            action = by_index.get(int(event_index) if event_index is not None else -1)
            if not action or action.get("tool_name") != event.get("tool_name"):
                continue
            expected_input = json.loads(action.get("tool_input_json") or "{}")
            expected_before = json.loads(action.get("precondition_json") or "{}")
            expected_locator = json.loads(action.get("locator_json") or "{}")
            expected_after = json.loads(action.get("postcondition_json") or "{}")
            event_before = event.get("page_before", {}) or {}
            event_locator = event.get("resolved_locator", {}) or {}
            event_after = event.get("page_after", {}) or {}
            if expected_input and expected_input != (event.get("tool_input", {}) or {}):
                continue
            if expected_before and not _page_facts_match(expected_before, event_before):
                continue
            if expected_locator.get("rid") and expected_locator.get(
                "rid"
            ) != event_locator.get("rid"):
                continue
            if expected_locator.get("path") and expected_locator.get(
                "path"
            ) != event_locator.get("path"):
                continue
            status = str(event.get("status", "") or "").upper()
            is_success = status in {"OK", "PASS", "YES"}
            postcondition_matched = bool(expected_after) and _page_facts_match(
                expected_after, event_after
            )
            postcondition_failed = (
                bool(expected_after)
                and not postcondition_matched
                and not is_success
            )

            attempt_count = int(action.get("attempt_count", 0) or 0) + 1
            success_count = int(action.get("success_count", 0) or 0) + (
                1 if is_success else 0
            )
            not_found_count = int(action.get("not_found_count", 0) or 0) + (
                1 if status == "NOT_FOUND" else 0
            )
            timeout_count = int(action.get("timeout_count", 0) or 0) + (
                1 if status == "TIMEOUT" else 0
            )
            postcondition_pass_count = int(
                action.get("postcondition_pass_count", 0) or 0
            ) + (
                1
                if is_success and postcondition_matched
                else 0
            )
            postcondition_failure_count = int(
                action.get("postcondition_failure_count", 0) or 0
            ) + (
                1 if postcondition_failed else 0
            )

            last_success_at = action.get("last_success_at")
            if is_success and postcondition_matched:
                last_success_at = now_str

            reliability = (success_count + 1) / (attempt_count + 2)
            postcondition_rate = (postcondition_pass_count + 1) / (attempt_count + 2)
            timeout_rate = (timeout_count + 1) / (attempt_count + 2)
            not_found_rate = (not_found_count + 1) / (attempt_count + 2)

            age_days = 30
            if last_success_at:
                try:
                    age_days = (now - datetime.fromisoformat(last_success_at)).days
                except Exception:
                    age_days = 0
            age_decay = math.exp(-decay_lambda * age_days)

            penalty = 0.25 * timeout_rate + 0.35 * not_found_rate
            quality = max(
                0.0,
                min(
                    1.0,
                    reliability * postcondition_rate * age_decay - penalty,
                ),
            )

            self.upsert(
                "plan_actions",
                {
                    **action,
                    "attempt_count": attempt_count,
                    "success_count": success_count,
                    "not_found_count": not_found_count,
                    "timeout_count": timeout_count,
                    "postcondition_pass_count": postcondition_pass_count,
                    "postcondition_failure_count": postcondition_failure_count,
                    "quality_score": quality,
                    "last_success_at": last_success_at,
                    "last_failed_at": (
                        now_str
                        if not is_success
                        else action.get("last_failed_at")
                    ),
                    "updated_at": now_str,
                },
                key="action_id",
            )

    def record_plan_action_alignment_events(
        self, run_id: str, plan_id: str, events: list[dict[str, Any]]
    ) -> None:
        """Record whether each attempted plan action aligned with current facts."""
        if not run_id or not plan_id:
            return
        actions = {
            int(action["action_index"]): action
            for action in self.select("plan_actions", {"plan_id": plan_id})
        }
        for event in events:
            if not isinstance(event, dict):
                continue
            index = event.get("action_index")
            action = actions.get(int(index) if index is not None else -1)
            result = (
                evaluate_action_alignment(action, event)
                if action
                else {"alignment": "deviated", "reason": "missing_plan_action"}
            )
            self.upsert(
                "plan_action_alignment_events",
                {
                    "alignment_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "plan_id": plan_id,
                    "action_index": int(index) if index is not None else -1,
                    "alignment": result["alignment"],
                    "deviation_reason": result["reason"],
                    "created_at": datetime.now().isoformat(),
                },
                key="alignment_id",
            )

    def record_execution_plan_outcome(
        self,
        plan_id: str,
        verdict: str,
        run_id: str = "",
        *,
        direct_quality_threshold: float = 0.5,
    ) -> None:
        """Update plan trust from distinct persisted compatible runs and action quality."""
        if not plan_id or not run_id:
            return
        plans = self.select("execution_plans", {"plan_id": plan_id}, limit=1)
        if not plans:
            return
        plan = plans[0]
        actions = self.select("plan_actions", {"plan_id": plan_id})
        action_quality = (
            sum(float(action.get("quality_score", 0.0) or 0.0) for action in actions)
            / len(actions)
            if actions
            else 0.0
        )
        run_rows = self._conn.execute(
            "SELECT verdict FROM execution_runs WHERE plan_id = ?", (plan_id,)
        ).fetchall()
        attempt_count = len(run_rows)
        success_count = sum(
            1 for row in run_rows if str(row["verdict"] or "") == "passed"
        )
        trust = str(plan.get("plan_trust", "candidate") or "candidate")
        if trust == "candidate" and success_count >= 2 and action_quality >= 0.6:
            trust = "trusted"
        direct_approved = int(plan.get("direct_approved", 0) or 0)
        if direct_approved and action_quality < direct_quality_threshold:
            direct_approved = 0
        self.upsert(
            "execution_plans",
            {
                **plan,
                "attempt_count": attempt_count,
                "success_count": success_count,
                "quality_score": action_quality,
                "plan_trust": trust,
                "direct_approved": direct_approved,
                "updated_at": datetime.now().isoformat(),
            },
            key="plan_id",
        )

    def set_direct_approval(self, plan_id: str, approved: bool) -> bool:
        """Apply an explicit human direct-execution approval decision."""
        plans = self.select("execution_plans", {"plan_id": plan_id}, limit=1)
        if not plans:
            return False
        plan = plans[0]
        self.upsert(
            "execution_plans",
            {
                **plan,
                "direct_approved": int(bool(approved)),
                "updated_at": datetime.now().isoformat(),
            },
            key="plan_id",
        )
        return True

    def list_execution_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT run_id, user_request, app_package, execution_mode, lifecycle_state,
                   plan_id, plan_trust, verdict, terminal_reason, resolution_metrics,
                   duration_seconds, llm_call_count, token_usage_json,
                   created_at, updated_at
            FROM execution_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                **dict(row),
                "resolution_metrics": json.loads(row["resolution_metrics"] or "{}"),
                "token_usage": json.loads(row["token_usage_json"] or "{}"),
            }
            for row in rows
        ]

    def summarize_by_mode(self) -> dict[str, dict[str, Any]]:
        """Phase 4 端到端质量门禁：按执行模式聚合并行质量指标。

        对每个 execution_mode 统计：通过率、平均耗时、总/平均 LLM 调用、模式转换次数、
        后置条件命中数、证据完整率、Token 消耗（input/output/total/cached），
        以及 locator 解析指标合计。
        后置条件命中 = action_events 中 status 为 success/continue（按 run_id 过滤）；
        证据完整率 = 有 evidence_events 的 run / 该模式总 run 数。
        """
        from collections import defaultdict

        runs = self.list_execution_runs(limit=10000)
        by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in runs:
            by_mode[r.get("execution_mode", "explore")].append(r)

        result: dict[str, dict[str, Any]] = {}
        for mode, mode_runs in by_mode.items():
            total = len(mode_runs)
            passed = sum(1 for r in mode_runs if r.get("verdict") == "passed")
            durations = [float(r.get("duration_seconds", 0) or 0) for r in mode_runs]
            llm_calls = [int(r.get("llm_call_count", 0) or 0) for r in mode_runs]
            exact_total = sum(
                int(r.get("resolution_metrics", {}).get("exact_resolution_count", 0))
                for r in mode_runs
            )
            semantic_total = sum(
                int(r.get("resolution_metrics", {}).get("semantic_resolution_count", 0))
                for r in mode_runs
            )
            mismatch_total = sum(
                int(r.get("resolution_metrics", {}).get("label_mismatch_count", 0))
                for r in mode_runs
            )
            token_usage_list = [r.get("token_usage", {}) for r in mode_runs]
            token_totals = {
                key: sum(int(tu.get(key, 0) or 0) for tu in token_usage_list)
                for key in ["input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"]
            }
            token_avgs = {
                key: round(value / total, 2) if total else 0.0
                for key, value in token_totals.items()
            }
            run_ids = [r["run_id"] for r in mode_runs]
            transitions = self._count_in(
                "mode_transition_events", "run_id", run_ids
            )
            postcond_hit = self._count_in(
                "action_events", "run_id", run_ids,
                extra_where="status IN ('success','continue')",
            )
            runs_with_evidence = self._count_distinct_runs_with(
                "evidence_events", "run_id", run_ids
            )
            result[mode] = {
                "run_count": total,
                "pass_count": passed,
                "pass_rate": round(passed / total, 4) if total else 0.0,
                "avg_duration_seconds": round(sum(durations) / total, 2) if total else 0.0,
                "total_llm_calls": sum(llm_calls),
                "avg_llm_calls": round(sum(llm_calls) / total, 2) if total else 0.0,
                "mode_transition_count": transitions,
                "postcond_hit_count": postcond_hit,
                "evidence_complete_rate": (
                    round(runs_with_evidence / total, 4) if total else 0.0
                ),
                "resolution_metrics": {
                    "exact_resolution_count": exact_total,
                    "semantic_resolution_count": semantic_total,
                    "label_mismatch_count": mismatch_total,
                },
                "token_usage": {
                    "total_input_tokens": token_totals["input_tokens"],
                    "total_output_tokens": token_totals["output_tokens"],
                    "total_tokens": token_totals["total_tokens"],
                    "total_cached_input_tokens": token_totals["cached_input_tokens"],
                    "avg_input_tokens": token_avgs["input_tokens"],
                    "avg_output_tokens": token_avgs["output_tokens"],
                    "avg_total_tokens": token_avgs["total_tokens"],
                    "avg_cached_input_tokens": token_avgs["cached_input_tokens"],
                },
            }
        return result

    def _count_in(
        self,
        table: str,
        id_col: str,
        ids: list[str],
        extra_where: str = "",
    ) -> int:
        """统计某表中 id_col 落在 ids 内的行数（可附加 WHERE 条件）。"""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        where = f"WHERE {id_col} IN ({placeholders})"
        if extra_where:
            where += f" AND {extra_where}"
        row = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} {where}", ids
        ).fetchone()
        return int(row["c"]) if row else 0

    def _count_distinct_runs_with(
        self, table: str, id_col: str, ids: list[str]
    ) -> int:
        """统计某表中 id_col 落在 ids 内、且去重 run_id 的数量。"""
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        row = self._conn.execute(
            f"SELECT COUNT(DISTINCT {id_col}) AS c FROM {table} "
            f"WHERE {id_col} IN ({placeholders})",
            ids,
        ).fetchone()
        return int(row["c"]) if row else 0

    def get_execution_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM execution_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        report = dict(row)
        report["goal"] = json.loads(report.pop("goal_json") or "{}")
        report["verification_contract"] = json.loads(
            report.pop("verification_contract_json") or "{}"
        )
        report["resolution_metrics"] = json.loads(
            report.pop("resolution_metrics", "{}") or "{}"
        )
        report["token_usage"] = json.loads(
            report.pop("token_usage_json", "{}") or "{}"
        )
        action_rows = self._conn.execute(
            """
            SELECT action_index, tool_name, tool_input_json, resolved_locator_json,
                   page_before_json, page_after_json, status, intent_text,
                   screenshot_path, execution_mode, created_at
            FROM action_events WHERE run_id = ? ORDER BY action_index ASC, created_at ASC
            """,
            (run_id,),
        ).fetchall()
        report["actions"] = [
            {
                **dict(action),
                "tool_input": json.loads(action["tool_input_json"] or "{}"),
                "resolved_locator": json.loads(action["resolved_locator_json"] or "{}"),
                "page_before": json.loads(action["page_before_json"] or "{}"),
                "page_after": json.loads(action["page_after_json"] or "{}"),
                "intent": action["intent_text"],
                "screenshot": action["screenshot_path"],
            }
            for action in action_rows
        ]
        evidence_rows = self._conn.execute(
            """
            SELECT evidence_id, verification_key, clause_id, channel, status,
                   tool_call_id, artifact_ref, fact_json, created_at
            FROM evidence_events WHERE run_id = ? ORDER BY created_at ASC
            """,
            (run_id,),
        ).fetchall()
        report["evidence"] = [
            {**dict(evidence), "fact": json.loads(evidence["fact_json"] or "{}")}
            for evidence in evidence_rows
        ]
        transition_rows = self._conn.execute(
            """
            SELECT from_mode, to_mode, reason, step_index, created_at
            FROM mode_transition_events WHERE run_id = ? ORDER BY created_at ASC
            """,
            (run_id,),
        ).fetchall()
        report["mode_transitions"] = [dict(event) for event in transition_rows]
        return report

    def delete_execution_run(self, run_id: str) -> bool:
        self._conn.execute("DELETE FROM action_events WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM evidence_events WHERE run_id = ?", (run_id,))
        self._conn.execute(
            "DELETE FROM mode_transition_events WHERE run_id = ?", (run_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM execution_runs WHERE run_id = ?", (run_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_full_execution_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Read a complete plan with its actions from the relational store."""
        row = self._conn.execute(
            "SELECT * FROM execution_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if not row:
            return None
        plan = dict(row)
        signature = json.loads(plan.get("task_signature_json") or "{}")
        plan["task_signature"] = signature
        actions = self._conn.execute(
            "SELECT * FROM plan_actions WHERE plan_id = ? ORDER BY action_index ASC",
            (plan_id,),
        ).fetchall()
        plan["actions"] = [dict(action) for action in actions]
        return plan

    def find_matching_execution_plan(
        self,
        app_package: str,
        user_request: str = "",
        verification_fingerprint: str = "",
        environment_key: str = "",
        task_signature: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return the best approved plan whose task signature matches this run.

        If ``task_signature`` is provided, prefer semantic compatibility matching
        (action semantics, verification semantics, parameter slots).  Otherwise fall
        back to the legacy exact-match behavior on ``user_request``.

        Environment compatibility is scored rather than compared with exact equality:
        critical fields (package/activity/screen_profile) must match, while optional
        fields (app_version/fixture_fingerprint) only incur penalties when both sides
        are present and differ.
        """
        from agents.plan_extractor import (
            environment_compatibility_score,
            task_signatures_compatible,
        )

        rows = self._conn.execute(
            """
            SELECT * FROM execution_plans
            WHERE app_package = ? AND contract_status = 'approved'
            ORDER BY direct_approved DESC, quality_score DESC, updated_at DESC
            """,
            (app_package,),
        ).fetchall()

        def _load_plan(row):
            plan = dict(row)
            signature = json.loads(plan.get("task_signature_json") or "{}")
            actions = self._conn.execute(
                "SELECT * FROM plan_actions WHERE plan_id = ? ORDER BY action_index ASC",
                (plan["plan_id"],),
            ).fetchall()
            plan["task_signature"] = signature
            plan["actions"] = [dict(action) for action in actions]
            return plan

        candidates = []

        # Semantic matching path.
        if task_signature:
            for row in rows:
                plan = _load_plan(row)
                signature = plan["task_signature"]
                if (
                    verification_fingerprint
                    and signature.get("verification_fingerprint")
                    != verification_fingerprint
                ):
                    continue
                if not task_signatures_compatible(task_signature, signature):
                    continue
                env_score = environment_compatibility_score(
                    plan.get("environment_key", ""), environment_key
                )
                if not env_score["compatible"]:
                    continue
                plan["environment_compatibility_score"] = env_score["score"]
                plan["environment_compatibility_reasons"] = env_score["reasons"]
                candidates.append(plan)

        # Legacy exact-match fallback.
        if not candidates:
            for row in rows:
                plan = _load_plan(row)
                signature = plan["task_signature"]
                if signature.get("user_request") != user_request:
                    continue
                if (
                    verification_fingerprint
                    and signature.get("verification_fingerprint")
                    != verification_fingerprint
                ):
                    continue
                env_score = environment_compatibility_score(
                    plan.get("environment_key", ""), environment_key
                )
                if not env_score["compatible"]:
                    continue
                plan["environment_compatibility_score"] = env_score["score"]
                plan["environment_compatibility_reasons"] = env_score["reasons"]
                candidates.append(plan)

        if not candidates:
            return None

        # Prefer highest environment compatibility, then plan quality.
        candidates.sort(
            key=lambda p: (
                float(p.get("environment_compatibility_score", 0.0) or 0.0),
                float(p.get("quality_score", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return candidates[0]

    def save_locator_knowledge(
        self,
        *,
        app_package: str,
        page_signature: str,
        alias: str,
        locator: dict[str, Any],
        identity: dict[str, Any],
        screen_profile: str = "",
    ) -> None:
        """Record a successful current-page locator without retaining old identities."""
        now = datetime.now().isoformat()
        rows = self.select(
            "locator_knowledge",
            {"app_package": app_package, "page_signature": page_signature},
            limit=100,
        )
        existing = next(
            (
                row
                for row in rows
                if json.loads(row.get("identity_json") or "{}").get("alias") == alias
            ),
            None,
        )
        payload = {
            "alias": alias,
            **(identity or {}),
        }
        data = {
            "locator_id": (
                str(existing.get("locator_id")) if existing else str(uuid.uuid4())
            ),
            "app_package": app_package,
            "page_signature": page_signature,
            "locator_json": json.dumps(locator or {}, ensure_ascii=False),
            "identity_json": json.dumps(payload, ensure_ascii=False),
            "screen_profile": screen_profile,
            "success_count": (
                int(existing.get("success_count", 0) or 0) + 1 if existing else 1
            ),
            "failure_count": (
                int(existing.get("failure_count", 0) or 0) if existing else 0
            ),
            "last_verified_at": now,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        self.upsert("locator_knowledge", data, key="locator_id")

    def query_locator_knowledge(
        self,
        app_package: str,
        *,
        alias: str = "",
        resource_id: str = "",
        page_signature: str = "",
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Return locator facts, preferring the current page over app-wide history."""
        page_rows = (
            self.select(
                "locator_knowledge",
                {"app_package": app_package, "page_signature": page_signature},
                order_by="success_count DESC, updated_at DESC",
                limit=100,
            )
            if page_signature
            else []
        )

        def matching(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            matches: list[dict[str, Any]] = []
            for row in rows:
                identity = json.loads(row.get("identity_json") or "{}")
                locator = json.loads(row.get("locator_json") or "{}")
                if alias and identity.get("alias") != alias:
                    continue
                if resource_id and identity.get("resource_id") != resource_id:
                    continue
                matches.append(
                    {
                        **identity,
                        **locator,
                        "success_count": int(row.get("success_count", 0) or 0),
                        "failure_count": int(row.get("failure_count", 0) or 0),
                        "page_signature": row.get("page_signature", ""),
                    }
                )
                if len(matches) >= limit:
                    break
            return matches

        page_matches = matching(page_rows)
        if page_matches:
            return page_matches
        rows = self.select(
            "locator_knowledge",
            {"app_package": app_package},
            order_by="success_count DESC, updated_at DESC",
            limit=100,
        )
        return matching(rows)
