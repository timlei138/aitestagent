from __future__ import annotations

import json
import logging
from typing import Any

from core.model_clients import create_llm_client
from core.tools import ALL_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是移动端自动化测试专家 Agent。你的任务是根据用户的测试目标，一步步操作手机/平板完成测试。

## 你的能力
你可以使用以下工具操作设备：

### 基础操作
- click: 点击屏幕元素（传元素文本）
- type_input: 输入文字
- swipe: 滑动屏幕（up/down/left/right）
- press_key: 按系统键（back/home/enter）
- navigate_to: 切换导航Tab
- scroll_find_and_click: 滑动查找并点击
- launch_app: 启动应用

### 感知操作
- get_screen_info: 获取当前页面的结构化语义信息（快速，用UI树）
- get_detailed_screen: 获取当前页面详细语义分析（含Vision说明）
- switch_perception_mode: 切换感知模式（ui_tree/vision/hybrid）

### 断言操作
- assert_element_exists: 断言元素存在
- assert_text_in_list: 断言列表中存在某文字
- assert_page_contains: 断言页面包含某文字

### 异常检测
- check_page_health: 检测当前页面健康状态（白屏/黑屏/ANR/崩溃/显示不全）
- check_against_baseline: 将当前页面与基线对比，检测异常
- recover_from_anomaly: 从异常中恢复（关弹窗→返回→重启）

### 知识查询
- query_app_knowledge: 查询APP的历史测试知识
- save_current_page_knowledge: 保存当前页面到知识库

### 辅助
- save_screenshot: 保存当前截图
- log_step: 记录测试步骤

## 工作方式
1. 先用 get_screen_info() 了解当前页面布局和主要入口
2. 根据测试目标规划下一步操作
3. 执行操作
4. 观察结果，决定继续还是结束
5. 每步操作后简要说明结果

