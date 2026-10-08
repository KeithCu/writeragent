# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Excel ↔ DAG-style =PY conversion (inline models + synthetic OOXML)."""

from __future__ import annotations

import ast
import io
import zipfile
from pathlib import Path

import pytest

from plugin.calc.excel_py_convert.convert import convert_to_excel, write_dag_formulas_xlsx, write_excel_python_xlsx
from plugin.calc.excel_py_convert.models import ExcelPyCell, ExcelWorkbookModel, SheetInfo
from plugin.calc.excel_py_convert.parse_excel_ooxml import (
    has_excel_python_xlsx,
    load_excel_model,
    parse_xlws_py_formula,
    split_top_level_args,
)
from plugin.calc.excel_py_convert.resolve_refs import resolve_dep
from plugin.calc.excel_py_convert.script_bank import iter_a1_span
from plugin.calc.excel_py_convert.to_dag import (
    _prefer_excel_dep_token,
    _skip_string,
    convert_model_to_dag,
    rewrite_excel_code,
)
from plugin.doc.text_helpers import ast_source_offset
from plugin.calc.excel_py_convert.to_excel import (
    assign_script_bank,
    convert_dag_formula_to_excel,
    expand_placeholders_to_literals,
    python_scripts_xml,
    rewrite_dag_code_to_excel,
    xlws_py_formula,
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
    assert 'xl("%P2%",headers=True)' in code
    assert "to_pandas" not in code
    assert used == ["0"]
    assert modes[0] == "true"
    assert not any("dynamic" in i for i in issues)


def test_rewrite_headers_false_preserved_in_mode():
    code, _issues, _used, modes = rewrite_excel_code("x = xl(%P2%, headers=False)", num_deps=1)
    assert 'xl("%P2%",headers=False)' in code
    assert modes[0] == "false"


def test_rewrite_multi_and_scalar():
    code, issues, used, _modes = rewrite_excel_code(
        "filterDF.groupby(xl(%P2%))[[xl(%P3%)]].agg(xl(%P4%))",
        num_deps=3,
    )
    assert 'xl("%P2%")' in code and 'xl("%P3%")' in code and 'xl("%P4%")' in code
    assert used == ["0", "1", "2"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('df = xl(f"A1:A{n}")', id="test_rewrite_rejects_dynamic"),
        pytest.param('x = xl("A1")', id="test_rewrite_assignment_literal_xl_still_dynamic"),
    ],
)
def test_rewrite_rejects_dynamic(value):
    code, issues, _used, _modes = rewrite_excel_code(value, num_deps=0)
    assert any("dynamic" in i for i in issues)
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
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    assert value_2 in code
    assert value_3 in code
    assert value_4 in code
    assert used == ["0"]
    assert not any("dynamic" in i for i in issues)

def test_rewrite_quoted_placeholder_constant():
    """Quoted ``xl("%P2%")`` is valid Python; AST Constant path must still bind."""
    code, issues, used, modes = rewrite_excel_code('df = xl("%P2%", headers=True)', num_deps=1)
    assert 'xl("%P2%",headers=True)' in code
    assert used == ["0"]
    assert modes[0] == "true"
    assert not any("dynamic" in i for i in issues)


def test_rewrite_non_ascii_prefix_offsets():
    """UTF-8 multi-byte prefix must not shift the ``xl(...)`` rewrite window."""
    src = "café = 1\ndf = xl(%P2%, headers=True)\n"
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    assert "café = 1" in code
    assert 'df = xl("%P2%",headers=True)' in code
    assert used == ["0"]
    assert not any("dynamic" in i for i in issues)


def test_rewrite_ignores_attribute_xl_calls():
    """``obj.xl(...)`` is not the Excel data-bridge builtin — leave it alone."""
    src = "x = obj.xl(%P2%)\ny = xl(%P2%)\n"
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    # Bare ``%Pn%`` is normalized to ``_Pn_`` for AST; attribute call is not rewritten.
    assert "obj.xl(_P2_)" in code or "obj.xl(%P2%)" in code
    assert 'y = xl("%P2%")' in code
    assert used == ["0"]
    assert not any("dynamic" in i for i in issues)


