# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO: geometric splice, insert/delete repair, and live Shared-kernel eval.

Formula I/O confirms Calc's stored spelling (equals, $, prefix) so
rebuild_formula_with_data_args does not guess from CalcDocStub. Phase 3
covers insert/delete/undo and cap-hit skip. Leftover §10 product proofs:
Shared-kernel A3 reads a name assigned in A1 (F9-stable, strip), and
auto-spill still writes neighbors on a chained origin.
"""

from __future__ import annotations

import time

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

# Isolated testing_runner profiles do not unopkg the OXT, so sheet =PY() is
# #NAME? (504/525). Linux PR CI installs the extension — those runs must
# assert values, not skip. Direct PythonFunction calls are not a substitute
# (they bypass Calc order). Same codes as test_py_dag_chain_uno.py.
_PY_UNREGISTERED = frozenset({504, 525})


def _store(cell, formula: str) -> str:
    cell.setFormula(formula)
    return str(cell.getFormula() or "")


@native_test
@with_native_doc("calc")
def test_geometric_formula_io_roundtrip(ctx, doc):
    from plugin.calc.python.formula_edit import (
        parse_python_formula,
        py_formula_has_unquoted_code_ref,
    )
    from plugin.calc.python.geometric_recalc import (
        formula_data_args,
        rebuild_formula_with_data_args,
    )

    sheet = doc.getSheets().getByIndex(0)
    a1 = sheet.getCellByPosition(0, 0)
    a2 = sheet.getCellByPosition(0, 1)
    b1 = sheet.getCellByPosition(1, 0)
    c5 = sheet.getCellByPosition(2, 4)
    a1.setString("result = 1")
    c5.setValue(5)

    # --- Probe: what does getFormula actually store? ---
    # Confirmed on Classic (Linux): =py("…"; $C$5) — has '=', lowercase py,
    # keeps $, no sheet prefix, not OriginalName. Splice uses parts.prefix.
    samples = {
        "quoted_abs": _store(a2, '=PY("y"; $C$5)'),
        "quoted_range": _store(b1, '=PY("np.mean(data)"; B1:B10)'),
        "code_in_cell": _store(sheet.getCellByPosition(3, 0), "=PY($A$1; B1:B10)"),
    }
    print("geometric getFormula probe:", samples)

    # 1) =PY("y"; $C$5) attach predecessor — $C$5 stays absolute.
    stored = samples["quoted_abs"]
    parts = parse_python_formula(stored)
    assert parts is not None, stored
    assert parts.prefix, stored
    old_args = formula_data_args(stored)
    assert old_args is not None, stored
    rebuilt = rebuild_formula_with_data_args(stored, old_args + ["A1"])
    assert rebuilt is not None
    assert rebuilt.startswith("="), rebuilt
    assert rebuilt.startswith(parts.prefix), (parts.prefix, rebuilt)
    a2.setFormula(rebuilt)
    after = str(a2.getFormula() or "")
    reparsed = parse_python_formula(after)
    assert reparsed is not None, after
    after_args = formula_data_args(after)
    assert after_args is not None and len(after_args) >= 2, after
    # Absolute user ref must survive setFormula+getFormula (Calc may keep $C$5
    # or add a sheet; do not accept a relative C5 if $ was stored).
    first_arg = after_args[0]
    stored_first = old_args[0]
    if "$" in stored_first:
        assert "$" in first_arg, (stored, rebuilt, after)
        assert first_arg.replace("$", "").upper().endswith("C5"), after
    else:
        # Calc itself dropped $ on first store — splice must not invent a
        # different cell, and must still parse.
        assert first_arg.replace("$", "").upper().endswith("C5"), after

    # 2) Quoted code + range: splice appends a pred; getFormula still parses.
    stored_range = samples["quoted_range"]
    range_parts = parse_python_formula(stored_range)
    assert range_parts is not None, stored_range
    range_args = formula_data_args(stored_range)
    assert range_args is not None, stored_range
    rebuilt_range = rebuild_formula_with_data_args(stored_range, range_args + ["A1"])
    assert rebuilt_range is not None
    assert rebuilt_range.startswith("="), rebuilt_range
    assert "np.mean(data)" in rebuilt_range
    b1.setFormula(rebuilt_range)
    after_range = str(b1.getFormula() or "")
    again = parse_python_formula(after_range)
    assert again is not None, after_range
    assert again.code == "np.mean(data)"
    after_range_args = formula_data_args(after_range)
    assert after_range_args is not None and len(after_range_args) == len(range_args) + 1, after_range

    # 3) Unquoted code-in-cell keeps the unquoted token (or Calc's stored form).
    stored_ref = samples["code_in_cell"]
    assert py_formula_has_unquoted_code_ref(stored_ref), stored_ref
    ref_parts = parse_python_formula(stored_ref)
    assert ref_parts is not None, stored_ref
    ref_args = formula_data_args(stored_ref)
    assert ref_args is not None, stored_ref
    rebuilt_ref = rebuild_formula_with_data_args(stored_ref, ref_args + ["C5"])
    assert rebuilt_ref is not None
    assert '"$A$1"' not in rebuilt_ref
    assert '"A1"' not in rebuilt_ref
    sheet.getCellByPosition(3, 0).setFormula(rebuilt_ref)
    after_ref = str(sheet.getCellByPosition(3, 0).getFormula() or "")
    assert py_formula_has_unquoted_code_ref(after_ref), after_ref
    after_ref_parts = parse_python_formula(after_ref)
    assert after_ref_parts is not None, after_ref
    assert after_ref_parts.code.replace("$", "").upper().endswith("A1"), after_ref
    if "$" in ref_parts.code:
        assert "$" in after_ref_parts.code, after_ref


def _pred(formula: str) -> str | None:
    from plugin.calc.python.geometric_recalc import formula_data_args, local_a1

    args = formula_data_args(formula)
    if not args:
        return None
    return local_a1(args[-1])


def _enable_geometric_flag():
    import plugin.calc.python.geometric_recalc as geo

    previous = geo.geometric_flag_enabled
    geo.geometric_flag_enabled = lambda: True
    return previous


def _restore_geometric_flag(previous) -> None:
    import plugin.calc.python.geometric_recalc as geo

    geo.geometric_flag_enabled = previous
    geo.reset_geometric_runtime_for_tests()


def _flush(ctx, doc, sheet) -> None:
    from plugin.calc.python.sheet_modify import flush_sheet_modify_pass_for_tests

    flush_sheet_modify_pass_for_tests(ctx, doc, sheet)


def _cold_kernel() -> None:
    """Drop leftover worker state so F9 cannot succeed on a warm Shared name."""
    from plugin.calc.python.function import clear_python_addin_cache
    from plugin.scripting.venv_worker import PythonWorkerManager

    PythonWorkerManager.shutdown_all()
    clear_python_addin_cache()


def _record_this_workbook_only(doc) -> None:
    """Strip needs exactly one recorded Calc session (unanimous-ours + unambiguous).

    Other UNO tests may have left ids in ``_RECORDED_CALC_SESSION_IDS``. Clear
    those, then record this doc the same way repair / Shared-kernel eval do.
    """
    import plugin.scripting.session_manager as sm
    from plugin.calc.python.geometric_recalc import record_geometric_calc_session

    sm.clear_active_calc_session()
    record_geometric_calc_session(doc)
    sm.calc_workbook_base_session_id(doc)


def _wait_cell_value(doc, cell, expected: float, timeout: float = 8.0) -> bool:
    """True if *cell* reaches *expected*. False if sheet =PY is #NAME? (no add-in)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc.calculateAll()
        if cell.getValue() == expected:
            return True
        if cell.getError() in _PY_UNREGISTERED:
            return False
        time.sleep(0.05)
    if cell.getValue() == expected:
        return True
    if cell.getError() in _PY_UNREGISTERED:
        return False
    raise AssertionError(
        "cell did not become %r: value=%r error=%r string=%r formula=%r"
        % (
            expected,
            cell.getValue(),
            cell.getError(),
            cell.getString(),
            cell.getFormula(),
        )
    )


