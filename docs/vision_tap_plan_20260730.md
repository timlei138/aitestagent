# vision_tap 工具 + RAG 人工知识 实施计划

**核心原则**：代码负责"稳"（工具契约 + 事实），LLM 负责"活"（理解页面、决策调用）。
不做自动 fallback 特例补丁，只提供工具 + 写好 RAG 知识，让 LLM 自主判断何时使用。

> **Review 修订说明（2026-07-30 第一轮）**：初版经代码逐行核对，发现 4 处运行即报错的硬伤（①②③④）
> 及 4 处稳健性改进（⑤⑥⑦⑧），已全部修正到本文档。
>
> **Review 修订说明（2026-07-30 第二轮）**：新发现 3 处问题（问题一：缺 `ok` 检查会 KeyError；
> 问题二：Task 0 前提已失效；问题三：外层 `except` 语义不匹配），已在本文档同步修正。

---

## ✅ Task 0（已完成/关闭）：确认 vision_model 是有效的视觉模型

~~**问题**：`config.yaml` 当前 `vision_model: qwen3.7-flash` 不是 DashScope 视觉模型，
已有日志证实调用必然 `APITimeoutError` 超时。~~

**现状（已验证）**：vision 调用已确认可用，日志出现 `[vision-invoke] OK elapsed=Xs`。
Task 0 关闭，无需重复操作。后续执行从 Task 1.5 开始。

---

## Task 1.5：`device/controller.py` 新增 `click_xy`

```python
def click_xy(self, x: int, y: int) -> None:
    """单点坐标点击，供 vision_tap 等需要精确坐标的工具调用。"""
    self.device.click(x, y)
```

> **为何要加**：初版写 `ctx.device.device.click(x, y)` 有两处错误：
> ① `ctx.device` 是 `DeviceController`，底层 `self.device`（uiautomator2）不对外暴露；
> ② `DeviceController` 无单点 `click(x,y)` 方法，公开只有 `click_bounds(tuple)`（controller.py:489）。
> 新增 `click_xy` 是有限且稳定的契约方法，不是特例补丁。

---

## Task 1：新增 `vision_tap` 工具

**文件**：`tools/perceive_tools.py`（末尾追加）

### 工具签名

```python
@tool
def vision_tap(description: str) -> str:
    """基于截图让 vision 模型定位目标区域并点击。
    专用于 Canvas 绘制、滚轮选择器等 view tree 无法访问的 UI 元素。

    description 示例：
      - "上课时长滚轮中显示数字 10 的那一行"
      - "休息时间滚轮中显示数字 5 的那一行"
      - "颜色选择器中紫色色块"
    """
```

### 内部流程（含 4 处硬伤修正）

