# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Excel ↔ DAG-style =PY conversion (inline models + synthetic OOXML)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from plugin.calc.excel_py_convert.convert import write_dag_formulas_xlsx
from plugin.calc.excel_py_convert.models import ExcelPyCell, ExcelWorkbookModel, SheetInfo
from plugin.calc.excel_py_convert.parse_excel_ooxml import (
    load_excel_model,
    parse_xlws_py_formula,
    split_top_level_args,
)
from plugin.calc.excel_py_convert.resolve_refs import resolve_dep
from plugin.calc.excel_py_convert.to_dag import convert_model_to_dag, rewrite_excel_code
from plugin.calc.excel_py_convert.to_excel import (
    convert_dag_formula_to_excel,
    expand_placeholders_to_literals,
    rewrite_dag_code_to_excel,
)
from plugin.calc.python.formula_edit import (
    escape_code_for_excel_formula,
    escape_code_for_formula,
    rebuild_python_formula_with_data,
)


def _cell(
    sheet: str,
    cell: str,
    script_index: int,
    *,
    return_type: int = 0,
    deps: list[str] | None = None,
    array_ref: str = "",
    row: int = 0,
    col: int = 0,
) -> ExcelPyCell:
    return ExcelPyCell(
        sheet=sheet,
        cell=cell,
        script_index=script_index,
        return_type=return_type,
        deps=list(deps or []),
        array_ref=array_ref or cell,
        row=row,
        col=col,
    )


def _sheet(title: str, order: int = 0) -> SheetInfo:
    return SheetInfo(title=title, order=order, part_name="")


def demo1_fillna() -> ExcelWorkbookModel:
    """Demo 1: two setup DataFrames, merge, fillna, spill outputs."""
    return ExcelWorkbookModel(
        scripts=[
            "df_tsx = xl(%P2%, headers=True)",
            "df_nyse = xl(%P2%, headers=True)",
            "merged = df_tsx.merge(df_nyse, how='outer', on='Date', suffixes=('_TSX','_NYSE'))",
            "merged_fixed = merged.sort_values('Date').fillna(method='ffill')",
            "merged.sort_values('Date')",
            "merged_fixed",
        ],
        cells=[
            _cell("Sheet1", "H4", 0, return_type=1, deps=["_xlfn.ANCHORARRAY(A6)"], row=4, col=8),
            _cell("Sheet1", "H5", 1, return_type=1, deps=["_xlfn.ANCHORARRAY(D6)"], row=5, col=8),
            _cell("Sheet1", "H6", 2, return_type=1, row=6, col=8),
            _cell("Sheet1", "H7", 3, return_type=1, row=7, col=8),
            _cell("Sheet1", "G13", 4, array_ref="G13:I268", row=13, col=7),
            _cell("Sheet1", "K13", 5, array_ref="K13:M268", row=13, col=11),
        ],
        sheets=[_sheet("Sheet1")],
        anchor_snapshots={"A6": "A6:B254", "D6": "D6:E256", "G13": "G13:I268", "K13": "K13:M268"},
    )


def demo3_groupby() -> ExcelWorkbookModel:
    """Demo 3: cross-sheet table + repeated groupby script cells."""
    return ExcelWorkbookModel(
        scripts=[
            "df = xl(%P2%,headers=True)",
            "clientsPivot = xl(%P2%, headers=True)\nclients = clientsPivot['Clients'].dropna().tolist()",
            "filterDF = df[df['Client Name'].isin(clients)]",
            "filterDF.groupby(xl(%P2%))[[xl(%P3%)]].agg(xl(%P4%))",
            "xl(%P2%).plot(kind='bar', title=xl(%P3%))",
            "filterDF[xl(%P2%)].sum()",
        ],
        cells=[
            _cell("Pivots", "C1", 0, return_type=1, deps=["tradeData[#All]"], row=1, col=3),
            _cell("Pivots", "C2", 1, return_type=1, deps=["B24:B44"], row=2, col=3),
            _cell("Pivots", "C3", 2, return_type=1, row=3, col=3),
            _cell("Pivots", "C9", 3, return_type=1, deps=["C4", "C5", "C6"], row=9, col=3),
            _cell("Pivots", "D9", 3, return_type=1, deps=["D4", "D5", "D6"], row=9, col=4),
            _cell("Pivots", "E9", 3, return_type=1, deps=["E4", "E5", "E6"], row=9, col=5),
            _cell("Pivots", "F9", 3, return_type=1, deps=["F4", "F5", "F6"], row=9, col=6),
            _cell("Pivots", "C10", 4, deps=["C9", "C7"], row=10, col=3),
            _cell("Pivots", "D10", 4, deps=["D9", "D7"], row=10, col=4),
            _cell("Pivots", "E10", 4, deps=["E9", "E7"], row=10, col=5),
            _cell("Pivots", "F10", 4, deps=["F9", "F7"], row=10, col=6),
            _cell("Pivots", "C15", 5, return_type=1, deps=["C14"], row=15, col=3),
            _cell("Pivots", "D15", 5, return_type=1, deps=["D14"], row=15, col=4),
        ],
        sheets=[_sheet("Data", 0), _sheet("Pivots", 1)],
        tables={"tradeData": "Data!A1:AA5850"},
    )