def _skip_if_py_unregistered(cell, *, test_name: str) -> bool:
    """Log-and-skip on blank-profile #NAME?. Return True when the caller should return."""
    if cell.getError() not in _PY_UNREGISTERED:
        return False
    from plugin.framework.logging import log

    log.warning(
        "[%s] skip live sheet eval — value=%r error=%r formula=%r (add-in not registered)",
        test_name,
        cell.getValue(),
        cell.getError(),
        cell.getFormula(),
    )
    return True


# User-script body. Runs in soffice's Python (OXT), not the URP client.
# Client-side reconcile updates the client's _STRIP_SAFE; sheet =PY() strip
# reads soffice's map. Same for Shared-kernel worker shutdown.
_SOFFICE_GEO_SCRIPT = '''
def reconcile():
    import plugin.calc.python.geometric_recalc as geo
    ctx = XSCRIPTCONTEXT.getComponentContext()
    doc = XSCRIPTCONTEXT.getDocument()
    geo.geometric_flag_enabled = lambda: True
    geo.reset_geometric_runtime_for_tests()
    geo.record_geometric_calc_session(doc)
    geo.reconcile_geometric_document(ctx, doc)


def cold_kernel():
    from plugin.calc.python.function import clear_python_addin_cache
    from plugin.scripting.venv_worker import PythonWorkerManager
    PythonWorkerManager.shutdown_all()
    clear_python_addin_cache()


def restore_flag():
    import plugin.calc.python.geometric_recalc as geo
    geo.reset_geometric_runtime_for_tests()
'''


