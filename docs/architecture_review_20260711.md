# AI 测试 Agent 紧急问题评审（2026-07-11）

> 视角：AI Agent 高级开发工程师。
> 部署前提（作者确认）：**本地 PyInstaller 打包成 exe 给人员单机使用，不暴露公网/局域网，用例在单机上一条一条串行执行。**
> 因此原报告中与「网络暴露」「并发」相关的问题不再适用，已删除（见文末「已移除条目」）。
> 本文只保留**紧急、且与当前实际痛点直接相关**的问题：**前端对话框按顺序执行时，偶尔出现「找不到控件」。**

---

## 0) 结论先行

「偶尔找不到控件」不是某一个 bug，而是**感知（perceive）与执行（click）之间存在时间差**导致的一类竞态问题。核心链路是：

```
agent_node 感知页面A → 构造带 [index] 的元素列表 → 交给 LLM
   → LLM 思考若干秒（此时页面可能已变成 A'）
   → 返回 click(index=k) 或 click(label=...)
   → click() 内部【再感知一次】→ 在 A' 上按 index/label 定位
   → A' 与 A 元素顺序/数量不一致 → 命中错误元素 或 找不到 ❌
```

我在代码里确认了这条链路的两个关键事实：

1. **`click()` 会重新感知一次**：`tools/__init__.py` 的 `click()` 里再次调用 `ctx.perceiver.perceive()` 来定位元素，用的**不是** `agent_node` 当初给 LLM 看的那份快照。
2. **`index` 依赖两次感知的元素顺序完全一致**：`SmartPerceiver.perceive()` 用 `md5(xml|mode)` 做 5 秒缓存，一旦页面 XML 有任何变化（动画、异步加载、Toast、状态栏刷新）缓存立即失效，重新解析出的元素列表顺序/数量可能变化，于是 LLM 基于旧列表给的 `index=k` 就指向了错误元素或越界。

围绕这条链路，下面 5 个点是**紧急需要解决**的，按对「找不到控件」的贡献度排序。

---

## 1) 紧急问题（全部指向「找不到控件」）

### 1.1【最高】感知与执行之间无「页面稳定」等待，采样发生在页面还在变化时

**现象**：页面切换动画没结束、列表还在异步加载、上一步操作的过渡还没稳定时，`perceive()` 就采样了，拿到的是**中间态**的 UI 树。等 LLM 决策完再点，页面已经是另一个样子。

**代码事实**：`SmartPerceiver.perceive()` 直接 `self.device.dump_hierarchy()` 取当前树，**没有任何「等页面稳定」的前置等待**；`device/controller.py` 也没用 uiautomator2 的隐式等待/`wait_activity`。

**修复建议**（这是性价比最高的一项）：
- 在 `perceive()` 采样前加一个轻量的「稳定等待」：连续轮询 `dump_hierarchy()` 的 hash，直到**连续 2~3 次不变**或超时（如 1.5s）再返回。这样能过滤掉绝大部分「采样到中间态」的情况。
- 关键操作（click/scroll）执行后，下一次 perceive 同样先等稳定，再判断结果。

### 1.2【最高】`click` 用 `index` 作首选定位，而 `index` 恰恰是最不稳定的定位方式

**现象**：`agent.txt` 里明确写「优先使用 index 精确点击」。但 `index` 的语义是「感知返回的可点击元素列表里的第 k 个」——它完全依赖**那一次感知**的元素顺序。而 `click()` 内部会**重新感知**，两次之间顺序一旦变化，`index=k` 就点错/找不到。

**代码事实**：
- `agent_node` 里 `[index]` 是对 `u.elements` 里 `clickable and label` 的元素**按遍历顺序**编号的；
- `click()` 里 `_exact_clickable_candidates(understanding, index=index)` 用的是**新一次** `perceive()` 的 `understanding`。两份列表并不保证一致。

**修复建议**：
- **prompt 层**：把定位优先级从「index 优先」改为 **`rid`（resource_id）> `path_contains` > `class_name` > `index`**。`rid`/`path` 是元素自身属性，跨两次感知稳定；`index` 是列表位置，最易漂移。`index` 只作为最后兜底。
- **代码层**：`click()` 定位时，若同时提供了 `rid`/`path`，忽略 `index`（或仅用 index 在 rid/path 命中多个时做二次筛选），不要让 index 单独决定点谁。

### 1.3【高】给 LLM 的页面元素列表被硬截断到 25 条，目标元素可能根本没被「看见」

