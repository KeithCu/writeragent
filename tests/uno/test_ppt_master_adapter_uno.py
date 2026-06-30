# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from plugin.contrib.ppt_master.svg_convert import svg_to_slide_plan
from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.ppt_master.adapter.uno_apply import apply_slide_plan
from plugin.testing_runner import native_test, setup, teardown


_test_doc = None
_test_ctx = None


@setup
def setup_ppt_master_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    _test_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None
    log.info("[PptMasterTests] starting")


@teardown
def teardown_ppt_master_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_export_svg_rect_to_impress(ctx):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        svg = Path(td) / "slide.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<rect x="100" y="50" width="400" height="200" fill="#2244aa"/>'
            '<text x="120" y="200" font-size="32">PPT-Master</text>'
            "</svg>",
            encoding="utf-8",
        )
        plan = svg_to_slide_plan(svg, slide_index=0)
        result = apply_slide_plan(_test_doc, plan)
        assert result.get("status") == "ok"
        assert result.get("shapes_applied", 0) >= 1
        pages = _test_doc.getDrawPages()
        assert pages.getCount() >= 1
        page = pages.getByIndex(0)
        assert page.getCount() >= 1
