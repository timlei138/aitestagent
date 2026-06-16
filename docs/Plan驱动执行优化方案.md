# Plan-First + Execute-Direct + Recover-Smart 架构优化方案

> 基于当前 Goal-Driven Agent 架构的性能和稳定性分析，提出混合执行优化方案。  
> 核心思路：**Plan 是加速器，Agent 是保险丝**。正常路径零 LLM 消耗，异常路径才调 LLM。

---

## 1. 当前架构分析

### 1.1 现状

```
START → Planner(1次LLM) → Agent(每步LLM) ⇄ loop → Reporter → END
```

- **Planner**：输出目标描述（goal + target_pages + verification + hints）
- **Agent**：每步 LLM 推理 → 决策 → 工具调用 → 观察结果 → 循环
- **Reporter**：生成报告 + 沉淀知识

### 1.2 性能瓶颈

| 指标 | 当前值 | 问题 |
|------|--------|------|
| 每步 LLM 调用 | 1-3 次 | 决策 + 工具调用循环 |
| 10 步测试 Token 消耗 | ~50,000 tokens | 每步都需推理"该做什么" |
| 10 步测试耗时 | 60-120s | LLM 延迟 3-10s/步 |
| 稳定性 | 中 | LLM 决策不确定性导致偶发错误 |

### 1.3 核心问题

Agent 每步都从零推理"该做什么"，但实际上：
- 80% 的步骤是确定性的（点这个、点那个、检查）
- 只有 20% 的步骤需要智能决策（异常恢复、路径探索）

---

## 2. 方案对比

### 2.1 三种执行模式对比

| 模式 | 正常路径 | 异常路径 | Token/步 | 速度 | 稳定性 |
|------|---------|---------|----------|------|--------|
| **A: Agent 探索（当前）** | LLM 推理 | LLM 推理 | ~5000 | 慢 | 中 |
| **B: 代码生成** | 纯 ADB | 报错退出 | 0 | 最快 | 低 |
| **C: Plan 驱动 + Agent 兜底（推荐）** | 直接执行 | LLM 恢复 | ~200 | 快 | 高 |

### 2.2 Token 消耗对比（10 步测试）

| 场景 | 当前（A） | Plan 驱动（C） | 节省 |
|------|----------|--------------|------|
| 顺利执行 | ~50,000 | ~5,000 | **90%** |
| 遇到 1 次异常 | ~55,000 | ~7,000 | **87%** |
| 遇到 3 次异常 | ~70,000 | ~12,000 | **83%** |

### 2.3 耗时对比（10 步测试）

| 场景 | 当前（A） | Plan 驱动（C） | 加速 |
|------|----------|--------------|------|
| 顺利执行 | 60-120s | 10-20s | **5-6x** |
| 遇到 1 次异常 | 70-130s | 15-25s | **4-5x** |

---

## 3. 目标架构

### 3.1 整体流程

```
                         ┌─────────────────────────┐
                         │   Planner（1次 LLM 调用）  │
                         │  查询 KB → 有命中？        │
                         │  有 → 输出详细步骤 Plan     │
                         │  无 → 输出目标描述 Plan     │
                         └────────────┬────────────┘
                                      ↓
                         ┌─── interrupt ───┐
                         │  用户确认/修改 Plan │
                         └────────┬────────┘
                                  ↓
                    ┌─────────────────────────────┐
                    │        Executor              │
                    │                              │
                    │  mode=detailed?              │
                    │  ├─ YES → 逐步直接执行（无LLM）│
                    │  │   每步: tool → check       │
                    │  │   ✅ → 下一步              │
                    │  │   ❌ → 进入 Recovery       │
                    │  │                           │
                    │  └─ NO → Agent 探索模式       │
                    │      每步: LLM + 工具         │
                    └──────────────┬──────────────┘
                                   ↓ (仅 detailed 模式失败时)
                    ┌─────────────────────────────┐
                    │    Recovery Agent（LLM 介入）  │
                    │  输入: 失败步骤 + 页面 + 原因  │
                    │  输出: 修复操作 / 跳过 / 放弃  │
                    │  修复成功 → 回到 Executor      │
                    │  3 次失败 → ABORT             │
                    └──────────────┬──────────────┘
                                   ↓
                    ┌─────────────────────────────┐
                    │        Reporter              │
                    │  生成报告                     │
                    │  成功 → 保存详细 Plan 到 KB    │
                    └─────────────────────────────┘
```

