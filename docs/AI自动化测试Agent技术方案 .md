# AI 自动化测试 Agent 技术方案

> 更新日期：2026-06-05
> 架构：ReAct Agent + SmartPerceiver（双模式感知）+ RAG 知识库 + Baseline 基线对比引擎 + Intent Parser（自然语言意图解析）
> 框架：LangChain + LangGraph + ChromaDB + imagehash + FastAPI

---

## 1. 系统架构设计

### 1.1 整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                          用户入口                                      │
│           CLI (main.py)  /  Web Chat (FastAPI + WebSocket)            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     Intent Parser (LLM)                               │
│  自然语言 → 结构化 Intent → 前端确认卡片 → 用户确认/修改 → 执行        │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
           ┌───────────────────┼───────────────────┐
           ▼                   ▼                   ▼
   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
   │ 遍历建基线    │    │ 回放对比     │    │ 自然语言执行  │
   │              │    │              │    │             │
   │ 遍历所有页面  │    │ 操作后与     │    │ ReAct Agent │
   │ 截图+UI树    │    │ Baseline对比 │    │ 自主决策     │
   │ 存Baseline   │    │ 检测异常      │    │             │
   └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
          │                  │                  │
          └──────────────────┼──────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  ReAct Agent (LangChain)                               │
│  System Prompt + Tools + Memory + RAG + Baseline DB                   │
│  Thought → Action → Observation → ...                                 │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  ┌──────────┐        ┌──────────────┐        ┌──────────────┐
  │基础操作    │        │SmartPerceiver│        │断言验证       │
  │click/type │        │UI树 ↔ Vision │        │assert_*      │
  │swipe/press│        └──────────────┘        └──────────────┘
  │navigate   │                                ┌──────────────┐
  │launch     │        ┌──────────────┐        │异常检测       │
  └──────────┘        │Baseline      │        │check_health  │
                      │Store         │        │check_baseline│
                      │pHash索引      │        │recover       │
                      └──────────────┘        └──────────────┘
                               │
                               ▼
                       ┌─────────────────┐
                       │  Device Control  │
                       │  (uiautomator2)  │
                       └─────────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼                             ▼
  ┌───────────────────┐           ┌───────────────────┐
  │ RAG Knowledge Base │           │  Baseline Store    │
  │ ChromaDB 向量存储   │           │  JSON + 截图 + pHash│
  │ 测试结束自动存入    │           │  遍历建、回放用     │
  │ 测试开始查询注入    │           │                   │
  └───────────────────┘           └───────────────────┘
```

### 1.2 核心组件

| 组件 | 职责 | 技术 |
|---|---|---|
| **Intent Parser** | 自然语言 → 结构化 Intent，前端确认后执行 | LLM + 关键词快速匹配 |
| **ReAct Agent** | 思考→行动→观察循环，自主决策 | LangChain `create_react_agent` |
| **SmartPerceiver** | 双模式感知，自动切换 | UI 树解析 + GPT-4V 截图 |
| **Tools** | 设备操作工具集（18 个） | LangChain `@tool` |
| **MemorySaver** | 状态 checkpoint，断点续跑 | LangGraph `MemorySaver` |
| **RAG KnowledgeBase** | APP UI 知识存储与检索 | ChromaDB + Embeddings |
| **BaselineStore** | 页面基线存储与对比 | JSON + pHash + numpy |
| **AnomalyDetector** | 异常检测引擎 | PIL + numpy + imagehash |
| **BaselineTraverser** | BFS 遍历建基线 | uiautomator2 + pHash 去重 |
| **StateMachine** | 错误恢复状态机 | 弹窗/卡死/崩溃恢复 |
| **ChatRunner** | 统一调度：解析意图 → 确认 → 执行 | FastAPI + WebSocket |

### 1.3 运行模式

```
模式 1: 遍历建基线 (traverse)
  输入: APP 包名（CLI 或自然语言）
  过程: BFS 遍历所有可达页面 → 截图 + UI树 → 存入 Baseline
  输出: storage/baselines/<package>/ 目录

模式 2: 回放对比 (replay)
  输入: YAML 用例 或 自然语言指令
  过程: 按步骤执行 → 每步与 Baseline 对比 → 检测异常
  输出: 测试报告 + 异常列表

模式 3: 自然语言执行 (run)
  输入: 自然语言指令（如 "测试 Settings 的 WiFi 开关"）
  过程: Intent Parser 解析 → 用户确认 → Agent 自主决策执行
  输出: 测试报告

模式 4: 执行 YAML 用例 (run_case)
  输入: YAML 用例文件路径
  过程: 加载用例 → Agent 按步骤执行
  输出: 测试报告
```

### 1.4 目录结构

```
ai-test-agent/
├── main.py                          # CLI 入口（支持四种模式 + chat）
├── config.py                        # 全局配置
├── requirements.txt
├── core/
│   ├── __init__.py
│   ├── agent.py                     # ReAct Agent 创建
│   ├── tools.py                     # 所有 Tools 定义（18个）
│   ├── smart_perceiver.py           # 智能感知器（双模式）
│   ├── device_controller.py         # uiautomator2 封装
│   ├── state_machine.py             # 错误恢复状态机
│   ├── knowledge_base.py            # RAG 知识库
│   ├── baseline_store.py            # 基线存储与管理
│   ├── anomaly_detector.py          # 异常检测引擎
│   ├── baseline_traverser.py        # 遍历建基线
│   ├── intent_parser.py             # [新增] 自然语言意图解析
│   └── chat_runner.py               # [新增] 统一调度执行器
├── api/
│   └── server.py                    # [新增] Web API + WebSocket
├── frontend/
│   └── index.html                   # [新增] Chat 前端界面
├── prompts/
│   ├── system_prompt.txt            # Agent 主 Prompt
│   └── vision_prompt.txt            # 视觉感知 Prompt
├── test_cases/
│   ├── feedback_submit.yaml
│   └── search_traverse.yaml
├── storage/
│   ├── results/
│   ├── screenshots/
│   ├── knowledge/
│   └── baselines/
└── reports/
```

---

## 2. Intent Parser (`core/intent_parser.py`)

### 2.1 设计思路

用户在前端 Chat 输入自然语言（如 "帮我跑 Settings 全部遍历"），Intent Parser 将其解析为结构化 Intent，前端展示确认卡片让用户确认/修改后执行。

```
用户输入 → LLM 解析 → 前端确认卡片 → 用户确认/修改 → 执行
```

### 2.2 APP 名称映射

```python
APP_NAME_MAP = {
    "settings": "com.android.settings",
    "设置": "com.android.settings",
    "联系人": "com.android.contacts",
    "相机": "com.android.camera",
    "电话": "com.android.dialer",
    "信息": "com.android.mms",
    "浏览器": "com.android.browser",
    "时钟": "com.android.deskclock",
    "计算器": "com.android.calculator2",
    "文件管理": "com.android.filemanager",
    "日历": "com.android.calendar",
    "服务与反馈": "com.lenovo.service",
    "反馈": "com.lenovo.service",
}
```

### 2.3 Intent 数据结构

```python
{
    "intent": "traverse|replay|run|run_case",
    "app_package": "com.android.settings",
    "app_name": "Settings",
    "task_description": "遍历 Settings 的所有页面",
    "case_file": "",            # run_case 时填写
    "scope": "full|partial",    # traverse: full=全部, partial=指定页面
    "target_pages": [],         # partial 时指定页面列表
    "extra_context": "",        # 用户补充的上下文
    "traversal_max_depth": 5,   # traverse 专属
    "traversal_max_pages": 50,  # traverse 专属
}
```

### 2.4 完整实现

```python
# core/intent_parser.py

import json
import re
from langchain_openai import ChatOpenAI


INTENT_PROMPT = """你是一个测试指令解析器。用户会用自然语言描述测试需求，你需要解析成结构化的 JSON。

## 支持的意图类型

1. **traverse** — 遍历建基线
   "遍历 Settings" / "跑一下桌面的全部遍历" / "建基线"

2. **replay** — 回放对比
   "回放反馈提交用例" / "跑一下之前的测试"

3. **run** — 自然语言直接执行测试
   "测试 Settings 的 WiFi 开关" / "验证蓝牙能不能打开"

4. **run_case** — 执行指定 YAML 用例
   "跑 feedback_submit.yaml"

## 输出格式

```json
{
    "intent": "traverse|replay|run|run_case",
    "app_package": "com.android.settings",
    "app_name": "Settings",
    "task_description": "遍历 Settings 的所有页面",
    "case_file": "",
    "scope": "full|partial",
    "target_pages": [],
    "extra_context": "",
    "traversal_max_depth": 5,
    "traversal_max_pages": 50
}
```

## 常见 APP 包名参考
- Settings: com.android.settings
- 联系人: com.android.contacts
- 相机: com.android.camera
- 电话: com.android.dialer
- 信息: com.android.mms
- 浏览器: com.android.browser
- 时钟: com.android.deskclock
- 计算器: com.android.calculator2
- 文件管理: com.android.filemanager
- 日历: com.android.calendar

注意：如果用户说的 APP 名称不明确，根据上下文推断最可能的包名。
如果实在不确定，app_package 留空，后续会自动探测。
"""

APP_NAME_MAP = {
    "settings": "com.android.settings",
    "设置": "com.android.settings",
    "联系人": "com.android.contacts",
    "相机": "com.android.camera",
    "电话": "com.android.dialer",
    "信息": "com.android.mms",
    "浏览器": "com.android.browser",
    "时钟": "com.android.deskclock",
    "计算器": "com.android.calculator2",
    "文件管理": "com.android.filemanager",
    "日历": "com.android.calendar",
    "服务与反馈": "com.lenovo.service",
    "反馈": "com.lenovo.service",
}


class IntentParser:
    """自然语言意图解析器"""

    def __init__(self, model="gpt-4o", api_key=None, base_url=None):
        self.llm = ChatOpenAI(
            model=model, temperature=0.1,
            api_key=api_key, base_url=base_url,
        )

    def parse(self, user_input: str) -> dict:
        """
        解析用户自然语言输入为结构化意图

        优先走关键词快速匹配（省 token），匹配不到再调 LLM。
        """
        # 快速匹配
        quick_result = self._quick_parse(user_input)
        if quick_result:
            return quick_result

        # LLM 解析
        response = self.llm.invoke([
            {"role": "system", "content": INTENT_PROMPT},
            {"role": "user", "content": user_input}
        ])

        try:
            content = response.content
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            intent = json.loads(content.strip())

            # 补充包名
            if not intent.get("app_package"):
                intent["app_package"] = self._resolve_package(
                    intent.get("app_name", ""), user_input
                )

            # 补充默认值
            intent.setdefault("traversal_max_depth", 5)
            intent.setdefault("traversal_max_pages", 50)
            intent.setdefault("scope", "full")
            intent.setdefault("target_pages", [])
            intent.setdefault("extra_context", "")
            intent.setdefault("case_file", "")
            intent.setdefault("task_description", user_input)

            return intent

        except (json.JSONDecodeError, IndexError):
            return self._fallback_intent(user_input)

    def _quick_parse(self, user_input: str) -> dict | None:
        """关键词快速匹配（常见场景秒回，不调 LLM）"""
        text = user_input.lower()

        # 遍历模式
        if any(kw in text for kw in ["遍历", "全部", "建基线", "traverse"]):
            app_package = self._resolve_package_from_text(text)
            return {
                "intent": "traverse",
                "app_package": app_package,
                "app_name": self._extract_app_name(user_input),
                "task_description": user_input,
                "case_file": "",
                "scope": "full",
                "target_pages": [],
                "extra_context": "",
                "traversal_max_depth": 5,
                "traversal_max_pages": 50,
            }

        # 回放模式
        if any(kw in text for kw in ["回放", "replay", "重跑"]):
            return {
                "intent": "replay",
                "app_package": "",
                "app_name": "",
                "task_description": user_input,
                "case_file": self._extract_case_file(user_input),
                "scope": "full",
                "target_pages": [],
                "extra_context": "",
                "traversal_max_depth": 5,
                "traversal_max_pages": 50,
            }

        return None  # 走 LLM

    def _resolve_package(self, app_name: str, context: str = "") -> str:
        """解析 APP 名称为包名"""
        for key, package in APP_NAME_MAP.items():
            if key in app_name.lower() or key in context.lower():
                return package

        # LLM 兜底
        try:
            response = self.llm.invoke([
                {"role": "system", "content": "你是 Android 专家。用户给你 APP 名称，返回最可能的包名。只返回包名。"},
                {"role": "user", "content": app_name or context}
            ])
            package = response.content.strip()
            if "." in package and package.startswith("com."):
                return package
        except Exception:
            pass

        return ""

    def _resolve_package_from_text(self, text: str) -> str:
        for key, package in APP_NAME_MAP.items():
            if key in text:
                return package
        return ""

    def _extract_app_name(self, text: str) -> str:
        for name in APP_NAME_MAP.keys():
            if name in text.lower():
                return name
        return ""

    def _extract_case_file(self, text: str) -> str:
        match = re.search(r'[\w/]+\.yaml', text)
        return match.group(0) if match else ""

    def _fallback_intent(self, user_input: str) -> dict:
        """解析失败时的降级处理"""
        return {
            "intent": "run",
            "app_package": "",
            "app_name": "",
            "task_description": user_input,
            "case_file": "",
            "scope": "full",
            "target_pages": [],
            "extra_context": "",
            "traversal_max_depth": 5,
            "traversal_max_pages": 50,
        }
