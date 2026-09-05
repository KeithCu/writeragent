# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.duckdb_sql (Phase A folder SQL path guard + execution)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pandas")

from plugin.scripting.venv.duckdb_sql import query_folder_sql


def _write_csv(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip() + "\n")


def test_query_folder_sql_happy_join(tmp_path):
    sales = tmp_path / "sales.csv"
    _write_csv(
        sales,
        """
        region,amount
        North,100
        South,200
        North,150
        """,
    )
    costs = tmp_path / "costs.csv"
    _write_csv(
        costs,
        """
        region,cost
        North,30
        South,90
        """,
    )

    sql = "SELECT s.region, SUM(s.amount) - SUM(c.cost) AS profit FROM 'sales.csv' s JOIN 'costs.csv' c USING (region) GROUP BY 1 ORDER BY 1"
    res = query_folder_sql(str(tmp_path), sql, files=["sales.csv", "costs.csv"])

    assert res["status"] == "ok"
    assert res["helper"] == "query_folder_sql"
    assert "region" in res["columns"]
    assert res["total_rows"] >= 2
    # profit North=220, South=110
    rows = res["rows"]
    assert any("North" in str(r) for r in rows)


def test_query_folder_sql_rejects_escape(tmp_path):
    evil = tmp_path.parent / "evil.csv"
    _write_csv(evil, "x,y\n1,2")
    # attempt via files=
    res = query_folder_sql(str(tmp_path), "SELECT * FROM 'evil.csv'", files=["../evil.csv"])
    assert res["status"] == "error"
    assert "NO_ALLOWED_FILES" in res.get("code", "") or "READONLY" in res.get("code", "") or "outside" in res.get("message", "").lower()

    # attempt via sql literal with ..
    good = tmp_path / "ok.csv"
    _write_csv(good, "a,b\n9,9")
    res2 = query_folder_sql(str(tmp_path), "SELECT * FROM '../evil.csv'", files=["ok.csv"])
    assert res2["status"] == "error"
    assert "READONLY_VIOLATION" in res2.get("code", "") or "escape" in res2.get("message", "").lower()


def test_query_folder_sql_readonly_blocks_write(tmp_path):
    f = tmp_path / "t.csv"
    _write_csv(f, "id,val\n1,10")
    res = query_folder_sql(str(tmp_path), "COPY (SELECT * FROM 't.csv') TO 'out.csv'", files=["t.csv"])
    assert res["status"] == "error"
    assert "READONLY_VIOLATION" in res["code"]


