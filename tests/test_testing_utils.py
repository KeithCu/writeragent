# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared pytest stubs in testing_utils."""

import pytest

from plugin.framework.tool import ToolContext
from plugin.tests.testing_utils import (
    CalcDocStub,
    MockContext,
    TestingFactory,
    WriterDocStub,
    _exception_leaving_native_doc,
)


def test_calc_doc_stub_defaults():
    doc = CalcDocStub()
    assert doc.supportsService("com.sun.star.sheet.SpreadsheetDocument")
    assert not doc.supportsService("com.sun.star.text.TextDocument")
    assert doc.getURL() == "test://calc"
    sheets = doc.getSheets()
    assert sheets.getCount() == 1
    assert sheets.hasByName("Sheet1")
    sheet = sheets.getByName("Sheet1")
    assert sheet.getName() == "Sheet1"
    assert doc.getCurrentController().getActiveSheet() is sheet
    assert doc.CurrentController.ActiveSheet is sheet
    sel = doc.CurrentController.Selection
    addr = sel.getRangeAddress()
    assert (addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow) == (0, 0, 0, 0)


def test_calc_doc_stub_seed_data_and_a1_range():
    doc = CalcDocStub(data=(("hello", 2.5), ("=A1", "")))
    sheet = doc.getSheets().getByIndex(0)
    assert sheet.getCellByPosition(0, 0).getString() == "hello"
    assert sheet.getCellByPosition(1, 0).getValue() == 2.5
    assert sheet.getCellByPosition(0, 1).getFormula() == "=A1"
    b2 = sheet.getCellRangeByName("B2")
    assert b2.getRangeAddress().StartColumn == 1
    assert b2.getRangeAddress().StartRow == 1
    rng = sheet.getCellRangeByName("A1:B1")
    assert rng.getDataArray() == (("hello", 2.5),)


def test_calc_doc_stub_insert_sheet_and_selection_override():
    doc = CalcDocStub(selection="B2")
    sheets = doc.getSheets()
    sheets.insertNewByName("Extra", 1)
    assert sheets.hasByName("Extra")
    assert sheets.getCount() == 2
    assert sheets.getByIndex(1).getName() == "Extra"
    addr = doc.getCurrentController().getSelection().getRangeAddress()
    assert (addr.StartColumn, addr.StartRow) == (1, 1)


def test_testing_factory_create_doc_calc():
    doc = TestingFactory.create_doc(doc_type="calc")
    assert isinstance(doc, CalcDocStub)
    assert doc.supportsService("com.sun.star.sheet.SpreadsheetDocument")
    assert doc.getSheets().hasByName("Sheet1")

    seeded = TestingFactory.create_doc(doc_type="calc", data=(("x",),))
    assert seeded.getSheets().getByIndex(0).getCellByPosition(0, 0).getString() == "x"


def test_calc_sheet_query_content_cells_formulas():
    doc = CalcDocStub(
        data=(
            ('=PY("a")', "plain"),
            ("=SUM(A1)", '=PY("b")'),
        )
    )
    sheet = doc.getSheets().getByName("Sheet1")
    enum = sheet.queryContentCells(16)
    assert enum.getCount() == 1
    rng = enum.getByIndex(0)
    addr = rng.getRangeAddress()
    assert (addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow) == (0, 0, 1, 1)
    formulas = rng.getFormulas()
    assert formulas[0][0] == '=PY("a")'
    assert formulas[1][1] == '=PY("b")'


def test_calc_doc_stub_calculate_all_and_props_listeners():
    doc = CalcDocStub(props={"RuntimeUID": "uid-1"})
    assert doc.getPropertyValue("RuntimeUID") == "uid-1"
    doc.setPropertyValue("RuntimeUID", "uid-2")
    assert doc.getPropertyValue("RuntimeUID") == "uid-2"
    doc.calculateAll()
    doc.calculateAll()
    assert doc.calculate_all_count == 2
    doc.addDocumentEventListener(object())
    assert len(doc._document_event_listeners) == 1


def test_testing_factory_create_doc_writer():
    doc = TestingFactory.create_doc(doc_type="writer")
    assert isinstance(doc, WriterDocStub)
    assert doc.supportsService("com.sun.star.text.TextDocument")
    assert not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument")

    seeded = TestingFactory.create_doc(
        doc_type="writer",
        content=[],
        items={"ParagraphStyles": object()},
    )
    assert isinstance(seeded, WriterDocStub)
    assert seeded.getStyleFamilies().hasByName("ParagraphStyles")
    assert seeded.getStyleFamilies().getElementNames() == ("ParagraphStyles",)


def test_testing_factory_create_context_mock():
    writer_ctx = TestingFactory.create_context(doc_type="writer")
    assert isinstance(writer_ctx, ToolContext)
    assert isinstance(writer_ctx.doc, WriterDocStub)
    assert isinstance(writer_ctx.ctx, MockContext)
    assert writer_ctx.doc_type == "writer"

    calc_ctx = TestingFactory.create_context(doc_type="calc")
    assert isinstance(calc_ctx.doc, CalcDocStub)
    assert calc_ctx.doc_type == "calc"


def test_testing_factory_create_context_native_requires_doc():
    with pytest.raises(ValueError, match="requires doc="):
        TestingFactory.create_context(env="native", doc_type="writer")


def test_clear_writeragent_udprops_skips_set_document_scripts():
    """GHA 33707990007: wipe must not call isReadonly via set_document_scripts."""
    from unittest.mock import MagicMock, patch

    from plugin.scripting.document_scripts import DOCUMENT_SCRIPTS_UDPROP
    from plugin.scripting.session_manager import PYTHON_WORKBOOK_SESSION_PROP
    from plugin.tests import testing_utils as tu

    doc = MagicMock()
    doc.isReadonly = MagicMock(side_effect=AssertionError("isReadonly must not run"))

    with (
        patch("plugin.scripting.document_scripts.set_document_scripts") as set_scripts,
        patch("plugin.doc.udprops.set_document_property") as set_prop,
    ):
        tu._clear_writeragent_udprops(doc)

    set_scripts.assert_not_called()
    doc.isReadonly.assert_not_called()
    written = {call.args[1]: call.args[2] for call in set_prop.call_args_list}
    assert written[DOCUMENT_SCRIPTS_UDPROP] == ""
    assert written[PYTHON_WORKBOOK_SESSION_PROP] == ""
    assert written["WriterAgentSessionID"] == ""


