# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared pytest stubs in testing_utils."""

import pytest

from plugin.framework.tool import ToolContext
from plugin.tests.testing_utils import CalcDocStub, MockContext, TestingFactory, WriterDocStub


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


def test_with_native_doc_logs_teardown_for_insert_cell_html(capsys):
    """GHA 33703959362: no TEST end after execute-done — name body vs teardown."""
    from unittest.mock import patch

    from plugin.tests.testing_utils import TestingFactory, with_native_doc

    @with_native_doc("calc")
    def test_insert_cell_html(ctx, doc):
        return "ok"

    with patch.object(TestingFactory, "native_doc") as mock_cm:
        mock_cm.return_value.__enter__.return_value = object()
        mock_cm.return_value.__exit__.return_value = None
        assert test_insert_cell_html(ctx=object()) == "ok"
    from plugin.tests import testing_utils as tu

    err = capsys.readouterr().err
    assert "with_native_doc: enter name=test_insert_cell_html doc_type=calc" in err
    assert "with_native_doc: body returned name=test_insert_cell_html; teardown start" in err
    assert "with_native_doc: teardown done name=test_insert_cell_html" in err
    assert tu._LOG_NATIVE_DOC_TEARDOWN is False


def test_with_native_doc_skips_teardown_log_for_other_tests(capsys):
    from unittest.mock import patch

    from plugin.tests.testing_utils import TestingFactory, with_native_doc

    @with_native_doc("calc")
    def test_other(ctx, doc):
        return "ok"

    with patch.object(TestingFactory, "native_doc") as mock_cm:
        mock_cm.return_value.__enter__.return_value = object()
        mock_cm.return_value.__exit__.return_value = None
        assert test_other(ctx=object()) == "ok"
    err = capsys.readouterr().err
    assert "with_native_doc:" not in err


def _calc_doc_for_reset():
    from unittest.mock import MagicMock

    empty = MagicMock()
    empty.getElementNames.return_value = ()
    sheets = MagicMock()
    sheets.getCount.return_value = 1
    sheet = MagicMock()
    sheet.Name = "Sheet1"
    sheet.getCharts.return_value = empty
    sheet.NamedRanges = None
    sheets.getByIndex.return_value = sheet
    doc = MagicMock()
    doc.getSheets.return_value = sheets
    doc.getEmbeddedObjects.return_value = empty
    doc.NamedRanges = None
    doc.DatabaseRanges = None
    return doc


def test_reset_calc_doc_logs_when_teardown_flag_set(capsys):
    from unittest.mock import MagicMock

    from plugin.tests import testing_utils as tu

    tu._LOG_NATIVE_DOC_TEARDOWN = True
    try:
        tu._reset_calc_doc(_calc_doc_for_reset(), MagicMock())
    finally:
        tu._LOG_NATIVE_DOC_TEARDOWN = False
    err = capsys.readouterr().err
    assert "native_doc: _reset_calc_doc start" in err
    assert "native_doc: _reset_calc_doc clearContents start" in err
    assert "native_doc: _reset_calc_doc clearContents done" in err
    assert "udprops probe: RuntimeUID start" in err
    assert "udprops clear: DOCUMENT_SCRIPTS start" in err
    assert "native_doc: _reset_calc_doc done" in err


def test_reset_calc_doc_silent_by_default(capsys):
    from unittest.mock import MagicMock

    from plugin.tests import testing_utils as tu

    tu._reset_calc_doc(_calc_doc_for_reset(), MagicMock())
    err = capsys.readouterr().err
    assert "native_doc:" not in err


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
    tr.reset_lifecycle_breadcrumb()
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
    tr.reset_lifecycle_breadcrumb()


def test_reraise_native_open_failure_passthrough_non_urp():
    from plugin.tests.testing_utils import _reraise_native_open_failure

    with pytest.raises(ValueError, match="not a bridge"):
        try:
            raise ValueError("not a bridge")
        except ValueError as exc:
            _reraise_native_open_failure(exc, "private:factory/sdraw")


