from __future__ import annotations

import pytest

from data.knowledge import KnowledgeBase, UIKnowledge


class _FakeBackend:
    def __init__(self):
        self.records: list[dict] = []
        self._seq = 0

    def add(self, content: str, metadata: dict):
        self._seq += 1
        self.records.append(
            {"id": f"r{self._seq}", "content": content, "metadata": dict(metadata)}
        )

    def search(self, query: str, filter: dict | None = None, top_k: int = 5):
        del query, filter
        out = []
        for r in self.records[:top_k]:
            out.append(
                {"content": r["content"], "metadata": r["metadata"], "score": 0.0}
            )
        return out

    def delete(self, filter: dict):
        del filter
        return 0

    def count(self) -> int:
        return len(self.records)

    def get_by_metadata(self, where: dict, limit: int = 50):
        def _matches(metadata: dict, cond: dict) -> bool:
            for key, value in cond.items():
                if key == "$or":
                    if not any(_matches(metadata, c) for c in value):
                        return False
                    continue
                if key == "$and":
                    if not all(_matches(metadata, c) for c in value):
                        return False
                    continue
                if metadata.get(key, "") != value:
                    return False
            return True

        out = []
        for r in self.records:
            if _matches(r["metadata"], where):
                out.append(
                    {
                        "id": r.get("id", ""),
                        "content": r["content"],
                        "metadata": r["metadata"],
                        "score": 1.0,
                    }
                )
            if len(out) >= limit:
                break
        return out

    def delete_by_ids(self, ids: list[str]) -> int:
        before = len(self.records)
        id_set = set(ids or [])
        self.records = [r for r in self.records if r.get("id", "") not in id_set]
        return before - len(self.records)


def _seed_rules(kb: KnowledgeBase):
    kb.save_curated_rule("com.zui.gallery", "图库规则：选择后点完成")
    kb.save_curated_rule("com.zui.calculator", "计算器规则：长按结果可复制")
    kb.save_curated_rule("com.zui.launcher", "桌面模式判断规则")
    kb.save_curated_rule(
        "",
        "通用规则：先等待页面稳定",
        scope="universal",
        reviewed_by="qa",
    )


def test_gallery_only_returns_universal_and_gallery():
    kb = KnowledgeBase(_FakeBackend())
    _seed_rules(kb)
    rules = kb.query_curated_rules("com.zui.gallery")
    assert "通用规则" in rules
    assert "图库规则" in rules
    assert "计算器规则" not in rules
    assert "桌面模式" not in rules


def test_calculator_only_returns_universal_and_calculator():
    kb = KnowledgeBase(_FakeBackend())
    _seed_rules(kb)
    rules = kb.query_curated_rules("com.zui.calculator")
    assert "通用规则" in rules
    assert "计算器规则" in rules
    assert "图库规则" not in rules


def test_no_universal_does_not_break_app_rules():
    kb = KnowledgeBase(_FakeBackend())
    kb.save_curated_rule("com.zui.gallery", "图库规则：多选入口")
    rules = kb.query_curated_rules("com.zui.gallery")
    assert "### App 操作前提" in rules
    assert "图库规则" in rules
    assert "### 通用知识" not in rules


def test_save_global_without_universal_scope_raises():
    kb = KnowledgeBase(_FakeBackend())
    with pytest.raises(ValueError):
        kb.save_curated_rule("", "some rule", scope="app")


def test_save_universal_without_reviewer_raises():
    kb = KnowledgeBase(_FakeBackend())
    with pytest.raises(ValueError):
        kb.save_curated_rule("", "some rule", scope="universal", reviewed_by="")


def test_contamination_sentinel():
    kb = KnowledgeBase(_FakeBackend())
    _seed_rules(kb)
    app_forbidden = {
        "com.zui.gallery": ("计算器规则", "桌面模式"),
        "com.zui.calculator": ("图库规则", "桌面模式"),
    }
    for app_package, forbidden_words in app_forbidden.items():
        rules = kb.query_curated_rules(app_package)
        for word in forbidden_words:
            assert word not in rules, f"CONTAMINATION: {app_package} contains {word}"


def test_save_knowledge_auto_sets_universal_scope_for_global_curated_rule():
    backend = _FakeBackend()
    kb = KnowledgeBase(backend)
    kb.save_knowledge(
        UIKnowledge(
            app_package="",
            knowledge_type="curated_rule",
            content="全局入口规则",
            metadata={},
        )
    )
    assert backend.records[0]["metadata"].get("scope") == "universal"


def test_query_curated_rules_excludes_global_rules_without_scope():
    backend = _FakeBackend()
    kb = KnowledgeBase(backend)
    backend.add(
        "缺少scope的全局规则",
        {
            "app_package": "",
            "knowledge_type": "curated_rule",
            # scope 故意缺失
            "timestamp": "2026-01-01T00:00:00",
        },
    )
    out = kb.query_curated_rules("com.zui.launcher")
    assert "缺少scope的全局规则" not in out


def test_save_curated_rule_drops_empty_applicable_domains():
    backend = _FakeBackend()
    kb = KnowledgeBase(backend)
    kb.save_curated_rule(
        "",
        "全局规则-empty-domains",
        scope="universal",
        reviewed_by="qa",
        applicable_domains=[],
    )
    assert "applicable_domains" not in backend.records[0]["metadata"]


def test_save_experience_normalizes_dynamic_page_tokens_and_dedups():
    backend = _FakeBackend()
    kb = KnowledgeBase(backend)
    kb.save_experience(
        app_package="com.zui.launcher",
        page="MainActivity「0.12\nK/s」",
        action='click_exact("应用列表") rid=com.zui.launcher:id/taskbar_view',
        to_page="MainActivity「0.00\nK/s」",
        outcome="成功",
        signal_type="exact_click",
        quality_score=0.9,
    )
    kb.save_experience(
        app_package="com.zui.launcher",
        page="MainActivity「0.20\nK/s」",
        action='click_exact("应用列表") rid=com.zui.launcher:id/taskbar_view',
        to_page="MainActivity「0.03\nK/s」",
        outcome="成功",
        signal_type="exact_click",
        quality_score=0.95,
    )
    rows = backend.get_by_metadata(
        {"app_package": "com.zui.launcher", "knowledge_type": "experience"},
        limit=10,
    )
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    assert meta.get("page_norm") == "MainActivity"
    assert int(meta.get("success_count", 0) or 0) >= 2


def test_query_experience_only_returns_strong_quality():
    backend = _FakeBackend()
    kb = KnowledgeBase(backend)
    kb.save_experience(
        app_package="com.zui.launcher",
        page="MainActivity",
        action='click_exact("应用列表") rid=app_list',
        to_page="CustomModeLauncher",
        outcome="成功",
        signal_type="exact_click",
        quality_score=0.9,
    )
    kb.save_experience(
        app_package="com.zui.launcher",
        page="MainActivity",
        action='click("热门词")',
        to_page="MainActivity",
        outcome="成功",
        signal_type="fallback_click",
        quality_score=0.3,
    )
    out = kb.query_experience("com.zui.launcher", user_request="", top_k=10)
    assert len(out) == 1
    assert "应用列表" in out[0]["content"]
