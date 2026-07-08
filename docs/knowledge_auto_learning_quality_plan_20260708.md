# 知识库自动学习质量优化计划（2026-07-08）

## 1. 目标

减少“低价值自动经验”进入 RAG，保留对 Agent 真正有帮助的高价值知识，避免提示噪声干扰决策。

---

## 2. 现状问题（结合当前代码）

### 2.1 自动经验写入过宽且缺上下文

当前 `tools/__init__.py::_record_page_transition(ctx, pre_page, label)` 只拿到 `label`，但质量评分需要 `exact_mode/index/rid/class/path/strategy` 等上下文。  
现状是只要页面变化就写 `experience`：

- `action=click(label)` 语义弱（缺少 class/path/rid 等区分）
- 页面标识常含波动信息（如网速/时间），可读性与可复用性差
- 成功一次即入库，缺少质量门槛

### 2.2 经验查询缺少质量分层

当前 `data/knowledge.py::query_experience` 主要按 app 与语义相似度查询：

- 没有对“信号强度”排序（精确点击 vs 模糊点击）
- 没有对噪声模式降权（动态 title、泛化 rid）
- 容易把“看起来成功但价值低”的样本注入到 RAG

### 2.3 RAG 注入没有“预算内优先级”

`agents/graph.py::_rag_ctx` 会拼接经验文本，但缺少“高价值样本优先”机制，导致 LLM看到很多“弱经验”。

---

## 3. 优化原则

1. **先过滤再写入**：低质量样本不入库。  
2. **仅存 strong**：只保留高质量经验，medium/weak 不入库。  
3. **检索即高质量检索**：RAG 只检索 strong 经验。  
4. **渐进升级**：重复成功的经验才提升为 curated 规则。  

---

## 4. 具体改法

## Phase 1：写入门控（优先做）

### 4.1 扩展 `save_experience` 元数据

修改 `data/knowledge.py::save_experience`，增加字段：

- `signal_type`: `exact_click` / `semantic_click` / `fallback_click`
- `quality_score`: 0~1
- `action_semantic`: 如 `click(label=应用列表,class=textview,path=...)`
- `page_stability`: stable / volatile
- `success_count`: 去重后累计成功次数（用于升级 curated）

### 4.2 `_record_page_transition` 质量打分后再写

先改签名：

```python
def _record_page_transition(ctx, pre_page, label, *, click_context: dict | None = None)
```

`click_context` 由 `click()` 传入，至少包含：

- `exact_mode`, `index`, `rid`, `class_name`, `path_contains`, `strategy`

并在 `_record_page_transition` 内组装：

```python
parts = [f"click(label={label})"]
if class_name: parts.append(f"class={class_name}")
if path_contains: parts.append(f"path={path_contains}")
if rid: parts.append(f"rid={rid}")
if index >= 0: parts.append(f"index={index}")
action_semantic = ",".join(parts)
```

然后做前置评分：

- 高分（>=0.75）才写 `experience`（strong）
- 低分（<0.75）直接丢弃（不存 medium / weak）

建议评分信号（示例）：

- +0.35：精确点击（index/rid/class/path 明确）
- +0.25：rid 非泛化且唯一
- +0.20：path 具备场景语义（非空且有层级）
- -0.30：label/页面命中动态噪声模式（时间/网速/通知）
- -0.25：fallback 或模糊匹配路径

### 4.3 新增“相似经验去重/合并”门控（避免重复写入）

采用两阶段，先简单可落地：

#### Phase 1（本轮）：严格去重

归一化后以下 4 字段全等即视为重复：

- `(app_package, page_norm, action_label_norm, rid_tail)`

命中重复时：

- 不新增文档
- 仅更新 `last_verified_at`
- `success_count += 1`

#### Phase 2（后续）：近重复去重

若 Phase 1 后仍有大量近重复，再增加路径相似度/Jaccard。

### 4.4 `page_norm` 归一化算法（高优先级）

`page_norm` / `to_page_norm` 必须先做动态噪声剔除。建议正则：

- 网速：`\\d+\\.\\d+\\s*[KMG]?[Bb]/s`
- 时间：`\\d{1,2}:\\d{2}(:\\d{2})?`
- 百分比：`\\d+%`

示例：`MainActivity「0.00\\nK/s」` → `MainActivity`

---

## Phase 2：检索分层（与写入并行）

### 4.5 `query_experience` 加排序策略

修改 `data/knowledge.py::query_experience`：

排序键建议：

1. `quality_score` desc
2. `signal_type` 优先级：exact > semantic > fallback
3. `last_verified_at` desc

并增加默认过滤：

- RAG 仅取 `quality_score >= 0.75`（即 strong）
- 不补 low-quality 样本，宁缺毋滥

生效前提说明：

- 该排序依赖 Phase 1 新增元数据（`quality_score`, `signal_type`, `last_verified_at`）。
- 存量历史经验默认 `quality_score=0`，排在末尾。

### 4.6 `_rag_ctx` 只拼“可读高价值经验”