```

---

## 3. Chat Runner (`core/chat_runner.py`)

统一调度器：接收 Intent → 分发执行 → 返回结果。

```python
# core/chat_runner.py

import json
import os
from datetime import datetime

from config import TestConfig
from core.agent import create_test_agent
from core.tools import init_device
from core.smart_perceiver import SmartPerceiver
from core.baseline_store import BaselineStore
from core.anomaly_detector import AnomalyDetector
from core.baseline_traverser import BaselineTraverser
from core.knowledge_base import KnowledgeBase
from core.intent_parser import IntentParser


class ChatRunner:
    """
    统一调度执行器

    职责：
    1. 接收结构化 Intent
    2. 分发到对应执行模式
    3. 返回统一格式结果
    """

    def __init__(self, config: TestConfig):
        self.config = config
        self.intent_parser = IntentParser(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self.device = init_device(config.device_serial)
        self.baseline_store = BaselineStore(config.baseline_dir)
        self.anomaly_detector = AnomalyDetector(self.device, self.baseline_store)

        self.knowledge_base = None
        if config.enable_rag:
            self.knowledge_base = KnowledgeBase(
                persist_dir=config.rag_persist_dir,
                embedding_model=config.embedding_model,
                api_key=config.api_key,
                base_url=config.base_url,
            )

    def run(self, user_input: str) -> dict:
        """
        完整流程：解析意图 → 执行

        适用于 CLI 模式（不需要前端确认）
        """
        intent = self.intent_parser.parse(user_input)
        return self.run_with_intent(intent)

    def run_with_intent(self, intent: dict) -> dict:
        """
        执行已确认的 Intent

        适用于 Web 模式（前端确认后调用）
        """
        action = intent["intent"]
        print(f"[EXEC] 意图: {action} | APP: {intent.get('app_package', '?')}")

        if action == "traverse":
            return self._run_traverse(intent)
        elif action == "replay":
            return self._run_replay(intent)
        elif action == "run":
            return self._run_natural(intent)
        elif action == "run_case":
            return self._run_case(intent)
        else:
            return {"status": "error", "message": f"未知意图: {action}"}

    def _run_traverse(self, intent: dict) -> dict:
        """执行遍历建基线"""
        app_package = intent["app_package"]
        if not app_package:
            return {"status": "error", "message": "无法识别目标 APP，请在确认卡片中填写包名"}

        print(f"[TRAVERSE] 开始遍历 {intent.get('app_name', app_package)}")

        self.device.app_start(app_package)
        import time
        time.sleep(2)

        traverser = BaselineTraverser(
            device=self.device,
            baseline_store=self.baseline_store,
            anomaly_detector=self.anomaly_detector,
            max_depth=intent.get("traversal_max_depth", self.config.traversal_max_depth),
            max_pages=intent.get("traversal_max_pages", self.config.traversal_max_pages),
        )

        result = traverser.traverse(
            app_package,
            start_page_name=intent.get("app_name", "首页")
        )

        return {
            "status": "success",
            "mode": "traverse",
            "app_package": app_package,
            "total_pages": result.total_pages,
            "duration_seconds": result.duration_seconds,
            "visited_keys": result.visited_keys,
            "errors": result.errors,
        }

    def _run_replay(self, intent: dict) -> dict:
        """执行回放对比"""
        case_file = intent.get("case_file", "")
        if not case_file:
            return {"status": "error", "message": "未指定用例文件"}

        if not os.path.exists(case_file):
            return {"status": "error", "message": f"用例文件不存在: {case_file}"}

        import yaml
        with open(case_file, "r", encoding="utf-8") as f:
            case = yaml.safe_load(f)

        app_package = case.get("app_package", intent.get("app_package", ""))

        # 检查基线
        pages = self.baseline_store.list_pages(app_package)
        if not pages:
            return {"status": "error", "message": f"未找到 {app_package} 的基线，请先执行遍历"}

        return self._execute_agent(case, app_package, mode="replay")

    def _run_natural(self, intent: dict) -> dict:
        """自然语言直接执行测试"""
        app_package = intent["app_package"]

        # 构建虚拟 case
        case = {
            "name": intent.get("task_description", "自然语言测试"),
            "description": intent.get("task_description", ""),
            "app_package": app_package,
            "app_name": intent.get("app_name", ""),
            "steps": [{"intent": intent.get("task_description", "")}],
            "verification": [],
        }

        return self._execute_agent(case, app_package, mode="run",
                                    extra_context=intent.get("extra_context", ""))

    def _run_case(self, intent: dict) -> dict:
        """执行指定 YAML 用例"""
        case_file = intent["case_file"]
        if not os.path.exists(case_file):
            return {"status": "error", "message": f"用例文件不存在: {case_file}"}

        import yaml
        with open(case_file, "r", encoding="utf-8") as f:
            case = yaml.safe_load(f)

        app_package = case.get("app_package", intent.get("app_package", ""))
        return self._execute_agent(case, app_package, mode="run")

    def _execute_agent(self, case: dict, app_package: str,
                       mode: str = "run", extra_context: str = "") -> dict:
        """执行 Agent 并返回结果"""
        # 构建任务描述
        task = f"""## 测试任务: {case['name']}
## 描述: {case.get('description', '')}
## 目标应用: {case.get('app_name', '')} ({app_package})

## 测试步骤:
"""
        for i, step in enumerate(case.get("steps", []), 1):
            task += f"{i}. {step['intent']}\n"

        if case.get("verification"):
            task += "\n## 验收标准:\n"
            for v in case["verification"]:
                task += f"- {v}\n"

        if extra_context:
            task += f"\n## 补充信息\n{extra_context}\n"

        # RAG 知识注入
        if self.knowledge_base and app_package:
            context = self.knowledge_base.get_app_context(app_package)
            if context:
                task += f"\n## 已知信息\n{context}\n"

        task += """
## 执行要求:
1. 先用 get_screen_info() 了解当前页面
2. 根据任务描述，自主规划并执行测试步骤
3. 每步操作后用 check_page_health() 检测异常
4. 每步操作后用 log_step() 记录结果
5. 遇到弹窗优先处理
6. 需要断言时使用 assert_* 工具
7. 所有步骤完成后给出测试结论（PASS/FAIL）和原因
"""

        # 启动应用
        if app_package:
            self.device.app_start(app_package)
            import time
            time.sleep(2)

        # 执行 Agent
        agent = create_test_agent(
            model=self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            knowledge_base=self.knowledge_base,
            app_package=app_package or "",
        )

        start_time = datetime.now()
        thread_id = f"chat-{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        result = agent.invoke(
            {"messages": [{"role": "user", "content": task}]},
            config={"configurable": {"thread_id": thread_id}}
        )

        duration = (datetime.now() - start_time).total_seconds()
        final_message = result["messages"][-1].content

        # RAG 知识提取
        if self.knowledge_base and app_package:
            is_pass = "PASS" in final_message.upper()
            log_file = "storage/results/test_log.jsonl"
            if os.path.exists(log_file):
                log = [json.loads(l) for l in open(log_file)]
                self.knowledge_base.extract_from_test_result(
                    app_package, case["name"], log,
                    "PASS" if is_pass else "FAIL"
                )

        # 保存报告
        report = {
            "mode": mode,
            "name": case["name"],
            "app_package": app_package,
            "duration_seconds": duration,
            "conclusion": final_message,
            "timestamp": datetime.now().isoformat(),
        }
        os.makedirs("reports", exist_ok=True)
        path = f"reports/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return {
            "status": "success",
            "mode": mode,
            "conclusion": final_message,
            "duration_seconds": duration,
            "report_path": path,
        }
```

---

## 4. Web API (`api/server.py`)

### 4.1 两步 API 设计

```
Step 1: POST /api/parse     → 解析意图（不执行）
Step 2: POST /api/confirm   → 用户确认后执行
WebSocket: /ws/chat         → 流式实时推送
```

### 4.2 完整实现

```python
# api/server.py

import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import TestConfig
from core.chat_runner import ChatRunner


app = FastAPI(title="AI 测试 Agent")

# 初始化
config = TestConfig.from_yaml("config.yaml")
runner = ChatRunner(config)

# 存储待确认的 intent
pending_intents: dict[str, dict] = {}


# ── 数据模型 ──

class ParseRequest(BaseModel):
    message: str
    session_id: str = "default"


class ConfirmRequest(BaseModel):
    session_id: str
    intent: dict
    confirmed: bool


# ── API 端点 ──

@app.post("/api/parse")
async def parse_intent(request: ParseRequest):
    """
    Step 1: 解析意图（不执行）

    返回结构化 Intent + 可编辑字段配置，
    前端渲染确认卡片让用户 review。
    """
    intent = runner.intent_parser.parse(request.message)
    pending_intents[request.session_id] = intent

    return {
        "status": "pending_confirmation",
        "intent": intent,
        "editable_fields": _get_editable_fields(intent),
    }


@app.post("/api/confirm")
async def confirm_intent(request: ConfirmRequest):
    """
    Step 2: 用户确认后执行

    confirmed=true  → 用用户确认/修改后的 intent 执行
    confirmed=false → 取消，不执行
    """
    if not request.confirmed:
        pending_intents.pop(request.session_id, None)
        return {"status": "cancelled", "message": "已取消"}

    intent = request.intent
    result = runner.run_with_intent(intent)
    pending_intents.pop(request.session_id, None)

    return {
        "status": result.get("status", "error"),
        "message": result.get("conclusion", result.get("message", "")),
        "data": result,
    }