def test_reraise_native_open_failure_names_previous_test(capsys, monkeypatch):
    """Factory-open DisposedException must name the previous TEST end in the message."""
    import plugin.testing_runner as tr
    from plugin.tests.testing_utils import _reraise_native_open_failure

    monkeypatch.setattr(tr, "_soffice_pids", lambda: "7")
    tr.current_office().reset_lifecycle_breadcrumb()
    tr.record_test_end("draw.test_draw_uno.test_duplicate_slide_copies_shapes", "OK")
    tr.record_test_start("draw.test_draw_uno.test_duplicate_rename_move_slide")

    with pytest.raises(RuntimeError, match="previous=draw.test_draw_uno.test_duplicate_slide_copies_shapes") as caught:
        try:
            raise RuntimeError("Binary URP bridge disposed during call")
        except RuntimeError as exc:
            _reraise_native_open_failure(exc, "private:factory/sdraw")
    assert "create_native_doc loadComponentFromURL(private:factory/sdraw)" in str(caught.value)
    assert "pre_open=" in str(caught.value)
    assert "Binary URP bridge" in str(caught.value)
    err = capsys.readouterr().err
    assert "LIFECYCLE native_doc open FAIL" in err
    assert "previous=draw.test_draw_uno.test_duplicate_slide_copies_shapes" in err
    tr.current_office().reset_lifecycle_breadcrumb()


def test_reraise_native_open_failure_passthrough_non_urp():
    from plugin.tests.testing_utils import _reraise_native_open_failure

    with pytest.raises(ValueError, match="not a bridge"):
        try:
            raise ValueError("not a bridge")
        except ValueError as exc:
            _reraise_native_open_failure(exc, "private:factory/sdraw")


class _Enum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


@pytest.mark.parametrize(
    ("has_leftover", "use_other_import", "expected_count"),
    [
        (True, False, 1),
        (False, False, 0),
        (True, True, 1),
    ],
)
def test_prepare_windows_writer_factory_reactivates_keeper(
    monkeypatch, has_leftover: bool, use_other_import: bool, expected_count: int
):
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as plugin_tu
    import tests.testing_utils as tests_tu

    keeper = MagicMock(name="keeper")
    keeper.RuntimeUID = "1"
    keeper.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    frame = MagicMock(name="keeper_frame")
    keeper.getCurrentController.return_value.getFrame.return_value = frame
    docs = [keeper]
    leftover = None
    if has_leftover:
        leftover = MagicMock(name="leftover")
        leftover.RuntimeUID = "26"
        leftover.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
        docs.append(leftover)

    desktop = MagicMock()
    desktop.getComponents.return_value.createEnumeration.return_value = _Enum(docs)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda _ctx: desktop)
    setter_mod = tests_tu if use_other_import else plugin_tu
    setter_mod.set_harness_keeper_uid("1", keeper)
    try:
        assert plugin_tu.prepare_windows_writer_factory(object()) == expected_count
        if leftover is not None:
            leftover.close.assert_not_called()
        keeper.close.assert_not_called()
        desktop.setActiveFrame.assert_called_once_with(frame)
        assert plugin_tu._STATE.keeper_uid == "1"
    finally:
        plugin_tu._set_windows_leftover_open(0)
        plugin_tu.set_harness_keeper_uid("")


def test_testing_utils_import_names_are_one_module():
    import plugin.tests.testing_utils as plugin_tu
    import tests.testing_utils as tests_tu

    assert tests_tu is plugin_tu
    import plugin.tests.doc_stubs as plugin_stubs
    import tests.doc_stubs as tests_stubs

    assert tests_stubs is plugin_stubs
    assert plugin_tu.CalcDocStub is plugin_stubs.CalcDocStub


def test_testing_utils_identity_when_tests_name_imported_first():
    import tests.testing_utils as tests_tu
    import plugin.tests.testing_utils as plugin_tu

    assert tests_tu is plugin_tu
    assert tests_tu.CalcDocStub is plugin_tu.CalcDocStub
    keeper_doc = object()
    try:
        plugin_tu.set_harness_keeper_uid("1", keeper_doc)
        assert tests_tu._STATE.keeper_uid == "1"
        assert tests_tu._STATE.keeper_doc is keeper_doc
    finally:
        plugin_tu.set_harness_keeper_uid("")


@pytest.mark.parametrize(
    ("doc_type", "expected_url", "expected_target"),
    [
        ("writer", "private:factory/swriter", "_wa_factory"),
        ("calc", "private:factory/scalc", "_wa_scalc"),
        ("draw", "private:factory/sdraw", "_wa_sdraw"),
        ("impress", "private:factory/simpress", "_wa_simpress"),
    ],
)
def test_create_native_doc_windows_reuses_stable_target(
    monkeypatch, doc_type: str, expected_url: str, expected_target: str
):
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = MagicMock()
    calls = []
    saved_seq = tu._STATE.factory_seq
    tu._STATE.factory_seq = 0
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(
        tu, "prepare_windows_writer_factory", lambda _ctx: calls.append("prepare") or 2
    )
    try:
        with (
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("uno.createUnoStruct", return_value=MagicMock()),
            patch("plugin.testing_runner.probe_uno_bridge", return_value="alive"),
        ):
            TestingFactory.create_native_doc(object(), doc_type)
            TestingFactory.create_native_doc(object(), doc_type)
        assert calls == ["prepare", "prepare"]
        args = desktop.loadComponentFromURL.call_args.args
        assert args[0] == expected_url
        assert args[1] == expected_target
        assert args[2] == (8 | 55)
    finally:
        tu._STATE.factory_seq = saved_seq