修改 `agents/graph.py::_rag_ctx` 的经验拼接：

- 优先使用 `action_semantic`
- 限制每条经验长度
- 丢弃明显无语义价值的 `page -> click(rid泛化) -> page`

---

## Phase 3：自动升级 curated（你当前诉求）

### 4.7 升级条件（避免污染）

在精确点击成功路径（`tools/__init__.py::_maybe_promote_exact_rule`）中：

仅当满足以下条件才 `save_curated_rule`：

1. 同 `(app, page_signature, label, class/path/rid)` 的去重记录 `success_count >= 3`
2. 无冲突候选（同 label 下未出现不同 class/path 的高频成功）
3. 最近一次验证成功且非 fallback

实现要求：

- 不做全量扫描统计，直接读取去重后的单条经验 `success_count` 决策。

否则只保留 `experience`，不升级 curated。

### 4.8 curated 内容模板标准化

统一模板：

- `在 {page_signature} 点击“{label}”时，优先匹配 class={x} 且 path={y}（rid={z}）`

要求可读、可泛化，避免写入动态 title（如网速时间）。

---

## Phase 4：存量清理（降噪）

新增一次性清理脚本（建议 `scripts/`）：

1. 标记或删除低价值历史经验（动态 title、泛化 action）
2. 对重复内容做去重（同 app/page/action/to_page）
3. 生成清理报告（保留数、删除数、降级数）

---

## 5. 验收标准

1. RAG 注入的 experience 条数下降 >= 50%（同任务对比）。
2. “应用列表/搜索/计算器”关键场景成功率不下降，误点率下降。
3. 日志中 `query_app_knowledge` 返回内容可读性提升（不再充斥网速/时间页名）。
4. 自动升级 curated 的规则中，人工 spot check 准确率 >= 90%。
5. 新增经验中“重复样本率”下降 >= 70%（按签名统计）。

---

## 6. 需要改动的文件（最小集合）

- `tools/__init__.py`
  - `_record_page_transition`（新增 `click_context` 参数）
  - `_maybe_promote_exact_rule`
- `data/knowledge.py`
  - `save_experience`
  - `_normalize_page_id`（新增）
  - `query_experience`
  - （可选）新增 `cleanup_low_value_experience`
- `agents/graph.py`
  - `_rag_ctx`（经验拼接与过滤）
- `tests/`
  - 新增经验质量门控与升级条件回归测试

---

## 7. 实施顺序建议

1. 先做 Phase 1 + Phase 2（立刻降噪）
2. 再做 Phase 3（自动升级 curated）
3. 最后做 Phase 4（存量清理）

这样可以先快速止血，再做长期知识质量闭环。

---

## 8. 全局风险补充（Review 合并）

### 8.1 知识冲突仲裁（高优先级）

风险：当前存在三条写入路径（人工 curated、自动 experience、自动升级 curated），同一 label 可能产生冲突规则，Agent 无法自动判别优先级。

改法（纳入 Phase 3）：

1. 在 `tools/__init__.py::_maybe_promote_exact_rule` 升级前，先查同 app 下同 label 的人工 curated。
2. 若存在人工规则且与自动候选冲突（class/path/rid 不一致）：
   - 不自动写入 curated；
   - 仅写 experience；
   - 打冲突日志（含 rule_id、差异字段）。
3. 规则优先级明确化：`人工 curated > 自动 curated > experience`。

验收：

- 冲突场景下不出现自动覆盖人工规则；
- 日志可追踪冲突原因。

### 8.2 Agent“自我怀疑”与主动降级（高优先级）

风险：当前 Agent 常在不确定状态下继续试错，直到 budget 耗尽，耗时且体验差。

改法（纳入 `agents/graph.py::agent_node`，复用已有 SystemMessage 注入模式）：

触发条件（任一命中）：

1. 连续 3 步同页无进展；
2. 当前 page_info 与 Goal 关键目标长期不对齐；
3. `get_screen_info` 元素集合突变（疑似弹窗/异常页）。

触发后注入系统指令：

- 明确要求先做一次 `get_screen_info` 复核；
- 若仍不确定，优先 `report_done(status="abort", summary="页面异常，建议人工确认")`。

验收：

- `MAX_TURNS_EXHAUSTED` 占比下降；
- “异常页卡死”场景平均步数下降。

### 8.3 端到端黄金用例集（中高优先级）

风险：当前单测覆盖函数逻辑，但缺少真实设备端到端回归，难以防止行为回退。

改法（新增 Phase 5）：

1. 选 5 条历史成功 run（跨不同 App、复杂度）。
2. 固定 `user_request + goal_description`，形成黄金用例。
3. 每次核心改动后跑一轮，比较：
   - 成功率
   - 总步数
   - 总耗时
   - 误点率（WARNING 模糊匹配次数）

落地建议：

- `tests/integration/` 新增用例入口；
- 通过 `--run-integration` 开关控制（默认本地可选，CI 分层执行）。

验收：

- 黄金集成功率稳定（不低于基线）；
- 关键场景误点率持续下降。