def _user_scripts_python_dirs(ctx) -> list[str]:
    """Candidate ``$(user)/Scripts/python`` dirs (testing_runner throwaway profile)."""
    import glob
    import os

    import uno

    dirs: list[str] = []

    def _add_user_dir(raw: str) -> None:
        path = (raw or "").strip()
        if path.startswith("file://"):
            path = uno.fileUrlToSystemPath(path)
        if not path:
            return
        user_dir = path
        steps = 0
        while steps < 4 and os.path.basename(user_dir) != "user":
            parent = os.path.dirname(user_dir)
            if parent == user_dir:
                break
            user_dir = parent
            steps += 1
        if os.path.basename(user_dir) != "user":
            user_dir = path
        candidate = os.path.join(user_dir, "Scripts", "python")
        if candidate not in dirs:
            dirs.append(candidate)

    smgr = ctx.getServiceManager()
    try:
        subst = smgr.createInstanceWithContext(
            "com.sun.star.util.PathSubstitution", ctx
        )
        _add_user_dir(str(subst.substituteVariables("$(user)", True) or ""))
    except Exception:
        pass
    try:
        ps = smgr.createInstanceWithContext("com.sun.star.util.PathSettings", ctx)
        _add_user_dir(str(getattr(ps, "UserConfig", "") or ""))
    except Exception:
        pass
    try:
        from plugin.framework.config import _config_path

        _add_user_dir(os.path.dirname(_config_path()))
    except Exception:
        pass
    for match in glob.glob("/tmp/writeragent-lo-test-profile-*/user"):
        _add_user_dir(match)
    return dirs


def _invoke_soffice_python(ctx, doc, func_name: str) -> None:
    """Run a user Python macro inside soffice so eval sees the same in-memory maps."""
    import os

    script_dirs = _user_scripts_python_dirs(ctx)
    if not script_dirs:
        raise AssertionError("could not resolve $(user)/Scripts/python")
    for scripts_dir in script_dirs:
        os.makedirs(scripts_dir, exist_ok=True)
        script_path = os.path.join(scripts_dir, "wa_geo_eval.py")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(_SOFFICE_GEO_SCRIPT)
        print("wa_geo_eval wrote", script_path, flush=True)

    smgr = ctx.getServiceManager()
    factory = smgr.createInstanceWithContext(
        "com.sun.star.script.provider.MasterScriptProviderFactory", ctx
    )
    provider = factory.createScriptProvider(doc)
    uri = (
        "vnd.sun.star.script:wa_geo_eval.%s?language=Python&location=user"
        % func_name
    )
    script = provider.getScript(uri)
    script.invoke((), (), ())