### 3.2 Graph 结构

```python
START → planner → (interrupt) → executor → (done? → reporter)
                                        ↑       ↓
                                        └── recovery ← (fail)
```

### 3.3 核心设计原则

1. **Plan 优先**：有 KB 命中时生成详细步骤，直接执行不走 LLM
2. **自适应降级**：无 KB 时退化为 Agent 探索模式（当前行为）
3. **异常恢复**：步骤失败时 LLM 介入，只修不重做
4. **自动沉淀**：成功执行后保存详细 Plan，下次直接复用

---

## 4. 详细设计

### 4.1 Planner 输出格式自适应

#### 有 KB 命中 → 详细步骤模式

```json
{
  "mode": "detailed",
  "goal": "验证设置中WLAN开关能正常打开",
  "app_package": "com.android.settings",
  "app_name": "Settings",
  "steps": [
    {
      "index": 1,
      "action": "launch_app",
      "target": "com.android.settings",
      "expect": "进入设置首页",
      "alt": []
    },
    {
      "index": 2,
      "action": "click",
      "target": "WLAN",
      "expect": "WLAN设置页",
      "alt": ["WiFi", "无线网络", "Wi-Fi"]
    },
    {
      "index": 3,
      "action": "click",
      "target": "WLAN开关",
      "expect": "开关切换为开启",
      "alt": ["WLAN 开关"]
    },
    {
      "index": 4,
      "action": "verify",
      "target": "WiFi网络列表",
      "expect": "显示可用WiFi网络",
      "alt": []
    }
  ],
  "target_pages": ["WLAN设置页"],
  "verification": ["WLAN开关已开启", "WiFi网络列表已显示"]
}
```

#### 无 KB → 目标描述模式（当前行为）

```json
{
  "mode": "goal",
  "goal": "验证设置中WLAN开关能正常打开",
  "app_package": "com.android.settings",
  "app_name": "Settings",
  "target_pages": ["WLAN设置页"],
  "verification": ["WLAN开关已开启", "WiFi网络列表已显示"],
  "hints": ["左侧导航栏找WLAN", "开关是switch_row类型"]
}
```

### 4.2 Planner 决策逻辑

```python
def planner_node(state, config):
    ctx = get_tool_context()
    
    # 1. 查询 KB 是否有成功计划
    verified_plan = None
    if ctx and ctx.knowledge_base:
        verified_plan = ctx.knowledge_base.query_verified_plan(
            state.get("app_package", ""),
            state.get("user_request", "")
        )
    
    # 2. 有 KB → 详细步骤；无 KB → 目标描述
    if verified_plan:
        plan = _build_detailed_plan(verified_plan)  # mode="detailed"
    else:
        plan = _build_goal_plan(state)              # mode="goal"
    
    # 3. 用户确认
    interrupt({"type": "plan_review", "plan": plan, ...})
    
    return Command(update={"goal_description": plan, ...})
```

### 4.3 Executor 双模式