def test_windows_factory_load_args_blank_and_unknown_sequence():
    import plugin.tests.testing_utils as tu

    saved = tu._STATE.factory_seq
    tu._STATE.factory_seq = 0
    try:
        assert tu._windows_factory_load_args("private:factory/swriter", 0) == ("_blank", 0)
        assert tu._windows_factory_load_args("private:factory/smath", 2) == ("_wa_factory_1", 8 | 55)
        assert tu._windows_factory_load_args("private:factory/sdraw", 2) == ("_wa_sdraw", 8 | 55)
        assert tu._windows_factory_load_args("private:factory/smath", 2) == ("_wa_factory_2", 8 | 55)
    finally:
        tu._STATE.factory_seq = saved


def test_create_native_doc_windows_skips_leftover_impress_when_leftovers_high(
    monkeypatch,
):
    """GHA 34661915875 / 34672065355: leftover _wa_simpress hung 30s.

    #737's stable name is live. Leftover _wa_factory swriter at
    leftover_open=15 returned. leftover_open=3 still hung (old max=4).
    Skip leftover Draw/Impress; leftover Writer still loads.
    """
    import unittest
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = MagicMock()
    calls = []
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(
        tu, "prepare_windows_writer_factory", lambda _ctx: calls.append("prepare") or 3
    )
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        patch("uno.createUnoStruct", return_value=MagicMock()),
        patch("plugin.testing_runner.probe_uno_bridge", return_value="alive"),
    ):
        try:
            TestingFactory.create_native_doc(object(), "impress")
        except unittest.SkipTest as exc:
            assert "simpress" in str(exc)
            assert "leftovers=3" in str(exc)
        else:
            raise AssertionError("expected SkipTest")
        desktop.loadComponentFromURL.assert_not_called()
        try:
            TestingFactory.create_native_doc(object(), "draw")
        except unittest.SkipTest as exc:
            assert "sdraw" in str(exc)
        else:
            raise AssertionError("expected SkipTest")
        TestingFactory.create_native_doc(object(), "writer")
        args = desktop.loadComponentFromURL.call_args.args
        assert args[0] == "private:factory/swriter"
        assert args[1] == "_wa_factory"
        assert calls == ["prepare", "prepare", "prepare"]


def test_note_windows_html_paste_leftover_sets_cached_count(monkeypatch):
    import plugin.tests.testing_utils as tu

    saved = tu._windows_leftover_open()
    monkeypatch.setattr(tu.sys, "platform", "win32")
    tu._set_windows_leftover_open(0)
    try:
        tu.note_windows_html_paste_leftover()
        assert tu._windows_leftover_open() == 1
        tu.note_windows_html_paste_leftover()
        assert tu._windows_leftover_open() == 1
    finally:
        tu._set_windows_leftover_open(saved)


def test_note_windows_html_paste_leftover_noop_on_posix(monkeypatch):
    import plugin.tests.testing_utils as tu

    saved = tu._windows_leftover_open()
    monkeypatch.setattr(tu.sys, "platform", "linux")
    tu._set_windows_leftover_open(0)
    try:
        tu.note_windows_html_paste_leftover()
        assert tu._windows_leftover_open() == 0
    finally:
        tu._set_windows_leftover_open(saved)


def test_reset_writer_page_regions_clears_first_page_and_shares(monkeypatch):
    """GHA 35466498641: body wipe left header_first / FirstIsShared on the pool."""
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu

    header = MagicMock()
    header.getString.return_value = "leftover"
    header_first = MagicMock()
    header_first.getString.return_value = "letterhead"
    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    leftover_enum = MagicMock()
    leftover_enum.hasMoreElements.side_effect = [True, False]
    leftover_enum.nextElement.return_value = table
    header_first.createEnumeration.return_value = leftover_enum

    style = MagicMock()
    props = {
        "HeaderText": header,
        "HeaderTextFirst": header_first,
        "HeaderTextLeft": None,
        "FooterText": None,
        "FooterTextFirst": None,
        "FooterTextLeft": None,
    }
    style.getPropertyValue.side_effect = props.get
    styles = MagicMock()
    styles.getElementNames.return_value = ("Standard",)
    styles.getByName.return_value = style
    families = MagicMock()
    families.getByName.return_value = styles
    body = MagicMock()
    doc = MagicMock()
    doc.getStyleFamilies.return_value = families
    doc.getText.return_value.createTextCursor.return_value = body

    tu._reset_writer_page_regions(doc)

    style.setPropertyValue.assert_any_call("FirstIsShared", True)
    header.setString.assert_called_with("")
    header_first.setString.assert_called_with("")
    header_first.removeTextContent.assert_called_with(table)
    body.setPropertyValue.assert_called_with("PageDescName", "Standard")


def test_create_native_doc_windows_skips_hidden_blank_after_bitmap(monkeypatch):
    import unittest
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = MagicMock()
    saved_bitmap = tu._windows_hidden_open_bitmap()
    monkeypatch.setattr(tu.sys, "platform", "win32")
    tu._set_windows_hidden_open_bitmap(True)
    try:
        with (
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch("uno.createUnoStruct", return_value=MagicMock()),
            patch("plugin.testing_runner.probe_uno_bridge", return_value="alive"),
        ):
            monkeypatch.setattr(tu, "prepare_windows_writer_factory", lambda _ctx: 0)
            with pytest.raises(unittest.SkipTest):
                TestingFactory.create_native_doc(object(), "writer")
            desktop.loadComponentFromURL.assert_not_called()
            monkeypatch.setattr(tu, "prepare_windows_writer_factory", lambda _ctx: 2)
            TestingFactory.create_native_doc(object(), "writer")
            args = desktop.loadComponentFromURL.call_args.args
            assert args[0] == "private:factory/swriter"
            assert args[1] == "_wa_factory"
    finally:
        tu._set_windows_hidden_open_bitmap(saved_bitmap)


