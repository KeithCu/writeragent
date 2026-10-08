# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for image_mark's pure helpers (no LibreOffice required). Baking itself was checked live."""
from types import SimpleNamespace

import pytest

from plugin.writer.images import image_mark


def _graphic(px=(2000, 1000), hmm=(52917, 26458)):
    props = {"SizePixel": SimpleNamespace(Width=px[0], Height=px[1]), "Size100thMM": SimpleNamespace(Width=hmm[0], Height=hmm[1])}
    return SimpleNamespace(getPropertyValue=props.__getitem__)


def test_pixel_size_uses_pixels():
    assert image_mark.pixel_size(_graphic()) == (2000, 1000)


def test_pixel_size_vector_picture_gets_96_dpi_frame():
    # SVG/WMF report no pixels: 2540 (1/100 mm) = 1 inch = 96 px.
    assert image_mark.pixel_size(_graphic(px=(0, 0), hmm=(2540, 5080))) == (96, 192)


def test_to_pixel_box_px_and_percent():
    assert image_mark.to_pixel_box([10, 20, 300, 40], "px", 2000, 1000) == (10, 20, 300, 40)
    assert image_mark.to_pixel_box([10, 20, 50, 5], "percent", 2000, 1000) == (200, 200, 1000, 50)


def test_to_pixel_box_clips_to_picture():
    assert image_mark.to_pixel_box([-50, 900, 300, 400], "px", 2000, 1000) == (0, 900, 250, 100)


@pytest.mark.parametrize("box", [[1, 2, 3], "0,0,10,10", [0, 0, True, 5], None])
def test_to_pixel_box_rejects_malformed(box):
    with pytest.raises(ValueError, match="x, y, width, height"):
        image_mark.to_pixel_box(box, "px", 2000, 1000)


@pytest.mark.parametrize(
    "match, value, value_2, value_3, value_4",
    [
        pytest.param("outside the picture", 2100, 50, 50, "px", id="test_to_pixel_box_rejects_box_outside_picture"),
        pytest.param("units", 0, 10, 10, "mm", id="test_to_pixel_box_rejects_unknown_units"),
    ],
)
def test_to_pixel_box_rejects_box_outside_picture(match, value, value_2, value_3, value_4):
    with pytest.raises(ValueError, match=match):
        image_mark.to_pixel_box([value, 0, value_2, value_3], value_4, 2000, 1000)

def test_parse_marks_defaults_and_colors():
    marks = image_mark.parse_marks(
        [{"box": [10, 10, 100, 20]}, {"box": [10, 40, 100, 20], "style": "box", "color": "#0000ff"}],
        "px", 2000, 1000, (0, 0, 2000, 1000),
    )
    assert marks == [((10, 10, 100, 20), "highlight", 0xFFEB3B), ((10, 40, 100, 20), "box", 0x0000FF)]


def test_parse_marks_rejects_mark_outside_kept_region():
    # The agent would never see this mark: say so instead of dropping it.
    with pytest.raises(ValueError, match="outside the kept region"):
        image_mark.parse_marks([{"box": [10, 10, 50, 20]}], "px", 2000, 1000, (500, 500, 200, 100))


@pytest.mark.parametrize("item,match", [({"box": [0, 0, 5, 5], "style": "circle"}, "style"), ({"box": [0, 0, 5, 5], "color": "notacolor"}, "color"), ([0, 0, 5, 5], "object")])
def test_parse_marks_rejects_bad_items(item, match):
    with pytest.raises(ValueError, match=match):
        image_mark.parse_marks([item], "px", 2000, 1000, (0, 0, 2000, 1000))


def test_visible_region_converts_crop_to_pixels():
    # 2000 px over 52917 (1/100 mm): a 1/100 mm crop of 2646 is 100 px.
    crop = SimpleNamespace(Left=2646, Top=0, Right=5292, Bottom=-500)
    assert image_mark.visible_region(crop, None, _graphic()) == (100, 0, 1700, 1000)


def test_visible_region_measures_crop_by_writer_actual_size():
    # A JPEG without a physical size reports Size100thMM 0; Writer measured it at the screen's
    # DPI (27.6 per px on the Mac checked), so the crop must be read against ActualSize.
    jpeg = _graphic(px=(795, 1124), hmm=(0, 0))
    actual = SimpleNamespace(Width=21948, Height=31032)
    crop = SimpleNamespace(Left=1500, Top=1000, Right=6000, Bottom=22000)
    assert image_mark.visible_region(crop, actual, jpeg) == (54, 36, 524, 291)