def demo5_melted() -> ExcelWorkbookModel:
    """Demo 5: table [#All] → melt."""
    return ExcelWorkbookModel(
        scripts=[
            "df=xl(%P2%, headers=True)",
            "df_melt =pd.melt(df,id_vars=['Category','Expense'],value_vars=['Q1','Q2','Q3','Q4'],var_name='Quarter', value_name='Amount')",
        ],
        cells=[
            _cell("Data", "H1", 0, return_type=1, deps=["Table1[#All]"], row=1, col=8),
            _cell("Data", "H3", 1, array_ref="H3:K83", row=3, col=8),
        ],
        sheets=[_sheet("Data")],
        tables={"Table1": "Data!A3:F23"},
        anchor_snapshots={"H3": "H3:K83"},
    )


def demo6_correlation() -> ExcelWorkbookModel:
    """Demo 6: multi-range + headers=False."""
    return ExcelWorkbookModel(
        scripts=[
            "df=xl(%P2%, headers=True)",
            'def portVar(w, V):\n    return np.matmul(w.T, np.matmul(V,w))\n"Port variance function"',
            "df.cov()*10000",
            "portVar(xl(%P2%),xl(%P3%))",
            "df.corr()",
            "sns.heatmap(df.corr(),annot=True,vmin=-1, vmax=1, cmap='BrBG').set_title(\"Correlation Matrix\")",
            "w = xl(%P2%, headers=False)\nV = xl(%P3%)\nportVar(w, V)**0.5",
        ],
        cells=[
            _cell("Sheet1", "L2", 0, return_type=1, deps=["A4:I63"], row=2, col=12),
            _cell("Sheet1", "L3", 1, return_type=1, row=3, col=12),
            _cell("Sheet1", "L7", 2, array_ref="L7:T15", row=7, col=12),
            _cell("Sheet1", "N17", 3, deps=["U8:U15", "M8:T15"], row=17, col=14),
            _cell("Sheet1", "L22", 4, array_ref="L22:T30", row=22, col=12),
            _cell("Sheet1", "M32", 5, row=32, col=13),
            _cell("Sheet1", "AK34", 6, deps=["AK12:AK13", "AN20:AO21"], row=34, col=37),
        ],
        sheets=[_sheet("Sheet1")],
        anchor_snapshots={"L7": "L7:T15", "L22": "L22:T30"},
    )


INLINE_MODELS = [
    ("demo1_fillna", demo1_fillna),
    ("demo3_groupby", demo3_groupby),
    ("demo5_melted", demo5_melted),
    ("demo6_correlation", demo6_correlation),
]


def test_split_top_level_args_nested():
    assert split_top_level_args("0,1,_xlfn.ANCHORARRAY(A6)") == ["0", "1", "_xlfn.ANCHORARRAY(A6)"]
    assert split_top_level_args("3,1,C4,C5,C6") == ["3", "1", "C4", "C5", "C6"]
    assert split_top_level_args("0,1,'My,Sheet'!A1:B2") == ["0", "1", "'My,Sheet'!A1:B2"]