**代码事实**：`agent_node` 构造 `page_info` 时：
```python
for e in u.elements:
    if e.clickable and e.label:
        lines.append(f"  - [{click_idx}] " + e.label + extra)
        click_idx += 1
        if len(lines) > 25:   # ← 硬截断，超过就 break
            break
```
在设置页、应用列表、长表单这类元素多的页面，可点击项经常远超 25 个。**目标控件排在 25 之后时，LLM 在输入里根本看不到它**，只能靠猜或滚动，自然「找不到」。而且这里 break 用的是 `len(lines)`（含 page/layout/clickable 3 行头），实际展示的可点击元素还不到 25 个。

**修复建议**：
- 上限提高到 60~80，并且**不要简单 break**：超限时按「与当前目标/verification 的文本相关性」排序后再截断，保证目标大概率进入列表前部。
- `index` 编号必须与这份**最终展示**的列表严格对应（配合 1.2，减少 index 漂移）。

### 1.4【高】`click` 命中失败后没有「等一下 + 重新感知 + 重试」，一次找不到就直接失败

**代码事实**：
- `click_text` / `click_resource_id` 用 `.exists(timeout=2.0)` 等 2 秒，找不到就返回 False；
- `click_bounds` 直接按坐标点，**不做任何存在性检查**；
- `click()` 主流程里，感知一次定位不到就走兜底/返回错误，**没有「短暂等待后重新感知再试一次」的重试环**。

对于「页面还没稳定就采样 → 这一拍没找到」的场景，往往**再等 0.5~1s 重新感知就能找到**。缺这一层重试，是「偶尔」失败而不是「必然」失败的直接原因。

**修复建议**：
- 在 `click()` 定位失败（非精确参数明确报 NOT_FOUND 的情况）时，加一个**有限重试**：`sleep(0.5) → 重新 perceive → 再定位`，最多 2~3 次。
- 与 1.1 的稳定等待配合，能把「偶发找不到」压到很低。

### 1.5【中】perceive 每次缓存未命中都同步写截图到磁盘，拉长单步耗时、放大竞态窗口

**代码事实**：`SmartPerceiver.perceive()` 在 cache miss 时会 base64 解码并**同步写一张 PNG 到磁盘**（供 assert_verification 复用）。长任务里 perceive 很频繁，每次写盘都是同步 IO，叠加真机 `dump_hierarchy` + 截图本身耗时，**单步延迟被拉长 → 1.1 的竞态窗口更大 → 更容易找不到控件**。

**修复建议**：截图改为**按需**落盘（只有真正需要留证时才写），或先在内存留最近一帧，`assert_verification` 要用时再写。降低单步延迟本身就能缓解竞态。

---

## 2) 次紧急：RAG 中文重排（rerank）失效——真实 bug，顺手修

虽然不直接等于「找不到控件」，但它让「查历史导航经验」这一步质量下降，间接影响 Agent 找对路径，且是**一处确定的功能性 bug、修复成本极低**，建议一并处理。

**代码事实**：`tools/__init__.py` 的 `query_app_knowledge` 用 `_experience_relevance` 对召回结果重排：
```python
def _experience_relevance(entry, query_lower):
    content = str(entry.get("content", "") or "").lower()
    words = [w for w in query_lower.split() if len(w) >= 2]   # split() 按空格分词
    ...
    return sum(1 for w in words if w in content)
```
中文查询**没有空格**，`.split()` 会把整句当成**一个词**，`if 整句 in content` 几乎永不命中 → relevance 恒为 0 → `merged.sort(key=relevance)` 退化成保持原顺序，**所谓语义 rerank 实际没生效**。本项目以中文为主，这个 bug 一直在默默削弱 RAG 召回质量（不报错，很隐蔽）。

**修复建议**：中文改用**字符级 2-gram 重叠**，或直接复用向量检索已经算出的相似度分数（`query()`/`query_experience` Layer2 已有 distance）来排序，别用基于空格分词的词频。

---

## 3) 模型能力依赖问题——把该由代码定的判断还给代码（症状「模型能力影响大」）

这是「换个更弱的模型就明显变差」的根因：系统把太多**本可以用代码确定性完成**的判断压给了模型。模型一弱，这些判断就不稳。

> 注：与「找不到控件」相关的感知/截断问题（原分析里的 6.5/6.6/6.7）已并入 §1（分别对应 1.3 / 1.2 / 1.5），此处不重复。

### 3.1【高】每轮往 prompt 注入多达 8 类启发式「提示」，弱模型容易忽略或被淹没

`agent_node` 会按触发条件注入至少 8 类提示：`FINALIZATION_HINT` / `KNOWLEDGE_QUERY_REQUIRED` / `SELF_DOUBT_HINT` / `APP_SWITCH_HINT` / `NO_PROGRESS_WARNING` / `COOLDOWN_TRIGGERED` / `[操作后页面状态]` / `[系统提醒] 连续 N 次相同操作`。

