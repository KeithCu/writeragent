# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Calc shape anchor reads on live document."""

from __future__ import annotations

from plugin.calc.python.image_egress import _cell_address_key, _shape_anchor_matches_cell
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_calc_page_anchored_shape_anchor_read_uno(ctx, doc):
    """Calc page-anchored shape anchor is CellRangeAddressable (REPORT37 E1)."""
    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    draw_page.add(shape)
    # The calc doc is pooled: a leftover shape grows sheet 0's used area and
    # breaks later used-range tests (calc.test_duckdb_tools_uno).
    try:
        anchor = shape.getPropertyValue("Anchor")
        assert anchor is not None
        # A page-anchored shape's Anchor in Calc is the sheet itself, which has getRangeAddress().
        key = _cell_address_key(anchor)
        assert key is not None
        assert key[0] == 0  # Sheet index 0
        # The sheet range starts at 0,0, but a page-anchored shape is not on A1.
        assert _shape_anchor_matches_cell(shape, sheet.getCellByPosition(0, 0)) is False
    finally:
        draw_page.remove(shape)


def _png_bytes(width: int, height: int) -> bytes:
    """A valid solid-red RGB PNG of the given pixel size."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@native_test
@with_native_doc("calc")
def test_reanchored_plot_slots_chain_reuse_and_sweep_uno(ctx, doc):
    """Live Calc re-anchors slots beyond the formula cell; the owner tag still finds them."""
    from plugin.calc.python.image_egress import (
        _anchored_plot_slots,
        _insert_image_result_on_sheet_impl,
        _read_plot_tag,
    )

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    cell = sheet.getCellByPosition(0, 0)
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    # With no formula code, the selected cell is the plot owner.
    controller.select(cell)
    payloads = [
        {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20 + i, 10)} for i in range(3)
    ]

    def run(count: int) -> list:
        for i in range(count):
            _insert_image_result_on_sheet_impl(payloads[i], doc=doc, image_index=i, image_count=count)
        return [shape for shape, _idx in sorted(_anchored_plot_slots(sheet, cell), key=lambda s: s[1])]

    try:
        slots = run(3)
        assert len(slots) == 3
        # Calc moved slot 1's anchor off A1 to the cell under its new position.
        assert not _shape_anchor_matches_cell(slots[1], cell)
        assert _read_plot_tag(slots[1]).owner == (0, 0)
        for prev, nxt in zip(slots, slots[1:]):
            assert nxt.getPosition().X == prev.getPosition().X + prev.getSize().Width

        assert len(run(3)) == 3
        assert draw_page.getCount() == 3

        assert len(run(2)) == 2
        assert draw_page.getCount() == 2
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))


@native_test
@with_native_doc("calc")
def test_plot_follows_formula_after_row_and_column_insert_uno(ctx, doc):
    """Inserting rows/columns above or left of the formula reuses the existing plot slots."""
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl, _read_plot_tag

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    payloads = [
        {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20 + i, 10)} for i in range(2)
    ]

    inserted_row = False
    inserted_col = False
    try:
        b3 = sheet.getCellByPosition(1, 2)
        controller.select(b3)
        for i in range(2):
            _insert_image_result_on_sheet_impl(payloads[i], doc=doc, image_index=i, image_count=2)
        assert draw_page.getCount() == 2
        orig_shapes = [draw_page.getByIndex(0), draw_page.getByIndex(1)]
        orig_positions = [(s.getPosition().X, s.getPosition().Y) for s in orig_shapes]

        sheet.getRows().insertByIndex(0, 1)
        inserted_row = True
        sheet.getColumns().insertByIndex(0, 1)
        inserted_col = True
        moved_positions = [(s.getPosition().X, s.getPosition().Y) for s in orig_shapes]

        c4 = sheet.getCellByPosition(2, 3)
        controller.select(c4)
        for i in range(2):
            _insert_image_result_on_sheet_impl(payloads[i], doc=doc, image_index=i, image_count=2)

        assert draw_page.getCount() == 2
        assert draw_page.getByIndex(0) == orig_shapes[0]
        assert draw_page.getByIndex(1) == orig_shapes[1]
        assert [(draw_page.getByIndex(i).getPosition().X, draw_page.getByIndex(i).getPosition().Y) for i in range(2)] == moved_positions
        assert moved_positions != orig_positions
        assert all(_read_plot_tag(draw_page.getByIndex(i)).owner == (2, 3) for i in range(2))
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        if inserted_col:
            sheet.getColumns().removeByIndex(0, 1)
        if inserted_row:
            sheet.getRows().removeByIndex(0, 1)


@native_test
@with_native_doc("calc")
def test_dragged_plot_is_refreshed_in_place_uno(ctx, doc):
    """A plot dragged to another cell is refreshed in place at its new position."""
    import hashlib
    from com.sun.star.awt import Point
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl, _read_plot_tag

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    b3 = sheet.getCellByPosition(1, 2)
    controller.select(b3)

    p1 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    p2 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(30, 15)}

    try:
        _insert_image_result_on_sheet_impl(p1, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        shape = draw_page.getByIndex(0)
        shape.setPosition(Point(30000, 20000))
        dragged_pos = (shape.getPosition().X, shape.getPosition().Y)

        controller.select(b3)
        _insert_image_result_on_sheet_impl(p2, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        assert draw_page.getByIndex(0) == shape
        assert (shape.getPosition().X, shape.getPosition().Y) == dragged_pos
        tag = _read_plot_tag(shape)
        assert tag.sha == hashlib.sha256(p2["data"]).hexdigest()
        anchor = shape.getPropertyValue("Anchor").getCellAddress()
        assert tag.anchor == (anchor.Column, anchor.Row)
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))


@native_test
@with_native_doc("calc")
def test_pasted_plot_copy_survives_recalc_uno(ctx, doc):
    """A copy-pasted plot gets a non-slot name from Calc and survives recalc."""
    from plugin.calc.python.image_egress import (
        _insert_image_result_on_sheet_impl,
        _plot_slot_index,
        _read_plot_tag,
    )

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    b3 = sheet.getCellByPosition(1, 2)
    controller.select(b3)

    p1 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    p2 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(24, 12)}

    try:
        _insert_image_result_on_sheet_impl(p1, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        orig = draw_page.getByIndex(0)
        orig_sha = _read_plot_tag(orig).sha

        controller.select(orig)
        transferable = controller.getTransferable()
        h20 = sheet.getCellByPosition(7, 19)
        controller.select(h20)
        controller.insertTransferable(transferable)
        assert draw_page.getCount() == 2

        controller.select(b3)
        _insert_image_result_on_sheet_impl(p2, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 2

        shapes = [draw_page.getByIndex(i) for i in range(2)]
        copies = [s for s in shapes if _plot_slot_index(s.getPropertyValue("Name")) is None]
        assert len(copies) == 1
        assert _read_plot_tag(copies[0]).sha == orig_sha
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))


@native_test
@with_native_doc("calc")
def test_py_plot_joins_formula_undo_step_uno(ctx, doc):
    """=PY() plot placement adds no undo entry and is removed when its formula is undone."""
    from com.sun.star.beans import PropertyValue
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl
    from plugin.framework.queue_executor import pump_main_thread_work_queue

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    um = doc.getUndoManager()
    assert um is not None
    um.clear()

    dispatcher = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = controller.getFrame()
    code = "result = 4107"
    formula = f'=PY("{code}")'

    def enter_string(cell, text: str) -> None:
        controller.select(cell)
        prop = PropertyValue()
        prop.Name = "StringName"
        prop.Value = text
        dispatcher.executeDispatch(frame, ".uno:EnterString", "", 0, (prop,))

    payloads = [
        {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20 + i, 10)} for i in range(3)
    ]

    def place(count: int) -> None:
        for i in range(count):
            _insert_image_result_on_sheet_impl(payloads[i], code=code, doc=doc, image_index=i, image_count=count)

    b3 = sheet.getCellByPosition(1, 2)
    d1 = sheet.getCellByPosition(3, 0)
    try:
        enter_string(b3, formula)
        entered_formula = b3.getFormula()
        assert entered_formula != ""
        undo_titles = list(um.getAllUndoActionTitles())
        assert len(undo_titles) == 1

        # Step 1: Place 2 slots. Undo titles unchanged, names are WriterAgentPlot, WriterAgentPlot_2.
        place(2)
        assert list(um.getAllUndoActionTitles()) == undo_titles
        assert draw_page.getCount() == 2
        assert [draw_page.getByIndex(i).getPropertyValue("Name") for i in range(2)] == [
            "WriterAgentPlot",
            "WriterAgentPlot_2",
        ]

        # Step 2: Undo removes formula and plots.
        um.undo()
        pump_main_thread_work_queue(max_items=20)
        assert draw_page.getCount() == 0
        assert b3.getFormula() == ""

        # Step 3: Redo and place 2 slots again -> both plots are back.
        um.redo()
        assert b3.getFormula() == entered_formula
        place(2)
        assert draw_page.getCount() == 2
        assert list(um.getAllUndoActionTitles()) == undo_titles

        # Step 4: Type "a" then "b" in D1, undo once so redo is pending. Place 3 slots, then 1.
        enter_string(d1, "a")
        enter_string(d1, "b")
        um.undo()
        undo_before = list(um.getAllUndoActionTitles())
        redo_before = list(um.getAllRedoActionTitles())
        assert len(redo_before) > 0
        place(3)
        assert draw_page.getCount() == 3
        place(1)
        assert draw_page.getCount() == 1
        assert list(um.getAllUndoActionTitles()) == undo_before
        assert list(um.getAllRedoActionTitles()) == redo_before

        # Step 5: Undo the unrelated entry "a": WriterAgentPlot remains.
        um.undo()
        pump_main_thread_work_queue(max_items=20)
        assert draw_page.getCount() == 1
        assert draw_page.getByIndex(0).getPropertyValue("Name") == "WriterAgentPlot"

        # Step 6: Undo the formula: no shapes.
        um.undo()
        pump_main_thread_work_queue(max_items=20)
        assert draw_page.getCount() == 0
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        b3.setFormula("")
        d1.setFormula("")
        um.clear()


@native_test
@with_native_doc("calc")
def test_undone_formula_on_deleted_sheet_preserves_neighbour_plot_uno(ctx, doc):
    """Undoing both a =PY formula and its sheet keeps Sheet1's plot; single undo still cleans up."""
    from com.sun.star.beans import PropertyValue
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl
    from plugin.framework.queue_executor import pump_main_thread_work_queue

    sheets = doc.getSheets()
    sheet1 = sheets.getByIndex(0)
    dp1 = sheet1.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet1)
    um = doc.getUndoManager()
    assert um is not None
    um.clear()

    dispatcher = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = controller.getFrame()
    code = "result = 4108"
    formula = f'=PY("{code}")'
    payload = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}

    def enter_string(cell, text: str) -> None:
        controller.select(cell)
        prop = PropertyValue()
        prop.Name = "StringName"
        prop.Value = text
        dispatcher.executeDispatch(frame, ".uno:EnterString", "", 0, (prop,))

    try:
        initial_sheets = sheets.getCount()
        # Place a plot on Sheet1.A1 (tool-style, no formula).
        a1 = sheet1.getCellByPosition(0, 0)
        controller.select(a1)
        _insert_image_result_on_sheet_impl(payload, doc=doc, image_index=0, image_count=1)
        assert dp1.getCount() == 1
        um.clear()

        # Insert Sheet2 and place a =PY() plot on Sheet2.A1.
        sheets.insertNewByName("Sheet2", 1)
        assert sheets.getCount() == initial_sheets + 1
        sheet2 = sheets.getByIndex(1)
        dp2 = sheet2.getDrawPage()
        controller.setActiveSheet(sheet2)
        enter_string(sheet2.getCellByPosition(0, 0), formula)
        _insert_image_result_on_sheet_impl(payload, code=code, doc=doc, image_index=0, image_count=1)
        assert dp2.getCount() == 1

        # Ordinary single undo still passes the DrawPage identity check and removes Sheet2's plot.
        um.undo()
        pump_main_thread_work_queue(max_items=20)
        assert dp2.getCount() == 0

        # Redo and re-place on Sheet2, then undo the formula and delete Sheet2 before pumping:
        # Sheet2's proxy shifts to Sheet1, and the DrawPage identity guard preserves Sheet1's plot.
        um.redo()
        _insert_image_result_on_sheet_impl(payload, code=code, doc=doc, image_index=0, image_count=1)
        assert dp2.getCount() == 1

        um.undo()
        sheets.removeByName("Sheet2")
        pump_main_thread_work_queue(max_items=20)
        assert sheets.getCount() == initial_sheets
        assert dp1.getCount() == 1
    finally:
        while sheets.getCount() > 1:
            sheets.removeByName(sheets.getByIndex(sheets.getCount() - 1).Name)
        while dp1.getCount():
            dp1.remove(dp1.getByIndex(0))
        um.clear()


