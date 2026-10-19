# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Excel PY auto-convert on open (no menu)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.calc.excel_py_convert.apply_calc import apply_dag_formulas_to_calc_doc
from plugin.calc.excel_py_convert.auto_open import (
    _CONVERTED_PROP,
    _doc_from_event,
    install_excel_py_auto_convert,
    maybe_convert_excel_py_document,
    maybe_export_excel_py_on_save,
)
from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
from plugin.calc.excel_py_convert.parse_excel_ooxml import has_excel_python_xlsx
from tests.testing_utils import CalcDocStub


def _minimal_xlsx(path: Path, *, with_scripts: bool = False, with_xlws: bool = False) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    wb = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    formula = '_xlfn._xlws.PY(0,0,A1:B2)' if with_xlws else "SUM(A1)"
    sheet = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>{formula}</f></c></row></sheetData>
</worksheet>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("xl/workbook.xml", wb)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        if with_scripts:
            zf.writestr(
                "xl/pythonScripts.xml",
                '<?xml version="1.0"?><pythonScripts><pythonScript><code>1+1</code></pythonScript></pythonScripts>',
            )


def test_has_excel_python_detects_scripts_and_xlws(tmp_path: Path):
    plain = tmp_path / "plain.xlsx"
    _minimal_xlsx(plain)
    assert has_excel_python_xlsx(plain) is False

    scripts = tmp_path / "scripts.xlsx"
    _minimal_xlsx(scripts, with_scripts=True)
    assert has_excel_python_xlsx(scripts) is True

    xlws = tmp_path / "xlws.xlsx"
    _minimal_xlsx(xlws, with_xlws=True)
    assert has_excel_python_xlsx(xlws) is True

    assert has_excel_python_xlsx(tmp_path / "missing.xlsx") is False
    assert has_excel_python_xlsx(tmp_path / "notes.txt") is False


def test_apply_dag_formulas_sets_calc_semicolon_formula():
    """Short scripts stay inline; no py_code_* sheet is created."""
    cell = ConvertedCell(
        sheet="Sheet1",
        cell="C1",
        direction="dag",
        original_code="x",
        converted_code="df = pd.DataFrame(data)",
        data_args=["A1:B2"],
        converted=True,
        array_ref="C1:D2",
    )
    report = ConversionReport(direction="dag", cells=[cell])

    doc = CalcDocStub()
    # Pre-fill a spill cell so clearing is observable.
    doc.getSheets().getByName("Sheet1").getCellRangeByName("D1").setFormula("=OLD")

    errors = apply_dag_formulas_to_calc_doc(doc, report)
    assert errors == []
    assert not doc.getSheets().hasByName("py_code_Sheet1")
    sheet = doc.getSheets().getByName("Sheet1")
    formula = sheet.getCellRangeByName("C1").getFormula()
    assert formula.startswith('=PY("')
    assert "df = pd.DataFrame(data)" in formula
    assert ";A1:B2)" in formula or formula.endswith(";A1:B2)")
    assert sheet.getCellRangeByName("D1").getFormula() == ""  # spill cells cleared


def test_apply_dag_formulas_banks_long_scripts():
    long_code = "x = data\n" + ("# pad\n" * 200)
    assert len(long_code) > 1000
    cell = ConvertedCell(
        sheet="Sheet1",
        cell="C1",
        direction="dag",
        original_code="x",
        converted_code=long_code,
        data_args=["A1:B2"],
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[cell])

    doc = CalcDocStub()
    errors = apply_dag_formulas_to_calc_doc(doc, report)
    assert errors == []
    assert doc.getSheets().hasByName("py_code_Sheet1")
    code_sheet = doc.getSheets().getByName("py_code_Sheet1")
    assert code_sheet.getCellRangeByName("C1").getString() == long_code
    formula = doc.getSheets().getByName("Sheet1").getCellRangeByName("C1").getFormula()
    assert "py_code_Sheet1.C1" in formula
    assert not formula.startswith('=PY("')

