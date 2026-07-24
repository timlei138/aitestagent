"""模拟前端收到的数据：用与 SmartPerceiver 完全相同的 label 规则，
在真机上 dump 当前页面，打印 action bar 按钮组中每个元素的真实 label。

只依赖 uiautomator2，不引入项目其他模块。
复刻 device/perceiver.py 的 UIElement.label 计算：
    label = text or content_desc or associated_label
（已从兜底链中移除 resource_id，避免把开发者命名错标成语义标签；
 对有名按钮 associated_label 为空，故等价于 text or content_desc）

用法:
    python scripts/check_frontend_labels.py [--serial <adb-serial>]
"""
from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET

import uiautomator2 as u2


def parse_bounds(raw: str) -> tuple[int, int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", raw or "")]
    return tuple(nums) if len(nums) == 4 else (0, 0, 0, 0)


def perceiver_label(node: ET.Element) -> str:
    """复刻 perceiver.UIElement.label 的计算口径。

    注意：真实 perceiver 还会做「重复 label 抑制」——同一 label 出现在多个控件上
    时会被置空（不可信就不给）。本脚本只复刻单元素口径，重复检测见 perceiver.parse_elements。
    """
    text = node.get("text", "") or ""
    desc = node.get("content-desc", "") or ""
    return text or desc


def main() -> int:
    parser = argparse.ArgumentParser(description="检查前端/LLM 实际收到的按钮 label")
    parser.add_argument("--serial", default=None)
    args = parser.parse_args()

    print(">>> 连接设备 ...")
    d = u2.connect(args.serial)
    w, h = d.window_size()
    print(f">>> 已连接: {d}  屏幕尺寸: {w}x{h}\n")

    xml = d.dump_hierarchy()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        print(f"[ERROR] 解析失败: {exc}")
        return 2

    # 找到 toolbar 内的按钮组（最后一层 LinearLayoutCompat / 含多个 ImageView 的容器）
    # 这里直接遍历所有 ImageView/ImageButton，筛出右半屏、y 在 action bar 顶部。
    top_thresh = max(1, int(h * 0.15))
    right_half = w // 2

    print("=== 所有在右半屏 + 顶部的 image 类控件（前端 overlay 会画的按钮）===\n")
    rows = []
    for node in root.iter():
        vis = (node.get("visibility") or "").lower()
        if vis in ("gone", "invisible"):
            continue
        cls = (node.get("class", "") or "").lower()
        if "image" not in cls:
            continue
        bounds = parse_bounds(node.get("bounds", ""))
        if bounds == (0, 0, 0, 0):
            continue
        x1, y1, x2, y2 = bounds
        if y1 >= top_thresh or x1 <= right_half:
            continue
        label = perceiver_label(node)
        rows.append((x1, node, label, bounds))

    rows.sort(key=lambda r: -r[0])  # 最右在前

    for i, (x1, node, label, bounds) in enumerate(rows):
        rid = node.get("resource-id", "") or "-"
        short_rid = rid.split("/")[-1] if rid else "-"
        clickable = node.get("clickable", "false")
        # 复刻 DeviceFloat overlay 的显示：label 取前 12 字符
        shown = (label or "")[:12]
        print(
            f"  [{i}] class={node.get('class','').split('.')[-1]:14s} "
            f"clickable={clickable}\n"
            f"       resource-id='{short_rid}'\n"
            f"       text='{(node.get('text','') or '')}'\n"
            f"       content-desc='{(node.get('content-desc','') or '')}'\n"
            f"       => perceiver.label = '{label}'\n"
            f"       => 前端 overlay 显示 = '{shown}'\n"
        )

    print("=== 结论 ===")
    if not rows:
        print("  右上角没有 image 类控件。")
        return 0
    labels = [r[2] for r in rows]
    print(f"  右上角共 {len(rows)} 个 image 控件")
    print(f"  各自的 perceiver.label: {labels}")
    if len(set(labels)) == 1 and labels[0]:
        print(f"  [!!] 所有按钮的 label 都是 '{labels[0]}' —— "
              f"前端会全部显示成它，且 LLM 收到的 View Tree 也全是这个 label（错误）。")
    else:
        print("  [OK] 各按钮 label 不同，前端显示应各不相同；LLM View Tree 也正确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
