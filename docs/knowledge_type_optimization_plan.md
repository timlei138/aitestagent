# 知识库类型优化计划（已融合 Review 意见）

## Context

当前知识库有 **6 种 ChromaDB 类型**（不含 SQLite 的 `element_identity`）：

| 类型 | 写入方式 | 用于 Planner? | 用于 Agent 运行时? |
|---|---|---|---|
| `page_structure` | 自动提取 | 否 | 是 |
| `navigation_path` | 自动提取 | 是 (`_rag_ctx`) | 是 |
| `test_experience` | 自动提取 | 否 | 是 |
| `verified_plan` | 自动提取(成功时) | 是 (`_rag_ctx`) | 否 |
| `app_precondition` | 手动(API) | 是 (`_rag_ctx`) | 否 |
| `global_knowledge` | 手动(API) | 是 (`_rag_ctx`) | 是 |

**核心问题：**
- `page_structure` / `navigation_path` / `test_experience` 三者从同一次测试执行中提取同一事件的不同侧面，前两者从未被 Planner prompt 使用
- `app_precondition` 和 `global_knowledge` 结构完全相同仅 scope 不同
- **现有 bug：** `_record_page_transition()` 调用 `save_navigation_path(..., via_action=...)` 但方法签名参数名是 `action`，因 try/except 静默捕获，运行时导航路径从未成功写入

## 目标

6 → **3 种类型**，每种对应不同使用场景：

| 新类型 | 合并旧类型 | 定位 |
|---|---|---|
| `experience` | page_structure + navigation_path + test_experience | 自动提取的操作经验 |
| `verified_plan` | verified_plan（不变） | 已验证成功的测试计划 |
| `curated_rule` | app_precondition + global_knowledge | 人工维护的领域知识 |

---

## 实施步骤

### Step 1: `data/vector_store.py` — ChromaBackend 支持 `$or` 透传

```python
def _to_chroma_filter(self, filter: dict[str, Any]) -> dict[str, Any]:
    """将多 key 简单过滤转为 ChromaDB 兼容的 $and / $or 格式。

    关键：当 filter 同时含普通字段（如 app_package）和复合操作符（$or/$and）时，
    ChromaDB 要求必须用 $and 包裹，不能直接透传 flat dict。
    例如 {"app_package":"x", "$or":[...]} 必须转为 {"$and":[{"app_package":"x"},{"$or":[...]}]}
    """
    has_compound = "$or" in filter or "$and" in filter
    if not has_compound:
        if len(filter) <= 1:
            return filter
        return {"$and": [{k: v} for k, v in filter.items()]}

    # 有 $or/$and + 普通字段 → 需要包成 $and
    plain = {k: v for k, v in filter.items() if not k.startswith("$")}
    compound = {k: v for k, v in filter.items() if k.startswith("$")}
    parts = [{k: v} for k, v in plain.items()] + [{k: v} for k, v in compound.items()]
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}
```

---

### Step 2: `data/knowledge.py` — 核心改造

更新类 docstring 为：`"""RAG 知识库 —— 业务逻辑（操作经验、验证计划、人工知识）和底层存储解耦。"""`

#### 2.1 类型别名映射（旧数据兼容）

在 `KnowledgeBase.query()` 中维护别名，查询 `experience` 时自动通过 ChromaDB `$or` 同时匹配旧类型：

```python
_TYPE_ALIASES = {
    "experience": ["experience", "navigation_path", "page_structure", "test_experience"],
    "curated_rule": ["curated_rule", "app_precondition", "global_knowledge"],
}

def query(self, query: str, app_package: str = "", knowledge_type: str = "",
          top_k: int = 5) -> list[dict[str, Any]]:
    filter_dict: dict[str, Any] = {}  # 注意：Any 而非 str，因为 $or 值是 list[dict]
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

#### 2.2 新增 `save_experience()`

```python
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

    # 去重：用 content 文本直接比较，简单可靠（避免依赖 metadata 字段名差异）
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
```

**去重方案说明：** 直接用 `content` 文本判重，而非 `_dedup_key` 方法。原因：
- 旧类型记录（navigation_path 等）的 metadata 中没有 page/action/outcome 字段，用 metadata 组合判重会返回 `"||"` 导致误判
- content 文本比较更简单可靠，且和现有 `save_precondition` 的去重逻辑一致

#### 2.3 新增 `query_experience()`

```python
def query_experience(self, app_package: str, page: str, top_k: int = 5):
    return self.query(f"从 {page} 操作", app_package=app_package,
                     knowledge_type="experience", top_k=top_k)