def test_script_bank_only_long_scripts_and_mirrors_a1():
    from plugin.calc.excel_py_convert.script_bank import (
        INLINE_CODE_MAX_CHARS,
        code_bank_ref,
        code_sheet_name_for,
        collect_script_bank,
        collect_safety_warnings,
        formula_for_converted_cell,
        normalize_bank_a1,
    )

    assert normalize_bank_a1("$h$4") == "H4"
    assert code_sheet_name_for("Pivots") == "py_code_Pivots"
    assert code_bank_ref("Pivots", "H4") == "py_code_Pivots.H4"

    short = ConvertedCell(
        sheet="Pivots",
        cell="C9",
        direction="dag",
        original_code="a",
        converted_code="x = data",
        data_args=["C4"],
        converted=True,
        script_index=3,
    )
    long_code = "y = data\n" + ("pass\n" * (INLINE_CODE_MAX_CHARS // 5 + 1))
    assert len(long_code) > INLINE_CODE_MAX_CHARS
    long_a = ConvertedCell(
        sheet="Data",
        cell="H4",
        direction="dag",
        original_code="b",
        converted_code=long_code,
        data_args=["A1"],
        converted=True,
        script_index=0,
    )
    long_b = ConvertedCell(
        sheet="Other",
        cell="H4",
        direction="dag",
        original_code="c",
        converted_code=long_code + "# other\n",
        data_args=["B1"],
        converted=True,
        script_index=1,
    )
    banks, warns = collect_script_bank(ConversionReport(direction="dag", cells=[short, long_a, long_b]))
    assert warns == []
    assert "py_code_Pivots" not in banks  # short C9 not banked
    assert banks["py_code_Data"]["H4"] == long_code
    assert banks["py_code_Other"]["H4"] == long_code + "# other\n"

    assert formula_for_converted_cell(short, separator=";") == '=PY("x = data";C4)'
    f_long = formula_for_converted_cell(long_a, separator=";")
    assert f_long == "=PY(py_code_Data.H4;A1)"

    assert any("non-binding xl()" in w for w in collect_safety_warnings("y = xl(1)"))
    assert any("non-binding xl()" in w for w in collect_safety_warnings('y = xl("A1")'))
    assert collect_safety_warnings('y = xl("%P2%", headers=True)') == []
    assert any("whitelist" in w for w in collect_safety_warnings("import requests\nx=1"))


def test_maybe_convert_skips_non_candidate(tmp_path: Path):
    doc = CalcDocStub()
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=None),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is False

    plain = tmp_path / "plain.xlsx"
    _minimal_xlsx(plain)
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(plain)),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is False


def test_maybe_convert_fail_closed_leaves_original(tmp_path: Path):
    src = tmp_path / "bad.xlsx"
    _minimal_xlsx(src, with_scripts=True)
    doc = CalcDocStub()
    # One cell converted so we pass the "any convertible" gate; overall report.ok is False.
    bad = ConversionReport(
        direction="dag",
        cells=[
            ConvertedCell(
                sheet="S",
                cell="A1",
                direction="dag",
                original_code="ok",
                converted_code="ok",
                converted=True,
            ),
            ConvertedCell(
                sheet="S",
                cell="B1",
                direction="dag",
                original_code="",
                converted_code="",
                converted=False,
                issues=["unresolved xl()"],
            ),
        ],
    )
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(src)),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
        patch("plugin.calc.excel_py_convert.convert.convert_to_dag", return_value=bad),
        patch("plugin.calc.excel_py_convert.apply_calc.apply_dag_formulas_to_calc_doc") as apply_uno,
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is False
        apply_uno.assert_not_called()
        assert doc._close_calls == []


def test_maybe_convert_uno_marks_converted(tmp_path: Path):
    src = tmp_path / "py.xlsx"
    _minimal_xlsx(src, with_scripts=True)
    doc = CalcDocStub()
    ok_cell = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="dag",
        original_code="1",
        converted_code="1",
        converted=True,
        dag_formula='=PY("1")',
    )
    report = ConversionReport(direction="dag", cells=[ok_cell])
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(src)),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
        patch("plugin.doc.udprops.set_document_property") as set_prop,
        patch("plugin.calc.excel_py_convert.convert.convert_to_dag", return_value=report),
        patch("plugin.calc.excel_py_convert.apply_calc.apply_dag_formulas_to_calc_doc", return_value=[]) as apply_uno,
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is True
        apply_uno.assert_called_once()
        names = [c.args[1] for c in set_prop.call_args_list]
        assert _CONVERTED_PROP in names
        assert "ExcelPyDagMeta" in names
        set_prop.assert_any_call(doc, _CONVERTED_PROP, "1")

