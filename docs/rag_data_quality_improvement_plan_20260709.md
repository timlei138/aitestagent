# RAG 数据质量优化计划（2026-07-09）

## 1) 现状（结合最新 run + 当前代码）

85 条数据中存在三类质量问题：

| 问题 | 数量 | 影响 |
|------|:---:|------|
| auto_exact_click 逐按钮生成 curated_rule | 14 条（含重复） | top_k 被挤占，通用规则被挤出 |
| Experience 同页同按钮膨胀 | ~45 条 | 每条仅 page hash 不同，LLM 无可用信息 |
| 跨场景噪声 | 每次查询 | calculator 测试收到无限工作台规则 |

补充观察（`logs/runs/175128_test-20260709_175040_resume_langchain.log` + 代码链路）：

- `query_app_knowledge` 在该 run 中出现频次偏高（关键字出现 35 次），知识查询存在过度调用/重复注入迹象。
- `tools.query_app_knowledge` 当前调用 `kb.query(..., knowledge_type="experience")`，绕过了 `query_experience` 的质量过滤与同 App 优先策略。
- `query_curated_rules` 当前是「universal + app」双路拼接，且 universal 段先输出，容易在上下文前部形成噪声。
- `save_experience` 去重前扫描 limit=80 的历史窗口，规模增长后存在"窗口外重复漏检"风险。

## 2) 目标

1. Curated rules 按场景聚合，不逐按钮生成
2. Experience 按 activity 去重，不因 hash 变化重复写入
3. App 查询时优先返回 App 规则，universal 规则降权

## 3) 修改点

### P0：统一查询入口 + 缓存（先修调用链）

**文件**：`tools/__init__.py` → `query_app_knowledge`

**问题**：
- 当前对 experience 直接走 `kb.query`（语义搜索），与 `_rag_ctx` 使用的 `kb.query_experience`（质量优先）策略不一致；
- `query_experience` 按 quality_score 排序不按 query 语义排序，直接替换会损失语义相关性；
- 同轮重复调用（35 次/run）无缓存。

**修改**：

**并行召回 + 合并 rerank**：

```python
# Layer 1: 并行召回
strong = kb.query_experience(package, query, top_k=10)  # 质量优先
semantic = kb.query(query, app_package=package, knowledge_type="experience", top_k=5)  # 语义优先

# Layer 2: 合并去重（id 优先，其次 sha1(normalized_content)）
seen = set()
merged = []
for r in strong + semantic:
    rid_val = str(r.get("id", "") or "")
    if rid_val:
        key = rid_val
    else:
        key = hashlib.sha1(
            str(r.get("content", "") or "").strip().lower().encode("utf-8")
        ).hexdigest()[:12]
    if key not in seen:
        seen.add(key)
        merged.append(r)

# Layer 3: 语义 rerank
merged.sort(key=lambda r: _experience_relevance(r, query), reverse=True)
merged = merged[:5]
```

**查询去重缓存**：

- 缓存完整键 = `(run_id, app_package, query_norm, page_signature_hash)`，存于 `ctx._rag_query_cache`
- 同 run 同页同 query 命中缓存直接返回；每次 run 开始时在 `planner_node` 显式清空，跨 run 不复用

### P0：curated_rule 聚合 + 去重

**文件**：`tools/__init__.py` → `_maybe_promote_exact_rule`

**改前**：每次 exact click 成功就生成一条 `在{page}点击"{label}"时，优先匹配...` 的 curated_rule。

**改后**：同 `(app_package, class_name, path_contains)` 的多个成功点击合并为一条通用规则：

```
在Calculator点击数字或运算符时，优先匹配 class=button 且 path=content > root_layout > content_layout > pad_layout，并用 rid 区分具体按钮
```

聚合条件：
- 同一 app_package
- 同一 path_contains（如 `content > root_layout > content_layout > pad_layout`）
- 同一 class_name（如 `button`）
- 至少 2 个不同 label 的成功点击满足以上条件 → 合并为一条通用规则
- 已有通用规则时不再逐按钮新增

