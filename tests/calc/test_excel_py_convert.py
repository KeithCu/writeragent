# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Excel ↔ DAG-style =PY conversion (inline models + synthetic OOXML)."""

from __future__ import annotations

import ast
import io
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from plugin.calc.excel_py_convert.auto_open import has_excel_python_xlsx
from plugin.calc.excel_py_convert.convert import convert_to_excel
from plugin.calc.excel_py_convert.models import (
    ConvertedCell,
    ConversionReport,
    ExcelPyCell,
    ExcelWorkbookModel,
    SheetInfo,
)
from plugin.calc.excel_py_convert.ooxml_util import (
    local_name,
    parse_and_extract_namespaces,
    serialize_xml_preserving_namespaces,
    xml_namespace_lock,
)
from plugin.calc.excel_py_convert.parse_excel_ooxml import (
    a1_span_bounds,
    load_excel_model,
    parse_xlws_py_formula,
    shift_xlws_py_dep,
    split_top_level_args,
)
from plugin.calc.excel_py_convert.resolve_refs import resolve_dep
from plugin.calc.excel_py_convert.script_bank import (
    formula_for_converted_cell,
)
from plugin.calc.excel_py_convert.to_dag import (
    _OBJECT_SUPPRESS,
    _prefer_excel_dep_token,
    _skip_string,
    analyze_xl_calls,
    apply_rewrite,
    convert_cell_to_dag,
    convert_model_to_dag,
)
from plugin.calc.excel_py_convert.to_excel import (
    assign_script_bank,
    convert_dag_formula_to_excel,
    rewrite_dag_code_to_excel,
    xlws_py_formula,
)
from plugin.calc.excel_py_convert.xlsx_write import (
    python_scripts_xml,
    write_dag_formulas_xlsx,
    write_excel_python_xlsx,
)
from plugin.calc.python.formula_edit import (
    escape_code_for_formula,
    rebuild_python_formula_with_data,
)


def _excel_rewrite(code: str, *, num_deps: int) -> tuple[str, object]:
    src = code or ""
    analysis = analyze_xl_calls(src, num_deps=num_deps)
    if analysis.fatal:
        return src, analysis
    return apply_rewrite(src, analysis.calls, index_map={}), analysis


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
    code, analysis = _excel_rewrite("df = xl(%P2%, headers=True)", num_deps=1)
    assert 'xl("%P2%",headers=True)' in code
    assert "to_pandas" not in code
    assert analysis.header_modes[0] == "true"
    assert not any("dynamic" in i for i in analysis.issues)


def test_rewrite_headers_false_preserved_in_mode():
    code, analysis = _excel_rewrite("x = xl(%P2%, headers=False)", num_deps=1)
    assert 'xl("%P2%",headers=False)' in code
    assert analysis.header_modes[0] == "false"


def test_rewrite_multi_and_scalar():
    code, _ = _excel_rewrite(
        "filterDF.groupby(xl(%P2%))[[xl(%P3%)]].agg(xl(%P4%))",
        num_deps=3,
    )
    assert 'xl("%P2%")' in code and 'xl("%P3%")' in code and 'xl("%P4%")' in code


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('df = xl(f"A1:A{n}")', id="test_rewrite_rejects_dynamic"),
        pytest.param('x = xl("A1")', id="test_rewrite_assignment_literal_xl_still_dynamic"),
    ],
)
def test_rewrite_rejects_dynamic(value):
    code, analysis = _excel_rewrite(value, num_deps=0)
    assert any("dynamic" in i for i in analysis.issues)
    assert "xl(" in code


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4",
    [
        pytest.param("a = 'xl(%P2%)'\n# xl(%P3%)\nb = xl(%P2%)\n", "a = 'xl(%P2%)'", "# xl(%P3%)", 'b = xl("%P2%")', id="test_rewrite_ignores_xl_in_strings_and_comments"),
        pytest.param('a = "say \\"xl(%P3%)\\" please"\n'
        'b = """xl(%P4%)\nstill string"""\n'
        "c = xl(%P2%)\n", 'xl(%P3%)', 'xl(%P4%)', 'c = xl("%P2%")', id="test_rewrite_ignores_xl_in_escaped_and_triple_quoted_strings"),
    ],
)
def test_rewrite_ignores_xl_in_strings_and_comments(value, value_2, value_3, value_4):
    src = value
    code, analysis = _excel_rewrite(src, num_deps=1)
    assert value_2 in code
    assert value_3 in code
    assert value_4 in code
    assert not any("dynamic" in i for i in analysis.issues)


def test_rewrite_quoted_placeholder_constant():
    """Quoted ``xl("%P2%")`` is valid Python; AST Constant path must still bind."""
    code, analysis = _excel_rewrite('df = xl("%P2%", headers=True)', num_deps=1)
    assert 'xl("%P2%",headers=True)' in code
    assert analysis.header_modes[0] == "true"
    assert not any("dynamic" in i for i in analysis.issues)


def test_rewrite_non_ascii_prefix_offsets():
    """UTF-8 multi-byte prefix must not shift the ``xl(...)`` rewrite window."""
    src = "café = 1\ndf = xl(%P2%, headers=True)\n"
    code, analysis = _excel_rewrite(src, num_deps=1)
    assert "café = 1" in code
    assert 'df = xl("%P2%",headers=True)' in code
    assert not any("dynamic" in i for i in analysis.issues)


def test_rewrite_ignores_attribute_xl_calls():
    """``obj.xl(...)`` is not the Excel data-bridge builtin — leave it alone."""
    src = "x = obj.xl(existing)\ny = xl(%P2%)\n"
    code, analysis = _excel_rewrite(src, num_deps=1)
    assert "obj.xl(existing)" in code
    assert 'y = xl("%P2%")' in code
    assert not any("dynamic" in i for i in analysis.issues)


def test_rewrite_attribute_xl_with_placeholder_is_fatal():
    """``obj.xl(%P2%)`` has placeholder outside xl(); fatal and returns source unchanged."""
    src = "x = obj.xl(%P2%)"
    code, analysis = _excel_rewrite(src, num_deps=1)
    assert analysis.fatal is True
    assert code == src
    assert any("outside xl()" in i for i in analysis.issues)


def test_rewrite_keeps_xl_statement_under_if():
    """Statement ``xl`` under ``if`` stays as a quoted binding (no strip/pass)."""
    src = "if config_param == 3:\n    xl(%P2%)\n"
    code, analysis = _excel_rewrite(src, num_deps=1)
    assert 'xl("%P2%")' in code
    assert "pass" not in code
    assert not any("dynamic" in i for i in analysis.issues)
    ast.parse(code)


def test_rewrite_literal_xl_statement_fail_closed():
    """Literal statement ``xl("A1")`` under ``if`` is dynamic — not silently stripped."""
    src = 'if config_param == 3:\n    xl("A1")\n'
    code, analysis = _excel_rewrite(src, num_deps=0)
    assert 'xl("A1")' in code
    assert any("dynamic" in i for i in analysis.issues)


def test_rewrite_keeps_xl_statement_with_siblings():
    src = "if True:\n    xl(%P2%)\n    x = 1\n"
    code, _ = _excel_rewrite(src, num_deps=1)
    assert 'xl("%P2%")' in code
    assert "x = 1" in code
    ast.parse(code)


def test_rewrite_top_level_xl_still_egress_binding():
    """Sole / last top-level ``xl(%P2%)`` remains Jupyter last-expression → ``xl("%P2%")``."""
    code, analysis = _excel_rewrite("xl(%P2%)", num_deps=1)
    assert code.strip() == 'xl("%P2%")'
    assert not any("dynamic" in i for i in analysis.issues)


def test_rewrite_multiline_xl_statement_quoted():
    src = "if True:\n    xl(\n        %P2%,\n        headers=True,\n    )\n"
    code, _ = _excel_rewrite(src, num_deps=1)
    assert 'xl("%P2%",headers=True)' in code
    assert "pass" not in code
    ast.parse(code)


