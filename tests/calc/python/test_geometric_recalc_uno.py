# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO: geometric splice, insert/delete repair, leftover §10, and §10 edges.

Formula I/O confirms Calc's stored spelling (equals, $, prefix) so
rebuild_formula_with_data_args does not guess from CalcDocStub. Phase 3
covers insert/delete/undo and cap-hit skip. Leftover §10 product proofs:
Shared-kernel A3 reads a name assigned in A1 (F9-stable, strip), and
auto-spill still writes neighbors on a chained origin. Remaining §10:
hidden undo / locked unit, re-entrancy, Isolated + flag on, flag off.
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


_GEO_FLAG_KEY = "scripting.python_geometric_recalc_order"


def _path_under_root(path: str, root) -> bool:
    """True when *path* resolves under *root* (throwaway UserInstallation)."""
    from pathlib import Path

    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except Exception:
        return str(root) in str(path)


def _session_config_paths(ctx) -> list[str]:
    """``writeragent.json`` under the active UserInstallation only.

    Prefer the throwaway profile ``testing_runner`` created. Include the
    ctx-resolved / client ``_config_path`` only when it sits under that
    profile (or when there is no throwaway — ``--user-profile`` mode).
    Never rewrite the default user profile while a throwaway is active;
    leftover diag previously dumped production API keys from there.
    """
    from plugin.framework.config import _config_path, _resolve_config_path_from_ctx
    from plugin.testing_runner import throwaway_writeragent_json

    paths: list[str] = []
    throwaway = throwaway_writeragent_json()
    throwaway_root = None
    if throwaway is not None:
        paths.append(str(throwaway))
        # …/user/config/writeragent.json → UserInstallation root
        throwaway_root = throwaway.parent.parent.parent

    candidates: list[str] = []
    try:
        candidates.append(_resolve_config_path_from_ctx(ctx))
    except Exception:
        pass
    try:
        candidates.append(_config_path())
    except Exception:
        pass
    for candidate in candidates:
        if not candidate:
            continue
        if throwaway_root is None or _path_under_root(candidate, throwaway_root):
            paths.append(candidate)

    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _patch_config_files(ctx, mutator) -> None:
    """Load/mutate/write each UserInstallation ``writeragent.json``; invalidate cache."""
    import os

    from plugin.framework.config import (
        _invalidate_config_cache,
        _load_config_dict,
        _write_config_file,
    )
    from plugin.testing_runner import _progress

    for path in _session_config_paths(ctx):
        data: dict = {}
        if os.path.exists(path):
            loaded = _load_config_dict(path, allow_repair=True, persist_repair=False)
            if isinstance(loaded, dict):
                data = loaded
        mutator(data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_config_file(path, data)
        _progress("geometric config path=%s" % path)
    _invalidate_config_cache()


def _enable_geometric_flag(ctx):
    """Client monkeypatch + persist flag so soffice ``get_config`` sees it.

    URP leftover eval runs inside soffice. Patching only the client leaves
    soffice flag-off → no in-process record/strip → Isolated Shared names.
    """
    import plugin.calc.python.geometric_recalc as geo
    from plugin.framework.config import set_config

    previous = geo.geometric_flag_enabled
    geo.geometric_flag_enabled = lambda: True
    set_config(_GEO_FLAG_KEY, True)

    def _on(data: dict) -> None:
        data[_GEO_FLAG_KEY] = True

    _patch_config_files(ctx, _on)
    return previous


def _restore_geometric_flag(ctx, previous) -> None:
    """Drop persisted flag (default off) and restore the client monkeypatch."""
    import plugin.calc.python.geometric_recalc as geo
    from plugin.framework.config import set_config

    geo.geometric_flag_enabled = previous
    set_config(_GEO_FLAG_KEY, False)

    def _off(data: dict) -> None:
        data.pop(_GEO_FLAG_KEY, None)

    _patch_config_files(ctx, _off)
    geo.reset_geometric_runtime_for_tests()


def _disable_geometric_flag_client_and_file(ctx) -> None:
    """Flag-off mid-test: client patch + file so soffice modify stays off."""
    import plugin.calc.python.geometric_recalc as geo
    from plugin.framework.config import set_config

    geo.geometric_flag_enabled = lambda: False
    set_config(_GEO_FLAG_KEY, False)

    def _off(data: dict) -> None:
        data.pop(_GEO_FLAG_KEY, None)

    _patch_config_files(ctx, _off)


def _runtime_uid(doc) -> str:
    try:
        uid = doc.getPropertyValue("RuntimeUID")
    except Exception:
        return ""
    return str(uid or "")


def _close_extra_calc_docs(ctx, keep) -> int:
    """Close other Calcs so soffice leftover Shared sees recorded=1.

    Full ``make test-uno`` often still has another factory scalc. Each OnNew
    records a session; leftover then stays ``recorded=2`` / Isolated.
    """
    from plugin.doc.doc_type import is_calc
    from plugin.framework.uno_context import get_desktop

    keep_uid = _runtime_uid(keep)
    desktop = get_desktop(ctx)
    comps = getattr(desktop, "getComponents", lambda: None)()
    if comps is None or not hasattr(comps, "createEnumeration"):
        return 0
    enum = comps.createEnumeration()
    extras = []
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
        if keep_uid and _runtime_uid(model) == keep_uid:
            continue
        extras.append(model)
    closed = 0
    for other in extras:
        try:
            other.close(True)
            closed += 1
        except Exception:
            pass
    return closed


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


def _set_session_mode(ctx, mode: str) -> None:
    """Write session mode where soffice ``get_config`` will see it.

    Client ``set_config`` can use a cached path that is not the throwaway
    ``UserInstallation`` profile. Write only throwaway / in-profile paths
    (see ``_session_config_paths``).
    """
    from plugin.framework.config import set_config

    set_config("scripting.python_session_mode", mode)

    def _mode(data: dict) -> None:
        if mode == "isolated":
            data.pop("scripting.python_session_mode", None)
            data.pop("python_session_mode", None)
        else:
            data["scripting.python_session_mode"] = mode

    _patch_config_files(ctx, _mode)


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
                    body = handle.read()
            except OSError:
                continue
            needles = ("PYTHON eval:", "excel_py lifecycle:", "geometric on_open")
            hits = [
                line.strip()
                for line in body.splitlines()
                if any(needle in line for needle in needles)
            ]
            # Worker tail hid soffice session_id=None. These lines are the eval.
            shown = hits[-12:] if hits else ["<no PYTHON eval / lifecycle lines>"]
            lines.append(
                "leftover diag log=%s hits=%s"
                % (debug_log, " || ".join(shown).replace("\n", " "))
            )
    blob = " | ".join(lines)
    _progress(blob)
    return blob


def _wait_cell_value(
    doc,
    cell,
    expected: float,
    timeout: float = 8.0,
    *,
    fail_fast_substrings: tuple[str, ...] = (),
) -> bool:
    """True if *cell* reaches *expected*. False if sheet =PY is #NAME? (no add-in)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc.calculateAll()
        if cell.getValue() == expected:
            return True
        if cell.getError() in _PY_UNREGISTERED:
            return False
        text = str(cell.getString() or "")
        for needle in fail_fast_substrings:
            if needle in text:
                raise AssertionError(
                    "cell did not become %r: value=%r error=%r string=%r formula=%r"
                    % (
                        expected,
                        cell.getValue(),
                        cell.getError(),
                        text,
                        cell.getFormula(),
                    )
                )
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
    previous = _enable_geometric_flag(ctx)
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
        _restore_geometric_flag(ctx, previous)


@native_test
@with_native_doc("calc")
def test_geometric_cap_hit_sheet_stays_unchained(ctx, doc):
    """Cap-hit: skip the whole sheet, do not chain the first 100, one msgbox."""
    from unittest.mock import patch

    from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag(ctx)
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
        _restore_geometric_flag(ctx, previous)


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
    ``session_id`` is dropped (XAddIn has no calling workbook). Desktop
    scan records only when exactly one Calc is open. Worker restart must
    not clear recorded sessions (leftover after cap-hit saw
    ``recorded=0``). Throwaway ``writeragent.json`` is seeded ``shared``
    before soffice starts. Persist the geometric flag into that throwaway
    profile too — a client-only monkeypatch leaves soffice flag-off.
    Do not seed checkout ``.venv`` as ``python_venv_path`` — that made A3
    Isolated (GHA 33751116865 / 33752809831). Workers use office Python.
    """
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    _cold_kernel()
    previous = _enable_geometric_flag(ctx)
    try:
        # Shared is seeded before soffice starts. Do not sleep 2.1s here —
        # leftover must fail or pass on the first calculateAll, not a cache wait.
        _set_session_mode(ctx, "shared")
        _close_extra_calc_docs(ctx, doc)
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
            # Isolated NameError will not become 41; do not poll for 8s.
            reached = _wait_cell_value(
                doc,
                a3,
                41.0,
                timeout=2.0,
                fail_fast_substrings=("not defined",),
            )
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
        _restore_geometric_flag(ctx, previous)


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
    previous = _enable_geometric_flag(ctx)
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
        _restore_geometric_flag(ctx, previous)


def _undo_titles(um) -> list[str]:
    if um is None:
        return []
    try:
        return list(um.getAllUndoActionTitles())
    except Exception:
        return []


@native_test
@with_native_doc("calc")
def test_geometric_hidden_undo_and_locked_unit(ctx, doc):
    """§10 Undo: rewrite hides under a user edit; flag-on reconcile is one lock().

    Same shape as ``test_calc_spill_undo_lock``: when ``isUndoPossible()``,
    ``_undo_lock`` uses ``enterHiddenUndoContext`` so the geometric
    ``setFormula`` is not a second undo step. Flag-on / open reconcile with
    an empty stack is one locked unit (spec §5: may appear as one undo
    action), not a hidden-under-nothing no-op and not a fragment per cell.
    """
    from plugin.calc.python.geometric_recalc import (
        reconcile_geometric_document,
        reset_geometric_runtime_for_tests,
    )
    from plugin.tests.testing_utils import _clear_undo
    from plugin.testing_runner import _progress

    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag(ctx)
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a3 = sheet.getCellByPosition(0, 2)
        # Same unwrap as ``function._undo_lock`` — a wrapped manager's
        # ``lock()`` is a no-op and ``setFormula`` still records ``Input``.
        um = _unwrap_uno(doc).getUndoManager()
        assert um is not None

        # --- Flag-on reconcile with no prior edit is one locked unit ---
        # Wipe already called ``_clear_undo``. Do this *before* the hidden-
        # undo phase: an empty ``enterUndoContext("Input")`` can leave a
        # title that Classic ``clear()`` does not drop (first run leftover
        # ``['Input']``).
        _clear_undo(doc)
        a1.setFormula('=PY("lock_first")')
        a3.setFormula('=PY("lock_third")')
        a5_setup = sheet.getCellByPosition(0, 4)
        a5_setup.setFormula('=PY("lock_fifth")')
        _clear_undo(doc)
        _progress(
            "geometric locked-unit pre-reconcile undo=%s locked=%s titles=%s"
            % (
                um.isUndoPossible(),
                getattr(um, "isLocked", lambda: None)(),
                _undo_titles(um),
            )
        )
        assert um.isUndoPossible() is False, (
            "setup: empty stack required for the lock() path, leftover %s"
            % _undo_titles(um)
        )
        locked_before = _undo_titles(um)
        reconcile_geometric_document(ctx, doc)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        assert _pred(str(a5_setup.getFormula() or "")) == "A3", a5_setup.getFormula()
        locked_after = _undo_titles(um)
        _progress(
            "geometric locked-unit titles before=%s after=%s undo=%s"
            % (locked_before, locked_after, um.isUndoPossible())
        )
        # Spec §5 / §10: empty-stack reconcile is one unit (lock() or one
        # coalesced Input), not a fragment per rewritten cell. Two successors
        # attached; more than one new title would be the fragmentation bug.
        new_titles = len(locked_after) - len(locked_before)
        assert new_titles <= 1, (
            "flag-on reconcile must be one locked unit, not per-cell fragments: "
            "%s vs %s"
            % (locked_after, locked_before)
        )

        # --- Hidden under the user edit (isUndoPossible) ---
        a7 = sheet.getCellByPosition(0, 6)
        a7.setFormula('=PY("seventh")')
        um.enterUndoContext("Input")
        um.leaveUndoContext()
        assert um.isUndoPossible() is True
        titles_before = _undo_titles(um)
        _flush(ctx, doc, sheet)
        assert _pred(str(a7.getFormula() or "")) == "A5", a7.getFormula()
        titles_after = _undo_titles(um)
        _progress(
            "geometric hidden-undo titles before=%s after=%s"
            % (titles_before, titles_after)
        )
        assert titles_after == titles_before, (
            "geometric rewrite added extra undo actions: %s vs %s"
            % (titles_after, titles_before)
        )
    finally:
        _restore_geometric_flag(ctx, previous)


@native_test
@with_native_doc("calc")
def test_geometric_repair_setformula_does_not_reenter(ctx, doc):
    """§10 re-entrancy: repair ``setFormula`` must not nest a second repair.

    ``_GEOMETRIC_REPAIRING`` is the mechanism. A modify listener (and an
    explicit nested ``reconcile_geometric_sheet``) during apply must no-op.
    Call reconcile directly so ``_DISPATCHING`` is not the thing that
    prevents the nest — the geometric flag is.
    """
    import unohelper
    from com.sun.star.util import XModifyListener

    import plugin.calc.python.geometric_recalc as geo
    from plugin.testing_runner import _progress

    geo.reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag(ctx)
    orig_apply = geo._apply_patches_to_sheet
    orig_repair = geo._repair_one_sheet
    apply_enters: list[int] = []
    repair_calls: list[bool] = []

    def _counting_apply(sheet, patches):
        apply_enters.append(len(patches))
        assert geo.is_geometric_repairing() is True
        # Nested repair while the flag is up must not apply a second time.
        geo.reconcile_geometric_sheet(ctx, doc, sheet)
        return orig_apply(sheet, patches)

    def _counting_repair(*args, **kwargs):
        repair_calls.append(geo.is_geometric_repairing())
        return orig_repair(*args, **kwargs)

    class _ReenterProbe(unohelper.Base, XModifyListener):
        def __init__(self) -> None:
            self.repairing_hits = 0
            self.nested_apply_growth = 0

        def modified(self, aEvent) -> None:  # noqa: N802 — UNO
            if not geo.is_geometric_repairing():
                return
            self.repairing_hits += 1
            before = len(apply_enters)
            geo.reconcile_geometric_sheet(ctx, doc, sheet)
            if len(apply_enters) != before:
                self.nested_apply_growth += 1

        def disposing(self, Source) -> None:  # noqa: N802 — UNO
            return

    sheet = doc.getSheets().getByIndex(0)
    a1 = sheet.getCellByPosition(0, 0)
    a3 = sheet.getCellByPosition(0, 2)
    probe = _ReenterProbe()
    sheet.addModifyListener(probe)
    geo._apply_patches_to_sheet = _counting_apply
    geo._repair_one_sheet = _counting_repair
    try:
        a1.setFormula('=PY("first")')
        a3.setFormula('=PY("third")')
        # Direct document reconcile: _DISPATCHING stays false.
        geo.reconcile_geometric_document(ctx, doc)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        _progress(
            "geometric reenter apply=%s repair=%s listener_hits=%s nested_growth=%s"
            % (
                apply_enters,
                repair_calls,
                probe.repairing_hits,
                probe.nested_apply_growth,
            )
        )
        assert apply_enters, "repair never applied a patch"
        assert len(apply_enters) == 1, apply_enters
        assert repair_calls, repair_calls
        assert all(repair_calls), repair_calls
        assert probe.nested_apply_growth == 0
        # setFormula during apply should have seen the flag. If Classic
        # swallowed the listener, the explicit nest inside _counting_apply
        # still proved _GEOMETRIC_REPAIRING.
        if probe.repairing_hits == 0:
            _progress(
                "geometric reenter: modify listener saw 0 repairing hits; "
                "nested reconcile during apply still no-op'd"
            )
    finally:
        geo._apply_patches_to_sheet = orig_apply
        geo._repair_one_sheet = orig_repair
        try:
            sheet.removeModifyListener(probe)
        except Exception:
            pass
        _restore_geometric_flag(ctx, previous)


@native_test
@with_native_doc("calc")
def test_geometric_isolated_flag_on_noop_and_strip(ctx, doc):
    """§9.3 / §10: Isolated + flag on — no Shared globals; strip when unambiguous.

    Isolated + no init never records via init-kwargs. UI load/repair must
    call ``record_geometric_calc_session`` (``calc:`` + ``_workbook_session_key``,
    never empty) so the no-init case can pass the unambiguous check.
    Stay on the reused Calc — a second factory ``scalc`` makes
    ``off_main_calc_session_is_unambiguous()`` false. Do not assert Isolated
    always leaves ``_RECORDED_CALC_SESSION_IDS`` empty.
    """
    from plugin.calc.python.geometric_recalc import (
        current_geometric_strip_safe,
        geometric_workbook_key,
        maybe_geometric_on_document_open,
        maybe_strip_geometric_eval_args,
        reset_geometric_runtime_for_tests,
    )
    from plugin.scripting.session_manager import (
        clear_active_calc_session,
        get_cached_calc_session_id,
        off_main_calc_session_is_unambiguous,
        recorded_calc_session_count,
        workbook_session_id,
    )
    from plugin.testing_runner import _progress

    reset_geometric_runtime_for_tests()
    _cold_kernel()
    previous = _enable_geometric_flag(ctx)
    try:
        _set_session_mode(ctx, "isolated")
        _settle_soffice_config()
        # Isolated + no init: do not set an init script. Clear only so this
        # test can prove the UI path records — not that Isolated "always"
        # leaves the set empty (non-empty init already records).
        clear_active_calc_session()
        maybe_geometric_on_document_open(ctx, doc)
        sid = geometric_workbook_key(doc)
        _progress(
            "geometric isolated sid=%r cached=%r recorded=%s unambiguous=%s"
            % (
                sid,
                get_cached_calc_session_id(),
                recorded_calc_session_count(),
                off_main_calc_session_is_unambiguous(),
            )
        )
        assert sid.startswith("calc:"), sid
        assert sid != "calc:"
        assert sid[5:], sid
        assert get_cached_calc_session_id() == sid
        assert recorded_calc_session_count() >= 1
        assert off_main_calc_session_is_unambiguous() is True
        assert workbook_session_id(ctx, doc) is None

        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a2 = sheet.getCellByPosition(0, 1)
        a3 = sheet.getCellByPosition(0, 2)
        b1 = sheet.getCellByPosition(1, 0)
        b2 = sheet.getCellByPosition(1, 1)
        b3 = sheet.getCellByPosition(1, 2)
        b1.setValue(10)
        b2.setValue(20)
        b3.setValue(30)
        mean_code = "np.mean(data)"
        a1.setFormula('=PY("x_geo_iso = 41")')
        a2.setFormula('=PY("%s"; B1:B3)' % mean_code)
        a3.setFormula('=PY("x_geo_iso")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a2.getFormula() or "")) == "A1", a2.getFormula()
        assert _pred(str(a3.getFormula() or "")) == "A2", a3.getFormula()
        assert _pred(str(a1.getFormula() or "")) is None

        # Strip-safe from the live UNO repair; client eval reads the same sid.
        safe = current_geometric_strip_safe()
        assert any(
            key.workbook_key == sid and key.resolved_code == mean_code and key.n_args == 2
            for key in safe
        ), safe
        col = ((10.0,), (20.0,), (30.0,))
        pred = ((0.0,),)
        assert maybe_strip_geometric_eval_args(mean_code, [col, pred]) == [col]

        # Isolated is a no-op for Python globals (Shared names do not appear).
        # Do not wait for 41 — that is the Shared leftover. One or two
        # calculateAll is enough for a NameError; a Shared leak would be 41.
        doc.calculateAll()
        if a1.getError() in _PY_UNREGISTERED or a3.getError() in _PY_UNREGISTERED:
            if _skip_if_py_unregistered(
                a1, test_name="test_geometric_isolated_flag_on_noop_and_strip"
            ):
                return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and a3.getValue() != 41.0:
            if a3.getError() in _PY_UNREGISTERED:
                break
            doc.calculateAll()
            time.sleep(0.05)
        if a3.getError() in _PY_UNREGISTERED:
            if _skip_if_py_unregistered(
                a1, test_name="test_geometric_isolated_flag_on_noop_and_strip"
            ):
                return
        assert a3.getValue() != 41.0, _leftover_shared_diag(ctx, a1, a3)

        # Live soffice eval of np.mean is office-Python (GHA often has no
        # numpy). Client-side maybe_strip above is the arity proof.
        _progress(
            "geometric isolated live A2 value=%r error=%r string=%r formula=%r"
            % (a2.getValue(), a2.getError(), a2.getString(), a2.getFormula())
        )
    finally:
        _set_session_mode(ctx, "isolated")
        _restore_geometric_flag(ctx, previous)


@native_test
@with_native_doc("calc")
def test_geometric_flag_off_leaves_existing_refs(ctx, doc):
    """§9.4 / §10: flag off — no new attaches; leftover ``;pred`` stays.

    Do not build strip-on-disable. Existing geometric refs are valid DAG
    edges and must survive a later modify pass with the flag off.
    """
    import plugin.calc.python.geometric_recalc as geo

    geo.reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag(ctx)
    try:
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a3 = sheet.getCellByPosition(0, 2)
        a5 = sheet.getCellByPosition(0, 4)
        a1.setFormula('=PY("first")')
        a3.setFormula('=PY("third")')
        _flush(ctx, doc, sheet)
        leftover = str(a3.getFormula() or "")
        assert _pred(leftover) == "A1", leftover

        # Client flush + soffice modify must both see flag off.
        _disable_geometric_flag_client_and_file(ctx)
        a5.setFormula('=PY("fifth")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a5.getFormula() or "")) is None, a5.getFormula()
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        # Leftover field is still the same attach (not stripped on disable).
        assert "PY" in str(a3.getFormula() or "").upper()
    finally:
        _restore_geometric_flag(ctx, previous)
