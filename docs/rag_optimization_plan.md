# RAG 优化方案（终版 — 已通过评审）

> 基于代码审查 + 最新日志分析。实施顺序：P2 → P1 → P3 → P4 → P5 → P6

---

## 一、当前问题全景图

```
Planner: _rag_ctx() 返回"" → 零RAG上下文 | query_verified_plan() 永远空 → 写路径未接通
Agent:   query_app_knowledge 可选但少调用 | query_element_identity 每次新建DB连接
Reporter: save_page_structure([]) 空数据 | save_navigation_path to_page="下一页面" 垃圾
          save_verified_plan() 从未调用 → 闭环断裂
```

---

## 二、优化计划

### P2：新增 App 前提条件（实施优先级：🥇 第一）

**文件**: [data/knowledge.py](data/knowledge.py)

新增 `app_precondition` 知识类型 + `query_preconditions()` / `save_precondition()` 方法。P1 依赖此方法，所以先做。

**代码**（插入 knowledge.py L223 `@property count` 之前）：

```python
# ── App 前提条件 (app_precondition) ──

def save_precondition(self, app_package: str, rule: str) -> None:
    """保存 App 特定的操作前提条件，如'计算器需先清空输入区'。"""
    # P6.1 去重：已有相同规则则跳过
    existing = self.query("", app_package=app_package,
                        knowledge_type="app_precondition", top_k=5)
    if any(e.get("content", "") == rule for e in existing):
        return
    self.save_knowledge(UIKnowledge(
        app_package=app_package,
        knowledge_type="app_precondition",
        content=rule,
        metadata={"timestamp": datetime.now().isoformat()},
    ))

def query_preconditions(self, app_package: str, top_k: int = 3) -> str:
    """查询 App 的操作前提条件，返回拼接后的规则文本。"""
    results = self.query("", app_package=app_package,
                       knowledge_type="app_precondition", top_k=top_k)
    if not results:
        return ""
    return "\n".join(f"- {r['content']}" for r in results)
```

预设规则（通过 API 写入）:

| App | 规则 |
|-----|------|
| `com.android.calculator2` | 开始前检查输入区，如有残留内容先点清除/C/AC 按钮清空 |
| `com.android.calculator2` | 科学计算模式可通过点击"显示科学计算按钮"或横屏进入 |
| `com.android.settings` | 设置导航使用双栏布局，左侧为导航列表，右侧为内容区 |

前端 [App.vue](frontend/spa/src/App.vue) L648 — kbTypes 数组增加 `{ value: 'app_precondition', label: 'App前提条件' }`，kbTypeColorMap 增加 `app_precondition: 'danger'`。

---

### P1：激活 Planner RAG 上下文（实施优先级：🥈 第二）

**文件**: [agents/graph.py](agents/graph.py), [agents/prompts/planner.txt](agents/prompts/planner.txt)

**P1.1 — `_rag_ctx()` 从死代码变活，同时合并 planner_node 已有的 `query_verified_plan`（消除重复查询）：**

[agents/graph.py](agents/graph.py) L258-259 改为：

```python
def _rag_ctx(kb, app_package: str, user_request: str = "") -> str:
    """查询 RAG 获取 App 上下文：前提条件 + 验证计划 + 导航经验。"""
    if not kb: return ""
    parts = []
    # 1. App 前提条件（如"计算器需先清空"）
    pre = kb.query_preconditions(app_package)
    if pre: parts.append("## App 操作前提\n" + pre)
    # 2. 历史验证计划（同 App 同需求优先）
    plans = kb.query_verified_plan(app_package, user_request, top_k=2)
    if plans: parts.append("## 历史验证计划\n" + "\n".join(f"- {p['content']}" for p in plans))
    # 3. 导航经验（用 user_request 动态查询，非硬编码"从主页导航"）
    if user_request:
        nav = kb.query_navigation(app_package, user_request[:50], top_k=2)
        if nav: parts.append("## 导航经验\n" + "\n".join(f"- {n['content']}" for n in nav))
    return "\n\n".join(parts)
```

**P1.2 — `planner_node` 简化，删除已有的 `query_verified_plan` 单独查询：**

[agents/graph.py](agents/graph.py) L265-285 改为：

```python
def planner_node(state: TestState, config: RunnableConfig) -> Command:
    cfg: TestConfig = config["configurable"]["test_config"]
    llm = _llm_cfg(cfg)
    ctx = get_tool_context()
    kb = ctx.knowledge_base if ctx else None
    rag = _rag_ctx(kb, state.get("app_package", ""), state.get("user_request", ""))
    msgs = PLANNER_TEMPLATE.format_messages(
        user_request=state.get("user_request", ""),
        app_name=state.get("app_name", ""),
        app_package=state.get("app_package", ""),
        rag_context=rag,
    )
    # ... 后面不变
```

**P1.3 — [planner.txt](agents/prompts/planner.txt) 末尾追加：**

