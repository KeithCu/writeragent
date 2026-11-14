# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Calc cell navigation."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_navigate_to_cell(ctx, doc):
    from plugin.calc.navigation import navigate_to_cell

    del ctx
    active_sheet = doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(4, 4).setString("target")
    ok = navigate_to_cell(doc, "E5")
    assert ok, "navigate_to_cell returned False"
    sel = doc.getCurrentController().getSelection()
    addr = sel.getCellAddress()
    assert addr.Column == 4 and addr.Row == 4, f"Expected E5 selected, got col={addr.Column} row={addr.Row}"