def test_prepare_windows_writer_factory_logs_leftovers_and_does_not_close(monkeypatch):
    """GHA 34556185752: leftover close(True) hung 30s. Log + reactivate only."""
    from unittest.mock import MagicMock

    from plugin.tests import testing_utils as tu

    keeper = MagicMock(name="keeper")
    keeper.RuntimeUID = "1"
    leftover = MagicMock(name="leftover")
    leftover.RuntimeUID = "26"
    leftover.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    keeper.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    frame = MagicMock(name="keeper_frame")
    keeper.getCurrentController.return_value.getFrame.return_value = frame

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    desktop = MagicMock()
    desktop.getComponents.return_value.createEnumeration.return_value = _Enum(
        [keeper, leftover]
    )
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda _ctx: desktop)
    tu.set_harness_keeper_uid("1", keeper)
    try:
        assert tu.prepare_windows_writer_factory(object()) == 1
        leftover.close.assert_not_called()
        keeper.close.assert_not_called()
        desktop.setActiveFrame.assert_called_once_with(frame)
    finally:
        tu.set_harness_keeper_uid("")


def test_prepare_windows_writer_factory_keeper_only_still_reactivates(monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests import testing_utils as tu

    keeper = MagicMock(name="keeper")
    keeper.RuntimeUID = "1"
    keeper.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    frame = MagicMock(name="keeper_frame")
    keeper.getCurrentController.return_value.getFrame.return_value = frame

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    desktop = MagicMock()
    desktop.getComponents.return_value.createEnumeration.return_value = _Enum([keeper])
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda _ctx: desktop)
    tu.set_harness_keeper_uid("1", keeper)
    try:
        assert tu.prepare_windows_writer_factory(object()) == 0
        keeper.close.assert_not_called()
        desktop.setActiveFrame.assert_called_once_with(frame)
    finally:
        tu.set_harness_keeper_uid("")


def test_set_harness_keeper_uid_writes_sibling_testing_utils_module(monkeypatch):
    """GHA 34595675515: runner set tests.testing_utils; factory read plugin.tests copy."""
    import sys
    import types

    from plugin.tests import testing_utils as tu

    sibling = types.ModuleType("tests.testing_utils")
    sibling.__file__ = tu.__file__
    sibling._HARNESS_KEEPER_UID = ""
    sibling._HARNESS_KEEPER_DOC = None
    monkeypatch.setitem(sys.modules, "tests.testing_utils", sibling)
    keeper_doc = object()
    try:
        tu.set_harness_keeper_uid("1", keeper_doc)
        assert tu._HARNESS_KEEPER_UID == "1"
        assert sibling._HARNESS_KEEPER_UID == "1"
        assert sibling._HARNESS_KEEPER_DOC is keeper_doc
    finally:
        tu.set_harness_keeper_uid("")


def test_prepare_windows_writer_factory_adopts_keeper_from_sibling(monkeypatch):
    """Same dual-module miss: prepare saw keeper=- and counted uid=1 as leftover."""
    import sys
    import types
    from unittest.mock import MagicMock

    from plugin.tests import testing_utils as tu

    sibling = types.ModuleType("tests.testing_utils")
    sibling.__file__ = tu.__file__
    keeper = MagicMock(name="keeper")
    keeper.RuntimeUID = "1"
    keeper.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    frame = MagicMock(name="keeper_frame")
    keeper.getCurrentController.return_value.getFrame.return_value = frame
    leftover = MagicMock(name="leftover")
    leftover.RuntimeUID = "26"
    leftover.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    sibling._HARNESS_KEEPER_UID = "1"
    sibling._HARNESS_KEEPER_DOC = keeper

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    desktop = MagicMock()
    desktop.getComponents.return_value.createEnumeration.return_value = _Enum(
        [keeper, leftover]
    )
    monkeypatch.setitem(sys.modules, "tests.testing_utils", sibling)
    monkeypatch.setattr("plugin.framework.uno_context.get_desktop", lambda _ctx: desktop)
    tu._HARNESS_KEEPER_UID = ""
    tu._HARNESS_KEEPER_DOC = None
    try:
        assert tu.prepare_windows_writer_factory(object()) == 1
        leftover.close.assert_not_called()
        desktop.setActiveFrame.assert_called_once_with(frame)
        assert tu._HARNESS_KEEPER_UID == "1"
    finally:
        tu.set_harness_keeper_uid("")