def test_rewrite_keeps_xl_statement_under_if():
    """Statement ``xl`` under ``if`` stays as a quoted binding (no strip/pass)."""
    src = "if config_param == 3:\n    xl(%P2%)\n"
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    assert 'xl("%P2%")' in code
    assert "pass" not in code
    assert used == ["0"]
    assert not any("dynamic" in i for i in issues)
    ast.parse(code)


def test_rewrite_literal_xl_statement_fail_closed():
    """Literal statement ``xl("A1")`` under ``if`` is dynamic — not silently stripped."""
    src = 'if config_param == 3:\n    xl("A1")\n'
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=0)
    assert 'xl("A1")' in code
    assert any("dynamic" in i for i in issues)
    assert used == []


def test_rewrite_keeps_xl_statement_with_siblings():
    src = "if True:\n    xl(%P2%)\n    x = 1\n"
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    assert 'xl("%P2%")' in code
    assert "x = 1" in code
    assert used == ["0"]
    ast.parse(code)


def test_rewrite_top_level_xl_still_egress_binding():
    """Sole / last top-level ``xl(%P2%)`` remains Jupyter last-expression → ``xl("%P2%")``."""
    code, issues, used, _modes = rewrite_excel_code("xl(%P2%)", num_deps=1)
    assert code.strip() == 'xl("%P2%")'
    assert used == ["0"]
    assert not any("dynamic" in i for i in issues)

def test_rewrite_multiline_xl_statement_quoted():
    src = "if True:\n    xl(\n        %P2%,\n        headers=True,\n    )\n"
    code, issues, used, _modes = rewrite_excel_code(src, num_deps=1)
    assert 'xl("%P2%",headers=True)' in code
    assert "pass" not in code
    assert used == ["0"]
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
    code, issues, used, _modes = rewrite_excel_code("df = xl(%P2%, headers=True\n", num_deps=1)
    assert any("syntax error" in i for i in issues)
    assert "xl(%P2%" in code  # unchanged
    assert used == []


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
    excel_code, deps, issues = rewrite_dag_code_to_excel(dag_code, ["A3:F23"], header_modes=["true"])
    assert "xl(%P2%,headers=True)" in excel_code
    assert deps == ["A3:F23"]
    assert not issues
    again, _issues2, _used, modes = rewrite_excel_code(excel_code, num_deps=1)
    assert 'xl("%P2%",headers=True)' in again
    assert modes[0] == "true"


def test_roundtrip_multi_dep_data_and_ranges():
    """``data[i]`` / ``ranges[i]`` reverse to ``xl(%Pn%)``; re-import keeps ``xl("%Pn%")``."""
    dag_code = "a = data[0]\nb = ranges[1]\nc = data[2].to_pandas()\n"
    deps = ["A1:A2", "B1:B2", "C1:C2"]
    excel_code, out_deps, issues = rewrite_dag_code_to_excel(
        dag_code, deps, header_modes=["omit", "omit", "true"]
    )
    assert out_deps == deps
    assert "xl(%P2%)" in excel_code
    assert "xl(%P3%)" in excel_code
    assert "xl(%P4%,headers=True)" in excel_code
    assert "ranges[" not in excel_code
    assert not any("ambiguous" in i for i in issues)
    again, _issues2, used, modes = rewrite_excel_code(excel_code, num_deps=3)
    assert 'xl("%P2%")' in again and 'xl("%P3%")' in again and 'xl("%P4%",headers=True)' in again
    assert used == ["0", "1", "2"]
    assert modes[2] == "true"


def test_export_passthrough_quoted_xl_bindings():
    """DAG code that already has ``xl("%Pn%")`` unquotes for the Excel package."""
    dag_code = 'df = xl("%P2%",headers=True)\nx = xl("%P3%")\n'
    excel_code, deps, issues = rewrite_dag_code_to_excel(
        dag_code, ["A1:B2", "C1"], header_modes=["true", "omit"]
    )
    assert deps == ["A1:B2", "C1"]
    assert "xl(%P2%,headers=True)" in excel_code
    assert "xl(%P3%)" in excel_code
    assert '"%P' not in excel_code
    assert not issues


