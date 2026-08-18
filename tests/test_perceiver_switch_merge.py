from __future__ import annotations

import xml.etree.ElementTree as ET

from device.perceiver import SmartPerceiver, UIElement


def _parse(xml: str) -> list[UIElement]:
    # parse_elements 不依赖 device，可直接用 None 实例化 SmartPerceiver。
    return SmartPerceiver(None).parse_elements(xml)


def test_switch_child_checked_overrides_parent_hardcoded_false():
    """perceiver merge 回归（锁定 device/perceiver.py:1123-1128）。

    Lenovo 设置页 WLAN 行：clickable 父容器 (`LinearLayout`) 上残留硬编码
    `checked="false"`，子 `Switch` 真实态为 `checked="true"`。合并后父必须读
    到 `checked == True`，否则 switch_state 与真实开关状态相反（回到「父永远停
    在 false」的回归）。

    构造规则（对照 `_merge_parent_with_switch_child`）：
    - 父 clickable=true、无 text、无 content-desc → 触发合并分支；
    - 子树中唯一可点击 Switch 子、无其他 clickable 后代 → len(switch_children)==1；
    - 子 Switch 带 `checked="true"` → 覆盖父。
    """
    xml = """
    <hierarchy>
      <node class="android.widget.LinearLayout"
            resource-id="com.zui.settings:id/wlan_item"
            clickable="true" checked="false"
            bounds="[0,0,1080,120]">
        <node class="android.widget.Switch"
              resource-id="com.zui.settings:id/wlan_switch"
              clickable="true" checked="true"
              bounds="[980,30,1060,90]" />
      </node>
    </hierarchy>
    """
    elements = _parse(xml)
    # 父容器（带 wlan_item resource-id）应被合并出 checked=True
    parent = next(
        e for e in elements if e.resource_id == "com.zui.settings:id/wlan_item"
    )
    # 子 Switch 仍保留在列表中供精确点击
    child = next(
        e for e in elements if e.resource_id == "com.zui.settings:id/wlan_switch"
    )
    assert parent.clickable is True
    assert parent.checked is True, (
        f"父容器残留硬编码 checked=false 未被子 Switch 覆盖: parent.checked={parent.checked}"
    )
    assert child.checked is True
    assert parent.has_switch_child is True


def test_switch_child_without_checked_does_not_invent_state():
    """对照：子 Switch 无 checked 信息时，父不应被赋予假的 checked 态。

    防止回归到「子无 checked 时父被错误置位」。
    """
    xml = """
    <hierarchy>
      <node class="android.widget.LinearLayout"
            resource-id="com.zui.settings:id/wlan_item"
            clickable="true" checked="false"
            bounds="[0,0,1080,120]">
        <node class="android.widget.Switch"
              resource-id="com.zui.settings:id/wlan_switch"
              clickable="true"
              bounds="[980,30,1060,90]" />
      </node>
    </hierarchy>
    """
    elements = _parse(xml)
    parent = next(
        e for e in elements if e.resource_id == "com.zui.settings:id/wlan_item"
    )
    # 子无 checked → 合并逻辑不应「臆造」父的 checked 态：父保留自身 XML 的
    # checked="false"（不覆盖、也不被误置为 True）。关键不变量是「子无 checked
    # 时父不会被错误置位成 True」（防止回归到『父永远停在 false』的反向 bug）。
    assert parent.checked is False, (
        f"子无 checked 时父不应被臆造/误置为 True: parent.checked={parent.checked}"
    )