@app.post("/api/chat")
async def chat_direct(request: ParseRequest):
    """
    一步到位接口（CLI 或不需要确认的场景）

    直接解析 + 执行，不经过确认环节。
    """
    result = runner.run(request.message)
    return {
        "status": result.get("status", "error"),
        "message": result.get("conclusion", result.get("message", "")),
        "data": result,
    }


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket 接口 — 流式实时推送

    消息协议：
    → 客户端发送: {"type": "parse", "message": "..."}
    ← 服务端推送: {"type": "intent", "content": {...}}
    → 客户端发送: {"type": "confirm", "intent": {...}, "confirmed": true}
    ← 服务端推送: {"type": "status", "content": "执行中..."}
    ← 服务端推送: {"type": "step", "content": "步骤描述"}
    ← 服务端推送: {"type": "result", "content": {...}}
    """
    await websocket.accept()
    session_id = id(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "parse":
                # 解析意图
                user_input = data.get("message", "")
                await websocket.send_json({
                    "type": "status",
                    "content": f"正在解析: {user_input}"
                })

                intent = runner.intent_parser.parse(user_input)
                pending_intents[session_id] = intent

                await websocket.send_json({
                    "type": "intent",
                    "content": intent,
                    "editable_fields": _get_editable_fields(intent),
                })

            elif msg_type == "confirm":
                # 确认执行
                confirmed = data.get("confirmed", False)
                if not confirmed:
                    pending_intents.pop(session_id, None)
                    await websocket.send_json({
                        "type": "cancelled",
                        "content": "已取消"
                    })
                    continue

                intent = data.get("intent", {})
                await websocket.send_json({
                    "type": "status",
                    "content": "开始执行..."
                })

                result = runner.run_with_intent(intent)
                pending_intents.pop(session_id, None)

                await websocket.send_json({
                    "type": "result",
                    "content": result
                })

    except WebSocketDisconnect:
        pending_intents.pop(session_id, None)


# ── 静态文件 ──

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
async def index():
    return FileResponse("frontend/index.html")


# ── 辅助函数 ──

def _get_editable_fields(intent: dict) -> dict:
    """返回可编辑字段配置，前端根据此渲染表单"""
    return {
        "intent": {
            "label": "模式",
            "type": "select",
            "options": [
                {"value": "traverse", "label": "🏗️ 遍历建基线"},
                {"value": "replay", "label": "🔄 回放对比"},
                {"value": "run", "label": "▶️ 直接执行"},
                {"value": "run_case", "label": "📄 执行用例"},
            ],
            "editable": True,
        },
        "app_name": {
            "label": "应用名称",
            "type": "text",
            "placeholder": "如 Settings",
            "editable": True,
        },
        "app_package": {
            "label": "包名",
            "type": "text",
            "placeholder": "com.android.settings",
            "editable": True,
        },
        "scope": {
            "label": "范围",
            "type": "select",
            "options": [
                {"value": "full", "label": "全部页面"},
                {"value": "partial", "label": "指定页面"},
            ],
            "editable": True,
        },
        "traversal_max_depth": {
            "label": "遍历深度",
            "type": "number",
            "min": 1, "max": 10,
            "default": 5,
            "editable": True,
            "show_when": {"intent": "traverse"},
        },
        "traversal_max_pages": {
            "label": "最大页面数",
            "type": "number",
            "min": 1, "max": 200,
            "default": 50,
            "editable": True,
            "show_when": {"intent": "traverse"},
        },
        "task_description": {
            "label": "任务描述",
            "type": "textarea",
            "editable": True,
        },
    }


# ── 启动 ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

---

## 5. 前端 Chat 界面 (`frontend/index.html`)

### 5.1 交互流程

```
用户输入消息
    → POST /api/parse（或 WS 发送 parse）
    ← 返回 Intent + editable_fields
    → 前端渲染确认卡片（可编辑）
    → 用户点击 "确认执行"
    → POST /api/confirm（或 WS 发送 confirm）
    ← 返回执行结果
```

### 5.2 完整实现

```html
<!-- frontend/index.html -->
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI 测试 Agent</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: system-ui, -apple-system, sans-serif; background: #0d1117; color: #c9d1d9;
               height: 100vh; display: flex; }

        /* ── 左侧面板 ── */
        .sidebar { width: 260px; background: #161b22; border-right: 1px solid #30363d;
                   display: flex; flex-direction: column; }
        .sidebar-header { padding: 20px; border-bottom: 1px solid #30363d; }
        .sidebar-header h1 { font-size: 16px; color: #58a6ff; }
        .sidebar-header p { font-size: 12px; color: #8b949e; margin-top: 4px; }
        .history-list { flex: 1; overflow-y: auto; padding: 10px; }
        .history-item { padding: 10px 12px; margin: 4px 0; background: #0d1117;
                        border-radius: 6px; cursor: pointer; font-size: 13px;
                        border: 1px solid transparent; }
        .history-item:hover { border-color: #30363d; background: #161b22; }
        .history-item.active { border-color: #58a6ff; background: #161b22; }

        /* ── 主区域 ── */
        .main { flex: 1; display: flex; flex-direction: column; }

        /* ── 快捷按钮 ── */
        .quick-bar { padding: 12px 20px; display: flex; gap: 8px; flex-wrap: wrap;
                     border-bottom: 1px solid #30363d; background: #161b22; }
        .quick-btn { padding: 6px 14px; background: #0d1117; border: 1px solid #30363d;
                     border-radius: 20px; color: #8b949e; cursor: pointer; font-size: 12px; }
        .quick-btn:hover { border-color: #58a6ff; color: #58a6ff; }

        /* ── 聊天区 ── */
        .chat-area { flex: 1; overflow-y: auto; padding: 20px; }

        .msg { max-width: 75%; margin: 8px 0; padding: 10px 14px; border-radius: 10px;
               font-size: 14px; line-height: 1.6; word-break: break-word; }
        .msg.user { background: #1f6feb; color: #fff; margin-left: auto;
                    border-bottom-right-radius: 4px; }
        .msg.agent { background: #161b22; border: 1px solid #30363d;
                     border-bottom-left-radius: 4px; }
        .msg.system { background: #0d1117; border-left: 3px solid #3fb950;
                      font-size: 12px; color: #8b949e; max-width: 90%; }
        .msg.error { background: #0d1117; border-left: 3px solid #f85149;
                     font-size: 12px; color: #f85149; }

        /* ── 确认卡片 ── */
        .confirm-card { max-width: 440px; margin: 12px 0; background: #161b22;
                        border: 1px solid #30363d; border-radius: 12px; overflow: hidden; }
        .card-header { padding: 12px 16px; background: #1f6feb22; display: flex;
                       align-items: center; gap: 8px; font-weight: 600; font-size: 14px; }
        .card-header .icon { font-size: 18px; }
        .card-body { padding: 16px; }
        .field-row { display: flex; align-items: center; justify-content: space-between;
                     margin: 10px 0; }
        .field-row label { font-size: 13px; color: #8b949e; min-width: 80px; }
        .field-input { flex: 1; max-width: 240px; padding: 7px 10px; background: #0d1117;
                       border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9;
                       font-size: 13px; outline: none; }
        .field-input:focus { border-color: #58a6ff; }
        .field-num { max-width: 80px; }
        .field-textarea { width: 100%; max-width: 100%; min-height: 60px; resize: vertical; }
        .card-actions { padding: 12px 16px; display: flex; justify-content: flex-end; gap: 10px;
                        border-top: 1px solid #30363d; }
        .btn { padding: 8px 20px; border: none; border-radius: 6px; cursor: pointer;
               font-size: 13px; transition: background 0.2s; }
        .btn-cancel { background: #21262d; color: #8b949e; }
        .btn-cancel:hover { background: #30363d; }
        .btn-confirm { background: #238636; color: #fff; }
        .btn-confirm:hover { background: #2ea043; }
        .executing { font-size: 13px; color: #58a6ff; }
        .cancelled { font-size: 13px; color: #8b949e; }

        /* ── 输入区 ── */
        .input-area { padding: 16px 20px; border-top: 1px solid #30363d;
                      display: flex; gap: 10px; background: #161b22; }
        .input-area input { flex: 1; padding: 10px 14px; background: #0d1117;
                            border: 1px solid #30363d; border-radius: 8px;
                            color: #c9d1d9; font-size: 14px; outline: none; }
        .input-area input:focus { border-color: #58a6ff; }
        .input-area button { padding: 10px 24px; background: #238636; border: none;
                             border-radius: 8px; color: #fff; cursor: pointer;
                             font-size: 14px; }
        .input-area button:hover { background: #2ea043; }
    </style>
</head>
<body>
    <!-- 左侧 -->
    <div class="sidebar">
        <div class="sidebar-header">
            <h1>🤖 AI 测试 Agent</h1>
            <p>自然语言驱动的自动化测试</p>
        </div>
        <div class="history-list" id="history"></div>
    </div>

    <!-- 主区域 -->
    <div class="main">
        <div class="quick-bar">
            <span class="quick-btn" onclick="quickSend('遍历 Settings')">🏗️ 遍历 Settings</span>
            <span class="quick-btn" onclick="quickSend('遍历 联系人')">🏗️ 遍历 联系人</span>
            <span class="quick-btn" onclick="quickSend('测试 Settings 的 WiFi 开关')">📶 测试 WiFi</span>
            <span class="quick-btn" onclick="quickSend('回放 feedback_submit.yaml')">🔄 回放反馈用例</span>
        </div>

        <div class="chat-area" id="chat"></div>

        <div class="input-area">
            <input type="text" id="input" placeholder="输入测试指令，如：帮我遍历 Settings 的所有页面"
                   onkeydown="if(event.key==='Enter') send()">
            <button onclick="send()">发送</button>
        </div>
    </div>

    <script>
        const chatEl = document.getElementById('chat');
        const inputEl = document.getElementById('input');
        const historyEl = document.getElementById('history');
        const sessionId = 'session-' + Date.now();

        // ── WebSocket ──
        let ws = null;
        function connectWS() {
            ws = new WebSocket(`ws://${location.host}/ws/chat`);
            ws.onmessage = (e) => {
                const data = JSON.parse(e.data);
                handleWSMessage(data);
            };
            ws.onclose = () => setTimeout(connectWS, 3000);
        }
        connectWS();

        function handleWSMessage(data) {
            switch (data.type) {
                case 'status':
                    addMessage(data.content, 'system');
                    break;
                case 'intent':
                    showConfirmCard(data.content, data.editable_fields);
                    break;
                case 'cancelled':
                    addMessage('已取消', 'system');
                    break;
                case 'result':
                    const r = data.content;
                    addMessage(r.conclusion || r.message || JSON.stringify(r, null, 2), 'agent');
                    addToHistory(r);
                    break;
            }
        }

        // ── 发送消息 ──
        function send() {
            const msg = inputEl.value.trim();
            if (!msg) return;
            addMessage(msg, 'user');
            inputEl.value = '';

            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'parse', message: msg }));
            } else {
                // 降级到 HTTP
                fetchParse(msg);
            }
        }

        function quickSend(msg) {
            inputEl.value = msg;
            send();
        }

        async function fetchParse(msg) {
            addMessage('正在解析...', 'system');
            const res = await fetch('/api/parse', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ message: msg, session_id: sessionId })
            });
            const data = await res.json();
            showConfirmCard(data.intent, data.editable_fields);
        }

        // ── 确认卡片 ──
        function showConfirmCard(intent, fields) {
            const card = document.createElement('div');
            card.className = 'confirm-card';
            card.dataset.intent = JSON.stringify(intent);

            const isTraverse = intent.intent === 'traverse';

            card.innerHTML = `
                <div class="card-header">
                    <span class="icon">📋</span>
                    <span>测试计划确认</span>
                </div>
                <div class="card-body">
                    <div class="field-row">
                        <label>模式</label>
                        <select id="f-intent" class="field-input">
                            <option value="traverse" ${intent.intent==='traverse'?'selected':''}>🏗️ 遍历建基线</option>
                            <option value="replay"   ${intent.intent==='replay'?'selected':''}>🔄 回放对比</option>
                            <option value="run"      ${intent.intent==='run'?'selected':''}>▶️ 直接执行</option>
                            <option value="run_case" ${intent.intent==='run_case'?'selected':''}>📄 执行用例</option>
                        </select>
                    </div>
                    <div class="field-row">
                        <label>应用名称</label>
                        <input id="f-app-name" type="text" value="${intent.app_name || ''}"
                               placeholder="Settings" class="field-input">
                    </div>
                    <div class="field-row">
                        <label>包名</label>
                        <input id="f-package" type="text" value="${intent.app_package || ''}"
                               placeholder="com.android.settings" class="field-input">
                    </div>
                    <div class="field-row">
                        <label>范围</label>
                        <select id="f-scope" class="field-input">
                            <option value="full"    ${intent.scope==='full'?'selected':''}>全部页面</option>
                            <option value="partial" ${intent.scope==='partial'?'selected':''}>指定页面</option>
                        </select>
                    </div>
                    <div class="field-row" id="depth-row" style="display:${isTraverse?'flex':'none'}">
                        <label>遍历深度</label>
                        <input id="f-depth" type="number" value="${intent.traversal_max_depth||5}"
                               min="1" max="10" class="field-input field-num">
                    </div>
                    <div class="field-row" id="pages-row" style="display:${isTraverse?'flex':'none'}">
                        <label>最大页面</label>
                        <input id="f-pages" type="number" value="${intent.traversal_max_pages||50}"
                               min="1" max="200" class="field-input field-num">
                    </div>
                    <div class="field-row">
                        <label>任务描述</label>
                        <textarea id="f-desc" class="field-input field-textarea"
                        >${intent.task_description || ''}</textarea>
                    </div>
                </div>
                <div class="card-actions" id="card-actions">
                    <button class="btn btn-cancel" onclick="cancelCard(this)">❌ 取消</button>
                    <button class="btn btn-confirm" onclick="confirmCard(this)">✅ 确认执行</button>
                </div>
            `;

            chatEl.appendChild(card);
            chatEl.scrollTop = chatEl.scrollHeight;

            // 模式切换
            card.querySelector('#f-intent').addEventListener('change', (e) => {
                const isT = e.target.value === 'traverse';
                card.querySelector('#depth-row').style.display = isT ? 'flex' : 'none';
                card.querySelector('#pages-row').style.display = isT ? 'flex' : 'none';
            });
        }

        // ── 确认执行 ──
        function confirmCard(btn) {
            const card = btn.closest('.confirm-card');
            const intent = {
                intent: card.querySelector('#f-intent').value,
                app_name: card.querySelector('#f-app-name').value,
                app_package: card.querySelector('#f-package').value,
                scope: card.querySelector('#f-scope').value,
                task_description: card.querySelector('#f-desc').value,
                case_file: '',
                target_pages: [],
                extra_context: '',
            };

            if (intent.intent === 'traverse') {
                intent.traversal_max_depth = parseInt(card.querySelector('#f-depth').value) || 5;
                intent.traversal_max_pages = parseInt(card.querySelector('#f-pages').value) || 50;
            }

            card.querySelector('#card-actions').innerHTML = '<span class="executing">⏳ 执行中...</span>';

            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'confirm', intent, confirmed: true }));
            } else {
                fetchConfirm(intent);
            }
        }

        async function fetchConfirm(intent) {
            const res = await fetch('/api/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ session_id: sessionId, intent, confirmed: true })
            });
            const data = await res.json();
            addMessage(data.message || JSON.stringify(data.data, null, 2), 'agent');
        }

        function cancelCard(btn) {
            const card = btn.closest('.confirm-card');
            card.querySelector('#card-actions').innerHTML = '<span class="cancelled">已取消</span>';
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'confirm', confirmed: false }));
            }
            setTimeout(() => card.remove(), 1500);
        }

        // ── 消息渲染 ──
        function addMessage(text, type) {
            // 移除旧的 "解析中" system 消息
            if (type !== 'system') {
                const systems = chatEl.querySelectorAll('.msg.system');
                systems.forEach(s => {
                    if (s.textContent.includes('正在解析')) s.remove();
                });
            }

            const div = document.createElement('div');
            div.className = `msg ${type}`;
            div.textContent = text;
            chatEl.appendChild(div);
            chatEl.scrollTop = chatEl.scrollHeight;
        }

        function addToHistory(result) {
            const item = document.createElement('div');
            item.className = 'history-item';
            const mode = result.mode || 'run';
            const icon = {traverse:'🏗️', replay:'🔄', run:'▶️', run_case:'📄'}[mode] || '📝';
            item.textContent = `${icon} ${result.name || result.conclusion?.substring(0, 30) || '测试'}`;
            historyEl.prepend(item);
        }
    </script>
</body>
</html>
```

---

## 6. ReAct Agent 设计 (`core/agent.py`)

### 6.1 Agent 创建

```python
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from core.tools import ALL_TOOLS
from prompts.system_prompt import SYSTEM_PROMPT
from core.knowledge_base import build_rag_enhanced_prompt


def create_test_agent(
    model: str = "gpt-4o",
    api_key: str = None,
    base_url: str = None,
    enable_memory: bool = True,
    knowledge_base=None,
    app_package: str = "",
):
    llm = ChatOpenAI(
        model=model, temperature=0.1,
        api_key=api_key, base_url=base_url,
    )

    system_prompt = SYSTEM_PROMPT
    if knowledge_base and app_package:
        system_prompt = build_rag_enhanced_prompt(
            SYSTEM_PROMPT, knowledge_base, app_package
        )

    checkpointer = MemorySaver() if enable_memory else None

    return create_react_agent(
        model=llm,
        tools=ALL_TOOLS,
        state_modifier=system_prompt,
        checkpointer=checkpointer,
    )
```

### 6.2 System Prompt

```
你是一个移动端自动化测试专家 Agent。你的任务是根据用户的测试目标，一步步操作手机/平板完成测试。

## 你的能力
你可以使用以下工具操作设备：

### 基础操作
- click: 点击屏幕元素
- type_input: 输入文字
- swipe: 滑动屏幕
- press_key: 按系统键（back/home/enter）
- navigate_to: 切换导航Tab
- scroll_find_and_click: 滑动查找并点击
- launch_app: 启动应用

### 感知操作
- get_screen_info: 获取当前页面UI结构（快速，用UI树）
- get_detailed_screen: 获取当前页面详细视觉分析（准确但贵，用截图）
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
1. 先用 get_screen_info() 了解当前页面
2. 根据测试目标规划下一步
3. 执行操作
4. 观察结果，决定继续还是结束
5. 每步操作后简要说明结果

## 规则
1. 每次操作前先看屏幕状态（get_screen_info）
2. 看到弹窗/对话框 → 优先处理（点允许/确定/关闭）
3. 输入文字前 → 先点击输入框获取焦点
4. 找不到元素 → 用 scroll_find_and_click
5. 不确定下一步 → 用 press_key(back) 或 swipe
6. UI树模式看不清 → 切换到 vision 模式（get_detailed_screen）
7. 断言时 → 根据实际UI状态判断，不要猜测
8. 所有步骤完成 → 给出测试结论（PASS/FAIL）和原因
9. 每步操作要调用 log_step() 记录
10. 每步操作后调用 check_page_health() 检测异常
11. 发现白屏/黑屏/ANR/崩溃 → 立即报告并调用 recover_from_anomaly() 恢复
12. 回放模式下，关键步骤调用 check_against_baseline() 与基线对比
13. 检测到严重异常（ANR/崩溃）→ 记录后尝试恢复，恢复失败则终止测试
```

---

## 7. Tools 设计 (`core/tools.py`)

### 7.1 基础操作

```python
from langchain_core.tools import tool
import uiautomator2 as u2
import time
import os
import json
import xml.etree.ElementTree as ET
from datetime import datetime

device: u2.Device = None
perceiver = None
knowledge_base = None
anomaly_detector = None
baseline_store = None


def init_device(serial: str = None):
    global device
    device = u2.connect(serial)
    return device


@tool
def click(text: str = "", resource_id: str = "", description: str = "") -> str:
    """点击屏幕上的元素。优先用 text 定位，其次 resource_id，最后 description。"""
    if text:
        el = device(text=text)
        target = text
    elif resource_id:
        el = device(resourceId=resource_id)
        target = resource_id
    elif description:
        el = device(description=description)
        target = description
    else:
        return "错误：必须提供 text、resource_id 或 description 之一"
    if el.exists(timeout=3):
        el.click()
        time.sleep(0.5)
        return f"成功点击: {target}"
    return f"未找到元素: {target}"


@tool
def type_input(text: str, clear_first: bool = True) -> str:
    """在当前焦点输入框中输入文字。"""
    if clear_first:
        device.clear_text()
    device.send_keys(text)
    time.sleep(0.3)
    return f"已输入: {text}"


@tool
def swipe(direction: str, distance: float = 0.5) -> str:
    """滑动屏幕。direction: up/down/left/right"""
    w = device.info["displayWidth"]
    h = device.info["displayHeight"]
    cx, cy = w // 2, h // 2
    d = int(min(w, h) * distance)
    coords = {
        "up": (cx, cy + d // 2, cx, cy - d // 2),
        "down": (cx, cy - d // 2, cx, cy + d // 2),
        "left": (cx + d // 2, cy, cx - d // 2, cy),
        "right": (cx - d // 2, cy, cx + d // 2, cy),
    }
    if direction not in coords:
        return f"错误：无效方向 {direction}"
    device.swipe(*coords[direction])
    time.sleep(0.5)
    return f"已向{direction}滑动"


@tool
def press_key(key: str) -> str:
    """按系统键。key: back/home/enter/volume_up/volume_down"""
    valid_keys = ["back", "home", "enter", "volume_up", "volume_down"]
    if key not in valid_keys:
        return f"错误：无效按键 {key}"
    device.press(key)
    time.sleep(0.5)
    return f"已按{key}"


@tool
def navigate_to(tab_name: str) -> str:
    """点击导航栏的指定Tab"""
    el = device(text=tab_name)
    if el.exists(timeout=3):
        el.click()
        time.sleep(1)
        return f"已切换到: {tab_name}"
    return f"未找到Tab: {tab_name}"


@tool
def scroll_find_and_click(text: str, max_swipes: int = 5, direction: str = "down") -> str:
    """向指定方向滑动查找目标文字并点击。"""
    for i in range(max_swipes):
        if device(text=text).exists(timeout=1):
            device(text=text).click()
            time.sleep(0.5)
            return f"第{i+1}次滑动后找到并点击: {text}"
        w = device.info["displayWidth"]
        h = device.info["displayHeight"]
        if direction == "down":
            device.swipe(w//2, h*3//4, w//2, h//4)
        else:
            device.swipe(w//2, h//4, w//2, h*3//4)
        time.sleep(0.5)
    return f"滑动{max_swipes}次后仍未找到: {text}"


@tool
def launch_app(package: str) -> str:
    """启动指定Android应用"""
    device.app_start(package)
    time.sleep(2)
    current = device.app_current()
    if current["package"] == package:
        return f"已启动: {package}"
    return f"启动可能失败，当前应用: {current['package']}"
```

### 7.2 感知操作

```python
@tool
def get_screen_info() -> str:
    """获取当前屏幕的UI结构信息（快速模式，使用UI树解析）。"""
    global perceiver
    if perceiver:
        return perceiver.perceive_ui_tree()
    xml_str = device.dump_hierarchy()
    root = ET.fromstring(xml_str)
    elements = []
    for node in root.iter():
        text = node.get("text", "")
        desc = node.get("content-desc", "")
        rid = node.get("resource-id", "")
        clickable = node.get("clickable", "false") == "true"
        if text or desc:
            prefix = "[可点击] " if clickable else ""
            elements.append(f"- {prefix}{text or desc} (id={rid})")
    return "\n".join(elements[:60]) if elements else "页面为空或无法解析"


@tool
def get_detailed_screen() -> str:
    """获取当前屏幕的详细视觉分析（使用截图+多模态AI）。"""
    global perceiver
    if perceiver:
        return perceiver.perceive_vision()
    return "错误：感知器未初始化"


@tool
def switch_perception_mode(mode: str) -> str:
    """切换感知模式。mode: ui_tree/vision/hybrid"""
    global perceiver
    if perceiver:
        perceiver.switch_mode(mode)
        return f"感知模式已切换为: {mode}"
    return "错误：感知器未初始化"
```

### 7.3 断言操作

```python
@tool
def assert_element_exists(text: str) -> str:
    """断言屏幕上存在包含指定文字的元素"""
    if device(text=text).exists(timeout=3):
        return f"断言通过: 元素 '{text}' 存在"
    return f"断言失败: 元素 '{text}' 不存在"


@tool
def assert_text_in_list(text: str) -> str:
    """断言当前页面的列表中存在包含指定文字的条目"""
    xml_str = device.dump_hierarchy()
    root = ET.fromstring(xml_str)
    matches = []
    for node in root.iter():
        node_text = node.get("text", "")
        if text in node_text and node_text:
            matches.append(node_text)
    if matches:
        return f"断言通过: 列表中找到 {len(matches)} 个匹配项 — {matches[:3]}"
    return f"断言失败: 列表中未找到包含 '{text}' 的条目"


@tool
def assert_page_contains(text: str) -> str:
    """断言当前页面包含指定文字"""
    if device(textContains=text).exists(timeout=2):
        return f"断言通过: 页面包含 '{text}'"
    return f"断言失败: 页面不包含 '{text}'"
```

### 7.4 异常检测

```python
@tool
def check_page_health(app_package: str = "") -> str:
    """检测当前页面健康状态。纯本地检测，零成本。"""
    global anomaly_detector, device
    if not app_package:
        try:
            app_package = device.app_current()["package"]
        except Exception:
            return "错误：无法获取当前应用信息"
    if not anomaly_detector:
        return "错误：异常检测器未初始化"
    result = anomaly_detector.detect(app_package=app_package, check_baseline=False)
    return result.summary


@tool
def check_against_baseline(page_key: str, app_package: str = "") -> str:
    """将当前页面与基线对比，检测白屏/黑屏/显示不全/布局异常。"""
    global anomaly_detector, device
    if not app_package:
        try:
            app_package = device.app_current()["package"]
        except Exception:
            return "错误：无法获取当前应用信息"
    if not anomaly_detector:
        return "错误：异常检测器未初始化"
    result = anomaly_detector.detect(
        app_package=app_package, page_key=page_key, check_baseline=True
    )
    if result.is_healthy:
        return f"✅ 页面 '{page_key}' 与基线一致，无异常"
    output = f"❌ 检测到 {len(result.anomalies)} 个异常:\n"
    for a in result.anomalies:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}[a.severity]
        output += f"\n{icon} [{a.type.value}] {a.description}"
    return output


@tool
def recover_from_anomaly(app_package: str = "") -> str:
    """从异常中恢复。1.关闭弹窗 2.按返回键 3.重启应用"""
    global device
    if not app_package:
        try:
            app_package = device.app_current()["package"]
        except Exception:
            return "错误：无法获取当前应用信息"
    for text in ["允许", "确定", "同意", "OK", "Allow", "关闭", "知道了", "跳过"]:
        if device(text=text).exists(timeout=1):
            device(text=text).click()
            time.sleep(0.5)
            return f"已关闭弹窗: '{text}'"
    device.press("back")
    time.sleep(0.5)
    try:
        if device.app_current()["package"] == app_package:
            return "已按返回键，应用仍在前台"
    except Exception:
        pass
    device.app_start(app_package)
    time.sleep(2)
    return f"已重启应用: {app_package}"
```

### 7.5 知识库

```python
@tool
def query_app_knowledge(query: str, app_package: str = "") -> str:
    """查询APP的历史测试知识。"""
    global knowledge_base
    if not knowledge_base:
        return "知识库未初始化"
    results = knowledge_base.query(query=query, app_package=app_package or None, top_k=3)
    if not results:
        return f"未找到关于 '{query}' 的知识"
    output = f"找到 {len(results)} 条相关知识:\n"
    for i, r in enumerate(results, 1):
        output += f"\n{i}. {r['content']}"
        output += f"\n   (来源: {r['metadata'].get('test_case', '?')}, 相关度: {1 - r['score']:.2f})\n"
    return output


@tool
def save_current_page_knowledge(page_name: str) -> str:
    """保存当前页面的UI结构到知识库。"""
    global knowledge_base, device
    if not knowledge_base:
        return "知识库未初始化"
    app_info = device.app_current()
    app_package = app_info["package"]
    xml_str = device.dump_hierarchy()
    root = ET.fromstring(xml_str)
    elements = []
    for node in root.iter():
        text = node.get("text", "")
        desc = node.get("content-desc", "")
        clickable = node.get("clickable", "false") == "true"
        if text or desc:
            elements.append({"text": text or desc, "type": "button" if clickable else "text", "clickable": clickable})
    count = knowledge_base.save_ui_structure(
        app_package=app_package, page_name=page_name,
        elements=elements, navigation={}, test_case="manual_save", success=True
    )
    return f"已保存 {count} 条关于 {app_package} {page_name} 的知识"
```

### 7.6 辅助

```python
@tool
def save_screenshot(filename: str = "") -> str:
    """保存当前屏幕截图"""
    if not filename:
        filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join("storage/screenshots", filename)
    os.makedirs("storage/screenshots", exist_ok=True)
    img = device.screenshot()
    img.save(path)
    return f"截图已保存: {path}"


@tool
def log_step(description: str, status: str = "info") -> str:
    """记录测试步骤"""
    log_entry = {"timestamp": datetime.now().isoformat(), "description": description, "status": status}
    os.makedirs("storage/results", exist_ok=True)
    with open("storage/results/test_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    emoji = {"info": "📝", "pass": "✅", "fail": "❌", "warn": "⚠️"}.get(status, "📝")
    return f"{emoji} [{status.upper()}] {description}"
```

### 7.7 完整工具列表

```python
ALL_TOOLS = [
    # 基础操作
    click, type_input, swipe, press_key,
    navigate_to, scroll_find_and_click, launch_app,
    # 感知
    get_screen_info, get_detailed_screen, switch_perception_mode,
    # 断言
    assert_element_exists, assert_text_in_list, assert_page_contains,
    # 异常检测
    check_page_health, check_against_baseline, recover_from_anomaly,
    # 知识库
    query_app_knowledge, save_current_page_knowledge,
    # 辅助
    save_screenshot, log_step,
]
```

---

## 8. SmartPerceiver (`core/smart_perceiver.py`)

```python
import hashlib
import base64
from io import BytesIO


class PerceptionMode:
    UI_TREE = "ui_tree"
    VISION = "vision"
    HYBRID = "hybrid"


class SmartPerceiver:
    """智能感知器：UI 树（快/便宜） ↔ Vision 截图（准/贵），卡住自动切换"""

    def __init__(self, device, llm_client=None,
                 default_mode=PerceptionMode.UI_TREE,
                 auto_switch=True, stuck_threshold=2):
        self.device = device
        self.llm = llm_client
        self.mode = default_mode
        self.auto_switch = auto_switch
        self.stuck_threshold = stuck_threshold
        self._last_hash = ""
        self._stuck_count = 0
        self._vision_calls = 0

    def perceive_ui_tree(self) -> str:
        import xml.etree.ElementTree as ET
        xml_str = self.device.dump_hierarchy()
        root = ET.fromstring(xml_str)
        elements = []
        for node in root.iter():
            text = node.get("text", "")
            desc = node.get("content-desc", "")
            rid = node.get("resource-id", "")
            clickable = node.get("clickable", "false") == "true"
            if text or desc:
                prefix = "[可点击] " if clickable else ""
                el_str = f"- {prefix}{text or desc}"
                if rid:
                    el_str += f" (id={rid})"
                elements.append(el_str)
        result = "\n".join(elements[:60])
        self._update_stuck(result)
        return result if result else "页面为空或无法解析"

    def perceive_vision(self) -> str:
        if not self.llm:
            return "错误：视觉模式需要 LLM 客户端"
        img = self.device.screenshot()
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        response = self.llm.invoke([
            {"role": "system", "content": VISION_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "请分析这个屏幕截图，描述当前页面状态、所有可见元素及其位置。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]}
        ])
        self._vision_calls += 1
        return response.content

    def perceive(self) -> str:
        if self.mode == PerceptionMode.UI_TREE:
            return self.perceive_ui_tree()
        elif self.mode == PerceptionMode.VISION:
            return self.perceive_vision()
        elif self.mode == PerceptionMode.HYBRID:
            return f"=== UI 树 ===\n{self.perceive_ui_tree()}\n\n=== 视觉 ===\n{self.perceive_vision()}"

    def switch_mode(self, mode):
        self.mode = mode
        self._stuck_count = 0

    def _update_stuck(self, current_result):
        result_hash = hashlib.md5(current_result.encode()).hexdigest()[:8]
        if result_hash == self._last_hash:
            self._stuck_count += 1
            if (self.auto_switch and self._stuck_count >= self.stuck_threshold
                and self.mode == PerceptionMode.UI_TREE):
                self.mode = PerceptionMode.VISION
        else:
            if self.mode == PerceptionMode.VISION and self._stuck_count > 0:
                self.mode = PerceptionMode.UI_TREE
            self._stuck_count = 0
        self._last_hash = result_hash

    @property
    def stats(self):
        return {"current_mode": self.mode, "vision_calls": self._vision_calls}


VISION_PROMPT = """你是一个移动端 UI 视觉分析专家。看截图分析当前屏幕。
输出 JSON: {"screen_type":"...","app_name":"...","elements":[...],"navigation":{...},"anomaly":null,"suggestions":"..."}"""
```

---

## 9. RAG 知识库 (`core/knowledge_base.py`)

```python
import os
from datetime import datetime
from dataclasses import dataclass, field

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document


@dataclass
class UIKnowledge:
    app_package: str
    knowledge_type: str        # ui_structure | navigation_path | test_experience
    content: str
    metadata: dict

    def to_document(self):
        return Document(page_content=self.content,
                        metadata={"app_package": self.app_package,
                                  "knowledge_type": self.knowledge_type, **self.metadata})


class KnowledgeBase:
    """RAG 知识库：测试结束自动存入，测试开始查询注入"""

    def __init__(self, persist_dir="storage/knowledge",
                 embedding_model="text-embedding-3-small",
                 api_key=None, base_url=None):
        self.persist_dir = persist_dir
        os.makedirs(persist_dir, exist_ok=True)
        self.embeddings = OpenAIEmbeddings(model=embedding_model, api_key=api_key, base_url=base_url)
        self.vectorstore = Chroma(collection_name="app_knowledge",
                                  embedding_function=self.embeddings,
                                  persist_directory=persist_dir)

    def save_knowledge(self, knowledge: UIKnowledge):
        self.vectorstore.add_documents([knowledge.to_document()])

    def save_knowledge_batch(self, knowledges: list):
        docs = [k.to_document() for k in knowledges]
        if docs:
            self.vectorstore.add_documents(docs)

    def save_ui_structure(self, app_package, page_name, elements,
                          navigation={}, test_case="", success=True):
        knowledges = []
        summary = [f"{el.get('type','元素')} '{el.get('text','')}'" +
                   (" (可点击)" if el.get("clickable") else "") for el in elements[:20]]
        knowledges.append(UIKnowledge(app_package, "ui_structure",
            f"{app_package} 的 {page_name} 页面包含以下元素：{'; '.join(summary)}",
            {"page": page_name, "test_case": test_case, "success": success,
             "timestamp": datetime.now().isoformat()}))
        if navigation and navigation.get("tabs"):
            knowledges.append(UIKnowledge(app_package, "ui_structure",
                f"{app_package} 的导航栏包含 Tab：{', '.join(navigation['tabs'])}",
                {"page": page_name, "element_type": "navigation",
                 "test_case": test_case, "success": success,
                 "timestamp": datetime.now().isoformat()}))
        self.save_knowledge_batch(knowledges)
        return len(knowledges)

    def query(self, query, app_package=None, knowledge_type=None, top_k=5):
        filter_dict = {}
        if app_package: filter_dict["app_package"] = app_package
        if knowledge_type: filter_dict["knowledge_type"] = knowledge_type
        kwargs = {"k": top_k}
        if filter_dict: kwargs["filter"] = filter_dict
        results = self.vectorstore.similarity_search_with_score(query, **kwargs)
        return [{"content": d.page_content, "metadata": d.metadata, "score": s} for d, s in results]

    def get_app_context(self, app_package):
        results = self.query(f"{app_package} 的界面结构和操作方式", app_package=app_package, top_k=10)
        if not results:
            return ""
        ui, path, exp = [], [], []
        for r in results:
            t = r["metadata"].get("knowledge_type", "")
            if t == "ui_structure": ui.append(r["content"])
            elif t == "navigation_path": path.append(r["content"])
            elif t == "test_experience": exp.append(r["content"])
        parts = [f"### 关于 {app_package} 的已知信息\n"]
        if ui:
            parts.append("**界面结构：**")
            for k in ui[:5]: parts.append(f"- {k}")
            parts.append("")
        if path:
            parts.append("**导航路径：**")
            for k in path[:3]: parts.append(f"- {k}")
            parts.append("")
        if exp:
            parts.append("**历史经验：**")
            for k in exp[:3]: parts.append(f"- {k}")
        return "\n".join(parts)

    def extract_from_test_result(self, app_package, test_case, execution_log, final_result):
        success = final_result == "PASS"
        knowledges = []
        visited = set()
        for log in execution_log:
            page = log.get("page", "未知页面")
            if page not in visited:
                visited.add(page)
                knowledges.append(UIKnowledge(app_package, "ui_structure",
                    f"{app_package} 的 {page} 页面：{log.get('observation', '')[:500]}",
                    {"page": page, "test_case": test_case, "success": success,
                     "timestamp": datetime.now().isoformat()}))
        for i in range(len(execution_log) - 1):
            f, t = execution_log[i], execution_log[i+1]
            if f.get("result") == "success":
                knowledges.append(UIKnowledge(app_package, "navigation_path",
                    f"在 {f.get('page','?')} 页面执行 '{f.get('action','?')}' 后到达 {t.get('page','?')}",
                    {"from_page": f.get("page","?"), "to_page": t.get("page","?"),
                     "test_case": test_case, "success": success,
                     "timestamp": datetime.now().isoformat()}))
        for log in execution_log:
            if log.get("result") == "fail":
                knowledges.append(UIKnowledge(app_package, "test_experience",
                    f"在 {log.get('page','?')} 执行 '{log.get('action','?')}' 失败：{log.get('error','')}",
                    {"page": log.get("page","?"), "test_case": test_case,
                     "success": False, "timestamp": datetime.now().isoformat()}))
        self.save_knowledge_batch(knowledges)
        return len(knowledges)


def build_rag_enhanced_prompt(base_prompt, knowledge_base, app_package):
    context = knowledge_base.get_app_context(app_package)
    if not context:
        return base_prompt
    return f"""{base_prompt}