@native_test
@with_native_doc("calc")
def test_tool_plot_insert_is_one_undo_step_uno(ctx, doc):
    """Tool plots without a formula record one 'WriterAgent: Insert plot' undo step per plot."""
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl
    from plugin.framework.i18n import _

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    um = doc.getUndoManager()
    assert um is not None
    um.clear()

    c4 = sheet.getCellByPosition(2, 3)
    controller.select(c4)
    p0 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    p1 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(24, 12)}

    def shape_facts(shape) -> tuple:
        pos = shape.getPosition()
        size = shape.getSize()
        return (
            shape.getPropertyValue("Name"),
            _cell_address_key(shape.getPropertyValue("Anchor")),
            (pos.X, pos.Y),
            (size.Width, size.Height),
            shape.getPropertyValue("Graphic").SizePixel.Width,
        )

    try:
        _insert_image_result_on_sheet_impl(p0, doc=doc, image_index=0, image_count=2)
        _insert_image_result_on_sheet_impl(p1, doc=doc, image_index=1, image_count=2)

        assert list(um.getAllUndoActionTitles()) == [_("WriterAgent: Insert plot")] * 2
        assert draw_page.getCount() == 2
        orig = [shape_facts(draw_page.getByIndex(i)) for i in range(2)]

        um.undo()
        assert draw_page.getCount() == 1
        assert draw_page.getByIndex(0).getPropertyValue("Name") == "WriterAgentPlot"

        um.undo()
        assert draw_page.getCount() == 0

        um.redo()
        um.redo()
        assert draw_page.getCount() == 2
        assert [shape_facts(draw_page.getByIndex(i)) for i in range(2)] == orig

        # Shrinking to 1 slot sweeps WriterAgentPlot_2; undoing restores both slots unchanged.
        _insert_image_result_on_sheet_impl(p0, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        um.undo()
        assert draw_page.getCount() == 2
        assert [shape_facts(draw_page.getByIndex(i)) for i in range(2)] == orig
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        um.clear()


@native_test
@with_native_doc("calc")
def test_image_note_joins_formula_undo_step_uno(ctx, doc):
    """WriterAgent image notes add no undo entry, preserve redo, and go away with the formula's undo."""
    from com.sun.star.beans import PropertyValue
    from plugin.calc.python.image_egress import _clear_image_cell_annotation, _set_image_cell_annotation
    from plugin.framework.queue_executor import pump_main_thread_work_queue

    sheet = doc.getSheets().getByIndex(0)
    annotations = sheet.getAnnotations()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    um = doc.getUndoManager()
    assert um is not None
    um.clear()

    dispatcher = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = controller.getFrame()
    code = "result = 4109"
    formula = f'=PY("{code}")'

    def enter_string(cell, text: str) -> None:
        controller.select(cell)
        prop = PropertyValue()
        prop.Name = "StringName"
        prop.Value = text
        dispatcher.executeDispatch(frame, ".uno:EnterString", "", 0, (prop,))

    b3 = sheet.getCellByPosition(1, 2)
    d1 = sheet.getCellByPosition(3, 0)
    try:
        enter_string(b3, formula)
        undo_titles = list(um.getAllUndoActionTitles())
        assert len(undo_titles) == 1

        _set_image_cell_annotation(doc, code, "boom")
        assert b3.getAnnotation().getString() == "WriterAgent image: boom"
        assert list(um.getAllUndoActionTitles()) == undo_titles

        um.undo()
        pump_main_thread_work_queue(max_items=20)
        assert b3.getFormula() == ""
        assert b3.getAnnotation().getString() == ""

        um.redo()
        enter_string(d1, "a")
        enter_string(d1, "b")
        um.undo()
        undo_before = list(um.getAllUndoActionTitles())
        redo_before = list(um.getAllRedoActionTitles())
        assert len(redo_before) > 0

        _set_image_cell_annotation(doc, code, "again")
        assert b3.getAnnotation().getString() == "WriterAgent image: again"
        _clear_image_cell_annotation(doc, code)
        assert b3.getAnnotation().getString() == ""
        assert list(um.getAllUndoActionTitles()) == undo_before
        assert list(um.getAllRedoActionTitles()) == redo_before
    finally:
        while annotations.getCount():
            annotations.removeByIndex(0)
        b3.setFormula("")
        d1.setFormula("")
        um.clear()


@native_test
@with_native_doc("calc")
def test_new_plot_stands_beside_its_cell_uno(ctx, doc):
    """A new slot 0 stands just inside the cell's far edge in both LTR and RTL, anchored to the formula cell."""
    from plugin.calc.calc_utils import get_cell_geometry
    from plugin.calc.python.image_egress import _PLOT_EDGE_INSET, _anchored_plot_slots, _insert_image_result_on_sheet_impl

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    orig_layout = int(sheet.getPropertyValue("TableLayout"))
    payload = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}

    try:
        for rtl in (False, True):
            while draw_page.getCount():
                draw_page.remove(draw_page.getByIndex(0))
            sheet.setPropertyValue("TableLayout", 1 if rtl else 0)
            b3 = sheet.getCellByPosition(1, 2)
            controller.select(b3)
            _insert_image_result_on_sheet_impl(payload, doc=doc, image_index=0, image_count=1)
            slots = _anchored_plot_slots(sheet, b3)
            assert len(slots) == 1
            shape = slots[0][0]
            cell_pos, cell_size = get_cell_geometry(sheet, b3)
            shape_pos = shape.getPosition()
            shape_size = shape.getSize()
            if rtl:
                near_edge = int(shape_pos.X) + int(shape_size.Width)
                far_edge = int(cell_pos.X)
                assert far_edge <= near_edge <= far_edge + _PLOT_EDGE_INSET
            else:
                near_edge = int(shape_pos.X)
                far_edge = int(cell_pos.X) + int(cell_size.Width)
                assert far_edge - _PLOT_EDGE_INSET <= near_edge <= far_edge
            assert _cell_address_key(shape.getPropertyValue("Anchor")) == _cell_address_key(b3)
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        sheet.setPropertyValue("TableLayout", orig_layout)
        doc.getUndoManager().clear()


