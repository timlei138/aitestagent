"""独立脚本：找 action_add_all_event 所在的 ViewGroup，列出同组全部控件。

不依赖项目现有代码，直接用 uiautomator2 连真机 dump 当前页面 UI 树。

用法:
    python scripts/find_viewgroup.py [--serial <adb-serial>] [--target action_add_all_event]
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


def node_summary(node: ET.Element, idx: str = "") -> str:
    cls = (node.get("class", "") or "").split(".")[-1]
    rid = node.get("resource-id", "") or ""
    text = node.get("text", "") or ""
    desc = node.get("content-desc", "") or ""
    bounds = parse_bounds(node.get("bounds", ""))
    label = text or desc or (rid.split("/")[-1] if rid else "(无文本)")
    short_rid = rid.split("/")[-1] if rid else "-"
    return (f"{idx}{cls:14s} rid='{short_rid}' "
            f"text/desc='{label[:24]}' bounds={bounds}")


def main() -> int:
    parser = argparse.ArgumentParser(description="列出与指定控件同 ViewGroup 的所有元素")
    parser.add_argument("--serial", default=None)
    parser.add_argument("--target", default="action_add_all_event",
                        help="要定位的 resource-id 关键字（默认 action_add_all_event）")
    args = parser.parse_args()

    print(">>> 正在连接设备 ...")
    d = u2.connect(args.serial)
    w, h = d.window_size()
    print(f">>> 已连接: {d}  屏幕尺寸: {w}x{h}\n")

    xml = d.dump_hierarchy()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        print(f"[ERROR] UI 树解析失败: {exc}")
        return 2

    # 1) 找到 resource-id 含 target 的节点
    target_nodes = [n for n in root.iter()
                    if (n.get("resource-id") or "").endswith("/" + args.target)
                    or (n.get("resource-id") or "").endswith(args.target)
                    or args.target in (n.get("resource-id") or "")]

    if not target_nodes:
        print(f"[提示] 当前页面未找到 resource-id 含 '{args.target}' 的控件。")
        print("       可能页面已切换 / 元素未展示。已 dump 的节点总数: "
              f"{sum(1 for _ in root.iter())}")
        return 0

    print(f"=== 找到 {len(target_nodes)} 个匹配 '{args.target}' 的控件 ===\n")

    for ti, tnode in enumerate(target_nodes):
        trid = tnode.get("resource-id")
        print(f"--- 目标[{ti}] resource-id='{trid}' ---")
        print(f"    {node_summary(tnode)}\n")

        # 2) 回溯父 ViewGroup（含自身）
        #    ElementTree 没有父指针，建索引
        parent_of = {}
        for node in root.iter():
            for child in node:
                parent_of[child] = node

        # 目标节点本身可能就是 ViewGroup；找它的直接父
        parent = parent_of.get(tnode)
        # 同时收集「祖先路径」，展示结构
        chain = []
        cur = tnode
        while cur is not None:
            chain.append(cur)
            cur = parent_of.get(cur)
        chain.reverse()  # 从根到目标

        print("    祖先路径 (根 -> 目标):")
        for depth, anc in enumerate(chain):
            pad = "      " + "  " * depth + ("└─ " if depth else "")
            cls = (anc.get("class", "") or "").split(".")[-1]
            rid = anc.get("resource-id", "") or ""
            short = rid.split("/")[-1] if rid else "(no-id)"
            print(f"{pad}{cls}  rid='{short}'")

        # 容器 = 目标的父 ViewGroup（若目标是叶子，则父即容器）
        container = parent if parent is not None else tnode
        ccls = (container.get("class", "") or "").split(".")[-1]
        crid = (container.get("resource-id", "") or "").split("/")[-1] or "(no-id)"
        print(f"\n    目标所在的 ViewGroup 容器: {ccls}  rid='{crid}'")

        # 3) 列出容器内所有直系子节点
        children = list(container)
        print(f"    该 ViewGroup 共 {len(children)} 个直系子控件:\n")
        for i, ch in enumerate(children):
            mark = "  <<< 目标本身" if ch is tnode else ""
            print(f"      [{i}] {node_summary(ch)}{mark}")

        # 4) 若容器自己还有文本/描述（有时按钮直接是容器子节点）
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
