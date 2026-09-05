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

from plugin.scripting.venv.duckdb_sql import (
    MAX_TABLE_ROWS,
    GuardedDuckDBConnection,
    ReadonlyViolation,
    invalidate_session_tables,
    persistable_duckdb_session_id,
    query_folder_sql,
    reset_session_duckdb,
    run_sql,
    session_duckdb,
    _looks_like_write_or_escape,
)


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
    # attempt via files= — basename-only resolve; ``../`` is a path escape
    res = query_folder_sql(str(tmp_path), "SELECT * FROM 'evil.csv'", files=["../evil.csv"])
    assert res["status"] == "error"
    assert res.get("code") == "READONLY_VIOLATION"

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
    """Folder files need a host-resolved scoped_dir; in-memory SELECT 1 does not."""
    res = query_folder_sql(None, "SELECT * FROM 'x.csv'", files=["x.csv"])
    assert res["status"] == "error"
    assert res.get("code") == "MISSING_SCOPED_DIR"

    res = query_folder_sql(None, "select 1")
    assert res["status"] == "ok", res
    assert res["total_rows"] == 1

    res = query_folder_sql(str(tmp_path), "select 1", files=[])
    assert res["status"] == "ok", res

    res = query_folder_sql(str(tmp_path), "SELECT * FROM missing", files=["nope.csv"])
    assert res["status"] == "error"
    assert res.get("code") == "MISSING_FILE"


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
    ns: dict[str, object] = {"data": data, "session_duckdb": session_duckdb}
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


