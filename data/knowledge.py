from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from data.vector_store import VectorStoreBackend

logger = logging.getLogger(__name__)


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
        "experience": [
            "experience",
            "navigation_path",
            "page_structure",
            "test_experience",
        ],
        "curated_rule": ["curated_rule", "app_precondition", "global_knowledge"],
    }
    _EXPERIENCE_LAYER2_MAX_DISTANCE = 1.2

    def __init__(self, backend: VectorStoreBackend):
        self.backend = backend

    # ── 通用存取 ──

    @staticmethod
    def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        """规范化 metadata，避免向量库因空列表值写入失败。"""
        cleaned: dict[str, Any] = {}
        for k, v in (metadata or {}).items():
            if v is None:
                continue
            if isinstance(v, list):
                normalized = [x for x in v if x not in (None, "")]
                if normalized:
                    cleaned[k] = normalized
                continue
            cleaned[k] = v
        return cleaned

    def save_knowledge(self, knowledge: UIKnowledge) -> None:
        metadata = dict(knowledge.metadata or {})
        # 防御性规范化：人工知识写入时自动补齐 scope，避免“有数据但不可检索”
        if knowledge.knowledge_type in self._TYPE_ALIASES.get(
            "curated_rule", ["curated_rule"]
        ):
            if "scope" not in metadata or not metadata.get("scope"):
                metadata["scope"] = "universal" if not knowledge.app_package else "app"
        metadata = self._sanitize_metadata(metadata)
        self.backend.add(
            knowledge.content,
            {
                "app_package": knowledge.app_package,
                "knowledge_type": knowledge.knowledge_type,
                **metadata,
            },
        )

    def query(
        self,
        query: str,
        app_package: str = "",
        knowledge_type: str = "",
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
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

    def list_entries(
        self, app_package: str = "", knowledge_type: str = "", top_k: int = 50
    ) -> list[dict[str, Any]]:
        """列出知识（metadata 精确读取，不走向量相似度检索）。"""
        where: dict[str, Any] = {}
        if app_package:
            where["app_package"] = app_package
        if knowledge_type:
            aliases = self._TYPE_ALIASES.get(knowledge_type, [knowledge_type])
            if len(aliases) == 1:
                where["knowledge_type"] = aliases[0]
            else:
                where["$or"] = [{"knowledge_type": a} for a in aliases]
        items = self.backend.get_by_metadata(where, limit=top_k)
        items.sort(
            key=lambda r: str(r.get("metadata", {}).get("timestamp", "")), reverse=True
        )
        return items

    # ── 操作经验 (experience) ──

    def save_experience(
        self,
        app_package: str,
        page: str,
        action: str = "",
        to_page: str = "",
        outcome: str = "",
        detail: str = "",
        labels: list[str] | None = None,
        app_version: str = "",
        last_verified_at: str = "",
    ) -> None:
        """保存操作经验 —— 精简格式: A → action → B"""

        # 精简内容格式
        content = page
        if action:
            content += f" → {action}"
        if to_page:
            content += f" → {to_page}"

        # 去重：用 get_by_metadata 按 metadata 精确过滤（不走向量搜索）
        existing = self.backend.get_by_metadata(
            where={
                "app_package": app_package,
                "knowledge_type": "experience",
                "page": page,
                "action": action,
                "to_page": to_page,
            },
            limit=1,
        )
        if existing:
            return

        self.save_knowledge(
            UIKnowledge(
                app_package=app_package,
                knowledge_type="experience",
                content=content,
                metadata={
                    "page": page,
                    "action": action,
                    "to_page": to_page,
                    "outcome": outcome,
                    "timestamp": datetime.now().isoformat(),
                    "app_version": app_version,
                    "last_verified_at": last_verified_at or datetime.now().isoformat(),
                },
            )
        )

    def query_experience(
        self, app_package: str, user_request: str = "", top_k: int = 5
    ) -> list[dict[str, Any]]:
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
                    r = {**r, "source_scope": "same_app"}
                    results.append(r)

        # Layer 2: 语义搜索兆底（先同 App；仅在无结果时跨 App）
        if len(results) < top_k and user_request:
            semantic = (
                self.query(
                    user_request[:80],
                    app_package=app_package,
                    knowledge_type="experience",
                    top_k=top_k - len(results),
                )
                if app_package
                else []
            )
            for r in semantic:
                if float(r.get("score", 999.0)) > self._EXPERIENCE_LAYER2_MAX_DISTANCE:
                    continue
                key = r["content"]
                if key not in seen:
                    seen.add(key)
                    r = {**r, "source_scope": "same_app"}
                    results.append(r)

        if len(results) < top_k and user_request:
            semantic = self.query(
                user_request[:80],
                knowledge_type="experience",
                top_k=top_k - len(results),
            )
            for r in semantic:
                if float(r.get("score", 999.0)) > self._EXPERIENCE_LAYER2_MAX_DISTANCE:
                    continue
                key = r["content"]
                if key not in seen:
                    seen.add(key)
                    r = {**r, "source_scope": "cross_app"}
                    results.append(r)

        return results

    # ── 人工知识 (curated_rule) ──

    def save_curated_rule(
        self,
        app_package: str,
        content: str,
        *,
        scope: str = "app",
        reviewed_by: str = "",
        domain: str = "",
        scenario: str = "",
        quality_score: float = 1.0,
        app_version: str = "",
        last_verified_at: str = "",
        applicable_domains: list[str] | None = None,
    ) -> None:
        """保存人工知识 —— 默认 App 规则；universal 规则需显式声明并审核。"""
        if scope not in {"app", "universal"}:
            raise ValueError(f"Invalid scope: {scope}. Expected 'app' or 'universal'.")
        if not app_package:
            if scope != "universal":
                raise ValueError(
                    f"Global rule requires scope='universal'. Got scope='{scope}'. "
                    f"Content: {content[:60]}"
                )
            if not reviewed_by:
                raise ValueError(
                    "Universal rule requires reviewed_by (audit trail). "
                    f"Content: {content[:60]}"
                )
            logger.info(
                "Saving universal rule (reviewed_by=%s): %s",
                reviewed_by,
                content[:60],
            )
        elif scope != "app":
            raise ValueError(
                f"App-specific rule must use scope='app'. Got scope='{scope}'. "
                f"app_package={app_package}"
            )

        existing = self.query("", knowledge_type="curated_rule", top_k=10)
        if any(
            e.get("content", "") == content
            and e.get("metadata", {}).get("app_package", "") == app_package
            for e in existing
        ):
            return
        self.save_knowledge(
            UIKnowledge(
                app_package=app_package,
                knowledge_type="curated_rule",
                content=content,
                metadata={
                    "scope": scope,
                    "reviewed_by": reviewed_by,
                    "timestamp": datetime.now().isoformat(),
                    "domain": domain,
                    "scenario": scenario,
                    "quality_score": quality_score,
                    "app_version": app_version,
                    "last_verified_at": last_verified_at or datetime.now().isoformat(),
                    "applicable_domains": applicable_domains,
                },
            )
        )

    def query_curated_rules(
        self, app_package: str, user_request: str = "", top_k: int = 5
    ) -> str:
        """查询人工知识：双路精确 metadata 查询，无评分过滤。"""
        del user_request  # 接口兼容：当前实现不使用 request 文本做规则筛选
        type_filter = {
            "$or": [
                {"knowledge_type": t}
                for t in self._TYPE_ALIASES.get("curated_rule", ["curated_rule"])
            ]
        }

        app_lines: list[str] = []
        if app_package:
            app_rules = self.backend.get_by_metadata(
                {**type_filter, "app_package": app_package},
                limit=top_k,
            )
            app_lines = [f"- {str(r.get('content', '') or '')}" for r in app_rules]

        universal_rules = self.backend.get_by_metadata(
            {**type_filter, "app_package": "", "scope": "universal"},
            limit=top_k,
        )
        # 兜底：兼容历史数据未写入 scope 的全局规则
        if not universal_rules:
            fallback_rules = self.backend.get_by_metadata(
                {**type_filter, "app_package": ""},
                limit=top_k * 2,
            )
            universal_rules = [
                r
                for r in fallback_rules
                if str((r.get("metadata", {}) or {}).get("scope", "") or "").strip()
                in ("", "universal")
            ][:top_k]
        universal_lines = [
            f"- {str(r.get('content', '') or '')}" for r in universal_rules
        ]

        parts = []
        if universal_lines:
            parts.append("### 通用知识\n" + "\n".join(universal_lines))
        if app_lines:
            parts.append("### App 操作前提\n" + "\n".join(app_lines))
        return "\n\n".join(parts)

    def ensure_gallery_seed_knowledge(self) -> int:
        """注入图库冷启动种子知识（幂等）。返回新增条数。"""
        before = self.count
        gallery_pkg = "com.zui.gallery"

        curated_rules = [
            "进入图库后，如需批量选择，优先查找‘选择’或‘多选’入口。",
            "批量选择模式下，选中图片后通常会出现勾选态或顶部计数提示。",
            "完成选择后，优先点击‘完成/确认/分享’而不是反复点缩略图。",
        ]
        experiences = [
            ("图库首页", "点击 多选/选择", "选择模式"),
            ("选择模式", "点击 图片项", "图片已选中"),
            ("选择模式", "点击 完成/确认", "退出选择模式或进入下一步"),
        ]

        for rule in curated_rules:
            self.save_curated_rule(
                gallery_pkg,
                rule,
                domain="media",
                scenario="gallery_selection",
                quality_score=1.0,
                app_version="seed",
                applicable_domains=["media", "gallery"],
            )
        for page, action, to_page in experiences:
            self.save_experience(
                app_package=gallery_pkg,
                page=page,
                action=action,
                to_page=to_page,
                outcome="seed",
                app_version="seed",
            )

        after = self.count
        return max(0, after - before)

    # ── 旧类型别名兼容（已删除的类型仍保留别名映射，避免旧数据查询报错） ──

    @property
    def count(self) -> int:
        return self.backend.count()
