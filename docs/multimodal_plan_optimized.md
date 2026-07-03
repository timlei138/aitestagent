# 多模态改造清单（OpenAI-first）

## 目标

将当前分散的视觉能力收敛为“主 LLM + 统一视觉入口 + UI Tree 优先”的实现，移除独立 VLM 配置面，降低维护成本。

## 范围

- 保留主 LLM 配置
- 保留 `perception_mode`
- 保留 UI Tree 解析、已存在工具 `find_element()`、已存在工具 `switch_perception_mode()`
- 新增或抽象统一视觉 helper
- 移除前端和后端暴露的独立 Vision 配置

## 非目标

- 不新增第二套视觉 provider 抽象
- 不引入新的多模态框架依赖
- 不在应用启动/初始化阶段做在线可用性探测；允许首次视觉调用时做懒探测
- 不调整与多模态无关的 RAG、存储、报告逻辑

## 当前问题清单

- 配置层同时存在主 LLM 与独立 Vision 配置，字段重复，契约分裂。
- 前端设置页直接暴露 Vision 配置，和后端的收敛方向不一致。
- `SmartPerceiver`、`find_element()`、`switch_perception_mode()` 已经存在视觉相关逻辑，但调用入口不统一。
- `llm/clients.py` 中仍保留文本/视觉分离抽象，当前阶段维护收益低。

## 工具现状基线

- 已存在：`find_element()`、`switch_perception_mode()`
- 需新建：`visual_check()`、`detect_overlay()`
- 本次改造不是“4 个工具都重写”，而是“2 个新增 + 2 个既有工具接入统一视觉链路”

## 改造原则

1. 优先收敛配置契约，再改运行时行为。
2. 视觉能力必须通过单一 helper 进入，不允许在多个位置重复拼装消息。
3. 初始化过程保持轻量，不做网络探测或 provider 探活。
4. 旧配置允许被忽略，但不作为新契约继续传播。
5. 先保证行为兼容，再做清理删除。

## 执行清单

### 1. `config.py`

- 删除 `vision_provider`、`vision_model`、`vision_api_key`、`vision_base_url`
- 保留 `perception_mode`
- 调整 `from_yaml()`：加载旧配置时忽略废弃 vision 字段
- 删除 Vision 凭证回退链（`VISION_API_KEY` / `VISION_BASE_URL` 及 zhipu->vision 回填）
- 删除 `_log_provider_summary()` 中 `[vision] ...` 日志块
- 调整 `resolve_perception_mode()`：不再返回独立 `VLMClient`
- 明确 `perception_mode` 支持值仅保留 `ui_tree`、`hybrid`
- 删除 `mode == "vision"` 分支

### 2. `api/config_routes.py`

- 从 `_EDITABLE_FIELDS` 中移除 vision 字段
- 从 `_SECRET_FIELDS` 中移除 `vision_api_key`
- 从 `ConfigUpdateRequest` 中移除 vision 字段
- 更新 `get_config()` 返回结构，前端不再看到 vision 配置
- 更新 `update_config()` 的重建条件，只关注 `perception_mode`
- 更新 `_save_yaml()`，避免把废弃 vision 字段继续写回配置文件

### 3. `api/server.py`

- 将 `SmartPerceiver` 初始化改为只接收主 LLM 侧的多模态能力封装
- 重连设备时使用同一初始化路径，避免 server 和 reconnect 两套逻辑分叉
- 保证 `ToolContext` 构建不依赖独立 VLM 配置
- 在 `ToolContext` 注入主 LLM 运行时凭证（provider/model/api_key/base_url）供视觉 helper 复用

### 4. `main.py`

- 对齐 `api/server.py` 的初始化方式
- CLI 场景与 server 场景保持同一套配置解析逻辑
- CLI 初始化时同样向 `ToolContext` 注入主 LLM 运行时凭证

### 5. `tools/__init__.py`