def test_install_excel_py_auto_convert_once():
    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    # Reset module listener between tests
    import plugin.calc.excel_py_convert.auto_open as mod

    mod._doc_listener = None
    install_excel_py_auto_convert(ctx)
    install_excel_py_auto_convert(ctx)
    assert broadcaster.addDocumentEventListener.call_count == 1


class _DisposedViewControllerEvent:
    """Simulates UNO DocumentEvent where ViewController access raises (disposed)."""

    def __init__(self, source=None):
        self.Source = source
        self.EventName = ""

    def __getattr__(self, name: str):
        if name == "ViewController":
            raise RuntimeError("DrawController object has already been disposed")
        raise AttributeError(name)


def test_doc_from_event_disposed_view_controller_returns_none():
    assert _doc_from_event(_DisposedViewControllerEvent()) is None


def test_doc_from_event_disposed_view_controller_falls_back_to_calc_source():
    source = CalcDocStub()
    setattr(source, "CurrentController", None)
    setattr(source, "_controller", None)
    assert _doc_from_event(_DisposedViewControllerEvent(source=source)) is source


def test_doc_from_event_onnew_controller_throw_still_returns_calc_source():
    """Hidden factory OnNew: Source is the scalc, getCurrentController raises."""
    source = CalcDocStub()

    def _boom():
        raise RuntimeError("no controller yet")

    setattr(source, "getCurrentController", _boom)

    class _OnNew:
        EventName = "OnNew"
        Source = source

    assert _doc_from_event(_OnNew()) is source


def test_onload_rewrites_collabora_py_via_execute_on_main_thread():
    """OnLoadFinished marshals Collabora rewrite (doc only) then geometric open (ctx, doc)."""
    import plugin.calc.excel_py_convert.auto_open as mod
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas

    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    install_excel_py_auto_convert(ctx)
    listener = broadcaster.addDocumentEventListener.call_args[0][0]
    doc = CalcDocStub()

    class _LoadEvent:
        EventName = "OnLoadFinished"
        Source = doc

    event = _LoadEvent()
    with (
        patch.object(mod, "maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.execute_on_main_thread") as marshal,
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch.object(mod.log, "warning") as warn,
    ):
        listener.on_document_event(event)
        convert.assert_called_once()
        assert marshal.call_count == 2
        assert marshal.call_args_list[0][0][0] is mod._geometric_open_job
        assert marshal.call_args_list[0][0][1:] == (ctx, doc)
        assert marshal.call_args_list[1][0][0] is maybe_rewrite_collabora_py_formulas
        assert marshal.call_args_list[1][0][1:] == (doc,)
        warn.assert_not_called()


def test_onnew_records_geometric_session_inline_on_uno_thread():
    """OnNew is already on the VCL thread — do not enqueue (30s leftover hang)."""
    import plugin.calc.excel_py_convert.auto_open as mod

    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    install_excel_py_auto_convert(ctx)
    listener = broadcaster.addDocumentEventListener.call_args[0][0]
    doc = CalcDocStub()

    class _NewEvent:
        EventName = "OnNew"
        Source = doc

    with (
        patch.object(mod, "maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.execute_on_main_thread") as marshal,
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch(
            "plugin.calc.python.geometric_recalc.maybe_geometric_on_document_open"
        ) as open_geo,
    ):
        listener.on_document_event(_NewEvent())
        convert.assert_not_called()
        marshal.assert_not_called()
        open_geo.assert_called_once_with(ctx, doc)


def test_oncreate_without_resolvable_doc_still_runs_geometric_open():
    """Leftover OnCreate Source is often the Writer keeper — the open hook still runs."""
    import plugin.calc.excel_py_convert.auto_open as mod

    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    install_excel_py_auto_convert(ctx)
    listener = broadcaster.addDocumentEventListener.call_args[0][0]

    class _CreateEvent:
        EventName = "OnCreate"
        Source = None

    with (
        patch.object(mod, "maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.execute_on_main_thread") as marshal,
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch(
            "plugin.calc.python.geometric_recalc.maybe_geometric_on_document_open"
        ) as open_geo,
    ):
        listener.on_document_event(_CreateEvent())
        convert.assert_not_called()
        marshal.assert_not_called()
        open_geo.assert_called_once_with(ctx, None)