本质问题：这些都只是**「建议」**，能否生效完全取决于模型是否遵循。你配的 `deepseek-v4-flash` / `glm-4.6v-flash` 都是 flash 小模型，经常忽略 SystemMessage 指令，或被同时存在的多条提示分散注意力，反而更不稳。**提示越多 → 越依赖模型的长上下文指令遵循能力 → 越吃模型能力。**

**修复方向**：把「软提示」逐步升级为「代码硬控制」。参考现有 `cooldown_map` 已经做到的 `COOLDOWN_SKIP`（直接不执行工具，而不是发提示求模型别做）——这就是对的方向。同时限制「同一轮注入的提示数量上限」，多个条件命中时只留优先级最高的 1 条，避免多条提示互相干扰弱模型。

### 3.2【高】终止信号靠文本正则 `DONE:`/`ABORT:` 兜底，弱模型不调 `report_done` 就会空转到预算耗尽

终止判定有两条路径：结构化的 `report_done` 工具调用（可靠）；文本兜底正则 `_DONE_PATTERN`（脆弱，要求行首出现 `DONE:`/`ABORT:`）。弱模型经常**用自然语言说「任务完成了」而不调用 `report_done`**，正则匹配不到 → 该结束不结束 → 一直空转到 `MAX_TOOL_CALLS_EXHAUSTED`，白烧大量步骤和 token。

**修复方向**：把 `report_done` 明确为**唯一**合法终止方式；检测到「模型输出了完成语义但没调工具」时，由代码注入一条强制 ToolMessage 要求下一步必须调 `report_done`，而不是靠正则去猜自然语言。收尾阶段可配合 §5 的 `tool_choice` 强制工具调用。

### 3.3【中】两条 provider 路径不对等：温度、错误统计都不一致

`_run_agent` 里 openai 分支 `ChatOpenAI(temperature=0.1)` 硬编码且有 `tool_call_400` 统计；zhipu 分支走原生 SDK，**没设 temperature**（用服务端默认，通常更高、输出更发散）、**也不统计 `tool_call_400`**。后果：同一模型在 zhipu 路径可能比 openai 路径更「跳」、更不稳；而 `tool_call_400_rate` 指标在 zhipu 下恒为 0，report 里看到的会误导判断。

**修复方向**：`temperature` 提到 `config.yaml` 可配（自动化任务建议 0~0.1）；两条 provider 的温度与错误统计对齐（最好统一收敛到 LangChain `ChatOpenAI`，见 §5 的 provider 统一建议）。

### 3.4【高】`assert_verification` 通过/失败完全靠模型主观上报，可确定性判断项也绕回模型

测试的「通过/失败」结论本身也压在模型身上：`assert_verification` 的 passed/failed 由模型判断并上报，`reporter_node` 只做汇总。弱模型可能看错页面就报 passed（**假阳性最危险：系统说通过、实际没过**）。而像「页面出现文字 X」「元素 Y 存在」「输入框内容等于 Z」这类**可以代码确定性判断**的条件，本可由代码直接对 UI 树断言——项目里其实已有 `assert_page_contains` / `assert_element_exists`，但 prompt 流程是「这些工具返回 PASS 后，模型**再**调 `assert_verification` 上报」，又绕回了模型。

**修复方向**：对文本/元素存在性这类可判定项，让 `assert_page_contains` / `assert_element_exists` 的结果**直接**写入 `_verifications`，不需要模型二次上报，少一个模型出错的环节；只有颜色/布局/Toast 等需要视觉语义的条件才走模型 + 视觉工具。既降低模型依赖，又提升结论可信度。

---

## 4) RAG 其余可优化项

§2 的中文 rerank 失效是**最紧急的 RAG bug**。除此之外还有三处影响召回质量：

### 4.1【中】两条 RAG 通道口径不一致，同样的知识按不同策略召回

同一知识库有两条读取路径且策略不同：
- `_rag_ctx`（planner + agent 自动注入）：`query_experience(top_k=3)` + `query_curated_rules(top_k=20)`；
- `query_app_knowledge`（模型主动调用的工具）：hybrid（`query_experience(top_k=10)` + 语义 `query(top_k=5)` 合并 rerank 到 5）+ `query_curated_rules(top_k=3)`。

「自动注入看到的知识」和「模型主动查到的知识」是两套不同结果集（数量/排序/是否 rerank 都不同），行为难预测、调优时按下葫芦浮起瓢。

