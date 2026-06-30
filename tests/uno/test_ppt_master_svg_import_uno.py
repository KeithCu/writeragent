# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.ppt_master.adapter.uno_svg_import import import_svg_files_to_doc, import_svg_to_slide
from plugin.testing_runner import native_test, setup, teardown

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ppt_master_minimal"
ATTENTION_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ppt-master" / "examples" / "ppt169_attention_is_all_you_need"
)

_test_doc = None
_test_ctx = None


@setup
def setup_ppt_master_svg_import_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    _test_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None
    log.info("[PptMasterSvgImportTests] starting")


@teardown
def teardown_ppt_master_svg_import_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_lo_import_single_svg_rect_and_text():
    svg = FIXTURE / "svg_final" / "01_intro.svg"
    result = import_svg_to_slide(_test_ctx, _test_doc, svg, slide_index=0, project_dir=FIXTURE)
    assert result.get("status") == "ok", result
    assert result.get("shapes_copied", 0) >= 2
    page = _test_doc.getDrawPages().getByIndex(0)
    assert page.getCount() >= 2
    text_shape = None
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if "TextShape" in shape.getShapeType():
            text_shape = shape
            break
    assert text_shape is not None
    # setString during clone must not balloon the text frame (would overlap other shapes).
    assert int(text_shape.getSize().Height) <= 800
    # LO Break frames are taller than one line; postprocess should tighten them.
    char_h = float(text_shape.getPropertyValue("CharHeight"))
    assert int(text_shape.getSize().Height) <= int(char_h * 35.28 * 1.2) + 50


@native_test
def test_lo_import_path_svg():
    svg = FIXTURE / "svg_final" / "02_chart.svg"
    result = import_svg_to_slide(_test_ctx, _test_doc, svg, slide_index=1, project_dir=FIXTURE, clear_slide=True)
    assert result.get("status") == "ok", result
    assert result.get("shapes_copied", 0) >= 1


@native_test
def test_lo_import_multi_slide_project():
    svgs = sorted((FIXTURE / "svg_final").glob("*.svg"))
    result = import_svg_files_to_doc(_test_ctx, _test_doc, svgs, project_dir=FIXTURE)
    assert result.get("status") == "ok", result
    assert result.get("slides") == 3
    pages = _test_doc.getDrawPages()
    assert pages.getCount() >= 3
    for i in range(3):
        assert pages.getByIndex(i).getCount() >= 2


@native_test
def test_lo_import_why_it_matters_no_stacked_graphics():
    """Real slide 2: filter strip must avoid four KPI GraphicObjects at one position."""
    svg = ATTENTION_EXAMPLE / "svg_final" / "02_why_it_matters.svg"
    if not svg.is_file():
        log.warning("[PptMasterSvgImportTests] skip why_it_matters — example project not present")
        return
    result = import_svg_to_slide(
        _test_ctx,
        _test_doc,
        svg,
        slide_index=2,
        project_dir=ATTENTION_EXAMPLE,
        clear_slide=True,
    )
    assert result.get("status") == "ok", result
    page = _test_doc.getDrawPages().getByIndex(2)
    graphic_count = 0
    kpi_card_x: list[int] = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        st = shape.getShapeType()
        if "GraphicObjectShape" in st:
            graphic_count += 1
        if "PolyPolygonShape" in st or "ClosedBezierShape" in st:
            pos = shape.getPosition()
            # KPI card rounded rects sit around y=160px → ~3200 hmm after import.
            if 2500 <= int(pos.Y) <= 4500 and int(shape.getSize().Height) >= 4000:
                kpi_card_x.append(int(pos.X))
    assert graphic_count == 0, f"expected no GraphicObjectShape after filter preprocess, got {graphic_count}"
    assert len(kpi_card_x) >= 4, f"expected KPI card paths at distinct columns, got x={kpi_card_x}"
    assert len(set(kpi_card_x)) >= 4, f"KPI cards stacked at same X: {kpi_card_x}"

    # tspan headline fragments on one row must not overlap the next fragment's origin.
    line_fragments: dict[int, list[tuple[int, int, Any]]] = {}
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if "TextShape" not in shape.getShapeType():
            continue
        pos = shape.getPosition()
        size = shape.getSize()
        if 9000 <= int(pos.Y) <= 11000:
            line_fragments.setdefault(int(pos.Y), []).append((int(pos.X), int(size.Width), shape))
    for _y, frags in line_fragments.items():
        frags.sort(key=lambda item: item[0])
        for idx in range(len(frags) - 1):
            x, w, _shape = frags[idx]
            next_x = frags[idx + 1][0]
            assert x + w <= next_x + 200, f"takeaway fragment overlap: {frags}"


@native_test
def test_lo_import_encoder_decoder_no_graphics():
    """Real slide 6: fill-opacity strip + card layout must yield 0 GraphicObjectShape."""
    svg = ATTENTION_EXAMPLE / "svg_final" / "06_encoder_decoder.svg"
    if not svg.is_file():
        log.warning("[PptMasterSvgImportTests] skip encoder_decoder — example project not present")
        return
    result = import_svg_to_slide(
        _test_ctx,
        _test_doc,
        svg,
        slide_index=5,
        project_dir=ATTENTION_EXAMPLE,
        clear_slide=True,
    )
    assert result.get("status") == "ok", result
    page = _test_doc.getDrawPages().getByIndex(5)
    graphic_count = 0
    text_count = 0
    card_outer_x: list[int] = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        st = shape.getShapeType()
        if "GraphicObjectShape" in st:
            graphic_count += 1
        if "TextShape" in st:
            text_count += 1
        if "ClosedBezierShape" in st or "PolyPolygonShape" in st:
            pos = shape.getPosition()
            size = shape.getSize()
            if 2500 <= int(pos.Y) <= 4500 and int(size.Height) >= 8000:
                card_outer_x.append(int(pos.X))
    assert graphic_count == 0, f"expected no GraphicObjectShape, got {graphic_count}"
    assert text_count >= 33, f"expected >=33 TextShape, got {text_count}"
    assert len(set(card_outer_x)) >= 2, f"expected encoder/decoder card columns, got x={card_outer_x}"

    # Decoder sub-layer title row: tspan fragments must not overlap.
    row_fragments: dict[int, list[tuple[int, int]]] = {}
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if "TextShape" not in shape.getShapeType():
            continue
        pos = shape.getPosition()
        size = shape.getSize()
        if 6000 <= int(pos.Y) <= 6500 and int(pos.X) >= 13000:
            row_fragments.setdefault(int(pos.Y), []).append((int(pos.X), int(size.Width)))
    for _y, frags in row_fragments.items():
        frags.sort(key=lambda item: item[0])
        for idx in range(len(frags) - 1):
            x, w = frags[idx]
            next_x = frags[idx + 1][0]
            assert x + w <= next_x + 200, f"decoder title fragment overlap: {frags}"
