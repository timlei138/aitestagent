from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data.vector_store import VectorStoreBackend


@dataclass
class UIKnowledge:
    app_package: str
    knowledge_type: str
    content: str
    metadata: dict[str, Any]


class KnowledgeBase:
    """Reviewed semantic knowledge for constraints, negative knowledge, and hints,
    plus task-plan summary indexing for plan retrieval.
    """

    _SEMANTIC_TYPES = ("constraint", "negative_knowledge", "semantic_hint")
    _PLAN_TYPE = "task_plan_summary"

    def __init__(self, backend: VectorStoreBackend):
        self.backend = backend

    @staticmethod
    def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            if isinstance(value, list):
                normalized = [item for item in value if item not in (None, "")]
                if normalized:
                    cleaned[key] = normalized
                continue
            cleaned[key] = value
        return cleaned

    @classmethod
    def _validate_type(cls, knowledge_type: str) -> None:
        if knowledge_type not in cls._SEMANTIC_TYPES:
            raise ValueError(
                "knowledge_type 必须是 constraint、negative_knowledge 或 semantic_hint"
            )

    @classmethod
    def _validate_type_or_plan(cls, knowledge_type: str) -> None:
        """查询/列出时允许 task_plan_summary（由 save_task_plan_summary 写入）。"""
        if knowledge_type not in cls._SEMANTIC_TYPES and knowledge_type != cls._PLAN_TYPE:
            raise ValueError(
                "knowledge_type 必须是 constraint、negative_knowledge、semantic_hint 或 task_plan_summary"
            )

    def save_knowledge(self, knowledge: UIKnowledge) -> None:
        self._validate_type(knowledge.knowledge_type)
        metadata = dict(knowledge.metadata or {})
        metadata.setdefault(
            "scope", "universal" if not knowledge.app_package else "app"
        )
        self.backend.add(
            knowledge.content,
            {
                "app_package": knowledge.app_package,
                "knowledge_type": knowledge.knowledge_type,
                **self._sanitize_metadata(metadata),
            },
        )

    def save_task_plan_summary(
        self,
        app_package: str,
        plan_id: str,
        summary: str,
        verification_fingerprint: str = "",
        user_request_template: str = "",
        parameter_slots: list[dict[str, Any]] | None = None,
        environment_key: str = "",
    ) -> None:
        """Index a task-plan summary for semantic retrieval.

        The full plan remains authoritative in the relational database; this vector
        entry is only a searchable summary carrying enough metadata for filtering.
        """
        metadata = {
            "app_package": app_package,
            "knowledge_type": self._PLAN_TYPE,
            "plan_id": plan_id,
            "verification_fingerprint": verification_fingerprint,
            "user_request_template": user_request_template,
            "parameter_slot_types": sorted(
                {str(s.get("type", "") or "") for s in parameter_slots or [] if s}
            ),
            "environment_key_hash": self._hash_text(environment_key),
        }
        self.backend.add(summary, self._sanitize_metadata(metadata))

    def query_task_plan_summaries(
        self,
        app_package: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return task-plan summary candidates by vector similarity."""
        where: dict[str, Any] = {"knowledge_type": self._PLAN_TYPE}
        if app_package:
            where["app_package"] = app_package
        return self.backend.search(query, where or None, top_k)

    @staticmethod
    def _hash_text(text: str) -> str:
        import hashlib

        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]

    def query(
        self,
        query: str,
        app_package: str = "",
        knowledge_type: str = "",
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        if knowledge_type:
            self._validate_type_or_plan(knowledge_type)
        where: dict[str, Any] = {}
        if app_package:
            where["app_package"] = app_package
        if knowledge_type:
            where["knowledge_type"] = knowledge_type
        return self.backend.search(query, where or None, top_k)

    def list_entries(
        self, app_package: str = "", knowledge_type: str = "", top_k: int = 50
    ) -> list[dict[str, Any]]:
        if knowledge_type:
            self._validate_type_or_plan(knowledge_type)
        where: dict[str, Any] = {}
        if app_package:
            where["app_package"] = app_package
        if knowledge_type:
            where["knowledge_type"] = knowledge_type
        items = self.backend.get_by_metadata(where, limit=top_k)
        items.sort(
            key=lambda item: str(item.get("metadata", {}).get("timestamp", "")),
            reverse=True,
        )
        return items

    def query_semantic_knowledge(
        self, app_package: str, top_k: int = 5
    ) -> dict[str, list[str]]:
        grouped = {knowledge_type: [] for knowledge_type in self._SEMANTIC_TYPES}
        for knowledge_type in self._SEMANTIC_TYPES:
            rows = self.backend.get_by_metadata(
                {"app_package": app_package, "knowledge_type": knowledge_type},
                limit=top_k,
            )
            if app_package:
                rows += self.backend.get_by_metadata(
                    {
                        "app_package": "",
                        "scope": "universal",
                        "knowledge_type": knowledge_type,
                    },
                    limit=top_k,
                )
            seen: set[str] = set()
            for row in rows:
                content = str(row.get("content", "") or "").strip()
                if content and content not in seen:
                    seen.add(content)
                    grouped[knowledge_type].append(content)
                    if len(grouped[knowledge_type]) >= top_k:
                        break
        return grouped

    @property
    def count(self) -> int:
        return self.backend.count()