def test_windows_hidden_open_bitmap_err_and_skip(monkeypatch):
    import unittest

    import plugin.tests.testing_utils as tu

    saved = tu._windows_hidden_open_bitmap()
    monkeypatch.setattr(tu.sys, "platform", "win32")
    tu._set_windows_hidden_open_bitmap(False)
    try:
        assert tu.windows_hidden_open_bitmap_err(None) is False
        assert tu.windows_hidden_open_bitmap_err("Failed to open x") is False
        assert tu.windows_hidden_open_bitmap_err(
            "Failed to open Budget_read.ods: Could not create system bitmap!"
        ) is True
        tu.skip_windows_hidden_open_after_bitmap("unit")
        tu.note_windows_hidden_open_bitmap(
            "Failed to open Budget_read.ods: Could not create system bitmap!"
        )
        assert tu._windows_hidden_open_bitmap() is True
        with pytest.raises(unittest.SkipTest):
            tu.skip_windows_hidden_open_after_bitmap("unit")
    finally:
        tu._set_windows_hidden_open_bitmap(saved)
    monkeypatch.setattr(tu.sys, "platform", "linux")
    tu._set_windows_hidden_open_bitmap(False)
    try:
        assert tu.windows_hidden_open_bitmap_err(
            "Could not create system bitmap!"
        ) is False
        tu.note_windows_hidden_open_bitmap("Could not create system bitmap!")
        assert tu._windows_hidden_open_bitmap() is False
        tu.skip_windows_hidden_open_after_bitmap("unit")
    finally:
        tu._set_windows_hidden_open_bitmap(saved)


def test_draw_doc_has_math_ole_reads_clsid():
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import _MATH_OLE_CLSID, _draw_doc_has_math_ole

    shape = MagicMock()
    shape.CLSID = _MATH_OLE_CLSID
    page = MagicMock()
    page.getCount.return_value = 1
    page.getByIndex.return_value = shape
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc = MagicMock()
    doc.getDrawPages.return_value = pages
    assert _draw_doc_has_math_ole(doc) is True
    shape.CLSID = "not-math"
    assert _draw_doc_has_math_ole(doc) is False
    assert _draw_doc_has_math_ole(None) is False


