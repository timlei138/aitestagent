# RAG 物料优化方案（已融合 Plan Review）

> 目标：LLM 拿到的 RAG 内容是可以直接用的领域知识，不是自己的回声。

---

## 一、核心理念

**不要给 LLM 塞它能自己推理出的内容。只给它没有的领域知识。**

| LLM 能自己推理 | LLM 缺的 |
|---|---|
| "打开计算器 → 输入0/0 → 验证显示不是数字" | formula_or_result 的 resource_id |
| "从桌面进入设置" | "应用列表"是 taskbar_view 容器，要点 navigation_item 子元素 |
| "复制后粘贴到搜索框" | paste() 工具比操作弹窗可靠 |
| | check_desktop_mode("dw") 判断是否在无限工作台 |

右列恰好是 **curated_rule（人工知识）** 和 **element_identity（元素身份）** 在做的事。

---

## 二、知识类型精简

| 类型 | 存储 | 定位 | 写入 |
|---|---|---|---|
| `experience` | ChromaDB | 页面跳转事实：A → click(X) → B | 自动（`_record_page_transition`） |
| `curated_rule` | ChromaDB | 人工领域知识 | 手动（API / 前端） |
| 元素身份 | **SQLite** | alias → resource_id 映射 | 自动（click 成功后） |
| ~~`verified_plan`~~ | **砍掉** | LLM 回声，自己不需要回忆自己的计划 | — |
| ~~`page_structure`~~ | **砍掉** | 页面元素列表，无检索价值 | — |

---

## 三、新架构

```
         ┌─────────────────────────────────┐
         │         Agent 执行操作            │
         │  click / long_press / scroll_find│
         └──────────────┬──────────────────┘
                        │
     ┌──────────────────┼──────────────────┐
     │                  │                  │
     v                  v                  v
┌──────────┐    ┌────────────┐    ┌──────────────┐
│ SQLite   │    │ ChromaDB   │    │ ChromaDB     │
│ 元素身份 │    │ experience │    │ curated_rule │
│ 精确匹配 │    │ 页面跳转   │    │ 人工知识     │
└──────────┘    └────────────┘    └──────────────┘
     │                  │                  │
     v                  v                  v
 click 内部          Planner            Planner + Agent
 自动加权命中      看跳转路径          看人工规则
 (LLM 无感)        ## 操作经验          ## 人工知识
```

---

## 四、具体设计

### 4.1 元素身份记忆（SQLite，LLM 无感）

**写入：** click / long_press / scroll_find_and_click 成功后自动保存。

```python
# understanding 是 click 时已有的 perceive() 结果，复用计算指纹
_save_click_identity(ctx, label, best_el, understanding)
```

存储内容：
```
alias="应用列表"
resource_id="com.zui.launcher:id/taskbar_view"
role="navigation_item"
class_name="TextView"
page_signature="CustomModeLauncher「0,1,2,3,4」"   # Activity + 稳定元素指纹
click_count += 1
```

**page_signature 稳定性：** Activity 名 + 页面可点击元素指纹。指纹算法：取前 8 个有稳定 label 的 clickable 元素（排除纯数字/时间/长文本等动态内容），排序后取前 5 个拼接。

```
# 同页面不同内容 → 指纹稳定（按钮标签不变）
# 同 Activity 不同 Fragment → 指纹不同（按钮集不同）
Calculator「0,1,2,3,4,5,6,7,8,9」           ← 标准模式
Calculator「AC,sin,cos,tan,log,ln,x!,π」      ← 科学模式
SettingsActivity「WLAN,蓝牙,声音,显示,电池」   ← 设置主页
SettingsActivity「日期,时间,时区,24小时」       ← 日期时间页
```

指纹计算复用 click 时已有的 perceive() 结果，无额外开销。

**session 去重：** 同一次测试执行中，同一个 alias 的 click_count 只 +1（不重复累加）。避免 Agent 重试同一按钮 3-5 次导致 click_count 快速膨胀。

**读取分两阶段：**

*阶段 1 — 正向（alias 精确匹配）：*
- `click_count >= 2` → 快速路径，直接命中 resource_id
- `click_count == 1` → +5 分加权

