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
    _GLOBAL_RULE_MIN_SCORE = 3

    def __init__(self, backend: VectorStoreBackend):
        self.backend = backend

    # ── 通用存取 ──

    def save_knowledge(self, knowledge: UIKnowledge) -> None:
        self.backend.add(
            knowledge.content,
            {
                "app_package": knowledge.app_package,
                "knowledge_type": knowledge.knowledge_type,
                **knowledge.metadata,
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
        domain: str = "",
        scenario: str = "",
        quality_score: float = 1.0,
        app_version: str = "",
        last_verified_at: str = "",
        applicable_domains: list[str] | None = None,
    ) -> None:
        """保存人工知识 —— app_package 为空表示全局，有值表示 App 特定。"""
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
                    "scope": "global" if not app_package else "app",
                    "timestamp": datetime.now().isoformat(),
                    "domain": domain,
                    "scenario": scenario,
                    "quality_score": quality_score,
                    "app_version": app_version,
                    "last_verified_at": last_verified_at or datetime.now().isoformat(),
                    "applicable_domains": applicable_domains or [],
                },
            )
        )

    def query_curated_rules(
        self, app_package: str, user_request: str = "", top_k: int = 5
    ) -> str:
        """查询人工知识：一次查询全部，Python 侧按 app_package 分组。"""
        # 拉取比 top_k 更多的结果，确保分组后每组都有足够条目
        all_results = self.query("", knowledge_type="curated_rule", top_k=top_k * 2)

        global_lines: list[tuple[int, str]] = []
        app_lines: list[str] = []
        app_tokens = self._app_tokens(app_package)
        req_tokens = self._request_tokens(user_request)
        app_domains = self._infer_app_domains(app_package)
        for r in all_results:
            pkg = r.get("metadata", {}).get("app_package", "")
            meta = r.get("metadata", {}) or {}
            content = str(r.get("content", "") or "")
            if not pkg:
                score = self._global_rule_relevance(content, app_tokens, req_tokens)
                score_detail = {
                    "rule_id": meta.get("id", ""),
                    "score": score,
                    "min_score": self._GLOBAL_RULE_MIN_SCORE,
                    "scope": "global",
                    "app_package": app_package,
                }
                applicable = set(
                    str(x).lower() for x in (meta.get("applicable_domains") or [])
                )
                if applicable and app_domains and not (app_domains & applicable):
                    logger.info(
                        "rule_drop_reason=%s rule_score_detail=%s",
                        "domain_mismatch",
                        score_detail,
                    )
                    continue
                if score >= self._GLOBAL_RULE_MIN_SCORE:
                    logger.info(
                        "rule_score_detail=%s",
                        {**score_detail, "decision": "keep"},
                    )
                    global_lines.append((score, f"- {content}"))
                else:
                    logger.info(
                        "rule_drop_reason=%s rule_score_detail=%s",
                        "score_below_threshold",
                        {**score_detail, "decision": "drop"},
                    )
            elif pkg == app_package:
                app_lines.append(f"- {content}")
            # 其他 app_package 的规则不返回，避免跨 App 泄漏

        parts = []
        global_lines.sort(key=lambda x: x[0], reverse=True)
        ranked_global = [line for _, line in global_lines[:top_k]]
        if ranked_global:
            parts.append("### 全局知识\n" + "\n".join(ranked_global))
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

    def _app_tokens(self, app_package: str) -> set[str]:
        if not app_package:
            return set()
        toks = {t.strip().lower() for t in app_package.split(".") if t.strip()}
        return {t for t in toks if len(t) >= 3}

    def _global_rule_relevance(
        self, content: str, app_tokens: set[str], req_tokens: set[str]
    ) -> int:
        text = (content or "").lower()
        if not text:
            return 0
        generic_tokens = (
            "系统",
            "导航",
            "权限",
            "返回",
            "选择",
            "确认",
            "设置",
            "页面",
        )
        score = 0
        if any(t in text for t in app_tokens):
            score += 3
        if req_tokens and any(t in text for t in req_tokens):
            score += 2
        if any(t in text for t in generic_tokens):
            score += 1
        return score

    def _request_tokens(self, user_request: str) -> set[str]:
        text = (user_request or "").strip().lower()
        if not text:
            return set()
        cn = {ch for ch in text if "一" <= ch <= "鿿"}
        en = {
            tok
            for tok in text.replace("/", " ").replace("_", " ").split()
            if len(tok) >= 3
        }
        return set(list(cn)[:20]) | set(list(en)[:20])

    def _infer_app_domains(self, app_package: str) -> set[str]:
        pkg = (app_package or "").lower()
        if not pkg:
            return set()
        domains: set[str] = set()
        if "gallery" in pkg or "photo" in pkg or "media" in pkg:
            domains.update({"gallery", "media"})
        if "settings" in pkg:
            domains.add("system")
        if "camera" in pkg:
            domains.add("camera")
        return domains

    # ── 旧类型别名兼容（已删除的类型仍保留别名映射，避免旧数据查询报错） ──

    @property
    def count(self) -> int:
        return self.backend.count()