def test_geometric_open_job_wires_unload_listener():
    """Closing the workbook is the soffice-side clear for its kernel and geometric state."""
    import plugin.calc.excel_py_convert.auto_open as mod

    doc = CalcDocStub()
    ctx = MagicMock()
    with (
        patch("plugin.calc.python.geometric_recalc.maybe_geometric_on_document_open") as open_geo,
        patch(
            "plugin.calc.python.workbook_lifecycle.ensure_calc_workbook_unload_resets_python"
        ) as ensure_unload,
    ):
        mod._geometric_open_job(ctx, doc)
    open_geo.assert_called_once_with(ctx, doc)
    ensure_unload.assert_called_once_with(ctx, doc)


def test_onnew_records_geometric_session_via_execute_on_main_thread():
    """Factory Calc fires OnNew off-main; leftover still needs a marshaled record."""
    import plugin.calc.excel_py_convert.auto_open as mod

    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    install_excel_py_auto_convert(ctx)
    listener = broadcaster.addDocumentEventListener.call_args[0][0]
    doc = CalcDocStub()

    class _NewEvent:
        EventName = "OnNew"
        Source = doc

    with (
        patch.object(mod, "maybe_convert_excel_py_document") as convert,
        patch("plugin.framework.queue_executor.execute_on_main_thread") as marshal,
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
    ):
        listener.on_document_event(_NewEvent())
        convert.assert_not_called()
        assert marshal.call_count == 1
        assert marshal.call_args_list[0][0][0] is mod._geometric_open_job
        assert marshal.call_args_list[0][0][1:] == (ctx, doc)


def test_excel_py_listener_disposed_view_controller_does_not_warn():
    """Listener must not hit the outer warning path when ViewController is disposed."""
    import plugin.calc.excel_py_convert.auto_open as mod

    ctx = MagicMock()
    smgr = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = broadcaster
    mod._doc_listener = None
    install_excel_py_auto_convert(ctx)
    listener = broadcaster.addDocumentEventListener.call_args[0][0]
    event = _DisposedViewControllerEvent()
    event.EventName = "OnLoadFinished"
    with patch.object(mod.log, "warning") as warn:
        listener.on_document_event(event)
        warn.assert_not_called()


def test_maybe_export_skips_without_py_cells(tmp_path: Path):
    path = tmp_path / "plain.xlsx"
    _minimal_xlsx(path)
    doc = CalcDocStub()
    empty = ConversionReport(direction="excel", cells=[])
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
        patch("plugin.calc.excel_py_convert.convert.convert_uno_doc_to_excel", return_value=empty),
        patch("plugin.calc.excel_py_convert.convert.write_excel_python_xlsx") as write_xlsx,
    ):
        assert maybe_export_excel_py_on_save(MagicMock(), doc) is False
        write_xlsx.assert_not_called()


def test_maybe_export_writes_native_package(tmp_path: Path):
    path = tmp_path / "dag.xlsx"
    _minimal_xlsx(path)
    doc = CalcDocStub()
    report = ConversionReport(
        direction="excel",
        cells=[
            ConvertedCell(
                sheet="Sheet1",
                cell="A1",
                direction="excel",
                original_code="df = data.to_pandas()",
                converted_code="df = xl(%P2%, headers=True)",
                data_args=["B1:C2"],
                converted=True,
                script_index=0,
                excel_formula="=_xlfn._xlws.PY(0,0,B1:C2)",
            )
        ],
    )
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
        patch("plugin.calc.excel_py_convert.convert.convert_uno_doc_to_excel", return_value=report),
        patch("plugin.calc.excel_py_convert.convert.write_excel_python_xlsx") as write_xlsx,
        patch("plugin.calc.excel_py_convert.parse_excel_ooxml.has_excel_python_xlsx", return_value=True),
    ):
        assert maybe_export_excel_py_on_save(MagicMock(), doc) is True
        write_xlsx.assert_called_once()