def test_visible_region_unmeasurable_crop_asks_for_crop_box():
    jpeg = _graphic(px=(795, 1124), hmm=(0, 0))
    with pytest.raises(ValueError, match="crop_box"):
        image_mark.visible_region(SimpleNamespace(Left=1500, Top=0, Right=0, Bottom=0), None, jpeg)


def test_visible_region_without_crop_is_whole_picture():
    assert image_mark.visible_region(None, None, _graphic()) == (0, 0, 2000, 1000)
    zero = SimpleNamespace(Left=0, Top=0, Right=0, Bottom=-10)
    assert image_mark.visible_region(zero, None, _graphic(hmm=(0, 0))) == (0, 0, 2000, 1000)


def test_stroke_px_prints_about_the_same_width():
    # 1600 px shown 160 mm wide = 10 px/mm -> 0.6 mm = 6 px; never thinner than 2 px.
    assert image_mark.stroke_px(1600, 160) == 6
    assert image_mark.stroke_px(100, 160) == 2
    assert image_mark.stroke_px(100, 0) == 2


def test_mark_rectangles_move_to_region_origin():
    region = (100, 50, 800, 400)
    marks = [((150, 70, 200, 30), "highlight", 1), ((150, 70, 200, 30), "box", 2), ((150, 70, 200, 30), "underline", 3)]
    assert image_mark.mark_rectangles(marks, region, 4) == [
        ((50, 20, 200, 30), "highlight", 1),
        ((46, 16, 208, 38), "box", 2),  # stroke drawn outside the marked text
        ((50, 50, 200, 4), "underline", 3),  # right below the box
    ]


def _bmp(pixels, bits=24, compression=0, top_down=False):
    """Tiny BMP: *pixels* is rows (top first) of (r, g, b)."""
    height, width, step = len(pixels), len(pixels[0]), bits // 8
    stride = (width * step + 3) & ~3
    rows = pixels if top_down else pixels[::-1]
    body = b"".join(bytes(sum(([b, g, r] + [255] * (step - 3) for r, g, b in row), [])).ljust(stride, b"\0") for row in rows)
    header = b"BM" + (54 + len(body)).to_bytes(4, "little") + bytes(4) + (54).to_bytes(4, "little")
    info = (40).to_bytes(4, "little") + width.to_bytes(4, "little", signed=True) + (-height if top_down else height).to_bytes(4, "little", signed=True)
    info += (1).to_bytes(2, "little") + bits.to_bytes(2, "little") + compression.to_bytes(4, "little") + bytes(20)
    return bytearray(header + info + body)


def _pixel(data, x, y, bits=24, top_down=False):
    width = int.from_bytes(data[18:22], "little", signed=True)
    rows = abs(int.from_bytes(data[22:26], "little", signed=True))
    step = bits // 8
    stride = (width * step + 3) & ~3
    start = 54 + (y if top_down else rows - 1 - y) * stride + x * step
    b, g, r = data[start:start + 3]
    return r, g, b


WHITE, BLACK = (255, 255, 255), (0, 0, 0)


@pytest.mark.parametrize("bits,compression,top_down", [(24, 0, False), (24, 0, True), (32, 3, False)])
def test_multiply_bmp_turns_paper_yellow_and_keeps_ink_black(bits, compression, top_down):
    data = _bmp([[WHITE, BLACK, WHITE], [WHITE, WHITE, WHITE]], bits, compression, top_down)
    image_mark.multiply_bmp(data, [((0, 0, 2, 1), "highlight", 0xFFEB3B)])
    assert _pixel(data, 0, 0, bits, top_down) == (0xFF, 0xEB, 0x3B)  # paper -> highlighter yellow
    assert _pixel(data, 1, 0, bits, top_down) == BLACK  # ink stays black
    assert _pixel(data, 2, 0, bits, top_down) == WHITE  # outside the box
    assert _pixel(data, 0, 1, bits, top_down) == WHITE  # row below untouched


def test_multiply_bmp_clips_to_picture():
    data = _bmp([[WHITE, WHITE]])
    image_mark.multiply_bmp(data, [((-5, -5, 6, 10), "highlight", 0x00FF00)])
    assert _pixel(data, 0, 0) == (0, 255, 0) and _pixel(data, 1, 0) == WHITE


def test_multiply_bmp_rejects_other_layouts():
    data = _bmp([[WHITE]], bits=24, compression=1)
    with pytest.raises(RuntimeError, match="bitmap layout"):
        image_mark.multiply_bmp(data, [((0, 0, 1, 1), "highlight", 0xFFEB3B)])


def test_multiply_bmp_rejects_missing_bitmap():
    with pytest.raises(RuntimeError, match="did not write a bitmap"):
        image_mark.multiply_bmp(bytearray(), [((0, 0, 1, 1), "highlight", 0xFFEB3B)])
