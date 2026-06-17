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
    """RAG 知识库 —— 通过 VectorStoreBackend 接口操作底层向量存储。

    支持 Chroma（有 API Key）和 Memory（无 API Key）两种后端。
    业务逻辑（页面结构、导航路径、测试经验）和底层存储解耦。
    """

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
        filter_dict: dict[str, str] = {}
        if app_package:
            filter_dict["app_package"] = app_package
        if knowledge_type:
            filter_dict["knowledge_type"] = knowledge_type
        return self.backend.search(query, filter_dict if filter_dict else None, top_k)

    # ── 导航路径 ──

    def save_navigation_path(self, app_package: str, from_page: str, to_page: str,
                             action: str) -> None:
        self.save_knowledge(UIKnowledge(
            app_package=app_package,
            knowledge_type="navigation_path",
            content=f"在 {from_page} 页面执行 '{action}' 后到达 {to_page}",
            metadata={"from_page": from_page, "to_page": to_page, "action": action,
                      "timestamp": datetime.now().isoformat()},
        ))

    def query_navigation(self, app_package: str, page: str,
                         top_k: int = 5) -> list[dict[str, Any]]:
        return self.query(f"从 {page} 导航", app_package=app_package,
                          knowledge_type="navigation_path", top_k=top_k)

    # ── 测试经验 ──

    def save_test_experience(self, app_package: str, page: str, action: str,
                             outcome: str, detail: str = "") -> None:
        self.save_knowledge(UIKnowledge(
            app_package=app_package,
            knowledge_type="test_experience",
            content=f"在 {page} 执行 '{action}' → {outcome}"
                    + (f": {detail}" if detail else ""),
            metadata={"page": page, "action": action, "outcome": outcome,
                      "timestamp": datetime.now().isoformat()},
        ))

    # ── 页面结构 ──

    def save_page_structure(self, app_package: str, page_name: str,
                            elements: list[dict[str, Any]]) -> int:
        labels = [e.get("label") or e.get("text") or e.get("content_desc")
                  for e in elements[:30]]
        labels = [l for l in labels if l]
        self.save_knowledge(UIKnowledge(
            app_package=app_package,
            knowledge_type="page_structure",
            content=f"{app_package} 的 {page_name} 页面包含: {'; '.join(labels)}",
            metadata={"page": page_name, "element_count": len(elements),
                      "timestamp": datetime.now().isoformat()},
        ))
        return 1

    # ── 从测试结果批量提取 ──

    def extract_from_test_result(
        self, app_package: str, test_case: str,
        execution_log: list[dict[str, Any]], final_result: str,
    ) -> int:
        count = 0
        visited: set[str] = set()
        for entry in execution_log:
            page = entry.get("page", "") or "未知页面"
            action = entry.get("action", "?")
            observation = entry.get("observation", "")
            step_ok = entry.get("result") == "success"

            # 从 agent_node 源头的 post_page 字段读取真实跳转页面
            to_page = entry.get("post_page", "")
            if page and to_page and to_page != page:
                self.save_navigation_path(app_package, page, to_page, action)
                count += 1

            # page_structure: 有标签才写入（不再写空列表）
            if page not in visited:
                visited.add(page)
                labels = _extract_labels_from_observation(observation)
                if labels:
                    self.save_page_structure(app_package, page, [{"label": l} for l in labels])
                    count += 1

            self.save_test_experience(
                app_package, page, action,
                "成功" if step_ok else "失败",
                observation or entry.get("error", ""),
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

    # ── App 前提条件 (app_precondition) ──

    def save_precondition(self, app_package: str, rule: str) -> None:
        """保存 App 特定的操作前提条件，如'计算器需先清空输入区'。"""
        existing = self.query("", app_package=app_package,
                            knowledge_type="app_precondition", top_k=5)
        if any(e.get("content", "") == rule for e in existing):
            return  # P6.1 去重：已有相同规则则跳过
        self.save_knowledge(UIKnowledge(
            app_package=app_package,
            knowledge_type="app_precondition",
            content=rule,
            metadata={"timestamp": datetime.now().isoformat()},
        ))

    def query_preconditions(self, app_package: str, top_k: int = 3) -> str:
        """查询 App 的操作前提条件，返回拼接后的规则文本。"""
        results = self.query("", app_package=app_package,
                           knowledge_type="app_precondition", top_k=top_k)
        if not results:
            return ""
        return "\n".join(f"- {r['content']}" for r in results)

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


