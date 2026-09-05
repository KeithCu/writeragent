# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/generate_pretty_demo_spreadsheet.py."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from scripts.generate_pretty_demo_spreadsheet import (
    CALC_PYTHON_ADDIN_FN,
    SALES_RANGE_ODS_CROSS,
    SALES_ZIPS_BY_REGION,
    SQL_SALES_ZIP_INCOME_JOIN,
    ZIP_INCOME_CSV_NAME,
    ZIP_INCOME_FIXTURE,
    _ODS_SHEET_COLUMNS,
    build_ods_showcase,
    get_sales_dataset,
    ods_formula,
    sql_demo_scenarios,
    write_zip_income_csv,
)

FIXTURE_ODS = Path(__file__).resolve().parents[1] / "fixtures" / "python_showcase_demo.ods"
_NESTED_SHEET_REF = re.compile(r"\[\$[A-Za-z0-9_]*\[\$")


def _ods_content_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("content.xml").decode("utf-8")


def test_ods_formula_sales_analytics_range_not_rematched() -> None:
    out = ods_formula('=PY("x"; Sales_Analytics.A5:I40)')
    assert "[$Sales_Analytics.A5:.I40]" in out
    assert _NESTED_SHEET_REF.search(out) is None
    assert "[$S[$ales_Analytics" not in out
    assert out.startswith(f"of:={CALC_PYTHON_ADDIN_FN}(")


def test_ods_formula_forecasting_range_not_rematched() -> None:
    out = ods_formula('=PY("y"; Forecasting.A1:B2)')
    assert "[$Forecasting.A1:.B2]" in out
    assert _NESTED_SHEET_REF.search(out) is None
    assert "[$F[$orecasting" not in out


def test_ods_formula_same_sheet_range_and_cell() -> None:
    out = ods_formula('=PY("sum(r[7] for r in data[1:])"; A5:I40; F46)')
    assert "[.A5:.I40]" in out
    assert "[.F46]" in out
    assert "A5:I40" not in out
    assert out.startswith(f"of:={CALC_PYTHON_ADDIN_FN}(")


def test_ods_formula_python_wrapper_and_cross_sheet_cell() -> None:
    out = ods_formula('=PYTHON("x"; Sales_Analytics.F47)')
    assert out.startswith(f"of:={CALC_PYTHON_ADDIN_FN}(")
    assert "[$Sales_Analytics.F47]" in out
    assert _NESTED_SHEET_REF.search(out) is None


def test_ods_formula_leaves_non_formula_text() -> None:
    assert ods_formula("plain") == "plain"


def test_ods_formula_statistics_ml_range_not_rematched() -> None:
    out = ods_formula('=PY("x"; Statistics_ML.A5:G25)')
    assert "[$Statistics_ML.A5:.G25]" in out
    assert _NESTED_SHEET_REF.search(out) is None


def test_sales_dataset_has_zip_column_from_real_zctas() -> None:
    grid = get_sales_dataset()
    assert grid[0][-1] == "ZIP"
    assert grid[0].index("Revenue") == 7
    zips = {str(row[-1]) for row in grid[1:]}
    allowed = {z for group in SALES_ZIPS_BY_REGION.values() for z in group}
    assert zips <= allowed
    assert zips  # at least one assigned


def test_sql_demo_scenarios_include_sheet_and_join() -> None:
    kinds = {s["kind"] for s in sql_demo_scenarios()}
    assert kinds == {"sheet_sales", "sheet_marketing", "join_zip"}
    join = next(s for s in sql_demo_scenarios() if s["kind"] == "join_zip")
    assert "zip_income" in join["sql"]
    assert join["sql"] == SQL_SALES_ZIP_INCOME_JOIN
    assert "ZIP" in join["sql"] or "zip" in join["sql"]


def test_zip_income_csv_is_fuller_acs_extract_and_covers_sales_zips() -> None:
    assert ZIP_INCOME_FIXTURE.is_file()
    lines = ZIP_INCOME_FIXTURE.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("zip,median_household_income")
    # Fuller ZCTA extract (not a 40-row toy); ACS has ~33k ZCTAs.
    assert len(lines) > 5000
    csv_zips = {line.split(",", 1)[0] for line in lines[1:] if line.strip()}
    sales_zips = {z for group in SALES_ZIPS_BY_REGION.values() for z in group}
    missing = sales_zips - csv_zips
    assert not missing, f"sales ZIPs missing from ACS extract: {missing}"


def test_write_zip_income_csv_copies_sibling(tmp_path: Path) -> None:
    dest = write_zip_income_csv(tmp_path)
    assert dest.name == ZIP_INCOME_CSV_NAME
    assert dest.is_file()
    assert dest.stat().st_size == ZIP_INCOME_FIXTURE.stat().st_size


def test_generated_ods_has_sql_duckdb_sheet(tmp_path: Path) -> None:
    out = tmp_path / "python_showcase_demo.ods"
    build_ods_showcase(out)
    write_zip_income_csv(tmp_path)
    assert out.is_file()
    assert (tmp_path / ZIP_INCOME_CSV_NAME).is_file()

    from odf.opendocument import load
    from odf.table import Table
    from odf.text import P

    doc = load(str(out))
    names = [str(t.getAttribute("name")) for t in doc.spreadsheet.getElementsByType(Table)]
    assert "Sales_Analytics" in names
    assert "SQL_DuckDB" in names
    sql_sheet = next(t for t in doc.spreadsheet.getElementsByType(Table) if t.getAttribute("name") == "SQL_DuckDB")
    text = "\n".join(str(p) for p in sql_sheet.getElementsByType(P))
    assert "zip_income" in text
    assert "GROUP BY" in text or "Region" in text


def _assert_ods_formulas_and_layout(xml: str) -> None:
    sales_of = f"[${SALES_RANGE_ODS_CROSS.replace(':', ':.')}]"
    assert sales_of in xml
    assert "[$Forecasting.A5:.E41]" in xml
    assert "[$S[$ales_Analytics" not in xml
    assert "[$F[$orecasting" not in xml
    assert "[$S[$tatistics_ML" not in xml
    assert _NESTED_SHEET_REF.search(xml) is None
    assert xml.count("<table:table-column") >= sum(len(cols) for cols in _ODS_SHEET_COLUMNS.values())
    assert "style:column-width" in xml
    assert "style:row-height" in xml
    assert "SQL_DuckDB" in xml


def test_build_ods_showcase_formulas_and_layout(tmp_path: Path) -> None:
    out_path = tmp_path / "python_showcase_demo.ods"
    build_ods_showcase(out_path)
    _assert_ods_formulas_and_layout(_ods_content_xml(out_path))


def test_shipped_ods_fixture_formulas_and_layout() -> None:
    _assert_ods_formulas_and_layout(_ods_content_xml(FIXTURE_ODS))
