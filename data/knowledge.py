from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data.vector_store import VectorStoreBackend


@dataclass
class UIKnowledge:
    app_package: str
    knowledge_type: str
    content: str
    metadata: dict[str, Any]


class KnowledgeBase:
    """RAG 知识库 —— 业务逻辑（操作经验、人工知识）和底层存储解耦。"""

    # 类型别名映射（旧数据兼容：查询新类型时自动匹配旧类型）
    _TYPE_ALIASES: dict[str, list[str]] = {
        # 旧类型名仍可能被查询到（清理前的过渡期），保留别名兼容
        "experience": ["experience", "navigation_path", "page_structure", "test_experience"],
        "curated_rule": ["curated_rule", "app_precondition", "global_knowledge"],
    }

    def __init__(self, backend: VectorStoreBackend):
        self.backend = backend

    # ── 通用存取 ──

    def save_knowledge(self, knowledge: UIKnowledge) -> None:
        self.backend.add(knowledge.content, {
            "app_package": knowledge.app_package,
            "knowledge_type": knowledge.knowledge_type,
            **knowledge.metadata,
        })

    def query(self, query: str, app_package: str = "", knowledge_type: str = "",
              top_k: int = 5) -> list[dict[str, Any]]:
        filter_dict: dict[str, Any] = {}  # Any 而非 str，因为 $or 值是 list[dict]
        if app_package:
            filter_dict["app_package"] = app_package
        if knowledge_type:
            aliases = self._TYPE_ALIASES.get(knowledge_type, [knowledge_type])
            if len(aliases) == 1:
                filter_dict["knowledge_type"] = aliases[0]
            else:
                filter_dict["$or"] = [{"knowledge_type": a} for a in aliases]
        return self.backend.search(query, filter_dict if filter_dict else None, top_k)

    # ── 操作经验 (experience) ──

    def save_experience(self, app_package: str, page: str, action: str = "",
                        to_page: str = "", outcome: str = "",
                        detail: str = "", labels: list[str] | None = None) -> None:
        """保存操作经验 —— 精简格式: A → action → B"""

        # 精简内容格式
        content = page
        if action:
            content += f" → {action}"
        if to_page:
            content += f" → {to_page}"

        # 去重：用 get_by_metadata 按 metadata 精确过滤（不走向量搜索）
        existing = self.backend.get_by_metadata(
            where={"app_package": app_package, "knowledge_type": "experience",
                   "page": page, "action": action, "to_page": to_page},
            limit=1,
        )
        if existing:
            return

        self.save_knowledge(UIKnowledge(
            app_package=app_package, knowledge_type="experience",
            content=content,
            metadata={"page": page, "action": action, "to_page": to_page,
                      "outcome": outcome, "timestamp": datetime.now().isoformat()},
        ))

    def query_experience(self, app_package: str, user_request: str = "",
                          top_k: int = 5) -> list[dict[str, Any]]:
        """分层查询操作经验: Layer1 精确过滤当前 App + Layer2 语义搜索兆底。"""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        # Layer 1: 精确过滤当前 App（有包名时，同 App 内导航最常见）
        if app_package:
            precise = self.backend.get_by_metadata(
                where={"app_package": app_package, "knowledge_type": "experience"},
                limit=top_k * 3,
            )
            # ChromaDB get() 不保证顺序，Python 侧按 timestamp 降序
            precise.sort(
                key=lambda r: r.get("metadata", {}).get("timestamp", ""),
                reverse=True,
            )
            for r in precise[:top_k]:
                key = r["content"]
                if key not in seen:
                    seen.add(key)
                    results.append(r)

        # Layer 2: 语义搜索兆底（跨 App / 系统级 / 无包名场景）
        if len(results) < top_k and user_request:
            semantic = self.query(user_request[:80], knowledge_type="experience",
                                  top_k=top_k - len(results))
            for r in semantic:
                key = r["content"]
                if key not in seen:
                    seen.add(key)
                    results.append(r)

        return results

    # ── 人工知识 (curated_rule) ──

    def save_curated_rule(self, app_package: str, content: str) -> None:
        """保存人工知识 —— app_package 为空表示全局，有值表示 App 特定。"""
        existing = self.query("", knowledge_type="curated_rule", top_k=10)
        if any(e.get("content", "") == content
               and e.get("metadata", {}).get("app_package", "") == app_package
               for e in existing):
            return
        self.save_knowledge(UIKnowledge(
            app_package=app_package, knowledge_type="curated_rule",
            content=content,
            metadata={"scope": "global" if not app_package else "app",
                      "timestamp": datetime.now().isoformat()},
        ))

    def query_curated_rules(self, app_package: str, top_k: int = 5) -> str:
        """查询人工知识：一次查询全部，Python 侧按 app_package 分组。"""
        # 拉取比 top_k 更多的结果，确保分组后每组都有足够条目
        all_results = self.query("", knowledge_type="curated_rule", top_k=top_k * 2)

        global_lines: list[str] = []
        app_lines: list[str] = []
        for r in all_results:
            pkg = r.get("metadata", {}).get("app_package", "")
            if not pkg:
                global_lines.append(f"- {r['content']}")
            elif pkg == app_package:
                app_lines.append(f"- {r['content']}")
            # 其他 app_package 的规则不返回，避免跨 App 泄漏

        parts = []
        if global_lines:
            parts.append("### 全局知识\n" + "\n".join(global_lines))
        if app_lines:
            parts.append("### App 操作前提\n" + "\n".join(app_lines))
        return "\n\n".join(parts)

    # ── 旧类型别名兼容（已删除的类型仍保留别名映射，避免旧数据查询报错） ──

    @property
    def count(self) -> int:
        return self.backend.count()