def test_parse_xlws_py_formula():
    parsed = parse_xlws_py_formula("_xlfn._xlws.PY(0,1,_xlfn.ANCHORARRAY(A6))")
    assert parsed == (0, 1, ["_xlfn.ANCHORARRAY(A6)"])
    parsed2 = parse_xlws_py_formula("_xlfn._xlws.PY(3,0,U8:U15,M8:T15)")
    assert parsed2 == (3, 0, ["U8:U15", "M8:T15"])


def test_rewrite_headers_single():
    code, issues, used, modes = rewrite_excel_code("df = xl(%P2%, headers=True)", num_deps=1)
    assert "data.to_pandas()" in code
    assert "xl(" not in code
    assert used == ["0"]
    assert modes[0] == "true"
    assert not any("dynamic" in i for i in issues)


def test_rewrite_headers_false_preserved_in_mode():
    code, _issues, _used, modes = rewrite_excel_code("x = xl(%P2%, headers=False)", num_deps=1)
    assert "to_pandas(header_row=None)" in code
    assert modes[0] == "false"


def test_rewrite_multi_and_scalar():
    code, issues, used, _modes = rewrite_excel_code(
        "filterDF.groupby(xl(%P2%))[[xl(%P3%)]].agg(xl(%P4%))",
        num_deps=3,
    )
    assert "data" in code and "inputs[1]" in code and "inputs[2]" in code
    assert "xl(" not in code
    assert used == ["0", "1", "2"]


def test_rewrite_rejects_dynamic():
    code, issues, _used, _modes = rewrite_excel_code('df = xl(f"A1:A{n}")', num_deps=0)
    assert any("dynamic" in i for i in issues)
    assert "xl(" in code


def test_rewrite_ignores_xl_in_strings_and_comments():
    src = "a = 'xl(%P2%)'\n# xl(%P3%)\nb = xl(%P2%)\n"
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    assert "a = 'xl(%P2%)'" in code
    assert "# xl(%P3%)" in code
    assert "b = data" in code
    assert used == ["0"]
    assert not any("dynamic" in i for i in issues)


def test_resolve_table_and_anchor():
    model = ExcelWorkbookModel(
        tables={"Table1": "Data!A3:F23"},
        anchor_snapshots={"A6": "A6:B20", "Sheet1!A6": "Sheet1!A6:B20"},
    )
    t = resolve_dep("Table1[#All]", model)
    assert t.a1 == "Data!A3:F23" and t.kind == "table_snapshot"
    a = resolve_dep("_xlfn.ANCHORARRAY(A6)", model)
    assert a.a1 == "A6:B20" and a.kind == "anchor_snapshot"


def test_resolve_whole_column_and_row():
    model = ExcelWorkbookModel()
    assert resolve_dep("A:A", model).kind == "range"
    assert resolve_dep("B:D", model).a1 == "B:D"
    assert resolve_dep("1:10", model).kind == "range"
    assert resolve_dep("Data!A:A", model).a1 == "Data!A:A"


def test_resolve_anchor_fail_closed_without_snapshot():
    model = ExcelWorkbookModel()
    r = resolve_dep("_xlfn.ANCHORARRAY(A6)", model)
    assert r.kind == "unresolved"


@pytest.mark.parametrize("name,factory", INLINE_MODELS, ids=[n for n, _ in INLINE_MODELS])
def test_inline_models_convert_to_dag(name: str, factory):
    model = factory()
    report = convert_model_to_dag(model)
    assert report.ok, (name, report.issues, [(c.cell, c.issues) for c in report.cells if not c.converted])
    assert len(report.cells) == len(model.cells)
    for cell in report.cells:
        assert cell.converted
        assert cell.dag_formula.startswith("=PY("), cell
        assert "%P" not in cell.converted_code, (name, cell.cell, cell.converted_code)


def test_demo1_fillna_specifics():
    report = convert_model_to_dag(demo1_fillna())
    by_cell = {c.cell: c for c in report.cells}
    h4 = by_cell["H4"]
    assert "to_pandas()" in h4.converted_code
    assert h4.data_args == ["A6:B254"]
    assert h4.return_type == 1
    assert "result = None" in h4.converted_code
    assert not h4.ordering_args
    assert by_cell["H5"].ordering_args == ["H4"]
    h6 = by_cell["H6"]
    assert h6.shared_kernel
    assert "merge" in h6.converted_code
    assert h6.ordering_args == ["H5"]


