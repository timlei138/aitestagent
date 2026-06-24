# 知识库类型优化计划 — 评审意见

## 总体评价

计划方向正确，6→3 的合并策略清晰，每种新类型的使用场景划分合理。以下是针对代码实现细节的具体建议。

---

## 1. 类型别名映射：直接用 ChromaDB `$or` 过滤

**计划描述：** 查询 `experience` 时自动同时匹配旧类型 `navigation_path` / `page_structure` / `test_experience`。

**问题：** 当前 `KnowledgeBase.query()` 直接用 `{"knowledge_type": knowledge_type}` 做精确过滤（见 `data/knowledge.py:43`），ChromaDB 的 `_to_chroma_filter` 只处理了 `$and`（见 `data/vector_store.py:128-132`）。

**建议实现方式：** 不考虑 MemoryBackend，直接在 `ChromaBackend` 中支持 `$or` 过滤，`KnowledgeBase.query()` 维护别名映射，查询时展开为 `$or` 条件，一次查询搞定：

```python
# ChromaBackend 扩展 _to_chroma_filter，支持 $or
def _to_chroma_filter(self, filter: dict[str, Any]) -> dict[str, Any]:
    """支持 $or / $and 复合过滤。"""
    if "$or" in filter or "$and" in filter:
        return filter  # 已经是复合条件
    if len(filter) <= 1:
        return filter
    return {"$and": [{k: v} for k, v in filter.items()]}

# KnowledgeBase 层维护别名映射
_TYPE_ALIASES = {
    "experience": ["experience", "navigation_path", "page_structure", "test_experience"],
    "curated_rule": ["curated_rule", "app_precondition", "global_knowledge"],
}

def query(self, query, app_package="", knowledge_type="", top_k=5):
    filter_dict = {}
    if app_package:
        filter_dict["app_package"] = app_package
    if knowledge_type:
        aliases = _TYPE_ALIASES.get(knowledge_type, [knowledge_type])
        if len(aliases) == 1:
            filter_dict["knowledge_type"] = aliases[0]
        else:
            filter_dict["$or"] = [{"knowledge_type": a} for a in aliases]
    return self.backend.search(query, filter_dict if filter_dict else None, top_k)
```

**优势：** 单次向量查询 + 过滤，比多次查询合并更高效，且代码更简洁。

---

## 2. `query_curated_rules` 的全局 + App 特定合并查询

**计划描述：** `query_curated_rules(app_package)` 一次返回全局知识 + App 特定规则。

**问题：** 当前 `save_global_knowledge` 的 `app_package=""` 而 `save_precondition` 的 `app_package` 有值。ChromaDB 过滤 `app_package=xxx` 不会匹配 `app_package=""` 的记录，反之亦然。

**建议：** 需要两次查询合并：

```python
def query_curated_rules(self, app_package: str, top_k: int = 5) -> str:
    """查询人工知识：全局知识 + App 特定规则，合并返回。"""
    # 全局知识（app_package=""）
    global_results = self.query("", knowledge_type="curated_rule", top_k=top_k)
    # App 特定规则
    app_results = []
    if app_package:
        app_results = self.query("", app_package=app_package, 
                                knowledge_type="curated_rule", top_k=top_k)
    # 合并去重
    all_results = []
    seen = set()
    for r in global_results + app_results:
        if r["content"] not in seen:
            seen.add(r["content"])
            all_results.append(r)
    if not all_results:
        return ""
    # 分组输出，便于 Prompt 区分
    global_lines = [f"- {r['content']}" for r in global_results if r["content"] in seen]
    app_lines = [f"- {r['content']}" for r in app_results if r["content"] in seen]
    parts = []
    if global_lines:
        parts.append("### 全局知识\n" + "\n".join(global_lines))
    if app_lines:
        parts.append("### App 操作前提\n" + "\n".join(app_lines))
    return "\n\n".join(parts)
```

这样 `_rag_ctx()` 中一次调用就能拿到分好组的人工知识文本。

---

## 3. `_record_page_transition` 的 `save_experience` 调用格式

**当前代码（`tools/__init__.py:745-763`）：** 记录页面跳转时只有 `pre_page`、`post_page`、`label` 三个信息。

**问题：** `save_experience` 的内容格式比 `save_navigation_path` 更丰富（含 labels、outcome），但 `_record_page_transition` 运行时只有导航信息，没有完整的页面元素列表和操作结果。

**建议：** 在 `_record_page_transition` 中使用简化的 experience 格式即可：

```python
kb.save_experience(
    app_package=app_pkg,
    page=pre_page,
    action=f"click({label})",
    to_page=post_page,
    outcome="成功",
    labels="",  # 运行时没有完整的 labels 列表
)
```