def test_reverse_non_ascii_prefix_offsets():
    """UTF-8 multi-byte prefix must not shift reverse AST rewrite of ``data`` / ``ranges``."""
    dag_code = "café = 1\nx = data[0]\ny = ranges[1]\n"
    excel_code, deps, issues = rewrite_dag_code_to_excel(dag_code, ["A1", "B1"], header_modes=["omit", "omit"])
    assert "café = 1" in excel_code
    assert "x = xl(%P2%)" in excel_code
    assert "y = xl(%P3%)" in excel_code
    assert "ranges[" not in excel_code
    assert deps == ["A1", "B1"]
    assert not any("ambiguous" in i for i in issues)


def test_iter_a1_span_expands_ranges():
    assert iter_a1_span("A1") == ["A1"]
    assert iter_a1_span("A1:B2") == ["A1", "B1", "A2", "B2"]
    assert iter_a1_span("Sheet1!B2:C3") == ["B2", "C2", "B3", "C3"]
    assert iter_a1_span("") == []


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
    assert escape_code_for_excel_formula(code) == code
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
    assert "A3:F23" in expand_placeholders_to_literals(cell.converted_code, cell.data_args)
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
    scripts, _ = assign_script_bank([a, b, c])
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
    model = load_excel_model(out, prefer_openpyxl_anchors=False)
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


def test_single_line_package_files_survive_strip(tmp_path: Path):
    """Single-line [Content_Types].xml and .rels from Excel/LO must survive _strip_python_in_excel_parts."""
    from xml.etree import ElementTree as ET
    from plugin.calc.excel_py_convert.convert import _strip_python_in_excel_parts

    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"

    # Minified single-line content types and rels as emitted by Excel / LibreOffice
    single_line_ct = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{ct_ns}">'
        f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        f'<Override PartName="/xl/pythonScripts.xml" ContentType="application/xml"/>'
        f'</Types>'
    )
    single_line_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{rel_ns}">'
        f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2022/relationships/pythonScript" Target="pythonScripts.xml"/>'
        f'</Relationships>'
    )

    xlsx = tmp_path / "single_line_strip.xlsx"
    with zipfile.ZipFile(xlsx, "w") as zf:
        zf.writestr("[Content_Types].xml", single_line_ct)
        zf.writestr("xl/_rels/workbook.xml.rels", single_line_rels)
        zf.writestr("xl/pythonScripts.xml", "<pythonScripts/>")
        zf.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")

    _strip_python_in_excel_parts(xlsx)

    with zipfile.ZipFile(xlsx, "r") as zf:
        assert "xl/pythonScripts.xml" not in zf.namelist()
        ct_root = ET.fromstring(zf.read("[Content_Types].xml"))
        ct_overrides = [
            c.attrib.get("PartName")
            for c in ct_root
            if c.tag.endswith("Override")
        ]
        assert "/xl/workbook.xml" in ct_overrides
        assert "/xl/worksheets/sheet1.xml" in ct_overrides
        assert "/xl/pythonScripts.xml" not in ct_overrides
        defaults = [
            c.attrib.get("Extension")
            for c in ct_root
            if c.tag.endswith("Default")
        ]
        assert "rels" in defaults
        assert "xml" in defaults

        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
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
    ws2 = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><is><t>x = 1</t></is></c></row></sheetData>'
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
    """Two source sheets mapping to the same py_code_* bank name must raise ValueError."""
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
    from plugin.calc.excel_py_convert.script_bank import collect_script_bank, code_sheet_name_for

    # 'My Sheet' and 'My_Sheet' both sanitize to 'py_code_My_Sheet'
    assert code_sheet_name_for("My Sheet") == code_sheet_name_for("My_Sheet")

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

    with pytest.raises(ValueError, match="script-bank sheet name collision"):
        collect_script_bank(report)

    assert not report.ok
    assert any("collision" in issue for issue in report.issues)


def test_script_bank_truncation_collision_raises():
    """Two long sheet names truncating to the same bank name must raise ValueError."""
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport
    from plugin.calc.excel_py_convert.script_bank import collect_script_bank, code_sheet_name_for

    name1 = "VeryLongSheetNameThatWillTruncate1"
    name2 = "VeryLongSheetNameThatWillTruncate2"
    assert code_sheet_name_for(name1) == code_sheet_name_for(name2)

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

    with pytest.raises(ValueError, match="script-bank sheet name collision"):
        collect_script_bank(report)


