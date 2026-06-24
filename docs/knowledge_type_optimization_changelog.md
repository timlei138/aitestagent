# 知识库类型优化变更日志

> 执行日期：2026-06-24
> 对应计划：`docs/knowledge_type_optimization_plan.md`

## 变更概述

6 种 ChromaDB 知识类型 → 3 种：

| 新类型 | 合并旧类型 | 定位 |
|---|---|---|
| `experience` | page_structure + navigation_path + test_experience | 自动提取的操作经验 |
| `verified_plan` | verified_plan（不变） | 已验证成功的测试计划 |
| `curated_rule` | app_precondition + global_knowledge | 人工维护的领域知识 |

---

## 文件级变更

### 1. `data/vector_store.py`

| 位置 | 变更 |
|---|---|
| `VectorStoreBackend.search` | 参数类型 `dict[str, str]` → `dict[str, Any]` |
| `VectorStoreBackend.delete` | 参数类型 `dict[str, str]` → `dict[str, Any]` |
| `MemoryBackend` | **删除整个类** — 强制要求向量数据库，不再提供内存 fallback |
| `ChromaBackend._to_chroma_filter` | 重写：支持 `$or` 透传 + 混合条件自动包装 `$and` |
| `ChromaBackend.search` | 参数类型同步更新 |
| `ChromaBackend.delete` | 参数类型同步更新 |

**`_to_chroma_filter` 新逻辑：**
- 无复合操作符 → 1 key 直接返回，多 key 包 `$and`
- 有 `$or`/`$and` + 普通字段 → 自动拆分 plain + compound，包成 `$and`
- 例如 `{"app_package":"x", "$or":[...]}` → `{"$and":[{"app_package":"x"},{"$or":[...]}]}`

### 2. `data/knowledge.py`

| 位置 | 变更 |
|---|---|
| 类 docstring | 更新为"操作经验、验证计划、人工知识" |
| `_TYPE_ALIASES` | 新增类属性，维护 experience/curated_rule 的旧类型别名映射 |
| `query()` | `filter_dict` 类型 `dict[str, str]` → `dict[str, Any]`；knowledge_type 有别名时生成 `$or` 过滤 |
| `save_experience()` | **新增** — 统一记录页面操作结果（替代 save_navigation_path + save_test_experience + save_page_structure） |
| `query_experience()` | **新增** — 查询操作经验 |
| `save_curated_rule()` | **新增** — 保存人工知识（app_package 为空=全局，有值=App 特定）；按 `(app_package, content)` 组合判重，避免跨 App 误判 |
| `query_curated_rules()` | **新增** — 一次查询全部，Python 侧按 app_package 分组，避免跨 App 泄漏 |
| `extract_from_test_result()` | 重写：每步只写 1 条 experience（原来是 3 条不同类型），labels visited 去重保留 |
| `save_navigation_path()` | 改为 wrapper → `save_experience()` + DeprecationWarning |
| `save_test_experience()` | 改为 wrapper → `save_experience()` + DeprecationWarning |
| `save_page_structure()` | 改为 wrapper → `save_experience()` + DeprecationWarning |
| `save_precondition()` | 改为 wrapper → `save_curated_rule()` + DeprecationWarning |
| `save_global_knowledge()` | 改为 wrapper → `save_curated_rule(app_package="")` + DeprecationWarning |
| `query_navigation()` | 改为 wrapper → `query_experience()` + DeprecationWarning |
| `query_preconditions()` | 改为 wrapper → `query_curated_rules()` + DeprecationWarning |
| `query_global_knowledge()` | 改为 wrapper → `query_curated_rules("")` + DeprecationWarning |
| 旧 save_precondition/query_preconditions | 删除原始实现，仅保留 wrapper |
| 旧 save_global_knowledge/query_global_knowledge | 删除原始实现，仅保留 wrapper |

### 3. `agents/graph.py`

| 位置 | 变更 |
|---|---|
| `_rag_ctx()` | 4 次查询 → 3 次查询 |
| Prompt 标题 | `## 全局知识` + `## App 操作前提` → `## 人工知识`（内含分组） |
| Prompt 标题 | `## 导航经验` → `## 操作经验` |
| 查询方法 | `query_global_knowledge` + `query_preconditions` → `query_curated_rules` |
| 查询方法 | `query_navigation` → `query_experience` |

