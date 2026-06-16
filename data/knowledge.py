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
            page = entry.get("page", "未知页面")
            action = entry.get("action", "?")
            observation = entry.get("observation", "")
            step_ok = entry.get("result") == "success"
            if page not in visited:
                visited.add(page)
                self.save_page_structure(app_package, page, [])
            if step_ok:
                self.save_navigation_path(app_package, page, "下一页面", action)
            self.save_test_experience(
                app_package, page, action,
                "成功" if step_ok else "失败",
                observation or entry.get("error", ""),
            )
            count += 1
        return count

    def get_app_context(self, app_package: str) -> str:
        results = self.query(app_package, app_package=app_package, top_k=10)
        if not results:
            return ""
        lines = [f"### {app_package} 历史知识"]
        lines.extend(f"- {r['content']}" for r in results)
        return "\n".join(lines)

    # ── Memory 接口 ──

    def as_retriever_memory(self):
        """返回 LangChain VectorStoreRetrieverMemory（仅 Chroma 后端支持）。"""
        if hasattr(self.backend, "store"):
            try:
                from langchain.memory import VectorStoreRetrieverMemory
                retriever = self.backend.store.as_retriever(search_kwargs={"k": 5})
                return VectorStoreRetrieverMemory(retriever=retriever, memory_key="rag_context")
            except Exception:
                pass
        return None

    def load_memory_context(self, query: str, app_package: str = "") -> str:
        results = self.query(query, app_package=app_package, top_k=5)
        if not results:
            return ""
        return "\n".join(f"- {r['content']}" for r in results)

    # ── 元素身份知识 (ChromaDB) ──

    def save_element_knowledge(self, app_package: str, page: str,
                               alias: str, role: str = "", region: str = "",
                               resource_id: str = "", strategy: str = "",
                               detail: str = "") -> None:
        """保存确认过的元素身份到向量库, 供 Planner RAG 检索。
        只存设备无关属性(resource_id/role/region), 不存 bounds 坐标。
        """
        parts = [f"{app_package} 的 {page} 页面中, '{alias}' 是 {role or '未知'} 类型"]
        if region:
            parts.append(f"位于 {region} 区域")
        if resource_id:
            parts.append(f"resource_id={resource_id}")
        if strategy:
            parts.append(f"推荐策略: {strategy}")
        content = ", ".join(parts) + "。" + (detail or "")
        self.save_knowledge(UIKnowledge(
            app_package=app_package,
            knowledge_type="element_identity",
            content=content,
            metadata={
                "page": page, "alias": alias, "role": role,
                "region": region, "resource_id": resource_id,
                "strategy": strategy,
                "timestamp": datetime.now().isoformat(),
            },
        ))

    def query_element_knowledge(self, app_package: str, query: str,
                                top_k: int = 5) -> list[dict[str, Any]]:
        """查询元素身份知识。"""
        return self.query(
            query, app_package=app_package,
            knowledge_type="element_identity", top_k=top_k,
        )

    # ── 验证计划 (ChromaDB) ──

    def save_verified_plan(self, app_package: str, user_request: str,
                           plan: list[dict[str, Any]],
                           results: list[dict[str, Any]]) -> None:
        """保存验证计划到向量库, 供下次 Planner RAG 检索。"""
        success_targets = [s.get("target", "") for s in results if s.get("status") == "success"]
        fail_targets = [s.get("target", "") for s in results if s.get("status") != "success"]
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


def build_rag_enhanced_prompt(base_prompt: str, knowledge_base: KnowledgeBase,
                              app_package: str) -> str:
    context = knowledge_base.get_app_context(app_package)
    if not context:
        return base_prompt
    return f"{base_prompt}\n\n## 已有 APP 知识\n{context}\n\n以当前页面实际状态为准。"
