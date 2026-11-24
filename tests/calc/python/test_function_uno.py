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
from plugin.calc.python.workbook_state import WorkbookState
from plugin.calc.python.spill import (
    SpillCell,
    SpillRecord,
    perform_deferred_spill,
)
from tests.calc.python.calc_workbook_test_access import SPILL_REGISTRY, reset_calc_workbook_states
from plugin.framework.doc_type import is_calc
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test, teardown
from plugin.tests.testing_utils import with_native_doc
from plugin.framework.uno_context import create_property_value

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


def _wait_spill_settled(doc, timeout: float = 10.0) -> None:
    """Wait until the doc's deferred spill / sheet-repair timers have run.

    Tip: closing while a deferred spill write is still pending hangs close() on Windows.
    """
    from plugin.framework.queue_executor import pump_main_thread_work_queue

    st = WorkbookState.for_doc(doc)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_main_thread_work_queue(max_items=20)
        if st is None or len(st.debounce.pending_for_tests()) == 0:
            break
        time.sleep(0.05)
    # A timer that just fired may still have its write queued for the UI thread.
    time.sleep(0.3)
    pump_main_thread_work_queue(max_items=20)


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
            from plugin.calc.python.function import build_cell_context

            bindings = {"scoped_dir": build_cell_context(ctx, doc, "", on_main=True).scoped_dir}
            folder = bindings.get("scoped_dir")
            assert folder, bindings
            assert os.path.samefile(folder, temp_dir), bindings
            assert os.path.isfile(os.path.join(folder, "zip_income.csv"))
        finally:
            _close_doc_and_clear_sessions(ctx, doc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _project_venv() -> str:
    """Checkout ``.venv`` that has an interpreter.

    ``parents[3]`` of this file is the repo during ``make test``, and the
    stripped temp tree during ``make release``. A configured venv with no
    interpreter is ``VENV_NOT_FOUND``, so ``=PY()`` never spills. Release
    exports ``WRITERAGENT_PROJECT_ROOT`` from the checkout Makefile.
    """

    def _has_python(venv: Path) -> bool:
        return (venv / "bin" / "python").is_file() or (venv / "Scripts" / "python.exe").is_file()

    for parent in Path(__file__).resolve().parents:
        venv = parent / ".venv"
        if _has_python(venv):
            return str(venv)
    root = os.environ.get("WRITERAGENT_PROJECT_ROOT", "").strip()
    if root:
        venv = Path(root) / ".venv"
        if _has_python(venv):
            return str(venv)
    raise AssertionError("project .venv not found (release tree needs WRITERAGENT_PROJECT_ROOT)")


# Cold macOS runners can exceed 30s spawning the venv worker, building the
# matplotlib font cache, and rendering the plot.
@native_test(timeout=60)
def test_py_multi_document_spill_and_plot(ctx):
    """When a blank doc is open, =PY in a second doc spills and inserts plots in the second doc."""
    import glob
    import json
    from plugin.framework.config import _resolve_config_path_from_ctx
    from plugin.scripting.venv_worker import shutdown_venv_workers_for_tests

    repo_venv = _project_venv()
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
    shutdown_venv_workers_for_tests()

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
        from plugin.framework.queue_executor import pump_main_thread_work_queue

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            pump_main_thread_work_queue(max_items=20)
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
        shutdown_venv_workers_for_tests()



@native_test
def test_row_insert_does_not_clear_moved_spill(ctx):
    """Row insertion shifts =PY() formula and its spill; modify listener remaps without clearing."""
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests
    from plugin.calc.python.sheet_modify import flush_sheet_modify_pass_for_tests, reset_sheet_modify_runtime_for_tests
    from plugin.framework.queue_executor import pump_main_thread_work_queue

    # Prior =PY() UNO tests schedule 0.1s sheet/spill debounces; drain before this case.
    pump_main_thread_work_queue(max_items=50)
    time.sleep(0.35)
    pump_main_thread_work_queue(max_items=50)
    reset_sheet_modify_runtime_for_tests()
    reset_geometric_runtime_for_tests()
    reset_calc_workbook_states()

    desktop = get_desktop(ctx)
    doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, _hidden_props())
    st = WorkbookState.for_doc(doc)
    assert st is not None
    doc_key = st.key
    try:
        sheet = doc.getSheets().getByIndex(0)
        if sheet.getName() != "Sheet1":
            sheet.setName("Sheet1")
        code = "result = [1, 2, 3, 4, 5]"
        sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
        for r in range(5, 10):
            sheet.getCellByPosition(0, r).setValue(float(r - 4))

        cells = [SpillCell(row=r, col=0, kind="n", value=float(r - 4)) for r in range(5, 10)]
        SPILL_REGISTRY[(doc_key, "Sheet1", 4, 0)] = SpillRecord(code=code, cells=cells)

        from plugin.calc.python.sheet_modify import flush_sheet_modify_pass_for_tests

        sheet.getRows().insertByIndex(0, 1)

        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name="Sheet1")

        a6_formula = sheet.getCellByPosition(0, 5).getFormula()
        assert "PY" in a6_formula, f"Expected PY in A6 formula, got {a6_formula!r}"
        for r in range(6, 11):
            val = sheet.getCellByPosition(0, r).getValue()
            assert val == float(r - 5), f"Expected {float(r - 5)} at row {r}, got {val}"

        assert (doc_key, "Sheet1", 5, 0) in SPILL_REGISTRY, "Expected (doc_key, Sheet1, 5, 0) in SPILL_REGISTRY"
        assert (doc_key, "Sheet1", 4, 0) not in SPILL_REGISTRY, "Old key should have been remapped"
        rec = SPILL_REGISTRY[(doc_key, "Sheet1", 5, 0)]
        assert [c.row for c in rec.cells] == list(range(6, 11))
        assert [c.col for c in rec.cells] == [0] * 5
        assert [c.value for c in rec.cells] == [float(i) for i in range(1, 6)]
    finally:
        _wait_spill_settled(doc)
        try:
            doc.close(True)
        except Exception:
            pass
        for k in list(SPILL_REGISTRY.keys()):
            if k[0] == doc_key:
                SPILL_REGISTRY.pop(k, None)


