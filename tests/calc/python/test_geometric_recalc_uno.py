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

from plugin.testing_runner import native_test, on_github_actions
from plugin.tests.testing_utils import with_native_doc

# Isolated testing_runner profiles do not inherit user-level unopkg, so sheet
# =PY() is #NAME? (504/525) unless the runner seeds ``user/uno_packages``
# from ``make register-built-oxt``. Linux PR CI must assert values, not skip.
# Direct PythonFunction calls are not a substitute (they bypass Calc order).
# Same codes as test_py_dag_chain_uno.py.
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
    """Drop this process's worker. Soffice's worker is a different process."""
    from plugin.calc.python.function import clear_python_addin_cache
    from plugin.scripting.venv_worker import PythonWorkerManager

    PythonWorkerManager.shutdown_all()
    clear_python_addin_cache()


def _settle_soffice_config() -> None:
    """``get_config`` in soffice is mtime-cached for 2s. Client ``set_config`` is not enough."""
    time.sleep(2.1)


def _session_config_paths(ctx) -> list[str]:
    """Every ``writeragent.json`` soffice or the URP client might read."""

    from plugin.framework.config import _config_path, _resolve_config_path_from_ctx
    from plugin.testing_runner import (
        _libreoffice_user_profile_dir,
        throwaway_writeragent_json,
    )

    paths: list[str] = []
    try:
        paths.append(_resolve_config_path_from_ctx(ctx))
    except Exception:
        pass
    try:
        paths.append(_config_path())
    except Exception:
        pass
    extra = throwaway_writeragent_json()
    if extra is not None:
        paths.append(str(extra))
    paths.append(str(_libreoffice_user_profile_dir() / "user" / "config" / "writeragent.json"))
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _set_session_mode(ctx, mode: str) -> None:
    """Write session mode where soffice ``get_config`` will see it.

    Client ``set_config`` can use a cached path that is not the throwaway
    ``UserInstallation`` profile. Soffice PathSettings ``UserConfig`` is
    usually the throwaway ``user/config``, but a cached ``init_config``
    path can stay on the default user profile. Write every candidate.
    """
    import os

    from plugin.framework.config import (
        _invalidate_config_cache,
        _load_config_dict,
        _write_config_file,
        set_config,
    )
    from plugin.testing_runner import _progress

    set_config("scripting.python_session_mode", mode)
    for path in _session_config_paths(ctx):
        data: dict = {}
        if os.path.exists(path):
            loaded = _load_config_dict(path, allow_repair=True, persist_repair=False)
            if isinstance(loaded, dict):
                data = loaded
        if mode == "isolated":
            data.pop("scripting.python_session_mode", None)
            data.pop("python_session_mode", None)
        else:
            data["scripting.python_session_mode"] = mode
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_config_file(path, data)
        _progress("geometric session_mode=%s path=%s" % (mode, path))
    _invalidate_config_cache()


