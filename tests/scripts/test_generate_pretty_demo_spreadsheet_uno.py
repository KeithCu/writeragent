# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO load of the pretty-demo ODS: Overview merges must survive headed Calc."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import uno

from plugin.doc.doc_type import is_calc
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test
from plugin.writer.format import create_property_value

_ODS = Path(__file__).resolve().parents[1] / "fixtures" / "python_showcase_demo.ods"


def _open_ods_copy(ctx, path: Path):
    """Load a temp copy so a headed lock on the fixture cannot fail the suite."""
    temp_dir = tempfile.mkdtemp(prefix="wa_showcase_ods_")
    dest = Path(temp_dir) / path.name
    shutil.copy2(path, dest)
    desktop = get_desktop(ctx)
    url = uno.systemPathToFileUrl(str(dest.resolve()))
    hidden = create_property_value("Hidden", True)
    doc = desktop.loadComponentFromURL(url, "_blank", 0, (hidden,))
    if doc is None or not is_calc(doc):
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise AssertionError(f"failed to open {dest} (from {path})")
    return doc, temp_dir


@native_test
def test_ods_overview_kpi_and_matrix_merges_survive_load(ctx):
    """Without covered-table-cell, LO stacks KPI 2–4 and matrix cols 2–4 into A.

    After the fix, labels sit at A5/C5/E5/G5 (each a 2-col merge) and matrix
    headers at A9/C9/E9/G9 — same skeleton as the XLSX executive dashboard.
    """
    if not _ODS.is_file():
        raise AssertionError(f"missing fixture {_ODS}")
    doc, temp_dir = _open_ods_copy(ctx, _ODS)
    try:
        sheet = doc.getSheets().getByName("Overview")
        assert "TOTAL REVENUE" in sheet.getCellByPosition(0, 4).getString()
        assert "AVG PROFIT" in sheet.getCellByPosition(2, 4).getString()
        assert "ANOMALIES" in sheet.getCellByPosition(4, 4).getString()
        assert "FORECAST" in sheet.getCellByPosition(6, 4).getString()
        assert sheet.getCellRangeByName("A5:B5").getIsMerged()
        assert sheet.getCellRangeByName("C5:D5").getIsMerged()
        assert sheet.getCellRangeByName("E5:F5").getIsMerged()
        assert sheet.getCellRangeByName("G5:H5").getIsMerged()

        assert "Capability Domain" in sheet.getCellByPosition(0, 8).getString()
        assert "Traditional" in sheet.getCellByPosition(2, 8).getString()
        assert "LibrePy" in sheet.getCellByPosition(4, 8).getString()
        assert "Scientific Engine" in sheet.getCellByPosition(6, 8).getString()
        assert sheet.getCellRangeByName("A9:B9").getIsMerged()
        assert sheet.getCellRangeByName("C9:D9").getIsMerged()
    finally:
        try:
            doc.close(True)
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)


@native_test
def test_ods_sales_header_stays_on_a4_after_load(ctx):
    """Self-closing spacer rows were dropped: section→R2, Order_ID→R3, A4=ORD-1001."""
    if not _ODS.is_file():
        raise AssertionError(f"missing fixture {_ODS}")
    doc, temp_dir = _open_ods_copy(ctx, _ODS)
    try:
        sheet = doc.getSheets().getByName("Sales_Analytics")
        assert sheet.getCellByPosition(0, 1).getString() == ""
        assert "TRANSACTIONAL SALES DATASET" in sheet.getCellByPosition(0, 2).getString()
        assert sheet.getCellByPosition(0, 3).getString() == "Order_ID"
        assert sheet.getCellByPosition(0, 4).getString() == "ORD-1001"
        marketing = doc.getSheets().getByName("Statistics_ML")
        assert marketing.getCellByPosition(0, 1).getString() == ""
        assert marketing.getCellByPosition(0, 3).getString() == "Campaign"
    finally:
        try:
            doc.close(True)
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)
