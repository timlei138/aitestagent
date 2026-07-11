# 大文件拆分重构计划（2026-07-11）

> 目标：按「单一职责 / 关注点分离」把超大文件拆成内聚的小模块，提升可维护性，且**不破坏现有对外接口**。
> 原则：保持 `from tools import ...`、`from agents.graph import build_graph/set_relational_db` 等现有 import 路径不变（靠包 `__init__.py` 再导出），做到「内部重组、外部无感」。
> 约束：分批推进，**每批次后跑一次 pytest 再进下一步**；与 `llm_native_architecture_migration_20260709.md` 的 legacy 下线协调。

---

## 1) 现状：文件规模盘点

| 文件 | 行数 | 结论 |
|---|:--:|---|
| `tools/__init__.py` | **2752** | 远超警戒线，**必须拆**（P0） |
| `agents/graph.py` | **1945** | 超标，**建议拆**（P1） |
| `device/perceiver.py` | 888 | 偏大，可选（P2） |
| `api/server.py` | 672 | 可接受（单一职责，暂不动） |
| `data/relational.py` | 672 | 可接受（DB 层单一职责，暂不动） |
| `agents/orchestrator.py` | 542 | 可接受（暂不动） |
| `tools/context.py` | 53 | 已是独立模块 |

> 参考标准：Python 无硬性行数规定，工程通行经验值为单文件 ≤500 行、单函数 ≤50~80 行。问题的本质不是行数，而是**单文件承载了过多不同职责**。

---

## 2) 目标一：`tools/__init__.py`（2752 行）→ 拆成包

按职责把当前 70+ 个函数重新归类到内聚模块。

| 新模块 | 职责 | 迁入函数（当前签名清单） | 依赖 |
|---|---|---|---|
| `tools/context.py`（已存在） | 工具上下文 + 全局存取 | `ToolContext`；**新迁入** `set_tool_context` / `get_tool_context`（现在 __init__.py L27-33） | 无 |
| `tools/text_utils.py` | 纯文本工具（**首批，零依赖**） | `_normalize_text`、`_has_cjk`、`_cjk_char_overlap`、`_expand_zh_keywords` | 无 |
| `tools/element_match.py` | **legacy 启发式匹配簇**（隔离便于将来整体下线） | `_score_element`、`_pref_bonus_for_element`、`_prefs_active_for_description`、`_disambiguate_container`、`_rank_click_candidates`、`_find_best_element_with_known`、`_promote_to_clickable_parent`、`_exact_clickable_candidates`、`_is_target_consistent`、`_is_expected_destination`、`_rid_matches`、`_search_elements`、`_extract_click_preferences_from_rag` | text_utils、context |
| `tools/click.py` | 点击动作及直接辅助 | `click`、`_check_switch_state`、`_format_click_log`、`_capture_page_id`、`_post_click_snapshot`、`_record_page_transition`、`_maybe_promote_exact_rule`、`_extract_curated_rule_label`、`_try_click_by_associated_label`、`navigate_to`、`scroll_find_and_click`、`long_press`；身份类 `_query_known_identities`、`_score_known_identity`、`_save_click_identity`、`_compute_page_signature`、`_query_known_by_rid`、`reset_session_click_ids` | element_match、context、text_utils |
| `tools/perceive_tools.py` | 页面感知类工具 | `get_screen_info`、`_format_element_line`、`find_element`、`visual_check`、`detect_overlay`、`detect_popup`、`dismiss_popup`、`check_page_health`、`recover_from_anomaly`、`switch_perception_mode`、`_has_meaningful_ui_elements`、`_run_multimodal_from_context` | context |
| `tools/device_ops.py` | 设备动作 | `copy`、`paste`、`type_input`、`press_key`、`swipe`、`open_notification`、`open_quick_settings`、`unlock_screen`、`set_orientation`、`toggle_auto_rotate`、`check_desktop_mode`、`scroll_panel`、`launch_app`、`wait_seconds` | context |
| `tools/knowledge_tools.py` | 知识查询 | `query_app_knowledge`、`_experience_relevance`、`query_element_identity` | context |
| `tools/verify.py` | 验证与终止 | `assert_page_contains`、`assert_element_exists`、`assert_verification`、`_resolve_verification_key`、`_normalize_verification_text`、`report_done`、`log_step`、`save_screenshot` | context |
| `tools/__init__.py`（瘦身后） | **只做组装** | `@tool` 装饰器 shim、`AGENT_TOOLS` 聚合列表、再导出各工具与 `set/get_tool_context` | 以上全部 |

**收益**：`element_match.py` 单独隔离后，`llm_native_architecture_migration` 里计划下线的启发式代码将来基本就是「删一个文件」，不必在 2752 行里逐个定位。

---

## 3) 目标二：`agents/graph.py`（1945 行）→ 按职责拆（agents 已是包）

