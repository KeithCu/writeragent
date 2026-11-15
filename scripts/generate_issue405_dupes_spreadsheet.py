#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual QA sheet for GitHub issue 405 (drop duplicate rows via Calc chat).

Builds an ODS with exactly A1:H500 of order-like rows, 99 exact duplicate
data rows, and empty columns I+ so a live =PY() spill can land at J1.

Usage (from repo root):
    python scripts/generate_issue405_dupes_spreadsheet.py
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "issue405_drop_duplicates.ods"

HEADERS = (
    "OrderID",
    "Customer",
    "Region",
    "Product",
    "Qty",
    "UnitPrice",
    "Status",
    "ShipDate",
)
CUSTOMERS = ("Acme", "Globex", "Initech", "Umbrella", "Soylent", "Hooli", "Stark", "Wayne")
REGIONS = ("NA", "EU", "APAC", "LATAM")
PRODUCTS = ("Widget", "Gadget", "Gizmo", "Doohickey", "Thingamajig")
STATUSES = ("Shipped", "Pending", "Hold", "Returned")

N_DATA_ROWS = 499  # A2:H500
N_UNIQUE_DATA = 400
N_DUPES = N_DATA_ROWS - N_UNIQUE_DATA  # 99


def unique_row(i: int) -> tuple[Any, ...]:
    """Deterministic unique order row (1-based unique index)."""
    return (
        10000 + i,
        CUSTOMERS[i % len(CUSTOMERS)],
        REGIONS[i % len(REGIONS)],
        PRODUCTS[i % len(PRODUCTS)],
        1 + (i % 12),
        round(9.5 + (i % 40) * 0.25, 2),
        STATUSES[i % len(STATUSES)],
        f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
    )


def data_rows() -> list[tuple[Any, ...]]:
    """400 unique rows, then 99 exact copies of the first 99 uniques."""
    uniques = [unique_row(i) for i in range(1, N_UNIQUE_DATA + 1)]
    return uniques + uniques[:N_DUPES]


def _cell(val: Any) -> TableCell:
    if val is None or val == "":
        return TableCell()
    if isinstance(val, int):
        cell = TableCell(valuetype="float", value=float(val))
        cell.addElement(P(text=str(val)))
        return cell
    if isinstance(val, float):
        cell = TableCell(valuetype="float", value=val)
        cell.addElement(P(text=str(val)))
        return cell
    cell = TableCell(valuetype="string")
    cell.addElement(P(text=str(val)))
    return cell


def _row(values: tuple[Any, ...]) -> TableRow:
    row = TableRow()
    for val in values:
        row.addElement(_cell(val))
    return row


def build_ods(out_path: Path) -> None:
    rows = data_rows()
    assert len(rows) == N_DATA_ROWS
    assert len({rows[i] for i in range(N_UNIQUE_DATA)}) == N_UNIQUE_DATA
    assert len(set(rows)) == N_UNIQUE_DATA

    doc = OpenDocumentSpreadsheet()

    orders = Table(name="Orders")
    orders.addElement(_row(HEADERS))
    for rec in rows:
        orders.addElement(_row(rec))
    doc.spreadsheet.addElement(orders)

    # How-to sheet: chat prompt + expected dest (J1) + unique counts.
    how = Table(name="HowToTest")
    howto_lines = (
        ("Issue", "https://github.com/KeithCu/writeragent/issues/405"),
        ("Sheet", "Orders — data occupies A1:H500 (header + 499 rows). I and J are empty."),
        ("Unique data rows", str(N_UNIQUE_DATA)),
        ("Exact duplicate data rows", str(N_DUPES)),
        ("Rows after drop_duplicates on A1:H500", str(1 + N_UNIQUE_DATA)),  # header + uniques
        (
            "Chat prompt (copy into Calc chat)",
            "run a Python script on the active sheet range A1:H500, drop exact duplicate rows, "
            "and write the cleaned result directly back to the active sheet.",
        ),
        (
            "Pass",
            "write_range of =PY(\"result = …drop_duplicates…\"; A1:H500) into J1 "
            "(or another empty column / new sheet). Not A1 or H1. Not domain=python.",
        ),
        (
            "Fail",
            "Confirmation-only reply with no tool call; =PY in A1/H1; delegate domain=python.",
        ),
        (
            "Check after tools fire",
            "J1 (or dest) holds =PY; spill has 401 rows (header + 400 unique). "
            "In-place overwrite of A1:H500 is circular — the model should say that.",
        ),
    )
    how.addElement(_row(("Key", "Value")))
    for rec in howto_lines:
        how.addElement(_row(rec))
    doc.spreadsheet.addElement(how)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output .ods path")
    args = parser.parse_args()
    build_ods(args.out)
    print(f"Wrote {args.out}")
    print(f"  A1:H500  unique data={N_UNIQUE_DATA}  duplicate copies={N_DUPES}")
    print("  Expected dest for =PY spill: J1 (columns I+ empty)")


if __name__ == "__main__":
    main()
