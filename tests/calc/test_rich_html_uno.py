# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    execute_calc_tool as _execute_calc_tool,
    note_windows_html_paste_leftover,
    with_native_doc,
)


def _cell_has_bold_text(cell, needle: str) -> bool:
    """Return True if any text portion in *cell* is bold and contains *needle*."""
    from com.sun.star.awt import FontWeight

    top = cell.getText().createEnumeration()
    while enum_has_more(top):
        block = top.nextElement()
        try:
            inner = block.createEnumeration()
        except Exception:
            portions = [block]
        else:
            portions = []
            while enum_has_more(inner):
                portions.append(inner.nextElement())
            if not portions:
                portions = [block]
        for portion in portions:
            try:
                wv = portion.getPropertyValue("CharWeight")
                ptxt = portion.getString()
            except Exception:
                continue
            is_bold = wv == FontWeight.BOLD or (wv is not None and float(wv) >= 135.0)
            if is_bold and needle in ptxt:
                return True
    return False


@native_test
@with_native_doc("calc")
def test_insert_cell_html(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(
        doc,
        ctx,
        "insert_cell_html",
        {
            "cell": "Z99",
            "html": "Plain <b>BoldBit</b> tail",
        },
    )
    assert res.get("status") == "ok", f"insert_cell_html failed: {res}"
    # Close skipped after paste. Windows defers this suite until after
    # document_research_uno 3/3 (34648929578). Cached leftover_open
    # for later leftover reuse. Do not enum getComponents.
    note_windows_html_paste_leftover()

    cell = active_sheet.getCellByPosition(25, 98)
    s = cell.getString()
    assert "BoldBit" in s and "Plain" in s and "tail" in s, f"unexpected cell string: {s!r}"
    assert _cell_has_bold_text(cell, "BoldBit"), "expected a bold text portion containing BoldBit"


@native_test
@with_native_doc("calc")
def test_insert_cell_html_sheet_prefix_without_switching(ctx, doc):
    sheets = doc.getSheets()
    sheets.insertNewByName("Sheet2", sheets.getCount())
    active = doc.getCurrentController().getActiveSheet()
    assert active.getName() != "Sheet2"
    res = _execute_calc_tool(
        doc,
        ctx,
        "insert_cell_html",
        {"cell": "Sheet2.A1", "html": "<b>remote</b>"},
    )
    assert res.get("status") == "ok", res
    cell = sheets.getByName("Sheet2").getCellByPosition(0, 0)
    assert "remote" in cell.getString()


@native_test
@with_native_doc("calc")
def test_insert_cell_html_is_one_undo_step_uno(ctx, doc):
    from plugin.doc.compound_undo import undo_titles

    sheet = doc.getSheets().getByIndex(0)
    d5 = sheet.getCellByPosition(3, 4)
    d5.setString("old")

    before = undo_titles(doc)
    assert before is not None

    res = _execute_calc_tool(doc, ctx, "insert_cell_html", {"cell": "D5", "html": "<b>new</b>"})
    assert res.get("status") == "ok", res
    assert "new" in d5.getString()

    after = undo_titles(doc)
    assert after is not None
    delta = after[: len(after) - len(before)]
    assert delta == ("WriterAgent: Insert HTML",)

    doc.getUndoManager().undo()
    assert d5.getString() == "old"


@native_test
@with_native_doc("calc")
def test_failed_paste_restores_cell_text_uno(ctx, doc):
    from plugin.calc.rich_html import insert_cell_html_rich
    from plugin.doc.compound_undo import undo_titles
    from plugin.framework.errors import ToolExecutionError

    sheet = doc.getSheets().getByIndex(0)
    d6 = sheet.getCellByPosition(3, 5)
    d6.setString("old")

    before = undo_titles(doc)
    assert before is not None

    class _FailPasteCtrl:
        def __init__(self, inner):
            self._inner = inner

        def insertTransferable(self, _xfer):
            raise RuntimeError("boom")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _FailPasteDoc:
        def __init__(self, inner):
            self._inner = inner

        def getCurrentController(self):
            return _FailPasteCtrl(self._inner.getCurrentController())

        def __getattr__(self, name):
            return getattr(self._inner, name)

    try:
        insert_cell_html_rich(_FailPasteDoc(doc), ctx, "D6", "<b>new</b>")
    except ToolExecutionError:
        pass
    else:
        raise AssertionError("Expected ToolExecutionError on failed paste")

    assert d6.getString() == "old"
    assert undo_titles(doc) == before
