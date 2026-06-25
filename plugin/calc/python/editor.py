# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Open the Monaco editor for the active Calc cell's ``=PYTHON()`` formula."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.bridge import CalcBridge
from plugin.calc.python.formula_edit import (
    PythonFormulaParts,
    build_new_python_formula,
    cell_looks_python_like,
    format_data_binding_display,
    parse_data_binding_text,
    parse_python_formula,
    rebuild_python_formula,
    rebuild_python_formula_with_data,
)
from plugin.chatbot.dialogs import msgbox, msgbox_with_report
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_desktop
from plugin.scripting.editor_host import (
    _PERSISTENT_EDITOR,
    get_active_session,
    launch_monaco_editor,
    probe_webview_import,
    resolve_editor_python,
    set_active_session,
)
from plugin.scripting.editor_ipc import failure_message

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
    data_binding_text: str | None = None,
) -> str | dict[str, Any]:
    """Build ``=PYTHON("…")`` for formula-mode save, or an error dict when args cannot be preserved."""
    if data_binding_text is not None:
        data_args = parse_data_binding_text(data_binding_text)
        return rebuild_python_formula_with_data(new_code, data_args, parts=parsed_parts)
    if parsed_parts is not None:
        return rebuild_python_formula(parsed_parts, new_code)
    if cell_has_unparsed_python:
        return {
            "type": "error",
            "message": _(
                "Could not preserve this cell's PYTHON formula arguments (e.g. data ranges). "
                "Edit the formula in Calc, or use a quoted code string like =PY(\"code\"; A1:B10) or =PYTHON(...)."
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
    data_binding_text: str | None = None,
) -> dict[str, Any]:
    new_formula = build_editor_formula_save(
        parsed_parts=parsed_parts,
        new_code=new_code,
        cell_has_unparsed_python=_cell_has_unparsed_python(cell),
        data_binding_text=data_binding_text,
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


def editor_load_save_as_plain(*, parsed_parts: PythonFormulaParts | None, initial_code: str) -> bool:
    """Default plain-text checkbox on editor load: on for plain cells, off for ``=PYTHON()`` or empty."""
    return parsed_parts is None and bool(initial_code.strip())


def _apply_cell_save(
    doc: Any,
    cell: Any,
    *,
    parsed_parts: PythonFormulaParts | None,
    new_code: str,
    save_as_plain: bool,
    data_binding_text: str | None = None,
) -> dict[str, Any]:
    if save_as_plain:
        return _apply_plain_text_save(doc, cell, new_code=new_code)
    return _apply_formula_save(
        doc,
        cell,
        parsed_parts=parsed_parts,
        new_code=new_code,
        data_binding_text=data_binding_text,
    )


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

    def on_save(code: str, save_as_plain: bool, data_binding: str | None = None, _action: str = "cell_save") -> dict[str, Any]:
        binding = None if save_as_plain else data_binding
        return _apply_cell_save(
            doc,
            cell,
            parsed_parts=parsed_parts,
            new_code=code,
            save_as_plain=save_as_plain,
            data_binding_text=binding,
        )

    def on_closed() -> None:
        log.debug("Python cell editor closed")

    load_msg: dict[str, Any] = {
        "type": "load",
        "mode": "calc_cell",
        "language": "python",
        "code": initial_code,
        "title": _("Python cell editor"),
        "plain_text_label": _("Save as plain text"),
        "save_as_plain": editor_load_save_as_plain(parsed_parts=parsed_parts, initial_code=initial_code),
        "save_label": _("Save"),
        "show_plain_text": True,
        "show_data_binding": True,
        "data_binding": data_binding,
    }
    launch_monaco_editor(
        ctx,
        exe=exe,
        load_message=load_msg,
        on_save=on_save,
        on_closed=on_closed,
    )


def open_python_cell_editor(ctx: Any) -> None:
    """Launch Monaco editor for the active Calc cell (creates or edits ``=PYTHON()``)."""
    log.info("python_editor: open_python_cell_editor")
    try:
        from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu

        install_calc_cell_context_menu(ctx)
        _open_python_cell_editor_impl(ctx)
    except Exception as e:
        log.exception("python_editor: unhandled failure")
        msg = failure_message(_("The Python editor failed unexpectedly."), exc=e)
        msgbox_with_report(ctx, "WriterAgent", msg, box_type=3, reportable=True, report_title="Python cell editor failed", report_extra=msg)


def _open_python_cell_editor_impl(ctx: Any) -> None:
    existing = get_active_session()
    if existing is not None and not existing.is_running:
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
                "Edit it in the formula bar, or use =PY(\"code\"; range) (or =PYTHON(...)) with quoted code."
            ),
        )
        return

    exe, err = resolve_editor_python(ctx)
    if not exe:
        msgbox(ctx, "WriterAgent", err or _("No Python interpreter available for the editor."))
        return
    log.info("python_editor: using interpreter %s", exe)

    if _PERSISTENT_EDITOR.is_running:
        log.info("python_editor: Monaco editor process already running, skipping webview probe")
    else:
        webview_ok, webview_detail = probe_webview_import(exe)
        log.info("python_editor: webview probe exe=%s ok=%s detail=%r", exe, webview_ok, webview_detail[:200] if webview_detail else "")
        if not webview_ok:
            summary = _(
                "Cannot import webview (pywebview) with the Python from Settings → Python:\n"
                "%(exe)s\n\n"
                "In that venv run: uv pip install pywebview\n"
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