### 4. `tools/__init__.py`

| 位置 | 变更 |
|---|---|
| `query_app_knowledge` | 重写：改为查询 experience + curated_rule，输出分组显示 |
| `query_app_knowledge` docstring | 更新为 "Query operation experience and curated rules" |
| `_record_page_transition` | **修复 bug**：`via_action=` → `action=`，改用 `save_experience()` |

**Bug 修复说明：** 原代码 `kb.save_navigation_path(..., via_action=f"click({label})")` 参数名 `via_action` 与方法签名 `action` 不匹配，被 try/except 静默吞掉，导航路径从未成功写入。现已改用 `kb.save_experience(action=f"click({label})", ...)` 修复。

### 5. `api/knowledge_routes.py`

| 位置 | 变更 |
|---|---|
| `/types` 端点 | 6 种类型 → 3 种：experience / verified_plan / curated_rule |
| `element_identity` | 移除（存储在 SQLite，不在 ChromaDB 类型列表） |

### 6. `frontend/spa/src/App.vue`

| 位置 | 变更 |
|---|---|
| `kbForm` 默认类型 | `test_experience` → `experience` |
| `kbTypes` | 6 种 → 3 种 |
| `kbTypeColorMap` | 更新映射：experience=warning, verified_plan=danger, curated_rule=success |
| `_legacyTypeMap` | **新增** — 旧类型兼容映射，前端显示旧数据时自动解析为新类型 |
| `kbTypeLabel()` | 更新 — 先通过 `_legacyTypeMap` 解析旧类型再查找 |
| `kbTypeColor()` | 更新 — 先通过 `_legacyTypeMap` 解析旧类型再查找颜色 |
| `openKbDialog()` | 默认类型 `test_experience` → `experience` |
| `saveKb()` 校验 | `curated_rule` 类型允许 `app_package` 为空（全局知识） |
| `app_package` 输入框 | placeholder 动态化：`curated_rule` 时提示“留空表示全局知识” |

---

## 旧数据兼容策略

1. **ChromaDB 旧数据不删除**：旧类型记录（navigation_path / page_structure / test_experience / app_precondition / global_knowledge）保留在向量库中
2. **查询时自动兼容**：`query()` 中 `_TYPE_ALIASES` 将查询 `experience` 时自动扩展为 `$or` 匹配旧类型，查询 `curated_rule` 同理
3. **前端显示兼容**：`_legacyTypeMap` 将旧类型映射到新类型的颜色和标签
4. **旧 API 方法保留**：wrapper 方式转发到新方法，发出 DeprecationWarning

---

## Review 修复

### `data/__init__.py`

| 位置 | 变更 |
|---|---|
| `create_vector_store()` | 删除 MemoryBackend fallback，ChromaDB 初始化失败时直接 `raise RuntimeError` |
| 导入 | 移除 `MemoryBackend` |
| `__all__` | 移除 `MemoryBackend` |

### Review 问题修复汇总

| # | 问题 | 严重度 | 修复 |
|---|---|---|---|
| 1 | 前端 `saveKb()` 强制要求 app_package 非空，无法创建全局 curated_rule | 中 | curated_rule 时跳过校验 + 动态 placeholder |
| 2 | MemoryBackend 不支持 `$or` 过滤 | 低 | 删除整个 MemoryBackend，强制要求向量数据库 |
| 3 | `save_curated_rule` 去重跨 App 误判 | 低 | 改为 `(app_package, content)` 组合判重 |

---

## Bug 修复

| Bug | 修复 |
|---|---|
| `_record_page_transition` 调用 `save_navigation_path(..., via_action=...)` 参数名错误，导航路径从未成功写入 | 改用 `save_experience(action=...)`，参数名正确 |

---

## 验证结果

- 所有修改文件 import 正常（包括 MemoryBackend 删除后的依赖清理）
- `pytest tests/test_api_smoke.py` — 5 passed / 1 failed（失败项 `test_case_content_read` 为已有 bug，与本次修改无关）