- 不在 `tools/__init__.py` 内实现视觉核心逻辑，仅调用共享 helper
- `visual_check()` 调用统一 helper，不直接依赖独立 VLM client
- `detect_overlay()` 调用统一 helper，不直接依赖独立 VLM client
- `find_element()` 的 Vision fallback 复用统一 helper
- 保持 `switch_perception_mode()` 作为运行期模式切换入口
- 将 `visual_check()`、`detect_overlay()` 注册进 `AGENT_TOOLS`
- 明确工具返回格式，避免 Agent 只能读自然语言
  - `visual_check()` 返回可解析结构，至少包含 `decision`、`reason`、`evidence`
  - `detect_overlay()` 返回可解析结构，至少包含 `has_overlay`、`overlay_type`、`reason`
- 返回值采用稳定的文本/JSON 约定，禁止同一工具在不同场景输出不同结构

### 5.1 新模块落点

- 统一视觉 helper 放在 `llm/multimodal.py`
- 禁止放在 `tools/__init__.py`（文件过大，且易形成依赖耦合）
- `device/perceiver.py` 直接依赖 `llm/multimodal.py`
- `tools/__init__.py` 只做工具包装，调用 `llm/multimodal.py`

### 5.2 Agent prompt 补充

- 更新 `agents/prompts/agent.txt`，显式告诉 Agent 什么时候用视觉工具
- 在 prompt 中明确以下使用边界：
  - 需要判断颜色、布局、Toast、遮挡、弹窗、图标状态时，优先调用 `visual_check()` 或 `detect_overlay()`
  - 纯结构导航、点击、输入、页面跳转优先用 `get_screen_info()`
  - 不确定 UI Tree 是否足够时，先看 `get_screen_info()`，再决定是否调用视觉工具
- 在 prompt 中明确输出规范，让 Agent 知道视觉工具返回的是可程序化判断结果，而不是仅供人工阅读的描述

### 6. `device/perceiver.py`

- `SmartPerceiver.__init__()` 移除 `VLMClient` 类型耦合
- `SmartPerceiver` 改为依赖通用视觉调用接口（推荐注入 callable 或调用 `llm/multimodal.py`）
- `_vision_describe()` 改为调用统一视觉 helper
- 保留 `ui_tree` / `hybrid` 分层逻辑
- 保留截图缓存与结果缓存
- 不在 `__init__()` 中做在线能力探测
- 补充 `hybrid` 的行为定义：
  - 默认先走 UI Tree
  - 只有在页面理解不完整、目标元素无法稳定定位、或 UI Tree 明显卡住时才触发视觉 helper
  - 视觉结果只作为补充信息，不替代 UI Tree 的主路径

### 6.1 `assert_verification()` 增强

- 扩展 `assert_verification()`，让失败项可以附带视觉分析结果
- 当 `result == "failed"` 且存在最近截图时，顺手调用统一视觉 helper 做一次自动分析
- 将视觉分析结果写入 `detail`，便于报告和回放时快速定位失败原因
- 失败截图仍然保留，但不再只是“人工看图”用途，要同时成为自动诊断输入

### 7. `frontend/spa/src/App.vue`

- 删除 Vision 独立配置区
- 只保留 `perception_mode` 控制项
- 删除 `value="vision"` 的选项
- 将前端的“多模态开关”映射为：
  - 开启多模态 = `hybrid`
  - 关闭多模态 = `ui_tree`

### 8. `llm/clients.py`

- 保留文本 client 作为主路径
- `VLMClient`、`OpenAIVisionClient`、`ZhipuVisionClient` 先标记为废弃
- 若后续确认没有保留必要，再删除 vision client 相关实现

## 依赖关系

1. 先改配置层，再改调用层。
2. 先抽统一视觉 helper，再切换 `tools` 和 `perceiver`。
3. 前端最后改，避免 UI 先于后端契约变更。
4. `llm/clients.py` 最后清理，确保行为稳定后再删代码。

5. `PerceptionMode.VISION` 清理需跨文件同步：
  - `device/perceiver.py` 删除常量定义
  - `config.py` 删除 `mode == "vision"` 分支
  - `frontend/spa/src/App.vue` 删除 `vision` 下拉选项