注意：`{rag_context}` 变量只在 `PLANNER_TEMPLATE` 的 **user 消息**中被 `format_messages()` 替换，不会在 SystemMessage 中替换。planner.txt 只放指令文字即可，RAG 内容已通过 user 消息注入。

```
## App 知识参考
如果用户消息中提供了 App 操作前提、历史验证计划或导航经验，请将其融入 hints（前提条件放第一条），但以实际页面为准。
```

---

### P3：闭环 verified_plan — 成功后自动保存

**文件**: [agents/graph.py](agents/graph.py), [data/knowledge.py](data/knowledge.py)

**P3.1 — reporter_node 成功后自动保存：**

[agents/graph.py](agents/graph.py) L557 之后（`except: pass` 和 `if _relational_db` 之间）插入：

```python
            # P3: 成功后保存 verified_plan 到 RAG
            if status == "success":
                try:
                    ctx.knowledge_base.save_verified_plan(
                        app_package=state.get("app_package", ""),
                        user_request=state.get("user_request", ""),
                        plan=history, results=history,
                    )
                except Exception:
                    pass
```

**P3.2 — `save_verified_plan` 用 `intent` 替代永远为空的 `target`：**

[data/knowledge.py](data/knowledge.py) L188-189 改为：

```python
# 改前: s.get("target", "")   ← step_history 中 target 永远是 ""
# 改后: s.get("intent", "")[:30]
success_targets = [s.get("intent", "")[:30] for s in results if s.get("status") == "success"]
fail_targets = [s.get("intent", "")[:30] for s in results if s.get("status") != "success"]
```

---

### P4：清理死代码 + 修复低质量数据

**文件**: [data/knowledge.py](data/knowledge.py)

**死代码删除**（确认零外部调用）:

| 方法 | 行号 | 说明 |
|------|------|------|
| `as_retriever_memory()` | L128-137 | 从未调用 |
| `load_memory_context()` | L139-143 | 从未调用 |
| `save_element_knowledge()` | L147-172 | 从未调用 |
| `query_element_knowledge()` | L174-180 | 从未调用 |
| `get_app_context()` | L118-123 | 仅被 build_rag_enhanced_prompt 调用 |
| `build_rag_enhanced_prompt()` | L229-234 | 从未调用 |

**垃圾导航数据修复**（两步：agent_node 源头存入 + extract_from_test_result 直接读取）：

核心思路：在 `agent_node` 构建 step_history 时就把 `post_page` 字段存好（数据源头结构化），下游 `extract_from_test_result` 直接读取，不需要 messages 参数和正则扫描，也避免了 page_idx 错位问题。

**Step A** — [agents/graph.py agent_node](agents/graph.py) L420 附近，构建 step 记录时增加 `post_page` 字段：