```

#### 2.4 新增 `save_curated_rule()` + `query_curated_rules()`

```python
def save_curated_rule(self, app_package: str, content: str) -> None:
    """保存人工知识 —— app_package 为空表示全局，有值表示 App 特定。"""
    existing = self.query("", knowledge_type="curated_rule", top_k=10)
    if any(e.get("content", "") == content for e in existing):
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
```

**关键点：** 一次查询所有 `curated_rule`，Python 侧按 `metadata.app_package` 分组——`pkg=""` 归入全局，`pkg == app_package` 归入 App 特定，其他包名的规则不返回。避免两次查询中 `app_package=""` 的全局记录被第二次 `app_package=xxx` 过滤掉的问题，也避免全局查询泄漏其他 App 专属规则的问题。

#### 2.5 重写 `extract_from_test_result()`

每步只写 1 条 `experience`（原来是 3 条不同类型），保留 labels 的 visited 去重：

```python
def extract_from_test_result(self, app_package, test_case, execution_log, final_result):
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
```

#### 2.6 旧方法保留为 wrapper（直接转发 + DeprecationWarning）

```python
def save_navigation_path(self, app_package, from_page, to_page, action):
    import warnings
    warnings.warn("save_navigation_path is deprecated, use save_experience", DeprecationWarning)
    self.save_experience(app_package=app_package, page=from_page,
                        action=action, to_page=to_page, outcome="成功")

def save_test_experience(self, app_package, page, action, outcome, detail=""):
    import warnings
    warnings.warn("save_test_experience is deprecated, use save_experience", DeprecationWarning)
    self.save_experience(app_package=app_package, page=page, action=action,
                        outcome=outcome, detail=detail)

def save_page_structure(self, app_package, page_name, elements):
    import warnings
    warnings.warn("save_page_structure is deprecated, use save_experience", DeprecationWarning)
    labels = [e.get("label") or e.get("text") or e.get("content_desc") for e in elements[:30]]
    labels = [l for l in labels if l]
    self.save_experience(app_package=app_package, page=page_name, labels=labels)

def save_precondition(self, app_package, rule):
    import warnings
    warnings.warn("save_precondition is deprecated, use save_curated_rule", DeprecationWarning)
    self.save_curated_rule(app_package=app_package, content=rule)

def save_global_knowledge(self, content):
    import warnings
    warnings.warn("save_global_knowledge is deprecated, use save_curated_rule", DeprecationWarning)
    self.save_curated_rule(app_package="", content=content)

def query_navigation(self, app_package, page, top_k=5):
    import warnings
    warnings.warn("query_navigation is deprecated, use query_experience", DeprecationWarning)
    return self.query_experience(app_package, page, top_k=top_k)

def query_preconditions(self, app_package, top_k=3):
    import warnings
    warnings.warn("query_preconditions is deprecated, use query_curated_rules", DeprecationWarning)
    rules = self.query_curated_rules(app_package, top_k=top_k)
    return rules.replace("### 全局知识\n", "").replace("### App 操作前提\n", "").strip()

def query_global_knowledge(self, query="", top_k=5):
    import warnings
    warnings.warn("query_global_knowledge is deprecated, use query_curated_rules", DeprecationWarning)
    rules = self.query_curated_rules("", top_k=top_k)
    return rules.replace("### 全局知识\n", "").strip()
```

---

### Step 3: `agents/graph.py` — `_rag_ctx()` 简化为 3 次查询

```python
def _rag_ctx(kb, app_package: str, user_request: str = "") -> str:
    """查询 RAG 获取上下文：人工知识 + 验证计划 + 操作经验。"""
    if not kb:
        return ""
    parts = []
    # 1. 人工知识（一次查询，Python 侧自动分组为全局知识 + App 操作前提）
    rules = kb.query_curated_rules(app_package)
    if rules:
        parts.append("## 人工知识\n" + rules)
    # 2. 历史验证计划（不变）
    plans = kb.query_verified_plan(app_package, user_request, top_k=2)
    if plans:
        parts.append("## 历史验证计划\n" + "\n".join(f"- {p['content']}" for p in plans))
    # 3. 操作经验（替代原导航经验，覆盖更广）
    if user_request:
        exp = kb.query_experience(app_package, user_request[:50], top_k=3)
        if exp:
            parts.append("## 操作经验\n" + "\n".join(f"- {e['content']}" for e in exp))
    return "\n\n".join(parts)
```

---

### Step 4: `tools/__init__.py` — 更新工具调用

#### 4.1 `query_app_knowledge` 工具

```python
@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """Query operation experience and curated rules for the given app."""
    ctx = get_tool_context()
    if not ctx.knowledge_base:
        return "未启用知识库"
    package = app_package or ctx.device.current_app().get("package", "")

    exp_results = ctx.knowledge_base.query(query, app_package=package,
                                           knowledge_type="experience", top_k=5)
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