def test_windows_factory_load_args_named_for_any_leftover_factory():
    """GHA 34597506651 / 34599838644: leftover _blank hangs Writer and Calc."""
    import plugin.tests.testing_utils as tu

    saved = tu._WINDOWS_FACTORY_SEQ
    tu._WINDOWS_FACTORY_SEQ = 0
    try:
        assert tu._windows_factory_load_args("private:factory/swriter", 0) == ("_blank", 0)
        assert tu._windows_factory_load_args("private:factory/scalc", 0) == ("_blank", 0)
        assert tu._windows_factory_load_args("private:factory/scalc", 2) == (
            "_wa_factory_1",
            8 | 55,
        )
        assert tu._windows_factory_load_args("private:factory/sdraw", 2) == (
            "_wa_factory_2",
            8 | 55,
        )
        assert tu._windows_factory_load_args("private:factory/swriter", 2) == (
            "_wa_factory_3",
            8 | 55,
        )
        assert tu._windows_factory_load_args("private:factory/swriter", 2) == (
            "_wa_factory_4",
            8 | 55,
        )
    finally:
        tu._WINDOWS_FACTORY_SEQ = saved


def test_create_native_doc_windows_prepares_writer_factory_before_load(monkeypatch):
    """Second text_helpers Writer factory hung after leftovers + one close_doc.

    GHA 34597506651: keeper reactivate printed, first swriter _blank returned,
    the next _blank hung 30s. Named CREATE|GLOBAL target matches HTML paste
    (rich_html._wa_calc_html) and must increment so two consecutive factories
    do not share a frame name.
    """
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = MagicMock()
    calls = []
    saved_seq = tu._WINDOWS_FACTORY_SEQ
    tu._WINDOWS_FACTORY_SEQ = 0
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
            TestingFactory.create_native_doc(object(), "writer")
            assert calls == ["prepare"]
            args = desktop.loadComponentFromURL.call_args.args
            assert args[0] == "private:factory/swriter"
            assert args[1] == "_wa_factory_1"
            assert args[2] == (8 | 55)
            TestingFactory.create_native_doc(object(), "writer")
            assert calls == ["prepare", "prepare"]
            args = desktop.loadComponentFromURL.call_args.args
            assert args[1] == "_wa_factory_2"
            assert args[2] == (8 | 55)
    finally:
        tu._WINDOWS_FACTORY_SEQ = saved_seq


def test_create_native_doc_windows_calc_prepares_factory(monkeypatch):
    """GHA 34599838644: leftover scalc _blank failed then hung; named CREATE target."""
    from unittest.mock import MagicMock, patch

    from plugin.tests.testing_utils import TestingFactory
    import plugin.tests.testing_utils as tu

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = MagicMock()
    calls = []
    saved_seq = tu._WINDOWS_FACTORY_SEQ
    tu._WINDOWS_FACTORY_SEQ = 0
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
            TestingFactory.create_native_doc(object(), "calc")
            assert calls == ["prepare"]
            args = desktop.loadComponentFromURL.call_args.args
            assert args[0] == "private:factory/scalc"
            assert args[1] == "_wa_factory_1"
            assert args[2] == (8 | 55)
            # Same hang family as leftover consecutive Hidden _blank swriter:
            # the next leftover scalc must not reuse the frame name.
            TestingFactory.create_native_doc(object(), "calc")
            assert calls == ["prepare", "prepare"]
            args = desktop.loadComponentFromURL.call_args.args
            assert args[0] == "private:factory/scalc"
            assert args[1] == "_wa_factory_2"
            assert args[2] == (8 | 55)
    finally:
        tu._WINDOWS_FACTORY_SEQ = saved_seq


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

    tr.reset_lifecycle_breadcrumb()
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
    tr.reset_lifecycle_breadcrumb()


def test_create_native_doc_wraps_disposed_exception(monkeypatch):
    from unittest.mock import MagicMock, patch

    import plugin.testing_runner as tr
    from plugin.tests.testing_utils import TestingFactory

    tr.reset_lifecycle_breadcrumb()
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
    tr.reset_lifecycle_breadcrumb()