def _write_parquet(path: Path, rows: list[tuple[str, int]]) -> None:
    """Write a tiny region/amount Parquet via DuckDB (no pyarrow required)."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE t (region VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO t VALUES (?, ?)", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


def _write_json_array(path: Path, rows: list[dict[str, object]]) -> None:
    import json

    path.write_text(json.dumps(rows), encoding="utf-8")


def _write_ndjson(path: Path, rows: list[dict[str, object]]) -> None:
    import json

    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_query_folder_sql_parquet_flat_files(tmp_path):
    pq = tmp_path / "ledger.parquet"
    _write_parquet(pq, [("North", 100), ("South", 200), ("North", 50)])
    res = query_folder_sql(
        str(tmp_path),
        "SELECT region, SUM(amount) AS total FROM ledger GROUP BY 1 ORDER BY 1",
        flat_files={"ledger": str(pq)},
    )
    assert res["status"] == "ok", res
    assert res["total_rows"] == 2
    rows = {r[0]: int(r[1]) for r in res["rows"]}
    assert rows == {"North": 150, "South": 200}
    assert "ledger" in res.get("files_used", [])


def test_query_folder_sql_parquet_files_dict(tmp_path):
    _write_parquet(tmp_path / "ledger.parquet", [("East", 9)])
    res = query_folder_sql(
        str(tmp_path),
        "SELECT region, amount FROM ledger",
        files={"ledger": "ledger.parquet"},
    )
    assert res["status"] == "ok", res
    assert res["rows"] == [["East", 9]]


def test_query_folder_sql_json_array_and_ndjson(tmp_path):
    events = tmp_path / "events.json"
    _write_json_array(events, [{"region": "North", "n": 1}, {"region": "South", "n": 2}])
    lines = tmp_path / "rows.jsonl"
    _write_ndjson(lines, [{"region": "North", "n": 10}, {"region": "South", "n": 20}])
    nd = tmp_path / "more.ndjson"
    _write_ndjson(nd, [{"region": "East", "n": 3}])

    array_res = query_folder_sql(
        str(tmp_path),
        "SELECT region, n FROM events ORDER BY 1",
        flat_files={"events": str(events)},
    )
    assert array_res["status"] == "ok", array_res
    assert array_res["rows"] == [["North", 1], ["South", 2]]

    jsonl_res = query_folder_sql(
        str(tmp_path),
        "SELECT SUM(n) AS total FROM rows",
        files={"rows": "rows.jsonl"},
    )
    assert jsonl_res["status"] == "ok", jsonl_res
    assert int(jsonl_res["rows"][0][0]) == 30

    nd_res = query_folder_sql(
        str(tmp_path),
        "SELECT region FROM more",
        flat_files={"more": str(nd)},
    )
    assert nd_res["status"] == "ok", nd_res
    assert nd_res["rows"] == [["East"]]


def test_query_folder_sql_join_parquet_and_csv(tmp_path):
    _write_parquet(tmp_path / "ledger.parquet", [("North", 100), ("South", 40)])
    _write_csv(
        tmp_path / "rates.csv",
        """
        region,rate
        North,2
        South,3
        """,
    )
    res = query_folder_sql(
        str(tmp_path),
        "SELECT l.region, l.amount * r.rate AS weighted FROM ledger l JOIN rates r USING (region) ORDER BY 1",
        files={"ledger": "ledger.parquet", "rates": "rates.csv"},
    )
    assert res["status"] == "ok", res
    rows = {r[0]: int(r[1]) for r in res["rows"]}
    assert rows == {"North": 200, "South": 120}


def test_query_folder_sql_unsupported_and_missing_flat_types(tmp_path):
    (tmp_path / "notes.txt").write_text("a,b\n1,2\n", encoding="utf-8")
    unsupported = query_folder_sql(
        str(tmp_path),
        "SELECT * FROM notes",
        files={"notes": "notes.txt"},
    )
    assert unsupported["status"] == "error"
    assert unsupported.get("code") == "UNSUPPORTED_FILE_TYPE"
    assert ".txt" in unsupported.get("message", "")
    assert "parquet" in unsupported.get("message", "").lower()

    missing = query_folder_sql(
        str(tmp_path),
        "SELECT * FROM ledger",
        flat_files={"ledger": str(tmp_path / "ledger.parquet")},
    )
    assert missing["status"] == "error"
    assert missing.get("code") == "MISSING_FILE"


def test_query_folder_sql_corrupt_parquet_is_read_error(tmp_path):
    bad = tmp_path / "ledger.parquet"
    bad.write_bytes(b"not a parquet file")
    res = query_folder_sql(
        str(tmp_path),
        "SELECT * FROM ledger",
        flat_files={"ledger": str(bad)},
    )
    assert res["status"] == "error"
    assert res.get("code") == "FLAT_FILE_READ_ERROR"
    assert "ledger.parquet" in res.get("message", "")


def test_pretty_demo_join_results_code_runs_via_run_sql():
    """Live ZIP-join RESULTS payload: data[0] sheet ⨝ sibling CSV via run_sql."""
    from plugin.scripting.calc_range import CalcRange
    from scripts.generate_pretty_demo_spreadsheet import (
        SQL_SALES_ZIP_INCOME_JOIN,
        ZIP_INCOME_CSV_NAME,
        duckdb_join_from_cell_code,
        sql_query_lines,
    )

    csv_path = _zip_income_path()
    data = [
        CalcRange(_pretty_demo_sales_grid()),
        CalcRange([[line] for line in sql_query_lines(SQL_SALES_ZIP_INCOME_JOIN)]),
    ]
    ns: dict[str, object] = {
        "data": data,
        "run_sql": run_sql,
        "scoped_dir": str(csv_path.parent),
    }
    exec(duckdb_join_from_cell_code("sales", ZIP_INCOME_CSV_NAME), ns)  # noqa: S102
    result = ns["result"]
    cols = [str(c).lower() for c in result.columns]
    assert "income_band" in cols and "revenue" in cols
    assert len(result) >= 2
    rev_col = result.columns[cols.index("revenue")]
    joined_rev = float(result[rev_col].sum())
    grid = _pretty_demo_sales_grid()
    sheet_rev = sum(float(r[7]) for r in grid[1:])
    assert abs(joined_rev - sheet_rev) < 0.02


def test_run_sql_registers_calcrange_preloaded(tmp_path: Path):
    """=PY() data[0] is a CalcRange; bool(range) must not skip registration."""
    from plugin.scripting.calc_range import CalcRange

    income = tmp_path / "zip_income.csv"
    _write_csv(income, "code,median_household_income\nNORTH,50000\n")
    df = run_sql(
        "SELECT z.median_household_income AS inc FROM sales s JOIN zip_income z ON s.code = z.code",
        preloaded={"sales": CalcRange([["code"], ["NORTH"]])},
        files={"zip_income": "zip_income.csv"},
        scoped_dir=str(tmp_path),
    )
    assert len(df) == 1
    assert int(df.iloc[0]["inc"]) == 50000


def test_run_sql_returns_dataframe_for_preloaded_and_sibling_csv(tmp_path: Path):
    sales = tmp_path / "ignored.csv"
    _write_csv(sales, "zip,amount\n02116,10\n")
    income = tmp_path / "zip_income.csv"
    _write_csv(income, "zip,median_household_income\n02116,120000\n")
    df = run_sql(
        "SELECT s.amount, z.median_household_income FROM sales s JOIN zip_income z ON s.zip = z.zip",
        preloaded={"sales": [["zip", "amount"], ["02116", 10]]},
        files={"zip_income": "zip_income.csv"},
        scoped_dir=str(tmp_path),
    )
    assert list(df.columns) == ["amount", "median_household_income"]
    assert len(df) == 1
    assert int(df.iloc[0]["amount"]) == 10
    assert int(df.iloc[0]["median_household_income"]) == 120000


def test_run_sql_raises_when_scoped_dir_missing_for_files():
    with pytest.raises(RuntimeError, match="scoped_dir"):
        run_sql(
            "SELECT 1",
            files={"zip_income": "zip_income.csv"},
            scoped_dir=None,
        )


def test_run_sql_raises_on_query_error():
    with pytest.raises(RuntimeError):
        run_sql("SELECT * FROM definitely_missing", preloaded={"sales": [["x"], [1]]})


# --- Phase D: shared-kernel DuckDB session cache ---


@pytest.fixture
def _clean_duckdb_sessions():
    reset_session_duckdb()
    from plugin.scripting.venv.venv_sandbox import clear_all_sandbox_sessions

    clear_all_sandbox_sessions()
    yield
    reset_session_duckdb()
    clear_all_sandbox_sessions()


def _tiny_grid(value: int) -> list[list[object]]:
    return [["x"], [value]]


def test_persistable_session_id_workbook_only():
    assert persistable_duckdb_session_id("calc:wb-1") == "calc:wb-1"
    assert persistable_duckdb_session_id("calc:wb-1:init") == "calc:wb-1"
    assert persistable_duckdb_session_id("rps:doc") == "rps:doc"
    assert persistable_duckdb_session_id("notebook:doc") == "notebook:doc"
    assert persistable_duckdb_session_id("writeragent:sql") is None
    assert persistable_duckdb_session_id("") is None
    assert persistable_duckdb_session_id(None) is None


def test_session_duckdb_reuses_connection(_clean_duckdb_sessions):
    sid = "calc:duckdb-reuse"
    first = session_duckdb(session_id=sid)
    second = session_duckdb(session_id=sid)
    assert first is second


def test_isolated_session_duckdb_is_fresh_each_call(_clean_duckdb_sessions):
    first = session_duckdb()
    second = session_duckdb()
    assert first is not second
    first.close()
    second.close()


def test_trusted_action_prefix_is_not_a_session_cache(_clean_duckdb_sessions):
    """Chat / run_folder_sql uses writeragent:sql as a routing id, not a kernel."""
    first = session_duckdb(session_id="writeragent:sql")
    second = session_duckdb(session_id="writeragent:sql")
    assert first is not second
    first.close()
    second.close()


def test_query_folder_sql_session_reuses_registered_table(_clean_duckdb_sessions):
    sid = "calc:duckdb-tables"
    first = query_folder_sql(
        None,
        "SELECT x FROM sales",
        preloaded={"sales": _tiny_grid(7)},
        session_id=sid,
    )
    assert first["status"] == "ok", first
    assert first["rows"][0][0] == 7

    # Second request sends only SQL — table stays registered on the session catalog.
    second = query_folder_sql(None, "SELECT x FROM sales", session_id=sid)
    assert second["status"] == "ok", second
    assert second["rows"][0][0] == 7


def test_query_folder_sql_isolated_does_not_reuse_tables(_clean_duckdb_sessions):
    first = query_folder_sql(None, "SELECT x FROM sales", preloaded={"sales": _tiny_grid(3)})
    assert first["status"] == "ok", first
    second = query_folder_sql(None, "SELECT x FROM sales")
    assert second["status"] == "error"
    assert "DUCKDB_ERROR" in second.get("code", "")


def test_query_folder_sql_reregister_refreshes_stale_snapshot(_clean_duckdb_sessions):
    sid = "calc:duckdb-refresh"
    query_folder_sql(None, "SELECT x FROM sales", preloaded={"sales": _tiny_grid(1)}, session_id=sid)
    refreshed = query_folder_sql(
        None,
        "SELECT x FROM sales",
        preloaded={"sales": _tiny_grid(42)},
        session_id=sid,
    )
    assert refreshed["status"] == "ok", refreshed
    assert refreshed["rows"][0][0] == 42


def test_reset_session_duckdb_clears_connection_and_tables(_clean_duckdb_sessions):
    sid = "calc:duckdb-reset"
    before = session_duckdb(session_id=sid)
    query_folder_sql(None, "SELECT x FROM sales", preloaded={"sales": _tiny_grid(5)}, session_id=sid)
    reset_session_duckdb(sid)
    after = session_duckdb(session_id=sid)
    assert after is not before
    missing = query_folder_sql(None, "SELECT x FROM sales", session_id=sid)
    assert missing["status"] == "error"


def test_reset_sandbox_session_closes_duckdb(_clean_duckdb_sessions):
    from plugin.scripting.venv.venv_sandbox import reset_sandbox_session

    sid = "calc:duckdb-sandbox-reset"
    query_folder_sql(None, "SELECT x FROM sales", preloaded={"sales": _tiny_grid(9)}, session_id=sid)
    assert reset_sandbox_session(sid)["status"] == "ok"
    missing = query_folder_sql(None, "SELECT x FROM sales", session_id=sid)
    assert missing["status"] == "error"


def test_invalidate_session_tables_drops_one_name(_clean_duckdb_sessions):
    sid = "calc:duckdb-invalidate"
    query_folder_sql(
        None,
        "SELECT x FROM sales",
        preloaded={"sales": _tiny_grid(1), "costs": _tiny_grid(2)},
        session_id=sid,
    )
    invalidate_session_tables(["sales"], session_id=sid)
    gone = query_folder_sql(None, "SELECT x FROM sales", session_id=sid)
    kept = query_folder_sql(None, "SELECT x FROM costs", session_id=sid)
    assert gone["status"] == "error"
    assert kept["status"] == "ok", kept
    assert kept["rows"][0][0] == 2


def test_cross_session_duckdb_isolation(_clean_duckdb_sessions):
    query_folder_sql(None, "SELECT x FROM sales", preloaded={"sales": _tiny_grid(11)}, session_id="calc:a")
    other = query_folder_sql(None, "SELECT x FROM sales", session_id="calc:b")
    assert other["status"] == "error"


def test_shared_kernel_cell_reuses_injected_session_duckdb(_clean_duckdb_sessions):
    from plugin.scripting.venv.worker_harness import _execute_request

    sid = "calc:duckdb-injected"
    first = _execute_request(
        "import pandas as pd\n"
        "con = session_duckdb()\n"
        "con.register('sales', pd.DataFrame({'x': [8]}))\n"
        "result = session_duckdb() is con",
        None,
        session_id=sid,
    )
    assert first["status"] == "ok", first
    assert first["result"] is True
    second = _execute_request(
        "con = session_duckdb()\n"
        "result = int(con.execute('SELECT x FROM sales').fetchone()[0])",
        None,
        session_id=sid,
    )
    assert second["status"] == "ok", second
    assert second["result"] == 8


def test_isolated_cell_session_duckdb_does_not_persist(_clean_duckdb_sessions):
    from plugin.scripting.venv.worker_harness import _execute_request

    first = _execute_request(
        "import pandas as pd\n"
        "con = session_duckdb()\n"
        "con.register('sales', pd.DataFrame({'x': [1]}))\n"
        "result = 1",
        None,
    )
    assert first["status"] == "ok", first
    second = _execute_request(
        "result = session_duckdb().execute('SELECT x FROM sales').fetchone()[0]",
        None,
    )
    assert second["status"] == "error"


def test_injected_run_sql_joins_preloaded_to_sibling_csv(_clean_duckdb_sessions, tmp_path: Path):
    """=PY() injects run_sql + scoped_dir; the join payload must not NameError."""
    from plugin.scripting.venv.worker_harness import _execute_request

    income = tmp_path / "zip_income.csv"
    _write_csv(income, "zip,median_household_income\n02116,99000\n")
    res = _execute_request(
        "df = run_sql("
        "'SELECT z.median_household_income AS inc FROM sales s JOIN zip_income z ON s.zip = z.zip',"
        "{'sales': [['zip'], ['02116']]},"
        "{'zip_income': 'zip_income.csv'},"
        "scoped_dir)\n"
        "result = int(df.iloc[0, 0])",
        None,
        bindings={"scoped_dir": str(tmp_path)},
    )
    assert res["status"] == "ok", res
    assert res["result"] == 99000


def test_query_folder_sql_uses_current_sandbox_session(_clean_duckdb_sessions):
    from plugin.scripting.venv.worker_harness import _execute_request

    sid = "calc:duckdb-sandbox-current"
    first = _execute_request(
        "from writeragent.scripting.duckdb_sql import query_folder_sql\n"
        "result = query_folder_sql(None, 'SELECT x FROM sales', preloaded={'sales': [['x'], [13]]})['status']",
        None,
        session_id=sid,
    )
    assert first["status"] == "ok", first
    assert first["result"] == "ok"
    second = _execute_request(
        "from writeragent.scripting.duckdb_sql import query_folder_sql\n"
        "result = query_folder_sql(None, 'SELECT x FROM sales')['rows'][0][0]",
        None,
        session_id=sid,
    )
    assert second["status"] == "ok", second
    assert second["result"] == 13


# --- Honesty + firewall polish ---


def test_query_folder_sql_truncation_is_honest():
    """Over-cap results must not look complete: truncated + total_rows + warning/flags."""
    n = MAX_TABLE_ROWS + 17
    grid = [["id", "val"]] + [[i, i * 2] for i in range(n)]
    res = query_folder_sql(
        None,
        "SELECT * FROM nums ORDER BY id",
        preloaded={"nums": {"grid": grid, "headers": True}},
    )
    assert res["status"] == "ok", res
    assert res["truncated"] is True
    assert res["total_rows"] == n
    assert res["row_cap"] == MAX_TABLE_ROWS
    assert len(res["rows"]) == MAX_TABLE_ROWS
    assert res["metrics"]["truncated"] is True
    assert res["metrics"]["returned_rows"] == MAX_TABLE_ROWS
    assert res["metrics"]["total_rows"] == n
    warning = res.get("warning") or res.get("message") or ""
    assert str(MAX_TABLE_ROWS) in warning
    assert str(n) in warning
    assert "not the full result" in warning.lower()
    assert warning in res.get("flags", [])
    table = res["tables"][0]
    assert table["truncated"] is True
    assert table["total_rows"] == n
    assert len(table["rows"]) == MAX_TABLE_ROWS


def test_query_folder_sql_under_cap_has_no_truncation_warning():
    grid = [["id"], [1], [2]]
    res = query_folder_sql(None, "SELECT * FROM t", preloaded={"t": {"grid": grid, "headers": True}})
    assert res["status"] == "ok", res
    assert res["truncated"] is False
    assert res["total_rows"] == 2
    assert not res.get("warning")
    assert res.get("flags") == []
    assert "message" not in res


def test_query_folder_sql_allows_cte():
    grid = [["x"], [1], [2]]
    cte = query_folder_sql(
        None,
        "WITH v AS (SELECT x FROM t) SELECT * FROM v",
        preloaded={"t": {"grid": grid, "headers": True}},
    )
    assert cte["status"] == "ok", cte
    assert cte["total_rows"] == 2


def test_query_folder_sql_readonly_blocks_export_attach_install(tmp_path):
    f = tmp_path / "t.csv"
    _write_csv(f, "id,val\n1,10")
    for sql in (
        "EXPORT DATABASE 'dump'",
        "ATTACH 'other.db'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "COPY (SELECT * FROM 't.csv') TO '/tmp/out.csv'",
    ):
        res = query_folder_sql(str(tmp_path), sql, files=["t.csv"])
        assert res["status"] == "error", sql
        assert res["code"] == "READONLY_VIOLATION", (sql, res)


def test_looks_like_write_ignores_comment_and_string_tokens():
    assert _looks_like_write_or_escape("SELECT 1 -- COPY later") is False
    assert _looks_like_write_or_escape("SELECT 'COPY TO x' AS note") is False
    assert _looks_like_write_or_escape("SELECT * FROM loading_dock") is False
    assert _looks_like_write_or_escape("SELECT * FROM t WHERE note = 'a..b'") is False
    # Division must not look like an absolute path (marketing ROAS demo).
    assert _looks_like_write_or_escape(
        "SELECT ROUND(SUM(Revenue) / NULLIF(SUM(Ad_Spend), 0), 2) AS roas FROM marketing"
    ) is False


def test_looks_like_write_blocks_quoted_absolute_and_uri():
    assert _looks_like_write_or_escape("SELECT * FROM '/etc/passwd'") is True
    assert _looks_like_write_or_escape("SELECT * FROM 'https://example.com/x.csv'") is True
    assert _looks_like_write_or_escape("SELECT * FROM 'C:/secret/x.csv'") is True


def test_run_sql_rejects_copy_and_escape_like_query_folder(tmp_path):
    res = run_sql("COPY (SELECT 1) TO 'out.csv'")
    assert res["status"] == "error"
    assert res["code"] == "READONLY_VIOLATION"
    assert res["helper"] == "run_sql"

    res2 = run_sql("SELECT * FROM '../evil.csv'")
    assert res2["status"] == "error"
    assert res2["code"] == "READONLY_VIOLATION"


def test_run_sql_truncation_honesty():
    con = session_duckdb()
    con.execute(f"CREATE TABLE nums AS SELECT range AS id FROM range({MAX_TABLE_ROWS + 5})")
    res = run_sql("SELECT * FROM nums", con=con)
    assert res["status"] == "ok", res
    assert res["helper"] == "run_sql"
    assert res["truncated"] is True
    assert res["total_rows"] == MAX_TABLE_ROWS + 5
    assert len(res["rows"]) == MAX_TABLE_ROWS
    assert "not the full result" in str(res.get("warning") or "").lower()
    con.close()


def test_session_duckdb_reuses_catalog_across_statements():
    """Shared-kernel Phase D pattern: bind one connection, register, query later."""
    con = session_duckdb()
    assert isinstance(con, GuardedDuckDBConnection)
    con.execute("CREATE VIEW v AS SELECT 41 AS x")
    res = run_sql("SELECT x + 1 AS y FROM v", con=con)
    assert res["status"] == "ok", res
    assert res["truncated"] is False
    assert res["total_rows"] == 1
    assert res["rows"][0][0] == 42
    with pytest.raises(ReadonlyViolation):
        con.execute("COPY (SELECT 1) TO 'out.csv'")
    with pytest.raises(ReadonlyViolation):
        con.sql("SELECT * FROM '/tmp/x.csv'")
    con.close()


def test_format_sql_for_calc_shows_truncation_note():
    from plugin.scripting.duckdb_sql import format_sql_for_calc, is_sql_result

    result = {
        "status": "ok",
        "helper": "query_folder_sql",
        "metrics": {"returned_rows": 200, "total_rows": 250, "row_cap": 200, "truncated": True},
        "flags": ["Result truncated: showing 200 of 250 rows"],
        "tables": [
            {
                "name": "sql_result",
                "columns": ["id"],
                "rows": [[i] for i in range(3)],
                "truncated": True,
                "total_rows": 250,
            }
        ],
        "truncated": True,
        "total_rows": 250,
    }
    assert is_sql_result(result)
    grid = format_sql_for_calc(result)
    flat = [str(cell) for row in grid for cell in row if cell]
    assert any("250" in cell and "truncated" in cell.lower() or "showing first" in cell.lower() for cell in flat)
    assert any("Flags" in cell or "truncated" in cell.lower() for cell in flat)
    err = format_sql_for_calc({"status": "error", "code": "READONLY_VIOLATION", "message": "SQL contains write"})
    assert err[0][0].startswith("SQL error")
    assert "write" in err[1][0]