| 新模块 | 职责 | 迁入函数 | 依赖 |
|---|---|---|---|
| `agents/runtime_state.py`（新增，小） | 模块级全局单一来源 | `_relational_db`、`set_relational_db`、`_ws_emit_callback`、`set_ws_emit_callback` | 无 |
| `agents/budget.py` | 预算 / token | `_estimate_tokens`、`_clip_to_token_budget`、`_calc_budget`、`_calc_budget_from_state`、`_safe_len` | 无 |
| `agents/loop_control.py` | 签名 / 循环 / 冷却 / 终止识别 | `_build_page_signature`、`_build_call_signature`、`_cooldown_group`、`_resolve_click_match_mode`、`_resolve_click_fallback`、`_output_has_page_change`、`_detect_termination` | 无（perceive 经 ctx） |
| `agents/llm_runtime.py` | LLM 运行时（**最大一块 ~450 行**） | `_run_agent`、`_call_retry`、`_call_retry_should_retry`、`_zhipu_schemas`、`_to_zhipu`、`_llm_cfg`、`_ensure_device_alive`、`_SubState`、`_take_step_screenshot`、`_build_tool_target` | budget、loop_control、runtime_state |
| `agents/rag_context.py` | RAG 注入 | `_rag_ctx`、`_apply_click_preferences`、`_should_include_rag`、`_should_force_query_app_knowledge` | 无 |
| `agents/verification.py` | 验证项映射 / 合并 / 判定 | `_normalize_verification_text`、`_goal_verification_items`、`_build_verification_key_maps`、`_resolve_verification_key`、`_merge_goal_verification_results`、`_determine_execution_status`、`_collect_verification_results` | 无 |
| `agents/nodes.py` | 图节点 | `planner_node`、`agent_node`、`reporter_node`、`plan_review_node` | 以上全部 |
| `agents/graph.py`（瘦身后） | **只做组装** | `build_graph`、`route_after_agent`、`route_after_plan_review`、`_load_prompt`、`_parse_goal`、`_prune_messages`；再导出 `set_relational_db` 等以保持兼容 | nodes、runtime_state |

---

## 4) 必须遵守的护栏（避免重构引入回归）

1. **循环导入**：现有 `tools → graph → tools` 已靠延迟 import 化解（如函数内 `from agents.graph import _relational_db`）。拆分放大此风险，须定死依赖方向：
   `text_utils/budget/loop_control（叶子）← element_match/llm_runtime/rag/verification ← nodes/click ← graph/__init__（组装）`。
   反向依赖一律用函数内延迟 import。
2. **模块级全局单一来源**：`_relational_db`、`_ws_emit_callback` 用 setter 改。节点搬走后**必须 import 同一个对象**，不能各持副本 → 集中放 `agents/runtime_state.py`，其他模块从它 import。
3. **`AGENT_TOOLS` 聚合不变**：所有 `@tool` 分散后，列表仍在 `tools/__init__.py` 汇总导出，保证图拿到的工具集与顺序一致。
4. **对外 import 路径不变**：`tools/__init__.py` 与 `agents/graph.py` 用再导出保持 `from tools import click, AGENT_TOOLS, set_tool_context`、`from agents.graph import build_graph, set_relational_db` 全部可用。**迁移前先 grep 所有 import 点**（`main.py`、`api/server.py`、`agents/orchestrator.py`、`tests/*`）核对。
5. **测试引用内部符号**：如 `tests/test_tools_click_preference.py` 直接 import 了 `click` 等。搬动前先确认测试 import，或在 `__init__` 再导出。
6. **与 migration 协调**：`element_match.py` 那批将来要删，别精修，隔离即可。
7. **纯搬运、不改逻辑**：本次重构**只移动代码 + 调整 import**，不顺手改行为。行为变更单独开 PR，避免「重构」和「改逻辑」混在一起难以 review/回滚。

---

## 5) 分批落地顺序（每批后跑 pytest）

**第一阶段 — tools 拆包（P0）**

| 批次 | 动作 | 风险 |
|:--:|---|:--:|
| T1 | 抽 `tools/text_utils.py`（纯函数） | 极低 |
| T2 | `set/get_tool_context` 归入 `tools/context.py` | 低 |
| T3 | 抽 `tools/element_match.py`（legacy 簇整体搬） | 中 |
| T4 | 抽 `tools/device_ops.py`、`tools/knowledge_tools.py` | 低 |
| T5 | 抽 `tools/perceive_tools.py`、`tools/verify.py` | 中 |
| T6 | 抽 `tools/click.py`，`__init__.py` 收敛为组装层 | 中高 |

**第二阶段 — graph 拆分（P1）**

| 批次 | 动作 | 风险 |
|:--:|---|:--:|
| G1 | 抽 `agents/runtime_state.py`（全局单一来源） | 中（关键：全局对象唯一） |
| G2 | 抽 `agents/budget.py`、`agents/loop_control.py` | 低 |
| G3 | 抽 `agents/verification.py`、`agents/rag_context.py` | 低 |
| G4 | 抽 `agents/llm_runtime.py`（最大块） | 中高 |
| G5 | 节点搬到 `agents/nodes.py`，`graph.py` 收敛为组装层 | 中高 |

**第三阶段 — 可选（P2）**：`device/perceiver.py`（888）可按「XML 解析 / 页面理解 / 视觉补充」拆为 `perceiver/parse.py`、`perceiver/understand.py`、`perceiver/vision.py`，优先级低，视精力而定。

---

## 6) 验收标准

- 每批次后 `pytest -q` 全绿（现有 8 个测试文件 + 集成骨架）。
- 拆分**前后无任何行为差异**（纯结构重构）；如需行为调整另行开单。
- 对外 import 路径零变更：`main.py` / `api/` / `orchestrator.py` / `tests/` 不改动即可运行。
- 单文件目标：`tools/*` 每个 ≤ ~500 行；`agents/*` 每个 ≤ ~500 行（`llm_runtime.py`、`nodes.py` 可略高，含大函数）。
- 无新增循环 import（可用 `python -c "import agents.graph, tools"` 冒烟验证）。

---

## 7) 不拆的部分（明确边界）

- `api/server.py`（672）、`data/relational.py`（672）、`agents/orchestrator.py`（542）：各自单一职责、规模可接受，本轮不动。
- 若后续 `api/server.py` 继续膨胀，可把报告类路由（`list/get/delete_report`）拆到 `api/report_routes.py`（当前 device/apps/knowledge/config 路由已分文件，模式现成）。

---

*本计划为纯结构性重构方案，不含逻辑变更；建议按 §5 批次逐步执行，每步独立提交便于回滚。*