def test_close_doc_windows_skips_math_ole_draw(monkeypatch):
    """GHA 34607010446: close_doc dispose of Math OLE Draw killed soffice."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory, mark_windows_math_ole_doc
    import plugin.tests.testing_utils as tu

    doc = MagicMock()
    doc.RuntimeUID = "48"
    doc.supportsService.side_effect = lambda svc: svc.endswith("DrawingDocument")
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(tu, "reactivate_harness_keeper", lambda desktop=None: True)
    tu._clear_windows_math_ole_uids()
    try:
        mark_windows_math_ole_doc(doc)
        TestingFactory.close_doc(doc)
        doc.close.assert_not_called()
        doc.supportsService.assert_not_called()
    finally:
        tu._clear_windows_math_ole_uids()


def test_close_doc_windows_draw_without_math_still_closes(monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    shape = MagicMock()
    shape.CLSID = "not-math"
    page = MagicMock()
    page.getCount.return_value = 1
    page.getByIndex.return_value = shape
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc = MagicMock()
    doc.RuntimeUID = "47"
    doc.supportsService.side_effect = lambda svc: svc.endswith("DrawingDocument")
    doc.getDrawPages.return_value = pages
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr("gc.collect", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(tu, "_windows_leftover_open", lambda: 3)
    tu.set_harness_keeper_uid("1")
    tu._clear_windows_math_ole_uids()
    try:
        TestingFactory.close_doc(doc)
        doc.close.assert_called_once_with(True)
        doc.getDrawPages.assert_not_called()
    finally:
        tu.set_harness_keeper_uid("")
        tu._clear_windows_math_ole_uids()


def test_close_doc_posix_closes_math_ole_draw(monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory, mark_windows_math_ole_doc
    import plugin.tests.testing_utils as tu

    doc = MagicMock()
    doc.RuntimeUID = "48"
    doc.supportsService.side_effect = lambda svc: svc.endswith("DrawingDocument")
    monkeypatch.setattr(tu.sys, "platform", "linux")
    monkeypatch.setattr("gc.collect", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    mark_windows_math_ole_doc(doc)
    TestingFactory.close_doc(doc)
    doc.close.assert_called_once_with(True)


def test_close_doc_skips_windows_notebook_leftover(monkeypatch):
    """GHA 34646877587: close notebook leftover then next _wa_notebook hung."""
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    doc.RuntimeUID = "41"
    saved = tu._windows_leftover_open()
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(tu, "reactivate_harness_keeper", lambda desktop=None: True)
    tu._set_windows_leftover_open(3)
    try:
        with patch(
            "plugin.notebook.cell_registry.has_notebook_registry",
            return_value=True,
        ):
            TestingFactory.close_doc(doc)
        doc.close.assert_not_called()
    finally:
        tu._set_windows_leftover_open(saved)


def test_windows_notebook_host_uses_dedicated_factory_target(monkeypatch):
    import plugin.tests.testing_utils as tu

    saved = tu._windows_leftover_open()
    monkeypatch.setattr(tu.sys, "platform", "win32")
    tu.set_windows_notebook_host(True)
    tu._set_windows_leftover_open(5)
    try:
        target, flags = tu._windows_factory_load_args("private:factory/swriter", 5)
        assert target == "_wa_notebook_host"
        assert flags == (8 | 55)
    finally:
        tu.set_windows_notebook_host(False)
        tu._set_windows_leftover_open(saved)


def test_close_doc_skips_windows_writer_when_leftovers_open(monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    doc.RuntimeUID = "34"
    saved = tu._windows_leftover_open()
    tu._set_windows_leftover_open(2)
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(tu, "reactivate_harness_keeper", lambda desktop=None: True)
    monkeypatch.setattr(
        "plugin.notebook.cell_registry.has_notebook_registry",
        lambda _doc: False,
    )
    try:
        TestingFactory.close_doc(doc)
        doc.close.assert_not_called()
    finally:
        tu._set_windows_leftover_open(saved)


def test_native_doc_windows_reuses_writer_when_leftovers_open(monkeypatch):
    """Second leftover swriter must not factory-load after the first close."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    writer = MagicMock(name="pooled_writer")
    created = []
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(tu, "prepare_windows_writer_factory", lambda _ctx: 2)
    monkeypatch.setattr(tu, "reset_native_doc", lambda *a, **k: None)
    monkeypatch.setattr(tu, "_writer_pool_is_clean", lambda _doc: True)
    monkeypatch.setattr(
        TestingFactory,
        "create_native_doc",
        lambda *a, **k: created.append("create") or writer,
    )
    monkeypatch.setattr(
        TestingFactory, "close_doc", lambda *a, **k: created.append("close")
    )
    tu._NATIVE_DOC_POOL.clear()
    ctx = object()
    try:
        with TestingFactory.native_doc(ctx, "writer") as first:
            assert first is writer
        with TestingFactory.native_doc(ctx, "writer") as second:
            assert second is writer
        assert created == ["create"]
    finally:
        tu._NATIVE_DOC_POOL.clear()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_native_doc_notebook_host_pool_is_separate(monkeypatch, platform):
    """Notebook suites get their own pooled Writer on every platform."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    doc_a = MagicMock(name="writer_a")
    doc_b = MagicMock(name="writer_b")
    docs = iter([("A", doc_a), ("B", doc_b)])
    created = []

    def _create(*_args, **_kwargs):
        label, doc = next(docs)
        created.append(label)
        return doc

    monkeypatch.setattr(tu.sys, "platform", platform)
    monkeypatch.setattr(tu, "reset_native_doc", lambda *a, **k: None)
    monkeypatch.setattr(tu, "_writer_pool_is_clean", lambda _doc: True)
    monkeypatch.setattr(TestingFactory, "create_native_doc", _create)
    tu._NATIVE_DOC_POOL.clear()
    tu.set_windows_notebook_host(False)
    ctx = object()
    try:
        with TestingFactory.native_doc(ctx, "writer") as first:
            assert first is doc_a
        tu.set_windows_notebook_host(True)
        with TestingFactory.native_doc(ctx, "writer") as nb_first:
            assert nb_first is doc_b
        with TestingFactory.native_doc(ctx, "writer") as nb_second:
            assert nb_second is doc_b
        tu.set_windows_notebook_host(False)
        with TestingFactory.native_doc(ctx, "writer") as back_to_shared:
            assert back_to_shared is doc_a
        assert created == ["A", "B"]
    finally:
        tu.set_windows_notebook_host(False)
        tu._NATIVE_DOC_POOL.clear()


def test_native_doc_windows_reuses_calc_when_leftovers_open(monkeypatch):
    """GHA 34643210006: leftover _wa_scalc hung; reuse=False must not factory-load."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    calc = MagicMock(name="pooled_calc")
    created = []
    saved = tu._windows_leftover_open()
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(tu, "reset_native_doc", lambda *a, **k: None)
    monkeypatch.setattr(
        TestingFactory,
        "create_native_doc",
        lambda *a, **k: created.append("create") or calc,
    )
    tu._NATIVE_DOC_POOL.clear()
    tu._set_windows_leftover_open(5)
    ctx = object()
    try:
        with TestingFactory.native_doc(ctx, "calc") as first:
            assert first is calc
        with TestingFactory.native_doc(ctx, "calc", reuse=False) as second:
            assert second is calc
        assert created == ["create"]
    finally:
        tu._set_windows_leftover_open(saved)
        tu._NATIVE_DOC_POOL.clear()


@pytest.mark.parametrize(
    "name, value, value_2, value_3, value_4",
    [
        # GHA 35413789298: non-pooled Impress must not close_doc (GC+50ms).
        pytest.param("impress_doc", "impress-uid", "native_doc impress teardown must not call close_doc", "impress", "impress", id="test_native_doc_impress_teardown_uses_close_draw_family"),
        # Same Draw-family routing for @with_native_doc('draw').
        pytest.param("draw_doc", "draw-uid", "native_doc draw teardown must not call close_doc", "draw", "draw", id="test_native_doc_draw_teardown_uses_close_draw_family"),
    ],
)
def test_native_doc_impress_teardown_uses_close_draw_family(monkeypatch, name, value, value_2, value_3, value_4):
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock(name=name)
    doc.RuntimeUID = value
    closed_docs = []
    settled = []
    health_docs = []

    def _fail_close_doc(_closed):
        raise AssertionError(value_2)

    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(TestingFactory, "close_doc", _fail_close_doc)
    monkeypatch.setattr(
        tu,
        "close_draw_family_doc",
        lambda closed: closed_docs.append(closed),
    )
    monkeypatch.setattr(tu, "settle_after_draw_family_close", lambda: settled.append(True))
    monkeypatch.setattr(
        tu,
        "_log_office_health_after_close",
        lambda _ctx, doc_type: health_docs.append(doc_type),
    )
    with TestingFactory.native_doc(object(), value_3) as got:
        assert got is doc
    assert closed_docs == [doc]
    assert settled == [True]
    assert health_docs == [value_4]