## 具体行为约定

### 1. Agent prompt 与工具使用

- `get_screen_info()` 负责结构化页面理解，默认优先使用。
- `visual_check()` 负责判断“是否满足某个视觉描述”。
- `detect_overlay()` 负责判断“是否存在弹窗、Toast、浮层、遮挡”。
- Agent 只有在需要视觉语义时才调用视觉工具，不能默认每步都看图。

### 2. 视觉工具输出格式

- `visual_check()` 推荐输出：`{"decision":"yes|no","reason":"...","evidence":"..."}`
- `detect_overlay()` 推荐输出：`{"has_overlay":true|false,"overlay_type":"...","reason":"..."}`
- 如果底层实现先返回文本，也必须保证前缀和字段顺序稳定，方便后续结构化解析。

### 3. 懒检测与回退

- 懒检测只在第一次真正调用视觉 helper 时触发。
- 检测内容只关注“当前主 LLM 是否接受图片消息并返回可用结果”。
- 如果检测失败，则将结果缓存为“不支持多模态”，后续直接回退到 `ui_tree`。
- 回退后不在每次调用时重复探测，避免反复打接口。

### 4. `hybrid` 的改造后语义

- `hybrid` 不再表示“每次都 UI Tree + VLM 标注”。
- `hybrid` 表示“UI Tree 为主，视觉按需补充”。
- `_vision_describe()` 只在页面卡住、UI Tree 不足或调用方明确要求视觉补充时触发。
- 这一点要在文档和 prompt 中同时写清楚，避免团队误解为旧行为延续。

## 关键实现细节

### 1. 统一视觉 helper 契约

建议在 `llm/multimodal.py` 新增单一入口函数，供 `tools/__init__.py` 和 `device/perceiver.py` 共用。

建议函数签名：

- `multimodal_vision_call(prompt: str, image_base64: str, purpose: str, strict_json: bool = True) -> dict`

凭证注入方案（本次固定实现）：

- 采用 `ToolContext` 注入主 LLM 运行时凭证，helper 从 `ToolContext` 读取
- `ToolContext` 新增字段：
  - `llm_provider`
  - `llm_model`
  - `llm_api_key`
  - `llm_base_url`
- `api/server.py` 与 `main.py` 在构建 `ToolContext` 时一次性注入
- 配置热更新后重建 `ToolContext`，保证 helper 始终读取最新凭证

备选方案记录：

- 全局 config 直读：实现简单，但耦合过高，不选
- 函数参数显式传凭证：解耦最好，但调用面改动大，本阶段不选

输入约束：

- `prompt`：业务问题描述
- `image_base64`：PNG/JPEG 的 base64
- `purpose`：调用用途，取值建议 `visual_check`、`detect_overlay`、`perceiver_annotate`、`verification_fail_analyze`
- `strict_json`：是否强制 JSON 输出

输出约束（统一字典结构）：

- `ok: bool`
- `capability: "supported" | "unsupported" | "unknown"`
- `decision: str`
- `reason: str`
- `evidence: str`
- `raw: str`
- `error: str`

实现要求：

- helper 内部负责组装带图消息
- helper 内部负责重试、异常归一化、结构化解析
- 调用方不再各自处理 provider 差异

探测请求最小模板（避免误判）：

- 文本：`请只返回 JSON: {"decision":"yes","reason":"ok"}`
- 图片：1 张当前截图
- 判断标准：
  - 请求被明确拒绝为不支持图像 => `unsupported`
  - 返回成功且可解析 => `supported`
  - 超时/限流/网关错误 => `unknown`

### 2. 懒探测状态机

建议在 `ToolContext` 或 helper 模块维护以下状态：

- `vision_capability_state`: `unknown | supported | unsupported`
- `vision_capability_checked_at`: 时间戳
- `vision_capability_error`: 最近一次失败原因

流程：

