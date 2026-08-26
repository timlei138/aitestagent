"""F3 clause 合并到人类步骤级单测（agent_evolution_plan §5）。

覆盖：
- 嵌套对象 {"claim", "clauses":[{"claim","spec"}...]}：planner 规划期语义合并
  直接采用，不再机械切分；
- 旧字符串项回归：ASCII `,` 不再拆（枚举逗号不拆），全角分隔符仍拆；
- validate_contract_spans 对嵌套格式无 gap/overlap 误报（含 planner 改写措辞、
  非逐字子串场景）；
- spec 归属：clause 自带 spec 优先；顶层 spec 仅兜底首条；
- evaluate_verification 在嵌套契约上正常工作。
"""

from __future__ import annotations

from agents.verification import (
    _CLAUSE_BOUNDARY,
    build_verification_contract,
    evaluate_verification,
    validate_contract_spans,
)


def test_nested_clauses_adopted_verbatim():
    goal = {
        "verification": [
            {
                "claim": "勾选周一,周三,周五后显示3节课",
                "clauses": [
                    {
                        "claim": "勾选周一、周三、周五后课程表显示3节课",
                        "spec": {"predicate": "list_count", "target": "课程节", "expected": 3},
                    }
                ],
            }
        ]
    }
    contract = build_verification_contract(goal)
    v0 = contract["verifications"][0]
    assert len(v0["clauses"]) == 1
    clause = v0["clauses"][0]
    assert clause["claim"] == "勾选周一、周三、周五后课程表显示3节课"
    assert clause["spec"] == {"predicate": "list_count", "target": "课程节", "expected": 3}
    assert clause["channels"] == ["element_state"]
    assert validate_contract_spans(contract)["valid"] is True


def test_nested_clauses_rewritten_wording_no_gap_overlap():
    """planner 改写措辞（非逐字子串）→ 占位 span 无重叠、完整覆盖，校验不误报。"""
    statement = "创建选择1,3,5周的课程后再次创建时1/3/5周不可选"
    goal = {
        "verification": [
            {
                "claim": statement,
                "clauses": [
                    {"claim": "创建选择 1/3/5 周的课程成功", "spec": None},
                    {
                        "claim": "再次点开上课周数时 1/3/5 周呈现置灰不可选",
                        "spec": {"predicate": "element_disabled", "target": "1", "expected": None},
                    },
                ],
            }
        ]
    }
    contract = build_verification_contract(goal)
    v0 = contract["verifications"][0]
    assert [c["claim"] for c in v0["clauses"]] == [
        "创建选择 1/3/5 周的课程成功",
        "再次点开上课周数时 1/3/5 周呈现置灰不可选",
    ]
    # 改写路径：占位 span 连续铺满 statement
    assert v0["clauses"][0]["goal_source_span"][0] == 0
    assert v0["clauses"][-1]["goal_source_span"][1] == len(statement)
    result = validate_contract_spans(contract)
    assert result["valid"] is True, result


def test_nested_clauses_literal_substring_exact_offsets():
    """逐字子串 → 用实际位置，间隔进 context_spans。"""
    statement = "弹窗出现并且提示已保存"
    goal = {
        "verification": [
            {
                "claim": statement,
                "clauses": [
                    {"claim": "弹窗出现", "spec": {"predicate": "page_contains", "target": "保存", "expected": None}},
                    {"claim": "提示已保存", "spec": None},
                ],
            }
        ]
    }
    contract = build_verification_contract(goal)
    v0 = contract["verifications"][0]
    c0, c1 = v0["clauses"]
    assert c0["goal_source_span"] == [0, 4]
    assert c1["goal_source_span"] == [statement.find("提示已保存"), len(statement)]
    result = validate_contract_spans(contract)
    assert result["valid"] is True, result


def test_nested_clause_spec_wins_over_top_level_only_first_inherits():
    goal = {
        "verification": [
            {
                "claim": "整句预期",
                "spec": {"predicate": "page_is", "target": "TopLevel", "expected": None},
                "clauses": [
                    {"claim": "子句一"},  # 无自带 spec → 顶层兜底（仅首条）
                    {
                        "claim": "子句二",
                        "spec": {"predicate": "element_disabled", "target": "开关", "expected": None},
                    },
                ],
            }
        ]
    }
    contract = build_verification_contract(goal)
    clauses = contract["verifications"][0]["clauses"]
    # 首条无自带 spec → 顶层 spec 兜底
    assert clauses[0]["spec"] == {"predicate": "page_is", "target": "TopLevel", "expected": None}
    assert clauses[0]["channels"] == ["page_state"]
    # 自带 spec 优先，不继承顶层
    assert clauses[1]["spec"]["predicate"] == "element_disabled"
    assert clauses[1]["channels"] == ["element_state"]


def test_legacy_string_ascii_comma_not_split():
    """F3：枚举逗号（ASCII ,）不再拆——「周一,周三,周五」是一个判定点。"""
    goal = {"verification": ["勾选周一,周三,周五后显示3节课"]}
    contract = build_verification_contract(goal)
    clauses = contract["verifications"][0]["clauses"]
    assert len(clauses) == 1
    assert clauses[0]["claim"] == "勾选周一,周三,周五后显示3节课"


def test_legacy_string_fullwidth_conjunctions_still_split():
    assert _split_via("弹窗出现，提示已保存") == ["弹窗出现", "提示已保存"]
    assert _split_via("A并且B") == ["A", "B"]


def _split_via(statement: str) -> list[str]:
    contract = build_verification_contract({"verification": [statement]})
    return [c["claim"] for c in contract["verifications"][0]["clauses"]]


def test_f4_flat_object_regression_unchanged():
    """F4 平面对象 {"claim","spec"} 行为不变：机械切分 + spec 挂首条。"""
    goal = {
        "verification": [
            {
                "claim": "设置页打开，开关置灰",
                "spec": {"predicate": "element_disabled", "target": "开关", "expected": None},
            }
        ]
    }
    contract = build_verification_contract(goal)
    clauses = contract["verifications"][0]["clauses"]
    assert len(clauses) == 2  # 全角逗号仍拆
    assert clauses[0]["spec"]["predicate"] == "element_disabled"
    assert clauses[1]["spec"] is None
    assert clauses[1]["channels"] != ["element_state"]  # spec:null 走全通道回退


def test_evaluate_on_nested_contract():
    goal = {
        "verification": [
            {
                "claim": "整句预期",
                "clauses": [
                    {"claim": "子句一", "spec": {"predicate": "page_contains", "target": "X", "expected": None}},
                    {"claim": "子句二", "spec": None},
                ],
            }
        ]
    }
    contract = build_verification_contract(goal)
    # F1 收窄在嵌套 clause 上仍生效：带 spec 的子句一只认 page_state，
    # ui_text PASS 无效（unknown）；spec:null 子句二 vision PASS 兜底有效。
    events = [
        {"verification_key": "v0", "clause_id": "v0.0", "channel": "ui_text", "status": "PASS"},
        {"verification_key": "v0", "clause_id": "v0.1", "channel": "vision_verify", "status": "YES"},
    ]
    result = evaluate_verification(contract, events)
    assert result["verdict"] == "inconclusive"
    assert result["verifications"][0]["clauses"][0]["status"] == "unknown"
    assert result["verifications"][0]["clauses"][1]["status"] == "passed"

    events.append(
        {"verification_key": "v0", "clause_id": "v0.0", "channel": "page_state", "status": "PASS"}
    )
    result = evaluate_verification(contract, events)
    assert result["verdict"] == "passed"