```
1. device / vision 可用性检查：
   if ctx.device is None → ERROR
   actual_model = ctx.vision_model or ctx.llm_model   # 独立视觉模型优先，否则回退主模型
   if not (ctx.llm_vision_enabled and actual_model) → ERROR
   ④⑨ _run_multimodal_from_context 内部已做同样回退（tools/__init__.py:122-125）：
      vision_provider/model 为空时自动用主模型。
      因此主模型本身是多模态时，无需单独配 vision_model，流程完全一致。
      启用检查必须用 (ctx.vision_model or ctx.llm_model)，
      否则主模型多模态但未配独立 vision_model 时会误报 ERROR。

2. 截图 + 坐标系：
   snap = ctx.device.snapshot()
   W, H = snap.width, snap.height
   ④ 坐标系统一以截图为准（不用 ctx.device.screen_size，后者在 ToolContext 不在 DeviceController）。
   ⑥ 截图 W/H 与 vision 看到的图天然同坐标系，无需 scale 换算；加一行日志方便排查 density 问题。

3. 构造 prompt：
   prompt = (
       f"截图尺寸 {W}x{H}，坐标原点左上角。"
       f"找到「{description}」所在位置。"
       f'只返回 JSON: {{"x": int, "y": int, "reason": str}}，'
       f"x∈[0,{W}]，y∈[0,{H}]。"
   )

4. 调用（正确完整签名）：
   try:
       res = _run_multimodal_from_context(
           prompt,               # ② 必填位置参数（初版漏传 → TypeError）
           snap.image_base64,    # ② 必填位置参数
           purpose="locate_tap", # ② 必填位置参数
           strict_json=True,
           timeout_sec=30,
       )
   except Exception as e:
       # 注意：此 except 只捕获 _run_multimodal_from_context 本身的意外 bug
       # （如属性访问异常），不捕获 vision 调用失败——后者已在 multimodal.py
       # 内部 try/except 兜底，返回 {"ok": False, "error": ...} 而非抛异常。③
       return f"ERROR: 工具内部异常 {e}"

   # ⚠️ 必须先检查 ok，失败时 data={} → 直接取 data["x"] 会 KeyError 崩溃
   if not res.get("ok"):
       return f"ERROR: vision 调用失败 {res.get('error', res.get('reason', 'unknown'))}"

5. 解析 + 边界裁剪：
   data = res.get("data") or {}
   if "x" not in data or "y" not in data:
       return "ERROR: vision 返回格式无效（缺少 x/y 字段）"
   x = max(0, min(W, int(data["x"])))
   y = max(0, min(H, int(data["y"])))

6. 执行点击：
   ctx.device.click_xy(x, y)   # ③ 需 Task 1.5 先添加该方法

7. fail_streak 防卡死（⑦）：
   工具内部维护 _vision_tap_fail_streak（附在 ctx 上），连续 2 次失败（超时/格式错）
   → 返回 ERROR 并提示 LLM 回退 UI-tree 路径，防止 30s×N 拖垮用例。

8. 返回：
   OK: 已点击坐标({x},{y}) reason={reason}
   ERROR: vision 未启用，请配置 vision_model
   ERROR: vision 调用失败 {e}
   ERROR: vision 返回格式无效
   ERROR: vision_tap 连续失败，建议回退 UI-tree 路径
```

### 异常处理一览

| 情况 | 处理 |
|------|------|
| `ctx.llm_vision_enabled=False` 或 `ctx.vision_model or ctx.llm_model` 均为空 | `ERROR: vision 未启用，请配置 vision_model 或使用多模态主模型` |
| `res["ok"] == False` | `ERROR: vision 调用失败 {error}` （multimodal.py 内部已兜底，不抛异常，必须主动检查）|
| data 无 x,y 字段 | `ERROR: vision 返回格式无效（缺少 x/y 字段）` |
| 坐标越界 | 裁剪到 `[0,W]x[0,H]`，仍执行，返回值标注 `(已裁剪)` |
| 连续 2 次失败 | `ERROR: vision_tap 连续失败，建议回退 UI-tree 路径` |
| device 为 None | `ERROR: 未连接 Android 设备` |

---

## Task 2：注册工具到 agent

**文件**：`tools/__init__.py`

```python
from tools.perceive_tools import vision_tap  # 已有其他 perceive_tools 导入，追加即可
```

在 `AGENT_TOOLS` 列表中追加 `vision_tap`。

---

## Task 3：更新 agent prompt

**文件**：`agents/prompts/agent.txt`（在"弹窗检测与视觉工具"小节追加）

```
- **Canvas/滚轮等无障碍元素用 `vision_tap("描述目标位置")`**：
  当 view tree 中无文本/无 rid 的自绘控件，或 RAG 人工知识指示需视觉时，使用 vision_tap。
  （curated_rule 在首轮/App 切换/循环失败时已自动注入 prompt，无需每次主动 query_app_knowledge）
  描述要说清楚"哪个组件 + 目标值"，如："上课时长滚轮中值为10的那一行"、"颜色选择器中紫色色块"。
  vision_tap 成功后建议用 visual_check 确认值是否正确选中。
  若连续 2 次返回 ERROR，回退 UI-tree 路径，不要死循环。
```