注意：`操作后页面:` 在 click 工具内部生成的 ToolMessage 中，不在 `msgs` 里。`msgs` 中可提取的是 Phase 1.5 注入的 `当前页面:` HumanMessage（[graph.py L275](agents/graph.py#L275)），格式为 `[操作后页面状态]\n当前页面: Calculator「10:13」`。

```python
# agent_node 中，构建 nh 之前提取 post_page
# 只在有 click 操作时才提取（避免非 click 步骤继承上一步的旧值）
post_page = ""
# 检测本步骤是否有 click/scroll_find_and_click 的 tool_calls
_had_click = any(
    tc.get("name") in ("click", "scroll_find_and_click")
    for m in msgs[-4:]
    if isinstance(m, AIMessage)
    for tc in (getattr(m, "tool_calls", None) or [])
)
if _had_click:
    for m in reversed(msgs):
        c = str(getattr(m, "content", "") or "")
        if "当前页面:" in c:
            match = re.search(r"当前页面:\s*(.+?)(?:\n|$)", c)
            if match:
                post_page = match.group(1).strip()
            break

nh = list(history) + [{
    "index": si, "intent": result[:80].replace("\n", " "),
    "action_type": "agent", "target": "", "status": st,
    "observation": result[:300],
    "post_page": post_page,
    "screenshot_path": "", "anomaly": None,
}]
```

**Step B** — [data/knowledge.py](data/knowledge.py) `extract_from_test_result` 简化，直接读 `post_page`：

```python
def extract_from_test_result(
    self, app_package: str, test_case: str,
    execution_log: list[dict[str, Any]], final_result: str,
) -> int:
    count = 0
    visited: set[str] = set()
    for entry in execution_log:
        page = entry.get("page", "")
        action = entry.get("action", "?")
        observation = entry.get("observation", "")
        step_ok = entry.get("result") == "success"

        # 直接从 step_history 读取已保存的 post_page（agent_node 源头填入）
        to_page = entry.get("post_page", "")
        if page and to_page and to_page != page:
            self.save_navigation_path(app_package, page, to_page, action)
            count += 1

        # page_structure: 有内容才写入（不再传空列表）
        if page and page not in visited:
            visited.add(page)
            labels = _extract_labels_from_observation(observation)
            if labels:
                self.save_page_structure(app_package, page, [{"label": l} for l in labels])
                count += 1

        self.save_test_experience(
            app_package, page, action,
            "成功" if step_ok else "失败",
            observation or entry.get("error", ""),
        )
        count += 1
    return count
```

注意：reporter_node 调用处**不需要**传 `messages` 参数了——`post_page` 已经在 step_history 中。

新增辅助函数（加在 knowledge.py 末尾）：

```python
def _extract_labels_from_observation(observation: str) -> list[str]:
    """从 observation 文本中提取元素标签。从 LLM 输出和工具返回中提取。"""
    import re
    labels = []
    for line in observation.split("\n"):
        # 匹配 "label='XXX'" 格式（工具返回）
        for m in re.finditer(r"label='([^']+)'", line):
            labels.append(m.group(1))
        # 匹配 click("XXX") 格式（LLM 输出）
        for m in re.finditer(r'click\("([^"]+)"\)', line):
            labels.append(m.group(1))
    return list(dict.fromkeys(labels))[:20]  # 去重，最多 20
```

---

### P5：增强 Agent 使用 RAG

**文件**: [agents/prompts/agent.txt](agents/prompts/agent.txt)

规则区增加一行：

```
- 不确定元素位置时，优先调用 query_app_knowledge 查找历史导航经验
```

---

### P6：附带修复

**P6.1 — KB 去重**（已内嵌到 P2 `save_precondition` 中）：

`save_verified_plan` 开头加查重：

```python
def save_verified_plan(self, ...):
    existing = self.query_verified_plan(app_package, user_request, top_k=1)
    if existing:
        return  # 已有相似计划，跳过
    # ... 原有逻辑
```

**P6.2 — `query_app_knowledge` 格式化返回：**

[tools/__init__.py](tools/__init__.py) L469 改为：

```python
# 改前
return str(ctx.knowledge_base.query(query, app_package=package))

# 改后
results = ctx.knowledge_base.query(query, app_package=package)
if not results:
    return f"未找到 '{query}' 的相关知识"
lines = [f"[{r.get('metadata',{}).get('knowledge_type','')}] {r['content']}" for r in results]
return "\n".join(lines)
```

**P6.3 — `query_element_identity` 复用全局 DB（避免循环导入）：**

[tools/__init__.py](tools/__init__.py) L482-487 改为延迟导入——不能顶层 `from agents.graph import _relational_db`（循环导入：tools → graph → tools）：

```python
# 改前
from data import create_relational_db
from config import TestConfig
cfg = TestConfig()
db = create_relational_db(cfg)

# 改后（函数内延迟导入，避免循环依赖）
db = None
try:
    from agents.graph import _relational_db as _gdb
    db = _gdb
except ImportError:
    pass
if db is None:
    from data import create_relational_db
    from config import TestConfig
    db = create_relational_db(TestConfig())
```

---

## 三、涉及文件清单

| # | 文件 | 改动类型 |
|---|------|---------|
| 1 | [data/knowledge.py](data/knowledge.py) | 新增 precondition + 去重 + 删死代码 + 修复 extract_from_test_result |
| 2 | [agents/graph.py](agents/graph.py) | 重写 _rag_ctx + 简化 planner_node + reporter 保存 verified_plan + agent_node 存入 post_page |
| 3 | [agents/prompts/planner.txt](agents/prompts/planner.txt) | 增加 App 知识参考段落 |
| 4 | [agents/prompts/agent.txt](agents/prompts/agent.txt) | 增加 RAG 调用提示 |
| 5 | [tools/__init__.py](tools/__init__.py) | 格式化 query_app_knowledge 返回 + 复用 DB 连接 |
| 6 | [frontend/spa/src/App.vue](frontend/spa/src/App.vue) | kbTypes 增加 app_precondition |

---

## 四、实施顺序

```
P2 (precondition 方法 + 去重)
  → P1 (激活 _rag_ctx + 合并 planner_node 重复查询)
    → P3 (闭环 verified_plan + 修复 intent/target)
      → P4 (删死代码 + 修复垃圾导航 + 空页面)
        → P5 (Agent prompt) → P6 (附带修复)
```

---

## 五、验证

1. 首次运行 RAG 为空：Planner 正常生成计划（`{rag_context}` 为空，不影响）
2. 运行一次时区切换成功后：`verified_plan` 有数据，再次运行时 hints 包含历史经验
3. 写入计算器前提条件后：计算器测试的 hints 第一行包含 "先点清除按钮清空输入区"
4. `extract_from_test_result` 不再写入 `to_page="下一页面"` 和空 `page_structure`
5. 重复运行同一测试 3 次：KB 中 verified_plan 只有 1 条（去重生效）
6. `query_app_knowledge` 返回可读文本而非 Python repr
7. `query_element_identity` 不再每次创建新 DB 连接