1. 初始状态为 `unknown`。
2. 首次调用视觉 helper 时执行最小探测请求。
3. 探测成功：状态置为 `supported`，继续正常视觉调用。
4. 探测失败且命中“不支持图像”类错误：状态置为 `unsupported`。
5. 后续调用若状态为 `unsupported`，直接返回回退结果，不再重复探测。
6. 可选增加 TTL，例如 30 分钟后允许一次重新探测。

错误分类建议：

- 归类为 `unsupported`：模型不支持图像输入、接口明确报 `image not supported`。
- 归类为 `unknown`：网络超时、网关故障、限流。
- 归类为 `supported`：返回可解析结果。

### 3. `visual_check` / `detect_overlay` 返回协议

`visual_check(description)` 返回：

- `decision`: `yes | no | unknown`
- `reason`: 一句话原因
- `evidence`: 可定位信息（文本片段、位置描述）
- `confidence`: `high | medium | low`

`detect_overlay()` 返回：

- `has_overlay`: `true | false`
- `overlay_type`: `toast | dialog | popup | sheet | unknown | none`
- `reason`: 一句话原因
- `evidence`: 识别依据
- `blocking`: `true | false`（是否可能阻断后续点击）

建议统一序列化为 JSON 字符串返回，便于 Agent 和后处理程序稳定解析。

### 4. `assert_verification` 失败自动分析

触发条件：

- `result == "failed"`
- 存在最近截图路径或最近截图 base64

执行步骤：

1. 读取失败截图。
2. 构造分析提示词：
  - 验证项是什么
  - 预期结果是什么
  - 当前可能失败表现是什么
3. 调用统一视觉 helper（`purpose=verification_fail_analyze`）。
4. 将分析摘要拼接到 `detail`，并写入 `_verifications`。

性能与降级策略：

- 默认仅对 `failed` 项做视觉分析，不对 `passed` 项调用
- 单次视觉分析超时建议 8-12 秒，超时即跳过，不阻塞主流程
- 若 `capability == unsupported`，后续失败项不再重复调用视觉分析
- 可选增加开关：`verification_auto_vision: on/off`（默认 on）
- 若后续需要更快报告，可改为异步后台补充 detail（当前阶段先同步 + 短超时）

写入 detail 建议格式：

- 原 detail 不为空：`{原detail} | vision={decision}: {reason}`
- 原 detail 为空：`vision={decision}: {reason}`

### 5. `SmartPerceiver` 触发视觉补充条件

建议把“是否调用 `_vision_describe`”改成可读的判定函数：

- `should_use_vision(understanding, mode, stuck_count, intent_hint) -> bool`

建议触发条件（任一满足）：

- `mode == hybrid` 且 `stuck_count >= threshold`
- 页面元素数量异常少或关键元素解析为空
- 调用方传入 `intent_hint` 表示需要视觉语义（例如颜色、布局、遮挡）

建议明确不触发条件：

- 普通点击、输入、滚动后可通过 UI Tree 明确定位
- 页面结构稳定且最近视觉结果未过期

接口改造约定：

- `SmartPerceiver.__init__()` 不再接收 `VLMClient`
- 新签名建议：
  - `vision_call: Callable[[str, str, str, bool], dict] | None`
- `_vision_describe()` 通过 `vision_call(...)` 调用统一 helper
- `vision_call` 为空时，`hybrid` 自动退化为仅 UI Tree

### 5.1 `find_element()` fallback 改造

- 删除 `ctx.perceiver.mode = "vision"` 的切换逻辑
- 改为：
  - Phase 1 仍走 `ui_tree`
  - Phase 2 调用 `visual_check`/统一 helper 进行定向视觉补充
- `switch_perception_mode()` 仅保留 `ui_tree`、`hybrid`，移除 `vision`

### 6. Prompt 级约束（防止 Agent 误用）

在 `agents/prompts/agent.txt` 补充硬性规则：

- 默认先 `get_screen_info`，只有视觉语义需求才调用视觉工具
- 连续两次视觉工具都返回 `unknown` 时，必须回退 UI Tree 路径或 ABORT
- 遇到 `detect_overlay.blocking=true` 时优先处理遮挡，再继续主流程

### 7. 最小测试用例清单