def _leftover_shared_diag(ctx, a1, a3) -> str:
    """Config + cell snapshot when leftover Shared does not become 41."""
    from plugin.framework.config import get_config_str
    from plugin.scripting.session_manager import (
        off_main_calc_session_is_unambiguous,
        python_session_mode,
        recorded_calc_session_count,
    )
    from plugin.testing_runner import _progress

    try:
        mode = python_session_mode(ctx)
        venv = get_config_str("scripting.python_venv_path")
    except Exception as exc:
        mode = "<unreadable %s>" % exc
        venv = ""
    lines = [
        "leftover diag client_mode=%s venv=%r recorded=%s unambiguous=%s"
        % (mode, venv, recorded_calc_session_count(), off_main_calc_session_is_unambiguous()),
        "leftover diag A1 value=%r error=%r string=%r formula=%r"
        % (a1.getValue(), a1.getError(), a1.getString(), a1.getFormula()),
        "leftover diag A3 value=%r error=%r string=%r formula=%r"
        % (a3.getValue(), a3.getError(), a3.getString(), a3.getFormula()),
    ]
    for path in _session_config_paths(ctx):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            text = "<unreadable %s>" % exc
        lines.append("leftover diag file=%s body=%s" % (path, text.replace("\n", " ")))
        debug_log = path.replace("writeragent.json", "writeragent_debug.log")
        if debug_log != path:
            try:
                with open(debug_log, encoding="utf-8") as handle:
                    tail = handle.read()[-2000:]
                lines.append("leftover diag log=%s tail=%s" % (debug_log, tail.replace("\n", " ")))
            except OSError:
                pass
    blob = " | ".join(lines)
    _progress(blob)
    return blob


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
    """Log-and-skip on local blank-profile #NAME?. GitHub Actions must assert values."""
    if cell.getError() not in _PY_UNREGISTERED:
        return False
    if on_github_actions():
        raise AssertionError(
            "[%s] sheet =PY() is #NAME? on GitHub Actions (error=%r value=%r formula=%r). "
            "testing_runner must seed WriterAgent into the throwaway UserInstallation "
            "from the user-level uno_packages that register-built-oxt wrote."
            % (test_name, cell.getError(), cell.getValue(), cell.getFormula())
        )
    from plugin.framework.logging import log

    log.warning(
        "[%s] skip live sheet eval — value=%r error=%r formula=%r (add-in not registered)",
        test_name,
        cell.getValue(),
        cell.getError(),
        cell.getFormula(),
    )
    return True


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

    A1 assigns ``x_geo_live = 41``. A2 stays empty so A3's predecessor is A1.
    A3 is ``=PY("x_geo_live")`` with no user-typed ``;A1``. After deferred
    attach, A3's formula names A1. ``calculateAll`` must yield 41 (geometric
    DAG order on a unique Shared name). A second ``calculateAll`` must stay
    41. Precedent-only strip is Phase 4 unit-tested; headless soffice eval
    is off Python MainThread so live ``data is None`` cannot be observed.

    Stay on the reused calc doc. A second factory ``scalc`` makes
    ``off_main_calc_session_is_unambiguous()`` false, so Shared
    ``session_id`` is dropped (XAddIn has no calling workbook). Throwaway
    ``writeragent.json`` is seeded ``shared`` before soffice starts. Do
    not seed checkout ``.venv`` as ``python_venv_path`` — that made A3
    Isolated (GHA 33751116865 / 33752809831). Workers use office Python.
    """
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    _cold_kernel()
    previous = _enable_geometric_flag()
    try:
        _set_session_mode(ctx, "shared")
        _settle_soffice_config()
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a3 = sheet.getCellByPosition(0, 2)
        # Unique name: leftover ``x`` from other tests cannot fake the first F9.
        a1.setFormula('=PY("x_geo_live = 41")')
        a3.setFormula('=PY("x_geo_live")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        assert _pred(str(a1.getFormula() or "")) is None

        try:
            reached = _wait_cell_value(doc, a3, 41.0)
        except AssertionError as exc:
            raise AssertionError("%s | %s" % (exc, _leftover_shared_diag(ctx, a1, a3))) from exc
        if not reached:
            if _skip_if_py_unregistered(
                a1, test_name="test_geometric_shared_kernel_a3_reads_a1_f9_stable"
            ):
                return
            raise AssertionError(
                "A3 did not become 41 after attach+F9: %s"
                % _leftover_shared_diag(ctx, a1, a3)
            )
        if a3.getValue() != 41.0:
            raise AssertionError(_leftover_shared_diag(ctx, a1, a3))

        # Second F9. Same value — geometric edge stays, Shared name persists.
        doc.calculateAll()
        if a3.getValue() != 41.0:
            raise AssertionError(_leftover_shared_diag(ctx, a1, a3))
    finally:
        _set_session_mode(ctx, "isolated")
        _restore_geometric_flag(previous)


@native_test
@with_native_doc("calc")
def test_geometric_chained_origin_still_auto_spills(ctx, doc):
    """§10 leftover: attaching ``;pred`` must not collapse an auto-spill origin to 1×1.

    Unchained live spill is already covered by ``test_calc_spill_undo_lock``
    (direct ``perform_deferred_spill``) and ``test_function`` DummyTimer cases.
    This adds the chained origin next to those. Attaching ``;pred`` must
    still match the origin (``is_matching_py_formula``). Neighbors use the
    same ``perform_deferred_spill`` path as ``test_calc_spill_undo_lock``.
    """
    from plugin.calc.python.formula_locator_cache import is_matching_py_formula
    from plugin.calc.python.function import perform_deferred_spill
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests
    from plugin.framework.config import get_config_bool

    assert get_config_bool("scripting.python_auto_spill") is True
    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag()
    try:
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a3 = sheet.getCellByPosition(0, 2)
        spill_code = "result = [[10, 20], [30, 40]]"
        a1.setFormula('=PY("result = 1")')
        a3.setFormula('=PY("%s")' % spill_code)
        _flush(ctx, doc, sheet)
        stored = str(a3.getFormula() or "")
        assert _pred(stored) == "A1", stored
        # Attaching ;pred must not break spill's origin match (code arg only).
        assert is_matching_py_formula(stored, spill_code), stored

        if a1.getError() in _PY_UNREGISTERED or a3.getError() in _PY_UNREGISTERED:
            if _skip_if_py_unregistered(
                a1, test_name="test_geometric_chained_origin_still_auto_spills"
            ):
                return

        doc_url = getattr(doc, "getURL", lambda: "")() or ""
        perform_deferred_spill(
            ctx,
            doc_url,
            sheet.Name,
            2,
            0,
            [[10, 20], [30, 40]],
            doc=doc,
            code=spill_code,
        )
        assert a3.getString() != "#SPILL!", a3.getString()
        assert sheet.getCellByPosition(1, 2).getValue() == 20.0
        assert sheet.getCellByPosition(0, 3).getValue() == 30.0
        assert sheet.getCellByPosition(1, 3).getValue() == 40.0
    finally:
        _restore_geometric_flag(previous)