对应 `save_experience` 内部逻辑：有 `to_page` 时生成 `"在 {page} 通过 '{action}' 到达 {to_page}，结果: {outcome}"`，无 `labels` 时省略 `包含:` 部分。

**注意：** 这里写入的 experience 和 `extract_from_test_result` 写入的 experience 可能会有重复（同一次跳转被记录两次）。建议在 `save_experience` 中加入去重逻辑（类似 `save_precondition` 的做法），用 content 哈希或 `(page, action, to_page)` 组合判重。

---

## 4. `extract_from_test_result` 重写细节

**当前逻辑（`data/knowledge.py:94-126`）：** 每步写 3 条（navigation_path + page_structure + test_experience）。

**建议的新逻辑：**

```python
def extract_from_test_result(self, app_package, test_case, execution_log, final_result):
    count = 0
    visited_pages = set()
    for entry in execution_log:
        page = entry.get("page", "") or "未知页面"
        action = entry.get("action", "?")
        observation = entry.get("observation", "")
        step_ok = entry.get("result") == "success"
        to_page = entry.get("post_page", "")
        
        # 动态构建内容
        labels = []
        if page not in visited_pages:
            visited_pages.add(page)
            labels = _extract_labels_from_observation(observation)
        
        outcome = "成功" if step_ok else "失败"
        detail = observation or entry.get("error", "")
        
        self.save_experience(
            app_package=app_package,
            page=page,
            action=action,
            to_page=to_page if (to_page and to_page != page) else "",
            outcome=outcome,
            detail=detail if not step_ok else "",
            labels=labels,
        )
        count += 1
    return count
```

**关键点：** `page_structure` 的 labels 提取逻辑应保留，但作为 experience 的一部分写入，而非独立条目。`visited` 去重逻辑也应保留（同一页面不重复提取 labels）。

---

## 5. `save_experience` 方法签名建议

```python
def save_experience(self, app_package: str, page: str, action: str = "",
                    to_page: str = "", outcome: str = "",
                    detail: str = "", labels: list[str] | None = None) -> None:
    """保存操作经验 —— 统一记录页面操作的结果。"""
    # 去重：检查是否已有相似记录
    query_text = f"{page} {action}"
    existing = self.query(query_text, app_package=app_package,
                         knowledge_type="experience", top_k=3)
    # 简单去重：相同 page + action + outcome 视为重复
    dedup_key = f"{page}|{action}|{outcome}"
    if any(self._dedup_key(e) == dedup_key for e in existing):
        return
    
    # 动态构建 content
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
    
    content = "，".join(parts) if len(parts) <= 2 else parts[0] + "，" + "，".join(parts[1:])
    
    self.save_knowledge(UIKnowledge(
        app_package=app_package,
        knowledge_type="experience",
        content=content,
        metadata={
            "page": page,
            "action": action,
            "to_page": to_page,
            "outcome": outcome,
            "timestamp": datetime.now().isoformat(),
        },
    ))

def _dedup_key(self, result: dict) -> str:
    meta = result.get("metadata", {})
    return f"{meta.get('page','')}|{meta.get('action','')}|{meta.get('outcome','')}"
```

---

## 6. `query_app_knowledge` 工具更新

**当前代码（`tools/__init__.py:462-476`）：** 查询时不带类型过滤 + 额外查 global_knowledge。

**建议更新为：**

```python
@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """Query operation experience and curated rules for the given app."""
    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = app_package or ctx.device.current_app().get("package", "")
    
    # 查询操作经验（自动兼容旧类型）
    exp_results = ctx.knowledge_base.query(query, app_package=package, 
                                           knowledge_type="experience", top_k=5)
    # 查询人工知识（含全局 + App 特定，自动兼容旧类型）
    rule_text = ctx.knowledge_base.query_curated_rules(package, top_k=3)
    
    parts = []
    if exp_results:
        parts.append("## 操作经验")
        parts.extend(f"- {r['content']}" for r in exp_results)
    if rule_text:
        parts.append("## 人工知识")
        parts.append(rule_text)
    
    return "\n".join(parts) if parts else f"未找到 '{query}' 的相关知识"
```

---

## 7. 前端默认类型需更新

**当前代码（`frontend/spa/src/App.vue:647`）：**
```javascript
const kbForm = ref({ app_package: '', knowledge_type: 'test_experience', content: '' });
```

`test_experience` 将不再存在，需改为 `experience`。

同时 `kbTypes` 和 `kbTypeColorMap` 更新为：