#### 4.2 `_record_page_transition` — 修复参数名 bug + 改用新方法

**现有 bug：** 调用 `save_navigation_path(..., via_action=...)`，但方法签名参数名是 `action`，被 try/except 静默吞掉，导航路径从未成功写入。

**修复后：**

```python
def _record_page_transition(ctx: Any, pre_page: str, label: str) -> None:
    """记录页面流转到知识库（异步，失败不阻塞）。"""
    if not pre_page:
        return
    try:
        post_page = _capture_page_id(ctx)
        if post_page and post_page != pre_page:
            kb = ctx.knowledge_base
            if kb:
                app_pkg = ctx.device.current_app().get("package", "")
                kb.save_experience(
                    app_package=app_pkg,
                    page=pre_page,
                    action=f"click({label})",
                    to_page=post_page,
                    outcome="成功",
                )
                logger.info("KB page transition: %s → %s (click %r)", pre_page, post_page, label)
    except Exception as exc:
        logger.debug("Page transition recording skipped: %s", exc)
```

---

### Step 5: `api/knowledge_routes.py` — `/types` 返回 3 种类型

```python
"types": [
    {"value": "experience", "label": "操作经验"},
    {"value": "verified_plan", "label": "验证计划"},
    {"value": "curated_rule", "label": "人工知识"},
],
```

注意：`element_identity` 存储在 SQLite，不在 ChromaDB 类型列表中，移除。

---

### Step 6: `frontend/spa/src/App.vue` — 更新类型 + 兼容旧数据

```javascript
// 新类型列表
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

// 旧类型兼容映射（前端显示用）
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
function kbTypeColor(type) {
  const resolved = _legacyTypeMap[type] || type;
  return kbTypeColorMap[resolved] || 'info';
}

// 默认表单类型
kbForm.value = { app_package: '', knowledge_type: 'experience', content: '' };
```

---

## Review 发现的问题及修复对照

| # | 问题 | 严重度 | 修复方案 |
|---|---|---|---|
| 1 | `query_curated_rules` 两次查询方案会泄漏其他 App 规则 | 高 | 改为一次查询 + Python 侧按 `metadata.app_package` 分组过滤 |
| 2 | `_record_page_transition` 参数名 `via_action` 与签名 `action` 不匹配 | 高 | 改用 `save_experience(action=...)`，修复现有 bug |
| 3 | `_dedup_key` 方法缺失 + 旧 metadata 无对应字段会误判 | 中 | 去掉 `_dedup_key`，直接用 `content` 文本判重 |
| 4 | `filter_dict` 类型注解 `dict[str, str]` 无法容纳 `$or: list[dict]` | 中 | 改为 `dict[str, Any]` |
| 5 | `_to_chroma_filter` 缺 `$or` 透传实现 + 混合条件 flat dict ChromaDB 不识别 | **高** | Step 1 补充完整逻辑：检测普通字段+复合操作符混合时自动包装 `$and` |
| 6 | 类 docstring 过时（旧类型名） | 低 | 更新为"操作经验、验证计划、人工知识" |

---

## 改动文件清单

| 文件 | 改动要点 |
|---|---|
| `data/vector_store.py` | `_to_chroma_filter` 支持 `$or` / `$and` 透传 |
| `data/knowledge.py` | 新增 4 方法 + 类型别名映射 + 重写 `extract_from_test_result` + 旧方法 wrapper + 更新 docstring + `filter_dict: dict[str, Any]` |
| `agents/graph.py` | `_rag_ctx()` 3 次查询 + 更新 Prompt 区域标题 |
| `tools/__init__.py` | `query_app_knowledge` 改用新方法 + `_record_page_transition` 修复参数名 bug |
| `api/knowledge_routes.py` | `/types` 返回 3 种类型 |
| `frontend/spa/src/App.vue` | 更新类型下拉 / 颜色 / 旧类型兼容映射 |

---

## 验证方式

1. 启动服务，`GET /api/knowledge/types` 返回 3 种类型
2. `POST /api/knowledge` 用新类型 `experience` / `curated_rule` 添加知识成功
3. 运行一次测试，查看日志确认 `_record_page_transition` 不再静默失败（应看到 `KB page transition` 日志）
4. 确认 `extract_from_test_result` 写入 `experience` 类型，每步 1 条（而非原来的 3 条）
5. 查看 Planner 日志，`_rag_ctx()` 输出含 `## 人工知识` / `## 历史验证计划` / `## 操作经验`
6. `query_app_knowledge` 工具返回结果分组显示操作经验和人工知识
7. 前端知识库页面能列出、搜索、筛选新旧数据（旧类型通过兼容映射正常显示颜色和标签）
8. 添加一条其他 App 的 curated_rule，查询当前 App 时确认不会泄漏