## 已有的 APP 知识（来自历史测试）
{context}

注意：这些知识来自历史测试，APP 可能已更新。如果发现与当前页面不符，以当前页面为准。
"""
```

---

## 10. Baseline Store (`core/baseline_store.py`)

```python
import json
import os
import hashlib
from datetime import datetime
from dataclasses import dataclass, field, asdict

from PIL import Image
import numpy as np


@dataclass
class ElementSnapshot:
    text: str
    element_type: str
    clickable: bool
    resource_id: str = ""
    content_desc: str = ""
    bounds: str = ""


@dataclass
class PageBaseline:
    app_package: str
    page_key: str
    page_name: str
    screenshot_path: str
    screenshot_hash: str
    screenshot_phash: str
    image_width: int = 0
    image_height: int = 0
    ui_tree_hash: str = ""
    element_count: int = 0
    clickable_count: int = 0
    elements: list = field(default_factory=list)
    text_snapshot: list = field(default_factory=list)
    clickable_texts: list = field(default_factory=list)
    nav_tabs: list = field(default_factory=list)
    has_input_fields: bool = False
    activity_name: str = ""
    white_pixel_ratio: float = 0.0
    black_pixel_ratio: float = 0.0
    unique_color_count: int = 0
    mean_brightness: float = 0.0
    timestamp: str = ""
    traversal_depth: int = 0
    parent_page: str = ""

    def to_dict(self):
        d = asdict(self)
        d["elements"] = [asdict(e) if isinstance(e, ElementSnapshot) else e for e in self.elements]
        return d

    @classmethod
    def from_dict(cls, d):
        elements = d.pop("elements", [])
        baseline = cls(**d)
        baseline.elements = [ElementSnapshot(**e) if isinstance(e, dict) else e for e in elements]
        return baseline


class BaselineStore:
    """基线存储管理：保存、加载、pHash 智能匹配"""

    def __init__(self, storage_dir="storage/baselines"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)

    def _get_app_dir(self, pkg):
        d = os.path.join(self.storage_dir, pkg)
        os.makedirs(os.path.join(d, "pages"), exist_ok=True)
        return d

    def _safe_fn(self, key):
        return key.replace("/", "_").replace("\\", "_").replace("|", "_").replace(" ", "_")

    def save_page(self, app_package, page_key, page_name, screenshot,
                  ui_tree_xml, elements, activity_name="", nav_tabs=None,
                  parent_page="", traversal_depth=0):
        from imagehash import phash
        app_dir = self._get_app_dir(app_package)

        # 截图
        fn = f"{self._safe_fn(page_key)}.png"
        path = os.path.join(app_dir, "pages", fn)
        screenshot.save(path)

        # 哈希
        with open(path, "rb") as f:
            file_hash = hashlib.md5(f.read()).hexdigest()
        phash_val = str(phash(screenshot))
        ui_hash = hashlib.md5(ui_tree_xml.encode()).hexdigest()[:16]

        # 元素
        snaps, click_texts, texts, has_input = [], [], [], False
        for el in elements:
            s = ElementSnapshot(text=el.get("text",""), element_type=el.get("type","text"),
                                clickable=el.get("clickable",False),
                                resource_id=el.get("resource_id",""),
                                content_desc=el.get("content_desc",""),
                                bounds=el.get("bounds",""))
            snaps.append(s)
            if s.text: texts.append(s.text)
            if s.clickable and s.text: click_texts.append(s.text)
            if s.element_type == "input": has_input = True

        # 像素统计
        arr = np.array(screenshot)
        w_ratio = float(np.mean(np.all(arr > 240, axis=2)))
        b_ratio = float(np.mean(np.all(arr < 15, axis=2)))
        u_colors = int(len(np.unique(arr.reshape(-1, 3), axis=0)))
        brightness = float(np.mean(arr))

        baseline = PageBaseline(
            app_package=app_package, page_key=page_key, page_name=page_name,
            screenshot_path=path, screenshot_hash=file_hash, screenshot_phash=phash_val,
            image_width=screenshot.width, image_height=screenshot.height,
            ui_tree_hash=ui_hash, element_count=len(snaps), clickable_count=len(click_texts),
            elements=snaps, text_snapshot=texts, clickable_texts=click_texts,
            nav_tabs=nav_tabs or [], has_input_fields=has_input, activity_name=activity_name,
            white_pixel_ratio=w_ratio, black_pixel_ratio=b_ratio,
            unique_color_count=u_colors, mean_brightness=brightness,
            timestamp=datetime.now().isoformat(), traversal_depth=traversal_depth,
            parent_page=parent_page,
        )

        # 保存
        json_path = os.path.join(app_dir, "pages", f"{self._safe_fn(page_key)}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(baseline.to_dict(), f, ensure_ascii=False, indent=2)

        self._update_phash_index(app_package, page_key, phash_val)
        self._update_manifest(app_package, page_key)
        return baseline

    def load_page(self, app_package, page_key):
        path = os.path.join(self._get_app_dir(app_package), "pages", f"{self._safe_fn(page_key)}.json")
        if not os.path.exists(path): return None
        with open(path, "r", encoding="utf-8") as f:
            return PageBaseline.from_dict(json.load(f))

    def list_pages(self, app_package):
        m = self.load_manifest(app_package)
        return m["page_keys"] if m else []

    def load_manifest(self, app_package):
        path = os.path.join(self._get_app_dir(app_package), "manifest.json")
        if not os.path.exists(path): return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def find_best_match(self, app_package, screenshot, threshold=15):
        from imagehash import hex_to_hash
        current = hex_to_hash(str(self._compute_phash(screenshot)))
        index = self._load_phash_index(app_package)
        if not index: return None
        best_key, best_dist = None, float("inf")
        for key, h in index.items():
            d = current - hex_to_hash(h)
            if d < best_dist:
                best_dist, best_key = d, key
        if best_key and best_dist <= threshold:
            return self.load_page(app_package, best_key)
        return None

    def _compute_phash(self, image):
        from imagehash import phash
        return phash(image)

    def _update_phash_index(self, pkg, key, phash_val):
        path = os.path.join(self._get_app_dir(pkg), "phash_index.json")
        idx = {}
        if os.path.exists(path):
            with open(path, "r") as f: idx = json.load(f)
        idx[key] = phash_val
        with open(path, "w") as f: json.dump(idx, f, indent=2)

    def _load_phash_index(self, pkg):
        path = os.path.join(self._get_app_dir(pkg), "phash_index.json")
        if not os.path.exists(path): return {}
        with open(path, "r") as f: return json.load(f)

    def _update_manifest(self, pkg, key):
        path = os.path.join(self._get_app_dir(pkg), "manifest.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f: data = json.load(f)
        else:
            data = {"app_package": pkg, "total_pages": 0, "page_keys": [],
                    "created_at": datetime.now().isoformat(), "updated_at": ""}
        if key not in data["page_keys"]:
            data["page_keys"].append(key)
            data["total_pages"] = len(data["page_keys"])
            data["updated_at"] = datetime.now().isoformat()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
```

---

## 11. Anomaly Detector (`core/anomaly_detector.py`)

```python
import time
import xml.etree.ElementTree as ET
from enum import Enum
from dataclasses import dataclass, field

from PIL import Image
import numpy as np


class AnomalyType(str, Enum):
    WHITE_SCREEN = "white_screen"
    BLACK_SCREEN = "black_screen"
    SOLID_SCREEN = "solid_screen"
    INCOMPLETE_DISPLAY = "incomplete_display"
    ANR = "anr"
    CRASH = "crash"
    PROCESS_LOST = "process_lost"
    LAYOUT_MISMATCH = "layout_mismatch"
    TEXT_MISMATCH = "text_mismatch"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Anomaly:
    type: AnomalyType
    severity: Severity
    description: str
    confidence: float = 1.0
    details: dict = field(default_factory=dict)


@dataclass
class DetectionResult:
    is_healthy: bool
    anomalies: list = field(default_factory=list)
    page_key: str = ""
    detection_time_ms: float = 0.0

    @property
    def has_critical(self):
        return any(a.severity == Severity.CRITICAL for a in self.anomalies)

    @property
    def summary(self):
        if self.is_healthy:
            return "✅ 页面正常"
        parts = []
        for a in self.anomalies:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}[a.severity]
            parts.append(f"{icon} [{a.type.value}] {a.description}")
        return "\n".join(parts)


class AnomalyDetector:
    """异常检测引擎 — 纯本地执行，不调 LLM"""

    def __init__(self, device, baseline_store=None):
        self.device = device
        self.baseline_store = baseline_store

    def detect(self, app_package, page_key="", screenshot=None, check_baseline=True):
        start = time.time()
        anomalies = []
        if screenshot is None:
            screenshot = self.device.screenshot()

        # Step 1: ANR / 崩溃
        anomalies.extend(self._check_health(app_package))
        if any(a.severity == Severity.CRITICAL for a in anomalies):
            return DetectionResult(False, anomalies, page_key, (time.time()-start)*1000)

        # Step 2: 白屏 / 黑屏
        anomalies.extend(self._check_color(screenshot))

        # Step 3: Baseline 对比
        if check_baseline and self.baseline_store:
            bl = self._find_baseline(app_package, page_key, screenshot)
            if bl:
                anomalies.extend(self._check_baseline(screenshot, bl))

        return DetectionResult(len(anomalies)==0, anomalies, page_key, (time.time()-start)*1000)

    def _check_health(self, pkg):
        anomalies = []
        try:
            xml_str = self.device.dump_hierarchy()
            root = ET.fromstring(xml_str)
            for n in root.iter():
                t = n.get("text", "")
                if any(k in t for k in ["无响应", "isn't responding", "ANR"]):
                    anomalies.append(Anomaly(AnomalyType.ANR, Severity.CRITICAL, f"ANR: {t}", 0.95))
                    break
            for n in root.iter():
                t = n.get("text", "")
                if any(k in t for k in ["已停止运行", "keeps stopping", "has stopped"]):
                    anomalies.append(Anomaly(AnomalyType.CRASH, Severity.CRITICAL, f"崩溃: {t}", 0.95))
                    break
            if not anomalies:
                try:
                    cur = self.device.app_current()
                    if cur["package"] != pkg:
                        anomalies.append(Anomaly(AnomalyType.PROCESS_LOST, Severity.HIGH,
                            f"进程不在前台: {cur['package']}", 0.9))
                except: pass
        except Exception as e:
            anomalies.append(Anomaly(AnomalyType.ANR, Severity.CRITICAL, f"UI树获取失败: {e}", 0.7))
        return anomalies

    def _check_color(self, screenshot):
        anomalies = []
        arr = np.array(screenshot)
        w = float(np.mean(np.all(arr > 240, axis=2)))
        if w > 0.95:
            anomalies.append(Anomaly(AnomalyType.WHITE_SCREEN, Severity.HIGH, f"白屏 {w:.1%}", w))
        b = float(np.mean(np.all(arr < 15, axis=2)))
        if b > 0.95:
            anomalies.append(Anomaly(AnomalyType.BLACK_SCREEN, Severity.HIGH, f"黑屏 {b:.1%}", b))
        if w < 0.95 and b < 0.95:
            u = int(len(np.unique(arr.reshape(-1, 3), axis=0)))
            if u < 10:
                anomalies.append(Anomaly(AnomalyType.SOLID_SCREEN, Severity.MEDIUM, f"单色屏 {u}色", 0.8))
        return anomalies

    def _find_baseline(self, pkg, key, screenshot):
        if key:
            bl = self.baseline_store.load_page(pkg, key)
            if bl: return bl
        return self.baseline_store.find_best_match(pkg, screenshot)

    def _check_baseline(self, screenshot, bl):
        anomalies = []
        cur_count = self._count_elements()
        if bl.element_count > 0:
            ratio = cur_count / bl.element_count
            if ratio < 0.3:
                anomalies.append(Anomaly(AnomalyType.INCOMPLETE_DISPLAY, Severity.HIGH,
                    f"严重显示不全 {cur_count}/{bl.element_count}", 0.9))
            elif ratio < 0.5:
                anomalies.append(Anomaly(AnomalyType.INCOMPLETE_DISPLAY, Severity.MEDIUM,
                    f"显示不全 {cur_count}/{bl.element_count}", 0.8))
        try:
            from imagehash import hex_to_hash
            dist = hex_to_hash(self._phash(screenshot)) - hex_to_hash(bl.screenshot_phash)
            if dist > 20:
                anomalies.append(Anomaly(AnomalyType.LAYOUT_MISMATCH, Severity.MEDIUM,
                    f"布局差异大 距离{dist}", 0.7))
            elif dist > 15:
                anomalies.append(Anomaly(AnomalyType.LAYOUT_MISMATCH, Severity.LOW,
                    f"布局偏差 距离{dist}", 0.6))
        except: pass
        return anomalies

    def _count_elements(self):
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
            return sum(1 for n in root.iter() if n.get("text","") or n.get("content-desc",""))
        except: return 0

    def _phash(self, image):
        from imagehash import phash
        return str(phash(image))
```

---

## 12. Baseline Traverser (`core/baseline_traverser.py`)

```python
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field


@dataclass
class TraversalNode:
    page_key: str
    page_name: str
    depth: int
    parent_key: str = ""
    action_to_reach: str = ""


@dataclass
class TraversalResult:
    app_package: str
    total_pages: int
    visited_keys: list
    page_tree: dict
    duration_seconds: float
    errors: list = field(default_factory=list)


class BaselineTraverser:
    """BFS 遍历建基线"""

    def __init__(self, device, baseline_store, anomaly_detector=None,
                 max_depth=5, max_pages=50, click_wait=1.5, back_wait=1.0):
        self.device = device
        self.baseline_store = baseline_store
        self.anomaly_detector = anomaly_detector
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.click_wait = click_wait
        self.back_wait = back_wait
        self.visited_hashes = set()
        self.visited_keys = set()
        self.errors = []

    def traverse(self, app_package, start_page_name="首页"):
        start_time = time.time()
        page_tree = {}
        queue = deque([TraversalNode(
            page_key=self._page_key(app_package),
            page_name=start_page_name, depth=0
        )])

        while queue and len(self.visited_keys) < self.max_pages:
            node = queue.popleft()
            if node.page_key in self.visited_keys or node.depth > self.max_depth:
                continue

            print(f"[TRAVERSE] 深度 {node.depth} | {node.page_name}")

            try:
                bl = self._capture(app_package, node.page_key, node.page_name,
                                   node.parent_key, node.depth)
                self.visited_keys.add(node.page_key)
                if bl.screenshot_phash in self.visited_hashes:
                    continue
                self.visited_hashes.add(bl.screenshot_phash)
                print(f"  ✅ 元素: {bl.element_count} | 可点击: {bl.clickable_count}")
            except Exception as e:
                self.errors.append({"page": node.page_key, "error": str(e)})
                continue

            if self.anomaly_detector:
                r = self.anomaly_detector.detect(app_package, node.page_key)
                if r.has_critical:
                    continue

            children = self._discover(app_package, node)
            page_tree[node.page_key] = {
                "name": node.page_name,
                "children": [c.page_key for c in children],
                "parent": node.parent_key,
            }
            for c in children:
                if c.page_key not in self.visited_keys:
                    queue.append(c)

        elapsed = time.time() - start_time
        print(f"\n[TRAVERSE] 完成: {len(self.visited_keys)} 页, {elapsed:.1f}秒")
        return TraversalResult(app_package, len(self.visited_keys),
                               list(self.visited_keys), page_tree, elapsed, self.errors)

    def _capture(self, pkg, key, name, parent="", depth=0):
        screenshot = self.device.screenshot()
        xml_str = self.device.dump_hierarchy()
        elements = self._parse_elements(xml_str)
        tabs = [el["text"] for el in elements if el.get("clickable") and el.get("text") and len(el["text"])<=6][:10]
        try: activity = self.device.app_current().get("activity", "")
        except: activity = ""
        return self.baseline_store.save_page(pkg, key, name, screenshot, xml_str,
                                              elements, activity, tabs, parent, depth)

    def _discover(self, pkg, node):
        children = []
        xml_str = self.device.dump_hierarchy()
        clickables = self._get_clickables(xml_str)
        try: orig_act = self.device.app_current().get("activity", "")
        except: orig_act = ""

        for el in clickables:
            text = el.get("text", "") or el.get("content-desc", "")
            if not text: continue
            try:
                self._click(el)
                time.sleep(self.click_wait)
                new_key = self._page_key(pkg)
                try: new_act = self.device.app_current().get("activity", "")
                except: new_act = ""

                if new_key != node.page_key or new_act != orig_act:
                    children.append(TraversalNode(new_key, text, node.depth+1,
                                                   node.page_key, f"点击 '{text}'"))
                    self.device.press("back")
                    time.sleep(self.back_wait)
                    if self._page_key(pkg) != node.page_key:
                        self.device.app_start(pkg)
                        time.sleep(2)

                if self.anomaly_detector:
                    h = self.anomaly_detector._check_health(pkg)
                    if any(a.severity.value == "critical" for a in h):
                        self._recover(pkg)
            except Exception as e:
                self.errors.append({"page": node.page_key, "action": f"click '{text}'", "error": str(e)})
                try: self.device.press("back")
                except: pass
        return children

    def _page_key(self, pkg):
        try: act = self.device.app_current().get("activity", "unknown")
        except: act = "unknown"
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
            tab = next((n.get("text","") for n in root.iter() if n.get("selected")=="true" and n.get("text","")), "")
        except: tab = ""
        return f"{pkg}|{act}|{tab}" if tab else f"{pkg}|{act}"

    def _parse_elements(self, xml_str):
        root = ET.fromstring(xml_str)
        return [{"text": n.get("text",""), "content_desc": n.get("content-desc",""),
                 "type": "button" if n.get("clickable")=="true" else "text",
                 "clickable": n.get("clickable")=="true",
                 "resource_id": n.get("resource-id",""), "bounds": n.get("bounds","")}
                for n in root.iter() if n.get("text","") or n.get("content-desc","")]

    def _get_clickables(self, xml_str):
        root = ET.fromstring(xml_str)
        return [{"text": n.get("text",""), "content-desc": n.get("content-desc",""),
                 "resource_id": n.get("resource-id","")}
                for n in root.iter() if n.get("clickable")=="true" and (n.get("text","") or n.get("content-desc",""))]

    def _click(self, el):
        if el.get("text"): self.device(text=el["text"]).click()
        elif el.get("resource_id"): self.device(resourceId=el["resource_id"]).click()

    def _recover(self, pkg):
        for t in ["允许","确定","同意","OK","Allow","关闭","知道了"]:
            try:
                if self.device(text=t).exists(timeout=1):
                    self.device(text=t).click()
                    time.sleep(0.5)
                    break
            except: pass
        try:
            if self.device.app_current()["package"] != pkg:
                self.device.app_start(pkg)
                time.sleep(2)
        except:
            self.device.app_start(pkg)
            time.sleep(2)
```

---

## 13. State Machine (`core/state_machine.py`)

```python
import time
from enum import Enum
from typing import Optional


class TestState(str, Enum):
    RUNNING = "running"
    STUCK = "stuck"
    CRASHED = "crashed"
    POPUP = "popup"
    LOST = "lost"
    COMPLETED = "completed"
    FAILED = "failed"


class StateMachine:
    """错误恢复状态机"""

    def __init__(self, device, max_recovery=5):
        self.device = device
        self.state = TestState.RUNNING
        self.recovery_count = 0
        self.max_recovery = max_recovery

    def detect_anomaly(self) -> Optional[str]:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(self.device.dump_hierarchy())
        popup_kw = ["允许","拒绝","确定","取消","同意","暂不","稍后","关闭","跳过","知道了","不再提醒",
                     "Allow","Deny","OK","Cancel","Dismiss"]
        buttons = [n.get("text","") for n in root.iter()
                   if n.get("clickable")=="true" and n.get("text","") in popup_kw]
        if len(buttons) >= 2:
            self.state = TestState.POPUP
            return f"弹窗: {buttons}"
        return None

    def recover_popup(self):
        if self.recovery_count >= self.max_recovery: return False
        self.recovery_count += 1
        for t in ["允许","确定","同意","OK","Allow"]:
            if self.device(text=t).exists(timeout=1):
                self.device(text=t).click()
                time.sleep(0.5)
                self.state = TestState.RUNNING
                return True
        return False

    def recover_stuck(self):
        if self.recovery_count >= self.max_recovery: return False
        self.recovery_count += 1
        w, h = self.device.info["displayWidth"], self.device.info["displayHeight"]
        self.device.swipe(w//2, h*3//4, w//2, h//4)
        time.sleep(0.5)
        self.state = TestState.RUNNING
        return True

    def recover_crashed(self, package):
        if self.recovery_count >= self.max_recovery: return False
        self.recovery_count += 1
        self.device.app_start(package)
        time.sleep(2)
        self.state = TestState.RUNNING
        return True

    def reset(self):
        self.state = TestState.RUNNING
        self.recovery_count = 0
```

---

## 14. 测试用例 YAML 格式

### 14.1 反馈提交流程

```yaml
name: "反馈提交流程测试"
description: "测试服务与反馈APP的反馈提交和验证流程"
app_package: "com.lenovo.service"
app_name: "服务与反馈"

steps:
  - intent: "启动服务与反馈APP"
    type: "launch_app"

  - intent: "点击左侧导航栏的'反馈'选项"
    type: "navigate_tab"
    tab_name: "反馈"

  - intent: "在右侧输入框中输入反馈内容"
    type: "type_text"
    text: "auto ai testing submit"

  - intent: "点击提交按钮"
    type: "click"
    target: "提交"

  - intent: "等待提交完成"
    type: "wait"
    seconds: 2

  - intent: "检查反馈列表中是否存在刚提交的反馈"
    type: "assert"
    condition: "反馈列表中存在包含 'auto ai testing submit' 的条目"

verification:
  - "反馈列表中可以看到包含 'auto ai testing submit' 的记录"
  - "页面无报错信息"
```

### 14.2 搜索条件遍历

```yaml
name: "搜索条件遍历测试"
description: "测试搜索功能的条件分支和元素遍历"
app_package: "com.lenovo.service"
app_name: "服务与反馈"

steps:
  - intent: "启动服务与反馈APP"
    type: "launch_app"

  - intent: "点击搜索框，输入关键词 AI"
    type: "search"
    text: "AI"

  - intent: "观察搜索结果，如果有结果则点击第一条，如果无结果则点击取消"
    type: "conditional"
    condition: "搜索结果是否为空或显示无结果"

  - intent: "返回首页"
    type: "press_key"
    key: "back"

  - intent: "遍历导航栏所有Tab"
    type: "traverse_tabs"

  - intent: "切换到反馈Tab，输入 auto ai testing submit 并提交"
    type: "feedback_submit"

  - intent: "验证提交结果"
    type: "assert"
    condition: "反馈列表中存在 'auto ai testing submit'"

verification:
  - "搜索功能正常响应"
  - "所有导航Tab可正常切换"
  - "反馈提交成功并可在列表中查看"
```

---

## 15. 入口 (`main.py`)

```python
#!/usr/bin/env python3
"""AI 自动化测试 Agent - 入口"""

import argparse
import yaml
import json
import os
from datetime import datetime

from config import TestConfig
from core.agent import create_test_agent
from core.tools import init_device
from core.smart_perceiver import SmartPerceiver
from core.knowledge_base import KnowledgeBase
from core.baseline_store import BaselineStore
from core.anomaly_detector import AnomalyDetector
from core.baseline_traverser import BaselineTraverser
from core.intent_parser import IntentParser
from core.chat_runner import ChatRunner


def load_test_case(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_task_message(case, mode="single"):
    task = f"""## 测试任务: {case['name']}