去重条件：
- 写入前检查 `query_curated_rules(app_package)` 是否已有同 path + 同 class 的规则
- 有则仅更新 `last_verified_at`，不新增

### P1：Experience 按 activity 去重

**文件**：`data/knowledge.py` → `save_experience`

**问题**：`_normalize_page_id` 去掉了动态 token（时间/网速），但 hash 仍让每条"唯一"。`Calculator「17:48」#abc` 和 `Calculator「17:49」#def` 是同页不同 hash，去重逻辑没有识别。

**修改**：`_normalize_page_id` 在去掉 `「xxx」#hash` 时保留到 activity 级别——`Calculator「17:48」#abc` → `Calculator`。`save_experience` 去重时用 `(app_package, page_activity, action_label_norm, rid_tail)` 四元组，page 不再包含 hash。

另外补充一条工程化约束（防窗口漏检）：

- 当前去重扫描窗口为 `limit=80`，规模增长后会漏检旧重复；
- 增加稳定去重键 `dedupe_key = sha1(app|page_norm|action_label_norm|rid_tail)` 写入 metadata；
- 去重优先按 `dedupe_key` 精确查找，扫描窗口仅作为兼容 fallback。

### P1：App 查询时 universal 降权

**文件**：`data/knowledge.py` → `query_curated_rules`

**改前**：`app_package` 非空时，先查 app_rules（top_k），再查 universal_rules（top_k），两者平等拼接。

**改后**：app_rules 占 top_k 的 80%（至少 1 条），universal 占 20%。`top_k=5` 时，app 规则 4 条 + universal 1 条（仅当有剩余名额时）。

并调整输出顺序：

1. 先 `### App 操作前提`
2. 后 `### 通用知识`

避免通用规则在 prompt 前部抢占注意力。

### P2：存量清理

清理脚本（`scripts/cleanup_rag_quality.py`）：

```python
# 1. 合并 calculator auto-rules（14条 → 1条通用规则）
# 2. 删除重复的 curated_rule（同 app + 同 path + 同 class）
# 3. 删除 experience 中 page 含 hash 的旧格式条目
# 4. 生成清理报告
```

### P2：RAG 观测闭环（避免"清理后回退"）

新增指标并在报告中保留：

- `rag_query_count`：每 run 主动/被动知识查询次数；
- `rag_same_app_ratio`：same_app 结果占比；
- `rag_empty_hit_rate`：查询无结果占比；
- `rag_cross_app_used_count`：跨 app 结果被注入次数。

用于判断"是数据质量问题还是查询策略问题"，避免只靠主观观感调参。

## 5) 存储侧优化（写的时候少写无用的）

### 5.1 Experience 不存页面 hash

**文件**：`tools/__init__.py` → `_record_page_transition` / `save_experience`

**问题**：当前 experience content = `Calculator「17:48」#36c49e58eb18 → click_exact("+") → Calculator「17:48」#dd5ecc`。hash 让每条"唯一"但语义上全是同一页，向量搜索时 hash 是噪声。

**修改**：content 用 `_normalize_page_id` 后的值（`Calculator`），hash 仅存 metadata 供追踪。效果：

```
改前：Calculator「17:48」#abc → click_exact("+") → Calculator「17:48」#def
改后：Calculator → click_exact("+") → Calculator   （hash 移到 metadata.page_hash）
```

### 5.2 不存同页同按钮的重复 experience

**文件**：`tools/__init__.py` → `_record_page_transition`

**问题**：计算器每按一次数字键就写一条 experience，这些"某页点 digit_5"的信息 LLM 不需要——它可以在 page_info 里直接看到按钮。

**规则**：`_record_page_transition` 写入前加判断——如果 `pre_page` 和 `post_page` 是同一 activity（如都是 Calculator），且 action 是精确点击（exact_click），则**不写 experience**。仅跨页导航（如 CustomModeLauncher → Calculator）才写入。

```
写入 experience：跨页导航（页面切换了）
写入 experience：首次在页面上发现关键操作（通过 success_count 判断）
不写 experience：同页重复按钮（计算器按键、表单连续输入）
```