def test_write_dag_formulas_xlsx_closes_workbook_on_error(tmp_path: Path):
    """write_dag_formulas_xlsx must close the openpyxl workbook in try/finally on all errors."""
    from plugin.calc.excel_py_convert.convert import write_dag_formulas_xlsx
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
    """Temporary files (.tmpstrip, .tmprewrite, .tmpexcelpy) must be cleaned up on exception."""
    from plugin.calc.excel_py_convert.convert import (
        _strip_python_in_excel_parts,
        write_excel_python_xlsx,
    )
    from plugin.calc.excel_py_convert.models import ConvertedCell, ConversionReport

    src = tmp_path / "temp_clean_src.xlsx"
    src.write_bytes(_dag_xlsx_bytes())

    # 1. Test .tmpstrip cleanup
    def fail_writestr(*_args, **_kwargs):
        raise RuntimeError("simulated write error")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_writestr)

    with pytest.raises(RuntimeError):
        _strip_python_in_excel_parts(src)
    assert not (src.with_suffix(src.suffix + ".tmpstrip")).exists()

    # 2. Test rewrite_zip cleanup
    from plugin.calc.excel_py_convert.ooxml_util import rewrite_zip

    def fail_transform(zin, zout):
        raise RuntimeError("simulated transform error")

    with pytest.raises(RuntimeError):
        rewrite_zip(src, src.with_suffix(src.suffix + ".out"), fail_transform, temp_suffix=".tmprewrite")
    assert not (src.with_suffix(src.suffix + ".out.tmprewrite")).exists()

    # 3. Test .tmpexcelpy cleanup
    monkeypatch.undo()
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
    from plugin.calc.excel_py_convert.convert import write_excel_python_xlsx
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
        formulas = {
            c.attrib.get("r"): "".join(c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f").itertext())
            for c in ws_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c")
            if c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}f") is not None
        }
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
        b'</Types>'
    )
    from plugin.calc.excel_py_convert.xlsx_write import _patch_content_types, _strip_content_types_python

    # 1. Test stripping python parts
    stripped = _strip_content_types_python(ct_data)
    assert b"pythonScripts.xml" not in stripped
    assert b'mc:Ignorable="x14ac xr"' in stripped
    assert b'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"' in stripped
    assert b'xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision"' in stripped

    # 2. Test patching python parts
    patched = _patch_content_types(stripped)
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
    res_ff = rewrite_excel_code(src_ff, num_deps=1)
    assert not res_ff.fatal
    assert 'y = xl("%P2%")' in res_ff.code
    assert 's = "hello\x0cworld"' in res_ff.code

    # Test 2: \u2028 inside string literal
    src_u2028 = 's = "hello\u2028world"\ny = xl(%P2%)\n'
    res_u2028 = rewrite_excel_code(src_u2028, num_deps=1)
    assert not res_u2028.fatal
    assert 'y = xl("%P2%")' in res_u2028.code
    assert 's = "hello\u2028world"' in res_u2028.code

    # Test 3: \x0c and \u2028 inside comments
    src_comm_ff = '# note \x0c here\ny = xl(%P2%)\n'
    res_comm_ff = rewrite_excel_code(src_comm_ff, num_deps=1)
    assert not res_comm_ff.fatal
    assert 'y = xl("%P2%")' in res_comm_ff.code

    src_comm_u2028 = '# note \u2028 here\ny = xl(%P2%)\n'
    res_comm_u2028 = rewrite_excel_code(src_comm_u2028, num_deps=1)
    assert not res_comm_u2028.fatal
    assert 'y = xl("%P2%")' in res_comm_u2028.code

    # Test 4: direct ast_source_offset indexing
    assert ast_source_offset(src_ff, 2, 4) == len('s = "hello\x0cworld"\n') + 4
    assert ast_source_offset(src_u2028, 2, 4) == len('s = "hello\u2028world"\n') + 4


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
    res = rewrite_excel_code(code, num_deps=1)
    assert not res.fatal
    assert 'x = xl("%P2%")' in res.code


