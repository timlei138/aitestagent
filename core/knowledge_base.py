from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class UIKnowledge:
    app_package: str
    knowledge_type: str
    content: str
    metadata: dict[str, Any]


class KnowledgeBase:
    """RAG 知识库封装；未配置 embedding 时自动降级为内存知识库。"""

    def __init__(
        self,
        persist_dir: str = "storage/knowledge",
        embedding_model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.persist_dir = persist_dir
        os.makedirs(persist_dir, exist_ok=True)
        self._memory: list[UIKnowledge] = []
        self.vectorstore = None
        if api_key:
            try:
                from langchain_chroma import Chroma
                from langchain_openai import OpenAIEmbeddings

                embeddings = OpenAIEmbeddings(
                    model=embedding_model, api_key=api_key, base_url=base_url
                )
                self.vectorstore = Chroma(
                    collection_name="app_knowledge",
                    embedding_function=embeddings,
                    persist_directory=persist_dir,
                )
            except Exception:
                self.vectorstore = None

    def save_knowledge(self, knowledge: UIKnowledge) -> None:
        self._memory.append(knowledge)
        if self.vectorstore:
            from langchain_core.documents import Document

            self.vectorstore.add_documents(
                [
                    Document(
                        page_content=knowledge.content,
                        metadata={
                            "app_package": knowledge.app_package,
                            "knowledge_type": knowledge.knowledge_type,
                            **knowledge.metadata,
                        },
                    )
                ]
            )

    def save_ui_structure(
        self,
        app_package: str,
        page_name: str,
        elements: list[dict[str, Any]],
        test_case: str = "",
        success: bool = True,
    ) -> int:
        labels = [
            e.get("label") or e.get("text") or e.get("content_desc")
            for e in elements[:30]
        ]
        labels = [label for label in labels if label]
        knowledge = UIKnowledge(
            app_package=app_package,
            knowledge_type="ui_structure",
            content=f"{app_package} 的 {page_name} 页面包含元素: {'; '.join(labels)}",
            metadata={
                "page": page_name,
                "test_case": test_case,
                "success": success,
                "timestamp": datetime.now().isoformat(),
            },
        )
        self.save_knowledge(knowledge)
        return 1

    def query(
        self,
        query: str,
        app_package: str | None = None,
        knowledge_type: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        if self.vectorstore:
            filters = {}
            if app_package:
                filters["app_package"] = app_package
            if knowledge_type:
                filters["knowledge_type"] = knowledge_type
            kwargs = {"k": top_k}
            if filters:
                kwargs["filter"] = filters
            results = self.vectorstore.similarity_search_with_score(query, **kwargs)
            return [
                {"content": doc.page_content, "metadata": doc.metadata, "score": score}
                for doc, score in results
            ]

        items = self._memory
        if app_package:
            items = [item for item in items if item.app_package == app_package]
        if knowledge_type:
            items = [item for item in items if item.knowledge_type == knowledge_type]
        return [
            {"content": item.content, "metadata": item.metadata, "score": 0.0}
            for item in items[:top_k]
        ]

    def get_app_context(self, app_package: str) -> str:
        results = self.query(
            f"{app_package} 的界面结构和操作方式", app_package=app_package, top_k=8
        )
        if not results:
            return ""
        lines = [f"### {app_package} 历史知识"]
        lines.extend(f"- {item['content']}" for item in results)
        return "\n".join(lines)


    def extract_from_test_result(
        self,
        app_package: str,
        test_case: str,
        execution_log: list[dict[str, Any]],
        final_result: str,
    ) -> int:
        """从测试结果中提取知识并存入知识库。

        Args:
            app_package: 目标应用包名。
            test_case: 测试用例名称。
            execution_log: 执行日志列表，每项含 page/action/observation/result/error。
            final_result: 最终结果，PASS 或 FAIL。
        """
        success = final_result.upper() == "PASS"
        knowledges: list[UIKnowledge] = []
        visited_pages: set[str] = set()

        for entry in execution_log:
            page_name = entry.get("page", "未知页面")
            if page_name not in visited_pages:
                visited_pages.add(page_name)
                observation = entry.get("observation", "")
                if not observation:
                    observation = f"{page_name} 页面执行 '{entry.get('action', '?')}' 后状态正常"
                knowledges.append(
                    UIKnowledge(
                        app_package=app_package,
                        knowledge_type="ui_structure",
                        content=f"{app_package} 的 {page_name} 页面：{observation[:500]}",
                        metadata={
                            "page": page_name,
                            "test_case": test_case,
                            "success": success,
                            "timestamp": datetime.now().isoformat(),
                        },
                    )
                )

        # 提取导航路径（成功步骤之间的跳转）
        for i in range(len(execution_log) - 1):
            frm = execution_log[i]
            to = execution_log[i + 1]
            if frm.get("result") == "success":
                knowledges.append(
                    UIKnowledge(
                        app_package=app_package,
                        knowledge_type="navigation_path",
                        content=(
                            f"在 {frm.get('page', '?')} 页面执行 "
                            f"'{frm.get('action', '?')}' 后到达 {to.get('page', '?')}"
                        ),
                        metadata={
                            "from_page": frm.get("page", "?"),
                            "to_page": to.get("page", "?"),
                            "test_case": test_case,
                            "success": success,
                            "timestamp": datetime.now().isoformat(),
                        },
                    )
                )

        # 提取失败经验
        for entry in execution_log:
            if entry.get("result") == "fail":
                knowledges.append(
                    UIKnowledge(
                        app_package=app_package,
                        knowledge_type="test_experience",
                        content=(
                            f"在 {entry.get('page', '?')} 执行 "
                            f"'{entry.get('action', '?')}' 失败：{entry.get('error', '未知错误')}"
                        ),
                        metadata={
                            "page": entry.get("page", "?"),
                            "test_case": test_case,
                            "success": False,
                            "timestamp": datetime.now().isoformat(),
                        },
                    )
                )

        for k in knowledges:
            self.save_knowledge(k)
        return len(knowledges)


def build_rag_enhanced_prompt(
    base_prompt: str, knowledge_base: KnowledgeBase, app_package: str
) -> str:
    context = knowledge_base.get_app_context(app_package)
    if not context:
        return base_prompt
    return f"{base_prompt}\n\n## 已有 APP 知识\n{context}\n\n以当前页面实际状态为准。"
