# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Excel PY auto-convert on open (no menu)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from plugin.calc.excel_py_convert.apply_calc import apply_dag_formulas_to_calc_doc
from plugin.calc.excel_py_convert.auto_open import (
    _CONVERTED_PROP,
    install_excel_py_auto_convert,
    maybe_convert_excel_py_document,
)
from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
from plugin.calc.excel_py_convert.parse_excel_ooxml import has_excel_python_xlsx
from plugin.tests.testing_utils import setup_uno_mocks

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

    spill = MagicMock()
    anchor = MagicMock()
    sheet = MagicMock()

    def _range(name: str):
        if name == "C1":
            return anchor
        return spill

    sheet.getCellRangeByName.side_effect = _range
    sheets = MagicMock()
    sheets.hasByName.return_value = True
    sheets.getByName.return_value = sheet
    doc = MagicMock()
    doc.getSheets.return_value = sheets

    errors = apply_dag_formulas_to_calc_doc(doc, report)
    assert errors == []
    formula = anchor.setFormula.call_args[0][0]
    assert formula.startswith('=PY("')
    assert ";A1:B2)" in formula or formula.endswith(";A1:B2)")
    assert spill.setFormula.called  # spill cells cleared


def test_maybe_convert_skips_non_candidate(tmp_path: Path):
    doc = MagicMock()
    doc.supportsService.return_value = True
    with (
        patch("plugin.doc.document_helpers.get_document_path", return_value=None),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is False

    plain = tmp_path / "plain.xlsx"
    _minimal_xlsx(plain)
    with (
        patch("plugin.doc.document_helpers.get_document_path", return_value=str(plain)),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is False


def test_maybe_convert_fail_closed_leaves_original(tmp_path: Path):
    src = tmp_path / "bad.xlsx"
    _minimal_xlsx(src, with_scripts=True)
    doc = MagicMock()
    doc.supportsService.return_value = True
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
        patch("plugin.doc.document_helpers.get_document_path", return_value=str(src)),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
        patch("plugin.calc.excel_py_convert.convert.convert_to_dag", return_value=bad),
        patch("plugin.calc.excel_py_convert.convert.write_dag_formulas_xlsx") as write_xlsx,
        patch("plugin.calc.excel_py_convert.apply_calc.apply_dag_formulas_to_calc_doc") as apply_uno,
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is False
        write_xlsx.assert_not_called()
        apply_uno.assert_not_called()
        doc.close.assert_not_called()


def test_maybe_convert_uno_fallback_marks_converted(tmp_path: Path):
    src = tmp_path / "py.xlsx"
    _minimal_xlsx(src, with_scripts=True)
    doc = MagicMock()
    doc.supportsService.return_value = True
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
        patch("plugin.doc.document_helpers.get_document_path", return_value=str(src)),
        patch("plugin.doc.udprops.get_document_property", return_value=None),
        patch("plugin.doc.udprops.set_document_property") as set_prop,
        patch("plugin.calc.excel_py_convert.convert.convert_to_dag", return_value=report),
        patch(
            "plugin.calc.excel_py_convert.convert.write_dag_formulas_xlsx",
            side_effect=ImportError("no openpyxl"),
        ),
        patch("plugin.calc.excel_py_convert.apply_calc.apply_dag_formulas_to_calc_doc", return_value=[]) as apply_uno,
    ):
        assert maybe_convert_excel_py_document(MagicMock(), doc) is True
        apply_uno.assert_called_once()
        set_prop.assert_called_once_with(doc, _CONVERTED_PROP, "1")


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
    mod._doc_listener = None
