# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live check: calc shapes target correct sheet."""

from plugin.calc.shapes import UpsertShape
from plugin.framework.tool import ToolContext
from plugin.testing_runner import native_test
from tests.testing_utils import with_native_doc

@native_test
@with_native_doc("calc", reuse=False)
def test_calc_shapes_target_active_sheet(ctx, doc):
    doc.getSheets().insertNewByName("Sheet2", 1)
    controller = doc.getCurrentController()
    sheet2 = doc.getSheets().getByIndex(1)
    controller.setActiveSheet(sheet2)

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="calc", services=None, caller="test")
    result = UpsertShape().execute(tctx, action="create", shape_type="rectangle", x=100, y=100, width=5000, height=5000)

    assert result["status"] == "ok"
    assert sheet2.getDrawPage().getCount() == 1
    assert doc.getSheets().getByIndex(0).getDrawPage().getCount() == 0
    assert result["page"] == 1
