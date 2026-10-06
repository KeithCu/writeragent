# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for =PY() named-range packing and scoped_dir inject."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import uno
from com.sun.star.table import CellAddress

from plugin.calc.calc_addin_data import calc_addin_data_to_python
from plugin.calc.python.function import _py_scoped_dir_bindings
from plugin.doc.doc_type import is_calc
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test, teardown
from plugin.writer.format import create_property_value

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"
_XLSX = _FIXTURE_DIR / "python_showcase_demo.xlsx"
_ODS = _FIXTURE_DIR / "python_showcase_demo.ods"
_ZIP_CSV = _FIXTURE_DIR / "zip_income.csv"


def _hidden_props(*, xlsx: bool = False) -> tuple:
    props = [create_property_value("Hidden", True)]
    if xlsx:
        props.append(create_property_value("FilterName", "Calc MS Excel 2007 XML"))
    return tuple(props)


def _open_showcase(ctx, path: Path, *, xlsx: bool = False):
    desktop = get_desktop(ctx)
    url = uno.systemPathToFileUrl(str(path.resolve()))
    doc = desktop.loadComponentFromURL(url, "_blank", 0, _hidden_props(xlsx=xlsx))
    if doc is None:
        raise AssertionError(f"failed to open {path}")
    return doc


def _sheet_index(doc, name: str) -> int:
    sheets = doc.getSheets()
    for idx in range(sheets.getCount()):
        if sheets.getByIndex(idx).getName() == name:
            return idx
    raise AssertionError(f"missing sheet {name}")


def _pack_named_range_from_row(doc, name: str, sheet_name: str, row_1based: int) -> list[list]:
    """Pack the named range the way =PY() would after Calc evaluates it from *row*.

    Relative names shift from ReferencePosition (the calling RESULTS cell).
    Absolute names keep the Name Manager bounds (header row included).
    """
    nr = doc.NamedRanges.getByName(name)
    nr.setReferencePosition(
        CellAddress(Sheet=_sheet_index(doc, sheet_name), Column=0, Row=row_1based - 1)
    )
    cells = nr.getReferredCells()
    raw = cells.getDataArray() if cells is not None else ()
    packed = calc_addin_data_to_python(raw)
    return packed if packed is not None else []


def _close_leftover_showcase_docs(ctx) -> int:
    """Close leftover python_showcase_demo workbooks so later UNO suites stay isolated."""
    desktop = get_desktop(ctx)
    comps = getattr(desktop, "getComponents", lambda: None)()
    if comps is None or not hasattr(comps, "createEnumeration"):
        return 0
    enum = comps.createEnumeration()
    leftovers = []
    while enum.hasMoreElements():
        elem = enum.nextElement()
        model = elem
        if not is_calc(model):
            try:
                ctrl = getattr(elem, "getController", lambda: None)()
                model = ctrl.getModel() if ctrl is not None else None
            except Exception:
                continue
        if not is_calc(model):
            continue
        try:
            url = str(getattr(model, "getURL", lambda: "")() or "")
        except Exception:
            url = ""
        if "python_showcase_demo" in url:
            leftovers.append(model)
    closed = 0
    for other in leftovers:
        try:
            other.close(True)
            closed += 1
        except Exception:
            pass
    return closed


def _close_doc_and_clear_sessions(ctx, doc) -> None:
    """Close the doc and any leftover showcase workbooks."""
    if doc is not None:
        try:
            doc.close(True)
        except Exception:
            pass
    _close_leftover_showcase_docs(ctx)


@teardown
def _teardown_showcase_sessions(ctx) -> None:
    """Suite-level isolation for the next native module in the same soffice."""
    _close_leftover_showcase_docs(ctx)


@native_test
def test_xlsx_named_range_pack_from_results_row_keeps_header(ctx):
    """User-visible skew: Name Manager shows A4:J39, =PY from A23/A51 must still pack headers.

    Relative ``Sheet!A4:J39`` used from SQL_DuckDB!A23 packed ORD-1022
    (Electronics/Consumer); from A51 packed empty (Channel / candidate
    ``column``). Absolute ``$A$4:$J$39`` keeps Order_ID / Channel.
    """
    if not _XLSX.is_file():
        raise AssertionError(f"missing fixture {_XLSX}")
    doc = _open_showcase(ctx, _XLSX, xlsx=True)
    try:
        # Assert on the packed grid. Do not call CalcRange.to_pandas() here —
        # soffice's UNO process has no pandas; the live =PY worker uses the
        # user venv. Header cells are enough to prove the named range did
        # not shift off Order_ID / Region / Channel.
        sales_def = _pack_named_range_from_row(doc, "SalesData", "Sales_Analytics", 4)
        assert sales_def[0][0] == "Order_ID"
        assert sales_def[0][2] == "Region", sales_def[0][:5]

        sales = _pack_named_range_from_row(doc, "SalesData", "SQL_DuckDB", 23)
        assert sales[0][0] == "Order_ID", sales[0][:5]
        assert sales[0][2] == "Region", sales[0][:5]

        marketing = _pack_named_range_from_row(doc, "MarketingData", "SQL_DuckDB", 51)
        assert marketing, "MarketingData pack from A51 was empty"
        assert marketing[0][1] == "Channel", marketing[0][:4]

        join = _pack_named_range_from_row(doc, "SalesData", "SQL_DuckDB", 87)
        assert join[0][0] == "Order_ID"
    finally:
        _close_doc_and_clear_sessions(ctx, doc)