def test_log_close_doc_failure_and_office_health(capsys, monkeypatch):
    from unittest.mock import MagicMock, patch

    import plugin.testing_runner as tr
    from plugin.tests.testing_utils import _log_close_doc_failure, _log_office_health_after_close

    monkeypatch.setattr(tr, "_soffice_pids", lambda: "-")
    tr.reset_lifecycle_breadcrumb()
    tr.record_test_end("draw.test_draw_uno.test_get_draw_tree", "OK")

    _log_close_doc_failure(RuntimeError("Binary URP bridge disposed during call"))
    err = capsys.readouterr().err
    assert "LIFECYCLE close_doc dispose" in err
    assert "previous=draw.test_draw_uno.test_get_draw_tree" in err

    desktop = MagicMock()
    desktop.getComponents.side_effect = RuntimeError("Binary URP bridge disposed during call")
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        _log_office_health_after_close(object(), "draw")
    err = capsys.readouterr().err
    assert "LIFECYCLE office dead after close doc_type=draw" in err
    assert "previous=draw.test_draw_uno.test_get_draw_tree" in err
    tr.reset_lifecycle_breadcrumb()


def test_close_doc_logs_urp_dispose(capsys, monkeypatch):
    from unittest.mock import MagicMock

    import plugin.testing_runner as tr
    from plugin.tests.testing_utils import TestingFactory

    monkeypatch.setattr(tr, "_soffice_pids", lambda: "8")
    tr.reset_lifecycle_breadcrumb()
    tr.record_test_start("draw.test_draw_uno.test_get_draw_tree")
    doc = MagicMock()
    doc.close.side_effect = RuntimeError("Binary URP bridge disposed during call")
    TestingFactory.close_doc(doc)
    err = capsys.readouterr().err
    assert "LIFECYCLE close_doc dispose" in err
    tr.reset_lifecycle_breadcrumb()


def test_close_doc_windows_writer_reactivates_keeper(monkeypatch):
    """GHA 34554275072: after test Writer close, keep leftover off current."""
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
    doc = MagicMock()
    doc.RuntimeUID = "99"
    doc.supportsService.side_effect = lambda svc: svc.endswith("TextDocument")
    try:
        TestingFactory.close_doc(doc)
        doc.close.assert_called_once_with(True)
        desktop.setActiveFrame.assert_called_once_with(frame)
    finally:
        tu.set_harness_keeper_uid("")


