"""独立测试脚本：用 uiautomator2 直连真机，检查当前页面右上角按钮。

不依赖项目现有代码。直接连设备、dump UI 树，找出顶部 action bar 右侧的按钮，
回答两个问题：
  1) 右上角到底有几个按钮？
  2) 它们是否都是「更多设置 / 更多选项」？

用法:
    python scripts/test_topright_buttons.py [--serial <adb-serial>]

依赖: uiautomator2 (pip install uiautomator2)
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET

import uiautomator2 as u2


def parse_bounds(raw: str) -> tuple[int, int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", raw or "")]
    if len(nums) == 4:
        return nums[0], nums[1], nums[2], nums[3]
    return 0, 0, 0, 0


# action bar 中常见的「按钮型」控件 class（图标按钮 / 菜单项 / 纯 ImageButton）
_BUTTON_CLASSES = (
    "imagebutton",
    "menuitemview",
    "imageview",
    "button",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查当前页面右上角按钮")
    parser.add_argument("--serial", default=None, help="adb 设备 serial（省略则默认连接）")
    args = parser.parse_args()

    print(">>> 正在连接设备 ...")
    d = u2.connect(args.serial)
    w, h = d.window_size()
    print(f">>> 已连接: {d}\n>>> 屏幕尺寸: {w} x {h}\n")

    # ── 1) UIAutomator 直连 selector 验证：是否存在「更多设置/更多选项」 ──
    for kw in ("更多设置", "更多选项"):
        sel = d(description=kw)
        try:
            cnt = sel.count if hasattr(sel, "count") else (1 if sel.exists else 0)
        except Exception:
            cnt = None
        exists = sel.exists
        print(f"[selector] description='{kw}': exists={exists} count={cnt}")

    # ── 2) dump 整页 UI 树，按区域分析右上角按钮 ──
    xml = d.dump_hierarchy()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        print(f"[ERROR] UI 树解析失败: {exc}")
        return 2

    # 顶部 action bar 区域：y 落在屏幕顶部 ~12% 内
    top_thresh = max(1, int(h * 0.12))
    # 右半屏：用于判定「右上角」按钮（中心 x > 宽度一半）
    right_thresh = w // 2

    buttons: list[dict] = []
    for node in root.iter():
        vis = (node.get("visibility") or "").lower()
        if vis in ("gone", "invisible"):
            continue
        bounds = parse_bounds(node.get("bounds", ""))
        if bounds == (0, 0, 0, 0):
            continue
        x1, y1, x2, y2 = bounds
        cls = (node.get("class", "") or "").lower()
        clickable = node.get("clickable", "false") == "true"

        # 必须是 action bar 顶部区域
        if y1 >= top_thresh:
            continue
        # 必须在右半屏（右上角）
        cx = (x1 + x2) // 2
        if cx <= right_thresh:
            continue
        # 是按钮型 / 可点击控件
        is_btn = clickable or any(k in cls for k in _BUTTON_CLASSES)
        if not is_btn:
            continue

        text = node.get("text", "") or ""
        desc = node.get("content-desc", "") or ""
        rid = node.get("resource-id", "") or ""
        buttons.append(
            {
                "class": node.get("class", "") or "",
                "label": text or desc or rid,
                "desc": desc,
                "text": text,
                "bounds": bounds,
                "cx": cx,
            }
        )

    # 按中心 x 降序（最靠右的排最前）
    buttons.sort(key=lambda b: -b["cx"])

    print(f"\n=== 右上角(action bar 右半屏) 共识别到 {len(buttons)} 个按钮/图标 ===")
    for i, b in enumerate(buttons):
        x1, y1, x2, y2 = b["bounds"]
        tag = ""
        if b["desc"] in ("更多设置", "更多选项"):
            tag = "  <-- 更多设置/选项"
        print(
            f"  [{i}] {b['class'].split('.')[-1]:14s} "
            f"bounds=({x1},{y1},{x2},{y2}) "
            f"label='{b['label'][:20]}'{tag}"
        )

    print("\n=== 结论 ===")
    if not buttons:
        print("  [空] 右上角未识别到任何按钮（本页可能没有 action bar 图标）。")
        return 0

    more_kw = ("更多设置", "更多选项")
    more_btns = [b for b in buttons if b["desc"] in more_kw]
    print(f"  右上角按钮总数: {len(buttons)}")
    print(f"  其中标记为『更多设置/更多选项』的: {len(more_btns)}")

    # 用户关心的「三个按钮是否都是更多设置」
    if len(buttons) == 3:
        if len(more_btns) == 3:
            print("  [判定] 三个按钮全部是『更多设置/更多选项』 ✓")
        else:
            others = [b["label"] for b in buttons if b["desc"] not in more_kw]
            print(f"  [判定] 并非都是：其余按钮 label = {others}")
    else:
        print(f"  [提示] 当前右上角不是 3 个按钮（实际 {len(buttons)} 个），"
              f"无法套用『三个都是更多设置』的判定。")
        if more_btns:
            print(f"  [提示] 最靠右(通常即溢出菜单) 是: '{more_btns[0]['label']}' "
                  f"bounds={more_btns[0]['bounds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