def test_native_doc_impress_teardown_skips_close_when_windows_leftovers(
    monkeypatch,
):
    """GHA 35450779692: leftover Writer + raw Impress close killed soffice."""
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock(name="impress_doc")
    doc.RuntimeUID = "impress-uid"
    health_docs = []

    def _fail_close(_closed):
        raise AssertionError("leftover impress teardown must not close")

    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(TestingFactory, "close_doc", _fail_close)
    monkeypatch.setattr(tu, "close_draw_family_doc", _fail_close)
    monkeypatch.setattr(tu, "settle_after_draw_family_close", _fail_close)
    monkeypatch.setattr(
        tu,
        "_log_office_health_after_close",
        lambda _ctx, doc_type: health_docs.append(doc_type),
    )
    import plugin.testing_runner as tr

    recycle_calls = []
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(
        tr.current_office(),
        "request_recycle",
        lambda: recycle_calls.append("recycle"),
    )
    saved = tu._windows_leftover_open()
    tu._set_windows_leftover_open(1)
    try:
        with TestingFactory.native_doc(object(), "impress") as got:
            assert got is doc
        assert health_docs == ["impress"]
        assert recycle_calls == ["recycle"]
    finally:
        tu._set_windows_leftover_open(saved)


def test_native_doc_draw_teardown_skips_close_when_windows_leftovers(
    monkeypatch,
):
    """Same leftover skip for @with_native_doc('draw') on win32."""
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock(name="draw_doc")
    doc.RuntimeUID = "draw-uid"
    health_docs = []

    def _fail_close(_closed=None):
        raise AssertionError("leftover draw teardown must not close")

    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(TestingFactory, "close_doc", _fail_close)
    monkeypatch.setattr(tu, "close_draw_family_doc", _fail_close)
    monkeypatch.setattr(tu, "settle_after_draw_family_close", _fail_close)
    monkeypatch.setattr(
        tu,
        "_log_office_health_after_close",
        lambda _ctx, doc_type: health_docs.append(doc_type),
    )
    import plugin.testing_runner as tr

    recycle_calls = []
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(
        tr.current_office(),
        "request_recycle",
        lambda: recycle_calls.append("recycle"),
    )
    saved = tu._windows_leftover_open()
    tu._set_windows_leftover_open(2)
    try:
        with TestingFactory.native_doc(object(), "draw") as got:
            assert got is doc
        assert health_docs == ["draw"]
        assert recycle_calls == ["recycle"]
    finally:
        tu._set_windows_leftover_open(saved)


@pytest.mark.parametrize(
    "value, value_2",
    [
        # leftover_open=0 still raw-closes on win32 (not an always-skip).
        pytest.param("win32", 0, id="test_native_doc_impress_teardown_closes_on_windows_without_leftovers"),
        # Linux still close_draw_family even when leftover_open is cached >0.
        pytest.param("linux", 1, id="test_native_doc_impress_teardown_closes_on_posix_with_leftovers"),
    ],
)
def test_native_doc_impress_teardown_closes_on_windows_without_leftovers(monkeypatch, value, value_2):
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock(name="impress_doc")
    doc.RuntimeUID = "impress-uid"
    closed_docs = []
    settled = []
    health_docs = []

    def _fail_close_doc(_closed):
        raise AssertionError("native_doc impress teardown must not call close_doc")

    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(TestingFactory, "close_doc", _fail_close_doc)
    monkeypatch.setattr(
        tu,
        "close_draw_family_doc",
        lambda closed: closed_docs.append(closed),
    )
    monkeypatch.setattr(tu, "settle_after_draw_family_close", lambda: settled.append(True))
    monkeypatch.setattr(
        tu,
        "_log_office_health_after_close",
        lambda _ctx, doc_type: health_docs.append(doc_type),
    )
    monkeypatch.setattr(tu.sys, "platform", value)
    saved = tu._windows_leftover_open()
    tu._set_windows_leftover_open(value_2)
    try:
        with TestingFactory.native_doc(object(), "impress") as got:
            assert got is doc
        assert closed_docs == [doc]
        assert settled == [True]
        assert health_docs == ["impress"]
    finally:
        tu._set_windows_leftover_open(saved)

def test_native_doc_writer_teardown_still_uses_close_doc(monkeypatch):
    """Writer is not Draw-family; keep close_doc."""
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock(name="writer_doc")
    closed_docs = []
    health_called = []

    def _fail_draw_family(_closed):
        raise AssertionError("Writer teardown must not call close_draw_family_doc")

    def _fail_settle():
        raise AssertionError("Writer teardown must not call settle_after_draw_family_close")

    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(
        TestingFactory, "close_doc", lambda closed, *_a, **_k: closed_docs.append(closed)
    )
    monkeypatch.setattr(tu, "close_draw_family_doc", _fail_draw_family)
    monkeypatch.setattr(tu, "settle_after_draw_family_close", _fail_settle)
    monkeypatch.setattr(
        tu, "_log_office_health_after_close", lambda *_a, **_k: health_called.append("health")
    )
    with TestingFactory.native_doc(object(), "writer", reuse=False) as got:
        assert got is doc
    assert closed_docs == [doc]
    assert health_called == ["health"]


def test_native_doc_draw_math_ole_teardown_keeps_close_doc_skip(monkeypatch):
    """GHA 34607010446: Math OLE Draw still skips via close_doc, not raw close."""
    from unittest.mock import MagicMock

    import plugin.tests.testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock(name="math_draw")
    doc.RuntimeUID = "math-ole-uid"
    closed_docs = []
    health_called = []

    def _fail_draw_family(_closed):
        raise AssertionError("Math OLE Draw must not raw-close via close_draw_family_doc")

    def _fail_settle():
        raise AssertionError("Math OLE Draw must not call settle_after_draw_family_close")

    monkeypatch.setattr(tu.sys, "platform", "win32")
    tu._clear_windows_math_ole_uids()
    tu.mark_windows_math_ole_doc(doc)
    saved = tu._windows_leftover_open()
    tu._set_windows_leftover_open(1)
    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(
        TestingFactory, "close_doc", lambda closed, *_a, **_k: closed_docs.append(closed)
    )
    monkeypatch.setattr(tu, "close_draw_family_doc", _fail_draw_family)
    monkeypatch.setattr(tu, "settle_after_draw_family_close", _fail_settle)
    monkeypatch.setattr(
        tu, "_log_office_health_after_close", lambda *_a, **_k: health_called.append("health")
    )
    try:
        with TestingFactory.native_doc(object(), "draw") as got:
            assert got is doc
        assert closed_docs == [doc]
        assert health_called == ["health"]
    finally:
        tu._clear_windows_math_ole_uids()
        tu._set_windows_leftover_open(saved)