def _soffice_reconcile(ctx, doc) -> None:
    _invoke_soffice_python(ctx, doc, "reconcile")


def _soffice_cold_kernel(ctx, doc) -> None:
    _invoke_soffice_python(ctx, doc, "cold_kernel")


def _soffice_restore_flag(ctx, doc) -> None:
    try:
        _invoke_soffice_python(ctx, doc, "restore_flag")
    except Exception:
        pass


@native_test
@with_native_doc("calc")
def test_geometric_insert_delete_undo_three_cell_column(ctx, doc):
    """Phase 3: insert/delete retargets; successor-becomes-first removes the field; undo restores."""
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag()
    try:
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a2 = sheet.getCellByPosition(0, 1)
        a3 = sheet.getCellByPosition(0, 2)
        a1.setFormula('=PY("first")')
        a3.setFormula('=PY("third")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        assert _pred(str(a1.getFormula() or "")) is None

        # 1) Insert PY in the middle → successor names the new predecessor.
        a2.setFormula('=PY("mid")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a2.getFormula() or "")) == "A1", a2.getFormula()
        assert _pred(str(a3.getFormula() or "")) == "A2", a3.getFormula()

        # 2) Delete middle → successor retargets to A1.
        a2.setFormula("")
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()

        # 3) Undo restores the inserted cell and A3's ;A2 (hidden under the delete).
        um = doc.getUndoManager()
        assert um is not None
        if um.isUndoPossible():
            um.undo()
            # Either one undo restores both (hidden) or the user cell is back.
            restored = str(a2.getFormula() or "")
            if "PY" in restored.upper() or "PYTHON" in restored.upper():
                _flush(ctx, doc, sheet)
                assert _pred(str(a3.getFormula() or "")) == "A2", (
                    a2.getFormula(),
                    a3.getFormula(),
                )

        # Successor-becomes-first → remove-field.
        a2.setFormula("")
        a1.setFormula("")
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) is None, a3.getFormula()
    finally:
        _restore_geometric_flag(previous)


@native_test
@with_native_doc("calc")
def test_geometric_cap_hit_sheet_stays_unchained(ctx, doc):
    """Cap-hit: skip the whole sheet, do not chain the first 100, one msgbox."""
    from unittest.mock import patch

    from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag()
    try:
        sheet = doc.getSheets().getByIndex(0)
        for i in range(_MAX_PYTHON_CELLS_FOUND + 1):
            sheet.getCellByPosition(0, i).setFormula(f'=PY("x{i}")')
        with patch("plugin.chatbot.dialogs.msgbox") as mock_box:
            _flush(ctx, doc, sheet)
            assert mock_box.call_count == 1
        # First successor stays unchained (whole sheet skipped).
        a2 = str(sheet.getCellByPosition(0, 1).getFormula() or "")
        assert _pred(a2) is None, a2
        a101 = str(sheet.getCellByPosition(0, _MAX_PYTHON_CELLS_FOUND).getFormula() or "")
        assert _pred(a101) is None, a101
    finally:
        _restore_geometric_flag(previous)


@native_test
@with_native_doc("calc")
def test_geometric_shared_kernel_a3_reads_a1_f9_stable(ctx, doc):
    """§10 leftover: Shared kernel, flag on — A3 reads A1's name; F9-stable; strip.

    A1 assigns ``x = 41``. A2 stays empty so A3's predecessor is A1. A3 is
    ``=PY("result = x if data is None else -999")`` with no user-typed ``;A1``.
    After deferred attach, A3's formula names A1. A cold worker + calculateAll
    must yield 41 (geometric order, not leftover kernel state). A second
    calculateAll must stay 41. ``-999`` means strip failed and A1's return
    was packed as ``data``.
    """
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests
    from plugin.framework.config import set_config

    reset_geometric_runtime_for_tests()
    _cold_kernel()
    previous = _enable_geometric_flag()
    try:
        set_config("scripting.python_session_mode", "shared")
        _record_this_workbook_only(doc)
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a3 = sheet.getCellByPosition(0, 2)
        # A2 left empty — A3's row-major predecessor is A1.
        a1.setFormula('=PY("x = 41")')
        a3.setFormula('=PY("result = x if data is None else -999")')
        _flush(ctx, doc, sheet)
        # Client flush writes the formula; soffice reconcile fills soffice's
        # strip-safe index + recorded session (eval runs in that process).
        _soffice_reconcile(ctx, doc)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        assert _pred(str(a1.getFormula() or "")) is None

        # Cold soffice worker after attach so the first F9 cannot succeed
        # because setFormula already left ``x`` in the Shared namespace.
        _soffice_cold_kernel(ctx, doc)
        _record_this_workbook_only(doc)
        if not _wait_cell_value(doc, a3, 41.0):
            if _skip_if_py_unregistered(
                a1, test_name="test_geometric_shared_kernel_a3_reads_a1_f9_stable"
            ):
                return
            raise AssertionError(
                "A3 did not become 41 after attach+F9: value=%r error=%r string=%r formula=%r"
                % (a3.getValue(), a3.getError(), a3.getString(), a3.getFormula())
            )
        assert a3.getValue() == 41.0, (
            a3.getValue(),
            a3.getString(),
            a3.getFormula(),
        )

        # Second F9 (warm kernel is fine here — first pass already proved order).
        doc.calculateAll()
        assert a3.getValue() == 41.0, (
            a3.getValue(),
            a3.getString(),
            a3.getFormula(),
        )
    finally:
        set_config("scripting.python_session_mode", "isolated")
        _soffice_restore_flag(ctx, doc)
        _restore_geometric_flag(previous)


@native_test
@with_native_doc("calc")
def test_geometric_chained_origin_still_auto_spills(ctx, doc):
    """§10 leftover: attaching ``;pred`` must not collapse an auto-spill origin to 1×1.

    Unchained live spill is already covered by ``test_calc_spill_undo_lock``
    (direct ``perform_deferred_spill``) and ``test_function`` DummyTimer cases.
    This adds the chained origin next to those, using the same spill path.
    """
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests
    from plugin.framework.config import get_config_bool

    assert get_config_bool("scripting.python_auto_spill") is True
    reset_geometric_runtime_for_tests()
    _cold_kernel()
    previous = _enable_geometric_flag()
    try:
        _record_this_workbook_only(doc)
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a3 = sheet.getCellByPosition(0, 2)
        b3 = sheet.getCellByPosition(1, 2)
        a4 = sheet.getCellByPosition(0, 3)
        b4 = sheet.getCellByPosition(1, 3)
        a1.setFormula('=PY("result = 1")')
        # 2×2 list — origin A3, neighbors B3 / A4 / B4. Distinct code from A1
        # so locate_formula_cell_in_doc stays unique after attach.
        a3.setFormula('=PY("result = [[10, 20], [30, 40]]")')
        _flush(ctx, doc, sheet)
        _soffice_reconcile(ctx, doc)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()

        if not _wait_cell_value(doc, a3, 10.0):
            if _skip_if_py_unregistered(
                a1, test_name="test_geometric_chained_origin_still_auto_spills"
            ):
                return
            raise AssertionError(
                "chained origin did not become 10: value=%r error=%r string=%r formula=%r"
                % (a3.getValue(), a3.getError(), a3.getString(), a3.getFormula())
            )

        assert a3.getValue() == 10.0, (a3.getValue(), a3.getString(), a3.getFormula())
        assert a3.getString() != "#SPILL!", a3.getString()
        # Deferred spill writes neighbors from soffice (0.1s timer). Poll.
        if not _wait_cell_value(doc, b3, 20.0):
            raise AssertionError(
                "chained origin did not spill to B3: value=%r string=%r origin=%r"
                % (b3.getValue(), b3.getString(), a3.getFormula())
            )
        assert a4.getValue() == 30.0, (a4.getValue(), a4.getString())
        assert b4.getValue() == 40.0, (b4.getValue(), b4.getString())
    finally:
        _soffice_restore_flag(ctx, doc)
        _restore_geometric_flag(previous)
