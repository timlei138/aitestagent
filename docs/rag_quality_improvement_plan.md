已确认（2026-07-03）：用户批准按此计划执行。

## Plan v3: RAG质量与决策稳定性提升（Review修订版）

### 背景结论
最新日志复盘显示当前问题不是单点失效，而是“RAG相关性不足 + Agent重复行为 + 多模态兼容失败”的叠加。Review 已识别 2 个致命风险会导致原 P0 步骤失效，故升级为 v3：先修致命缺陷，再推进召回优化。

### 已核验事实
1. 全局知识污染存在：图库任务被注入了与计算器/桌面模式相关的全局知识。
2. 操作经验跨 App 泄漏机制存在：experience 的 Layer2 语义兜底无 app_package 约束。
3. Agent 重复循环存在：right_select_image/item_layout/selection_cancel 序列重复，最终 exhausted。
4. 多模态 400 大量存在：image_url 兼容错误导致视觉能力持续失败。
5. query_app_knowledge 并非“从未调用”：初次 run 为 0 次，resume run 为 2 次，但收益不足。
6. _is_unsupported_error 当前关键词无法命中 DeepSeek 典型报错（unknown variant 'image_url'）。
7. 现有外层重复检测位置过晚，不能阻止 _run_agent 内层 turn 被耗尽。

### 优先级与执行步骤

#### P0（最高优先级，先做）
1. P0.1 多模态能力开关（前后端联动）
2. 前端设置新增模型级多模态开关（vision_enabled），用户可显式关闭不支持视觉的模型。
3. 后端在视觉调用入口做硬拦截：vision_enabled=false 时直接返回 unsupported，不发起视觉请求。
4. 配置读写链路扩展并持久化，避免仅前端展示开关而后端仍误调用。
5. 本次同批次交付：后端开关与硬拦截 + 前端开关同步上线，确保配置与执行行为一致，直接阻断 400。

6. P0.2 多模态不支持识别修复
7. 扩展 _is_unsupported_error 关键词：unknown variant、image_url、expected 'text' 等 provider 通用特征。
8. 增加连续 N 次探测失败自动标记 unsupported 的兜底计数器，防止 provider 文案漂移。
9. 增加 provider 差异化识别分支（至少覆盖 deepseek/zhipu/openai-compatible）。

10. P0.3 Agent 内层循环断路器
11. 断路器必须放在 _run_agent 内部（_inc/_tools_node/_limit 链路），不能只放外层 agent_node。
12. 连续 N 次相同工具 name+args+页面签名时，直接触发 END 并输出 loop_break_reason。
13. 页面签名定义：activity + page_title + visible_labels_hash（当前可见可点击元素标签集合的稳定 hash），避免仅 activity 导致误判。
14. 增加观测字段：loop_detected、loop_pattern、loop_break_action。

15. P0.4 上下文预算护栏
16. 设定强约束：每轮附加 RAG 文本 <= 500 tokens；loop 警告 <= 100 tokens。
17. 达到预算阈值时优先裁剪历史消息，再裁剪低相关 RAG 片段。

#### P1（高优先级，小改动高收益）
18. P1.1 curated_rule 召回收敛
19. 读取策略改为“app_package 精确优先”；全局规则仅在满足匹配条件时追加。
20. 过渡方案（P2 元数据未就位前）：先按规则文本中的 app 名称/包名关键词做轻量匹配与降权。
21. 升级方案（P2 元数据就位后）：切换到 domain/scenario 结构化标签匹配。
22. 对低相关全局规则降权，不进入 Planner 主上下文。

23. P1.2 experience 兜底收紧
24. Layer2 优先同 app 语义召回；仅在完全无结果时放开跨 app。
25. 相似度阈值先做分布实验再定值（避免拍阈值）。
26. 返回结果附带 source_scope（same_app/cross_app）用于评估。

27. P1.3 RAG 注入频率降载
28. 不做“内层每 turn 注入”；改为 _run_agent 入口注入一次轻量摘要。
29. 工具级 query_app_knowledge 作为补充路径，不承担主检索职责。

30. P1.4 图库冷启动知识
31. 补齐 com.zui.gallery 的 curated_rule 与 experience 种子（多选入口、选中提示、退出路径）。
32. 种子知识写入时带 last_verified_at、app_version 元数据，降低后续陈旧污染风险。

33. P1.5 变更冲突管理
34. 明确 P0/P1 冲突文件与函数，尤其是 agents/graph.py 的 _run_agent 区域，采用串行合入策略。

#### P2（中低优先级，体系化建设）
35. 元数据治理
36. 引入 domain/scenario/quality_score/last_verified_at，并对旧数据分级降权。

37. 历史噪声清理
38. 基于“最近 N 次是否被采用”做清理，不再仅凭主观“过期/无关”。

39. A/B 验证与灰度发布
40. 第一阶段采用代理指标（turn/token/耗时）快速筛选；关键里程碑再做完整 10 次/任务 A/B。
41. 达阈值灰度发布，未达阈值回滚并保留观测数据继续调参。

