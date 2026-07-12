from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import re
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

    # 当前仅保留标准知识类型；历史别名已由迁移脚本归一化。
    _TYPE_ALIASES: dict[str, list[str]] = {
        "experience": ["experience"],
        "curated_rule": ["curated_rule"],
    }
    _EXPERIENCE_LAYER2_MAX_DISTANCE = 1.2
    _EXPERIENCE_MIN_QUALITY = 0.75
    _DYN_PAGE_PATTERNS = [
        re.compile(r"\d+\.\d+\s*[KMG]?[Bb]/s", re.IGNORECASE),
        re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),
        re.compile(r"\d+%"),
    ]

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
        app_version: str = "",
        last_verified_at: str = "",
        signal_type: str = "",
        quality_score: float = 1.0,
        action_semantic: str = "",
        page_stability: str = "",
    ) -> None:
        """保存操作经验 —— 精简格式: A → action → B"""

        # 精简内容格式：page/to_page 归一化后拼接，不含 hash/动态 token
        page_norm = self._normalize_page_id(page)
        to_page_norm = self._normalize_page_id(to_page)
        content = page_norm or page
        if action:
            content += f" → {action}"
        if to_page_norm:
            content += f" → {to_page_norm}"
        action_type, action_label_norm, rid_tail = self._parse_action_signature(action)

        # Phase 1：严格去重（app_package, page_norm, action_label_norm, rid_tail）
        existing = self.backend.get_by_metadata(
            where={"app_package": app_package, "knowledge_type": "experience"},
            limit=80,
        )
        duplicate = None
        for row in existing:
            meta = row.get("metadata", {}) or {}
            old_page_norm = str(meta.get("page_norm", "") or self._normalize_page_id(meta.get("page", "")))
            old_action = str(meta.get("action", "") or "")
            _, old_label_norm, old_rid_tail = self._parse_action_signature(old_action)
            if (
                old_page_norm == page_norm
                and old_label_norm == action_label_norm
                and old_rid_tail == rid_tail
            ):
                duplicate = row
                break

        now_iso = last_verified_at or datetime.now().isoformat()
        dedupe_raw = f"{app_package}|{page_norm}|{action_label_norm}|{rid_tail}"
        dedupe_key = hashlib.sha1(dedupe_raw.encode("utf-8")).hexdigest()[:16]

        # 优先按 dedupe_key 精确查找
        try:
            existing_by_key = self.backend.get_by_metadata(
                {"knowledge_type": "experience", "dedupe_key": dedupe_key},
                limit=1,
            )
            if existing_by_key and existing_by_key[0].get("id"):
                duplicate = existing_by_key[0]
        except Exception:
            logger.debug("dedupe_key lookup failed, fallback to scan", exc_info=True)

        if duplicate:
            meta = dict(duplicate.get("metadata", {}) or {})
            merged = {
                **meta,
                "last_verified_at": now_iso,
                "success_count": int(meta.get("success_count", 1) or 1) + 1,
                "quality_score": max(float(meta.get("quality_score", 0.0) or 0.0), float(quality_score or 0.0)),
                "signal_type": str(meta.get("signal_type", "") or signal_type),
                "action_semantic": str(meta.get("action_semantic", "") or action_semantic),
                "page_norm": str(meta.get("page_norm", "") or page_norm),
                "to_page_norm": str(meta.get("to_page_norm", "") or to_page_norm),
                "action_type": str(meta.get("action_type", "") or action_type),
                "action_label_norm": str(meta.get("action_label_norm", "") or action_label_norm),
                "rid_tail": str(meta.get("rid_tail", "") or rid_tail),
            }
            dup_id = duplicate.get("id", "")
            if dup_id:
                self.backend.delete_by_ids([dup_id])
                self.save_knowledge(
                    UIKnowledge(
                        app_package=app_package,
                        knowledge_type="experience",
                        content=duplicate.get("content", content),
                        metadata=merged,
                    )
                )
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
                    "last_verified_at": now_iso,
                    "signal_type": signal_type,
                    "quality_score": float(quality_score or 0.0),
                    "action_semantic": action_semantic,
                    "page_stability": page_stability,
                    "success_count": 1,
                    "page_norm": page_norm,
                    "to_page_norm": to_page_norm,
                    "action_type": action_type,
                    "action_label_norm": action_label_norm,
                    "rid_tail": rid_tail,
                    "dedupe_key": dedupe_key,
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
            raw_precise = self.backend.get_by_metadata(
                where={"app_package": app_package, "knowledge_type": "experience"},
                limit=top_k * 3,
            )
            # 优先 strong（quality>=门槛；缺失 quality_score 的历史默认 0）
            strong = [
                r
                for r in raw_precise
                if float((r.get("metadata", {}) or {}).get("quality_score", 0.0) or 0.0)
                >= self._EXPERIENCE_MIN_QUALITY
            ]
            # G3: strong 为空时用未过滤结果兜底，避免"有数据但查不到"
            # （缺分/低分历史被硬门槛静默丢弃）。仅在无强数据时启用，不削弱强数据优先。
            precise = strong if strong else raw_precise
            # ChromaDB get() 不保证顺序，Python 侧按质量+时间排序
            precise.sort(
                key=lambda r: (
                    float((r.get("metadata", {}) or {}).get("quality_score", 0.0) or 0.0),
                    {"exact_click": 2, "semantic_click": 1}.get(
                        str((r.get("metadata", {}) or {}).get("signal_type", "") or ""),
                        0,
                    ),
                    str((r.get("metadata", {}) or {}).get("last_verified_at", "")),
                ),
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

        # 确定性去重：扫描全量 curated_rule，按 (app_package, content) 精确匹配
        existing = self.backend.get_by_metadata(
            {"knowledge_type": "curated_rule"}, limit=500
        )
        for e in existing:
            if (
                str(e.get("content", "") or "") == content
                and str((e.get("metadata", {}) or {}).get("app_package", "")) == app_package
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

    def query_curated_rules(self, app_package: str, top_k: int = 5) -> str:
        """查询人工知识：双路精确 metadata 查询，无评分过滤。

        只返回测试人员手写的规则（无 scenario 或 scenario 不以 auto_ 开头）。
        Agent 自动生成的规则（scenario=auto_exact_click 等）走操作经验通道，
        不混入人工知识。
        """
        curated_aliases = self._TYPE_ALIASES.get("curated_rule", ["curated_rule"])
        if len(curated_aliases) == 1:
            type_filter = {"knowledge_type": curated_aliases[0]}
        else:
            type_filter = {
                "$or": [{"knowledge_type": t} for t in curated_aliases]
            }

        # 取超额（ChromaDB 不支持 NOT 过滤），Python 侧筛掉自动规则
        universal_slots = max(1, top_k // 5)
        _fetch_n = max(top_k * 3, 50)

        # G2: 人工知识是 prompt 第一优先级，不该只取 metadata 任意顺序前 N；
        # 按 (quality_score, last_verified_at) 降序排序后再截断（确定性、无需 query）。
        def _rule_rank(r: dict) -> tuple[float, str]:
            m = r.get("metadata", {}) or {}
            return (
                float(m.get("quality_score", 0.0) or 0.0),
                str(m.get("last_verified_at", "") or ""),
            )

        def _non_auto(rows: list) -> list:
            return [
                r
                for r in rows
                if not str(r.get("metadata", {}).get("scenario", "")).startswith("auto_")
            ]

        app_lines: list[str] = []
        if app_package:
            raw = _non_auto(
                self.backend.get_by_metadata(
                    {**type_filter, "app_package": app_package},
                    limit=_fetch_n,
                )
            )
            raw.sort(key=_rule_rank, reverse=True)
            app_lines = [f"- {str(r.get('content', '') or '')}" for r in raw][:top_k]

        raw_univ = _non_auto(
            self.backend.get_by_metadata(
                {**type_filter, "app_package": "", "scope": "universal"},
                limit=max(universal_slots * 3, 20),
            )
        )
        raw_univ.sort(key=_rule_rank, reverse=True)
        universal_lines = [
            f"- {str(r.get('content', '') or '')}" for r in raw_univ
        ][:universal_slots]

        parts = []
        # 优先级：人工全局 > App 规则
        if universal_lines:
            parts.append("### 通用知识\n" + "\n".join(universal_lines))
        if app_lines:
            parts.append("### App 操作前提\n" + "\n".join(app_lines))
        return "\n\n".join(parts)

    @property
    def count(self) -> int:
        return self.backend.count()

    @classmethod
    def _normalize_page_id(cls, value: str) -> str:
        s = str(value or "").strip()
        if not s:
            return ""
        # 去掉标题栏中动态片段（网速/时间/百分比）
        for p in cls._DYN_PAGE_PATTERNS:
            s = p.sub("", s)
        # 去掉 hash 后缀和残留括号
        s = re.sub(r"#[a-f0-9]{6,}", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        s = s.replace("「」", "").replace("「」", "")
        if "「" in s:
            s = s.split("「", 1)[0].strip()
        return s

    @staticmethod
    def _parse_action_signature(action: str) -> tuple[str, str, str]:
        t = str(action or "").strip()
        if not t:
            return "", "", ""
        action_type = "click_exact" if "click_exact" in t else "click"
        label = ""
        m = re.search(r'click(?:_exact)?\("([^"]+)"\)', t)
        if m:
            label = m.group(1)
        elif t.startswith("click(") and t.endswith(")"):
            label = t[6:-1]
        rid_tail = ""
        mr = re.search(r"rid=([^\s,]+)", t)
        if mr:
            rid_tail = mr.group(1).split("/")[-1].strip()
        return action_type, str(label or "").strip().lower(), rid_tail