def test_query_folder_sql_missing_package(monkeypatch, tmp_path):
    real = __import__
    def fake_import(name, *a, **k):
        if name == "duckdb":
            raise ImportError("no duckdb")
        return real(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", fake_import)
    res = query_folder_sql(str(tmp_path), "select 1", [])
    assert res["status"] == "error"
    assert "MISSING_PACKAGE" in res["code"]


def test_query_folder_sql_requires_scoped_and_files(tmp_path):
    res = query_folder_sql(None, "select 1")
    assert res["status"] == "error"

    res = query_folder_sql(str(tmp_path), "select 1", files=[])
    assert res["status"] == "error"
    assert "NO_ALLOWED" in res.get("code", "") or "allowed" in res.get("message", "").lower()


def test_query_folder_sql_preloaded_compact_grid(tmp_path):
    """Host used-range path ships a tight grid; worker must register it as-is (no padding)."""
    grid = [["Region", "Sales"], ["North", 100], ["South", 200]]
    res = query_folder_sql(
        str(tmp_path),
        "SELECT Region, Sales FROM budget ORDER BY 1",
        files=None,
        preloaded={"budget": {"grid": grid, "headers": True}},
    )
    assert res["status"] == "ok", res
    assert res["total_rows"] == 2
    assert len(res["columns"]) == 2
    assert not any(len(row) > 2 for row in res["rows"])


def _pretty_demo_sales_grid():
    from scripts.generate_pretty_demo_spreadsheet import get_sales_dataset

    return get_sales_dataset()


def _pretty_demo_marketing_grid():
    from scripts.generate_pretty_demo_spreadsheet import get_marketing_dataset

    return get_marketing_dataset()


def _zip_income_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "zip_income.csv"


def test_pretty_demo_sheet_sql_sales_groupby_region_category():
    """Happy path: real DuckDB GROUP BY on the pretty-demo sales grid (not wallpaper)."""
    from scripts.generate_pretty_demo_spreadsheet import SQL_SALES_BY_REGION_CATEGORY

    res = query_folder_sql(
        None,
        SQL_SALES_BY_REGION_CATEGORY,
        preloaded={"sales": _pretty_demo_sales_grid()},
    )
    assert res["status"] == "ok", res
    assert res["helper"] == "query_folder_sql"
    cols = [str(c).lower() for c in res["columns"]]
    assert "region" in cols and "category" in cols and "revenue" in cols
    assert res["total_rows"] >= 8
    # Independent pandas check — proves DuckDB actually aggregated the grid.
    import pandas as pd

    grid = _pretty_demo_sales_grid()
    df = pd.DataFrame(grid[1:], columns=grid[0])
    expected = df.groupby(["Region", "Category"], as_index=False)["Revenue"].sum()
    got = { (r[cols.index("region")], r[cols.index("category")]): float(r[cols.index("revenue")]) for r in res["rows"] }
    for rec in expected.itertuples(index=False):
        assert abs(got[(rec.Region, rec.Category)] - float(rec.Revenue)) < 1e-6


def test_pretty_demo_sheet_sql_marketing_channel_roas():
    """Second sheet-only KPI: marketing ROAS via the same query_folder_sql helper."""
    from scripts.generate_pretty_demo_spreadsheet import SQL_MARKETING_CHANNEL_ROAS

    res = query_folder_sql(
        None,
        SQL_MARKETING_CHANNEL_ROAS,
        preloaded={"marketing": _pretty_demo_marketing_grid()},
    )
    assert res["status"] == "ok", res
    cols = [str(c).lower() for c in res["columns"]]
    assert "channel" in cols and "roas" in cols
    assert res["total_rows"] >= 4
    import pandas as pd

    grid = _pretty_demo_marketing_grid()
    df = pd.DataFrame(grid[1:], columns=grid[0])
    search = df[df["Channel"] == "Search Ads"]
    expected_roas = round(float(search["Revenue"].sum() / search["Ad_Spend"].sum()), 2)
    roas_idx = cols.index("roas")
    ch_idx = cols.index("channel")
    search_row = next(r for r in res["rows"] if r[ch_idx] == "Search Ads")
    assert abs(float(search_row[roas_idx]) - expected_roas) < 1e-6


def test_pretty_demo_join_sales_to_sibling_zip_income():
    """Happy path: sheet sales ⨝ sibling ACS zip_income.csv through query_folder_sql."""
    from scripts.generate_pretty_demo_spreadsheet import (
        SQL_SALES_ZIP_INCOME_JOIN,
        SALES_ZIPS_BY_REGION,
    )

    csv_path = _zip_income_path()
    assert csv_path.is_file()
    scoped = str(csv_path.parent)
    res = query_folder_sql(
        scoped,
        SQL_SALES_ZIP_INCOME_JOIN,
        preloaded={"sales": _pretty_demo_sales_grid()},
        flat_files={"zip_income": str(csv_path)},
    )
    assert res["status"] == "ok", res
    assert res["helper"] == "query_folder_sql"
    cols = [str(c).lower() for c in res["columns"]]
    assert "income_band" in cols and "revenue" in cols and "avg_zip_income" in cols
    assert res["total_rows"] >= 2
    # Every sales ZIP must exist in the ACS extract or the join would drop rows.
    sales_zips = {z for zips in SALES_ZIPS_BY_REGION.values() for z in zips}
    csv_zips = {
        line.split(",", 1)[0]
        for line in csv_path.read_text(encoding="utf-8").splitlines()[1:]
        if line.strip()
    }
    assert sales_zips <= csv_zips
    # Revenue across bands equals full sales revenue (inner join kept every order).
    rev_idx = cols.index("revenue")
    joined_rev = sum(float(r[rev_idx]) for r in res["rows"])
    grid = _pretty_demo_sales_grid()
    sheet_rev = sum(float(r[7]) for r in grid[1:])
    assert abs(joined_rev - sheet_rev) < 0.02
    assert "zip_income" in str(res.get("files_used", []))


def test_pretty_demo_results_code_reads_sql_from_cell_range():
    """The short RESULTS =PY() payload runs the SQL sitting in data[1], not a copy."""
    from plugin.scripting.calc_range import CalcRange
    from scripts.generate_pretty_demo_spreadsheet import (
        SQL_SALES_BY_REGION_CATEGORY,
        duckdb_sql_from_cell_code,
        sql_query_lines,
    )

    data = [
        CalcRange(_pretty_demo_sales_grid()),
        CalcRange([[line] for line in sql_query_lines(SQL_SALES_BY_REGION_CATEGORY)]),
    ]
    ns: dict[str, object] = {"data": data}
    exec(duckdb_sql_from_cell_code("sales"), ns)  # noqa: S102 — exact demo payload
    result = ns["result"]
    assert list(result.columns) == ["Region", "Category", "revenue", "orders"]
    assert len(result) >= 8
    import pandas as pd

    grid = _pretty_demo_sales_grid()
    df = pd.DataFrame(grid[1:], columns=grid[0])
    expected = df.groupby(["Region", "Category"], as_index=False)["Revenue"].sum()
    got = {(r.Region, r.Category): float(r.revenue) for r in result.itertuples(index=False)}
    for rec in expected.itertuples(index=False):
        assert abs(got[(rec.Region, rec.Category)] - float(rec.Revenue)) < 1e-6