def test_close_doc_settles_urp_before_close(monkeypatch):
    """GC then settle must run before close so URP can finish ~SvxShape."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import TestingFactory, _CLOSE_DOC_URP_SETTLE_S

    order = []
    monkeypatch.setattr("gc.collect", lambda: order.append("gc"))
    monkeypatch.setattr("time.sleep", lambda seconds: order.append(("sleep", seconds)))
    doc = MagicMock()
    doc.close.side_effect = lambda _save: order.append("close")
    TestingFactory.close_doc(doc)
    assert order == ["gc", ("sleep", _CLOSE_DOC_URP_SETTLE_S), "close"]
    doc.close.assert_called_once_with(True)


def test_close_doc_none_skips_settle(monkeypatch):
    from plugin.tests.testing_utils import TestingFactory

    def _fail_sleep(_seconds):
        raise AssertionError("close_doc(None) must not sleep")

    monkeypatch.setattr("time.sleep", _fail_sleep)
    TestingFactory.close_doc(None)


def test_settle_after_draw_family_close_gcs_then_sleeps(monkeypatch):
    """Post-Impress settle must GC after the caller drops the closed proxy."""
    from plugin.tests.testing_utils import (
        _DRAW_FAMILY_POST_CLOSE_SETTLE_S,
        settle_after_draw_family_close,
    )

    order = []
    monkeypatch.setattr("gc.collect", lambda: order.append("gc"))
    monkeypatch.setattr("time.sleep", lambda seconds: order.append(("sleep", seconds)))
    settle_after_draw_family_close()
    assert order == ["gc", ("sleep", _DRAW_FAMILY_POST_CLOSE_SETTLE_S)]


def test_settle_after_draw_family_close_windows_longer_than_posix():
    """Windows is the hang host (GHA 34419828920); POSIX stays a short drain."""
    from plugin.tests import testing_utils as tu

    assert tu._DRAW_FAMILY_POST_CLOSE_SETTLE_S > tu._CLOSE_DOC_URP_SETTLE_S
    assert tu._DRAW_FAMILY_PRE_CLOSE_SETTLE_S == tu._DRAW_FAMILY_POST_CLOSE_SETTLE_S
    if tu.sys.platform == "win32":
        assert tu._DRAW_FAMILY_POST_CLOSE_SETTLE_S == 0.75
    else:
        assert tu._DRAW_FAMILY_POST_CLOSE_SETTLE_S == 0.15


def test_close_draw_family_doc_setmodified_gc_settle_then_close(monkeypatch):
    """POSIX Impress close uses close(True), never close_doc (GHA 34518091151)."""
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import (
        TestingFactory,
        _DRAW_FAMILY_PRE_CLOSE_SETTLE_S,
        close_draw_family_doc,
    )

    order = []
    monkeypatch.setattr("gc.collect", lambda: order.append("gc"))
    monkeypatch.setattr("time.sleep", lambda seconds: order.append(("sleep", seconds)))
    monkeypatch.setattr(
        "plugin.tests.testing_utils._draw_family_raw_close",
        lambda: False,
    )

    def _fail_close_doc(_doc):
        raise AssertionError("close_draw_family_doc must not call close_doc")

    monkeypatch.setattr(TestingFactory, "close_doc", _fail_close_doc)
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc.endswith("PresentationDocument")
    doc.RuntimeUID = "impress-uid"
    doc.setModified.side_effect = lambda _modified: order.append("setModified")
    doc.close.side_effect = lambda _save: order.append("close")
    close_draw_family_doc(doc)
    assert order == [
        "setModified",
        "gc",
        ("sleep", _DRAW_FAMILY_PRE_CLOSE_SETTLE_S),
        "close",
    ]
    doc.setModified.assert_called_once_with(False)
    doc.close.assert_called_once_with(True)
    doc.dispose.assert_not_called()


def test_close_draw_family_doc_windows_raw_close_skips_pre_close_gc(monkeypatch):
    """GHA 34537826720: skip left Impress alive; #710 raw close returned."""
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


def test_close_draw_family_doc_logs_svc_and_steps(capsys, monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import close_draw_family_doc

    monkeypatch.setattr("gc.collect", lambda: None)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "plugin.tests.testing_utils._draw_family_raw_close",
        lambda: False,
    )
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc.endswith("PresentationDocument")
    doc.RuntimeUID = "uid-9"
    close_draw_family_doc(doc)
    err = capsys.readouterr().err
    assert "close_draw_family: start svc=impress uid=uid-9" in err
    assert "close_draw_family: setModified(False) ok" in err
    assert "close_draw_family: close(True) start svc=impress uid=uid-9" in err
    assert "close_draw_family: close(True) done svc=impress uid=uid-9" in err


def test_close_draw_family_doc_windows_logs_raw_close(capsys, monkeypatch):
    from unittest.mock import MagicMock

    from plugin.tests.testing_utils import close_draw_family_doc

    monkeypatch.setattr(
        "plugin.tests.testing_utils._draw_family_raw_close",
        lambda: True,
    )
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc.endswith("PresentationDocument")
    doc.RuntimeUID = "uid-9"
    close_draw_family_doc(doc)
    err = capsys.readouterr().err
    assert "close_draw_family: start svc=impress uid=uid-9" in err
    assert "close_draw_family: raw close(True) start svc=impress uid=uid-9" in err
    assert "close_draw_family: raw close(True) done svc=impress uid=uid-9" in err
    assert "skip uno teardown" not in err
    assert "setModified" not in err
    doc.close.assert_called_once_with(True)


