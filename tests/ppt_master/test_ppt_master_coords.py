# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.contrib.ppt_master.coords import parse_viewbox, px_to_hmm, slide_dims_for_viewbox
from plugin.contrib.ppt_master.shape_ops import ShapeOp, shape_op_from_dict, shape_op_to_dict, slide_plan_from_dict, slide_plan_to_dict, SlideBuildPlan
from plugin.contrib.ppt_master.svg_convert import svg_to_slide_plan


def test_parse_viewbox_ppt169():
    _min_x, _min_y, w, h = parse_viewbox("0 0 1280 720")
    assert w == 1280.0
    assert h == 720.0


def test_px_to_hmm_center():
    hmm = px_to_hmm(640, slide_hmm=25400, viewbox_px=1280)
    assert hmm == 12700


def test_slide_dims_for_viewbox():
    w, h = slide_dims_for_viewbox(1280, 720, width_hmm=25400)
    assert w == 25400
    assert h == 14288


def test_shape_op_roundtrip():
    op = ShapeOp(kind="rect", x_hmm=100, y_hmm=200, w_hmm=300, h_hmm=400, fill_color=0xFF0000)
    restored = shape_op_from_dict(shape_op_to_dict(op))
    assert restored.kind == "rect"
    assert restored.fill_color == 0xFF0000


def test_slide_plan_roundtrip():
    plan = SlideBuildPlan(
        slide_index=1,
        viewbox_width_px=1280,
        viewbox_height_px=720,
        slide_width_hmm=25400,
        slide_height_hmm=14288,
        shapes=[ShapeOp(kind="text", x_hmm=1, y_hmm=2, w_hmm=3, h_hmm=4, text="Hi")],
    )
    back = slide_plan_from_dict(slide_plan_to_dict(plan))
    assert back.slide_index == 1
    assert back.shapes[0].text == "Hi"


def test_svg_to_slide_plan_rect(tmp_path: Path):
    svg = tmp_path / "slide.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect x="100" y="50" width="200" height="100" fill="#336699"/>'
        '<text x="120" y="120" font-size="24">Hello</text>'
        "</svg>",
        encoding="utf-8",
    )
    plan = svg_to_slide_plan(svg)
    assert len(plan.shapes) >= 2
    kinds = {s.kind for s in plan.shapes}
    assert "rect" in kinds
    assert "text" in kinds
