# Vision 工具链优化计划（二次评审修正版）

> **指导原则**：代码负责"稳"（契约/事实/基础设施），LLM 负责"活"（理解/决策/判断）。
> 凡是让"稳"收敛、让 LLM 更放得开的兜底就做；凡是让代码陷入无限特例补丁的就停下来换思路。
>
> **Review 修正说明（2026-08-03 第一轮）**：方案 A 新截图、方案 C evidence 反馈、方案 D 降级。
>
> **Review 修正说明（2026-08-03 第二轮）**：对照 langchain log 原始数据逐项核实，
> 发现 3 处数据不准确（F2 根因诊断错误 / vision 调用总数错误 / 裁剪图尺寸缺失），
> 其中 F2 根因错误导致优先级重排，已全部修正到本文档。

---

## 1. 现状总结（已完成）

| 项目 | 状态 | 效果 |
|---|---|---|
| 智能裁剪（view tree bounds → 裁原图） | ✅ | 坐标不再打到弹窗外，Y 精度从 ±129px → ±10px |
| `repeat=N` 批量连点 | ✅ | 53 次分钟连点仅 15 秒 |
| `click_and_check` 捕 toast | ✅ | 一次捕到"课程时间有冲突"toast |
| RAG 知识（6 条日历规则） | ✅ | LLM 正确使用 repeat、理解红色提示、理解间隔计算 |
| vision 模型升级 qwen3.7-pro | ✅ | X 精度从 ±145px → ±23px（多数列） |
| JPEG/PNG 自动检测 | ✅ | 已验证不影响精度，回退 PNG |
| 横屏坐标 swap 修复 | ✅ | 坐标变换链路正确 |

---

## 2. 剩余问题（二次评审修正版）

### P2→P0：模型对裁剪图右边缘有系统性空间偏差（真正的根因）

**现象**：模型对前 3 列定位准确（±23px），但第 4 列（最右）仍偏左 ~120px。

**Trace 证据**（105801 run，9 次 vision_tap 完整数据）：

| # | description 关键词 | 返回 x | dev_x | 结果 |
|---|---|---|---|---|
| 1 | "分钟列…50下方" | 800 | 1740 | 打到小时列 ❌ |
| 2 | "小时列…12上方…11" | 652 | 1592 | 修回小时 ✅ |
| 3 | "小时列(左数第3列)…12上方…11" | 652 | 1592 | 修回小时 ✅ |
| 4 | **"分钟列(第4列)…50正下方…51"** | **800** | **1740** | **又打到小时列 ❌** |
| 5 | "右半边分钟列…50正下方…51" | — | timeout | — |
| 6 | "弹出窗口最右侧一列…51那一行" | 920 | 1860 | 命中分钟列 ✅ |
| 7 | "小时列(左数第3列)…12上方…11" | 663 | 1603 | 修回小时 ✅ |
| 8 | "小时列…11下方…12" | 652 | 1592 | 第4节 ✅ |
| 9 | "小时列…11下方…12" | — | timeout | — |

**关键发现**：call #4 的 description **已经包含"第4列"列序号**，但模型仍返回 x=800（偏差）。call #6 换了描述方式（"弹出窗口最右侧一列"），模型才返回 x=920（正确）。

**根因**：不是"LLM 重复相同描述"，而是**模型对裁剪图右边缘的空间判断有系统性偏差**。即使给出列序号，模型仍可能返回偏差坐标。这是模型能力上限。

---

### F0：Vision 超时管理

**现象**：`vision_timeout: 45` 一刀切，3 次 timeout（call #5/#9 + 1 次 locate_tap）占 **135s**，是最大的单项时间浪费。

**数据**：locate_tap 平均 20.1s，visual_check 平均 14.4s。45s 超时意味着模型卡住时白等 25-30s。

---

### P1：Vision 调用次数过多

**修正数据**（二次评审核实）：

| 指标 | 值 |
|---|---|
| vision_tap | **9 次**（6 OK + 3 ERROR/timeout） |
| visual_check | **13 次** |
| click_and_check | **1 次** |
| **总计** | **23 次**（初版误写 21 次，漏算 3 次 ERROR） |
| 总 vision 耗时 | 348s = 31% 运行时间 |
| 平均每次 | 16.6s |
| 理想调用 | 5 次 ≈ 75s |
| 浪费 | **4.6x** |
| 3 次 timeout | **135s**（占总 vision 时间 39%） |