def test_demo3_table_and_scalar_groupby():
    report = convert_model_to_dag(demo3_groupby())
    by_cell = {c.cell: c for c in report.cells}
    c1 = by_cell["C1"]
    assert c1.data_args == ["Data!A1:AA5850"]
    assert "to_pandas()" in c1.converted_code
    c9 = by_cell["C9"]
    assert c9.data_args == ["C4", "C5", "C6"]
    assert c9.ordering_args == ["C3"]
    assert "data" in c9.converted_code and "inputs[2]" in c9.converted_code
    d9 = by_cell["D9"]
    assert d9.data_args == ["D4", "D5", "D6"]
    assert d9.ordering_args == ["C9"]


def test_demo5_table1():
    report = convert_model_to_dag(demo5_melted())
    h1 = next(c for c in report.cells if c.cell == "H1")
    assert h1.data_args == ["Data!A3:F23"]
    h3 = next(c for c in report.cells if c.cell == "H3")
    assert h3.shared_kernel


def test_demo6_multi_range_and_headers_false():
    report = convert_model_to_dag(demo6_correlation())
    by_cell = {c.cell: c for c in report.cells}
    assert by_cell["L2"].data_args == ["A4:I63"]
    n17 = by_cell["N17"]
    assert n17.data_args[:2] == ["U8:U15", "M8:T15"]
    assert n17.ordering_args
    assert "data" in n17.converted_code and "inputs[1]" in n17.converted_code
    ak = by_cell["AK34"]
    assert "data" in ak.converted_code and "inputs[1]" in ak.converted_code
    assert ak.bindings[0].header_mode == "false"