def test_native_doc_teardown_reset_failure_closes_without_session_clear(monkeypatch):
    """Failed pooled reset closes the doc and skips session clear (no return in finally)."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    doc = MagicMock(name="pooled_doc")
    closed_docs = []

    def reset_fail(*_a, **_k):
        raise RuntimeError("reset failed")

    def fail_clear():
        raise AssertionError("must not clear session on reset failure")

    monkeypatch.setattr(tu, "reset_native_doc", reset_fail)
    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda *_a, **_k: doc)
    monkeypatch.setattr(
        TestingFactory, "close_doc", lambda closed, *_a, **_k: closed_docs.append(closed)
    )
    monkeypatch.setattr(
        "plugin.calc.python.function.clear_python_addin_cache",
        fail_clear,
    )
    tu._NATIVE_DOC_POOL.clear()
    ctx = object()
    try:
        with TestingFactory.native_doc(ctx, "calc", reuse=True) as got:
            assert got is doc
        assert closed_docs == [doc]
    finally:
        tu._NATIVE_DOC_POOL.clear()


def test_create_native_doc_posix_does_not_prepare_writer_factory(monkeypatch):
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = MagicMock()
    calls = []
    monkeypatch.setattr(tu.sys, "platform", "linux")
    monkeypatch.setattr(
        tu, "prepare_windows_writer_factory", lambda _ctx: calls.append("prepare")
    )
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        patch("uno.createUnoStruct", return_value=MagicMock()),
        patch("plugin.testing_runner.probe_uno_bridge", return_value="alive"),
    ):
        TestingFactory.create_native_doc(object(), "writer")
    assert calls == []


def test_create_native_doc_skips_load_when_bridge_already_dead(monkeypatch):
    from unittest.mock import MagicMock, patch

    import plugin.testing_runner as tr
    from plugin.tests.testing_utils import TestingFactory

    tr.current_office().reset_lifecycle_breadcrumb()
    tr.record_test_end("draw.test_draw_uno.test_duplicate_slide_copies_shapes", "OK")
    tr.record_test_start("draw.test_draw_uno.test_duplicate_rename_move_slide")

    class _DeadCtx:
        def getServiceManager(self) -> None:
            raise RuntimeError("Binary URP bridge disposed during call")

    desktop = MagicMock()
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        patch("uno.createUnoStruct", return_value=MagicMock()),
        pytest.raises(RuntimeError, match="pre_open=disposed") as caught,
    ):
        TestingFactory.create_native_doc(_DeadCtx(), "draw")
    assert "already disposed before loadComponentFromURL" in str(caught.value)
    assert "previous=draw.test_draw_uno.test_duplicate_slide_copies_shapes" in str(caught.value)
    desktop.loadComponentFromURL.assert_not_called()
    tr.current_office().reset_lifecycle_breadcrumb()


def test_create_native_doc_wraps_disposed_exception(monkeypatch):
    from unittest.mock import MagicMock, patch

    import plugin.testing_runner as tr
    from plugin.tests.testing_utils import TestingFactory

    tr.current_office().reset_lifecycle_breadcrumb()
    tr.record_test_end("draw.test_draw_uno.test_get_draw_tree", "OK")
    tr.record_test_start("draw.test_draw_uno.test_insert_math_draw")

    desktop = MagicMock()
    desktop.loadComponentFromURL.side_effect = RuntimeError("Binary URP bridge disposed during call")
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        patch("uno.createUnoStruct", return_value=MagicMock()),
        pytest.raises(RuntimeError, match="previous=draw.test_draw_uno.test_get_draw_tree"),
    ):
        TestingFactory.create_native_doc(object(), "draw")
    tr.current_office().reset_lifecycle_breadcrumb()


def test_close_doc_windows_untyped_mock_still_logs_dispose(capsys, monkeypatch):
    """GHA 34609539461: leftover=1 + MagicMock Writer skip hid the dispose log."""
    from unittest.mock import MagicMock

    import plugin.testing_runner as tr
    from plugin.tests import testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr(tr, "_soffice_pids", lambda: "8")
    monkeypatch.setattr("gc.collect", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    saved = tu._windows_leftover_open()
    tu._set_windows_leftover_open(1)
    doc = MagicMock()
    doc.close.side_effect = RuntimeError("Binary URP bridge disposed during call")
    try:
        TestingFactory.close_doc(doc)
        err = capsys.readouterr().err
        assert "LIFECYCLE close_doc dispose" in err
        assert "skip writer close" not in err
    finally:
        tu._set_windows_leftover_open(saved)


def test_native_doc_svc_magicmock_is_not_writer():
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import _native_doc_svc

    assert _native_doc_svc(MagicMock()) == ""


def test_close_doc_windows_writer_reactivates_keeper(monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests import testing_utils as tu
    from plugin.tests.testing_utils import TestingFactory

    keeper = MagicMock(name="keeper")
    frame = MagicMock(name="keeper_frame")
    desktop = MagicMock(name="desktop")
    keeper.getCurrentController.return_value.getFrame.return_value = frame
    frame.getCreator.return_value = desktop
    monkeypatch.setattr(tu.sys, "platform", "win32")
    monkeypatch.setattr("gc.collect", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    tu.set_harness_keeper_uid("1", keeper)
    saved_leftover = tu._windows_leftover_open()
    tu._set_windows_leftover_open(0)
    doc = MagicMock()
    doc.RuntimeUID = "99"
    doc.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    try:
        TestingFactory.close_doc(doc)
        doc.close.assert_called_once_with(True)
        desktop.setActiveFrame.assert_called_once_with(frame)
    finally:
        tu._set_windows_leftover_open(saved_leftover)
        tu.set_harness_keeper_uid("")


def test_close_draw_family_doc_windows_raw_close_skips_pre_close_gc(monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import close_draw_family_doc

    order = []

    def _fail_gc():
        raise AssertionError("Windows raw close must not gc.collect before close")

    def _fail_sleep(_seconds):
        raise AssertionError("Windows raw close must not sleep before close")

    monkeypatch.setattr("gc.collect", _fail_gc)
    monkeypatch.setattr("time.sleep", _fail_sleep)
    monkeypatch.setattr(
        "plugin.tests.testing_utils._draw_family_raw_close",
        lambda: True,
    )
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc.endswith("PresentationDocument")
    doc.RuntimeUID = "impress-uid"
    doc.setModified.side_effect = lambda _modified: order.append("setModified")
    doc.close.side_effect = lambda _save: order.append("close")
    close_draw_family_doc(doc)
    assert order == ["close"]
    doc.setModified.assert_not_called()
    doc.close.assert_called_once_with(True)
    doc.dispose.assert_not_called()


def test_close_draw_family_doc_none_skips_settle(monkeypatch):
    from plugin.tests.testing_utils import close_draw_family_doc

    def _fail_sleep(_seconds):
        raise AssertionError("close_draw_family_doc(None) must not sleep")

    monkeypatch.setattr("time.sleep", _fail_sleep)
    close_draw_family_doc(None)


def test_testing_factory_execute_tool_unknown_name():
    from unittest.mock import MagicMock, patch

    doc = CalcDocStub()
    ctx = MockContext()
    fake_tools = MagicMock()
    fake_tools.execute.side_effect = KeyError("bad_tool")
    with (
        patch("plugin.app.get_tools", return_value=fake_tools),
        patch("plugin.app.get_services", return_value={}),
    ):
        res = TestingFactory.execute_tool(doc, ctx, "bad_tool", {}, doc_type="calc")
    assert res["status"] == "error"
    assert "bad_tool" in res["error"]


def test_default_native_doc_reuse():
    import plugin.tests.testing_utils as tu

    assert tu._default_native_doc_reuse("calc") is True
    assert tu._default_native_doc_reuse("writer") is True
    assert tu._default_native_doc_reuse("draw") is False
    assert tu._default_native_doc_reuse("impress") is False


def test_native_doc_pool_clean_tracking():
    from plugin.tests.testing_utils import _NativeDocPool

    pool = _NativeDocPool()
    doc1 = object()
    doc2 = object()

    assert pool.is_clean(doc1) is False
    pool.mark_clean(doc1, True)
    assert pool.is_clean(doc1) is True
    assert pool.is_clean(doc2) is False

    pool.mark_clean(doc1, False)
    assert pool.is_clean(doc1) is False

    pool["key1"] = doc1
    pool.mark_clean(doc1, True)
    pool.clear()
    assert len(pool) == 0
    assert pool.is_clean(doc1) is False


def _uno_struct__setattr__(self: object, name: str, value: object) -> None:
    if name == "__traceback__":
        raise RuntimeError("Couldn't convert <traceback object> to a UNO type")
    object.__setattr__(self, name, value)


class _UnoTracebackTrap(Exception):
    __setattr__ = _uno_struct__setattr__  # type: ignore[assignment]


def test_native_doc_wraps_uno_setattr_exception(monkeypatch):
    from unittest.mock import MagicMock

    sentinel = object()
    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda ctx, **kwargs: sentinel)
    mock_close = MagicMock(return_value=None)
    monkeypatch.setattr(TestingFactory, "close_doc", mock_close)

    def _no_desktop(ctx):
        raise RuntimeError("no office")

    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", _no_desktop)

    trap = _UnoTracebackTrap("Binary URP bridge disposed during call")
    wrapped = _exception_leaving_native_doc(trap)
    assert type(wrapped) is RuntimeError
    assert wrapped.__cause__ is trap

    ctx = MockContext()
    with pytest.raises(RuntimeError) as ei:
        with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False):
            raise trap

    assert "Binary URP bridge disposed during call" in str(ei.value)
    assert "Couldn't convert" not in str(ei.value)
    assert ei.value.__cause__ is trap
    assert type(ei.value) is RuntimeError


def test_native_doc_reraises_plain_exception_unchanged(monkeypatch):
    from unittest.mock import MagicMock

    sentinel = object()
    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda ctx, **kwargs: sentinel)
    mock_close = MagicMock(return_value=None)
    monkeypatch.setattr(TestingFactory, "close_doc", mock_close)

    def _no_desktop(ctx):
        raise RuntimeError("no office")

    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", _no_desktop)

    ctx = MockContext()
    with pytest.raises(ValueError) as ei:
        with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False):
            raise ValueError("plain")

    assert str(ei.value) == "plain"
    assert type(ei.value).__name__ != "RuntimeError"


def test_native_doc_yields_doc_when_body_succeeds(monkeypatch):
    from unittest.mock import MagicMock

    sentinel = object()
    monkeypatch.setattr(TestingFactory, "create_native_doc", lambda ctx, **kwargs: sentinel)
    mock_close = MagicMock(return_value=None)
    monkeypatch.setattr(TestingFactory, "close_doc", mock_close)

    def _no_desktop(ctx):
        raise RuntimeError("no office")

    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", _no_desktop)

    ctx = MockContext()
    with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False) as doc:
        assert doc is sentinel

    mock_close.assert_called_once_with(sentinel)

