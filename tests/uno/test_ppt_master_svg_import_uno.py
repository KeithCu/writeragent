# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.ppt_master.adapter.uno_svg_import import import_svg_files_to_doc, import_svg_to_slide
from plugin.testing_runner import native_test, setup, teardown

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ppt_master_minimal"

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