## 描述: {case.get('description', '')}
## 目标应用: {case.get('app_name', '')} ({case.get('app_package', '')})

## 测试步骤:
"""
    for i, step in enumerate(case["steps"], 1):
        task += f"{i}. {step['intent']}\n"
    if case.get("verification"):
        task += "\n## 验收标准:\n"
        for v in case["verification"]: task += f"- {v}\n"
    task += """
## 执行要求:
1. 每步操作前先用 get_screen_info() 了解当前页面
2. 每步操作后用 check_page_health() 检测异常
3. 每步操作后用 log_step() 记录结果
4. 遇到弹窗优先处理
5. 所有步骤完成后给出测试结论（PASS/FAIL）和原因
"""
    return task


# ── 遍历 ──

def run_traverse(config, app_package, app_name=""):
    print(f"\n{'='*60}\n[TRAVERSE] {app_name} ({app_package})\n{'='*60}\n")
    device = init_device(config.device_serial)
    device.app_start(app_package)
    import time; time.sleep(2)

    store = BaselineStore(config.baseline_dir)
    detector = AnomalyDetector(device, store)
    traverser = BaselineTraverser(device, store, detector,
                                   config.traversal_max_depth, config.traversal_max_pages)
    result = traverser.traverse(app_package, app_name or "首页")

    os.makedirs("reports", exist_ok=True)
    path = f"reports/traverse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mode": "traverse", "app_package": app_package,
                    "total_pages": result.total_pages, "duration": result.duration_seconds,
                    "errors": result.errors}, f, ensure_ascii=False, indent=2)
    print(f"[REPORT] {path}")


# ── 回放 ──

def run_replay(case_path, config):
    case = load_test_case(case_path)
    pkg = case["app_package"]
    print(f"\n{'='*60}\n[REPLAY] {case['name']}\n{'='*60}\n")

    device = init_device(config.device_serial)
    store = BaselineStore(config.baseline_dir)
    pages = store.list_pages(pkg)
    if not pages:
        print(f"[ERROR] 未找到 {pkg} 的基线，请先 traverse")
        return
    print(f"[INIT] {len(pages)} 个基线页面")

    from langchain_openai import ChatOpenAI
    perceiver = SmartPerceiver(device, ChatOpenAI(model=config.vision_model, temperature=0.1,
                                                    api_key=config.api_key, base_url=config.base_url))
    detector = AnomalyDetector(device, store)
    kb = KnowledgeBase(config.rag_persist_dir, config.embedding_model,
                        config.api_key, config.base_url) if config.enable_rag else None

    agent = create_test_agent(config.model, config.api_key, config.base_url,
                               knowledge_base=kb, app_package=pkg)
    device.app_start(pkg)
    import time; time.sleep(2)

    task = build_task_message(case)
    start = datetime.now()
    result = agent.invoke({"messages": [{"role": "user", "content": task}]},
                           config={"configurable": {"thread_id": f"replay-{start.strftime('%Y%m%d_%H%M%S')}"}})
    duration = (datetime.now() - start).total_seconds()
    final = result["messages"][-1].content
    print(f"\n[DONE] {duration:.1f}秒\n{final}")

    if kb:
        log_file = "storage/results/test_log.jsonl"
        if os.path.exists(log_file):
            log = [json.loads(l) for l in open(log_file)]
            kb.extract_from_test_result(pkg, case["name"], log, "PASS" if "PASS" in final.upper() else "FAIL")


# ── 单次执行 ──

def run_single(case_path, config):
    case = load_test_case(case_path)
    pkg = case["app_package"]
    print(f"\n{'='*60}\n[TEST] {case['name']}\n{'='*60}\n")

    device = init_device(config.device_serial)
    from langchain_openai import ChatOpenAI
    perceiver = SmartPerceiver(device, ChatOpenAI(model=config.vision_model, temperature=0.1,
                                                    api_key=config.api_key, base_url=config.base_url))
    detector = AnomalyDetector(device)
    kb = KnowledgeBase(config.rag_persist_dir, config.embedding_model,
                        config.api_key, config.base_url) if config.enable_rag else None

    agent = create_test_agent(config.model, config.api_key, config.base_url,
                               knowledge_base=kb, app_package=pkg)
    device.app_start(pkg)
    import time; time.sleep(2)

    task = build_task_message(case)
    start = datetime.now()
    result = agent.invoke({"messages": [{"role": "user", "content": task}]},
                           config={"configurable": {"thread_id": f"test-{start.strftime('%Y%m%d_%H%M%S')}"}})
    duration = (datetime.now() - start).total_seconds()
    final = result["messages"][-1].content
    print(f"\n[DONE] {duration:.1f}秒\n{final}")

    if kb:
        log_file = "storage/results/test_log.jsonl"
        if os.path.exists(log_file):
            log = [json.loads(l) for l in open(log_file)]
            kb.extract_from_test_result(pkg, case["name"], log, "PASS" if "PASS" in final.upper() else "FAIL")


# ── Chat 模式 ──

def run_chat(user_input, config):
    runner = ChatRunner(config)
    result = runner.run(user_input)
    print(f"\n[RESULT] {json.dumps(result, ensure_ascii=False, indent=2)}")


# ── Web 服务 ──

def run_server(config):
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8080, reload=True)


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="AI 自动化测试 Agent")
    sub = parser.add_subparsers(dest="mode")

    t = sub.add_parser("traverse", help="遍历建基线")
    t.add_argument("--package", required=True)
    t.add_argument("--name", default="")
    t.add_argument("--config", default="config.yaml")

    r = sub.add_parser("replay", help="回放对比")
    r.add_argument("--case", required=True)
    r.add_argument("--config", default="config.yaml")

    s = sub.add_parser("run", help="单次执行")
    s.add_argument("--case", required=True)
    s.add_argument("--config", default="config.yaml")

    c = sub.add_parser("chat", help="自然语言执行")
    c.add_argument("message", help="测试指令")
    c.add_argument("--config", default="config.yaml")

    w = sub.add_parser("server", help="启动 Web 服务")
    w.add_argument("--config", default="config.yaml")

    args = parser.parse_args()
    config = TestConfig.from_yaml(getattr(args, "config", "config.yaml"))

    if args.mode == "traverse": run_traverse(config, args.package, args.name)
    elif args.mode == "replay": run_replay(args.case, config)
    elif args.mode == "run": run_single(args.case, config)
    elif args.mode == "chat": run_chat(args.message, config)
    elif args.mode == "server": run_server(config)
    else: parser.print_help()


if __name__ == "__main__":
    main()
```

---

## 16. 配置文件 (`config.py`)

```python
import os
from dataclasses import dataclass


@dataclass
class TestConfig:
    # LLM
    model: str = "gpt-4o"
    vision_model: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small"
    api_key: str = None
    base_url: str = None

    # 设备
    device_serial: str = None

    # 感知
    auto_switch_perception: bool = True
    stuck_threshold: int = 2
    enable_vision: bool = True

    # RAG
    enable_rag: bool = True
    rag_persist_dir: str = "storage/knowledge"

    # Baseline
    baseline_dir: str = "storage/baselines"
    traversal_max_depth: int = 5
    traversal_max_pages: int = 50

    # 异常检测阈值
    white_screen_threshold: float = 0.95
    black_screen_threshold: float = 0.95
    incomplete_display_ratio: float = 0.5
    phash_distance_threshold: int = 15

    @classmethod
    def from_yaml(cls, path):
        import yaml
        if not os.path.exists(path): return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
```

---

## 17. 依赖 (`requirements.txt`)

```
langchain>=0.3.0
langchain-openai>=0.2.0
langchain-chroma>=0.2.0
langgraph>=0.2.0
uiautomator2>=3.0.0
Pillow>=10.0.0
numpy>=1.24.0
imagehash>=4.3.0
pyyaml>=6.0
chromadb>=0.5.0
fastapi>=0.110.0
uvicorn>=0.27.0
websockets>=12.0
```