### 实施时间线（估算）
1. P0.1（后端开关/硬拦截）+ P0.2：1.0-1.5 人天。
2. P0.3 + P0.4：1.0-1.5 人天。
3. P1.1 ~ P1.3：1.5-2.0 人天。
4. P1.4 ~ P1.5：0.5-1.0 人天。
5. P0.1（前端开关 UI）与后端开关同批次交付，额外投入约 0.5 人天。
6. P2：按里程碑滚动推进。

### 回滚策略
1. 所有 P0/P1 改动使用 feature flag（vision_enabled、loop_breaker_enabled、rag_prefetch_enabled）。
2. 任一项上线后若失败率上升，优先开关回退，不做热修拼接。
3. 回滚后保留完整观测日志用于二次修复。

### Relevant Files
- d:/Project/python/AiAgentTest/logs/runs/144756_test-20260703_144718_resume_langchain.log
- d:/Project/python/AiAgentTest/logs/runs/144718_test-20260703_144718_langchain.log
- d:/Project/python/AiAgentTest/logs/service.log
- d:/Project/python/AiAgentTest/llm/multimodal.py
- d:/Project/python/AiAgentTest/agents/graph.py
- d:/Project/python/AiAgentTest/api/config_routes.py
- d:/Project/python/AiAgentTest/config.py
- d:/Project/python/AiAgentTest/tools/__init__.py
- d:/Project/python/AiAgentTest/data/knowledge.py
- d:/Project/python/AiAgentTest/frontend/spa/src/App.vue

### Verification（更新版）
1. vision_enabled=false 时，多模态请求数=0，且无 image_url 400。
2. vision_enabled=true 且模型不支持时，最多首次探测失败 1 次，后续均走 unsupported 快速回退。
3. 多模态 400 错误较基线下降 >= 95%。
4. Agent 重复循环次数较基线下降 >= 50%，exhausted 比例下降 >= 40%。
5. 无关全局规则占比 < 20%。
6. query_app_knowledge 返回包含 app 专属经验或规则的比例 >= 70%。
7. 报告与日志中新增观测字段完整率 = 100%。
8. 单次 run 中 budget_violation 次数 = 0（即 RAG 注入和 loop 警告均未超限）。

### Metrics Definition（补充）
1. Token 计量口径
2. 统计对象：每次 run 的 llm 请求总和，来源优先取 provider 返回的 usage（prompt_tokens、completion_tokens）；缺失时用统一 tokenizer 离线估算。
3. 统一定义：total_tokens = input_tokens + output_tokens。
4. RAG 注入 token：rag_injected_tokens = 本轮注入 RAG 文本的 token 数（从 provider usage 差值或文本长度估算）。
5. 预算判定：每轮 rag_injected_tokens <= 500，loop_warning_tokens <= 100；超过即记为 budget_violation。

6. Verification 指标 A（对应原第 4 条前半）
7. loop_repeat_drop = (baseline_repeat_count - current_repeat_count) / baseline_repeat_count，目标 >= 50%。

8. Verification 指标 B（对应原第 4 条后半）
9. exhausted_drop = (baseline_exhausted_rate - current_exhausted_rate) / baseline_exhausted_rate，目标 >= 40%。

### Decisions
- 当前结论：RAG质量不合格，且优先级应先解决“多模态识别修复 + 内层循环断路器”。
- 实施顺序：P0 -> P1 -> P2，避免先做重治理再回头补救高频失败。
- 范围内：RAG检索、注入、观测、Agent收敛控制、多模态能力治理（含前端开关+后端硬拦截）。
- 范围外：设备底层驱动稳定性、前端交互重构。

### Further Considerations
1. 保留本文件作为 v3 主计划，后续改动按版本号递增。
2. 先建立 50 条相关性金标样本，再逐步引入自动化评估。
3. 灰度按 app 维度推进（先图库/设置），避免全量风险扩散。
4. 所有“依赖 P2 元数据”的 P1 条目必须提供过渡策略，避免执行顺序倒挂。

### Addendum（2026-07-03）
1. 本计划继续作为 v3 主计划，执行层补充采用：`docs/rag_execution_gap_closure_plan_20260703.md`。
2. 自本补充生效后，P0.3 的验收口径从“是否接入断路器”升级为“是否拦截多样化无效操作（页面停滞/空转）”。
3. 自本补充生效后，P1.1 的验收口径从“是否有过滤逻辑”升级为“无关全局知识注入占比 < 20% 且日志可解释（含评分与丢弃原因）”。
4. 自本补充生效后，turn 预算采用“基础公式收紧”评估，不再仅以 MAX_TURNS_EXHAUSTED 作为终止兜底。
5. 本轮以“轻设计”执行：不新增 feature flag，不引入并行补丁链；优先最小改动闭环，失败时通过 git revert 回退。