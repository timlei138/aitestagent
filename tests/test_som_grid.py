"""SoM（Set-of-Mark）网格定位的单元测试。

覆盖 vision_tap 中最易出错、却尚无覆盖的两段纯函数逻辑：
  - _parse_cell_response：模型返回的各种格子引用格式 -> (col_letter, row)
  - 格子中心 -> 设备坐标的映射数学（与 vision_tap 内联实现保持一致口径）

不依赖真机 / vision 模型，可在普通 pytest 下运行。
"""

from __future__ import annotations

import math

from tools import perceive_tools as pt


# ── _parse_cell_response：格式宽容性 ──


def test_parse_col_row_separate():
    assert pt._parse_cell_response({"col": "G", "row": 7}, 8, 12) == ("G", 7)


def test_parse_lowercase_col():
    assert pt._parse_cell_response({"col": "g", "row": "7"}, 8, 12) == ("G", 7)


def test_parse_merged_cell_field():
    assert pt._parse_cell_response({"cell": "G7"}, 8, 12) == ("G", 7)


def test_parse_col_field_carries_merged_value():
    # 模型有时把 "G7" 整个塞进 col 字段、不给 row
    assert pt._parse_cell_response({"col": "G7"}, 8, 12) == ("G", 7)


def test_parse_from_reason_text():
    data = {"reason": "目标位于格子 G7 附近"}
    assert pt._parse_cell_response(data, 8, 12) == ("G", 7)


def test_parse_invalid_when_no_cell_info():
    assert pt._parse_cell_response({"reason": "没看清"}, 8, 12) is None


def test_parse_rejects_out_of_range_col():
    # 只有 8 列（A-H），列 I 越界
    assert pt._parse_cell_response({"col": "I", "row": 1}, 8, 12) is None


def test_parse_rejects_out_of_range_row():
    # 只有 12 行（1-12），行 13 越界
    assert pt._parse_cell_response({"col": "A", "row": 13}, 8, 12) is None


def test_parse_rejects_multiletter_col():
    # 多字母 col 不应被当作单字母解析
    assert pt._parse_cell_response({"col": "AA", "row": 1}, 8, 12) is None


# ── 格子中心 -> 设备坐标映射 ──
#
# vision_tap 内联逻辑（裁剪/全屏模式共用）：
#   x_img = (col_idx + 0.5) * cell_w   # 原图坐标系
#   y_img = (row - 0.5) * cell_h
#   device = offset + img_coord * scale
# 这里复刻同一公式并验证其不变量。


def _cell_center_device(
    col_letter: str,
    row: int,
    img_w: int,
    img_h: int,
    cols: int,
    rows: int,
    offset_x: int = 0,
    offset_y: int = 0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
):
    cell_w = img_w / cols
    cell_h = img_h / rows
    col_idx = ord(col_letter) - ord("A")
    x_img = (col_idx + 0.5) * cell_w
    y_img = (row - 0.5) * cell_h
    x = offset_x + x_img * scale_x
    y = offset_y + y_img * scale_y
    return x, y


def test_cell_center_of_first_cell():
    # A1 的中心应落在原图第一象限中心
    x, y = _cell_center_device("A", 1, 480, 720, 8, 12)
    assert math.isclose(x, 480 / 8 / 2, rel_tol=1e-6)
    assert math.isclose(y, 720 / 12 / 2, rel_tol=1e-6)


def test_cell_center_of_last_cell():
    # H12 中心应贴近原图右下角内侧
    x, y = _cell_center_device("H", 12, 480, 720, 8, 12)
    cell_w, cell_h = 480 / 8, 720 / 12
    assert math.isclose(x, 480 - cell_w / 2, rel_tol=1e-6)
    assert math.isclose(y, 720 - cell_h / 2, rel_tol=1e-6)


def test_cell_center_with_crop_offset():
    # 裁剪模式：offset 为裁剪框在原图的左上角，scale=1
    x, y = _cell_center_device(
        "C", 3, 200, 300, 8, 12, offset_x=50, offset_y=40
    )
    assert math.isclose(x, 50 + (2 + 0.5) * (200 / 8), rel_tol=1e-6)
    assert math.isclose(y, 40 + (3 - 0.5) * (300 / 12), rel_tol=1e-6)


def test_cell_center_with_fullscreen_scale():
    # 全屏模式：scale = device / snapshot 原图尺寸
    snap_w, snap_h = 360, 640
    dev_w, dev_h = 1080, 1920
    sx, sy = dev_w / snap_w, dev_h / snap_h
    x, y = _cell_center_device(
        "D", 6, snap_w, snap_h, 8, 12, scale_x=sx, scale_y=sy
    )
    # D6 中心映射到设备坐标
    assert math.isclose(x, (3 + 0.5) * (snap_w / 8) * sx, rel_tol=1e-6)
    assert math.isclose(y, (6 - 0.5) * (snap_h / 12) * sy, rel_tol=1e-6)
    # 设备坐标应正好落在设备尺寸内
    assert 0 <= x <= dev_w
    assert 0 <= y <= dev_h


def test_grid_dimensions_bounded():
    # _draw_som_grid 的自适应网格约束：cols∈[4,8], rows∈[4,12]
    from PIL import Image

    for w, h in [(1080, 1920), (480, 800), (720, 720)]:
        _, _, cols, rows, *_ = pt._draw_som_grid(Image.new("RGB", (w, h)))
        assert 4 <= cols <= 8
        assert 4 <= rows <= 12