### 5.3 Experience content 精简

**当前长度**：~150 chars（含完整 rid + path + hash）
**目标长度**：~60 chars

```
改前：Calculator「17:48」#hash → click_exact("×") rid=com.zui.calculator:id/op_mul class=button path=content > root_layout > content_layout > pad_layout → Calculator「17:48」#hash2

改后：Calculator → click(×) rid=op_mul
```

`rid` 只保留尾部（`op_mul` 而非全路径），`path` 移到 metadata，`class` 移到 metadata。向量搜索靠 label + rid_tail 已经够区分。

---

## 6) 验收标准

- Calculator 查询返回的 curated_rules 中，通用规则 ≤ 3 条（含合并后的按钮规则）
- Experience 中同 `(app, page_activity, action_label, rid_tail)` 的组合 ≤ 1 条
- Calculator 测试时 RAG 注入不再出现无限工作台规则
- 关键场景通过率不下降
- `query_app_knowledge` 与 `_rag_ctx` 采用一致的 Hybrid 检索口径（质量召回 + 语义召回 + 合并 rerank）
- `rag_same_app_ratio >= 0.8`，`rag_cross_app_used_count` 持续下降
- `rag_query_count` 从 35 次/run 降到 ≤ 5 次/run（缓存命中 + 并行召回去重）

## 7) 风险点与对应措施（上线可执行版）

### 7.1 召回覆盖下降（降噪过强）

**风险**：去重/降权后，长尾但有用经验被压掉，导致任务通过率下降。  
**措施**：

1. 灰度发布：Hybrid 检索仅在 10% run 启用，观察 2 天；
2. 保护阈值：若 `pass_rate` 相比基线下降 > 2%，自动回滚到上一个稳定策略；
3. 兜底补召回（长期保留，非仅灰度期间）：Hybrid 合并后若有效结果 `<2` 条，补一次 `kb.query(..., top_k=3)`。

### 7.2 缓存误命中（跨页/跨轮复用错误）

**风险**：命中旧页面缓存，返回过期知识。  
**措施**：

1. 缓存键固定为 `(run_id, app_package, query_norm, page_signature_hash)`；
2. 失效策略：
   - 页面签名变化立即失效；
   - app 切换立即清空；
   - run 结束清空；
3. TTL：同 key 在同一 `agent_node` 调用内有效；跨 tool call 可共享，`route_after_agent` 触发下一轮时失效。

### 7.3 rerank 偏置（相关性打分不稳）

**风险**：打分偏向常见短词，导致 top5 相关性波动。  
**措施**：

1. 可解释打分：记录 `relevance_score` 子项（label/rid/quality）；
2. 权重初值：`final = 0.6 * relevance + 0.4 * quality`，按 A/B 数据调整；
3. 准入门槛：`relevance_score < 0.2` 的结果不进入 top5；若过滤后不足 2 条，放宽阈值为 0（全量保留）。

### 7.4 存量清理误删

**风险**：清理脚本删除了仍有用的旧知识。  
**措施**：

1. 先 dry-run 输出拟删除清单，不直接写库；
2. 执行前备份向量库与元数据库（含时间戳）；
3. 按 app 分批清理，每批后做 spot check（至少抽样 20 条）。

### 7.5 指标阈值过严（例如 `rag_query_count <= 5`）

**风险**：复杂任务被硬阈值误判。  
**措施**：

1. 先使用统一宽松阈值 `<= 8/run`（简单/复杂任务统一门槛），等数据积累后视 P50/P90 分布再分层；
2. 同时观察 P50/P90，不只看均值；
3. 连续 3 天达标后再收紧阈值。

### 7.6 回滚策略（统一）

任一条件触发时，由配置/编排层自动切回上一个稳定策略（可能是旧 Hybrid 参数或旧查询链），`reporter_node` 负责检测阈值并上报触发信号：

- `pass_rate` 较基线下降 > 2%（连续 50 run）
- `rag_empty_hit_rate` 升高 > 10%（连续 2 天）
- 关键场景（Calculator）失败率上升 > 5%
