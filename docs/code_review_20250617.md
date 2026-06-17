# 代码 Review + 最新日志分析报告

> 日期: 2025-06-17 | 测试用例: 计算器 tan(30) 验证

---

## 一、Review 结果验证

### 1. 精简 Prompt → ✅ 通过

Prompt 从 52 行减至 20 行，不影响执行效果。日志中 Agent 行为正常（启动计算器、点击清除）。

### 2. click 返回状态变化 → ⚠️ 有隐患（确认）

**Review 结论正确。** 但根因不是 "无 sleep 导致缓存旧数据"，而是另外两个问题：

#### 问题 2a：`_post_click_snapshot` 只扫描可点击元素，遗漏关键状态

**代码位置**: [tools/__init__.py:711](tools/__init__.py#L711)

```python
nearby = [e for e in u.elements if e.clickable and e.label][:15]
```

**实际影响**（langchain log 证据）：
- 点击"计算器"后: `操作后页面: Calculator「10:13」 | 页面变化: DrawerLauncher → Calculator` ✅ 有用
- 点击"清除"后: `操作后页面: Calculator「10:13」` ❌ 只有页面标题，没有显示值变化

计算器显示屏是**非可点击的 TextView**，不会被 `e.clickable` 过滤到。清除按钮点击后的显示变化（如 "123" → "0"）无法被捕获。

#### 问题 2b：perceive 缓存不影响正确性

**已验证**: `perceive()` 用 `hashlib.md5(f"{xml}|{mode}")` 做缓存键（[perceiver.py:119](device/perceiver.py#L119)）。XML 是每次 `dump_hierarchy()` 新鲜获取的，点击后 UI 变化 → XML 变化 → sig 不同 → 缓存 miss。5 秒 TTL 不影响正确性。

### 3. 重复操作检测 → ✅ 通过

代码逻辑正确，本次测试中未触发（Agent #1 只点了 2 个不同元素就 reached max_turns）。

### 4. 抑制 KeyError 日志 → ✅ 通过

最新 langchain log 中**零条** `KeyError('input')` 警告，确认 `langchain_core.callbacks.manager` 设为 ERROR 级别生效。

---

## 二、新发现的 Bug（来自最新日志）

### Bug 1：MAX_TURNS_EXHAUSTED 被当作"成功"上报 ⛔ 严重

**日志证据**（[app_20260617_101001.log:81-85](logs/app_20260617_101001.log#L81-L85)）：
```
Agent #1: Let me check the full display...
DONE: MAX_TURNS_EXHAUSTED
Agent #1 decision: DONE
Route: reporter (status=success, steps=1)
Reporter: status=success success=1 fail=0 continue=0
```

**根因**: Phase 1.1 引入的截断标记 `DONE: MAX_TURNS_EXHAUSTED` 被 `_detect_termination()` 解析为 `done=True`（匹配行首 `DONE:`），reporter 直接写入 `status=success`。

但实际上 Agent 只完成了"打开计算器 + 清除"，tan(30) 验证还没开始。**这不是成功，是未完成。**

**修改方案**:
- `_detect_termination()` 增加 `MAX_TURNS_EXHAUSTED` 检测，返回 `done=False, abort=True`
- 或在 agent_node 中检测到 truncation 时设置 `status="incomplete"` 而非 `success`

### Bug 2：HF 模型每次启动耗时 ~18 秒 🟡 性能

**日志证据**: lines 9-49，每次服务启动 ~40+ HTTP 请求下载/验证 BGE 模型文件。

**修改方案**: `data/vector_store.py` 中 ChromaBackend 初始化前，检查 `persist_dir` 是否已有 collection 文件。如果已有，跳过模型重新加载（目前 Chroma 每次都重新初始化 embedding function）。

---

## 三、`_post_click_snapshot` 优化方案

### 当前缺陷

只捕获 `clickable + label` 元素，遗漏非可点击的显示值（如计算器屏幕、设置项当前值）。

### 修改方案

```python
# tools/__init__.py _post_click_snapshot 函数

# 修改前（只扫描可点击）
nearby = [e for e in u.elements if e.clickable and e.label][:15]

# 修改后（增加非可点击但含关键文本的元素）
clickables = [e for e in u.elements if e.clickable and e.label]
# 追加所有含数字且非容器的叶子节点（计算器显示、设置值等）
text_leaves = [e for e in u.elements
               if not e.clickable and e.text
               and any(c.isdigit() for c in (e.text or ""))
               and e.role not in ("container", "text")
               and len(e.text or "") < 20]
nearby = (clickables + text_leaves)[:15]
```

同时增加 `wait_seconds(0.3)` 调用确保 UI 刷新完成。

---

## 四、修改清单（最终状态）

| # | 问题 | 文件 | 状态 |
|---|------|------|------|
| Bug 1 | MAX_TURNS_EXHAUSTED 假成功 | graph.py:106-107, graph.py:118-123 | ✅ 已修复（改 ABORT + finditer 取最后匹配） |
| Bug 2a | _post_click_snapshot 遗漏非可点击元素 | tools/__init__.py:711-718 | ✅ 已修复（增加 text_leaves） |
| Bug 2b | _post_click_snapshot 无等待 | tools/__init__.py:704 | ✅ 已修复（time.sleep(0.3)） |
| Bug 3 | HF 启动耗时 | vector_store.py:94-102 | ✅ 已修复（local_files_only 缓存检测） |

---

## 五、Review 结论准确性

| Review 结论 | 准确性 | 说明 |
|------------|--------|------|
| 精简 Prompt ✅ | 正确 | — |
| click 返回状态变化 ⚠️ | **部分正确** | 隐患存在，但根因不是缓存而是元素过滤范围不足 |
| 重复操作检测 ✅ | 正确 | — |
| 抑制 KeyError ✅ | 正确 | 已验证生效 |
| HF 缓存 — | 正确 | 每次启动仍有 ~40 请求 |