*阶段 2 — 反向（resource_id 反查）：*
```
click("左下角应用按钮")
  → SQLite alias='左下角应用按钮' 未命中
  → 语义搜索 → 找到 el，resource_id=taskbar_view
  → 反查 SQLite: WHERE resource_id='taskbar_view'
  → 有！alias='应用列表' click_count=3 → +5 加权
```

无论怎么描述同一个元素，只要语义搜索找到了，身份加分就生效。

### 4.2 页面跳转记忆 — `experience`（ChromaDB）

**写入：** `_record_page_transition`。只在 click 导致页面变化时写入。

**内容格式（精简）：**
```
"NormalLauncher → click(无限工作台) → CustomModeLauncher"
```

**去重：** 用 `get_by_metadata(where={"app_package": pkg, "page": from_page, "action": action, "to_page": to_page})` 按 metadata 精确过滤。`action` 格式统一为 `click(label)` / `long_press(label)` 等。不走向量搜索。

**Layer 1 排序：** ChromaDB 原生 `get()` 不保证顺序。Python 侧按 metadata 中的 `timestamp` 降序排列后取 top N。

**查询：分层策略（Layer1 精确过滤 + Layer2 语义补充）：**

```python
def query_experience(self, app_package, user_request, top_k=5):
    results = []
    seen = set()

    # Layer 1: 精确过滤当前 App（有包名时，同 App 内导航最常见）
    if app_package:
        precise = self.backend.get_by_metadata(
            where={"app_package": app_package, "knowledge_type": "experience"},
            limit=top_k
        )
        for r in precise:
            key = r["content"]
            if key not in seen:
                seen.add(key)
                results.append(r)

    # Layer 2: 语义搜索补充（跨 App / 系统级 / 无包名场景兜底）
    if len(results) < top_k and user_request:
        semantic = self.query(user_request[:80], knowledge_type="experience",
                             top_k=top_k - len(results))
        for r in semantic:
            key = r["content"]
            if key not in seen:
                seen.add(key)
                results.append(r)

    return results
```

Layer 1 走 `get_by_metadata`（ChromaDB 原生 `collection.get(where=...)`），不做向量搜索，快且精确。覆盖最常见的"当前 App 内导航"。Layer 2 走语义搜索，覆盖通知中心、跨 App 跳转等无固定包名的场景。

**前置依赖：** `VectorStoreBackend` 需新增 `get_by_metadata(where, limit)` 方法。返回格式**对齐 `search()`**（`[{"content": ..., "metadata": {...}, "score": 1.0}]`），确保上层去重和分层查询用同一套取值逻辑：

```python
# ChromaBackend 实现
def get_by_metadata(self, where, limit=50) -> list[dict[str, Any]]:
    raw = self._store.get(where=where, limit=limit, include=["documents", "metadatas"])
    return [
        {"content": doc, "metadata": meta, "score": 1.0}
        for doc, meta in zip(raw.get("documents", []), raw.get("metadatas", []))
    ]
```

此方法在阶段 1 就去重使用，所以**抽象方法定义应放在阶段 1**，阶段 3 只做 query_experience 分层查询重构。

**旧数据清理：** 阶段 1 执行时，删除所有非 `experience` / `curated_rule` 类型的旧数据，以及垃圾 experience：

```python
# 删除所有旧类型
for old in ["verified_plan", "navigation_path", "page_structure",
            "test_experience", "app_precondition", "global_knowledge"]:
    backend.delete({"knowledge_type": old})
# 删除垃圾 experience（无意义的自动提取记录）
backend.delete({"knowledge_type": "experience", "page": ""})
backend.delete({"knowledge_type": "experience", "action": "agent"})
```

**签名变更：** `query_experience` 参数从 `(app_package, page, top_k)` 改为 `(app_package, user_request, top_k)`。调用方：
- `agents/graph.py:275` `_rag_ctx` — 已传 `user_request[:50]`，一致
- `tools/__init__.py:514` `query_app_knowledge` — 直接调 `kb.query(..., knowledge_type="experience")`，不经过 `query_experience`，不变
- deprecated wrapper 已在阶段 1 全部删除，无需考虑

### 4.3 人工知识 — `curated_rule`（ChromaDB）

保持不变。查询方式不变（一次查全部 + Python 侧按 app_package 分组）。

---

## 五、给 LLM 的最终物料

