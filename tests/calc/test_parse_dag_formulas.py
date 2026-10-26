# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline DAG =PY scanning: shared-formula expansion and py_code_* bank refs."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from plugin.calc.excel_py_convert.parse_dag_formulas import (
    _sheet_cell_map,
    iter_dag_py_formulas_xlsx,
    resolve_code_bank_ref,
)


def _ws(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData></worksheet>"
    )


def _write_xlsx(tmp_path: Path, sheets: dict[str, str]) -> Path:
    wb = [
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    ]
    rels = [
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    ]
    for i, title in enumerate(sheets, start=1):
        wb.append(f'<sheet name="{title}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        )
    wb.append("</sheets></workbook>")
    rels.append("</Relationships>")
    path = tmp_path / "book.xlsx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", "".join(wb))
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        for i, xml in enumerate(sheets.values(), start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", xml)
    return path


def test_shared_plain_py_group_expands() -> None:
    ws = _ws(
        '<row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">PY("x = xl(A1) + 1";B1)</f></c></row>'
        '<row r="2"><c r="A2"><f t="shared" si="0"/></c></row>'
    )
    cell_map = _sheet_cell_map(ET.fromstring(ws), [])
    assert cell_map["A1"][0] == '=PY("x = xl(A1) + 1";B1)'
    assert cell_map["A2"][0] == '=PY("x = xl(A1) + 1";B2)'


def test_shared_python_alias_keeps_prefix() -> None:
    ws = _ws(
        '<row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">PYTHON("x=1";B1)</f></c></row>'
        '<row r="2"><c r="A2"><f t="shared" si="0"/></c></row>'
    )
    cell_map = _sheet_cell_map(ET.fromstring(ws), [])
    assert cell_map["A2"][0].startswith("=PYTHON(")
    assert cell_map["A2"][0].endswith(";B2)")


def test_shared_non_py_master_produces_no_formula_for_followers() -> None:
    ws = _ws(
        '<row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">B1</f></c></row>'
        '<row r="2"><c r="A2"><f t="shared" si="0"/></c></row>'
    )
    cell_map = _sheet_cell_map(ET.fromstring(ws), [])
    assert cell_map["A1"][0] == "=B1"
    assert cell_map["A2"][0] == ""


def test_bank_cell_lowercase_r_resolves(tmp_path: Path) -> None:
    host_ws = _ws('<row r="1"><c r="A1"><f>PY(py_code_Data.A1)</f></c></row>')
    bank_ws = _ws('<row r="1"><c r="a1" t="inlineStr"><is><t>print(1)</t></is></c></row>')
    path = _write_xlsx(tmp_path, {"Data": host_ws, "py_code_Data": bank_ws})
    items = iter_dag_py_formulas_xlsx(path)
    assert len(items) == 1
    assert "print(1)" in items[0][2]


def test_bank_sheet_title_case_mismatch_resolves(tmp_path: Path) -> None:
    host_ws = _ws('<row r="1"><c r="A1"><f>PY(py_code_data.A1)</f></c></row>')
    bank_ws = _ws('<row r="1"><c r="A1" t="inlineStr"><is><t>print(1)</t></is></c></row>')
    path = _write_xlsx(tmp_path, {"Data": host_ws, "py_code_Data": bank_ws})
    items = iter_dag_py_formulas_xlsx(path)
    assert len(items) == 1
    assert "print(1)" in items[0][2]


def test_quoted_bank_looking_code_is_not_resolved(tmp_path: Path) -> None:
    host_ws = _ws('<row r="1"><c r="A1"><f>PY("py_code_Data.A1")</f></c></row>')
    bank_ws = _ws('<row r="1"><c r="A1" t="inlineStr"><is><t>print(1)</t></is></c></row>')
    path = _write_xlsx(tmp_path, {"Data": host_ws, "py_code_Data": bank_ws})
    items = iter_dag_py_formulas_xlsx(path)
    assert len(items) == 1
    assert '"py_code_Data.A1"' in items[0][2]
    assert "print(1)" not in items[0][2]


def test_blank_bank_value_is_not_resolved(tmp_path: Path) -> None:
    host_ws = _ws('<row r="1"><c r="A1"><f>PY(py_code_Data.A1)</f></c></row>')
    bank_ws = _ws('<row r="1"><c r="A1" t="inlineStr"><is><t>   </t></is></c></row>')
    path = _write_xlsx(tmp_path, {"Data": host_ws, "py_code_Data": bank_ws})
    items = iter_dag_py_formulas_xlsx(path)
    assert len(items) == 1
    assert "py_code_Data.A1" in items[0][2]


def test_resolve_code_bank_ref_uppercases_a1_lookup() -> None:
    assert resolve_code_bank_ref("py_code_Data.A1", {"py_code_Data": {"a1": ("", "print(1)")}}) == "print(1)"


def test_empty_sheet_returns_no_py_formulas(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"Data": _ws("")})
    assert iter_dag_py_formulas_xlsx(path) == []
