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
from plugin.tests.testing_utils import CalcDocStub, setup_uno_mocks

setup_uno_mocks()


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

    def __getattr__(self, name: str):
        if name == "ViewController":
            raise RuntimeError("DrawController object has already been disposed")
        raise AttributeError(name)


def test_doc_from_event_disposed_view_controller_returns_none():
    assert _doc_from_event(_DisposedViewControllerEvent()) is None


def test_doc_from_event_disposed_view_controller_falls_back_to_calc_source():
    source = CalcDocStub()
    source.CurrentController = None
    source._controller = None
    assert _doc_from_event(_DisposedViewControllerEvent(source=source)) is source


def test_onload_rewrites_collabora_py_via_execute_on_main_thread():
    """OnLoadFinished marshals Collabora rewrite (doc only) then geometric open (ctx, doc)."""
    import plugin.calc.excel_py_convert.auto_open as mod
    from plugin.calc.python.collabora_formula import maybe_rewrite_collabora_py_formulas
    from plugin.calc.python.geometric_recalc import maybe_geometric_on_document_open

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
        patch.object(mod.log, "warning") as warn,
    ):
        listener.on_document_event(event)
        convert.assert_called_once()
        assert marshal.call_count == 2
        assert marshal.call_args_list[0][0][0] is maybe_rewrite_collabora_py_formulas
        assert marshal.call_args_list[0][0][1:] == (doc,)
        assert marshal.call_args_list[1][0][0] is maybe_geometric_on_document_open
        assert marshal.call_args_list[1][0][1:] == (ctx, doc)
        warn.assert_not_called()


def test_onnew_records_geometric_session_via_execute_on_main_thread():
    """Factory Calc fires OnNew; Shared-kernel leftover UNO needs that record."""
    import plugin.calc.excel_py_convert.auto_open as mod
    from plugin.calc.python.geometric_recalc import maybe_geometric_on_document_open

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
    ):
        listener.on_document_event(_NewEvent())
        convert.assert_not_called()
        assert marshal.call_count == 1
        assert marshal.call_args_list[0][0][0] is maybe_geometric_on_document_open
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
