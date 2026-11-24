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
            # Calc cells may not advertise Paragraph; mirror document.get_string_without_... logic.
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
                # UNO may use float/enum; BOLD is 150, NORMAL 100 in awt.FontWeight.
                is_bold = wv == FontWeight.BOLD or (wv is not None and float(wv) >= 135.0)
            except (TypeError, ValueError, Exception):
                continue
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
        "insert_html",
        {
            "cell": "Z99",
            "html": "Plain <b>BoldBit</b> tail",
        },
    )
    assert res.get("status") == "ok", f"insert_html failed: {res}"
    # Kept Writer stays open after paste. Windows defers this suite until after
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
        "insert_html",
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

    res = _execute_calc_tool(doc, ctx, "insert_html", {"cell": "D5", "html": "<b>new</b>"})
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


@native_test
@with_native_doc("calc")
def test_insert_result_into_calc_undo(ctx, doc):
    """Running a script that inserts structured content into Calc can be reverted with Ctrl+Z."""
    from plugin.scripting.python_runner import insert_result_into_calc

    active_sheet = doc.getCurrentController().getActiveSheet()
    primes_result = {
        "title": "Prime Numbers in Range",
        "primes": [
            {"position": 1000, "prime": 7919},
            {"position": 1001, "prime": 7927},
        ],
    }

    insert_result_into_calc(doc, ctx, primes_result)
    # Kept Writer stays open after paste. Windows defers this suite until after
    # document_research_uno 3/3 (34648929578). Cached leftover_open
    # for later leftover reuse. Do not enum getComponents.
    note_windows_html_paste_leftover()
    assert active_sheet.getCellByPosition(0, 0).getString() == "Prime Numbers in Range"
    assert active_sheet.getCellByPosition(0, 2).getString() == "position"
    assert active_sheet.getCellByPosition(1, 2).getString() == "prime"

    um = doc.getUndoManager()
    assert um is not None
    assert um.isUndoPossible() is True

    um.undo()
    assert active_sheet.getCellByPosition(0, 0).getString() == "", "A1 title should revert after single undo"
    assert active_sheet.getCellByPosition(0, 2).getString() == "", "A3 position header should revert after single undo"
    assert active_sheet.getCellByPosition(1, 2).getString() == "", "B3 prime header should revert after single undo"


@native_test
@with_native_doc("calc")
def test_insert_cell_html_reports_replaced_formula(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    a1 = sheet.getCellByPosition(0, 0)
    a1.setFormula("=1+2")

    res = _execute_calc_tool(doc, ctx, "insert_html", {"cell": "A1", "html": "<b>x</b>"})
    note_windows_html_paste_leftover()
    assert res.get("status") == "ok", res
    assert res["replaced_text"] == "=1+2"
    assert "x" in a1.getString()

    res_empty = _execute_calc_tool(doc, ctx, "insert_html", {"cell": "B1", "html": "<b>y</b>"})
    assert res_empty.get("status") == "ok", res_empty
    assert "replaced_text" not in res_empty


@native_test
@with_native_doc("calc")
def test_repeated_pastes_open_no_new_frames_uno(ctx, doc):
    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    res1 = _execute_calc_tool(doc, ctx, "insert_html", {"cell": "C1", "html": "<b>one</b>"})
    assert res1.get("status") == "ok", res1
    note_windows_html_paste_leftover()
    frames_after_first = desktop.getFrames().getCount()

    for idx, html in enumerate(("<i>two</i>", "<u>three</u>"), start=2):
        res = _execute_calc_tool(doc, ctx, "insert_html", {"cell": f"C{idx}", "html": html})
        assert res.get("status") == "ok", res

    assert desktop.getFrames().getCount() == frames_after_first


@native_test
@with_native_doc("calc")
def test_paste_formatting_does_not_reach_the_next_paste_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    cases = [
        ("<p align='center'>centered</p>", "plain_after_center"),
        ("<b><font color='#ff0000' size='7'>big red</font></b>", "plain_after_bold"),
        ("<ul><li>item</li></ul>", "plain_after_list"),
        ("<p dir='rtl'>rtl</p>", "plain_after_rtl"),
        ("<a href='http://x.y/'>link</a>", "plain_after_link"),
    ]
    for row, (styled_html, plain_text) in enumerate(cases):
        r1 = _execute_calc_tool(doc, ctx, "insert_html", {"cell": f"E{row * 2 + 1}", "html": styled_html})
        assert r1.get("status") == "ok", r1
        note_windows_html_paste_leftover()

        if styled_html == "<ul><li>item</li></ul>":
            r_right = _execute_calc_tool(doc, ctx, "insert_html", {"cell": "F1", "html": "<p align='right'>r</p>"})
            assert r_right.get("status") == "ok", r_right
            right_cell = sheet.getCellByPosition(5, 0)
            right_para = right_cell.getText().createEnumeration().nextElement()
            assert int(right_para.getPropertyValue("ParaAdjust")) == 1

        plain_addr = f"E{row * 2 + 2}"
        r2 = _execute_calc_tool(doc, ctx, "insert_html", {"cell": plain_addr, "html": plain_text})
        assert r2.get("status") == "ok", r2

        cell = sheet.getCellByPosition(4, row * 2 + 1)
        assert cell.getString() == plain_text
        para = cell.getText().createEnumeration().nextElement()
        portion = para.createEnumeration().nextElement()
        assert float(portion.getPropertyValue("CharWeight")) == 100.0
        assert float(portion.getPropertyValue("CharHeight")) == 12.0
        assert int(portion.getPropertyValue("CharColor")) == -1
        assert int(para.getPropertyValue("ParaAdjust")) == 0


@native_test
def test_manage_changes_survives_unclearable_paste_writer_uno(ctx):
    from unittest.mock import patch

    from plugin.calc import rich_html
    from plugin.framework.errors import ToolExecutionError
    from plugin.framework.uno_context import get_service_manager
    from plugin.tests.testing_utils import TestingFactory

    with (
        TestingFactory.native_doc(ctx, "writer", reuse=False) as writer_doc,
        TestingFactory.native_doc(ctx, "calc") as calc_doc,
    ):
        writer_doc.getText().setString("keep")
        with patch.object(rich_html, "clear_writer_body", return_value=False):
            try:
                rich_html.insert_cell_html_rich(calc_doc, ctx, "A1", "<b>x</b>")
            except ToolExecutionError:
                pass
            else:
                raise AssertionError("Expected ToolExecutionError when Writer cannot be emptied")
            finally:
                note_windows_html_paste_leftover()

        dispatcher = get_service_manager(ctx).createInstanceWithContext(
            "com.sun.star.frame.DispatchHelper", ctx
        )
        frame = writer_doc.getCurrentController().getFrame()
        dispatcher.executeDispatch(frame, ".uno:AcceptTrackedChanges", "", 0, ())
        dispatcher.executeDispatch(frame, ".uno:AcceptTrackedChanges", "", 0, ())
        assert writer_doc.getText().getString() == "keep"