def test_maybe_export_msgbox_on_failure(tmp_path: Path):
    path = tmp_path / "dag.xlsx"
    _minimal_xlsx(path)
    doc = CalcDocStub()
    report = ConversionReport(
        direction="excel",
        cells=[
            ConvertedCell(
                sheet="Sheet1",
                cell="A1",
                direction="excel",
                original_code="x",
                converted_code="xl(%P2%)",
                data_args=["A1"],
                converted=True,
                script_index=0,
            )
        ],
    )
    ctx = MagicMock()
    from plugin.framework.constants import EXTENSION_ID_LIBREPY
    from plugin.framework.uno_context import reset_package_extension_id_for_tests, set_package_extension_id

    set_package_extension_id(EXTENSION_ID_LIBREPY)
    try:
        with (
            patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
            patch("plugin.calc.excel_py_convert.convert.convert_uno_doc_to_excel", return_value=report),
            patch(
                "plugin.calc.excel_py_convert.convert.write_excel_python_xlsx",
                side_effect=ValueError("boom"),
            ),
            patch("plugin.chatbot.dialogs.msgbox") as box,
        ):
            assert maybe_export_excel_py_on_save(ctx, doc) is False
            box.assert_called_once()
            assert box.call_args[0][1] == "LibrePy"
            assert "boom" in box.call_args[0][2]
    finally:
        reset_package_extension_id_for_tests()


def test_convert_uno_doc_to_excel_snapshot():
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellRangeByName("A1").setFormula('=PY("x = 1")')
    report = convert_uno_doc_to_excel(doc)
    assert report.direction == "excel"
    assert len(report.cells) == 1
    assert report.cells[0].cell == "A1"
    assert report.cells[0].sheet == "Sheet1"


def test_convert_uno_doc_to_excel_resolves_script_bank():
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheets = doc.getSheets()
    sheets.insertNewByName("py_code_Sheet1", sheets.getCount())
    bank = sheets.getByName("py_code_Sheet1")
    bank.getCellRangeByName("B2").setString("long_script_body = 42")
    sheet = sheets.getByName("Sheet1")
    sheet.getCellRangeByName("B2").setFormula("=PY(py_code_Sheet1.B2)")
    report = convert_uno_doc_to_excel(doc)
    assert report.direction == "excel"
    assert len(report.cells) == 1
    assert report.cells[0].cell == "B2"
    assert "long_script_body = 42" in report.cells[0].converted_code


def test_convert_uno_doc_to_excel_empty_bank_cell_leaves_reference():
    """Empty or whitespace-only bank cell leaves py_code reference unresolved in formula."""
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheets = doc.getSheets()
    sheets.insertNewByName("py_code_Sheet1", sheets.getCount())
    bank = sheets.getByName("py_code_Sheet1")
    bank.getCellRangeByName("B2").setString("   ")
    sheet = sheets.getByName("Sheet1")
    sheet.getCellRangeByName("B2").setFormula("=PY(py_code_Sheet1.B2)")
    report = convert_uno_doc_to_excel(doc)
    assert report.direction == "excel"
    assert len(report.cells) == 1
    assert "py_code_Sheet1.B2" in report.cells[0].original_code


def test_convert_uno_doc_to_excel_max_scan_refuses_export(tmp_path: Path):
    """Exceeding formula scan cap marks report failed and refuses export."""
    from plugin.calc.excel_py_convert import uno_snapshot
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    for r in range(1, 10):
        sheet.getCellRangeByName(f"A{r}").setFormula("=1+1")

    with patch.object(uno_snapshot, "FORMULA_SCAN_CAP", 5):
        report = convert_uno_doc_to_excel(doc)
        assert not report.ok
        assert any("scan limit reached (5 formula cells); export aborted" in i for i in report.issues)

    ctx = MagicMock()
    path = tmp_path / "capped.xlsx"
    path.touch()
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
        patch("plugin.calc.excel_py_convert.convert.convert_uno_doc_to_excel", return_value=report),
        patch("plugin.calc.excel_py_convert.convert.write_excel_python_xlsx") as mock_write,
        patch("plugin.chatbot.dialogs.msgbox") as box,
    ):
        assert maybe_export_excel_py_on_save(ctx, doc) is False
        mock_write.assert_not_called()
        box.assert_called_once()