## 规则
1. 每次操作前先看屏幕状态（get_screen_info）
2. 看到弹窗/对话框 → 优先处理（点允许/确定/关闭）
3. 输入文字前 → 先点击输入框获取焦点
4. 找不到元素 → 用 scroll_find_and_click 滑动查找
5. 不确定下一步 → 用 press_key(back) 返回或 swipe 探索
6. UI树模式看不清 → 切换到 vision 模式（get_detailed_screen）
7. 断言时 → 根据实际UI状态判断，不要猜测
8. 所有步骤完成 → 给出测试结论（PASS/FAIL）和原因
9. 每步操作要调用 log_step() 记录
10. 每步操作后调用 check_page_health() 检测异常
11. 发现白屏/黑屏/ANR/崩溃 → 立即报告并调用 recover_from_anomaly() 恢复
12. 回放模式下，关键步骤调用 check_against_baseline() 与基线对比
13. 检测到严重异常（ANR/崩溃）→ 记录后尝试恢复，恢复失败则终止测试
14. 危险操作（删除/支付/重置/注销）不要执行，优先寻找安全的替代路径
"""


class SimpleAgent:
    """无 API Key 时的降级 Agent，返回任务信息但不执行自动推理。"""

    def invoke(self, payload, config=None):
        message = payload["messages"][-1]["content"]
        return {
            "messages": [
                {
                    "content": (
                        "未配置 LangGraph Agent，已接收任务但未执行自动推理。\n"
                        f"任务:\n{message}\n\n"
                        "FAIL: Agent runtime unavailable — 请配置 OPENAI_API_KEY"
                    )
                }
            ]
        }


class ProviderTextAgent:
    """基于抽象 LLMClient 的文本降级 Agent（不含工具执行）。"""

    def __init__(self, llm_client, system_prompt: str):
        self.llm = llm_client
        self.system_prompt = system_prompt

    def invoke(self, payload, config=None):
        last_message = payload["messages"][-1]
        user_text = (
            last_message.get("content", "")
            if isinstance(last_message, dict)
            else getattr(last_message, "content", "")
        )
        content = self.llm.invoke(
            [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": (
                        "请给出测试执行方案与判断标准。当前运行在 provider 文本模式，"
                        "不支持 LangGraph 工具自动点击。\n\n"
                        f"任务:\n{user_text}"
                    ),
                },
            ]
        )
        return {"messages": [{"content": str(content)}]}


class ZhipuToolAgent:
    """基于智谱 tools/tool_calls 的可执行 Agent。"""

    def __init__(self, model: str, api_key: str, base_url: str | None, system_prompt: str):
        from zhipuai import ZhipuAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = ZhipuAI(**kwargs)
        self.model = model
        self.system_prompt = system_prompt
        self.max_steps = 20
        self._tool_map = {tool.name: tool for tool in ALL_TOOLS}
        self._tool_schemas = self._build_tool_schemas()

    def _build_tool_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for tool in ALL_TOOLS:
            properties = {}
            args = getattr(tool, "args", {}) or {}
            for arg_name, arg_meta in args.items():
                schema = {"type": arg_meta.get("type", "string")}
                if arg_meta.get("description"):
                    schema["description"] = arg_meta["description"]
                if arg_meta.get("enum"):
                    schema["enum"] = arg_meta["enum"]
                properties[arg_name] = schema

            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": getattr(tool, "description", "") or "",
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": [],
                        },
                    },
                }
            )
        return schemas

    def _extract_message(self, response: Any) -> Any:
        choices = getattr(response, "choices", None) or response.get("choices", [])
        if not choices:
            return None
        first = choices[0]
        return getattr(first, "message", None) or first.get("message")

    def _normalize_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for tc in tool_calls or []:
            tc_id = getattr(tc, "id", None) or tc.get("id")
            fn = getattr(tc, "function", None) or tc.get("function", {})
            fn_name = getattr(fn, "name", None) or fn.get("name")
            fn_args_raw = getattr(fn, "arguments", None) or fn.get("arguments", "{}")
            normalized.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": fn_name, "arguments": fn_args_raw},
                }
            )
        return normalized

    def _invoke_tool(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tool_map.get(name)
        if not tool:
            return f"工具不存在: {name}"
        try:
            if hasattr(tool, "invoke"):
                result = tool.invoke(args or {})
            else:
                result = tool(**(args or {}))
            return str(result)
        except Exception as exc:
            return f"工具执行异常[{name}]: {exc}"

    def invoke(self, payload, config=None):
        last_message = payload["messages"][-1]
        user_text = (
            last_message.get("content", "")
            if isinstance(last_message, dict)
            else getattr(last_message, "content", "")
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_text},
        ]

        final_content = ""
        for _ in range(self.max_steps):
            logger.info(
                "ZhipuToolAgent LLM request step=%s messages=%s",
                _ + 1,
                len(messages),
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self._tool_schemas,
                tool_choice="auto",
                temperature=0.1,
            )
            message = self._extract_message(response)
            if not message:
                break

            content = getattr(message, "content", None) or message.get("content") or ""
            tool_calls_raw = getattr(message, "tool_calls", None) or message.get("tool_calls")
            tool_calls = self._normalize_tool_calls(tool_calls_raw)
            logger.info(
                "ZhipuToolAgent LLM response step=%s content=%s tool_calls=%s",
                _ + 1,
                str(content)[:800],
                len(tool_calls),
            )
            final_content = str(content)

            if not tool_calls:
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                }
            )

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args_raw = tc["function"]["arguments"] or "{}"
                try:
                    fn_args = (
                        json.loads(fn_args_raw)
                        if isinstance(fn_args_raw, str)
                        else dict(fn_args_raw or {})
                    )
                except Exception:
                    fn_args = {}
                tool_result = self._invoke_tool(fn_name, fn_args)
                logger.info(
                    "ZhipuToolAgent tool_result name=%s result=%s",
                    fn_name,
                    str(tool_result)[:600],
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    }
                )

        if not final_content:
            final_content = "FAIL: 未生成有效结果"
        return {"messages": [{"content": final_content}]}


def create_test_agent(
    provider: str = "openai",
    model: str = "gpt-4o",
    api_key: str | None = None,
    base_url: str | None = None,
    enable_memory: bool = True,
    knowledge_base=None,
    app_package: str = "",
):
    """创建 ReAct Agent。

    Args:
        model: LLM 模型名。
        api_key: API 密钥。为空时使用降级 SimpleAgent。
        base_url: API 基础 URL。
        enable_memory: 启用 LangGraph MemorySaver 断点续跑。
        knowledge_base: RAG 知识库实例，用于注入历史测试知识。
        app_package: 当前测试的目标包名，用于查询 RAG。
    """
    # RAG 知识注入
    from core.knowledge_base import build_rag_enhanced_prompt

    system_prompt = SYSTEM_PROMPT
    if knowledge_base and app_package:
        system_prompt = build_rag_enhanced_prompt(
            SYSTEM_PROMPT, knowledge_base, app_package
        )

    if not api_key:
        return SimpleAgent()

    provider_name = (provider or "openai").lower()

    # OpenAI: 继续走 LangGraph + Tools 的完整能力
    if provider_name == "openai":
        try:
            from langchain_openai import ChatOpenAI
            from langgraph.checkpoint.memory import MemorySaver
            from langgraph.prebuilt import create_react_agent

            llm = ChatOpenAI(
                model=model,
                temperature=0.1,
                api_key=api_key,
                base_url=base_url,
            )
            checkpointer = MemorySaver() if enable_memory else None

            try:
                return create_react_agent(
                    model=llm,
                    tools=ALL_TOOLS,
                    prompt=system_prompt,
                    checkpointer=checkpointer,
                )
            except TypeError:
                return create_react_agent(
                    model=llm,
                    tools=ALL_TOOLS,
                    state_modifier=system_prompt,
                    checkpointer=checkpointer,
                )
        except Exception:
            return SimpleAgent()

    if provider_name == "zhipu":
        try:
            return ZhipuToolAgent(
                model=model,
                api_key=api_key,
                base_url=base_url,
                system_prompt=system_prompt,
            )
        except Exception:
            return SimpleAgent()

    # 非 OpenAI: 使用抽象客户端，保证 provider 可插拔
    llm_client = create_llm_client(
        provider=provider_name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.1,
    )
    if llm_client is None:
        return SimpleAgent()
    return ProviderTextAgent(llm_client, system_prompt)
