from __future__ import annotations

import xml.etree.ElementTree as ET

from device.perceiver import SmartPerceiver, UIElement


# ── UIElement.label 单元素口径 ──


def test_label_uses_text_when_present():
    el = UIElement(text="回到今天")
    assert el.label == "回到今天"


def test_label_uses_content_desc_when_no_text():
    el = UIElement(content_desc="更多选项")
    assert el.label == "更多选项"


def test_label_uses_associated_when_only_associated():
    el = UIElement(associated_label="WLAN 开关")
    assert el.label == "WLAN 开关"


def test_label_does_not_fallback_to_resource_id():
    # §核心契约：没有 text/desc/associated 时，绝不回退到 resource-id
    # （那是开发者命名，不是用户可见语义，会误导前端/LLM）。
    el = UIElement(resource_id="com.zui.calendar:id/action_add_all_event")
    assert el.label == ""


def test_label_suppress_flag_forces_empty():
    # 重复 label 被判定为不可信时，suppress_label 强制返回空（不给不存在的 content-desc）。
    el = UIElement(content_desc="更多选项", suppress_label=True)
    assert el.label == ""


# ── parse_elements 重复 label 抑制 ──


def _parse(xml: str) -> list[UIElement]:
    # parse_elements 不依赖 device，可直接用 None 实例化 SmartPerceiver。
    return SmartPerceiver(None).parse_elements(xml)


# ── rag_hint 经验推断富集（不污染 label） ──


class _FakeKB:
    """迷你知识库：按 rid 叶子名返回推断语义。"""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def query_element_semantic(self, app_package: str, rid_tail: str) -> str:
        return self._mapping.get(rid_tail, "")


_RAG_XML = """
<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][2560,1600]">
    <node class="android.widget.ImageView"
          resource-id="com.zui.calendar:id/overflow"
          content-desc="更多选项" clickable="true" bounds="[2371,131][2443,203]"/>
    <node class="android.widget.ImageView"
          resource-id="com.zui.calendar:id/action_add_all_event"
          clickable="true" bounds="[2119,131][2245,203]"/>
    <node class="android.widget.ImageView"
          resource-id="com.zui.calendar:id/action_back_today"
          clickable="true" bounds="[2245,131][2371,203]"/>
  </node>
</hierarchy>
"""


def test_rag_hint_enriches_unlabeled_icons_without_touching_label():
    # 无屏上标签但有 rid 的图标 → 按 rid 查知识库填 rag_hint；
    # 关键：label 仍为空（不污染），有真实 label 的元素 rag_hint 为空。
    perceiver = SmartPerceiver(None)
    perceiver.attach_knowledge(
        _FakeKB(
            {
                "action_add_all_event": "添加事件",
                "action_back_today": "回到今天",
            }
        ),
        lambda: "com.zui.calendar",
    )
    elements = perceiver.parse_elements(_RAG_XML)
    by_rid = {e.resource_id.split("/")[-1]: e for e in elements if e.resource_id}
    perceiver._enrich_rag_hints(elements)

    assert by_rid["action_add_all_event"].label == ""
    assert by_rid["action_add_all_event"].rag_hint == "添加事件"
    assert by_rid["action_back_today"].label == ""
    assert by_rid["action_back_today"].rag_hint == "回到今天"
    # 有真实 content-desc 的溢出菜单：label 保留，rag_hint 不覆盖
    assert by_rid["overflow"].label == "更多选项"
    assert by_rid["overflow"].rag_hint == ""


def test_rag_hint_empty_when_kb_has_no_match():
    # 知识库无该 rid 经验 → rag_hint 为空，行为与之前一致（仍靠 index 点）。
    perceiver = SmartPerceiver(None)
    perceiver.attach_knowledge(_FakeKB({}), lambda: "com.zui.calendar")
    elements = perceiver.parse_elements(_RAG_XML)
    by_rid = {e.resource_id.split("/")[-1]: e for e in elements if e.resource_id}
    perceiver._enrich_rag_hints(elements)
    assert by_rid["action_add_all_event"].rag_hint == ""
    assert by_rid["action_back_today"].rag_hint == ""