```javascript
const kbTypes = [
  { value: 'experience', label: '操作经验' },
  { value: 'verified_plan', label: '验证计划' },
  { value: 'curated_rule', label: '人工知识' },
];
const kbTypeColorMap = {
  experience: 'warning',
  verified_plan: 'danger',
  curated_rule: 'success',
};
```

**注意：** 旧数据在前端列表中仍能查到（通过别名映射），但 `kbTypeLabel` / `kbTypeColor` 函数需要能处理旧类型值。建议增加兼容映射：

```javascript
const _legacyTypeMap = {
  page_structure: 'experience',
  navigation_path: 'experience',
  test_experience: 'experience',
  app_precondition: 'curated_rule',
  global_knowledge: 'curated_rule',
};
function kbTypeLabel(type) {
  const resolved = _legacyTypeMap[type] || type;
  const t = kbTypes.find(x => x.value === resolved);
  return t ? t.label : (type || '未知');
}
```

---

## 8. `_rag_ctx` 中的 Prompt 区域标题

计划中提到 Prompt 应包含 `## 人工知识` / `## 历史验证计划` / `## 操作经验` 三个区域。建议更新后的 `_rag_ctx`：

```python
def _rag_ctx(kb, app_package: str, user_request: str = "") -> str:
    if not kb:
        return ""
    parts = []
    # 1. 人工知识（含全局 + App 特定）
    rules = kb.query_curated_rules(app_package)
    if rules:
        parts.append("## 人工知识\n" + rules)
    # 2. 历史验证计划
    plans = kb.query_verified_plan(app_package, user_request, top_k=2)
    if plans:
        parts.append("## 历史验证计划\n" + "\n".join(f"- {p['content']}" for p in plans))
    # 3. 操作经验（替代原导航经验）
    if user_request:
        exp = kb.query_experience(app_package, user_request[:50], top_k=3)
        if exp:
            parts.append("## 操作经验\n" + "\n".join(f"- {e['content']}" for e in exp))
    return "\n\n".join(parts)
```

---

## 9. 其他注意事项

### 9.1 `element_identity` 类型

计划中 `/types` API 原来返回 6 种类型（含 `element_identity`），但 `element_identity` 实际存储在 SQLite 而非 ChromaDB。新 `/types` 返回 3 种时不应包含 `element_identity`。当前前端 `kbTypes` 列表中也没有它，保持一致即可。

### 9.2 旧方法保留为 wrapper 的开销

计划说"旧方法保留为兼容 wrapper"。建议 wrapper 直接调用新方法而非复制逻辑：

```python
def save_navigation_path(self, app_package, from_page, to_page, action):
    import warnings
    warnings.warn("save_navigation_path is deprecated, use save_experience", DeprecationWarning)
    self.save_experience(app_package=app_package, page=from_page, 
                        action=action, to_page=to_page, outcome="成功")
```

### 9.3 `query_experience` 方法

```python
def query_experience(self, app_package: str, page: str, top_k: int = 5) -> list[dict]:
    return self.query(f"从 {page} 操作", app_package=app_package,
                     knowledge_type="experience", top_k=top_k)
```

### 9.4 数据迁移（可选）

如果希望彻底消除旧类型，可以提供一个一次性迁移脚本，将 ChromaDB 中旧类型记录的 `knowledge_type` 字段更新为新值。但这不是必须的——别名映射已经保证了兼容性。

---

## 10. 建议的实施顺序

1. **`data/knowledge.py`** — 新增 4 个方法 + 类型别名 + 重写 `extract_from_test_result`（核心，其他文件依赖）
2. **`agents/graph.py`** — 更新 `_rag_ctx()`（依赖 Step 1 的新方法）
3. **`tools/__init__.py`** — 更新 `query_app_knowledge` + `_record_page_transition`（依赖 Step 1）
4. **`api/knowledge_routes.py`** — 更新 `/types`（独立改动）
5. **`frontend/spa/src/App.vue`** — 更新类型列表 + 兼容映射（依赖 Step 4 的 API 变更）

每个 Step 完成后建议运行一次现有测试（`pytest tests/`）确保不破坏现有功能。

---

## 总结

计划整体可行，主要补充建议：

| # | 要点 | 优先级 |
|---|---|---|
| 1 | 别名映射直接用 ChromaDB `$or` 过滤，一次查询 | 高 |
| 2 | `query_curated_rules` 需两次查询（全局+App）合并 | 高 |
| 3 | `_record_page_transition` 用简化 experience 格式 | 高 |
| 4 | `save_experience` 需去重逻辑 | 中 |
| 5 | 前端 `kbTypeLabel` 需兼容旧类型显示 | 中 |
| 6 | 旧方法 wrapper 直接转发 + DeprecationWarning | 低 |