@native_test
@with_native_doc("calc")
def test_py_addin_worker_exception_formula_error(ctx, doc):
    """A failing =PY() run is a Calc formula error chosen from the exception name.

    The code really runs in soffice's Python worker; a patch in this test
    process cannot reach the add-in Calc calls. An exception with no
    Calc-specific mapping is #VALUE! (519); ZeroDivisionError is #DIV/0! (532).
    """
    sheet = doc.getSheets().getByIndex(0)
    try:
        value_cell = sheet.getCellByPosition(0, 0)
        div_cell = sheet.getCellByPosition(0, 1)
        value_cell.setFormula('=PY("raise ValueError(\'bad\')")')
        div_cell.setFormula('=PY("result = 1/0")')
        doc.calculateAll()
        assert value_cell.getError() == 519, value_cell.getString()
        assert "Error: Code execution failed" not in value_cell.getString()
        assert div_cell.getError() == 532, div_cell.getString()
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_py_addin_child_timeout_shows_guidance_text(ctx, doc):
    """Child in-process timeout (code EXECUTION_TIMEOUT) displays timeout guidance text in the cell."""
    sheet = doc.getSheets().getByIndex(0)
    try:
        cell = sheet.getCellByPosition(0, 0)
        cell.setFormula('=PY("import time; time.sleep(12); result = 1")')
        doc.calculateAll()
        assert cell.getError() == 0, cell.getError()
        assert cell.getString().startswith("Error: Python timed out"), repr(cell.getString())
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


