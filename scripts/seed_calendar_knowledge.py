"""写入 vision_tap 相关 RAG 人工知识（通用 Canvas 规则 + 联想日历专属规则）。

运行方式：
    cd d:/Project/python/AiAgentTest
    python scripts/seed_calendar_knowledge.py

写完后重启服务即可生效。
"""

from __future__ import annotations

import logging
import sys
import os

# 确保项目根目录在 sys.path
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from config import TestConfig
from data import create_vector_store
from data.knowledge import KnowledgeBase, UIKnowledge

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def _save(kb: KnowledgeBase, app_package: str, knowledge_type: str, content: str,
          scope: str, reviewed_by: str, domain: str, scenario: str) -> None:
    kb.save_knowledge(
        UIKnowledge(
            app_package=app_package,
            knowledge_type=knowledge_type,
            content=content,
            metadata={
                "scope": scope,
                "domain": domain,
                "scenario": scenario,
                "reviewed_by": reviewed_by,
            },
        )
    )


def main() -> None:
    config = TestConfig("config.yaml")
    vs = create_vector_store(config)
    kb = KnowledgeBase(vs)

    # ── 条目一：通用自绘控件规则（scope=universal，对所有 App 生效）──
    _save(
        kb,
        app_package="",
        knowledge_type="constraint",
        content=(
            "Canvas、SurfaceView、TextureView、WebView 内嵌内容、OpenGL 绘制区域等自绘/原生渲染控件，"
            "view tree 中无可用文本节点和 resource-id，click(label) 会返回 NOT_FOUND。"
            "此类控件必须使用 vision_tap('描述目标位置') 操作；"
            "颜色/背景/状态校验使用 visual_check('描述')。"
        ),
        scope="universal",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="custom_view",
    )
    logger.info("已写入通用 Canvas 规则 (scope=universal)")

    # ── 条目二：联想日历专属规则（scope=app）──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="semantic_hint",
        content=(
            "TimeSlotSettingsActivity（课程时间设置）页面包含「上课时长」和「课间休息」两个滚轮选择器，"
            "均为 Canvas 绘制，view tree 无文本节点。"
            "滚轮显示 3 行：中间行 = 当前选中值，上方行 = 当前值-1，下方行 = 当前值+1；"
            "点击目标行即可选中对应值。"
            "操作示例：vision_tap('上课时长滚轮中值为50的那一行')"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="canvas_widget",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #2: 单滚轮选择器")

    # ── 条目三：课程时间设置弹窗（双时间轴拾取器）──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="semantic_hint",
        content=(
            "课程时间设置弹窗（编辑小节时间）包含双时间轴拾取器，均为 Canvas 自绘，view tree 无节点。\n"
            "结构：左侧 = 开始时间（小时列 + 分钟列），右侧 = 结束时间（小时列 + 分钟列）。\n"
            "每列显示 3 个值：上方 = 上一值，中间 = 当前选中值，下方 = 下一值。\n"
            "点击上方值可选中上一值，点击下方值可选中下一值。\n"
            "\n"
            "高效设置时间策略：\n"
            "1) 设置小时：直接 vision_tap 目标小时值所在位置（上方/中间/下方），通常 1-2 次即可\n"
            "2) 设置分钟：用 vision_tap repeat 参数批量连点\n"
            "   - 先定位到分钟列「下方值」的位置\n"
            "   - 计算需要的点击次数（目标分钟 - 当前分钟，跨 0 需加 60）\n"
            "   - 一次调用完成：vision_tap('分钟列当前选中值下方的那一行', repeat=N)\n"
            "   例如：当前 00，目标 10 → vision_tap('分钟列当前选中值下方的那一行', repeat=10)\n"
            "   避免每次点击都单独调用 vision_tap（每次调用都重新截图+视觉推理，浪费 token 和时间）\n"
            "3) 如需减少分钟值，定位到分钟列「上方值」的位置，同样用 repeat=N 批量连点"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="time_picker",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #3: 双时间轴拾取器")

    # ── 条目四：时间间隔计算规则 ──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="semantic_hint",
        content=(
            "「修改时间间隔为 N 分钟」的正确操作方式：\n"
            "时间间隔 = 结束时间 - 开始时间。\n"
            "要修改间隔，只需调整结束时间，不要动开始时间。\n"
            "\n"
            "计算方法：新结束时间 = 当前开始时间 + N 分钟\n"
            "示例：当前第2节 09:00-09:50（间隔50分钟），要改为53分钟：\n"
            "  新结束时间 = 09:00 + 53 = 09:53\n"
            "  只需把结束分钟从 50 改到 53：vision_tap('结束分钟列选中值50下方的位置', repeat=3)\n"
            "  不需要修改开始时间！\n"
            "\n"
            "常见错误（不要犯）：\n"
            "  把「间隔53分钟」误解为「开始分钟设为53」→ 08:53-09:43 仍然是50分钟\n"
            "  同时修改开始和结束时间 → 间隔不变，白操作"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="time_picker",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #4: 时间间隔计算")

    # ── 条目五：时间冲突红色提示规则 ──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="semantic_hint",
        content=(
            "课程时间设置中时间冲突的红色提示行为：\n"
            "当某小节的结束时间晚于下一小节的开始时间时，产生时间冲突。\n"
            "冲突表现：该小节及冲突小节的时间文本会显示为红色。\n"
            "注意：小节序号（如「第4节」）保持黑色是正常行为，不会变红。\n"
            "验证规则：只要时间文本显示为红色，即可认为红色冲突提示已生效，验证通过。\n"
            "不要因为序号是黑色就认为验证失败。"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="visual_verification",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #5: 时间冲突红色提示")

    # ── 条目六：Toast 捕获策略 ──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="semantic_hint",
        content=(
            "Toast 提示捕获策略：\n"
            "Toast 显示时间极短（约 2 秒），普通 visual_check 截图时 toast 已消失。\n"
            "正确做法：使用 click_and_check(label, check_desc) 工具——点击后立即截图再送 vision 分析。\n"
            "示例：click_and_check('完成', '屏幕底部是否出现toast提示') \n"
            "如果没有 click_and_check 工具，可用 detect_overlay 或 get_screen_info 快速检测。\n"
            "如果多种方式都无法捕获 toast，但 visual_check 已确认时间显示为红色，\n"
            "可认为冲突提示已生效（红色本身就是冲突的视觉提示），由 evaluator 记录 toast 未捕获。"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="toast_capture",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #6: Toast 捕获策略")

    # ── 条目七：课程表导航路径（TimetableActivity → TimetableListActivity）──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="semantic_hint",
        content=(
            "课程表创建/管理的导航路径：\n"
            "1) 从 AllInOneActivity（日历主页）进入课程表：\n"
            "   点击 iv_more（右上角更多按钮）展开菜单 → 点击「课程表」→ 进入 TimetableActivity\n"
            "\n"
            "2) 从 TimetableActivity 进入课程表列表（TimetableListActivity）：\n"
            "   点击「课程表设置」按钮 → TimetableListActivity\n"
            "   注意：点击工具栏标题（toolbar_title）会弹出「切换课表」对话框，不是列表页\n"
            "\n"
            "3) 在 TimetableListActivity 中：\n"
            "   「添加课程表」按钮用于新建课程表 → 进入 EditTimetableActivity（编辑页）\n"
            "   已有课程表以列表形式展示，点击可进入对应课程表\n"
            "\n"
            "4) 工具栏「+」加号按钮（toolbar 标题旁）是「导入课程表」功能，\n"
            "   点击后弹出「拍照导入课程表 / 图库导入课程表」面板，不是新建入口"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="navigation",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #7: 课程表导航路径")

    # ── 条目八：创建空课程表 ≠ 清空数据（负知识，堵错误理解）──
    _save(
        kb,
        app_package="com.zui.calendar",
        knowledge_type="negative_knowledge",
        content=(
            "「创建空课程表」≠「清空应用数据 / clear_app_data」。\n"
            "测试前提「创建空课程表」的正确做法：在 TimetableListActivity 点「添加课程表」"
            "新建一个空课表（进入 EditTimetableActivity 后命名、关闭「周末是否有课」、保存）。\n"
            "看到残留课程（如已存在的「语文」课）时，用「新建课表」绕开即可，"
            "不要清除/重置 App 数据（clear_app_data 会删除全部用户数据，不可恢复）。"
        ),
        scope="app",
        reviewed_by="dev",
        domain="ui_interaction",
        scenario="create_empty_timetable",
    )
    logger.info("已写入联想日历专属规则 (scope=app) #8: 创建空课程表≠清空数据")

    logger.info(
        "RAG 知识写入完成。重启服务后规则将在首轮/App 切换时自动注入 agent prompt。"
    )


if __name__ == "__main__":
    main()