> **修正**：初版写"click() 返回 NOT_FOUND 且 query_app_knowledge 提示才触发"——
> 实际 `curated_rule` 在 `rag_context.py:24` 首轮已自动注入（`query_curated_rules(top_k=20)`），
> Agent 不需主动调工具就能看到规则。

---

## Task 4：写入 RAG 人工知识

**关键**：`scenario` 不加 `auto_` 前缀（否则 `query_curated_rules` 的 `_non_auto` 过滤会丢弃）。

### 条目一：通用 Canvas 规则（scope=universal，对所有 App 生效）⑤

```python
kb.save_curated_rule(
    app_package="",        # 空串 = 通用规则
    content=(
        "Canvas 自绘控件（滚轮选择器/色块/图表等）的 view tree 中无可用文本节点和 resource-id，"
        "click(label) 会返回 NOT_FOUND。"
        "此类控件必须使用 vision_tap('描述目标位置') 操作；"
        "颜色/背景/状态校验使用 visual_check('描述')。"
    ),
    scope="universal",
    reviewed_by="dev",
    domain="ui_interaction",
    scenario="canvas_widget",
)
```

> 这条对所有 App 通用，未来遇到任何 Canvas 组件零成本受益，契合"契约收敛"原则。

### 条目二：联想日历专属规则（scope=app，承载非显然语义）

```python
kb.save_curated_rule(
    app_package="com.zui.calendar",
    content=(
        "TimeSlotSettingsActivity（课程时间设置）页面包含「上课时长」和「课间休息」两个滚轮选择器，"
        "均为 Canvas 绘制，view tree 无文本节点。"
        "滚轮显示 3 行：中间行 = 当前选中值，上方行 = 当前值-1，下方行 = 当前值+1；"
        "点击目标行即可选中对应值。"
        "操作示例：vision_tap('上课时长滚轮中值为50的那一行')"
    ),
    scope="app",
    domain="ui_interaction",
    scenario="canvas_widget",
    reviewed_by="dev",
)
```

---

## Task 5：写入 RAG 的脚本

**文件**：`scripts/seed_calendar_knowledge.py`

手动运行一次写入向量库，无需每次重复。后续有新 Canvas 组件，往同一脚本追加即可。

---

## 数据流（修正后）

```
Agent 进入 TimeSlotSettingsActivity
    ↓
RAG 首轮自动注入 curated_rule（rag_context.py:24 query_curated_rules）
  → "该页滚轮为 Canvas，需用 vision_tap（滚轮3行，中间=当前值）"
    ↓ LLM 自主决策（无需主动 query_app_knowledge）
vision_tap("上课时长滚轮中值为50的那一行")
    ↓ 工具层
snapshot() → W,H
→ _run_multimodal_from_context(prompt, snap.image_base64, "locate_tap", ...)
→ data={x, y, reason}
→ click_xy(x, y)
    ↓
OK: 已点击坐标(540, 320) reason=...
    ↓（LLM 自主决策是否校验）
visual_check("上课时长是否已显示50分钟")
```

---

## 不做的事（防止变成特例补丁）

- ❌ 不在 `click()` 里加"失败则自动改调 vision_tap"的 fallback
- ❌ 不在感知层检测 Canvas 并自动切换模式
- ❌ 不针对联想日历写任何硬编码特例（RAG 知识是数据，不是代码）
- ✅ 契约方法（`click_xy`）+ 工具（`vision_tap`）+ RAG 知识 + prompt，让 LLM 自主判断

---

## 实施顺序

1. ~~**Task 0**~~（✅ 已完成，跳过）
2. **Task 1.5**：`device/controller.py` 新增 `click_xy`
3. **Task 1**：`tools/perceive_tools.py` 新增 `vision_tap`
4. **Task 2**：工具注册（`tools/__init__.py`）
5. **Task 3**：`agents/prompts/agent.txt` 补充说明
6. **Task 5**：`scripts/seed_calendar_knowledge.py`（通用规则 + 日历专属规则）
7. 运行脚本写入 RAG → 重启服务验证
