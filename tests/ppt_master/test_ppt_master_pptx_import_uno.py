# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.framework.logging import log
from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_to_doc
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ppt_master_minimal"
MINIMAL_PPTX = FIXTURE / "minimal.pptx"


@native_test
@with_native_doc("impress")
def test_lo_import_minimal_pptx_multi_slide(ctx, doc):
    if not MINIMAL_PPTX.is_file():
        log.warning("[PptMasterPptxImportTests] skip — minimal.pptx fixture missing")
        return
    result = import_pptx_to_doc(ctx, doc, MINIMAL_PPTX, clear_existing=True)
    assert result.get("status") == "ok", result
    assert result.get("slides") == 3
    assert result.get("route") == "pptx_to_odp"
    pages = doc.getDrawPages()
    assert pages.getCount() >= 3
    for i in range(3):
        page = pages.getByIndex(i)
        assert page.getCount() >= 1
    page0 = pages.getByIndex(0)
    has_text = any("TextShape" in page0.getByIndex(j).getShapeType() for j in range(page0.getCount()))
    assert has_text or page0.getCount() >= 2