@native_test
def test_ods_named_range_pack_from_results_row_keeps_header(ctx):
    """ODS already used absolute names — packing must stay header-first."""
    if not _ODS.is_file():
        raise AssertionError(f"missing fixture {_ODS}")
    doc = _open_showcase(ctx, _ODS)
    try:
        sales = _pack_named_range_from_row(doc, "SalesData", "SQL_DuckDB", 23)
        assert sales[0][0] == "Order_ID"
        marketing = _pack_named_range_from_row(doc, "MarketingData", "SQL_DuckDB", 51)
        assert marketing[0][1] == "Channel"
    finally:
        _close_doc_and_clear_sessions(ctx, doc)


@native_test
def test_py_scoped_dir_bindings_saved_showcase_workbook(ctx):
    """=PY() must inject the document folder when zip_income.csv sits beside the file."""
    if not _XLSX.is_file() or not _ZIP_CSV.is_file():
        raise AssertionError("missing showcase fixtures")
    temp_dir = tempfile.mkdtemp(prefix="wa_scoped_dir_")
    try:
        dest_xlsx = os.path.join(temp_dir, "python_showcase_demo.xlsx")
        dest_csv = os.path.join(temp_dir, "zip_income.csv")
        shutil.copy2(_XLSX, dest_xlsx)
        shutil.copy2(_ZIP_CSV, dest_csv)
        doc = _open_showcase(ctx, Path(dest_xlsx), xlsx=True)
        try:
            bindings = _py_scoped_dir_bindings(doc)
            folder = bindings.get("scoped_dir")
            assert folder, bindings
            assert os.path.samefile(folder, temp_dir), bindings
            assert os.path.isfile(os.path.join(folder, "zip_income.csv"))
        finally:
            _close_doc_and_clear_sessions(ctx, doc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@native_test
def test_py_multi_document_spill_and_plot(ctx):
    """When a blank doc is open, =PY in a second doc spills and inserts plots in the second doc."""
    import glob
    import json
    from plugin.framework.config import _resolve_config_path_from_ctx
    from plugin.scripting.venv_worker import PythonWorkerManager

    repo_venv = str(Path(__file__).resolve().parents[3] / ".venv")
    profile_cfgs = set(glob.glob("/tmp/writeragent-lo-test-profile-*/user/config/writeragent.json"))
    try:
        profile_cfgs.add(_resolve_config_path_from_ctx(ctx))
    except Exception:
        pass

    old_cfgs: dict[str, dict] = {}
    for pcfg in profile_cfgs:
        if os.path.exists(pcfg):
            try:
                with open(pcfg, "r", encoding="utf-8") as f:
                    old_cfgs[pcfg] = json.load(f)
                d = dict(old_cfgs[pcfg])
                d["scripting.python_venv_path"] = repo_venv
                with open(pcfg, "w", encoding="utf-8") as f:
                    json.dump(d, f, indent=2)
            except Exception:
                pass
    PythonWorkerManager.shutdown_all()

    desktop = get_desktop(ctx)
    doc_blank = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, _hidden_props())
    doc_second = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, _hidden_props())
    try:
        sheet_second = doc_second.getSheets().getByIndex(0)

        a1 = sheet_second.getCellByPosition(0, 0)
        d1 = sheet_second.getCellByPosition(3, 0)
        a1.setFormula('=PY("[[1,2],[3,4]]")')
        d1.setFormula('=PY("plt.figure(); plt.plot([1,2,3])")')

        time.sleep(2.1)
        doc_second.calculateAll()

        # Wait for spill and image insertion
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            b1_val = sheet_second.getCellByPosition(1, 0).getValue()
            dp = sheet_second.getDrawPage()
            has_shape = False
            for idx in range(dp.getCount()):
                sh = dp.getByIndex(idx)
                name = getattr(sh, "Name", "")
                if not name and hasattr(sh, "getPropertyValue"):
                    try:
                        name = sh.getPropertyValue("Name")
                    except Exception:
                        name = ""
                if name and "WriterAgentPlot" in str(name):
                    has_shape = True
                    break
            if b1_val == 2.0 and has_shape:
                break
            time.sleep(0.2)

        # Assert B1/A2/B2 are spilled in second doc
        assert sheet_second.getCellByPosition(1, 0).getValue() == 2.0
        assert sheet_second.getCellByPosition(0, 1).getValue() == 3.0
        assert sheet_second.getCellByPosition(1, 1).getValue() == 4.0

        # Assert WriterAgentPlot shape is on its sheet
        dp_second = sheet_second.getDrawPage()
        shape_names = []
        for idx in range(dp_second.getCount()):
            sh = dp_second.getByIndex(idx)
            name = getattr(sh, "Name", "")
            if not name and hasattr(sh, "getPropertyValue"):
                try:
                    name = sh.getPropertyValue("Name")
                except Exception:
                    name = ""
            shape_names.append(str(name))
        assert any("WriterAgentPlot" in n for n in shape_names), f"Plot shape not found in {shape_names}"

        # Assert blank doc is untouched
        sheet_blank0 = doc_blank.getSheets().getByIndex(0)
        assert sheet_blank0.getCellByPosition(0, 0).getString() == ""
        assert sheet_blank0.getCellByPosition(1, 0).getValue() == 0.0
        assert sheet_blank0.getDrawPage().getCount() == 0
    finally:
        _close_doc_and_clear_sessions(ctx, doc_second)
        _close_doc_and_clear_sessions(ctx, doc_blank)
        for pcfg, old_d in old_cfgs.items():
            try:
                with open(pcfg, "w", encoding="utf-8") as f:
                    json.dump(old_d, f, indent=2)
            except Exception:
                pass
        PythonWorkerManager.shutdown_all()