def test_convert_xl_statement_cell_keeps_binding():
    model = ExcelWorkbookModel(
        scripts=["if True:\n    xl(%P2%)\n"],
        cells=[_cell("S", "A1", 0, deps=["B1"], row=1, col=1)],
        sheets=[_sheet("S")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert cell.converted
    assert 'xl("%P2%")' in cell.converted_code
    assert "pass" not in cell.converted_code


def test_convert_literal_xl_statement_fail_closed():
    model = ExcelWorkbookModel(
        scripts=['if True:\n    xl("A1")\n'],
        cells=[_cell("S", "A1", 0, deps=[], row=1, col=1)],
        sheets=[_sheet("S")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert not cell.converted
    assert any("dynamic" in i for i in cell.issues)

def test_rewrite_syntax_error_with_placeholder_fail_closed():
    """Malformed scripts fail closed even when they contain ``%Pn%`` (no regex guess)."""
    code, analysis = _excel_rewrite("df = xl(%P2%, headers=True\n", num_deps=1)
    assert any("syntax error" in i for i in analysis.issues)
    assert "xl(%P2%" in code  # unchanged


def test_convert_syntax_error_fail_closed():
    model = ExcelWorkbookModel(
        scripts=["df = xl(%P2%, headers=True"],
        cells=[_cell("S", "A1", 0, deps=["B1"], row=1, col=1)],
        sheets=[_sheet("S")],
    )
    report = convert_model_to_dag(model)
    assert not report.cells[0].converted
    assert report.cells[0].dag_formula == ""
    assert any("syntax error" in i for i in report.cells[0].issues)


def test_resolve_table_and_anchor():
    model = ExcelWorkbookModel(
        tables={"Table1": "Data!A3:F23"},
        anchor_snapshots={"A6": "A6:B20", "Sheet1!A6": "Sheet1!A6:B20"},
    )
    t = resolve_dep("Table1[#All]", model)
    assert t.a1 == "Data!A3:F23" and t.kind == "table_snapshot"
    assert resolve_dep("TABLE1[#All]", model).a1 == "Data!A3:F23"
    a = resolve_dep("_xlfn.ANCHORARRAY(A6)", model)
    assert a.a1 == "A6:B20" and a.kind == "anchor_snapshot"


def test_resolve_quoted_sheet_name_with_apostrophe():
    model = ExcelWorkbookModel()
    r = resolve_dep("'O''Brien'!A1", model)
    assert r.kind == "range" and r.a1 == "'O''Brien'!A1"
    assert resolve_dep("'O''Brien'!A1:B2", model).a1 == "'O''Brien'!A1:B2"
    assert resolve_dep("''!A1", model).kind == "unresolved"


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
        # Runnable DAG keeps xl("%Pn%") bindings (sandbox resolves them).
        if "xl(" in (cell.original_code or ""):
            assert 'xl("%P' in cell.converted_code or "xl('%P" in cell.converted_code, (
                name,
                cell.cell,
                cell.converted_code,
            )


def test_demo1_fillna_specifics():
    report = convert_model_to_dag(demo1_fillna())
    by_cell = {c.cell: c for c in report.cells}
    h4 = by_cell["H4"]
    assert 'xl("%P2%",headers=True)' in h4.converted_code
    assert h4.data_args == ["A6:B254"]
    assert h4.excel_deps == ["_xlfn.ANCHORARRAY(A6)"]
    assert h4.return_type == 1
    assert "result = None" in h4.converted_code
    assert not h4.ordering_args
    assert by_cell["H5"].excel_deps == ["_xlfn.ANCHORARRAY(D6)"]
    assert not by_cell["H5"].ordering_args
    assert "H4" not in (by_cell["H5"].dag_formula or "")
    h6 = by_cell["H6"]
    assert h6.shared_kernel
    assert "merge" in h6.converted_code
    assert not h6.ordering_args
    assert "H5" not in (h6.dag_formula or "")
    assert any("does not add order edges" in i for i in h6.issues)

def test_demo3_table_and_scalar_groupby():
    report = convert_model_to_dag(demo3_groupby())
    by_cell = {c.cell: c for c in report.cells}
    c1 = by_cell["C1"]
    assert c1.data_args == ["Data!A1:AA5850"]
    assert c1.excel_deps == ["tradeData[#All]"]
    assert 'xl("%P2%",headers=True)' in c1.converted_code
    c9 = by_cell["C9"]
    assert c9.data_args == ["C4", "C5", "C6"]
    assert not c9.ordering_args
    assert "C3" not in (c9.dag_formula or "")
    assert 'xl("%P2%")' in c9.converted_code and 'xl("%P4%")' in c9.converted_code
    d9 = by_cell["D9"]
    assert d9.data_args == ["D4", "D5", "D6"]
    assert not d9.ordering_args
    assert "C9" not in (d9.dag_formula or "")


def test_demo5_table1():
    report = convert_model_to_dag(demo5_melted())
    h1 = next(c for c in report.cells if c.cell == "H1")
    assert h1.data_args == ["Data!A3:F23"]
    assert h1.excel_deps == ["Table1[#All]"]
    h3 = next(c for c in report.cells if c.cell == "H3")
    assert h3.shared_kernel


def test_excel_deps_roundtrip_on_xlws_export():
    """Table/ANCHORARRAY tokens are restored on _xlws.PY; Calc still uses A1 snapshots.

    Policy: EXCEL_DEP_TOKEN_FIDELITY / models.py module doc.
    """
    from plugin.calc.excel_py_convert.models import EXCEL_DEP_TOKEN_FIDELITY
    from plugin.calc.excel_py_convert.to_excel import convert_dag_report_to_excel, deps_for_xlws_export

    assert "round-trip fidelity" in EXCEL_DEP_TOKEN_FIDELITY
    dag = convert_model_to_dag(demo3_groupby())
    excel = convert_dag_report_to_excel(dag)
    c1 = next(c for c in excel.cells if c.cell == "C1")
    assert c1.data_args == ["Data!A1:AA5850"]
    assert deps_for_xlws_export(c1) == ["tradeData[#All]"]
    assert "tradeData[#All]" in c1.excel_formula

def test_demo6_multi_range_and_headers_false():
    report = convert_model_to_dag(demo6_correlation())
    by_cell = {c.cell: c for c in report.cells}
    assert by_cell["L2"].data_args == ["A4:I63"]
    n17 = by_cell["N17"]
    assert n17.data_args[:2] == ["U8:U15", "M8:T15"]
    assert not n17.ordering_args
    assert 'xl("%P2%")' in n17.converted_code and 'xl("%P3%")' in n17.converted_code
    ak = by_cell["AK34"]
    assert 'xl("%P2%",headers=False)' in ak.converted_code and 'xl("%P3%")' in ak.converted_code
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
    # Both sites remap onto the single binding.
    assert cell.converted_code.count('xl("%P2%")') == 2
    assert "%P3%" not in cell.converted_code


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
    """Legacy ``data.to_pandas()`` still reverses; forward path keeps ``xl("%Pn%")``."""
    dag_code = "df = data.to_pandas()"
    excel_code, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A3:F23"], header_modes=["true"])
    assert not fatal
    assert "xl(%P2%,headers=True)" in excel_code
    assert deps == ["A3:F23"]
    assert not issues
    again_code, again_analysis = _excel_rewrite(excel_code, num_deps=1)
    assert 'xl("%P2%",headers=True)' in again_code
    assert again_analysis.header_modes[0] == "true"


def test_roundtrip_multi_dep_data_and_ranges():
    """``data[i]`` / ``ranges[i]`` reverse to ``xl(%Pn%)``; re-import keeps ``xl("%Pn%")``."""
    dag_code = "a = data[0]\nb = ranges[1]\nc = data[2].to_pandas()\n"
    deps = ["A1:A2", "B1:B2", "C1:C2"]
    excel_code, out_deps, issues, fatal = rewrite_dag_code_to_excel(
        dag_code, deps, header_modes=["omit", "omit", "true"]
    )
    assert not fatal
    assert out_deps == deps
    assert "xl(%P2%)" in excel_code
    assert "xl(%P3%)" in excel_code
    assert "xl(%P4%,headers=True)" in excel_code
    assert "ranges[" not in excel_code
    assert not any("ambiguous" in i for i in issues)
    again_code, again_analysis = _excel_rewrite(excel_code, num_deps=3)
    assert 'xl("%P2%")' in again_code and 'xl("%P3%")' in again_code and 'xl("%P4%",headers=True)' in again_code
    assert again_analysis.header_modes[2] == "true"


def test_export_passthrough_quoted_xl_bindings():
    """DAG code that already has ``xl("%Pn%")`` unquotes for the Excel package."""
    dag_code = 'df = xl("%P2%",headers=True)\nx = xl("%P3%")\n'
    excel_code, deps, issues, fatal = rewrite_dag_code_to_excel(
        dag_code, ["A1:B2", "C1"], header_modes=["true", "omit"]
    )
    assert not fatal
    assert deps == ["A1:B2", "C1"]
    assert "xl(%P2%,headers=True)" in excel_code
    assert "xl(%P3%)" in excel_code
    assert '"%P' not in excel_code
    assert not issues


def test_reverse_non_ascii_prefix_offsets():
    """UTF-8 multi-byte prefix must not shift reverse AST rewrite of ``data`` / ``ranges``."""
    dag_code = "café = 1\nx = data[0]\ny = ranges[1]\n"
    excel_code, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1", "B1"], header_modes=["omit", "omit"])
    assert not fatal
    assert "café = 1" in excel_code
    assert "x = xl(%P2%)" in excel_code
    assert "y = xl(%P3%)" in excel_code
    assert "ranges[" not in excel_code
    assert deps == ["A1", "B1"]
    assert not any("ambiguous" in i for i in issues)


def test_export_legacy_patterns_in_strings_and_comments():
    dag_code = (
        's1 = "data.to_pandas()"\n'
        "s2 = 'data[0]'\n"
        's3 = """pd.DataFrame(data[1:], columns=data[0])"""\n'
        "# data.to_pandas()\n"
        "# data[0]\n"
        "x = data[0]\n"
        "y = data.to_pandas()\n"
    )
    res, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1:B2"])
    assert not fatal
    assert 's1 = "data.to_pandas()"' in res
    assert "s2 = 'data[0]'" in res
    assert 's3 = """pd.DataFrame(data[1:], columns=data[0])"""' in res
    assert "# data.to_pandas()" in res
    assert "# data[0]" in res
    assert "x = xl(%P2%)" in res
    assert "y = xl(%P2%,headers=True)" in res


def test_export_quoted_xl_tokens_in_strings_and_comments():
    dag_code = (
        's1 = \'xl("%P2%")\'\n'
        's2 = """xl("%P2%")"""\n'
        '# xl("%P2%")\n'
        'x = xl("%P2%", headers=True)\n'
    )
    res, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1"])
    assert not fatal
    assert 's1 = \'xl("%P2%")\'' in res
    assert 's2 = """xl("%P2%")"""' in res
    assert '# xl("%P2%")' in res
    assert "x = xl(%P2%,headers=True)" in res


def test_export_divert_comment_with_xl_and_legacy_call():
    dag_code = '# xl("%P2%")\nx = data.to_pandas()\n'
    res, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1"])
    assert not fatal
    assert '# xl("%P2%")' in res
    assert "x = xl(%P2%,headers=True)" in res


def test_export_fstrings_expression_vs_literal():
    dag_code = 's = f"data.to_pandas()"\nexpr = f"{data.to_pandas()}"\n'
    res, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1"])
    assert not fatal
    assert 's = f"data.to_pandas()"' in res
    assert 'expr = f"{xl(%P2%,headers=True)}"' in res


def test_export_unparseable_code_fails_closed():
    dag_code = "data[0] = ("
    res, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1"])
    assert fatal is True
    assert res == dag_code
    assert any("syntax error" in i.lower() for i in issues)


def test_export_object_suppress_exact_trailer_only():
    dag_code = "x = data[0]" + _OBJECT_SUPPRESS
    res, deps, issues, fatal = rewrite_dag_code_to_excel(dag_code, ["A1"])
    assert not fatal
    assert res == "x = xl(%P2%)"

    inside_str = f's = """{_OBJECT_SUPPRESS}"""\nresult = None\n'
    res2, deps2, issues2, fatal2 = rewrite_dag_code_to_excel(inside_str, ["A1"])
    assert not fatal2
    assert res2 == inside_str




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


def test_convert_dag_report_preserves_return_type_drops_legacy_ordering():
    """Legacy reports may still carry ordering_args; export must not put them on _xlws.PY."""
    from plugin.calc.excel_py_convert.models import BindingInfo, ConvertedCell, ConversionReport
    from plugin.calc.excel_py_convert.to_excel import convert_dag_report_to_excel, xlws_py_formula

    dag = ConversionReport(
        direction="dag",
        cells=[
            ConvertedCell(
                sheet="Sheet1",
                cell="H5",
                direction="dag",
                original_code="x = data.to_pandas()",
                converted_code="x = data.to_pandas()",
                data_args=["A1:B2"],
                ordering_args=["H4"],
                bindings=[BindingInfo(a1="A1:B2", header_mode="true", role="data", original_indices=[0])],
                return_type=1,
                converted=True,
                array_ref="H5:I10",
            )
        ],
    )
    excel = convert_dag_report_to_excel(dag)
    assert excel.ok
    c = excel.cells[0]
    assert c.return_type == 1
    assert c.data_args == ["A1:B2"]
    assert c.ordering_args == ["H4"]
    assert "H4" not in xlws_py_formula(c.script_index, c.return_type, c.data_args)
    assert "xl(%P2%,headers=True)" in c.converted_code
    assert c.excel_formula.startswith("=_xlfn._xlws.PY(0,1,")


def test_dag_meta_payload_roundtrip():
    from plugin.calc.excel_py_convert.convert import (
        cell_meta_key,
        dag_report_to_meta_payload,
        load_dag_meta_from_doc,
        store_dag_meta_on_doc,
    )
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
    from unittest.mock import MagicMock, patch

    report = ConversionReport(
        direction="dag",
        cells=[
            ConvertedCell(
                sheet="S",
                cell="A1",
                direction="dag",
                original_code="",
                converted_code="data",
                data_args=["B1"],
                ordering_args=["Z9"],
                return_type=1,
                converted=True,
            )
        ],
    )
    payload = dag_report_to_meta_payload(report)
    assert cell_meta_key("S", "A1") in payload
    assert payload[cell_meta_key("S", "A1")]["return_type"] == 1
    assert payload[cell_meta_key("S", "A1")]["ordering_args"] == ["Z9"]

    doc = MagicMock()
    with patch("plugin.doc.udprops.set_document_property") as set_prop:
        store_dag_meta_on_doc(doc, report)
        set_prop.assert_called_once()
        name, value = set_prop.call_args[0][1], set_prop.call_args[0][2]
        assert name == "ExcelPyDagMeta"
        assert "return_type" in value

    with patch("plugin.doc.udprops.get_document_property", return_value=value):
        loaded = load_dag_meta_from_doc(doc)
        assert loaded[cell_meta_key("S", "A1")]["data_args"] == ["B1"]


def test_excel_escape_skips_calc_sanitizer():
    code = "x = float(1)"
    assert escape_code_for_formula(code) == code
    calc = rebuild_python_formula_with_data(code, [])
    xlsx = rebuild_python_formula_with_data(code, [], separator=",", excel_escape=True)
    assert "float(1)" in calc
    assert "float(1)" in xlsx
    assert xlsx.endswith('")')


def test_roundtrip_via_formula_string():
    formula = rebuild_python_formula_with_data(
        "df = pd.DataFrame(data[1:], columns=data[0])",
        ["A3:F23"],
    )
    cell = convert_dag_formula_to_excel(formula, cell="H1")
    assert "xl(" in cell.converted_code
    assert cell.excel_formula.startswith("=_xlfn._xlws.PY(")
    assert cell.script_index == 0


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
    model = load_excel_model(path)
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

    model = load_excel_model(path)
    assert [s.title for s in model.sheets] == ["Data", "Pivots"]
    assert model.tables["tradeData"] == "Data!A1:AA10"
    assert "A6:B10" in model.anchor_snapshots.get("A6", "") or "A6:B10" in model.anchor_snapshots.get("Pivots!A6", "")
    report = convert_model_to_dag(model)
    assert report.ok, [(c.cell, c.issues) for c in report.cells if not c.converted]
    by_cell = {c.cell: c for c in report.cells}
    assert by_cell["C1"].data_args == ["Data!A1:AA10"]
    assert by_cell["H4"].data_args[0].endswith("A6:B10") or "A6:B10" in by_cell["H4"].data_args[0]


def test_parse_ooxml_malformed_xml_parts_recorded_in_issues(tmp_path: Path):
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
  </sheetData>
</worksheet>"""
    table = """<?xml version="1.0" encoding="UTF-8"?>
<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="1" name="tradeData" displayName="tradeData" ref="A1:AA10"/>"""
    scripts = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>df = xl(%P2%, headers=True)</code></pythonScript>
</pythonScripts>"""

    # (a) replace xl/worksheets/sheet2.xml with malformed XML: sheet1's table still parses
    path_corrupt_sheet = tmp_path / "corrupt_sheet2.xlsx"
    with zipfile.ZipFile(path_corrupt_sheet, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
        zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet1_rels)
        zf.writestr("xl/worksheets/sheet2.xml", b"<worksheet")
        zf.writestr("xl/tables/table1.xml", table)
        zf.writestr("xl/pythonScripts.xml", scripts)

    model1 = load_excel_model(path_corrupt_sheet)
    assert model1.tables.get("tradeData") == "Data!A1:AA10"
    assert any("worksheets/sheet2.xml:" in iss for iss in model1.issues)

    # (b) malformed scripts_xml yields model.scripts == [] plus xl/pythonScripts.xml: issue
    path_corrupt_scripts = tmp_path / "corrupt_scripts.xlsx"
    path_corrupt_scripts.write_bytes(_minimal_xlsx_bytes(
        sheet_xml="""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>""",
        workbook_xml=wb_xml,
        rels_xml=wb_rels,
        scripts_xml="<pythonScripts",
    ))
    model2 = load_excel_model(path_corrupt_scripts)
    assert model2.scripts == []
    assert any("xl/pythonScripts.xml:" in iss for iss in model2.issues)

    # (c) corrupt xl/tables/table1.xml: issue names table part, tables is empty
    path_corrupt_table = tmp_path / "corrupt_table.xlsx"
    with zipfile.ZipFile(path_corrupt_table, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
        zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet1_rels)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2)
        zf.writestr("xl/tables/table1.xml", b"<table")
        zf.writestr("xl/pythonScripts.xml", scripts)

    model3 = load_excel_model(path_corrupt_table)
    assert model3.tables == {}
    assert any("xl/tables/table1.xml:" in iss for iss in model3.issues)


@pytest.mark.slow
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
    profile = tmp_path / "lo-profile"
    profile.mkdir()
    cmd = [
        "soffice",
        "--headless",
        "--norestore",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        "csv",
        "--outdir",
        str(tmp_path),
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert any(tmp_path.glob("*.csv"))


def test_xlws_and_python_scripts_builders():
    assert xlws_py_formula(0, 1, ["A1:B2", "Sheet1.C1"]) == "_xlfn._xlws.PY(0,1,A1:B2,Sheet1!C1)"
    raw = python_scripts_xml(["df = xl(%P2%, headers=True)", "x = 1"])
    text = raw.decode("utf-8")
    assert 'xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript"' in text
    assert "df = xl(%P2%, headers=True)" in text
    assert "&lt;" not in text  # no accidental double-escape of plain code


def test_python_scripts_xml_escapes_text_nodes():
    """Script bodies with &, <, > must be entity-escaped in pythonScripts.xml."""
    raw = python_scripts_xml(["a = 1 & 2\nif x < 3:\n    y = x > 0"])
    text = raw.decode("utf-8")
    assert "a = 1 &amp; 2" in text
    assert "if x &lt; 3:" in text
    assert "y = x &gt; 0" in text
    assert "1 & 2" not in text
    assert "x < 3" not in text


def test_assign_script_bank_dedupes():
    from plugin.calc.excel_py_convert.models import ConvertedCell

    a = ConvertedCell(
        sheet="S", cell="A1", direction="excel", original_code="", converted_code="x = data", data_args=["A1"], converted=True
    )
    b = ConvertedCell(
        sheet="S", cell="B1", direction="excel", original_code="", converted_code="x = data", data_args=["B1"], converted=True
    )
    c = ConvertedCell(
        sheet="S", cell="C1", direction="excel", original_code="", converted_code="y = data", data_args=["C1"], converted=True
    )
    scripts = assign_script_bank([a, b, c])
    assert scripts == ["x = data", "y = data"]
    assert a.script_index == b.script_index == 0
    assert c.script_index == 1
    assert "_xlws.PY(0," in a.excel_formula


def _dag_xlsx_bytes() -> bytes:
    """Minimal DAG =PY workbook (stdlib only) for native Excel export tests."""
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    # Quoted quotes in OOXML formula: "" inside the attribute-free <f> text.
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1"><f>PY(&quot;df = data.to_pandas()&quot;,B1:C2)</f></c>
      <c r="B1"><v>1</v></c>
    </row>
  </sheetData>
</worksheet>"""
    return _minimal_xlsx_bytes(sheet_xml=sheet, workbook_xml=workbook, rels_xml=rels)


def test_write_excel_python_xlsx_roundtrip(tmp_path: Path):
    src = tmp_path / "dag.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    report = convert_to_excel(src)
    assert any(c.converted for c in report.cells), report.to_dict()
    out = tmp_path / "excel_native.xlsx"
    write_excel_python_xlsx(src, report, out)
    assert has_excel_python_xlsx(out)
    model = load_excel_model(out)
    assert model.scripts
    assert "xl(%P2%" in model.scripts[0]
    assert model.cells
    assert model.cells[0].deps
    again = convert_model_to_dag(model)
    assert again.ok
    assert 'xl("%P2%"' in again.cells[0].converted_code


def test_cli_excel_write_xlsx(tmp_path: Path):
    from plugin.calc.excel_py_convert.cli import main

    src = tmp_path / "dag.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    out = tmp_path / "out.xlsx"
    rc = main([str(src), "--to", "excel", "--write-xlsx", str(out)])
    assert rc == 0
    assert has_excel_python_xlsx(out)


def test_single_line_rels_survive_strip():
    """Single-line .rels from Excel/LO must survive _strip_rels_python."""
    from xml.etree import ElementTree as ET
    from plugin.calc.excel_py_convert.xlsx_write import _strip_rels_python

    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"

    # Minified single-line rels as emitted by Excel / LibreOffice
    single_line_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{rel_ns}">'
        f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2022/relationships/pythonScript" Target="pythonScripts.xml"/>'
        f'</Relationships>'
    )

    res = _strip_rels_python(single_line_rels.encode("utf-8"))
    rels_root = ET.fromstring(res)
    rel_targets = [
        c.attrib.get("Target")
        for c in rels_root
        if c.tag.endswith("Relationship")
    ]
    assert "worksheets/sheet1.xml" in rel_targets
    assert "pythonScripts.xml" not in rel_targets


def test_write_excel_python_xlsx_single_line_content_types(tmp_path: Path):
    """write_excel_python_xlsx must preserve entries in single-line [Content_Types].xml."""
    from xml.etree import ElementTree as ET
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport

    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    # Single line [Content_Types].xml with sheet1 and a py_code sheet to be dropped
    single_line_ct = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{ct_ns}">'
        f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        f'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        f'</Types>'
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
        '<sheet name="py_code_Sheet1" sheetId="2" r:id="rId2"/>'
        '</sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '</Relationships>'
    )
    ws1 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><f>PY("x = 1")</f></c></row></sheetData>'
        '</worksheet>'
    )
    from plugin.calc.excel_py_convert.script_bank import CODE_SHEET_NOTE
    ws2 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1"><is><t>x = 1</t></is></c><c r="ZZ1"><is><t>'
        + CODE_SHEET_NOTE
        + '</t></is></c></row>'
        '</sheetData>'
        '</worksheet>'
    )

    src = tmp_path / "single_line_in.xlsx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", single_line_ct)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", ws1)
        zf.writestr("xl/worksheets/sheet2.xml", ws2)

    cell = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="excel",
        original_code="x = 1",
        converted_code="x = 1",
        data_args=[],
        converted=True,
        script_index=0,
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "single_line_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ct_raw = zf.read("[Content_Types].xml").decode("utf-8")
        assert "ns0:" not in ct_raw
        ct_root = ET.fromstring(ct_raw)
        overrides = {
            c.attrib.get("PartName"): c.attrib.get("ContentType")
            for c in ct_root
            if c.tag.endswith("Override")
        }
        assert "/xl/workbook.xml" in overrides
        assert "/xl/worksheets/sheet1.xml" in overrides
        assert "/xl/pythonScripts.xml" in overrides
        assert "/xl/worksheets/sheet2.xml" not in overrides


def test_cell_string_value_inlinestr():
    """_cell_string_value must match both t='inlineStr' and t='inlinestr'."""
    from xml.etree import ElementTree as ET
    from plugin.calc.excel_py_convert.parse_dag_formulas import _cell_string_value

    c1 = ET.fromstring('<c r="A1" t="inlineStr"><is><t>code string 1</t></is></c>')
    assert _cell_string_value(c1, []) == "code string 1"

    c2 = ET.fromstring('<c r="A2" t="inlinestr"><is><t>code string 2</t></is></c>')
    assert _cell_string_value(c2, []) == "code string 2"

    c3 = ET.fromstring('<c r="A3" t="inlineStr"><is><t>part A</t><t> and part B</t></is></c>')
    assert _cell_string_value(c3, []) == "part A and part B"

    c_empty = ET.fromstring('<c r="A4" t="inlineStr"/>')
    assert _cell_string_value(c_empty, []) == ""


def test_no_double_xml_unescape():
    """Formulas containing literal &lt; or &amp; must not be double unescaped."""
    from xml.etree import ElementTree as ET
    from plugin.calc.excel_py_convert.parse_dag_formulas import _sheet_cell_map
    from plugin.calc.excel_py_convert.parse_excel_ooxml import _iter_py_cells

    # An OOXML worksheet with a formula containing XML entities for '<' and '&'
    # ET automatically unescapes &lt; to < and &amp; to &
    # If code contained a string literal with '&lt;', in OOXML it is written as '&amp;lt;'
    # which ET decodes to '&lt;'. Double unescape would corrupt '&lt;' to '<'.
    ws_xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1">'
        '<c r="A1"><f>_xlfn._xlws.PY(0,0,&quot;&amp;lt;tag&amp;gt;&quot;)</f></c>'
        '<c r="A2"><f>PY(&quot;x = 1 &lt; 2 and s == &apos;&amp;lt;&apos;&quot;)</f></c>'
        '</row>'
        '</sheetData>'
        '</worksheet>'
    )
    ws_root = ET.fromstring(ws_xml)

    cells = _iter_py_cells(ws_root, "Sheet1")
    assert len(cells) == 1
    # Literal &lt; must remain &lt;, not converted to <
    assert "&lt;tag&gt;" in cells[0].formula_raw

    cell_map = _sheet_cell_map(ws_root, [])
    formula_a2, _ = cell_map["A2"]
    # < was &lt; so decoded to < once, and &lt; was &amp;lt; so decoded to &lt;
    assert "1 < 2" in formula_a2
    assert "&lt;" in formula_a2


def test_resolve_refs_lookup_anchor_sheet_qualified_priority():
    """_lookup_anchor must prefer Sheet!A1 over bare A1 when sheet_hint is given."""
    from plugin.calc.excel_py_convert.models import ExcelWorkbookModel
    from plugin.calc.excel_py_convert.resolve_refs import _lookup_anchor

    snaps = {
        "A1": "Sheet1!A1:B10",
        "Sheet1!A1": "Sheet1!A1:B10",
        "Sheet2!A1": "Sheet2!A1:C20",
    }
    model = ExcelWorkbookModel(anchor_snapshots=snaps)

    # Looking up bare A1 with Sheet2 hint must resolve Sheet2's anchor, not Sheet1's
    assert _lookup_anchor(model, "A1", sheet_hint="Sheet2") == "Sheet2!A1:C20"
    assert _lookup_anchor(model, "A1", sheet_hint="Sheet1") == "Sheet1!A1:B10"
    # Without sheet hint, bare A1 resolves bare key
    assert _lookup_anchor(model, "A1", sheet_hint="") == "Sheet1!A1:B10"

    # Multi-sheet model: hint for a sheet with no snapshot must NOT leak another sheet's bare key
    from plugin.calc.excel_py_convert.models import SheetInfo
    model_multi = ExcelWorkbookModel(
        sheets=[SheetInfo("Sheet1", 0, ""), SheetInfo("Sheet2", 1, "")],
        anchor_snapshots={"Sheet1!A6": "Sheet1!A6:B10", "A6": "A6:B10", "Sheet2!A6": "Sheet2!A6:B12"},
    )
    assert _lookup_anchor(model_multi, "A6", sheet_hint="Sheet3") is None

    # Hint with apostrophe (e.g. O'Brien) resolves "'O''Brien'!A6"
    model_obrien = ExcelWorkbookModel(
        sheets=[SheetInfo("O'Brien", 0, "")],
        anchor_snapshots={"'O''Brien'!A6": "O'Brien!A6:C20"},
    )
    assert _lookup_anchor(model_obrien, "A6", sheet_hint="O'Brien") == "O'Brien!A6:C20"

    # Hint with hyphen (e.g. My-Sheet) resolves "'My-Sheet'!A6"
    model_hyphen = ExcelWorkbookModel(
        sheets=[SheetInfo("My-Sheet", 0, "")],
        anchor_snapshots={"'My-Sheet'!A6": "My-Sheet!A6:C20"},
    )
    assert _lookup_anchor(model_hyphen, "A6", sheet_hint="My-Sheet") == "My-Sheet!A6:C20"

    # Explicit sheet with literal '$' in sheet name
    model_dollar = ExcelWorkbookModel(
        anchor_snapshots={"'A$B'!A6": "'A$B'!A6:D30"},
    )
    assert _lookup_anchor(model_dollar, "'A$B'!A6") == "'A$B'!A6:D30"

    # Single-sheet model with only bare key still resolves with a hint
    model_single = ExcelWorkbookModel(
        sheets=[SheetInfo("Sheet1", 0, "")],
        anchor_snapshots={"A6": "A6:B10"},
    )
    assert _lookup_anchor(model_single, "A6", sheet_hint="Sheet1") == "A6:B10"


def test_parse_excel_xlsx_bare_anchor_first_wins(tmp_path: Path):
    """parse_excel_xlsx must keep first-wins for bare keys across sheets."""
    from plugin.calc.excel_py_convert.parse_excel_ooxml import parse_excel_xlsx

    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
        '<sheet name="Sheet2" sheetId="2" r:id="rId2"/>'
        '</sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '</Relationships>'
    )
    # Sheet1 has A1 with array ref A1:B10
    ws1 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><f t="array" ref="A1:B10">_xlfn._xlws.PY(0,0)</f></c></row></sheetData>'
        '</worksheet>'
    )
    # Sheet2 has A1 with array ref A1:C20
    ws2 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><f t="array" ref="A1:C20">_xlfn._xlws.PY(0,0)</f></c></row></sheetData>'
        '</worksheet>'
    )

    xlsx = tmp_path / "multi_sheet_anchors.xlsx"
    with zipfile.ZipFile(xlsx, "w") as zf:
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", ws1)
        zf.writestr("xl/worksheets/sheet2.xml", ws2)

    model = parse_excel_xlsx(xlsx)
    # Both sheet-qualified anchors must be preserved
    assert model.anchor_snapshots["Sheet1!A1"] == "Sheet1!A1:B10"
    assert model.anchor_snapshots["Sheet2!A1"] == "Sheet2!A1:C20"
    # Bare A1 must preserve Sheet1's anchor (first-wins), not overwritten by Sheet2
    assert model.anchor_snapshots["A1"] == "A1:B10"


def test_script_bank_name_collision_raises():
    """Two source sheets that previously collided must receive distinct bank sheet names."""
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
    from plugin.calc.excel_py_convert.script_bank import code_sheet_name_for, collect_script_bank

    assert code_sheet_name_for("My_Sheet") == "py_code_My_Sheet"
    assert code_sheet_name_for("My Sheet") != code_sheet_name_for("My_Sheet")

    long_code_1 = "x = 1\n" * 250  # >1000 chars
    long_code_2 = "y = 2\n" * 250

    cell1 = ConvertedCell(
        sheet="My Sheet",
        cell="A1",
        direction="dag",
        original_code="",
        converted_code=long_code_1,
        converted=True,
    )
    cell2 = ConvertedCell(
        sheet="My_Sheet",
        cell="A2",
        direction="dag",
        original_code="",
        converted_code=long_code_2,
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[cell1, cell2])

    banks, warns = collect_script_bank(report)
    assert code_sheet_name_for("My Sheet") in banks
    assert code_sheet_name_for("My_Sheet") in banks
    assert warns == []
    assert report.issues == []


def test_script_bank_truncation_collision_raises():
    """Two long sheet names truncating must receive distinct bank names <= 31 chars."""
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
    from plugin.calc.excel_py_convert.script_bank import code_sheet_name_for, collect_script_bank

    name1 = "VeryLongSheetNameThatWillTruncate1"
    name2 = "VeryLongSheetNameThatWillTruncate2"
    bank1 = code_sheet_name_for(name1)
    bank2 = code_sheet_name_for(name2)
    assert bank1 != bank2
    assert len(bank1) <= 31
    assert len(bank2) <= 31

    cell1 = ConvertedCell(
        sheet=name1,
        cell="A1",
        direction="dag",
        original_code="",
        converted_code="x = 1\n" * 250,
        converted=True,
    )
    cell2 = ConvertedCell(
        sheet=name2,
        cell="A1",
        direction="dag",
        original_code="",
        converted_code="x = 2\n" * 250,
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[cell1, cell2])

    banks, warns = collect_script_bank(report)
    assert bank1 in banks
    assert bank2 in banks
    assert warns == []
    assert report.issues == []


def test_write_dag_formulas_xlsx_closes_workbook_on_error(tmp_path: Path):
    """write_dag_formulas_xlsx must close the openpyxl workbook in try/finally on all errors."""
    from plugin.calc.excel_py_convert.xlsx_write import write_dag_formulas_xlsx
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport

    src = tmp_path / "dag_src.xlsx"
    src.write_bytes(_dag_xlsx_bytes())

    # Create a cell with an unmapped sheet to trigger ValueError in write_dag_formulas_xlsx
    cell = ConvertedCell(
        sheet="NonExistentSheet",
        cell="A1",
        direction="dag",
        original_code="x = 1",
        converted_code="x = 1",
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[cell])
    out = tmp_path / "dag_out.xlsx"

    with pytest.raises(ValueError, match="unmapped sheet"):
        write_dag_formulas_xlsx(src, report, out)


def test_temp_files_cleaned_up_on_exception(tmp_path: Path, monkeypatch):
    """Temporary files (.tmprewrite, .tmpexcelpy) must be cleaned up on exception."""
    from plugin.calc.excel_py_convert.xlsx_write import write_excel_python_xlsx
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport

    src = tmp_path / "temp_clean_src.xlsx"
    src.write_bytes(_dag_xlsx_bytes())

    # 1. Test rewrite_zip cleanup
    from plugin.calc.excel_py_convert.ooxml_util import rewrite_zip

    def fail_transform(zin, zout):
        raise RuntimeError("simulated transform error")

    with pytest.raises(RuntimeError):
        rewrite_zip(src, src.with_suffix(src.suffix + ".out"), fail_transform, temp_suffix=".tmprewrite")
    assert not (src.with_suffix(src.suffix + ".out.tmprewrite")).exists()

    # 2. Test .tmpexcelpy cleanup
    def fail_writestr(*_args, **_kwargs):
        raise RuntimeError("simulated write error")

    cell = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="excel",
        original_code="x = 1",
        converted_code="x = 1",
        converted=True,
        script_index=0,
    )
    rep = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "temp_clean_out.xlsx"

    # Cause failure during writing inside ZipFile
    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_writestr)
    with pytest.raises(RuntimeError):
        write_excel_python_xlsx(src, rep, out)
    assert not (out.with_suffix(out.suffix + ".tmpexcelpy")).exists()


def test_write_excel_python_xlsx_patched_empty_element(tmp_path: Path):
    """write_excel_python_xlsx must not treat an Element with 0 children as falsy."""
    from xml.etree import ElementTree as ET
    from plugin.calc.excel_py_convert.xlsx_write import write_excel_python_xlsx
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport

    # Minimal xlsx
    src = tmp_path / "empty_el_src.xlsx"
    src.write_bytes(_dag_xlsx_bytes())

    # Two cells on the same sheet
    cell1 = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="excel",
        original_code="x = 1",
        converted_code="x = 1",
        converted=True,
        script_index=0,
    )
    cell2 = ConvertedCell(
        sheet="Sheet1",
        cell="A2",
        direction="excel",
        original_code="y = 2",
        converted_code="y = 2",
        converted=True,
        script_index=1,
    )
    report = ConversionReport(direction="excel", cells=[cell1, cell2])
    out = tmp_path / "empty_el_out.xlsx"

    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ws_xml = zf.read("xl/worksheets/sheet1.xml")
        ws_root = ET.fromstring(ws_xml)
        formulas: dict[str, str] = {}
        for c in ws_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            f_node = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
            if f_node is not None:
                r_key = c.attrib.get("r")
                if r_key:
                    formulas[r_key] = "".join(f_node.itertext())
        assert "_xlws.PY(0" in formulas.get("A1", "")
        assert "_xlws.PY(1" in formulas.get("A2", "")


def test_single_line_content_types_with_mc_ignorable():
    """Minified [Content_Types].xml with mc:Ignorable must preserve namespaces and ignorable prefixes."""
    ct_data = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types" '
        b'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        b'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" '
        b'xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision" '
        b'mc:Ignorable="x14ac xr">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        b'<Override PartName="/xl/pythonScripts.xml" ContentType="application/xml"/>'
        b'<Override PartName="/xl/pythonScripts/script1.py" ContentType="application/xml"/>'
        b'</Types>'
    )
    from plugin.calc.excel_py_convert.xlsx_write import _patch_content_types

    # 1. Test stripping python parts
    stripped = _patch_content_types(ct_data, strip_python=True)
    assert b"pythonScripts.xml" not in stripped
    assert b"pythonScripts/script1.py" not in stripped
    assert b'mc:Ignorable="x14ac xr"' in stripped
    assert b'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"' in stripped
    assert b'xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision"' in stripped

    # 2. Test patching python parts
    patched = _patch_content_types(stripped, ensure_python_scripts=True)
    assert b"pythonScripts.xml" in patched
    assert b'mc:Ignorable="x14ac xr"' in patched
    assert b'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"' in patched
    assert b'xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision"' in patched


def test_ensure_cell_missing_row_ordering():
    """_ensure_cell must insert missing rows and cells in strictly ascending order, including when r is omitted."""
    from xml.etree import ElementTree as ET

    from plugin.calc.excel_py_convert.ooxml_util import local_name
    from plugin.calc.excel_py_convert.xlsx_write import _ensure_cell, _ensure_sheet_data

    # Worksheet with row 1 and row 5
    xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1"/><c r="C1"/></row>'
        '<row r="5"><c r="B5"/></row>'
        '</sheetData>'
        '</worksheet>'
    )
    ws = ET.fromstring(xml)

    # Insert B1 in row 1: must be inserted between A1 and C1
    _ensure_cell(ws, "B1")
    sheet_data = _ensure_sheet_data(ws)
    row1 = [r for r in sheet_data if r.attrib.get("r") == "1"][0]
    col_order_1 = [c.attrib.get("r") for c in row1]
    assert col_order_1 == ["A1", "B1", "C1"]

    # Insert row 3 (A3): must be inserted between row 1 and row 5
    _ensure_cell(ws, "A3")
    row_order = [r.attrib.get("r") for r in sheet_data if local_name(r.tag) == "row"]
    assert row_order == ["1", "3", "5"]

    # Test rows with omitted 'r' attribute: row 1 (implicit 1), row 2 (implicit 2)
    xml_no_r = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row><c><v>10</v></c></row>'
        '<row><c><v>20</v></c></row>'
        '</sheetData>'
        '</worksheet>'
    )
    ws_no_r = ET.fromstring(xml_no_r)
    # Ensure cell A2: should match second row (implicit 2) rather than creating a duplicate row
    _c_a2 = _ensure_cell(ws_no_r, "A2")
    sheet_data_no_r = _ensure_sheet_data(ws_no_r)
    assert len(list(sheet_data_no_r)) == 2


def test_shared_formula_vs_array_formula(tmp_path: Path):
    """parse_excel_xlsx must only set array_ref for t='array', never for t='shared'."""
    from plugin.calc.excel_py_convert.parse_excel_ooxml import parse_excel_xlsx

    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    # A1 is a shared-formula master with ref="A1:A5"
    # B1 is an array-formula master with t="array" and ref="B1:B5"
    ws_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1">'
        '<c r="A1"><f t="shared" ref="A1:A5" si="0">_xlfn._xlws.PY(0,0)</f></c>'
        '<c r="B1"><f t="array" ref="B1:B5">_xlfn._xlws.PY(1,0)</f></c>'
        '</row>'
        '</sheetData>'
        '</worksheet>'
    )
    scripts_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">'
        '<pythonScript><code>x = 1</code></pythonScript>'
        '<pythonScript><code>y = 2</code></pythonScript>'
        '</pythonScripts>'
    )

    xlsx = tmp_path / "shared_vs_array.xlsx"
    with zipfile.ZipFile(xlsx, "w") as zf:
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", ws_xml)
        zf.writestr("xl/pythonScripts.xml", scripts_xml)

    model = parse_excel_xlsx(xlsx)
    cells_by_a1 = {c.cell: c for c in model.cells}
    # Shared formula master A1 must NOT have array_ref populated
    assert cells_by_a1["A1"].array_ref == ""
    # Array formula master B1 MUST have array_ref populated
    assert cells_by_a1["B1"].array_ref == "B1:B5"

    # anchor_snapshots must NOT contain A1 / Sheet1!A1
    assert "Sheet1!A1" not in model.anchor_snapshots
    assert "A1" not in model.anchor_snapshots
    # anchor_snapshots must contain B1
    assert model.anchor_snapshots.get("Sheet1!B1") == "Sheet1!B1:B5"
    assert model.anchor_snapshots.get("B1") == "B1:B5"


def test_inline_string_bank_reading(tmp_path: Path):
    """_cell_string_value and iter_dag_py_formulas_xlsx must read bank code stored as inlineStr."""
    from plugin.calc.excel_py_convert.parse_dag_formulas import iter_dag_py_formulas_xlsx

    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        '<sheet name="Data" sheetId="1" r:id="rId1"/>'
        '<sheet name="py_code_Data" sheetId="2" r:id="rId2"/>'
        '</sheets>'
        '</workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '</Relationships>'
    )
    # Sheet1 (Data): cell A1 references bank py_code_Data.A1
    ws1_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1"><f>=PY(py_code_Data.A1)</f></c></row>'
        '</sheetData>'
        '</worksheet>'
    )
    # Sheet2 (py_code_Data): bank cell A1 uses inlineStr
    ws2_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>df = xl("A1:B10")\ndf.describe()</t></is></c></row>'
        '</sheetData>'
        '</worksheet>'
    )

    xlsx = tmp_path / "inlinestr_bank.xlsx"
    with zipfile.ZipFile(xlsx, "w") as zf:
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", ws1_xml)
        zf.writestr("xl/worksheets/sheet2.xml", ws2_xml)

    items = iter_dag_py_formulas_xlsx(xlsx)
    assert len(items) == 1
    sheet, a1, formula = items[0]
    assert sheet == "Data"
    assert a1 == "A1"
    assert 'df = xl(""A1:B10"")' in formula


def test_convert_cell_no_deps_with_xl_reference_fails_closed():
    """Bug 1: Cell with no deps and xl(%P2%) in script must fail closed as fatal error."""
    model = ExcelWorkbookModel(
        scripts=["df = xl(%P2%, headers=True)"],
        cells=[_cell("Sheet1", "A1", 0, deps=[], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert not cell.converted
    assert cell.dag_formula == ""
    assert any("%P2% has no matching formula dep" in i for i in cell.issues)


def test_convert_cell_invalid_placeholder_indices_fail_closed():
    """Bug 2: Invalid placeholders %P0%, %P1%, %P99% must fail closed without raising exceptions."""
    for bad_tok, bad_p in [("%P0%", 0), ("%P1%", 1), ("%P99%", 99)]:
        model = ExcelWorkbookModel(
            scripts=[f"df = xl({bad_tok})"],
            cells=[_cell("Sheet1", "A1", 0, deps=["B1"], row=1, col=1)],
            sheets=[_sheet("Sheet1")],
        )
        report = convert_model_to_dag(model)
        cell = report.cells[0]
        assert not cell.converted
        assert cell.dag_formula == ""
        assert any(f"invalid placeholder {bad_tok}" in i for i in cell.issues)


def test_convert_cell_extra_args_and_bad_headers_fail_closed():
    """Bug 3: Extra positional args, non-constant headers, extra kwargs, and **kw must fail closed."""
    cases = [
        ("xl(%P2%, True)", "does not accept positional arguments beyond the first"),
        ("xl(%P2%, headers=flag)", "headers argument must be a True or False constant"),
        ("xl(%P2%, extra='val')", "unsupported keyword argument 'extra'"),
        ("xl(%P2%, **kw)", "unsupported **kwargs"),
    ]
    for script, expected_issue in cases:
        model = ExcelWorkbookModel(
            scripts=[f"df = {script}"],
            cells=[_cell("Sheet1", "A1", 0, deps=["B1"], row=1, col=1)],
            sheets=[_sheet("Sheet1")],
        )
        report = convert_model_to_dag(model)
        cell = report.cells[0]
        assert not cell.converted
        assert cell.dag_formula == ""
        assert any(expected_issue in i for i in cell.issues)


def test_convert_cell_nested_overlapping_xl_calls_fail_closed():
    """Bug 4: Overlapping / nested xl() calls must fail closed."""
    model = ExcelWorkbookModel(
        scripts=["df = xl(%P2%, headers=xl(%P3%))"],
        cells=[_cell("Sheet1", "A1", 0, deps=["B1", "C1"], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert not cell.converted
    assert cell.dag_formula == ""
    assert any("nested or overlapping xl() calls are not supported" in i for i in cell.issues)


def test_ast_source_offset_with_form_feed_and_u2028():
    """Bug 5: ast_source_offset must split lines only on \\r\\n, \\r, \\n, not \\x0c or \\u2028."""
    # Test 1: \x0c inside string literal
    src_ff = 's = "hello\x0cworld"\ny = xl(%P2%)\n'
    code_ff, analysis_ff = _excel_rewrite(src_ff, num_deps=1)
    assert not analysis_ff.fatal
    assert 'y = xl("%P2%")' in code_ff
    assert 's = "hello\x0cworld"' in code_ff

    # Test 2: \u2028 inside string literal
    src_u2028 = 's = "hello\u2028world"\ny = xl(%P2%)\n'
    code_u2028, analysis_u2028 = _excel_rewrite(src_u2028, num_deps=1)
    assert not analysis_u2028.fatal
    assert 'y = xl("%P2%")' in code_u2028
    assert 's = "hello\u2028world"' in code_u2028

    # Test 3: \x0c and \u2028 inside comments
    src_comm_ff = '# note \x0c here\ny = xl(%P2%)\n'
    code_comm_ff, analysis_comm_ff = _excel_rewrite(src_comm_ff, num_deps=1)
    assert not analysis_comm_ff.fatal
    assert 'y = xl("%P2%")' in code_comm_ff

    src_comm_u2028 = '# note \u2028 here\ny = xl(%P2%)\n'
    code_comm_u2028, analysis_comm_u2028 = _excel_rewrite(src_comm_u2028, num_deps=1)
    assert not analysis_comm_u2028.fatal
    assert 'y = xl("%P2%")' in code_comm_u2028


def test_convert_cell_best_effort_message_accurately_reports_skip():
    """Bug 6: best_effort=True must state that placeholder remapping was skipped."""
    model = ExcelWorkbookModel(
        scripts=["df = xl(%P2%)"],
        cells=[_cell("Sheet1", "A1", 0, deps=["UnknownRef"], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    # In best_effort mode, placeholder remapping is skipped
    report_be = convert_model_to_dag(model, best_effort=True)
    cell_be = report_be.cells[0]
    assert any("skipped placeholder remapping in best-effort mode" in i for i in cell_be.issues)
    assert not any("refusing to emit shifted data indices" in i for i in cell_be.issues)

    # In strict mode, shifted data indices are refused
    report_strict = convert_model_to_dag(model, best_effort=False)
    cell_strict = report_strict.cells[0]
    assert any("refusing to emit shifted data indices" in i for i in cell_strict.issues)
    assert not any("skipped placeholder remapping in best-effort mode" in i for i in cell_strict.issues)


def test_convert_cell_unresolved_deps_and_syntax_error_both_reported():
    """Bug 6: Unresolved deps plus a syntax error must report both issues."""
    model = ExcelWorkbookModel(
        scripts=["df = xl(%P2%, headers=True"],
        cells=[_cell("Sheet1", "A1", 0, deps=["UnknownRef"], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert not cell.converted
    assert any("syntax error" in i for i in cell.issues)
    assert any("unresolved" in i for i in cell.issues)


def test_convert_cell_bare_placeholder_outside_xl_fails_closed():
    """Bare %P2% outside xl() must fail closed."""
    for script in ["df = %P2%", "df = %P2% + 1"]:
        model = ExcelWorkbookModel(
            scripts=[script],
            cells=[_cell("Sheet1", "A1", 0, deps=["B1"], row=1, col=1)],
            sheets=[_sheet("Sheet1")],
        )
        report = convert_model_to_dag(model)
        cell = report.cells[0]
        assert not cell.converted
        assert cell.dag_formula == ""
        assert any("outside xl()" in i for i in cell.issues)


def test_convert_cell_real_sentinel_identifier_fails_closed():
    """Real user identifier _P2_ passed to xl() must fail closed."""
    model = ExcelWorkbookModel(
        scripts=["_P2_ = 5\ndf = xl(_P2_)"],
        cells=[_cell("Sheet1", "A1", 0, deps=["B1"], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert not cell.converted
    assert cell.dag_formula == ""
    assert any("collides with the placeholder sentinel" in i for i in cell.issues)


def test_convert_cell_bare_placeholder_in_fstring_fails_closed():
    """Placeholder inside f-string produces syntax error and fails closed."""
    model = ExcelWorkbookModel(
        scripts=['x = f"{%P2%}"'],
        cells=[_cell("Sheet1", "A1", 0, deps=["B1"], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert not cell.converted
    assert cell.dag_formula == ""
    assert any("syntax error" in i.lower() for i in cell.issues)


def test_convert_cell_long_script_bad_cell_fails_closed():
    """1,100-char script with malformed cell or empty sheet fails closed without raising."""
    long_script = "x = 1\n" * 220  # 1320 chars (> 1000)
    model = ExcelWorkbookModel(
        scripts=[long_script],
        cells=[_cell("Sheet1", "not-a1", 0, deps=[], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    res = convert_cell_to_dag(model, model.cells[0])
    assert res.converted is False
    assert res.dag_formula == ""
    assert res.converted_code == long_script
    assert any("script too long" in i for i in res.issues)

    model_no_sheet = ExcelWorkbookModel(
        scripts=[long_script],
        cells=[_cell("", "A1", 0, deps=[], row=1, col=1)],
        sheets=[],
    )
    res_no_sheet = convert_cell_to_dag(model_no_sheet, model_no_sheet.cells[0])
    assert res_no_sheet.converted is False
    assert res_no_sheet.dag_formula == ""
    assert res_no_sheet.converted_code == long_script
    assert any("to inline" in i for i in res_no_sheet.issues)


def test_dedup_merge_rules_header_mode_true_wins():
    """Dedup merge: when two deps snap to the same A1, headers=True wins over False/omit."""
    model = ExcelWorkbookModel(
        scripts=["a = xl(%P2%, headers=False)\nb = xl(%P3%, headers=True)"],
        cells=[_cell("Sheet1", "A1", 0, deps=["B2", "B2"], row=1, col=1)],
        sheets=[_sheet("Sheet1")],
    )
    report = convert_model_to_dag(model)
    cell = report.cells[0]
    assert cell.converted
    assert len(cell.bindings) == 1
    assert cell.bindings[0].header_mode == "true"


def test_dedup_merge_rules_prefer_excel_dep_token():
    """Dedup merge: _prefer_excel_dep_token retains [#All] or ANCHORARRAY tokens."""
    assert _prefer_excel_dep_token("A1:B10", "Table1[#All]") == "Table1[#All]"
    assert _prefer_excel_dep_token("Table1[#All]", "A1:B10") == "Table1[#All]"
    assert _prefer_excel_dep_token("A1:B10", "_xlfn.ANCHORARRAY(A1)") == "_xlfn.ANCHORARRAY(A1)"
    assert _prefer_excel_dep_token("_xlfn.ANCHORARRAY(A1)", "A1:B10") == "_xlfn.ANCHORARRAY(A1)"


def test_skip_string_escaped_quote_in_triple_quotes():
    """_skip_string must handle backslash escapes inside triple-quoted strings."""
    src = '"""a\\"b"""'
    assert _skip_string(src, 0) == len(src)
    code = 's = """a\\"b"""\nx = xl(%P2%)\n'
    rewritten, analysis = _excel_rewrite(code, num_deps=1)
    assert not analysis.fatal
    assert 'x = xl("%P2%")' in rewritten


def test_shift_xlws_py_dep_variants():
    """Shifting handles relative/absolute coords, structured refs, and external sheets."""
    assert shift_xlws_py_dep("B1", 1, 0) == "B2"
    assert shift_xlws_py_dep("B1", 0, 1) == "C1"
    assert shift_xlws_py_dep("$B$1", 2, 2) == "$B$1"
    assert shift_xlws_py_dep("B$1", 2, 1) == "C$1"
    assert shift_xlws_py_dep("$B1", 2, 1) == "$B3"
    assert shift_xlws_py_dep("Table1[Col]", 1, 0) == "Table1[Col]"
    assert shift_xlws_py_dep("Sheet1!B1", 1, 0) == "Sheet1!B2"
    assert shift_xlws_py_dep("'Data Sheet'!$B1", 2, 0) == "'Data Sheet'!$B3"
    assert shift_xlws_py_dep("_xlfn.ANCHORARRAY(B1)", 1, 0) == "_xlfn.ANCHORARRAY(B2)"
    assert shift_xlws_py_dep("B1+C1", 1, 0) is None
    assert shift_xlws_py_dep("MyNamedRange", 1, 0) == "MyNamedRange"
    assert shift_xlws_py_dep("A:A", 1, 0) == "A:A"
    assert shift_xlws_py_dep("A:C", 0, 1) == "B:D"
    assert shift_xlws_py_dep("$A:$C", 0, 1) == "$A:$C"
    assert shift_xlws_py_dep("1:5", 1, 0) == "2:6"
    assert shift_xlws_py_dep("1:5", 0, 1) == "1:5"
    assert shift_xlws_py_dep("Sheet1!A:A", 1, 0) == "Sheet1!A:A"
    assert shift_xlws_py_dep("Sheet1!MyName", 1, 0) == "Sheet1!MyName"


def test_shared_xlws_py_group_shifts_dependencies(tmp_path: Path):
    """Shared formula dependents must have their dependencies shifted from master."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1"><f t="shared" ref="A1:A3" si="0">_xlfn._xlws.PY(0,0,B1,$B$1,B$1,Table1[Col],Sheet1!B1,A:A,1:5,Sheet1!MyName)</f></c>
    </row>
    <row r="2">
      <c r="A2"><f t="shared" si="0"/></c>
    </row>
    <row r="3">
      <c r="A3"><f t="shared" si="0"/></c>
    </row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    scripts_xml = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>x = xl(%P2%)</code></pythonScript>
</pythonScripts>"""
    src = tmp_path / "shared_py.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(
        sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml, scripts_xml=scripts_xml
    ))

    model = load_excel_model(src)
    assert len(model.cells) == 3
    c1, c2, c3 = model.cells
    assert c1.cell == "A1"
    assert c1.deps == ["B1", "$B$1", "B$1", "Table1[Col]", "Sheet1!B1", "A:A", "1:5", "Sheet1!MyName"]
    assert c2.cell == "A2"
    assert c2.deps == ["B2", "$B$1", "B$1", "Table1[Col]", "Sheet1!B2", "A:A", "2:6", "Sheet1!MyName"]
    assert c3.cell == "A3"
    assert c3.deps == ["B3", "$B$1", "B$1", "Table1[Col]", "Sheet1!B3", "A:A", "3:7", "Sheet1!MyName"]


def test_shared_xlws_py_group_invalid_dep_refused(tmp_path: Path):
    """Unrecognized dep expressions in shared formula emit conversion issues and skip copy."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">_xlfn._xlws.PY(0,0,B1+C1)</f></c></row>
    <row r="2"><c r="A2"><f t="shared" si="0"/></c></row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    scripts_xml = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>x = 1</code></pythonScript>
</pythonScripts>"""
    src = tmp_path / "invalid_shared_py.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(
        sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml, scripts_xml=scripts_xml
    ))

    model = load_excel_model(src)
    assert any("si=0" in i and "A2" in i for i in model.issues)
    assert len(model.cells) == 0
    dag_rep = convert_model_to_dag(model)
    assert not dag_rep.ok


def test_write_excel_python_xlsx_unshares_and_preserves_non_py_sibling(tmp_path: Path):
    """Export must unshare shared groups and write explicit formulas for non-PY siblings."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">B1</f></c></row>
    <row r="2"><c r="A2"><f t="shared" si="0"/></c></row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    src = tmp_path / "shared_sibling.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml))

    cell = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="excel",
        original_code="",
        converted_code="x = 42",
        data_args=[],
        converted=True,
        script_index=0,
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "unshared_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ws_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert 'si="0"' not in ws_xml
        assert 't="shared"' not in ws_xml
        ws_root = ET.fromstring(ws_xml)
        cells = {
            c.attrib.get("r"): c
            for c in ws_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
        }
        a1_f = cells["A1"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        a2_f = cells["A2"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        assert a1_f is not None and "_xlws.PY" in (a1_f.text or "")
        assert a2_f is not None and (a2_f.text or "") == "B2"


def test_shared_formula_roundtrip_deps_match(tmp_path: Path):
    """Importing shared formulas then exporting and reimporting retains shifted deps."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">_xlfn._xlws.PY(0,0,B1)</f></c></row>
    <row r="2"><c r="A2"><f t="shared" si="0"/></c></row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    scripts_xml = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>y = xl(%P2%)</code></pythonScript>
</pythonScripts>"""
    src = tmp_path / "roundtrip_shared.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(
        sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml, scripts_xml=scripts_xml
    ))

    model1 = load_excel_model(src)
    dag_rep = convert_model_to_dag(model1)
    assert dag_rep.ok

    excel_cells = [
        ConvertedCell(
            sheet=c.sheet,
            cell=c.cell,
            direction="excel",
            original_code=c.converted_code,
            converted_code=c.converted_code,
            data_args=c.data_args,
            converted=True,
            script_index=0,
        )
        for c in dag_rep.cells
    ]
    excel_rep = ConversionReport(direction="excel", cells=excel_cells)
    out = tmp_path / "roundtrip_out.xlsx"
    write_excel_python_xlsx(src, excel_rep, out)

    model2 = load_excel_model(out)
    deps_by_cell = {c.cell: c.deps for c in model2.cells}
    assert deps_by_cell["A1"] == ["B1"]
    assert deps_by_cell["A2"] == ["B2"]


def test_write_excel_python_xlsx_package_guards_calc_chain_and_drawing(tmp_path: Path):
    """calcChain is dropped, workbook python relationship added, unrelated drawing preserved."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>PY("x = 1")</f></c></row></sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    wb_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" Target="calcChain.xml"/>
</Relationships>"""
    ct_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/>
</Types>"""

    src = tmp_path / "with_calc_chain.xlsx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("_rels/.rels", """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/calcChain.xml", "<calcChain xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><c r='A1'/></calcChain>")
        zf.writestr("xl/drawings/drawing1.xml", "<xdr:wsDr xmlns:xdr='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'/>")

    cell = ConvertedCell(
        sheet="Sheet1", cell="A1", direction="excel", original_code="", converted_code="x = 1", data_args=[], converted=True, script_index=0
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "cleared_chain.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        names = zf.namelist()
        assert "xl/calcChain.xml" not in names
        assert "xl/drawings/drawing1.xml" in names
        assert "xl/pythonScripts.xml" in names

        ct = zf.read("[Content_Types].xml").decode("utf-8")
        assert "calcChain" not in ct
        assert "/xl/pythonScripts.xml" in ct

        rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        assert "calcChain" not in rels
        assert "pythonScripts.xml" in rels
        assert "relationships/pythonScript" in rels


def test_write_excel_python_xlsx_unwritten_xlws_raises(tmp_path: Path):
    """Extra _xlws.PY cell not in report's write set raises ValueError without modifying out."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1"><f>_xlfn._xlws.PY(0,0)</f></c>
      <c r="B1"><f>_xlfn._xlws.PY(0,0)</f></c>
    </row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    scripts_xml = """<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript><code>x = 1</code></pythonScript>
</pythonScripts>"""
    src = tmp_path / "extra_py.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml, scripts_xml=scripts_xml))

    cell = ConvertedCell(
        sheet="Sheet1", cell="A1", direction="excel", original_code="", converted_code="x = 1", data_args=[], converted=True, script_index=0
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "never_written.xlsx"
    out.write_text("sentinel")

    with pytest.raises(ValueError, match=r"(?s)not in conversion report write set.*Sheet1!B1"):
        write_excel_python_xlsx(src, report, out)

    assert out.read_text() == "sentinel"


def test_write_excel_python_xlsx_unmarked_py_code_sheet_survives(tmp_path: Path):
    """A user sheet named py_code_Notes without defined name or ZZ1 note must not be dropped."""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
    <sheet name="py_code_Notes" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>"""
    wb_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>"""
    ws1 = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>PY("x = 1")</f></c></row></sheetData>
</worksheet>"""
    ws2 = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><v>user data</v></c></row></sheetData>
</worksheet>"""
    ct_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    src = tmp_path / "user_notes.xlsx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("_rels/.rels", """<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/worksheets/sheet1.xml", ws1)
        zf.writestr("xl/worksheets/sheet2.xml", ws2)

    cell = ConvertedCell(
        sheet="Sheet1", cell="A1", direction="excel", original_code="", converted_code="x = 1", data_args=[], converted=True, script_index=0
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "user_notes_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        wb_out = zf.read("xl/workbook.xml").decode("utf-8")
        assert "py_code_Notes" in wb_out
        assert "xl/worksheets/sheet2.xml" in zf.namelist()


def test_write_dag_formulas_bank_sheet_collision_raises(tmp_path: Path):
    """Colliding with existing unmanaged sheet raises ValueError without overwriting."""
    src = tmp_path / "collision_src.xlsx"
    src_bytes = _dag_xlsx_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src_bytes), "r") as zin, zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            if info.filename == "xl/workbook.xml":
                wb_root = ET.fromstring(zin.read(info.filename))
                sheets_el = wb_root.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets")
                assert sheets_el is not None
                ET.SubElement(sheets_el, "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet", {"name": "py_code_Sheet1", "sheetId": "2", "r:id": "rId2"})
                zout.writestr(info, ET.tostring(wb_root))
            elif info.filename == "xl/_rels/workbook.xml.rels":
                rels_root = ET.fromstring(zin.read(info.filename))
                ET.SubElement(rels_root, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship", {"Id": "rId2", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet", "Target": "worksheets/sheet2.xml"})
                zout.writestr(info, ET.tostring(rels_root))
            else:
                zout.writestr(info, zin.read(info.filename))
        sh2_xml = b'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>user data</v></c></row></sheetData></worksheet>'
        zout.writestr("xl/worksheets/sheet2.xml", sh2_xml)
    src.write_bytes(buf.getvalue())

    cell = ConvertedCell(
        sheet="Sheet1", cell="A1", direction="dag", original_code="", converted_code="a" * 1200, data_args=[], converted=True
    )
    report = ConversionReport(direction="dag", cells=[cell])
    out = tmp_path / "collision_out.xlsx"
    with pytest.raises(ValueError, match="Bank sheet collision"):
        write_dag_formulas_xlsx(src, report, out)
    assert not out.exists()


