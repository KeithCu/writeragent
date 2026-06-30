# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_to_doc
from plugin.testing_runner import native_test, setup, teardown

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ppt_master_minimal"
MINIMAL_PPTX = FIXTURE / "minimal.pptx"

_test_doc = None
_test_ctx = None


@setup
def setup_ppt_master_pptx_import_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    _test_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None
    log.info("[PptMasterPptxImportTests] starting")


@teardown
def teardown_ppt_master_pptx_import_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        try:
            _test_doc.close(True)
        except Exception as exc:
            log.debug("teardown close doc: %s", exc)
    _test_doc = None
    _test_ctx = None


@native_test
def test_lo_import_minimal_pptx_multi_slide():
    if not MINIMAL_PPTX.is_file():
        log.warning("[PptMasterPptxImportTests] skip — minimal.pptx fixture missing")
        return
    result = import_pptx_to_doc(_test_ctx, _test_doc, MINIMAL_PPTX, clear_existing=True)
    assert result.get("status") == "ok", result
    assert result.get("slides") == 3
    assert result.get("route") == "pptx_to_odp"
    pages = _test_doc.getDrawPages()
    assert pages.getCount() >= 3
    for i in range(3):
        page = pages.getByIndex(i)
        assert page.getCount() >= 1
    page0 = pages.getByIndex(0)
    has_text = any("TextShape" in page0.getByIndex(j).getShapeType() for j in range(page0.getCount()))
    assert has_text or page0.getCount() >= 2