def test_rag_hint_noop_without_kb():
    # 未挂载知识库时完全无副作用。
    perceiver = SmartPerceiver(None)
    elements = perceiver.parse_elements(_RAG_XML)
    by_rid = {e.resource_id.split("/")[-1]: e for e in elements if e.resource_id}
    perceiver._enrich_rag_hints(elements)
    assert by_rid["action_add_all_event"].rag_hint == ""


def test_duplicate_content_desc_all_suppressed():
    # App 把同一个 content-desc 串到 3 个兄弟图标上（如 action bar 多个
    # 按钮都标成「更多选项」）→ 该 label 不可信 → 全部抑制（不给），
    # 而不是合成 (action_add_all_event) 这类控件本没有的 content-desc。
    xml = """
    <hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][2560,1600]">
        <node class="android.widget.ImageView"
              resource-id="com.zui.calendar:id/action_add_all_event"
              content-desc="更多选项" clickable="true" bounds="[2119,131][2245,203]"/>
        <node class="android.widget.ImageView"
              resource-id="com.zui.calendar:id/action_back_today"
              content-desc="更多选项" clickable="true" bounds="[2245,131][2371,203]"/>
        <node class="android.widget.ImageView"
              resource-id="com.zui.calendar:id/overflow"
              content-desc="更多选项" clickable="true" bounds="[2371,131][2443,203]"/>
      </node>
    </hierarchy>
    """
    elements = _parse(xml)
    # 外层 FrameLayout 这类结构性容器也会被 parse 返回，这里只断言真实控件
    controls = [e for e in elements if e.resource_id]
    assert len(controls) == 3
    assert all(e.label == "" for e in controls)


def test_unique_content_desc_is_kept_with_empty_desc_siblings():
    # 正常页：溢出菜单是唯一「更多选项」，其余两个图标本就没 content-desc
    # 也不该从兄弟溢出菜单「借」来 content-desc（关联标签只认文本标签）。
    # → 真·溢出菜单保留「更多选项」，另两个不给（空）。
    xml = """
    <hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][2560,1600]">
        <node class="android.widget.ImageView"
              resource-id="com.zui.calendar:id/overflow"
              content-desc="更多选项" clickable="true" bounds="[2371,131][2443,203]"/>
        <node class="android.widget.ImageView"
              resource-id="com.zui.calendar:id/action_add_all_event"
              clickable="true" bounds="[2119,131][2245,203]"/>
        <node class="android.widget.ImageView"
              resource-id="com.zui.calendar:id/action_back_today"
              clickable="true" bounds="[2245,131][2371,203]"/>
      </node>
    </hierarchy>
    """
    elements = _parse(xml)
    labels = {e.resource_id.split("/")[-1]: e.label for e in elements if e.resource_id}
    assert labels["overflow"] == "更多选项"
    assert labels["action_add_all_event"] == ""
    assert labels["action_back_today"] == ""


def test_duplicate_text_suppressed():
    # 两个都标「确定」的文本按钮 → 同名不可信 → 全部抑制。
    xml = """
    <hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1200,800]">
        <node class="android.widget.TextView" text="确定" clickable="true"
              bounds="[0,0][100,50]"/>
        <node class="android.widget.TextView" text="确定" clickable="true"
              bounds="[0,60][100,110]"/>
      </node>
    </hierarchy>
    """
    elements = _parse(xml)
    controls = [e for e in elements if not e.is_container]
    assert len(controls) == 2
    assert all(e.label == "" for e in controls)


def test_distinct_labels_untouched():
    # 不同 label 之间互不影响；结构性容器借来的文本不参与重复判定。
    xml = """
    <hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1200,800]">
        <node class="android.widget.TextView" text="保存" clickable="true"
              bounds="[0,0][100,50]"/>
        <node class="android.widget.TextView" text="取消" clickable="true"
              bounds="[0,60][100,110]"/>
      </node>
    </hierarchy>
    """
    elements = _parse(xml)
    labels = {e.label for e in elements if not e.is_container}
    assert labels == {"保存", "取消"}
