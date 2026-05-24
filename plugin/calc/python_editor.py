# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Open the Monaco editor for the active Calc cell's ``=PYTHON()`` formula."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.bridge import CalcBridge
from plugin.calc.python_formula_edit import (
    PythonFormulaParts,
    build_new_python_formula,
    cell_looks_python_like,
    format_data_binding_display,
    parse_python_formula,
    rebuild_python_formula,
)
from plugin.chatbot.dialogs import msgbox
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_desktop
from plugin.scripting.editor_bridge import EditorSession, get_active_session, set_active_session
from plugin.scripting.editor_diagnostics import failure_message
from plugin.scripting.editor_launcher import probe_webview_import, resolve_editor_python, spawn_editor_process

log = logging.getLogger("writeragent.scripting")


def _cell_formula_strings(cell: Any) -> list[str]:
    """Collect formula strings LibreOffice may expose for the cell."""
    out: list[str] = []
    try:
        f = cell.getFormula()
        if f:
            out.append(str(f))
    except Exception:
        pass
    for prop in ("FormulaLocal", "Formula"):
        try:
            val = cell.getPropertyValue(prop)
            if val and str(val) not in out:
                out.append(str(val))
        except Exception:
            pass
    return out


def _parse_cell_python_formula(cell: Any) -> tuple[str, PythonFormulaParts | None, str | None]:
    """Return (code, parts, source formula string that parsed) from the cell."""
    for raw in _cell_formula_strings(cell):
        parts = parse_python_formula(raw)
        if parts is not None:
            return parts.code, parts, raw
    return "", None, None


def _load_cell_editor_code(cell: Any) -> tuple[str, PythonFormulaParts | None, str | None]:
    """Return Monaco source: stripped PYTHON code or plain cell text."""
    code, parts, source = _parse_cell_python_formula(cell)
    if parts is not None:
        return code, parts, source
    if _cell_has_unparsed_python(cell):
        return "", None, None
    try:
        plain = cell.getString()
        if plain:
            return str(plain), None, None
    except Exception:
        log.debug("python_editor: getString failed", exc_info=True)
    return "", None, None


def build_editor_formula_save(
    *,
    parsed_parts: PythonFormulaParts | None,
    new_code: str,
    cell_has_unparsed_python: bool,
) -> str | dict[str, Any]:
    """Build ``=PYTHON("…")`` for formula-mode save, or an error dict when args cannot be preserved."""
    if parsed_parts is not None:
        return rebuild_python_formula(parsed_parts, new_code)
    if cell_has_unparsed_python:
        return {
            "type": "error",
            "message": _(
                "Could not preserve this cell's PYTHON formula arguments (e.g. data ranges). "
                "Edit the formula in Calc, or use a quoted code string like =PYTHON(\"code\"; A1:B10)."
            ),
        }
    return build_new_python_formula(new_code)


def _cell_has_unparsed_python(cell: Any) -> bool:
    """True when the cell looks like PYTHON but strict parse failed (data binding at risk)."""
    for raw in _cell_formula_strings(cell):
        if cell_looks_python_like(raw) and parse_python_formula(raw) is None:
            return True
    return False


def _get_active_calc_cell(ctx: Any) -> tuple[Any, Any, str] | None:
    """Return (doc, cell, primary formula string) for the current selection, or None."""
    desktop = get_desktop(ctx)
    if desktop is None:
        log.warning("python_editor: no desktop")
        return None
    frame = desktop.getCurrentFrame()
    if frame is None:
        log.warning("python_editor: no current frame")
        return None
    controller = frame.getController()
    if controller is None:
        log.warning("python_editor: no controller")
        return None
    model = controller.getModel()
    if model is None or not hasattr(model, "getSheets"):
        log.warning("python_editor: not a spreadsheet document")
        return None
    cc = model.getCurrentController()
    if cc is None:
        log.warning("python_editor: no CurrentController")
        return None
    selection = cc.getSelection()
    if selection is None:
        log.warning("python_editor: no selection on CurrentController")
        return None
    try:
        addr = selection.getRangeAddress()
    except Exception:
        log.warning("python_editor: selection has no RangeAddress", exc_info=True)
        return None
    bridge = CalcBridge(model)
    sheet = bridge.get_active_sheet()
    cell = bridge.get_cell(sheet, addr.StartColumn, addr.StartRow)
    formulas = _cell_formula_strings(cell)
    formula = formulas[0] if formulas else ""
    log.info("python_editor: cell (%s,%s) formulas=%r", addr.StartColumn, addr.StartRow, formulas)
    return model, cell, formula


def _recalculate_after_save(doc: Any) -> None:
    try:
        doc.calculateAll()
    except Exception:
        log.debug("calculateAll after editor save failed", exc_info=True)


def _apply_formula_save(
    doc: Any,
    cell: Any,
    *,
    parsed_parts: PythonFormulaParts | None,
    new_code: str,
) -> dict[str, Any]:
    new_formula = build_editor_formula_save(
        parsed_parts=parsed_parts,
        new_code=new_code,
        cell_has_unparsed_python=_cell_has_unparsed_python(cell),
    )
    if isinstance(new_formula, dict):
        return new_formula
    cell.setFormula(new_formula)
    _recalculate_after_save(doc)
    return {"type": "saved", "ok": True, "save_as_plain": False}


