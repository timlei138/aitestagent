# RAG 知识架构优化计划（2026-07-10）

## 1) 现状

| 指标 | 优化前 | 优化后（当前） |
|------|:---:|:---:|
| com.zui.calculator 人工知识总数 | 101 条（83 auto + 16 dup + 2 hand） | **2 条**（全部手写） |
| auto_exact_click 自动生成 | ~85 条写入，每次点击都生成 | **已关闭写入 + 已物理删除** |
| 查询过滤 | 无，手写规则被淹没 | `query_curated_rules` 过滤 `auto_*` |
| 优先级顺序 | App 规则在前 | 通用知识（全局）> App 规则 |
| 去重策略 | 语义搜索 top_k=10（不可靠） | 精确匹配 limit=500 |
| 重复 | 大量 | 已全量去重 |

### 已完成（✓）

1. **P0 彻底关闭 auto_exact_click 写入** — `_maybe_promote_exact_rule` 在 `save_experience` 后直接 `return`，后续 ~80 行 curated 查询/冲突检测/`dedup_sig` 逻辑已全部移除
2. **P0.1 dedup_sig 未定义 bug** — 随 P0 一起消除（相关代码已删除）
3. **物理删除存量** — 16 条 `scenario=auto_exact_click` 已从 DB 删除
4. **查询过滤** — `query_curated_rules` 过滤 `scenario` 以 `auto_` 开头的条目
5. **优先级调整** — 通用知识排在 App 规则前面
6. **P2 去重修复** — `save_curated_rule` 从 `query(top_k=10)` 语义搜索改为 `get_by_metadata(limit=500)` 精确匹配
7. **前端 knowledge_type** — `App.vue` 编辑/保存已按 `row.metadata.knowledge_type` 处理，状态：**已修，待回归验证**

### 仍存在的问题（低优先级）

1. **P2 limit=500 边界风险** — `save_curated_rule` 目前用 `get_by_metadata(limit=500)` 全量扫描后精确比对。当 curated_rule 总量超过 500 条时可能漏重复。建议后续改成 metadata 内嵌 `dedupe_key`（与 experience 一致），从 ChromaDB 过滤层直接去重。

## 2) 知识优先级体系

```
人工全局（通用知识）    >  人工 App（App 操作前提）  >  操作经验
hand-written,            hand-written,              execution traces,
scope=universal          scope=app                  auto + tester
```

| 层级 | knowledge_type | 来源 | 写入方式 | 示例 |
|------|---------------|------|----------|------|
| 人工全局 | curated_rule, scope=universal | 测试人员手写 | 前端/API | "切换无限工作台需要打开快速设置" |
| 人工 App | curated_rule, scope=app | 测试人员手写 | 前端/API | "输入负数用减号键 op_sub，不用 +/−" |
| 操作经验 | experience | 执行自动记录 + 测试人员手动录入 | 自动/手动 | "Calculator → click(5) → Calculator" |

**Agent 自动生成的点击偏好不属于以上任何一级**，不应作为独立知识条目存在。点击偏好已通过 `save_experience` 保存，`_extract_click_preferences_from_rag` 在 RAG 查询时自动提取。

## 3) 修改记录

### P0 ✓：彻底关闭 _maybe_promote_exact_rule

**文件**：`tools/__init__.py:1180-1193`

**已实现**：在 `save_experience` 调用后直接结束函数，不再查询或写入 `curated_rule`。`dedup_sig` 未定义 bug 随代码删除自然消除。

### P1 ✓：全量删除 auto 规则脚本

**文件**：`scripts/cleanup_rag_quality.py`

**已实现**：`--purge-auto` 模式，全量删除 `scenario` 以 `auto_` 开头的 `curated_rule`。

```bash
# dry-run 预览
python scripts/cleanup_rag_quality.py --purge-auto

# 执行删除
python scripts/cleanup_rag_quality.py --purge-auto --apply
```

### P2 ✓：save_curated_rule 改为精确去重

**文件**：`data/knowledge.py:351-359`

**已实现**：从 `query(top_k=10)` 语义搜索改为 `get_by_metadata(limit=500)` 全量精确匹配。

**遗留风险**：`limit=500` 仍是硬上限。若 curated_rule 总条目超过 500，建议改成 metadata 内嵌 `dedupe_key` + ChromaDB 过滤层直接去重（与 experience 一致）。

### P3 待回归：前端字段链路确认

**文件**：`frontend/spa/src/App.vue` + `api/knowledge_routes.py`

**状态**：代码层面已修。需手动验证新增/编辑 curated_rule 时 `knowledge_type`、`scope`、`reviewed_by` 正确入库。

## 4) 验证方法

```bash
# 1. 确认人工知识无 auto_ 条目
python -c "
from data.knowledge import KnowledgeBase
from data import create_vector_store
from config import TestConfig
kb = KnowledgeBase(create_vector_store(TestConfig.from_yaml('config.yaml')))
result = kb.query_curated_rules('com.zui.calculator', top_k=30)
print(result)
# 预期：只有手写规则，含"输入负数时直接按减号键（op_sub）"
"

# 2. 全量清理 auto 规则（dry-run 先预览）
python scripts/cleanup_rag_quality.py --purge-auto
python scripts/cleanup_rag_quality.py --purge-auto --apply

# 3. 确认 save_curated_rule 去重有效
# 重复调用 save_curated_rule 同一内容 → count 不增长
```