**修复方向**：抽一个统一的 `retrieve_knowledge(app_package, query, page_sig)`，两条路径都走它，只是 top_k 不同，保证召回/去重/rerank 单一来源。

### 4.2【中】`query_curated_rules` 完全不做相关性排序，只取 metadata 前 N 条

`query_curated_rules` 用 `get_by_metadata(limit=N)` 拉取后按 top_k 截断，**没有任何与当前 query/页面的相关性排序**——只是「metadata 里排在前面的若干条」。而人工知识（curated_rule）恰恰是 `planner.txt` 里当作**第一优先级**注入 hints 的。第一优先级的内容却是「随便拿几条」，会稀释高价值规则。

**修复方向**：curated_rule 也按语义相关性检索（走向量 `query()`），或至少按 `quality_score` / `last_verified_at` 排序后再截断。

### 4.3【中】experience 的 `quality_score >= 0.75` 硬门槛会静默丢弃缺分的历史数据

`query_experience` Layer1 精确召回时过滤 `quality_score >= 0.75`。缺失 `quality_score` 的历史经验默认 0，**永远** < 0.75，被直接排除在精确召回外（只能靠 Layer2 语义兜底，而 Layer2 又受 §2 的 rerank 失效影响）。结果是早期积累、还没打分的经验「存了但查不到」。

**修复方向**：给缺分历史数据一个合理默认分（如 0.5），或门槛过滤后若结果为空则放宽兜底，避免「有数据但检索不到」。

---

## 5) 可引入的 LangChain / LangGraph 框架能力

已在用且用得对：`astream_events(v2)` 流式、`interrupt()` 人在环、`StateGraph` + `Command` + `MemorySaver`、`bind_tools`。以下能力**尚未使用**，且能直接对上上面的痛点。

### 高价值（直接命中现有痛点）

- **`.with_structured_output(TestGoalOutput)` — 修 planner 脆弱解析**。`planner_node` 现在用 `_parse_goal` 的正则 `re.search(r"\{...\}")` 抠 JSON，弱模型格式一乱就退化成 `goal=文本[:200]`。而 `TestGoalOutput`（state.py）这个 Pydantic 模型**已经定义好却没用**。直接结构化输出，框架强制/校验结构，弱模型也能稳定出合法 goal。（对应 3.x「降低模型依赖」）
- **`.with_fallbacks([更强的模型])` — 针对「模型能力影响大」**。关键节点（planner、验证判定）配「flash 小模型主跑，失败或输出不可用时自动降级到更强模型」，是对弱模型不稳定最直接的框架级兜底，比塞一堆 prompt 提示可靠。
- **`bind_tools(tool_choice=...)` 强制工具调用 — 针对 3.2**。收尾/验证阶段强制模型必须产生工具调用（如 `report_done` / `assert_verification`），而不是靠 prompt 求它、再用正则去猜自然语言。
- **`EnsembleRetriever`(BM25 + 向量) 或 reranker — 直接替代失效的 `_experience_relevance`（§2）**。`BM25Retriever` 对中文关键词召回好，与 Chroma 向量做 hybrid 融合；或 `ContextualCompressionRetriever` + CrossEncoder reranker。`data/vector_store.py` 已暴露 `.store` 属性注释「用于 as_retriever」，本就朝这个方向。

### 中价值（健壮性 / 契合桌面形态）

- **`SqliteSaver` 替代 `MemorySaver` — 很契合桌面 exe**。`build_graph` 现在用内存态 `MemorySaver`，resume 还依赖内存字典 `_state_cache`。桌面 exe 进程随时被关，一关 checkpoint 全丢。`SqliteSaver` 持久化到磁盘，**应用重启后仍可 resume 中断的用例**。
- **节点级 `RetryPolicy` — `add_node("agent", agent_node, retry=RetryPolicy(...))`**。设备抖动、perceive 偶发异常时，LangGraph 在节点层自动重试，比到处手写 try/except 干净。（注意：这解决「节点抛异常」，与 §1「感知竞态」是两个层面，竞态仍需 1.1 的稳定等待 + 1.4 的重试）
- **LLM / 节点缓存** — 减少重复 LLM 调用、顺带省成本。

### 架构级（非紧急，改动大）

- **工具用 `InjectedState` / `InjectedStore` + 工具返回 `Command`**：可消除全局单例 `ToolContext`，工具直接从注入的 state 拿依赖、直接返回状态更新，更贴合 LangGraph 数据流。但改动面大，且单机串行下全局单例目前没造成实际 bug，不急。
- **LangSmith 追踪（可评估本地/自建）**：现在把 stdout tee 到 `*_langchain.log`，LangSmith 能给完整 turn-by-turn trace 树，定位「这步到底是模型决策错、工具契约错、还是感知错」比翻日志强很多。

