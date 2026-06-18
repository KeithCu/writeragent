# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.duckdb_sql (Phase A folder SQL path guard + execution)."""

from __future__ import annotations

import tempfile
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