@native_test
@with_native_doc("calc")
def test_beside_plot_follows_cut_paste_and_xlsx_uno(ctx, doc):
    """A beside-cell plot follows Cut/Paste of its formula and survives LTR XLSX reload."""
    import os
    import tempfile
    import uno
    from com.sun.star.beans import PropertyValue
    from plugin.calc.python.image_egress import _anchored_plot_slots, _insert_image_result_on_sheet_impl

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    dispatcher = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = controller.getFrame()
    um = doc.getUndoManager()

    code = "result = 'cut_paste_xlsx_probe'"
    formula = f'=PY("{code}")'
    payload = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    b3 = sheet.getCellByPosition(1, 2)
    f9 = sheet.getCellByPosition(5, 8)

    try:
        b3.setFormula(formula)
        _insert_image_result_on_sheet_impl(payload, code=code, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1

        controller.select(b3)
        dispatcher.executeDispatch(frame, ".uno:Cut", "", 0, ())
        controller.select(f9)
        dispatcher.executeDispatch(frame, ".uno:Paste", "", 0, ())

        _insert_image_result_on_sheet_impl(payload, code=code, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        assert len(_anchored_plot_slots(sheet, f9)) == 1

        with tempfile.TemporaryDirectory(prefix="wa-plot-xlsx-") as tmpdir:
            xlsx_path = os.path.join(tmpdir, "plot_beside.xlsx")
            xlsx_url = uno.systemPathToFileUrl(xlsx_path)
            save_prop = PropertyValue()
            save_prop.Name = "FilterName"
            save_prop.Value = "Calc MS Excel 2007 XML"
            doc.storeToURL(xlsx_url, (save_prop,))

            desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            hidden_prop = PropertyValue()
            hidden_prop.Name = "Hidden"
            hidden_prop.Value = True
            reloaded = desktop.loadComponentFromURL(xlsx_url, "_blank", 0, (hidden_prop,))
            try:
                reloaded_sheet = reloaded.getSheets().getByIndex(0)
                reloaded_f9 = reloaded_sheet.getCellByPosition(5, 8)
                assert len(_anchored_plot_slots(reloaded_sheet, reloaded_f9)) == 1
            finally:
                reloaded.close(True)
    finally:
        for cell in (b3, f9):
            cell.setFormula("")
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        um.clear()


@native_test
@with_native_doc("calc")
def test_spill_plot_stands_past_spill_row_and_xlsx_uno(ctx, doc):
    """Multi-column spill places slot 0 past the last spilled column (LTR/RTL) and survives XLSX reload."""
    import os
    import tempfile
    import uno
    from com.sun.star.beans import PropertyValue
    from plugin.calc.python.image_egress import _PLOT_EDGE_INSET, _anchored_plot_slots, _insert_image_result_on_sheet_impl

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    um = doc.getUndoManager()
    code = "result = [[1, 2, fig]]"
    payload = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    b3 = sheet.getCellByPosition(1, 2)
    d3 = sheet.getCellByPosition(3, 2)

    try:
        for rtl in (False, True):
            while draw_page.getCount():
                draw_page.remove(draw_page.getByIndex(0))
            sheet.setPropertyValue("TableLayout", 1 if rtl else 0)
            b3.setFormula(f'=PY("{code}")')
            _insert_image_result_on_sheet_impl(payload, code=code, doc=doc, image_index=0, image_count=1, spill_cols=3)
            assert draw_page.getCount() == 1
            shape = draw_page.getByIndex(0)
            pos = shape.getPosition()
            size = shape.getSize()
            if rtl:
                assert abs((pos.X + size.Width) - (d3.Position.X + _PLOT_EDGE_INSET)) <= 2
            else:
                assert abs(pos.X - (d3.Position.X + d3.Size.Width - _PLOT_EDGE_INSET)) <= 2
            _insert_image_result_on_sheet_impl(payload, code=code, doc=doc, image_index=0, image_count=1, spill_cols=3)
            assert draw_page.getCount() == 1

            with tempfile.TemporaryDirectory(prefix="wa-spill-plot-xlsx-") as tmpdir:
                xlsx_path = os.path.join(tmpdir, "spill_plot.xlsx")
                xlsx_url = uno.systemPathToFileUrl(xlsx_path)
                save_prop = PropertyValue()
                save_prop.Name = "FilterName"
                save_prop.Value = "Calc MS Excel 2007 XML"
                doc.storeToURL(xlsx_url, (save_prop,))

                desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
                hidden_prop = PropertyValue()
                hidden_prop.Name = "Hidden"
                hidden_prop.Value = True
                reloaded = desktop.loadComponentFromURL(xlsx_url, "_blank", 0, (hidden_prop,))
                try:
                    reloaded_sheet = reloaded.getSheets().getByIndex(0)
                    reloaded_b3 = reloaded_sheet.getCellByPosition(1, 2)
                    assert len(_anchored_plot_slots(reloaded_sheet, reloaded_b3, 3)) == 1
                    if not rtl:
                        assert len(_anchored_plot_slots(reloaded_sheet, reloaded_b3)) == 0
                finally:
                    reloaded.close(True)
    finally:
        sheet.setPropertyValue("TableLayout", 0)
        b3.setFormula("")
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        um.clear()