def test_teardown_peer_pair_closes_writer_before_impress(monkeypatch):
    """POSIX: Writer first, then Impress (GHA 34518091151)."""
    from unittest.mock import MagicMock

    from tests.chatbot.test_peer_message_uno import _teardown_peer_pair

    order = []
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._draw_family_raw_close",
        lambda: False,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._close",
        lambda doc: order.append(("close_doc", doc)) or None,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.close_draw_family_doc",
        lambda doc: order.append(("close_draw_family", doc)),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.settle_after_draw_family_close",
        lambda: order.append("post_settle"),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._reactivate_writer_after_impress",
        lambda ctx, doc: order.append(("reactivate", ctx, doc)),
    )
    writer = MagicMock(name="writer")
    impress = MagicMock(name="impress")
    out_writer, out_impress = _teardown_peer_pair(writer, impress)
    assert out_writer is None and out_impress is None
    assert order == [
        ("close_doc", writer),
        ("close_draw_family", impress),
        "post_settle",
    ]


def test_teardown_peer_pair_windows_closes_impress_skips_writer(monkeypatch):
    """GHA 34540353452: Impress raw close returned; Writer close_doc hung."""
    from unittest.mock import MagicMock

    import tests.chatbot.test_peer_message_uno as peer
    from tests.chatbot.test_peer_message_uno import _teardown_peer_pair

    peer._windows_impress_raw_closed = False
    order = []
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._draw_family_raw_close",
        lambda: True,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._close",
        lambda doc: order.append(("close_doc", doc)) or None,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.close_draw_family_doc",
        lambda doc: order.append(("close_draw_family", doc)),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.settle_after_draw_family_close",
        lambda: order.append("post_settle"),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._reactivate_writer_after_impress",
        lambda ctx, doc: order.append(("reactivate", ctx, doc)),
    )
    writer = MagicMock(name="writer")
    impress = MagicMock(name="impress")
    ctx = MagicMock(name="ctx")
    recycle_calls = []
    monkeypatch.setattr(
        "plugin.testing_runner.request_office_recycle_after_suite",
        lambda: recycle_calls.append("recycle"),
    )
    try:
        out_writer, out_impress = _teardown_peer_pair(writer, impress, ctx)
        assert out_writer is None and out_impress is None
        assert order == [
            ("close_draw_family", impress),
            "post_settle",
            ("reactivate", ctx, writer),
        ]
        assert recycle_calls == ["recycle"]
        assert peer._windows_impress_raw_closed is True
    finally:
        peer._windows_impress_raw_closed = False


def test_teardown_peer_pair_windows_skips_second_impress_close(capsys, monkeypatch):
    """GHA 34547869791: second Impress raw close exited soffice 0."""
    from unittest.mock import MagicMock

    import tests.chatbot.test_peer_message_uno as peer
    from tests.chatbot.test_peer_message_uno import _teardown_peer_pair

    peer._windows_impress_raw_closed = False
    order = []
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._draw_family_raw_close",
        lambda: True,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._close",
        lambda doc: order.append(("close_doc", doc)) or None,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.close_draw_family_doc",
        lambda doc: order.append(("close_draw_family", doc)),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.settle_after_draw_family_close",
        lambda: order.append("post_settle"),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._reactivate_writer_after_impress",
        lambda ctx, doc: order.append(("reactivate", ctx, doc)),
    )
    recycle_calls = []
    monkeypatch.setattr(
        "plugin.testing_runner.request_office_recycle_after_suite",
        lambda: recycle_calls.append("recycle"),
    )
    writer1 = MagicMock(name="writer1")
    impress1 = MagicMock(name="impress1")
    writer2 = MagicMock(name="writer2")
    impress2 = MagicMock(name="impress2")
    ctx = MagicMock(name="ctx")
    try:
        _teardown_peer_pair(writer1, impress1, ctx)
        _teardown_peer_pair(writer2, impress2, ctx)
        assert order == [
            ("close_draw_family", impress1),
            "post_settle",
            ("reactivate", ctx, writer1),
        ]
        assert recycle_calls == ["recycle", "recycle"]
        err = capsys.readouterr().err
        assert "peer_message_uno: skip second impress close (windows)" in err
    finally:
        peer._windows_impress_raw_closed = False


def test_reactivate_writer_after_impress_sets_active_frame(monkeypatch):
    from unittest.mock import MagicMock, patch

    from tests.chatbot.test_peer_message_uno import _reactivate_writer_after_impress

    frame = MagicMock()
    writer = MagicMock()
    writer.getCurrentController.return_value.getFrame.return_value = frame
    desktop = MagicMock()
    ctx = MagicMock()
    with patch(
        "tests.chatbot.test_peer_message_uno.get_desktop",
        return_value=desktop,
    ) as get_desktop:
        _reactivate_writer_after_impress(ctx, writer)
    get_desktop.assert_called_once_with(ctx)
    desktop.setActiveFrame.assert_called_once_with(frame)
    frame.getContainerWindow.return_value.toFront.assert_called_once_with()