### 明确不建议

- **`create_react_agent` 预制 agent**：会丢掉你 `_run_agent` 里大量自定义断路器/冷却/循环检测/事件发射，保持自定义子图是对的。

> **provider 注意点**：`with_structured_output` / `with_fallbacks` / `tool_choice` 都是 LangChain Runnable 能力，在 `ChatOpenAI` 路径可直接用；但 zhipu 走**原生 SDK**（`ZhipuAI(...)`）享受不到。要统一用这些能力，建议把 zhipu 也接到 LangChain `ChatOpenAI`（智谱有 OpenAI 兼容端点）或 `langchain-community` 的智谱封装——这同时也解决 3.3 的「两条 provider 不对等」。

---

## 6) 修复优先级与落地顺序

**第一批（紧急，直接修「找不到控件」）：**

| 顺序 | 修复项 | 直接效果 | 预估成本 |
|---|---|---|---|
| 1 | **1.1 perceive 前加页面稳定等待** | 消除「采样到中间态」，最大幅度减少偶发找不到 | 半天~1 天 |
| 2 | **1.2 定位优先级 index→rid/path**（prompt + click 代码） | 消除 index 漂移导致的点错/找不到 | 半天 |
| 3 | **1.4 click 失败后有限重试（等待+重新感知）** | 把「偶发」失败兜住 | 半天 |
| 4 | **1.3 元素列表上限提高 + 相关性排序** | 目标控件不再被截断在视野外 | 半天 |
| 5 | **2 RAG 中文 rerank 修复**（可直接用 §5 的 EnsembleRetriever/reranker） | 提升导航经验召回质量 | 半天 |
| 6 | **1.5 截图写盘按需/异步** | 降单步延迟，进一步缩小竞态窗口 | 半天 |

**第二批（降低模型依赖 + RAG + 框架能力，建议第一批验证通过后推进）：**

| 顺序 | 修复项 | 直接效果 | 预估成本 |
|---|---|---|---|
| 7 | **§5 `with_structured_output` 改造 planner** | 修 3.x 脆弱解析，降低模型依赖 | 半天 |
| 8 | **3.4 可确定性验证项由代码直接断言** | 提升结论可信度，减少假阳性 | 1~2 天 |
| 9 | **3.2 + §5 `tool_choice` 统一终止/验证走强制工具调用** | 弱模型不再空转到预算耗尽 | 1 天 |
| 10 | **4.1 统一两条 RAG 检索通道口径** | 行为可预测、可调优 | 1 天 |
| 11 | **§5 `.with_fallbacks` 关键节点模型降级** | 直接对冲「模型能力影响大」 | 1 天 |
| 12 | **3.3 provider 统一到 ChatOpenAI + temperature 可配** | 行为/指标对齐，解锁上面几项框架能力 | 1 天 |
| 13 | **§5 `SqliteSaver` 持久化 checkpoint** | 桌面 exe 重启后可 resume | 半天 |
| 14 | **3.1 限制单轮注入提示数、软提示逐步升级为硬拦截** | 降低模型依赖 | 持续 |
| 15 | **4.2 / 4.3 curated_rule 相关性排序、quality 门槛兜底** | RAG 召回质量 | 各半天 |

> 建议先做 1、2、3 这三项——它们直接命中「感知-执行竞态」这个根因，通常能立竿见影地把「偶尔找不到控件」压下去。做完后再用真机回放集验证一轮，观察失败率变化，再决定 4/5/6。

---

## 已移除条目（不再适用或非紧急）

按作者确认的「本地单机 exe、串行执行、只保留紧急问题」原则，以下原报告条目已删除：

- **API/WebSocket 无鉴权**：本地 exe、不暴露公网/局域网，不构成实际风险。（若哪天用 CLI `server` 模式对外起服务，再把默认 host 从 `0.0.0.0` 收到 `127.0.0.1` 即可，一行改动。）
- **全局 ToolContext 无并发保护**：用例单机串行、一条一条执行，不存在并发 run，问题不成立。
- **危险动作关键词匹配、Chroma workaround、DB 迁移无版本表、LLM 成本/熔断、依赖锁定、CI、单测覆盖、前端 WS 重连**：均为工程健壮性/可维护性改进，非当前紧急痛点，暂不纳入本文。需要时可另开专项。

---

*本文基于 2026-07-11 代码库状态，聚焦「偶尔找不到控件」这一紧急问题的根因与修复。*