```python
def executor_node(state, config):
    plan = state.get("goal_description", {})
    mode = plan.get("mode", "goal")
    
    if mode == "detailed":
        return _execute_detailed(state, config)
    else:
        return _execute_exploratory(state, config)  # 当前 Agent 行为


def _execute_detailed(state, config):
    """详细 Plan 模式：逐步执行，零 LLM"""
    plan = state["goal_description"]["steps"]
    idx = state.get("current_step_idx", 0)
    recovery_count = state.get("recovery_count", 0)
    
    # 所有步骤完成
    if idx >= len(plan):
        return Command(update={"status": "success",
                               "conclusion": f"DONE: {len(plan)} steps completed"})
    
    step = plan[idx]
    
    # 直接调工具执行（不走 LLM）
    result = _execute_single_step(step)
    
    # 检查预期结果
    if _check_expectation(result, step.get("expect", "")):
        logger.info("Step %d/%d OK: %s → %s", idx+1, len(plan),
                     step["action"], step["target"])
        return Command(update={
            "current_step_idx": idx + 1,
            "recovery_count": 0,
            "step_history": _append_history(state, step, result, "success"),
        })
    
    # 执行失败 → Recovery
    logger.warning("Step %d/%d FAIL: %s → %s | result=%s",
                   idx+1, len(plan), step["action"], step["target"], result[:100])
    
    if recovery_count >= 3:
        return Command(update={"status": "fail",
                               "conclusion": f"ABORT: Step {idx+1} failed 3 times"})
    
    return Command(update={
        "recovery_mode": True,
        "failed_step": step,
        "failed_result": result,
        "recovery_count": recovery_count + 1,
    })


def _execute_single_step(step: dict) -> str:
    """直接调工具执行单步，不走 LLM"""
    action = step["action"]
    target = step["target"]
    alts = step.get("alt", [])
    
    if action == "launch_app":
        return launch_app.invoke({"package": target})
    
    elif action == "click":
        # 先试 target，再试 alternatives
        for t in [target] + alts:
            r = click.invoke({"label": t})
            if "未找到" not in r and "ERROR" not in r:
                return r
        return f"未找到: {target} 及所有备选 {alts}"
    
    elif action == "wait":
        s = 3
        try: s = int(target)
        except: pass
        return wait_seconds.invoke({"seconds": s})
    
    elif action == "verify":
        return assert_page_contains.invoke({"text": target})
    
    elif action == "scroll":
        panel = target if target in ("left_navigation", "right_content") else "right_content"
        return scroll_panel.invoke({"panel": panel, "direction": "down"})
    
    elif action == "navigate":
        return navigate_to.invoke({"tab": target})
    
    return f"未知 action: {action}"
```

### 4.4 Recovery Agent

```python
def recovery_node(state, config):
    """Recovery: 仅失败时唤醒，1 次 LLM 调用修复"""
    failed_step = state.get("failed_step", {})
    failed_result = state.get("failed_result", "")
    
    # 获取当前页面信息
    ctx = get_tool_context()
    page_info = _get_page_info(ctx)
    
    # 1 次 LLM 调用
    messages = [
        SystemMessage(content="""你是 Android UI 测试恢复专家。
当前步骤执行失败，请分析原因并给出一个修复操作。
输出 JSON: {"action":"click|scroll|dismiss|skip|navigate","target":"...","reason":"..."}
- skip: 此步可跳过（如已达成目标状态）
- dismiss: 关闭弹窗后重试
- click/scroll/navigate: 修复操作"""),
        HumanMessage(content=f"""
失败步骤: {json.dumps(failed_step, ensure_ascii=False)}
失败原因: {failed_result[:200]}
当前页面: {page_info}
""")
    ]
    
    result = _call_llm_once(messages, config)
    fix = _parse_fix(result)
    
    if fix["action"] == "skip":
        idx = state.get("current_step_idx", 0)
        return Command(update={"current_step_idx": idx + 1,
                               "recovery_mode": False})
    
    if fix["action"] == "dismiss":
        dismiss_popup.invoke({})
        return Command(update={"recovery_mode": False})  # 重试当前步
    
    # 执行修复操作
    _execute_single_step(fix)
    return Command(update={"recovery_mode": False})  # 重试当前步
```

### 4.5 Graph 路由

```python
def build_graph(config):
    g = StateGraph(TestState)
    g.add_node("planner", planner_node)
    g.add_node("executor", executor_node)
    g.add_node("recovery", recovery_node)
    g.add_node("reporter", reporter_node)
    
    g.add_edge(START, "planner")
    g.add_edge("planner", "executor")
    g.add_conditional_edges("executor", route_after_executor, {
        "executor": "executor",    # 继续下一步
        "recovery": "recovery",    # 失败 → 恢复
        "reporter": "reporter",    # 完成/放弃
    })
    g.add_edge("recovery", "executor")  # 恢复后继续执行
    g.add_edge("reporter", END)
    
    return g.compile(checkpointer=MemorySaver())


def route_after_executor(state):
    # 完成或失败
    if state.get("status") in ("success", "fail"):
        return "reporter"
    # Recovery 模式
    if state.get("recovery_mode"):
        if state.get("recovery_count", 0) >= 3:
            return "reporter"  # 放弃
        return "recovery"
    # 最大迭代保护
    if len(state.get("step_history", [])) >= 30:
        return "reporter"
    # 继续执行
    return "executor"
```

### 4.6 Reporter 自动保存 Plan