def test_convert_uno_doc_to_excel_large_range_chunked_exports_later_window():
    """Formula range larger than 100k cells under the cap exports =PY in later window."""
    from plugin.calc.excel_py_convert import uno_snapshot
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")

    mock_ranges = MagicMock()
    mock_ranges.getCount.return_value = 1
    mock_range = MagicMock()
    # 100_005 rows, 1 col = 100_005 cells (> 100_000)
    mock_range.getRangeAddress.return_value = MagicMock(StartRow=0, StartColumn=0, EndRow=100_004, EndColumn=0)
    mock_ranges.getByIndex.return_value = mock_range

    def _get_range_by_pos(start_col: int, start_row: int, end_col: int, end_row: int):
        sub = MagicMock()
        num_rows = end_row - start_row + 1
        if start_row >= 100_000:
            formulas = tuple(tuple(['=PY("x = 42")'] if r == 0 else ["=1"]) for r in range(num_rows))
        else:
            formulas = tuple(tuple(["=1"]) for _ in range(num_rows))
        sub.getFormulas.return_value = formulas
        return sub

    with (
        patch.object(sheet, "queryContentCells", return_value=mock_ranges),
        patch.object(sheet, "getCellRangeByPosition", side_effect=_get_range_by_pos),
        patch.object(uno_snapshot, "FORMULA_SCAN_CHUNK_ROWS", 50_000),
    ):
        report = convert_uno_doc_to_excel(doc)
        assert report.ok
        assert len(report.cells) == 1
        assert report.cells[0].cell == "A100001"
        assert "x = 42" in report.cells[0].converted_code


def test_convert_uno_doc_to_excel_unparseable_py_formula(tmp_path: Path):
    """Unparseable =PY formula names the cell issue and blocks package write."""
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellRangeByName("B2").setFormula('=PY("x = 1" NOTANARG)')

    report = convert_uno_doc_to_excel(doc)
    assert not report.ok
    assert any("Sheet1!B2: =PY formula could not be parsed" in i for i in report.issues)

    ctx = MagicMock()
    path = tmp_path / "bad_py.xlsx"
    path.touch()
    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
        patch("plugin.calc.excel_py_convert.convert.convert_uno_doc_to_excel", return_value=report),
        patch("plugin.calc.excel_py_convert.convert.write_excel_python_xlsx") as mock_write,
        patch("plugin.chatbot.dialogs.msgbox") as box,
    ):
        assert maybe_export_excel_py_on_save(ctx, doc) is False
        mock_write.assert_not_called()
        box.assert_called_once()


def test_convert_uno_doc_to_excel_bank_sheet_cached_once():
    """Multiple banked cells on a sheet perform string lookup from cached bank map."""
    from plugin.calc.excel_py_convert.uno_snapshot import convert_uno_doc_to_excel

    doc = CalcDocStub()
    sheets = doc.getSheets()
    sheets.insertNewByName("py_code_Sheet1", sheets.getCount())
    bank = sheets.getByName("py_code_Sheet1")
    bank.getCellRangeByName("B2").setString("code_b2 = 1")
    bank.getCellRangeByName("C3").setString("code_c3 = 2")

    sheet = sheets.getByName("Sheet1")
    sheet.getCellRangeByName("B2").setFormula("=PY(py_code_Sheet1.B2)")
    sheet.getCellRangeByName("C3").setFormula("=PY(py_code_Sheet1.C3)")

    with patch.object(bank, "getCellRangeByName", wraps=bank.getCellRangeByName) as spy_get_cell:
        report = convert_uno_doc_to_excel(doc)
        assert report.ok
        assert len(report.cells) == 2
        assert spy_get_cell.call_count == 0