def test_windows_skip_doc_close_follows_platform(monkeypatch):
    import tests.chatbot.test_peer_message_uno as peer

    monkeypatch.setattr(peer.sys, "platform", "win32")
    assert peer._windows_skip_doc_close() is True
    monkeypatch.setattr(peer.sys, "platform", "linux")
    assert peer._windows_skip_doc_close() is False


def test_close_skips_on_windows(capsys, monkeypatch):
    """GHA 34544965319: second Writer close_doc hung before any Impress."""
    from unittest.mock import MagicMock

    import tests.chatbot.test_peer_message_uno as peer
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock()
    doc.RuntimeUID = "uid-later"
    calls = []
    recycle_calls = []
    monkeypatch.setattr(peer, "_windows_skip_doc_close", lambda: True)
    monkeypatch.setattr(
        TestingFactory, "close_doc", lambda _doc: calls.append("close_doc")
    )
    monkeypatch.setattr(
        "plugin.testing_runner.request_office_recycle_after_suite",
        lambda: recycle_calls.append("recycle"),
    )
    assert peer._close(doc) is None
    assert calls == []
    assert recycle_calls == ["recycle"]
    err = capsys.readouterr().err
    assert "peer_message_uno: skip close (windows) uid=uid-later" in err


def test_close_logs_uid_before_close_doc(capsys, monkeypatch):
    from unittest.mock import MagicMock

    import tests.chatbot.test_peer_message_uno as peer
    from plugin.tests.testing_utils import TestingFactory

    doc = MagicMock()
    doc.RuntimeUID = "uid-7"
    monkeypatch.setattr(peer, "_windows_skip_doc_close", lambda: False)
    monkeypatch.setattr(TestingFactory, "close_doc", lambda _doc: None)
    peer._close(doc)
    err = capsys.readouterr().err
    assert "peer_message_uno: close_doc start uid=uid-7" in err
    assert "peer_message_uno: close_doc done uid=uid-7" in err


def test_teardown_peer_pair_windows_no_impress_just_closes_writer(monkeypatch):
    from unittest.mock import MagicMock

    from tests.chatbot.test_peer_message_uno import _teardown_peer_pair

    order = []
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._draw_family_raw_close",
        lambda: True,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno._close",
        lambda doc: order.append(("close_doc", doc)) or None,
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.close_draw_family_doc",
        lambda doc: order.append(("close_draw_family", doc)),
    )
    monkeypatch.setattr(
        "tests.chatbot.test_peer_message_uno.settle_after_draw_family_close",
        lambda: order.append("post_settle"),
    )
    writer = MagicMock(name="writer")
    out_writer, out_impress = _teardown_peer_pair(writer, None, MagicMock())
    assert out_writer is None and out_impress is None
    assert order == [("close_doc", writer)]


def test_reactivate_writer_after_impress_skips_when_missing():
    from unittest.mock import MagicMock, patch

    from tests.chatbot.test_peer_message_uno import _reactivate_writer_after_impress

    with patch("tests.chatbot.test_peer_message_uno.get_desktop") as get_desktop:
        _reactivate_writer_after_impress(None, MagicMock())
        _reactivate_writer_after_impress(MagicMock(), None)
    get_desktop.assert_not_called()


def test_testing_factory_execute_tool_unknown_name():
    from unittest.mock import MagicMock, patch

    doc = CalcDocStub()
    ctx = MockContext()
    fake_tools = MagicMock()
    fake_tools.execute.side_effect = KeyError("bad_tool")
    with (
        patch("plugin.main.get_tools", return_value=fake_tools),
        patch("plugin.main.get_services", return_value={}),
    ):
        res = TestingFactory.execute_tool(doc, ctx, "bad_tool", {}, doc_type="calc")
    assert res["status"] == "error"
    assert "bad_tool" in res["error"]