**裁剪图尺寸**：1160×1063 PNG ≈ 200KB/次（从 langchain log "截图尺寸 1160x1063" 提取）。图片较大，每次 vision 调用的 upload + 推理成本不低。

---

### F2：prompt 缺"失败后换描述"指导（降级为辅助改善）

**二次评审修正**：初版将 F2 判断为 P0 根因。实际 trace 数据显示，call #4 已包含"第4列"但模型仍返回偏差坐标。**F2 的修复（"禁止相同 description"）无法解决模型右边缘偏差问题**。

F2 仍然有价值（避免完全相同的 description 重试），但不是 P0 的根因修复。降级为**辅助改善**。

---

### P3：visual_check 单条件限制

**现象**：一次只能验证一个描述，多条件场景需多次调用。

---

### F1：_vision_tap_fail_streak 不区分错误类型

**现象**：timeout（临时性）和坐标偏差（系统性）都计入同一个 streak，2 次就触发"回退 UI-tree"。

---

## 3. 优化方案（二次评审修正版，按 ROI 排序）

### 方案 A：vision_tap 自带 verify 闭环（升为优先级 1）

**二次评审修正**：从优先级 3 升为 1。理由——**方案 A 是唯一能让 Agent 在运行时发现"坐标偏了"并自动调整策略的机制**。F2（prompt 指导）只能事前提醒，不能在运行时感知偏差。

**思路**：`vision_tap(desc, repeat=N, verify="分钟是否为53")` — 点击后用新截图验证，如果 verify 发现值没变，Agent 立刻知道坐标偏了，必须换策略。

**流程**：
```
screenshot1 → vision定位 → 坐标映射 → 点击 → sleep(0.3s 等动画) → screenshot2 → vision验证 → return
```

**实现**：
```python
@tool
def vision_tap(description: str, repeat: int = 1, verify: str = "") -> str:
    # ... 现有定位+点击逻辑 ...

    if verify:
        time.sleep(0.3)  # 等滚轮动画
        snap2 = ctx.device.snapshot_for_vision()  # ⚠️ 重新截图，不能用旧图！
        verify_prompt = f"请根据截图判断：{verify}。只返回JSON..."
        vr = _run_multimodal_from_context(verify_prompt, snap2.image_base64, ...)
        return make_result(OK,
            f"已点击({x},{y}) reason={reason}\n"
            f"verify=[{vr.get('decision')}] {vr.get('evidence')}\n"
            f"⚠️ 如果 verify 显示值未变，说明坐标可能偏差，请调整 description 后重试。"
        )
```

**verify 失败时的行为**：verify 结果直接包含在 tool result 中，Agent 看到"值没变"就知道要换 description。不需要额外代码逻辑——这就是"代码给事实，LLM 做判断"。

**真实节省**：
- 省 1 次 LLM 决策轮次 ≈ 5-8s
- **消除无效重试循环**：Agent 在第一次 verify 失败后就知道要换策略，而不是用同样坐标再打一遍 ≈ 省 ~100s+

---

### 方案 F0：Vision 超时分级（优先级 2）

**思路**：按 purpose 分级超时。

```python
timeout_map = {
    "locate_tap": 30,      # 平均 20s，超时即放弃
    "visual_check": 20,    # 平均 14s
    "click_and_check": 20,
}
```

**节省**：每次 timeout 省 15-25s，trace 中 3 次 timeout 可省 **~60s**。

---

### 方案 C：注入 evidence 反馈（优先级 3，与 A 互补）

**思路**：`vision_tap` 返回时附带上次验证的 evidence，给 Agent 更多诊断信息。

```python
return make_result(OK,
    f"已点击({x},{y}) reason={reason}\n"
    f"⚠️ 上次验证发现当前值仍为 {last_known_value}。"
    f"如果目标值未改变，请调整 description 使其更精确。"
)
```

`last_known_value` 来自 ctx 上缓存的上次 `visual_check` evidence，不是硬编码坐标。

---

### 方案 F2：prompt 补充换描述指导（降级为辅助改善）