后续实现完成后，至少验证以下场景：

1. 主模型支持图像：首次探测成功，`visual_check` 正常返回结构化结果。
2. 主模型不支持图像：首次探测失败并缓存，后续直接回退不再探测。
3. `assert_verification` 失败时自动附加视觉分析。
4. `hybrid` 模式下普通导航不触发视觉调用，卡页时触发。
5. 前端不再显示独立 Vision 配置，保存配置后后端契约一致。
6. 旧 YAML 含 vision 字段时启动不报错，且配置回读不再返回 vision 字段。
7. `config_routes.py` 中 `_SECRET_FIELDS` 不再包含 `vision_api_key`，更新配置不再读写该字段。
8. `find_element()` 在 `hybrid` 下仍有视觉补充能力，但代码中不再出现 `mode = "vision"` 切换。

## 阻塞项关闭说明

### A. helper 放置与注入

- helper 固定放 `llm/multimodal.py`
- 通过 `ToolContext` 注入主 LLM 运行时凭证
- 由 `api/server.py` / `main.py` 在上下文初始化时完成注入

### B. `SmartPerceiver` 取 LLM 能力

- 通过构造函数注入 `vision_call`（可调用对象）
- 不从 `tools` 反向获取，避免新增循环依赖

### C. `find_element()` 视觉 fallback

- fallback 不再依赖 `PerceptionMode.VISION`
- 改为直接调用统一 helper 做视觉补充判断

### D. 懒探测消息定义

- 使用固定最小探测模板（短文本 + 单图 + JSON 回答）
- 将能力判定与业务推理解耦，避免因业务提示词复杂导致误判

## 结合代码的额外优化点

1. `device/perceiver.py` 当前存在对 `tools` 的反向导入（用于写 `_last_screenshot_path`），建议改为回调注入或上下文方法，降低模块耦合。
2. `tools/__init__.py` 文件体积已较大，新增视觉工具时建议仅薄包装，核心逻辑全部放 `llm/multimodal.py`。
3. `switch_perception_mode()` 的入参校验需与前端下拉同步，只允许 `ui_tree`、`hybrid`，避免运行期写入非法旧值。

## 建议实施顺序

### Phase 1: 配置收敛

- 修改 `config.py`
- 修改 `api/config_routes.py`
- 验证旧 `config.local.yaml` 可被忽略

### Phase 2: 运行时收敛

- 修改 `api/server.py`
- 修改 `main.py`
- 抽统一视觉 helper
- 接入 `tools/__init__.py`
- 接入 `device/perceiver.py`

### Phase 3: 前端收敛

- 删除 Vision 配置区
- 仅保留 `perception_mode`
- 检查保存/回显字段是否与后端一致

### Phase 4: 代码清理

- 评估并标记 `llm/clients.py` 的 vision client 为废弃
- 在确认无引用后再删除

## 验收标准

- 启动后不再读取或暴露独立 Vision 配置。
- `config.yaml` 和 `config.local.yaml` 中残留的旧 vision 字段不会导致启动失败。
- `get_config()` 返回值不包含 vision 字段。
- 前端设置页不再展示独立 Vision 配置块。
- `find_element()`、`visual_check()`、`detect_overlay()` 统一走同一视觉入口。
- `SmartPerceiver` 仍可在 `ui_tree` 和 `hybrid` 模式下正常工作。
- `perception_mode` 变更后，重连设备或重建 perceiver 行为与预期一致。

## 风险与处理

- 旧配置残留：加载时忽略，不要求手工清理。
- 主 LLM 不支持多模态：第一次视觉调用失败时回退到 `ui_tree`，并缓存回退结果。
- 视觉入口不统一：只允许一个 helper 被工具层和感知层共同复用。

## 结论

这次改造的核心不是“增加新能力”，而是“收敛已有能力”。最终应落到以下状态：

- 一套主 LLM 配置
- 一个 `perception_mode`
- 一个统一视觉 helper
- UI Tree 优先，视觉按需启用
- 不再维护独立 VLM provider 抽象