```python
def reporter_node(state, config):
    # ... 现有报告逻辑 ...
    
    # 成功 → 保存详细 Plan 到 KB（供下次复用）
    if status == "success" and ctx and ctx.knowledge_base:
        detailed_plan = _reconstruct_detailed_plan(state)
        ctx.knowledge_base.save_verified_plan(
            app_package=state.get("app_package", ""),
            user_request=state.get("user_request", ""),
            plan=detailed_plan
        )
        logger.info("Saved verified plan to KB for next run")
```

---

## 5. TestState 变更

```python
class TestState(TypedDict, total=False):
    user_request: str
    app_package: str
    app_name: str
    goal_description: dict[str, Any]   # 含 mode 字段
    current_step_idx: int              # 详细模式下当前步骤索引
    recovery_mode: bool                # 是否进入恢复模式
    recovery_count: int                # 当前步骤恢复次数
    failed_step: dict[str, Any]        # 失败的步骤详情
    failed_result: str                 # 失败原因
    step_history: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    conclusion: str
    status: str
    started_at: str
    step_times: list[dict[str, Any]]
```

---

## 6. 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `agents/state.py` | 修改 | 新增 current_step_idx, recovery_mode 等字段 |
| `agents/graph.py` | 重构 | executor_node 双模式 + recovery_node 新增 |
| `agents/prompts/planner.txt` | 修改 | 支持输出 detailed 和 goal 两种模式 |
| `agents/prompts/recovery.txt` | 新增 | Recovery Agent 的 system prompt |
| `data/knowledge.py` | 修改 | save_verified_plan 保存详细步骤 |
| `agents/orchestrator.py` | 微调 | 适配新的 interrupt/resume 流程 |

---

## 7. 迁移策略

### 7.1 分阶段实施

**Phase 1：Executor 双模式（核心）**
- 改造 executor_node 支持 detailed/goal 两种 mode
- detailed 模式：逐步调工具，零 LLM
- goal 模式：保持当前 Agent 探索行为
- Planner 暂不改动，手动测试 detailed 模式

**Phase 2：Planner 自适应**
- Planner 根据 KB 命中自动选择 mode
- 有 KB → detailed，无 KB → goal
- 添加 interrupt 用户确认环节

**Phase 3：Recovery Agent**
- 新增 recovery_node
- 步骤失败时 LLM 介入修复
- 修复后回到 Executor 继续

**Phase 4：自动沉淀闭环**
- Reporter 成功时自动保存详细 Plan 到 KB
- 下次相同请求自动命中 detailed 模式
- 形成"探索 → 沉淀 → 复用"闭环

### 7.2 向后兼容

- `mode="goal"` 时行为与当前完全一致
- 新增字段均有默认值，不影响现有流程
- 可通过配置强制使用 goal 模式（关闭详细执行）

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 详细 Plan 步骤与实际 UI 不匹配 | 执行失败 | Recovery Agent 自动修复 |
| KB 中 Plan 过期（App 更新） | 步骤失效 | Plan 带版本号 + 失败后降级 goal 模式 |
| Recovery Agent 修复不准确 | 重复失败 | 3 次上限 → ABORT |
| 详细 Plan 生成质量不高 | 不如探索模式 | Planner prompt 优化 + 用户确认 |
| 自动保存 Plan 引入错误知识 | 下次执行失败 | 仅保存成功执行的 Plan |

---

## 9. 预期收益

| 指标 | 当前 | 优化后 | 提升 |
|------|------|--------|------|
| 正常执行 Token | ~50,000 | ~5,000 | **↓ 90%** |
| 正常执行耗时 | 60-120s | 10-20s | **↑ 5x** |
| 执行稳定性 | 中（LLM 不确定） | 高（确定性执行） | **显著提升** |
| 首次执行 | 探索（慢） | 探索（同当前） | 不变 |
| 二次执行 | 探索（同样慢） | Plan 复用（快） | **↑ 5x** |
| 异常恢复 | LLM 全程参与 | 仅失败时 LLM | Token ↓ 80% |

---

## 10. 总结

当前架构是 **Agent 全程探索**，每步都走 LLM 推理，灵活但慢且不稳定。

优化后架构是 **Plan 驱动 + Agent 兜底**：
- 有 KB 经验 → 详细 Plan 直接执行（快、稳）
- 无 KB 经验 → Agent 探索（灵活）
- 执行失败 → Recovery Agent 修复（智能恢复）
- 执行成功 → 自动沉淀 Plan（知识闭环）

**核心原则：Plan 是加速器，Agent 是保险丝。**