`_rag_ctx()` 只有 2 个 section：

```
## 操作经验
- NormalLauncher → click(无限工作台) → CustomModeLauncher
- CustomModeLauncher → click(应用) → 全部应用列表

## 人工知识
### 全局知识
- CustomModeLauncher 中点击左下角「应用」图标（而非 TaskBar 标签）
- 粘贴用 paste() 工具，不要操作弹窗
```

---

## 六、实施计划

### 阶段 1：砍掉垃圾 + 清理旧数据 + 基础去重（P0）

| 文件 | 改动 |
|---|---|
| `agents/graph.py` | reporter_node 移除 `extract_from_test_result` 和 `save_verified_plan` 调用；`_rag_ctx` 移除 verified_plan 查询，改为 2 sections |
| `data/knowledge.py` | 删除 `save_verified_plan` / `query_verified_plan` / `save_page_structure` / `save_test_experience` |
| `data/knowledge.py` | 删除所有 deprecated wrapper 方法：`save_navigation_path` / `save_test_experience` / `save_page_structure` / `save_precondition` / `save_global_knowledge` / `query_navigation` / `query_preconditions` / `query_global_knowledge` |
| `data/knowledge.py` | `_TYPE_ALIASES` 移除 verified_plan 及其他旧类型映射（只保留 `experience` 和 `curated_rule` 的别名兼容） |
| `data/vector_store.py` | **新增** `get_by_metadata(where, limit)` 抽象方法 + `ChromaBackend` 实现（去重立即需要，不能等阶段 3） |
| `data/knowledge.py` | `save_experience` 去重改为 `get_by_metadata(where={"app_package": pkg, "page": p, "action": a, "to_page": tp})`；`get_by_metadata` 返回后 Python 侧按 `timestamp` 降序排列 |
| **清理脚本** | 删除所有非 `experience` / `curated_rule` 类型的旧数据 + 垃圾 experience（page="" 或 action="agent"） |

### 阶段 2：激活元素身份自动记忆（P1）

| 文件 | 改动 |
|---|---|
| `tools/__init__.py` | 新增 `_save_click_identity(ctx, label, best_el, understanding)` → 调 SQLite `save_element_identity`；page_signature 用 understanding 计算 Activity + 指纹 |
| `tools/__init__.py` | 新增 `_query_known_by_rid()` → 反查 resource_id 的历史身份 |
| `tools/__init__.py` | click / long_press / scroll_find_and_click 成功后调用 `_save_click_identity()` |
| `tools/__init__.py` | session 去重：同一次执行中同 alias 只 count 1 次 |

### 阶段 3：优化 experience 查询 + 格式（P2）

| 文件 | 改动 |
|---|---|
| `data/knowledge.py` | `save_experience` 内容精简为 "A → action → B" 格式；action 格式统一为 `click(label)` / `long_press(label)` |
| `data/knowledge.py` | `query_experience` 改为分层查询：Layer1 `get_by_metadata` 精确过滤 + Layer2 语义搜索兜底 |
| `data/knowledge.py` | `query_experience` 签名从 `(app_package, page, top_k)` 改为 `(app_package, user_request, top_k)`；deprecated wrapper 已在阶段 1 删除，无需同步 |
| `tools/__init__.py` | `_record_page_transition` 增加 (page, action, to_page) 组合去重 |

### 阶段 4：Planner prompt（P3）

| 文件 | 改动 |
|---|---|
| `agents/prompts/planner.txt` | 明确告诉 LLM：操作经验怎么用、人工知识怎么用、乱码忽略 |

---

## 七、效果预期

| | 当前 | 优化后 |
|---|---|---|
| 知识类型数 | 3 | 2（experience + curated_rule） |
| 元素点击准确性 | 每次语义搜索，可能选错 | click_count≥2 直接命中 resource_id |
| experience 去重 | `query("")` 随机 top_k=5，漏检严重 | `get(where=...)` 精确过滤 |
| experience 查询 | 语义搜索，随机结果 | 按 app_package 精确拉取 |
| 别名变化兼容 | 说法不同就查不到身份 | resource_id 反查，不同说法都能加权 |
| LLM 能否复用 RAG | ✗ 只能忽略 | ✓ 提取页面名和操作做 hints |