def test_dedup_duplicate_range_bindings():
    model = ExcelWorkbookModel(
        scripts=["x = xl(%P2%) + xl(%P3%)"],
        cells=[_cell("S", "A1", 0, deps=["B1", "B1"], row=1, col=1)],
        sheets=[_sheet("S")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert cell.converted
    assert cell.data_args == ["B1"]
    assert "data[1]" not in cell.converted_code


def test_fail_closed_unresolved_dep():
    model = ExcelWorkbookModel(
        scripts=["df = xl(%P2%, headers=True)"],
        cells=[_cell("S", "A1", 0, deps=["MissingTable[#All]"], row=1, col=1)],
        sheets=[_sheet("S")],
    )
    report = convert_model_to_dag(model)
    assert not report.ok
    assert not report.cells[0].converted
    assert report.cells[0].dag_formula == ""


def test_roundtrip_dag_excel_headers():
    dag_code = "df = data.to_pandas()"
    excel_code, deps, issues = rewrite_dag_code_to_excel(dag_code, ["A3:F23"], header_modes=["true"])
    assert "xl(%P2%, headers=True)" in excel_code
    assert deps == ["A3:F23"]
    assert not issues
    again, _issues2, _used, modes = rewrite_excel_code(excel_code, num_deps=1)
    assert "data.to_pandas()" in again
    assert modes[0] == "true"


def test_reverse_preserves_headers_false_and_return_type():
    formula = rebuild_python_formula_with_data("x = data", ["A1:A2"])
    cell = convert_dag_formula_to_excel(
        formula,
        cell="Z1",
        meta={
            "return_type": 1,
            "data_args": ["A1:A2"],
            "ordering_args": ["Y1"],
            "bindings": [{"a1": "A1:A2", "header_mode": "false", "role": "data", "original_indices": [0]}],
        },
    )
    assert "headers=False" in cell.converted_code
    assert cell.return_type == 1
    assert any("ordering-only" in i for i in cell.issues)
    assert "Y1" not in cell.converted_code


def test_excel_escape_skips_calc_sanitizer():
    code = "x = float(1)"
    assert "+0.0" in escape_code_for_formula(code)
    assert escape_code_for_excel_formula(code) == code
    calc = rebuild_python_formula_with_data(code, [])
    xlsx = rebuild_python_formula_with_data(code, [], separator=",", excel_escape=True)
    assert "+0.0" in calc
    assert "float(1)" in xlsx
    assert xlsx.endswith('")')


def test_roundtrip_via_formula_string():
    formula = rebuild_python_formula_with_data(
        "df = pd.DataFrame(data[1:], columns=data[0])",
        ["A3:F23"],
    )
    cell = convert_dag_formula_to_excel(formula, cell="H1")
    assert "xl(" in cell.converted_code
    assert "A3:F23" in expand_placeholders_to_literals(cell.converted_code, cell.data_args)
    assert cell.excel_formula.startswith('=PY("')


def test_convert_model_api():
    report = convert_model_to_dag(demo5_melted())
    assert report.direction == "dag"
    assert report.ok
    assert report.cells


def _minimal_xlsx_bytes(*, sheet_xml: str, workbook_xml: str, rels_xml: str, scripts_xml: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if scripts_xml:
            zf.writestr("xl/pythonScripts.xml", scripts_xml)
    return buf.getvalue()


def test_ooxml_prefixed_namespace_and_entities(tmp_path: Path):
    """Namespace prefixes + XML entities in formulas must still parse."""
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
<x:workbook xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <x:sheets><x:sheet name="Alpha" sheetId="1" r:id="rId1"/></x:sheets>
</x:workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <x:sheetData>
    <x:row r="1">
      <x:c r="A1"><x:f>_xlfn._xlws.PY(0,0,B1)</x:f></x:c>
      <x:c r="B6"><x:f t="array" ref="B6:C10">IF(1&lt;2,1,0)</x:f></x:c>
    </x:row>
  </x:sheetData>
</x:worksheet>"""
    scripts = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>xl(%P2%)</code></pythonScript>
</pythonScripts>"""
    path = tmp_path / "ns.xlsx"
    path.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet, workbook_xml=workbook, rels_xml=rels, scripts_xml=scripts))
    model = load_excel_model(path, prefer_openpyxl_anchors=False)
    assert model.sheets[0].title == "Alpha"
    assert model.cells[0].deps == ["B1"]
    assert model.scripts[0] == "xl(%P2%)"
    assert "B6:C10" in model.anchor_snapshots.get("B6", "") or "B6:C10" in model.anchor_snapshots.get("Alpha!B6", "")


def _write_synthetic_source_xlsx(path: Path) -> None:
    """Minimal multi-sheet workbook for ``write_dag_formulas_xlsx`` artifact checks."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    data = wb.active
    data.title = "Data"
    data["A1"] = "keep-me"
    pivots = wb.create_sheet("Pivots")
    pivots["C1"] = "placeholder"
    # Spill residue that conversion must clear (anchor H37 keeps the new formula).
    sheet1 = wb.create_sheet("Sheet1")
    sheet1["H37"] = "old-anchor"
    sheet1["H38"] = "spill-residue"
    sheet1["I38"] = "spill-residue"
    wb.save(path)
    wb.close()


def test_write_xlsx_artifact_commas_spill_and_sheets(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    src = tmp_path / "src.xlsx"
    _write_synthetic_source_xlsx(src)

    report = convert_model_to_dag(demo3_groupby())
    assert report.ok
    # Only write the cross-sheet table cell for this artifact check.
    report.cells = [c for c in report.cells if c.cell == "C1"]
    out = tmp_path / "out_pivots.xlsx"
    write_dag_formulas_xlsx(src, report, out)
    wb = openpyxl.load_workbook(out)
    assert "Data" in wb.sheetnames and "Pivots" in wb.sheetnames
    # Short converted C1 stays inline — no py_code_Pivots sheet.
    assert "py_code_Pivots" not in wb.sheetnames
    formula = wb["Pivots"]["C1"].value
    assert isinstance(formula, str)
    assert formula.startswith('=PY("')
    assert "Data!A1:AA5850" in formula
    assert ";" not in formula
    assert wb["Data"]["A1"].value == "keep-me"
    wb.close()

    # Spill cleanup + long script → bank sheet at same A1.
    long_script = "df = data\n" + ("# pad\n" * 250)
    spill_model = ExcelWorkbookModel(
        scripts=[long_script],
        cells=[_cell("Sheet1", "H37", 0, array_ref="H37:I38", row=37, col=8)],
        sheets=[_sheet("Sheet1")],
    )
    spill_report = convert_model_to_dag(spill_model)
    assert spill_report.ok
    assert len(spill_report.cells[0].converted_code) > 1000
    out2 = tmp_path / "out_spill.xlsx"
    write_dag_formulas_xlsx(src, spill_report, out2)
    wb2 = openpyxl.load_workbook(out2)
    assert wb2["Sheet1"]["H38"].value is None
    assert wb2["Sheet1"]["I38"].value is None
    assert isinstance(wb2["Sheet1"]["H37"].value, str)
    assert wb2["Sheet1"]["H37"].value.startswith("=PY(")
    assert "py_code_Sheet1!H37" in wb2["Sheet1"]["H37"].value
    assert wb2["py_code_Sheet1"]["H37"].value == spill_report.cells[0].converted_code
    wb2.close()


def test_write_xlsx_no_silent_first_sheet_fallback(tmp_path: Path):
    src = tmp_path / "src.xlsx"
    _write_synthetic_source_xlsx(src)
    report = convert_model_to_dag(demo1_fillna())
    report.cells[0].sheet = "DefinitelyMissing"
    with pytest.raises(ValueError, match="unmapped sheet"):
        write_dag_formulas_xlsx(src, report, tmp_path / "bad.xlsx")


def test_parse_and_convert_synthetic_workbook_with_table(tmp_path: Path):
    """End-to-end OOXML: scripts, sheet titles, table ownership, ANCHORARRAY snapshot."""
    # Two sheets: Data owns the table; Pivots hosts the PY formula.
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>
  <Override PartName="/xl/pythonScripts.xml" ContentType="application/xml"/>
</Types>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Data" sheetId="1" r:id="rId1"/>
    <sheet name="Pivots" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>"""
    wb_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>"""
    sheet1 = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetData/>
  <tableParts count="1"><tablePart r:id="rId1"/></tableParts>
</worksheet>"""
    sheet1_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/>
</Relationships>"""
    sheet2 = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="C1"><f>_xlfn._xlws.PY(0,1,tradeData[#All])</f></c></row>
    <row r="4"><c r="H4"><f>_xlfn._xlws.PY(1,1,_xlfn.ANCHORARRAY(A6))</f></c></row>
    <row r="6"><c r="A6"><f t="array" ref="A6:B10">1</f></c></row>
  </sheetData>
</worksheet>"""
    table = """<?xml version="1.0" encoding="UTF-8"?>
<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="1" name="tradeData" displayName="tradeData" ref="A1:AA10"/>"""
    scripts = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>df = xl(%P2%, headers=True)</code></pythonScript>
  <pythonScript><code>df2 = xl(%P2%, headers=True)</code></pythonScript>
</pythonScripts>"""
    path = tmp_path / "synthetic.xlsx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
        zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet1_rels)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2)
        zf.writestr("xl/tables/table1.xml", table)
        zf.writestr("xl/pythonScripts.xml", scripts)

    model = load_excel_model(path, prefer_openpyxl_anchors=False)
    assert [s.title for s in model.sheets] == ["Data", "Pivots"]
    assert model.tables["tradeData"] == "Data!A1:AA10"
    assert "A6:B10" in model.anchor_snapshots.get("A6", "") or "A6:B10" in model.anchor_snapshots.get("Pivots!A6", "")
    report = convert_model_to_dag(model)
    assert report.ok, [(c.cell, c.issues) for c in report.cells if not c.converted]
    by_cell = {c.cell: c for c in report.cells}
    assert by_cell["C1"].data_args == ["Data!A1:AA10"]
    assert by_cell["H4"].data_args[0].endswith("A6:B10") or "A6:B10" in by_cell["H4"].data_args[0]


def test_libreoffice_import_smoke(tmp_path: Path):
    """When soffice is available, a converted synthetic XLSX must open without crash."""
    import shutil
    import subprocess

    if not shutil.which("soffice"):
        pytest.skip("soffice not on PATH")
    src = tmp_path / "src.xlsx"
    _write_synthetic_source_xlsx(src)
    report = convert_model_to_dag(demo5_melted())
    out = tmp_path / "converted.xlsx"
    write_dag_formulas_xlsx(src, report, out)
    cmd = [
        "soffice",
        "--headless",
        "--convert-to",
        "csv",
        "--outdir",
        str(tmp_path),
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert any(tmp_path.glob("*.csv"))