def _with_repo_venv_for_tests(ctx):
    """Temporarily point the test profile's ``scripting.python_venv_path`` at the repo venv."""
    from contextlib import contextmanager
    import glob
    import json
    from plugin.framework.config import _resolve_config_path_from_ctx
    from plugin.scripting.venv_worker import shutdown_venv_workers_for_tests

    @contextmanager
    def _mgr():
        repo_venv = _project_venv()
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
        shutdown_venv_workers_for_tests()
        try:
            yield
        finally:
            for pcfg, old_d in old_cfgs.items():
                try:
                    with open(pcfg, "w", encoding="utf-8") as f:
                        json.dump(old_d, f, indent=2)
                except Exception:
                    pass
            shutdown_venv_workers_for_tests()

    return _mgr()


@native_test
@with_native_doc("calc")
def test_py_addin_token_formula_errors(ctx, doc):
    """Real =PY() results of #DIV/0! and #N/A tokens map to Calc errors 532 and 32767 (computed in soffice's worker)."""
    sheet = doc.getSheets().getByIndex(0)
    cases = [("#DIV/0!", 532), ("#N/A", 32767)]
    with _with_repo_venv_for_tests(ctx):
        try:
            for row_idx, (token, _expected_err) in enumerate(cases):
                sheet.getCellByPosition(0, row_idx).setFormula(f'=PY("result = \'{token}\'")')
            sheet.getCellByPosition(0, 2).setFormula('=PY("result=calc.na()")')
            sheet.getCellByPosition(0, 3).setFormula('=PY("result=calc.xmatch(9,[1,2])")')
            sheet.getCellByPosition(0, 4).setFormula('=PY("result = \'Err:502\'")')
            sheet.getCellByPosition(1, 2).setFormula("=ISNA(A3)")
            sheet.getCellByPosition(1, 3).setFormula("=ISNA(A4)")
            sheet.getCellByPosition(2, 2).setFormula('=IFNA(A3;"na")')
            sheet.getCellByPosition(2, 3).setFormula('=IFNA(A4;"na")')
            helper_cases = [
                ("result=calc.filter([1,2],['#DIV/0!',1])", 532),
                ("result=calc.ifna(lambda: float('nan'), 0)", 503),
                ("result=calc.xmatch(1,[1],9)", 504),
                ("result=calc.xlookup(1,[1],[2],'m',9)", 504),
                ("result=calc.growth([1,2],[1,2,3])", 502),
                ("result=calc.textafter('abc','z')", 32767),
            ]
            for offset, (helper_code, _expected) in enumerate(helper_cases):
                sheet.getCellByPosition(0, 5 + offset).setFormula(f'=PY("{helper_code}")')
            doc.calculateAll()
            for row_idx, (_token, expected_err) in enumerate(cases):
                assert sheet.getCellByPosition(0, row_idx).getError() == expected_err
            assert sheet.getCellByPosition(0, 4).getError() == 502
            for offset, (helper_code, expected_err) in enumerate(helper_cases):
                cell = sheet.getCellByPosition(0, 5 + offset)
                assert cell.getError() == expected_err, f"{helper_code}: got {cell.getError()} ({cell.getString()!r})"
            a3 = sheet.getCellByPosition(0, 2)
            a4 = sheet.getCellByPosition(0, 3)
            assert a3.getError() == 32767, (a3.getError(), a3.getString(), a3.getValue())
            assert a4.getError() == 32767, (a4.getError(), a4.getString(), a4.getValue())
            assert sheet.getCellByPosition(1, 2).getValue() == 1.0
            assert sheet.getCellByPosition(1, 3).getValue() == 1.0
            assert sheet.getCellByPosition(2, 2).getString() == "na"
            assert sheet.getCellByPosition(2, 3).getString() == "na"
        finally:
            st = WorkbookState.known(doc)
            if st is not None:
                WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_py_spill_neighbors_formula_error(ctx, doc):
    """Spill neighbors: #N/A is written as void (error 32767), others as =#ERRxxx!."""
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [[1, '#VALUE!'], ['#N/A', '#DIV/0!']]"
    try:
        cell_a1 = sheet.getCellByPosition(0, 0)
        cell_a1.setFormula(f'=PY("{code}")')
        grid = [[1, "#VALUE!"], ["#N/A", "#DIV/0!"]]
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            grid,
            doc=doc,
            sheet=sheet,
            code=code,
            key=WorkbookState.for_doc(doc).key,
        )
        doc.calculateAll()

        # B1 is (col 1, row 0): #VALUE! -> getError() 519, getFormula() =#ERR519! or =#VALUE!
        cell_b1 = sheet.getCellByPosition(1, 0)
        assert cell_b1.getError() == 519
        assert cell_b1.getFormula() in ("=#ERR519!", "=#VALUE!")

        # A2 is (col 0, row 1): #N/A -> getError() 32767
        cell_a2 = sheet.getCellByPosition(0, 1)
        assert cell_a2.getError() == 32767

        # B2 is (col 1, row 1): #DIV/0! -> getError() 532, getFormula() =#ERR532! or =#DIV/0!
        cell_b2 = sheet.getCellByPosition(1, 1)
        assert cell_b2.getError() == 532
        assert cell_b2.getFormula() in ("=#ERR532!", "=#DIV/0!")

        # A1 is still the =PY() formula
        assert "PY" in cell_a1.getFormula()

        # Spill neighbour with NA_NAN (e.g. calc.na()) -> error 32767 and ISNA 1.
        from plugin.calc.python.addin_values import ERR_NA, calc_formula_error_double

        na_nan = calc_formula_error_double(ERR_NA)
        cell_d1 = sheet.getCellByPosition(3, 0)
        cell_d1.setFormula('=PY("result = [[1, calc.na(), \'Err:502\']]")')
        perform_deferred_spill(
            sheet_name,
            0,
            3,
            [[1, na_nan, "Err:502"]],
            doc=doc,
            sheet=sheet,
            code="result = [[1, calc.na(), 'Err:502']]",
            key=WorkbookState.for_doc(doc).key,
        )
        sheet.getCellByPosition(4, 1).setFormula("=ISNA(E1)")
        sheet.getCellByPosition(5, 1).setFormula("=ISERROR(F1)")
        doc.calculateAll()
        cell_e1 = sheet.getCellByPosition(4, 0)
        assert cell_e1.getError() == 32767
        assert sheet.getCellByPosition(4, 1).getValue() == 1.0
        cell_f1 = sheet.getCellByPosition(5, 0)
        assert cell_f1.getError() == 502
        assert sheet.getCellByPosition(5, 1).getValue() == 1.0

        # Unpublished error payloads in spill neighbors are written as =#ERR503!.
        unpub_code = "result = [[1, float('nan')]]"
        sheet.getCellByPosition(7, 0).setFormula(f'=PY("{unpub_code}")')
        perform_deferred_spill(
            sheet_name,
            0,
            7,
            [[1, calc_formula_error_double(999)]],
            doc=doc,
            sheet=sheet,
            code=unpub_code,
            key=WorkbookState.for_doc(doc).key,
        )
        doc.calculateAll()
        cell_i1 = sheet.getCellByPosition(8, 0)
        assert cell_i1.getError() == 503
        assert cell_i1.getFormula() in ("=#ERR503!", "=#NUM!"), cell_i1.getFormula()
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_py_deferred_spill_late_collision_uno(ctx, doc):
    """Late collision between prepare_auto_spill and perform_deferred_spill preserves the user cell."""
    from com.sun.star.table.CellContentType import EMPTY
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.calc.python.spill import prepare_auto_spill

    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [[1, 2, 3]]"
    try:
        cell_m1 = sheet.getCellByPosition(12, 0)
        cell_m1.setFormula(f'=PY("{code}")')
        state = WorkbookState.for_doc(doc)
        assert state is not None
        prep = prepare_auto_spill(
            ctx,
            doc,
            state,
            FormulaOrigin(sheet, cell_m1, sheet_name, 0, 12),
            code,
            [[1, 2, 3]],
        )
        assert not isinstance(prep, str) and prep is not None

        cell_o1 = sheet.getCellByPosition(14, 0)
        cell_o1.setFormula("=1+1")
        perform_deferred_spill(
            prep.sheet_name,
            prep.formula_row,
            prep.formula_col,
            [[1, 2, 3]],
            doc=doc,
            sheet=sheet,
            code=prep.code,
            key=prep.key,
        )
        doc.calculateAll()

        assert cell_o1.getFormula() == "=1+1"
        assert cell_o1.getValue() == 2.0
        assert sheet.getCellByPosition(13, 0).getType() == EMPTY
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_py_spill_neighbors_temporal_formats(ctx, doc):
    """Spill neighbors: temporal values get numeric serials and standard date/time/duration formats."""
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [['2026-08-08', 'PT30H'], ['2026-08-08T12:00:00', '12:30:00']]"
    try:
        cell_a1 = sheet.getCellByPosition(0, 0)
        cell_a1.setFormula(f'=PY("{code}")')
        grid = [["2026-08-08", "PT30H"], ["2026-08-08T12:00:00", "12:30:00"]]
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            grid,
            doc=doc,
            sheet=sheet,
            code=code,
            key=WorkbookState.for_doc(doc).key,
        )
        doc.calculateAll()

        formats = doc.getNumberFormats()
        formatter = ctx.getServiceManager().createInstanceWithContext("com.sun.star.util.NumberFormatter", ctx)
        formatter.attachNumberFormatsSupplier(doc)

        # B1: duration PT30H -> 1.25 with [HH]:MM:SS formatting
        cell_b1 = sheet.getCellByPosition(1, 0)
        assert abs(cell_b1.getValue() - 1.25) < 1e-9
        assert cell_b1.getString() == "30:00:00"

        # A2: datetime 2026-08-08T12:00:00 -> 46242.5, Type == 6 (DATETIME)
        cell_a2 = sheet.getCellByPosition(0, 1)
        assert abs(cell_a2.getValue() - 46242.5) < 1e-9
        nf_a2 = int(cell_a2.getPropertyValue("NumberFormat"))
        assert formats.getByKey(nf_a2).Type == 6
        assert cell_a2.getString() == formatter.convertNumberToString(nf_a2, 46242.5)

        # B2: time 12:30:00 -> 45000/86400, Type == 4 (TIME)
        cell_b2 = sheet.getCellByPosition(1, 1)
        expected_time = 45000.0 / 86400.0
        assert abs(cell_b2.getValue() - expected_time) < 1e-9
        nf_b2 = int(cell_b2.getPropertyValue("NumberFormat"))
        assert formats.getByKey(nf_b2).Type == 4
        assert cell_b2.getString() == formatter.convertNumberToString(nf_b2, expected_time)
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_calc_spill_undo_lock(ctx, doc):
    """Deferred spill writes must not create extra undo actions on top of the formula."""
    from plugin.calc.python.spill import perform_deferred_spill
    from plugin.calc.python.workbook_state import WorkbookState

    active_sheet = doc.getCurrentController().getActiveSheet()
    # Pass the spill registry key (RuntimeUID), like the real caller. A pooled doc can
    # be a saved file, and its URL would not match, so the spill would be skipped.
    st = WorkbookState.for_doc(doc)
    assert st is not None
    doc_url = st.key

    # Simulate user typing a formula into A1
    active_sheet.getCellByPosition(0, 0).setFormula('=PY("result = [1, 2, 3]")')
    um = doc.getUndoManager()
    if um is not None:
        um.enterUndoContext("Input")
        um.leaveUndoContext()

    titles_before = list(um.getAllUndoActionTitles()) if um else []

    perform_deferred_spill(active_sheet.Name, 0, 0, [[1, 2], [3, 4]], doc=doc, sheet=active_sheet, code="result = [1, 2, 3]", key=doc_url)

    assert active_sheet.getCellByPosition(1, 0).getValue() == 2.0
    assert active_sheet.getCellByPosition(0, 1).getValue() == 3.0

    titles_after = list(um.getAllUndoActionTitles()) if um else []
    assert titles_after == titles_before, f"Deferred spill added extra undo actions: {titles_after} vs {titles_before}"