def test_formula_for_converted_cell_over_1000_chars_refuses_inline():
    """Script over 1000 characters cannot compile inline and must raise ValueError."""
    cell = ConvertedCell(
        sheet="Sheet1", cell="invalid_cell", direction="dag", original_code="", converted_code="x = " + "1" * 1050, data_args=[], converted=True
    )
    with pytest.raises(ValueError, match="script too long"):
        formula_for_converted_cell(cell)


def test_dag_write_rejects_xml_noncharacter_in_formula(tmp_path: Path):
    src = tmp_path / "src.xlsx"
    out = tmp_path / "out.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    cell = ConvertedCell(
        sheet="Sheet1",
        cell="A1",
        direction="dag",
        original_code="",
        converted_code="x = '\uffff'",
        data_args=[],
        converted=True,
    )
    report = ConversionReport(direction="dag", cells=[cell])
    with pytest.raises(ValueError, match="illegal XML character in formula"):
        write_dag_formulas_xlsx(src, report, out)
    assert not out.exists()


def test_write_dag_formulas_atomic_save_failure_leaves_dest(tmp_path: Path):
    """Failure during rewrite_zip / replace leaves destination file untouched and cleans up temp files."""
    src = tmp_path / "src.xlsx"
    src.write_bytes(_dag_xlsx_bytes())
    out = tmp_path / "atomic_target.xlsx"
    out.write_text("original content")

    cell = ConvertedCell(
        sheet="Sheet1", cell="A1", direction="dag", original_code="", converted_code="x = 1", data_args=[], converted=True
    )
    report = ConversionReport(direction="dag", cells=[cell])

    from unittest.mock import patch
    with patch.object(Path, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            write_dag_formulas_xlsx(src, report, out)

    assert out.read_text() == "original content"
    temp_files = [p for p in tmp_path.iterdir() if p.name.startswith("atomic_target") and p != out]
    assert temp_files == []


def test_a1_span_bounds_allocates_nothing_large():
    """a1_span_bounds returns bounding box without instantiating coordinate lists."""
    assert a1_span_bounds("A1:A100000") == ((1, 1), (1, 100000))
    assert a1_span_bounds("Sheet1!$B$2:$D$10") == ((2, 2), (4, 10))
    assert a1_span_bounds("A:A") is None
    assert a1_span_bounds("invalid") is None


def test_clear_spill_uno_single_range_clear():
    """Spill clear calls getCellRangeByName on the bounding box once, not per cell."""
    from unittest.mock import MagicMock
    from plugin.calc.excel_py_convert.apply_calc import apply_dag_formulas_to_calc_doc

    doc = MagicMock()
    sheet = MagicMock()
    doc.getSheets().getByName.return_value = sheet
    doc.getSheets().hasByName.return_value = True

    spill_range = MagicMock()
    anchor_cell = MagicMock()
    anchor_cell.getFormula.return_value = ""

    def get_range(name: str):
        if name == "C1:D2":
            return spill_range
        if name == "C1":
            return anchor_cell
        return MagicMock()

    sheet.getCellRangeByName.side_effect = get_range

    cell = ConvertedCell(
        sheet="Sheet1",
        cell="C1",
        direction="dag",
        original_code="",
        converted_code="x = 1",
        data_args=[],
        converted=True,
        array_ref="C1:D2",
    )
    report = ConversionReport(direction="dag", cells=[cell])
    errors = apply_dag_formulas_to_calc_doc(doc, report)
    assert errors == []
    spill_range.clearContents.assert_called_once_with(55)
    assert anchor_cell.getFormula.call_count == 1
    assert anchor_cell.setFormula.call_args[0][0] == '=PY("x = 1")'


def test_worksheet_cell_index_sparse_order():
    """Sparse inserts maintain ascending numerical row order and alphabetical column order."""
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex

    root = ET.Element("worksheet")
    ET.SubElement(root, "sheetData")
    idx = _WorksheetCellIndex(root)

    coords = ["Z100", "A2", "C10", "B5", "AA50", "B2", "A1"]
    for c in coords:
        el = idx.ensure_cell(c)
        el.text = c

    sd = root.find("sheetData")
    assert sd is not None
    rows = list(sd)
    row_nums = [int(r.attrib["r"]) for r in rows]
    assert row_nums == sorted(row_nums)

    for r in rows:
        cells = list(r)
        cell_rs = [c.attrib.get("r", "") for c in cells]
        assert cell_rs == sorted(cell_rs, key=lambda a1: (len(a1), a1))


def test_worksheet_cell_index_inserts_before_trailing_extlst():
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex

    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"/><extLst/></sheetData></worksheet>'
    root = ET.fromstring(xml)
    idx = _WorksheetCellIndex(root)
    idx.ensure_cell("A5")
    assert [local_name(c.tag) for c in idx.sheet_data] == ["row", "row", "extLst"]
    assert [r.attrib.get("r") for r in idx.sheet_data if local_name(r.tag) == "row"] == ["1", "5"]


def test_worksheet_cell_index_no_r_attribute():
    """Rows and cells lacking explicit r attributes are tracked and updated in place."""
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex

    xml = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row>
      <c><v>1</v></c>
      <c><v>2</v></c>
    </row>
  </sheetData>
</worksheet>"""
    root = ET.fromstring(xml)
    idx = _WorksheetCellIndex(root)
    c1 = idx.ensure_cell("A1")
    v = c1.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
    if v is None:
        v = c1.find("v")
    assert v is not None and v.text == "1"
    sd = root.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheetData")
    if sd is None:
        sd = root.find("sheetData")
    assert sd is not None
    assert len(list(sd[0])) == 2


def test_bad_package_xml_raises_value_error():
    """Malformed [Content_Types].xml or .rels raises ValueError with part name."""
    from plugin.calc.excel_py_convert.xlsx_write import _patch_content_types, _strip_rels_python

    with pytest.raises(ValueError, match=r"\[Content_Types\]\.xml"):
        _patch_content_types(b"<malformed XML", strip_python=True)
    with pytest.raises(ValueError, match=r"\.rels"):
        _strip_rels_python(b"<malformed XML")


def test_strip_rels_preserves_python_notes():
    """Relationships targeting python_notes.xml or non-pythonScripts must not be stripped."""
    from plugin.calc.excel_py_convert.xlsx_write import _strip_rels_python

    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2022/relationships/pythonScript" Target="pythonScripts.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom" Target="python_notes.xml"/>
</Relationships>"""
    res = _strip_rels_python(xml.encode("utf-8")).decode("utf-8")
    assert "rId1" in res
    assert "rId2" not in res
    assert "rId3" in res
    assert "python_notes.xml" in res


def test_strip_rels_keeps_python_when_not_stripping():
    from plugin.calc.excel_py_convert.xlsx_write import _strip_rels_python

    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" Target="calcChain.xml"/>
  <Relationship Id="rId3" Type="http://schemas.microsoft.com/office/2022/relationships/pythonScript" Target="pythonScripts.xml"/>
</Relationships>"""
    res = _strip_rels_python(xml, strip_python=False).decode("utf-8")
    assert "rId1" in res
    assert "rId2" not in res
    assert "rId3" in res
    assert "pythonScripts.xml" in res


def test_xml_namespace_lock_thread_safety():
    """Concurrent serialization with different namespaces does not cross-pollute declarations."""
    errors: list[str] = []

    def run_serialization(prefix: str, uri: str):
        try:
            for _ in range(50):
                with xml_namespace_lock():
                    ET.register_namespace(prefix, uri)
                    el = ET.Element(f"{{{uri}}}root")
                    _ = ET.SubElement(el, f"{{{uri}}}item")
                    out = ET.tostring(el, encoding="utf-8").decode("utf-8")
                    if f"xmlns:{prefix}" not in out and "xmlns=" not in out:
                        errors.append(f"Missing xmlns in: {out}")
        except Exception as e:
            errors.append(str(e))

    threads = [
        threading.Thread(target=run_serialization, args=("nsA", "http://example.com/nsA")),
        threading.Thread(target=run_serialization, args=("nsB", "http://example.com/nsB")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_write_excel_python_xlsx_leaves_untouched_shared_group_intact(tmp_path: Path):
    """Shared formula group with no converted cell stays shared on export."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="B1"><f t="shared" ref="B1:B3" si="0">A1+1</f></c>
      <c r="D1"><f>_xlfn._xlws.PY(0,0)</f></c>
    </row>
    <row r="2"><c r="B2"><f t="shared" si="0"/></c></row>
    <row r="3"><c r="B3"><f t="shared" si="0"/></c></row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    src = tmp_path / "shared_untouched.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml))

    cell = ConvertedCell(
        sheet="Sheet1",
        cell="D1",
        direction="excel",
        original_code="",
        converted_code="x = 1",
        data_args=[],
        converted=True,
        script_index=0,
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "shared_untouched_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ws_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        ws_root = ET.fromstring(ws_xml)
        cells = {
            c.attrib.get("r"): c
            for c in ws_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
        }
        b1_f = cells["B1"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        b2_f = cells["B2"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        b3_f = cells["B3"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        assert b1_f is not None and b1_f.attrib.get("t") == "shared" and b1_f.attrib.get("si") == "0"
        assert b1_f.text == "A1+1"
        assert b2_f is not None and b2_f.attrib.get("t") == "shared" and b2_f.attrib.get("si") == "0"
        assert b3_f is not None and b3_f.attrib.get("t") == "shared" and b3_f.attrib.get("si") == "0"


def test_write_excel_python_xlsx_unshares_partial_group_non_py_master(tmp_path: Path):
    """Shared C1:C2 master A1+1 with C1 converted unshares C2 as explicit A2+1 without si."""
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="C1"><f t="shared" ref="C1:C2" si="0">A1+1</f></c></row>
    <row r="2"><c r="C2"><f t="shared" si="0"/></c></row>
  </sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    src = tmp_path / "shared_c1_c2.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml))

    cell = ConvertedCell(
        sheet="Sheet1",
        cell="C1",
        direction="excel",
        original_code="",
        converted_code="x = 99",
        data_args=[],
        converted=True,
        script_index=0,
    )
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "shared_c1_c2_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ws_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert 'si="0"' not in ws_xml
        ws_root = ET.fromstring(ws_xml)
        cells = {
            c.attrib.get("r"): c
            for c in ws_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
        }
        c1_f = cells["C1"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        c2_f = cells["C2"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        assert c1_f is not None and "_xlws.PY" in (c1_f.text or "")
        assert c2_f is not None and (c2_f.text or "") == "A2+1"
        assert "si" not in c2_f.attrib


def test_split_top_level_args_structured_and_braces():
    # Structured ref with internal comma is one dep
    assert split_top_level_args("Table1[[#Headers],[Col1]]") == ["Table1[[#Headers],[Col1]]"]
    # Comma outside brackets still separates deps
    assert split_top_level_args("Table1[[#This Row],[Amount]],B2") == ["Table1[[#This Row],[Amount]]", "B2"]
    # Braces is one dep
    assert split_top_level_args("{1,2,3}") == ["{1,2,3}"]
    # Quoted comma unchanged
    assert split_top_level_args('"a,b",A1') == ['"a,b"', "A1"]

    parsed = parse_xlws_py_formula("_xlfn._xlws.PY(0,1,Table1[[#Headers],[Col1]])")
    assert parsed == (0, 1, ["Table1[[#Headers],[Col1]]"])


def test_rewrite_dag_code_to_excel_out_of_range_placeholder():
    code1 = 'x = xl("%P9%")'
    res1, deps1, issues1, fatal1 = rewrite_dag_code_to_excel(code1, ["A1", "A2"])
    assert fatal1 is True
    assert res1 == code1

    code2 = 'x = xl("%P2%")\ny = xl("%P9%")'
    res2, deps2, issues2, fatal2 = rewrite_dag_code_to_excel(code2, ["A1", "A2"])
    assert fatal2 is True
    assert res2 == code2


def test_rewrite_dag_code_to_excel_out_of_range_legacy_data():
    code1 = "x = data[2]"
    res1, deps1, issues1, fatal1 = rewrite_dag_code_to_excel(code1, ["A1"])
    assert fatal1 is True
    assert "xl(%P2%)" not in res1
    assert "xl(%P4%)" not in res1
    assert res1 == code1

    code2 = "df = data[2].to_pandas()"
    res2, deps2, issues2, fatal2 = rewrite_dag_code_to_excel(code2, ["A1"])
    assert fatal2 is True
    assert "xl(%P" not in res2
    assert res2 == code2


def test_rewrite_dag_code_to_excel_valid_placeholder():
    code = 'x = xl("%P2%")'
    res, deps, issues, fatal = rewrite_dag_code_to_excel(code, ["A1"])
    assert fatal is False
    assert res == "x = xl(%P2%)"


def test_excel_report_with_fatal_placeholder_refuses_export(tmp_path: Path):
    formula = '=PY("x = xl(\\"%P9%\\")"; A1)'
    cell = convert_dag_formula_to_excel(formula, sheet="Sheet1", cell="A1", return_type=0)
    assert cell.converted is False
    report = ConversionReport(direction="excel", cells=[cell])
    assert report.ok is False

    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>=PY("x = 1")</f></c></row></sheetData>
</worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    src = tmp_path / "fatal_p9.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml))
    out = tmp_path / "fatal_p9_out.xlsx"
    with pytest.raises(ValueError, match="no converted Excel PY cells"):
        write_excel_python_xlsx(src, report, out)


def test_bank_defined_name_exact_matching(tmp_path: Path):
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
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
    <sheet name="py_code_Data" sheetId="2" r:id="rId2"/>
    <sheet name="py_code_Data2" sheetId="3" r:id="rId3"/>
  </sheets>
  <definedNames>
    <definedName name="_py_code_bank_py_code_Data2">py_code_Data2!$A$1</definedName>
  </definedNames>
</workbook>"""
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
</Relationships>""",
        )
        sh1 = """<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>_xlfn._xlws.PY(0,0)</f></c></row></sheetData></worksheet>"""
        sh2 = """<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>user data</v></c></row></sheetData></worksheet>"""
        sh3 = """<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>bank code</v></c></row></sheetData></worksheet>"""
        zf.writestr("xl/worksheets/sheet1.xml", sh1)
        zf.writestr("xl/worksheets/sheet2.xml", sh2)
        zf.writestr("xl/worksheets/sheet3.xml", sh3)

    src = tmp_path / "bank_exact.xlsx"
    src.write_bytes(buf.getvalue())

    # On DAG write side: py_code_Data is not a bank sheet, so banking for Data raises collision
    dag_cell = ConvertedCell(sheet="Data", cell="A1", direction="dag", original_code="", converted_code="x = " + "1" * 1200, data_args=[], converted=True)
    dag_report = ConversionReport(direction="dag", cells=[dag_cell])
    dag_out = tmp_path / "dag_collision_out.xlsx"
    with pytest.raises(ValueError, match="Bank sheet collision"):
        write_dag_formulas_xlsx(src, dag_report, dag_out)

    # On excel export side: py_code_Data kept, py_code_Data2 dropped
    cell = ConvertedCell(sheet="Sheet1", cell="A1", direction="excel", original_code="", converted_code="x = 1", data_args=[], converted=True, script_index=0)
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "bank_exact_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        wb_res = zf.read("xl/workbook.xml").decode("utf-8")
        assert 'name="py_code_Data"' in wb_res
        assert 'name="py_code_Data2"' not in wb_res


def test_namespace_serialize_isolated_per_part():
    from plugin.calc.excel_py_convert.ooxml_util import parse_and_extract_namespaces, serialize_xml_preserving_namespaces

    xml_a = b'<a:root xmlns:a="http://example.com/nsA"><a:child/></a:root>'
    xml_b = b'<b:root xmlns:b="http://example.com/nsB"><b:child/></b:root>'

    ns_map_attr = getattr(ET, "_namespace_map", {})
    map_before = dict(ns_map_attr) if isinstance(ns_map_attr, dict) else {}
    # Parse both parts
    root_a, ns_a = parse_and_extract_namespaces(xml_a)
    root_b, ns_b = parse_and_extract_namespaces(xml_b)
    assert getattr(ET, "_namespace_map", {}) == map_before

    # Serializing root_a preserves prefix "a"
    out_a = serialize_xml_preserving_namespaces(root_a, ns_a).decode("utf-8")
    assert "<a:root" in out_a
    assert "xmlns:a=" in out_a
    out_b = serialize_xml_preserving_namespaces(root_b, ns_b).decode("utf-8")
    assert "<b:root" in out_b
    assert "xmlns:b=" in out_b


def test_worksheet_custom_prefix_roundtrip(tmp_path: Path):
    sheet_xml = """<?xml version="1.0" encoding="UTF-8"?>
<custom:worksheet xmlns:custom="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <custom:sheetData>
    <custom:row r="1"><custom:c r="A1"><custom:f>_xlfn._xlws.PY(0,0)</custom:f></custom:c></custom:row>
  </custom:sheetData>
</custom:worksheet>"""
    wb_xml = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    src = tmp_path / "custom_prefix.xlsx"
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml))

    cell = ConvertedCell(sheet="Sheet1", cell="A1", direction="excel", original_code="", converted_code="x = 1", data_args=[], converted=True, script_index=0)
    report = ConversionReport(direction="excel", cells=[cell])
    out = tmp_path / "custom_prefix_out.xlsx"
    write_excel_python_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ws_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "<custom:worksheet" in ws_xml
        assert 'xmlns:custom="http://schemas.openxmlformats.org/spreadsheetml/2006/main"' in ws_xml


def test_clear_spill_xml_touches_stored_cells_only():
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex, _clear_spill_xml

    ws_root = ET.Element("worksheet")
    ET.SubElement(ws_root, "sheetData")
    index = _WorksheetCellIndex(ws_root)

    c_anchor = index.ensure_cell("A1")
    v_a = ET.SubElement(c_anchor, "v")
    v_a.text = "anchor"

    c_spill = index.ensure_cell("B2")
    v_s = ET.SubElement(c_spill, "v")
    v_s.text = "spill"

    c_outside = index.ensure_cell("A10001")
    v_o = ET.SubElement(c_outside, "v")
    v_o.text = "outside"

    _clear_spill_xml(ws_root, "A1", "A1:XFD10000", index=index)

    # Anchor value kept
    v_a_found = c_anchor.find("v")
    assert v_a_found is not None and v_a_found.text == "anchor"
    # In-range non-anchor cleared
    assert c_spill.find("v") is None
    # Out-of-range cell kept
    v_o_found = c_outside.find("v")
    assert v_o_found is not None and v_o_found.text == "outside"


def test_drop_py_code_sheets_sets_full_calc_on_load():
    from plugin.calc.excel_py_convert.xlsx_write import _drop_py_code_sheets_from_workbook

    wb_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
  <calcPr calcMode="auto" calcId="12345"/>
</workbook>"""
    rels_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

    new_wb_xml, _ = _drop_py_code_sheets_from_workbook(wb_xml, rels_xml, set())
    root = ET.fromstring(new_wb_xml)
    calc_pr = root.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}calcPr")
    assert calc_pr is not None
    assert calc_pr.attrib.get("fullCalcOnLoad") == "1"
    assert calc_pr.attrib.get("calcMode") == "auto"
    assert calc_pr.attrib.get("calcId") == "12345"


def test_write_dag_formulas_xlsx_direction_guard(tmp_path: Path):
    from plugin.calc.excel_py_convert.xlsx_write import write_dag_formulas_xlsx

    src = tmp_path / "dummy.xlsx"
    out = tmp_path / "out.xlsx"
    report = ConversionReport(direction="excel")
    with pytest.raises(ValueError, match="requires report.direction == 'dag'"):
        write_dag_formulas_xlsx(src, report, out)


def test_ensure_cell_child_order_trailing_extlst():
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex

    ws_root = ET.fromstring(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"/><extLst/></row></sheetData></worksheet>'
    )
    index = _WorksheetCellIndex(ws_root)
    index.ensure_cell("B1")
    row = index.rows[1]
    tags = [local_name(ch.tag) for ch in row]
    assert tags == ["c", "c", "extLst"]
    assert row[1].attrib.get("r") == "B1"


def test_ensure_cell_child_order_extlst_between_cells():
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex

    ws_root = ET.fromstring(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"/><extLst/><c r="C1"/></row></sheetData></worksheet>'
    )
    index = _WorksheetCellIndex(ws_root)
    index.ensure_cell("B1")
    row = index.rows[1]
    tags = [local_name(ch.tag) for ch in row]
    assert tags == ["c", "c", "extLst", "c"]
    assert row[1].attrib.get("r") == "B1"


def test_ensure_cell_ascending_large_sequence():
    from plugin.calc.excel_py_convert.xlsx_write import _WorksheetCellIndex

    ws_root = ET.fromstring(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData/></worksheet>'
    )
    index = _WorksheetCellIndex(ws_root)
    for i in range(1, 5001):
        index.ensure_cell(f"H{i}")
    row_nums = [int(r.attrib["r"]) for r in index.sheet_data if local_name(r.tag) == "row"]
    assert row_nums == list(range(1, 5001))


def test_set_cell_xlws_formula_child_order_extlst_without_f():
    from plugin.calc.excel_py_convert.xlsx_write import _set_cell_xlws_formula

    ws_root = ET.fromstring(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><v>123</v><extLst/></c></row></sheetData></worksheet>'
    )
    _set_cell_xlws_formula(ws_root, "A1", "_xlws.PY(0,0)")
    cell = ws_root.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
    assert cell is not None
    tags = [local_name(ch.tag) for ch in cell]
    assert tags == ["f", "extLst"]
    assert cell[0].text == "_xlws.PY(0,0)"


def test_set_cell_xlws_formula_existing_f_with_extlst():
    from plugin.calc.excel_py_convert.xlsx_write import _set_cell_xlws_formula

    ws_root = ET.fromstring(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><f>OLD()</f><extLst/></c></row></sheetData></worksheet>'
    )
    _set_cell_xlws_formula(ws_root, "A1", "_xlws.PY(0,0)")
    cell = ws_root.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
    assert cell is not None
    tags = [local_name(ch.tag) for ch in cell]
    assert tags == ["f", "extLst"]
    assert cell[0].text == "_xlws.PY(0,0)"


def test_write_excel_python_xlsx_duplicate_cell_raises(tmp_path: Path):
    src = tmp_path / "dup_input.xlsx"
    out = tmp_path / "dup_output.xlsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>')
    src.write_bytes(buf.getvalue())

    c1 = ConvertedCell(sheet="Sheet1", cell="A1", direction="excel", original_code="x = 1", converted_code="x = 1", script_index=0, converted=True)
    c2 = ConvertedCell(sheet="sheet1", cell="$A$1", direction="excel", original_code="x = 1", converted_code="x = 1", script_index=0, converted=True)
    report = ConversionReport(direction="excel", cells=[c1, c2])

    with pytest.raises(ValueError, match="duplicate cell A1"):
        write_excel_python_xlsx(src, report, out)


def test_write_excel_python_xlsx_shared_group_ref_shift_prevented_by_combined_write_set(tmp_path: Path):
    src = tmp_path / "shared_input.xlsx"
    out = tmp_path / "shared_output.xlsx"
    ws_xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        '<row r="1"><c r="A1"><f t="shared" si="0"/></c></row>'
        '<row r="2"><c r="A2"><f t="shared" ref="A1:A2" si="0">A1</f></c></row>'
        '</sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", ws_xml)
    src.write_bytes(buf.getvalue())

    c1 = ConvertedCell(sheet="Sheet1", cell="A1", direction="excel", original_code="x = 1", converted_code="x = 1", script_index=0, converted=True)
    c2 = ConvertedCell(sheet="sheet1", cell="A2", direction="excel", original_code="x = 1", converted_code="x = 1", script_index=0, converted=True)
    report = ConversionReport(direction="excel", cells=[c1, c2])

    write_excel_python_xlsx(src, report, out)
    with zipfile.ZipFile(out, "r") as zf:
        patched_ws = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "_xlws.PY" in patched_ws


def test_python_scripts_xml_cr_roundtrip_and_invalid_xml_char():
    code_with_cr = "a = 'x\r\ny'"
    data = python_scripts_xml([code_with_cr])
    root = ET.fromstring(data)
    code_el = root.find(".//{http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript}code")
    assert code_el is not None
    assert code_el.text == code_with_cr

    with pytest.raises(ValueError, match="invalid XML 1.0 character"):
        python_scripts_xml(["a = 'x\x0by'"])


def test_write_excel_python_xlsx_missing_required_parts_raises(tmp_path: Path):
    src = tmp_path / "missing_part.xlsx"
    out = tmp_path / "missing_part_out.xlsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
    src.write_bytes(buf.getvalue())

    c = ConvertedCell(sheet="Sheet1", cell="A1", direction="excel", original_code="x = 1", converted_code="x = 1", script_index=0, converted=True)
    report = ConversionReport(direction="excel", cells=[c])
    with pytest.raises(ValueError, match="package missing required part: xl/workbook.xml"):
        write_excel_python_xlsx(src, report, out)


def test_parse_and_extract_namespaces_single_pass():
    xml_data = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        b'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        b'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" '
        b'mc:Ignorable="x14ac">'
        b'<sheetData><row r="1" x14ac:dyDescent="0.25"><c r="A1"><v>1</v></c></row></sheetData>'
        b'</worksheet>'
    )
    root, ns_map = parse_and_extract_namespaces(xml_data)
    assert "" in ns_map
    assert ns_map[""] == "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    assert "x14ac" in ns_map
    assert ns_map["x14ac"] == "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
    assert local_name(root.tag) == "worksheet"

    out = serialize_xml_preserving_namespaces(root, ns_map)
    assert b'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"' in out
    assert b'mc:Ignorable="x14ac"' in out


def test_parse_python_scripts_normal_and_hostile_indices():
    from plugin.calc.excel_py_convert.parse_excel_ooxml import _parse_python_scripts

    normal_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript index="0"><code>x = 1</code></pythonScript>
  <pythonScript index="1"><code>y = 2</code></pythonScript>
</pythonScripts>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/pythonScripts.xml", normal_xml)
    with zipfile.ZipFile(buf, "r") as zf:
        scripts = _parse_python_scripts(zf)
        assert scripts == ["x = 1", "y = 2"]

    hostile_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript index="1000000000"><code>x = 1</code></pythonScript>
</pythonScripts>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/pythonScripts.xml", hostile_xml)
    issues: list[str] = []
    with zipfile.ZipFile(buf, "r") as zf:
        scripts = _parse_python_scripts(zf, issues)
        assert scripts == []
        assert any("out of range" in iss for iss in issues)

    # Duplicate index: last-wins and issue recorded
    dup_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript index="0"><code>x = 1</code></pythonScript>
  <pythonScript index="0"><code>x = 2</code></pythonScript>
</pythonScripts>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/pythonScripts.xml", dup_xml)
    issues = []
    with zipfile.ZipFile(buf, "r") as zf:
        scripts = _parse_python_scripts(zf, issues)
        assert scripts == ["x = 2"]
        assert any("duplicate script index 0" in iss for iss in issues)

    # Mixed: good script parses while bad index is skipped
    mixed_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<pythonScripts xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/pythonscript">
  <pythonScript index="0"><code>x = 1</code></pythonScript>
  <pythonScript index="1000000000"><code>x = 2</code></pythonScript>
</pythonScripts>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/pythonScripts.xml", mixed_xml)
    issues = []
    with zipfile.ZipFile(buf, "r") as zf:
        scripts = _parse_python_scripts(zf, issues)
        assert scripts == ["x = 1"]
        assert any("out of range" in iss for iss in issues)


def test_standard_namespaces_x16_mapping():
    from plugin.calc.excel_py_convert.ooxml_util import register_standard_namespaces

    register_standard_namespaces()
    el = ET.Element("{http://schemas.microsoft.com/office/spreadsheetml/2014/11/main}test")
    out = ET.tostring(el, encoding="utf-8")
    assert b"x16:test" in out


def test_write_dag_formulas_preserves_drawings_and_vba(tmp_path: Path):
    src = tmp_path / "preserve_src.xlsx"
    out = tmp_path / "preserve_out.xlsx"
    drawing_bytes = b"<xdr:wsDr xmlns:xdr='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'/>"
    vba_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1vba_binary"
    sheet_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        b'<sheetData><row r="1"><c r="A1"><v>old</v></c><c r="B1"><v>keep</v></c></row></sheetData>'
        b'<drawing r:id="rIdDrawing1"/>'
        b'</worksheet>'
    )
    sheet_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rIdDrawing1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>'
        b'</Relationships>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
        zf.writestr("xl/drawings/drawing1.xml", drawing_bytes)
        zf.writestr("xl/vbaProject.bin", vba_bytes)
    src.write_bytes(buf.getvalue())

    cell = ConvertedCell(sheet="Sheet1", cell="A1", direction="dag", original_code="x = 1", converted_code="x = 1", data_args=[], converted=True)
    report = ConversionReport(direction="dag", cells=[cell])
    write_dag_formulas_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        assert zf.read("xl/drawings/drawing1.xml") == drawing_bytes
        assert zf.read("xl/vbaProject.bin") == vba_bytes
        sh1 = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "drawing" in sh1
        assert "rIdDrawing1" in sh1


def test_write_dag_formulas_bank_sheet_structure(tmp_path: Path):
    src = tmp_path / "bank_struct_src.xlsx"
    out = tmp_path / "bank_struct_out.xlsx"
    src.write_bytes(_dag_xlsx_bytes())

    long_code = "result = " + "9" * 1100
    cell = ConvertedCell(sheet="Sheet1", cell="H4", direction="dag", original_code="", converted_code=long_code, data_args=[], converted=True)
    report = ConversionReport(direction="dag", cells=[cell])
    write_dag_formulas_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets_el = wb.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets")
        assert sheets_el is not None
        sheet_tags = list(sheets_el)
        assert len(sheet_tags) == 2
        assert sheet_tags[0].attrib.get("name") == "Sheet1"
        assert sheet_tags[1].attrib.get("name") == "py_code_Sheet1"
        assert sheet_tags[1].attrib.get("sheetId") == "2"
        bank_rid = sheet_tags[1].attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")

        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        bank_rel = [r for r in rels if r.attrib.get("Id") == bank_rid][0]
        assert bank_rel.attrib.get("Target") == "worksheets/sheet2.xml"

        ct = zf.read("[Content_Types].xml").decode("utf-8")
        assert 'PartName="/xl/worksheets/sheet2.xml"' in ct

        dns = wb.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}definedNames")
        assert dns is not None
        dn = [d for d in dns if d.attrib.get("name") == "_py_code_bank_py_code_Sheet1"][0]
        assert dn.text == "'py_code_Sheet1'!$A$1"

        bank_ws = ET.fromstring(zf.read("xl/worksheets/sheet2.xml"))
        cells = {c.attrib.get("r"): c for c in bank_ws.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")}
        assert "ZZ1" in cells
        assert cells["ZZ1"].attrib.get("t") == "inlineStr"
        t_zz1 = cells["ZZ1"].find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        assert t_zz1 is not None and t_zz1.text is not None and t_zz1.text.startswith("Python script bank")
        assert "H4" in cells
        assert cells["H4"].attrib.get("t") == "inlineStr"
        t_h4 = cells["H4"].find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        assert t_h4 is not None and t_h4.text == long_code


def test_write_dag_formulas_shared_formula_unsharing(tmp_path: Path):
    src = tmp_path / "shared_unshare_src.xlsx"
    out = tmp_path / "shared_unshare_out.xlsx"
    sheet_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<sheetData>'
        b'<row r="1">'
        b'<c r="A1"><f t="shared" ref="A1:A2" si="0">B1+1</f><v>2</v></c>'
        b'<c r="B1"><v>1</v></c>'
        b'<c r="C1"><f t="shared" ref="C1:C2" si="1">D1+1</f><v>3</v></c>'
        b'<c r="D1"><v>2</v></c>'
        b'</row>'
        b'<row r="2">'
        b'<c r="A2"><f t="shared" si="0"/><v>3</v></c>'
        b'<c r="B2"><v>2</v></c>'
        b'<c r="C2"><f t="shared" si="1"/><v>4</v></c>'
        b'<c r="D2"><v>3</v></c>'
        b'</row>'
        b'</sheetData>'
        b'</worksheet>'
    )
    wb_xml = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels_xml = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml.decode("utf-8"), workbook_xml=wb_xml, rels_xml=rels_xml))

    cell = ConvertedCell(sheet="Sheet1", cell="A1", direction="dag", original_code="x = 10", converted_code="x = 10", data_args=[], converted=True)
    report = ConversionReport(direction="dag", cells=[cell])
    write_dag_formulas_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        ws = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        cells = {c.attrib.get("r"): c for c in ws.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")}
        f_a1 = cells["A1"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        assert f_a1 is not None
        assert f_a1.attrib.get("t") is None
        assert f_a1.attrib.get("si") is None

        f_a2 = cells["A2"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        assert f_a2 is not None
        assert f_a2.attrib.get("t") is None
        assert f_a2.attrib.get("si") is None
        assert f_a2.text == "B2+1"

        f_c1 = cells["C1"].find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f")
        assert f_c1 is not None
        assert f_c1.attrib.get("t") == "shared"
        assert f_c1.attrib.get("si") == "1"


def test_write_dag_formulas_multiple_cell_errors_collected(tmp_path: Path):
    src = tmp_path / "multi_err_src.xlsx"
    out = tmp_path / "multi_err_out.xlsx"
    src.write_bytes(_dag_xlsx_bytes())

    c1 = ConvertedCell(sheet="Sheet1", cell="invalid_cell_1", direction="dag", original_code="", converted_code="x = " + "1" * 1100, data_args=[], converted=True)
    c2 = ConvertedCell(sheet="Sheet1", cell="invalid_cell_2", direction="dag", original_code="", converted_code="y = " + "2" * 1100, data_args=[], converted=True)
    report = ConversionReport(direction="dag", cells=[c1, c2])

    with pytest.raises(ValueError) as excinfo:
        write_dag_formulas_xlsx(src, report, out)
    msg = str(excinfo.value)
    assert "Sheet1!invalid_cell_1" in msg
    assert "Sheet1!invalid_cell_2" in msg
    assert not out.exists()


def test_write_dag_formulas_drops_calc_chain_when_strip_python_parts_false(tmp_path: Path):
    src = tmp_path / "with_calc_chain.xlsx"
    out = tmp_path / "out.xlsx"
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
  <Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/>
  <Override PartName="/xl/pythonScripts.xml" ContentType="application/xml"/>
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" Target="calcChain.xml"/>
  <Relationship Id="rId3" Type="http://schemas.microsoft.com/office/2022/10/relationships/pythonScript" Target="pythonScripts.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1"><f>_xlfn._xlws.PY(0,0)</f></c></row></sheetData>
</worksheet>""",
        )
        zf.writestr("xl/calcChain.xml", "<calcChain xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><c r='A1'/></calcChain>")
        zf.writestr("xl/pythonScripts.xml", "<pythonScripts><script>x = 1</script></pythonScripts>")
    src.write_bytes(buf.getvalue())

    cell = ConvertedCell(sheet="Sheet1", cell="A1", direction="dag", original_code="x = 1", converted_code="x = 1", data_args=[], converted=True)
    report = ConversionReport(direction="dag", cells=[cell])
    write_dag_formulas_xlsx(src, report, out, strip_python_parts=False)

    with zipfile.ZipFile(out, "r") as zf:
        names = zf.namelist()
        assert "xl/calcChain.xml" not in names
        assert "xl/pythonScripts.xml" in names

        ct = zf.read("[Content_Types].xml").decode("utf-8")
        assert "calcChain" not in ct
        assert "/xl/pythonScripts.xml" in ct

        rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        assert "calcChain" not in rels
        assert "pythonScripts.xml" in rels


def test_ensure_workbook_full_calc_on_load_before_extlst():
    from plugin.calc.excel_py_convert.xlsx_write import _ensure_workbook_full_calc_on_load

    wb = ET.fromstring(
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        '<extLst><ext uri="{0000}"/></extLst>'
        '</workbook>'
    )
    _ensure_workbook_full_calc_on_load(wb)
    tags = [local_name(c.tag) for c in wb]
    assert tags == ["sheets", "calcPr", "extLst"]
    calc_pr = wb.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}calcPr")
    assert calc_pr is not None
    assert calc_pr.attrib.get("fullCalcOnLoad") == "1"


def test_drop_py_code_sheets_places_calc_pr_before_extlst():
    from plugin.calc.excel_py_convert.xlsx_write import _drop_py_code_sheets_from_workbook

    wb_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
  <extLst><ext uri="{0000}"/></extLst>
</workbook>"""
    rels_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

    new_wb_xml, _ = _drop_py_code_sheets_from_workbook(wb_xml, rels_xml, set())
    root = ET.fromstring(new_wb_xml)
    tags = [local_name(c.tag) for c in root]
    assert tags == ["sheets", "calcPr", "extLst"]
    calc_pr = root.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}calcPr")
    assert calc_pr is not None
    assert calc_pr.attrib.get("fullCalcOnLoad") == "1"


def test_write_dag_formulas_places_calc_pr_before_extlst(tmp_path: Path):
    src = tmp_path / "src_extlst.xlsx"
    out = tmp_path / "out_extlst.xlsx"
    sheet_xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><f>_xlfn._xlws.PY(0,0)</f></c></row></sheetData>'
        '</worksheet>'
    )
    wb_xml = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        '<extLst><ext uri="{0000}"/></extLst>'
        '</workbook>'
    )
    rels_xml = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    src.write_bytes(_minimal_xlsx_bytes(sheet_xml=sheet_xml, workbook_xml=wb_xml, rels_xml=rels_xml))

    cell = ConvertedCell(sheet="Sheet1", cell="A1", direction="dag", original_code="x = 1", converted_code="x = 1", data_args=[], converted=True)
    report = ConversionReport(direction="dag", cells=[cell])
    write_dag_formulas_xlsx(src, report, out)

    with zipfile.ZipFile(out, "r") as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        tags = [local_name(c.tag) for c in wb]
        assert "calcPr" in tags
        assert "extLst" in tags
        assert tags.index("calcPr") < tags.index("extLst")


def test_shared_strings_helper(tmp_path: Path):
    """shared_strings extracts strings from xl/sharedStrings.xml or returns [] if absent."""
    from plugin.calc.excel_py_convert.ooxml_util import shared_strings

    zip_path = tmp_path / "test_sst.zip"
    sst_xml = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<si><t>Alpha</t></si>'
        '<si><r><t>Beta</t></r><r><t>Gamma</t></r></si>'
        '<si><other>Ignored</other></si>'
        '</sst>'
    )
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", sst_xml)

    with zipfile.ZipFile(zip_path, "r") as zf:
        assert shared_strings(zf) == ["Alpha", "BetaGamma", ""]

    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")

    with zipfile.ZipFile(empty_zip, "r") as zf:
        assert shared_strings(zf) == []