def test_apply_dag_formulas_rollback_on_cell_failure(tmp_path: Path):
    """When a cell write fails midway, previous formulas and bank sheets are restored."""
    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellRangeByName("A1").setFormula("=ORIGINAL_A1")
    sheet.getCellRangeByName("B1").setFormula("=ORIGINAL_B1")

    long_code = "x = " + "a" * 1200
    c1 = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="dag",
        original_code=long_code,
        converted_code=long_code,
        converted=True,
    )
    c2 = ConvertedCell(
        sheet="Sheet1",
        cell="B1",
        direction="dag",
        original_code="y = 2",
        converted_code="y = 2",
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[c1, c2])

    def _failing_set_formula(val: str):
        raise RuntimeError("simulated setFormula failure on B1")

    sheet.getCellRangeByName("B1").setFormula = _failing_set_formula

    path = tmp_path / "test.xlsx"
    path.touch()

    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
        patch("plugin.calc.excel_py_convert.parse_excel_ooxml.has_excel_python_xlsx", return_value=True),
        patch("plugin.calc.excel_py_convert.convert.convert_to_dag", return_value=report),
    ):
        result = maybe_convert_excel_py_document(MagicMock(), doc)
        assert result is False
        from plugin.doc.udprops import get_document_property
        assert get_document_property(doc, _CONVERTED_PROP) is None
        assert sheet.getCellRangeByName("A1").getFormula() == "=ORIGINAL_A1"
        assert not doc.getSheets().hasByName("py_code_Sheet1")


def test_apply_dag_formulas_preflight_failure_writes_nothing():
    """Preflight failure (unknown sheet or non-bank collision) leaves doc unchanged."""
    doc_a = CalcDocStub()
    sheet_a = doc_a.getSheets().getByName("Sheet1")
    sheet_a.getCellRangeByName("A1").setFormula("=ORIGINAL")
    c_unknown = ConvertedCell(
        sheet="NoSuchSheet",
        cell="A1",
        direction="dag",
        original_code="1",
        converted_code="1",
        converted=True,
    )
    report_a = ConversionReport(direction="dag", cells=[c_unknown])
    errors_a = apply_dag_formulas_to_calc_doc(doc_a, report_a)
    assert any("unmapped sheet" in err for err in errors_a)
    assert sheet_a.getCellRangeByName("A1").getFormula() == "=ORIGINAL"
    assert doc_a.getSheets().getCount() == 1

    doc_b = CalcDocStub()
    sheet_b = doc_b.getSheets().getByName("Sheet1")
    sheet_b.getCellRangeByName("A1").setFormula("=ORIGINAL")
    doc_b.getSheets().insertNewByName("py_code_Sheet1", 1)
    fake_bank = doc_b.getSheets().getByName("py_code_Sheet1")
    fake_bank.getCellRangeByName("A1").setString("user data")

    long_code = "x = " + "b" * 1200
    c_bank = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="dag",
        original_code=long_code,
        converted_code=long_code,
        converted=True,
    )
    report_b = ConversionReport(direction="dag", cells=[c_bank])
    errors_b = apply_dag_formulas_to_calc_doc(doc_b, report_b)
    assert any("Bank sheet collision" in err for err in errors_b)
    assert sheet_b.getCellRangeByName("A1").getFormula() == "=ORIGINAL"
    assert fake_bank.getCellRangeByName("A1").getString() == "user data"


def test_maybe_convert_excel_py_document_meta_store_warning(tmp_path: Path):
    """Successful apply logs warning if ExcelPyDagMeta store fails, leaving open converted."""

    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    sheet.getCellRangeByName("A1").setFormula('=PY("x = 1")')

    c = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="dag",
        original_code="x = 1",
        converted_code="x = 1",
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[c])
    path = tmp_path / "valid.xlsx"
    path.touch()

    with (
        patch("plugin.doc.text_helpers.get_document_path", return_value=str(path)),
        patch("plugin.calc.excel_py_convert.parse_excel_ooxml.has_excel_python_xlsx", return_value=True),
        patch("plugin.calc.excel_py_convert.convert.convert_to_dag", return_value=report),
        patch("plugin.calc.excel_py_convert.convert.store_dag_meta_on_doc", side_effect=RuntimeError("meta store failure")),
        patch("plugin.calc.excel_py_convert.auto_open.log.warning") as mock_warn,
    ):
        result = maybe_convert_excel_py_document(MagicMock(), doc)
        assert result is True
        from plugin.doc.udprops import get_document_property
        assert get_document_property(doc, _CONVERTED_PROP) == "1"
        assert mock_warn.called
        assert any("ExcelPyDagMeta" in str(arg) for call in mock_warn.call_args_list for arg in call.args)
