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
    """RAG 知识库 —— 业务逻辑（操作经验、验证计划、人工知识）和底层存储解耦。"""

    # 类型别名映射（旧数据兼容：查询新类型时自动匹配旧类型）
    _TYPE_ALIASES = {
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
        """保存操作经验 —— 统一记录页面操作的结果。"""

        # 动态构建 content（根据实际参数灵活组合）
        parts = [f"在 {page} 页面"]
        if action:
            parts.append(f"通过 '{action}' 操作")
        if to_page:
            parts.append(f"到达 {to_page}")
        if labels:
            parts.append(f"包含: {'; '.join(labels[:15])}")
        if outcome:
            parts.append(f"结果: {outcome}")
        if detail and outcome != "成功":
            parts.append(f"详情: {detail[:100]}")
        content = "，".join(parts)

        # 去重：用 content 文本直接比较，简单可靠
        existing = self.query("", app_package=app_package,
                             knowledge_type="experience", top_k=5)
        if any(e.get("content", "") == content for e in existing):
            return

        self.save_knowledge(UIKnowledge(
            app_package=app_package, knowledge_type="experience",
            content=content,
            metadata={"page": page, "action": action, "to_page": to_page,
                      "outcome": outcome, "timestamp": datetime.now().isoformat()},
        ))

    def query_experience(self, app_package: str, page: str, top_k: int = 5):
        return self.query(f"从 {page} 操作", app_package=app_package,
                         knowledge_type="experience", top_k=top_k)

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

    # ── 旧方法兼容 wrapper（转发 + DeprecationWarning）──

    def save_navigation_path(self, app_package: str, from_page: str, to_page: str,
                             action: str) -> None:
        import warnings
        warnings.warn("save_navigation_path is deprecated, use save_experience", DeprecationWarning)
        self.save_experience(app_package=app_package, page=from_page,
                            action=action, to_page=to_page, outcome="成功")

    def save_test_experience(self, app_package: str, page: str, action: str,
                             outcome: str, detail: str = "") -> None:
        import warnings
        warnings.warn("save_test_experience is deprecated, use save_experience", DeprecationWarning)
        self.save_experience(app_package=app_package, page=page, action=action,
                            outcome=outcome, detail=detail)

    def save_page_structure(self, app_package: str, page_name: str,
                            elements: list[dict[str, Any]]) -> int:
        import warnings
        warnings.warn("save_page_structure is deprecated, use save_experience", DeprecationWarning)
        labels = [e.get("label") or e.get("text") or e.get("content_desc") for e in elements[:30]]
        labels = [l for l in labels if l]
        self.save_experience(app_package=app_package, page=page_name, labels=labels)
        return 1

    def save_precondition(self, app_package: str, rule: str) -> None:
        import warnings
        warnings.warn("save_precondition is deprecated, use save_curated_rule", DeprecationWarning)
        self.save_curated_rule(app_package=app_package, content=rule)

    def save_global_knowledge(self, content: str) -> None:
        import warnings
        warnings.warn("save_global_knowledge is deprecated, use save_curated_rule", DeprecationWarning)
        self.save_curated_rule(app_package="", content=content)

    def query_navigation(self, app_package: str, page: str,
                         top_k: int = 5) -> list[dict[str, Any]]:
        import warnings
        warnings.warn("query_navigation is deprecated, use query_experience", DeprecationWarning)
        return self.query_experience(app_package, page, top_k=top_k)

    def query_preconditions(self, app_package: str, top_k: int = 3) -> str:
        import warnings
        warnings.warn("query_preconditions is deprecated, use query_curated_rules", DeprecationWarning)
        rules = self.query_curated_rules(app_package, top_k=top_k)
        return rules.replace("### 全局知识\n", "").replace("### App 操作前提\n", "").strip()

    def query_global_knowledge(self, query: str = "",
                                top_k: int = 5) -> str:
        import warnings
        warnings.warn("query_global_knowledge is deprecated, use query_curated_rules", DeprecationWarning)
        rules = self.query_curated_rules("", top_k=top_k)
        return rules.replace("### 全局知识\n", "").strip()

    # ── 从测试结果批量提取 ──

    def extract_from_test_result(
        self, app_package: str, test_case: str,
        execution_log: list[dict[str, Any]], final_result: str,
    ) -> int:
        count = 0
        visited_pages: set[str] = set()
        for entry in execution_log:
            page = entry.get("page", "") or "未知页面"
            action = entry.get("action", "?")
            observation = entry.get("observation", "")
            step_ok = entry.get("result") == "success"
            to_page = entry.get("post_page", "")

            # labels 提取沿用原逻辑，visited 去重（同一页面不重复提取）
            labels = []
            if page not in visited_pages:
                visited_pages.add(page)
                labels = _extract_labels_from_observation(observation)

            self.save_experience(
                app_package=app_package, page=page, action=action,
                to_page=to_page if (to_page and to_page != page) else "",
                outcome="成功" if step_ok else "失败",
                detail=observation if not step_ok else "",
                labels=labels,
            )
            count += 1
        return count

    # ── 验证计划 (ChromaDB) ──

    def save_verified_plan(self, app_package: str, user_request: str,
                           plan: list[dict[str, Any]],
                           results: list[dict[str, Any]]) -> None:
        """保存验证计划到向量库, 供下次 Planner RAG 检索。"""
        # P6.1 去重：已有相似计划则跳过
        existing = self.query_verified_plan(app_package, user_request, top_k=1)
        if existing:
            return
        success_targets = [s.get("intent", "")[:30] for s in results if s.get("status") == "success"]
        fail_targets = [s.get("intent", "")[:30] for s in results if s.get("status") != "success"]
        steps_desc = "; ".join(
            f"步骤{s.get('index')}: {s.get('intent')}({s.get('action_type')}->{s.get('target')})"
            for s in plan
        )
        content = (
            f"{app_package} 测试 '{user_request[:50]}' 的计划: "
            f"共 {len(plan)} 步, "
            f"已验证成功: {', '.join(success_targets) or '无'}, "
            f"待验证: {', '.join(fail_targets) or '无'}. "
            f"步骤: {steps_desc}"
        )
        self.save_knowledge(UIKnowledge(
            app_package=app_package,
            knowledge_type="verified_plan",
            content=content,
            metadata={
                "user_request_hash": str(hash(user_request))[:16],
                "total_steps": len(plan),
                "success_count": len(success_targets),
                "fail_count": len(fail_targets),
                "timestamp": datetime.now().isoformat(),
            },
        ))

    def query_verified_plan(self, app_package: str, user_request: str,
                            top_k: int = 1) -> list[dict[str, Any]]:
        """查询向量库中的验证计划。"""
        return self.query(
            f"{user_request[:50]} 测试计划",
            app_package=app_package,
            knowledge_type="verified_plan",
            top_k=top_k,
        )

    @property
    def count(self) -> int:
        return self.backend.count()


def _extract_labels_from_observation(observation: str) -> list[str]:
    """从 observation 文本中提取元素标签。"""
    labels: list[str] = []
    for line in observation.split("\n"):
        for m in __import__("re").finditer(r"label='([^']+)'", line):
            labels.append(m.group(1))
        for m in __import__("re").finditer(r'click\("([^"]+)"\)', line):
            labels.append(m.group(1))
    return list(dict.fromkeys(labels))[:20]