**思路**：在 `agent.txt` 补充：

```
- vision_tap 返回 OK 后必须 visual_check 验证值是否改变。
  如果值未变，说明坐标定位有误，下次 vision_tap 必须使用不同的描述策略：
  - 指定列序号（"第4列"而非"最右列"）
  - 指定行位置（"选中行上方第一行"而非"数字08"）
  - 禁止用与上次完全相同的 description
```

**注意**：此方案无法解决模型右边缘偏差（call #4 已含"第4列"但仍偏差），只是辅助改善。

---

### 方案 B：visual_check 支持多条件

```python
@tool
def visual_check(description: str = "", conditions: list[str] = None) -> str:
```

---

### 方案 D：vision_tap 行列定位模式（终极解，需先做列探测）

**前提不成立**：view tree 中弹窗只有 2 个 primary_path（取消/确定），LinearLayout 容器在 all_elements 中但无语义标签。需要先做"列结构探测"子功能。

**可行路径**：
1. 扩展 `_find_dialog_crop_bounds` → 同时提取 `custom` 下的 LinearLayout 子节点 bounds（这些节点在 all_elements 中存在）
2. 或首次进入弹窗时用一次 vision 调用识别列结构，缓存到 ctx

---

### 方案 E：vision 模型分层（待 A/B 测试）

**二次评审补充**：裁剪图 1160×1063 PNG ≈ 200KB，图片较大。flash 处理此尺寸的图可能比 plus 快 **40%+**（不只是 30%），因为 flash 的图像编码器更轻量。

**前提**：A/B 测试 flash vs plus 在 visual_check 上的 decision 一致率 > 90%。

---

## 4. 修正后的实施优先级（二次评审版）

| 优先级 | 方案 | 根因 | 预估收益 | 难度 |
|---|---|---|---|---|
| **1** | A: verify 闭环 | P0 根因修复——点完立刻验证，发现偏差→Agent 被迫换策略 | 消除无效重试循环（**省 ~100s+**） | 低 |
| **2** | F0: 超时分级 | 3 次 timeout = 135s 纯浪费 | **省 ~60s** | 低 |
| **3** | C: evidence 反馈 | 与 A 互补，给 Agent 更多诊断信息 | 辅助改善 | 低 |
| **4** | F2: prompt 补充 | 辅助改善（不是根因） | 有限 | 极低 |
| **5** | B: 多条件 | 真实需求存在 | 省 14-28s/场景 | 低 |
| **6** | D: 行列定位 | 终极解（但需先做列探测） | 消除坐标偏差 | 高 |
| **7** | E: 模型分层 | 成本优化 | 待 A/B 测试 | 低 |

**核心变化**：方案 A 从优先级 3 升为 1——它是唯一能让 Agent **在运行时发现坐标偏差**并自动调整策略的机制。F2 只能事前提醒，不能运行时感知。

**建议先做 1+2**（verify 闭环 + 超时分级），然后做 3+4。

---

## 5. 不做的事

- ❌ 不在 vision_tap 里加"如果第一次失败就自动调整坐标重试"的逻辑（特例补丁）
- ❌ 不针对某个 App 硬编码列数/行数/坐标范围（RAG 知识是数据，不是代码）
- ❌ 不在 visual_check 里加"如果 toast 没捕到就等 1 秒再试"（click_and_check 已解决）
- ❌ 不用点击前的旧截图做 verify（方案 A 致命错误，必须用新截图）
- ✅ 方案 D 的行列定位是新的调用模式（契约收敛），不是"如果 X 列偏了就自动偏移"的补丁

---

## 6. 验证标准

| 指标 | 当前基线 | 目标 | 说明 |
|---|---|---|---|
| vision 调用总次数 | **23 次**（9 tap + 13 check + 1 cac） | ≤ 10 次 | 二次评审修正 |
| vision 总耗时 | 348s | ≤ 150s | |
| vision_tap timeout 次数 | 3 次/运行 | 0 次 | 超时分级后不应出现 |
| Agent 使用相同 description 重试 | 2 次 (call #1/#4) | 0 次 | verify 闭环后应换策略 |
| 最右列 X 偏差 | ~120px | verify 发现偏差后 Agent 自动调整 | |
| 测试通过率 | passed | passed | |