def _apply_plain_text_save(doc: Any, cell: Any, *, new_code: str) -> dict[str, Any]:
    cell.setString(new_code)
    _recalculate_after_save(doc)
    return {"type": "saved", "ok": True, "save_as_plain": True}


def _apply_cell_save(
    doc: Any,
    cell: Any,
    *,
    parsed_parts: PythonFormulaParts | None,
    new_code: str,
    save_as_plain: bool,
) -> dict[str, Any]:
    if save_as_plain:
        return _apply_plain_text_save(doc, cell, new_code=new_code)
    return _apply_formula_save(doc, cell, parsed_parts=parsed_parts, new_code=new_code)


def _launch_editor_with_code(
    ctx: Any,
    doc: Any,
    cell: Any,
    *,
    initial_code: str,
    parsed_parts: PythonFormulaParts | None,
    exe: str,
) -> None:
    data_binding = format_data_binding_display(parsed_parts.data_suffix) if parsed_parts else ""

    def on_save(code: str, save_as_plain: bool) -> dict[str, Any]:
        return _apply_cell_save(
            doc,
            cell,
            parsed_parts=parsed_parts,
            new_code=code,
            save_as_plain=save_as_plain,
        )

    def on_closed() -> None:
        log.debug("Python cell editor closed")

    try:
        proc = spawn_editor_process(exe)
    except OSError as e:
        log.exception("Failed to spawn editor")
        msgbox(ctx, "WriterAgent", failure_message(_("Could not start the Python editor."), exc=e))
        return

    session = EditorSession(proc, on_save=on_save, on_closed=on_closed)
    set_active_session(session)
    session.start_reader()

    if not session.wait_for_ready(ctx, timeout_sec=45.0):
        detail = session.read_stderr_tail()
        set_active_session(None)
        msgbox(ctx, "WriterAgent", failure_message(_("The Python editor window did not start."), detail=detail))
        return

    if not session.is_running:
        detail = session.read_stderr_tail()
        set_active_session(None)
        msgbox(ctx, "WriterAgent", failure_message(_("The Python editor exited before it could load your code."), detail=detail))
        return

    load_msg: dict[str, Any] = {
        "type": "load",
        "code": initial_code,
        "title": _("PYTHON cell editor"),
        "plain_text_label": _("Save as plain text"),
    }
    if data_binding:
        load_msg["data_binding"] = data_binding
    try:
        session.send(load_msg)
    except Exception as e:
        log.exception("Failed to send load to editor")
        set_active_session(None)
        msgbox(ctx, "WriterAgent", failure_message(_("Could not talk to the Python editor."), detail=session.read_stderr_tail(), exc=e))
        return


def open_python_cell_editor(ctx: Any) -> None:
    """Launch Monaco editor for the active Calc cell (creates or edits ``=PYTHON()``)."""
    log.info("python_editor: open_python_cell_editor")
    try:
        _open_python_cell_editor_impl(ctx)
    except Exception as e:
        log.exception("python_editor: unhandled failure")
        msgbox(ctx, "WriterAgent", failure_message(_("The Python editor failed unexpectedly."), exc=e))


def _open_python_cell_editor_impl(ctx: Any) -> None:
    existing = get_active_session()
    if existing is not None:
        if existing.is_running:
            msgbox(ctx, "WriterAgent", _("The Python editor is already open."))
            return
        set_active_session(None)

    resolved = _get_active_calc_cell(ctx)
    if resolved is None:
        msgbox(ctx, "WriterAgent", _("Select a cell in a Calc spreadsheet to edit Python."))
        return
    doc, cell, _formula = resolved

    initial_code, parsed_parts, source_formula = _load_cell_editor_code(cell)
    log.info(
        "python_editor: initial_code len=%s parsed=%s source=%r",
        len(initial_code),
        parsed_parts is not None,
        (source_formula or "")[:80],
    )

    if parsed_parts is None and _cell_has_unparsed_python(cell):
        msgbox(
            ctx,
            "WriterAgent",
            _(
                "This PYTHON formula uses a form the editor cannot safely rewrite (e.g. code in another cell). "
                "Edit it in the formula bar, or use =PYTHON(\"code\"; range) with quoted code."
            ),
        )
        return

    exe, err = resolve_editor_python(ctx)
    if not exe:
        msgbox(ctx, "WriterAgent", err or _("No Python interpreter available for the editor."))
        return
    log.info("python_editor: using interpreter %s", exe)

    webview_ok, webview_detail = probe_webview_import(exe)
    log.info("python_editor: webview probe exe=%s ok=%s detail=%r", exe, webview_ok, webview_detail[:200] if webview_detail else "")
    if not webview_ok:
        summary = _(
            "Cannot import webview (pywebview) with the Python from Settings → Python:\n"
            "%(exe)s\n\n"
            "In that venv run: pip install pywebview\n"
            "(import name is webview, package name is pywebview)."
        ) % {"exe": exe}
        msgbox(ctx, "WriterAgent", failure_message(summary, detail=webview_detail or _("unknown error")))
        return

    log.info("python_editor: launching Monaco subprocess")
    _launch_editor_with_code(
        ctx,
        doc,
        cell,
        initial_code=initial_code,
        parsed_parts=parsed_parts,
        exe=exe,
    )
    log.info("python_editor: editor session started")
