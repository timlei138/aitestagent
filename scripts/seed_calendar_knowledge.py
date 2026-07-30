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
from data.knowledge import KnowledgeBase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    config = TestConfig("config.yaml")
    vs = create_vector_store(config)
    kb = KnowledgeBase(vs)

    # ── 条目一：通用自绘控件规则（scope=universal，对所有 App 生效）──
    kb.save_curated_rule(
        app_package="",
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
    logger.info("已写入联想日历专属规则 (scope=app, package=com.zui.calendar)")

    logger.info("RAG 知识写入完成。重启服务后规则将在首轮/App 切换时自动注入 agent prompt。")


if __name__ == "__main__":
    main()