@native_test
@with_native_doc("calc")
def test_nan_spill_neighbour_error_and_orphan_cleanup(ctx, doc):
    """NaN spill neighbours become #NUM! (503) and orphan cleanup clears error literals but keeps user =1/0."""
    from com.sun.star.table.CellContentType import EMPTY, FORMULA
    from plugin.calc.python.spill import cleanup_orphans, perform_deferred_spill
    from plugin.calc.python.workbook_state import WorkbookState

    sheet = doc.getCurrentController().getActiveSheet()
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        code = "result = [[1, float('nan'), '#VALUE!', '#N/A', '#DIV/0!']]"
        sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
        perform_deferred_spill(
            sheet.Name,
            0,
            0,
            [[1.0, float("nan"), "#VALUE!", "#N/A", "#DIV/0!"]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=st.key,
        )
        # Verify ISERROR on the NaN neighbour (B1)
        sheet.getCellByPosition(1, 1).setFormula("=ISERROR(B1)")
        doc.calculateAll()

        nan_cell = sheet.getCellByPosition(1, 0)
        assert nan_cell.getError() == 503
        assert nan_cell.getString() == "#NUM!"
        assert sheet.getCellByPosition(1, 1).getValue() == 1.0

        # Replace formula in A1, and replace E1 (#DIV/0!) with a user formula =1/0
        sheet.getCellByPosition(0, 0).setFormula("")
        sheet.getCellByPosition(4, 0).setFormula("=1/0")
        doc.calculateAll()

        cleanup_orphans(st, sheet, doc=doc, sheet_name=sheet.Name)

        assert sheet.getCellByPosition(1, 0).getType() == EMPTY
        assert sheet.getCellByPosition(2, 0).getType() == EMPTY
        assert sheet.getCellByPosition(3, 0).getType() == EMPTY
        assert sheet.getCellByPosition(4, 0).getType() == FORMULA
        assert sheet.getCellByPosition(4, 0).getFormula() == "=1/0"
    finally:
        WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_inf_spill_neighbour_survives_ods_reload_uno(ctx, doc):
    """±inf spill neighbours become #NUM! (503), ISERROR true, and survive ODS store and reload."""
    from com.sun.star.beans import PropertyValue
    import uno
    from plugin.calc.python.spill import perform_deferred_spill
    from plugin.calc.python.workbook_state import WorkbookState

    sheet = doc.getCurrentController().getActiveSheet()
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        code = "result = [[1, float('inf'), float('-inf')]]"
        sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
        perform_deferred_spill(
            sheet.Name,
            0,
            0,
            [[1.0, float("inf"), float("-inf")]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=st.key,
        )
        sheet.getCellByPosition(1, 1).setFormula("=ISERROR(B1)")
        sheet.getCellByPosition(2, 1).setFormula("=ISERROR(C1)")
        doc.calculateAll()

        for col in (1, 2):
            c = sheet.getCellByPosition(col, 0)
            assert c.getError() == 503
            assert c.getString() == "#NUM!"
            assert sheet.getCellByPosition(col, 1).getValue() == 1.0

        with tempfile.TemporaryDirectory(prefix="wa-inf-ods-") as tmpdir:
            ods_path = os.path.join(tmpdir, "inf_spill.ods")
            ods_url = uno.systemPathToFileUrl(ods_path)
            doc.storeToURL(ods_url, ())

            desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            hidden_prop = PropertyValue()
            hidden_prop.Name = "Hidden"
            hidden_prop.Value = True
            reloaded = desktop.loadComponentFromURL(ods_url, "_blank", 0, (hidden_prop,))
            try:
                rsheet = reloaded.getSheets().getByIndex(0)
                reloaded.calculateAll()
                for col in (1, 2):
                    rc = rsheet.getCellByPosition(col, 0)
                    assert rc.getError() == 503
                    assert rc.getString() == "#NUM!"
            finally:
                reloaded.close(True)
    finally:
        WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_py_spill_keeps_user_edits_uno(ctx, doc):
    """Spilled values remain owned across ;;; formatting, ODS and XLSX reload, while user edits block respill."""
    import datetime
    import os
    import tempfile
    import uno
    from com.sun.star.beans import PropertyValue
    from plugin.calc.python.formula_locator_cache import FormulaOrigin
    from plugin.calc.python.spill import _unchanged_cells, perform_deferred_spill, prepare_auto_spill
    from plugin.calc.python.workbook_state import WorkbookState

    sheet = doc.getCurrentController().getActiveSheet()
    st = WorkbookState.for_doc(doc)
    assert st is not None
    try:
        code = "result = [[1, '123', chr(39) + 'q', 'a\\r\\nb', date, float('nan'), '#N/A', True]]"
        origin_cell = sheet.getCellByPosition(0, 0)
        origin_cell.setFormula(f'=PY("{code}")')
        grid = [[1, "123", "'q", "a\r\nb", datetime.date(2026, 8, 8), float("nan"), "#N/A", True, "9" * 400]]
        perform_deferred_spill(sheet.Name, 0, 0, grid, doc=doc, sheet=sheet, code=code, key=st.key)
        rec = st.spills[(sheet.Name, 0, 0)]

        # Apply ;;; number format across spilled neighbors; second spill still succeeds.
        formats = doc.getNumberFormats()
        locale = doc.getPropertyValue("CharLocale")
        hidden_key = formats.queryKey(";;;", locale, True)
        if hidden_key == -1:
            hidden_key = formats.addNew(";;;", locale)
        sheet.getCellRangeByPosition(1, 0, 8, 0).setPropertyValue("NumberFormat", int(hidden_key))

        origin = FormulaOrigin(sheet, origin_cell, sheet.Name, 0, 0)
        assert prepare_auto_spill(ctx, doc, st, origin, code, grid) != "#SPILL!"

        # ODS and XLSX reloads keep every record cell recognizable.
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        hidden_prop = PropertyValue()
        hidden_prop.Name = "Hidden"
        hidden_prop.Value = True
        with tempfile.TemporaryDirectory(prefix="wa-spill-reload-") as tmpdir:
            for fname, flt in (("spill.ods", None), ("spill.xlsx", "Calc MS Excel 2007 XML")):
                path = uno.systemPathToFileUrl(os.path.join(tmpdir, fname))
                props = ()
                if flt is not None:
                    sp = PropertyValue()
                    sp.Name = "FilterName"
                    sp.Value = flt
                    props = (sp,)
                doc.storeToURL(path, props)
                reloaded = desktop.loadComponentFromURL(path, "_blank", 0, (hidden_prop,))
                try:
                    rsheet = reloaded.getSheets().getByIndex(0)
                    assert len(_unchanged_cells(rsheet, rec.cells)) == len(rec.cells)
                finally:
                    reloaded.close(True)

        # User formula and user text over one spilled cell each block the next spill and stay untouched.
        sheet.getCellByPosition(1, 0).setFormula("=1+1")
        assert prepare_auto_spill(ctx, doc, st, origin, code, grid) == "#SPILL!"
        assert sheet.getCellByPosition(1, 0).getFormula() == "=1+1"

        sheet.getCellByPosition(1, 0).setString("user text")
        assert prepare_auto_spill(ctx, doc, st, origin, code, grid) == "#SPILL!"
        assert sheet.getCellByPosition(1, 0).getFormula() == "user text"
    finally:
        WorkbookState.forget_key(st.key)
