"""dump 当前页面右上角区域，判断是不是「更多选项」(overflow menu)。

用途：排查 Agent 是否把右上角误当成「更多选项」，或确认当前页 action bar
右上角到底是什么控件。直接连真机（uiautomator2），无需 LLM / 常驻服务。

用法:
    python scripts/dump_more_options.py [--serial <adb-serial>] [--top 0.1] [--right 0.8]

输出:
    1) 终端打印：屏幕尺寸、右上角候选控件列表（按 x2 降序、y1 升序）、
       是否命中 content-desc="更多选项"。
    2) 原始 UI 树 XML 落盘到 logs/dumps/<时间戳>.xml，便于离线检查。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import device.controller as controller
from device.controller import DeviceController, DeviceUnavailableError

MORE_OPTIONS_DESC = "更多选项"


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", raw or "")]
    if len(nums) == 4:
        return nums[0], nums[1], nums[2], nums[3]
    return 0, 0, 0, 0


def find_top_right_elements(
    xml: str, width: int, height: int, top_frac: float, right_frac: float
) -> list[dict]:
    """返回落在「右上角区域」内的所有可见节点（含冗余系统元素）。

    top_frac / right_frac 定义区域：y1 < height*top_frac 且 x2 > width*right_frac。
    返回按 x2 降序、y1 升序，最靠近右上角的排最前。
    """
    top_thresh = max(1, int(height * top_frac))
    right_thresh = max(1, int(width * right_frac))
    hits: list[dict] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return hits
    for node in root.iter():
        vis = (node.get("visibility") or "").lower()
        if vis in ("gone", "invisible"):
            continue
        bounds = _parse_bounds(node.get("bounds", ""))
        if bounds == (0, 0, 0, 0):
            continue
        x1, y1, x2, y2 = bounds
        if y1 >= top_thresh or x2 <= right_thresh:
            continue
        # 与右上角区域有交集即可（不要求完全在内），避免漏掉跨边界的控件
        if y2 <= 0 or x1 >= width:
            continue
        text = node.get("text", "") or ""
        desc = node.get("content-desc", "") or ""
        rid = node.get("resource-id", "") or ""
        hits.append(
            {
                "class": node.get("class", "") or "",
                "text": text,
                "desc": desc,
                "rid": rid,
                "bounds": bounds,
                "clickable": node.get("clickable", "false") == "true",
                "label": text or desc or rid,
            }
        )
    hits.sort(key=lambda h: (-h["bounds"][2], h["bounds"][1], h["bounds"][0]))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="dump 当前页面右上角是否『更多选项』")
    parser.add_argument("--serial", default=None, help="adb 设备 serial（省略则用默认连接）")
    parser.add_argument("--top", type=float, default=0.10, help="右上角区域高度占比（默认 0.10）")
    parser.add_argument("--right", type=float, default=0.80, help="右上角区域右沿占比（默认 0.80）")
    args = parser.parse_args()

    try:
        dev = DeviceController(serial=args.serial)
    except DeviceUnavailableError as exc:
        print(f"[ERROR] 设备连接失败: {exc}")
        return 2

    snap = dev.snapshot()
    width, height = snap.width, snap.height
    print(f"当前应用: {snap.package}/{snap.activity}")
    print(f"屏幕尺寸: {width} x {height}")

    xml = dev.dump_hierarchy()

    # 原始 UI 树落盘
    dump_dir = os.path.join("logs", "dumps")
    os.makedirs(dump_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    xml_path = os.path.join(dump_dir, f"{ts}_topright.xml")
    try:
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"原始 UI 树已落盘: {xml_path}")
    except Exception as exc:
        print(f"[WARN] 落盘失败: {exc}")

    hits = find_top_right_elements(xml, width, height, args.top, args.right)
    print(f"\n=== 右上角区域 (y1<{int(height*args.top)}, x2>{int(width*args.right)}) "
          f"命中 {len(hits)} 个控件 ===")
    more_options = [h for h in hits if h["desc"] == MORE_OPTIONS_DESC]
    for i, h in enumerate(hits[:15]):
        b = h["bounds"]
        print(f"  [{i}] {h['class'].split('.')[-1]:14s} "
              f"bounds=({b[0]},{b[1]},{b[2]},{b[3]}) "
              f"clickable={h['clickable']} "
              f"label='{h['label'][:24]}'")
        if h["rid"]:
            print(f"        rid={h['rid']}")

    print("\n=== 结论 ===")
    if more_options:
        for m in more_options:
            b = m["bounds"]
            inside = (
                b[1] < int(height * args.top) and b[2] > int(width * args.right)
            )
            print(f"  [命中] 找到「更多选项」: class={m['class'].split('.')[-1]} "
                  f"bounds=({b[0]},{b[1]},{b[2]},{b[3]}) "
                  f"完全在右上角区域={'是' if inside else '否（部分跨界）'}")
        # 若最靠近右上角的控件不是更多选项，提示可疑
        topmost = hits[0] if hits else None
        if topmost and topmost["desc"] != MORE_OPTIONS_DESC:
            print(f"  [注意] 最靠近右上角的控件是 '{topmost['label']}' "
                  f"而非「更多选项」——Agent 若按右上角坐标点击可能误触。")
    else:
        print("  [未命中] 右上角区域未找到 content-desc='更多选项' 的控件。")
        if hits:
            print(f"  [提示] 该区域实际控件: {[h['label'] for h in hits[:5]]}")
        else:
            print("  [提示] 该区域无任何可见控件（可能本页无 action bar 溢出菜单）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
