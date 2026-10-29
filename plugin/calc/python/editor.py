# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Open the Python cell editor for the active Calc cell's ``=PY()`` formula.

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO calc_cell FRONTEND
# Load payload keys, save modes, and toolbar labels must stay aligned with:
#   - Native Dialog Layout:     extension/Dialogs/PythonCellEditorDialog.xdl
#   - Native Controller:        plugin/calc/python/cell_editor_ui.py
#   - Monaco HTML / Toolbar:    plugin/contrib/scripting/assets/editor/index.html
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor_shell.js
#   - UI Strings Catalog:       plugin/scripting/editor_ui_strings.py (_calc_cell_ui_strings)
# =========================================================================================
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.bridge import CalcBridge
from plugin.calc.python.formula_edit import (
    PythonFormulaParts,
    build_editor_formula_save,
    cell_looks_python_like,
    format_data_binding_display,
    format_data_binding_text,
    parse_data_binding_text,
    parse_python_formula,
    py_formula_has_unquoted_code_ref,
)
from plugin.calc.python.xl_static_rewrite import apply_xl_static_rewrite
from plugin.chatbot.dialogs import msgbox, msgbox_with_report
from plugin.framework.config import get_config
from plugin.framework.errors import best_effort, uno_probe
from plugin.framework.i18n import _
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import get_desktop, product_display_name
from plugin.scripting.editor_host import get_active_session, launch_monaco_editor, monaco_editor_available, set_active_session
from plugin.scripting.editor_errors import failure_message

log = logging.getLogger("writeragent.scripting")


def _cell_formula_content(cell: Any) -> bool | None:
    if not hasattr(cell, "getType"):
        return None
    kind = cell.getType()
    if kind == 3:
        return True
    from plugin.calc.calc_utils import EMPTY, FORMULA, TEXT, VALUE

    if kind == FORMULA:
        return True
    if kind in (EMPTY, VALUE, TEXT):
        return False
    if isinstance(kind, int):
        return False
    return None


def _cell_formula_strings(cell: Any) -> list[str]:
    """Collect formula strings LibreOffice may expose for the cell.

    Bugfix: Text cells containing PY(...) were incorrectly treated as PY formulas
    because ScCellObj::getFormula (cellsuno.cxx lcl_GetInputString) returns the
    literal input text for non-formula cells, which normalize_formula_string then
    prepended '=' to. We now check getType() to return [] for non-formula cells, and
    for untyped proxies/mocks, drop strings whose lstrip() does not start with '=' or '{'.
    """
    content = _cell_formula_content(cell)
    if content is False:
        return []
    out: list[str] = []
    f = cell.getFormula()
    if f:
        out.append(str(f))
    for prop in ("FormulaLocal", "Formula"):
        val = uno_probe(lambda: cell.getPropertyValue(prop), None)
        if val and str(val) not in out:
            out.append(str(val))
    if content is None:
        out = [s for s in out if s.lstrip().startswith(("=", "{"))]
    return out


def _parse_cell_python_formula(cell: Any, formulas: list[str] | None = None) -> tuple[str, PythonFormulaParts | None, str | None]:
    """Return (code, parts, source formula string that parsed) from the cell."""
    f_list = formulas if formulas is not None else _cell_formula_strings(cell)
    for raw in f_list:
        parts = parse_python_formula(raw)
        if parts is not None:
            return parts.code, parts, raw
    return "", None, None


def _same_calc_cell(left: Any, right: Any) -> bool:
    if left is right:
        return True
    a = left.getCellAddress()
    b = right.getCellAddress()
    return int(a.Column) == int(b.Column) and int(a.Row) == int(b.Row) and int(a.Sheet) == int(b.Sheet)


def _resolve_code_ref_cell(doc: Any, code_ref: str) -> Any | None:
    """Resolve ``$A$1`` / ``Sheet.A1`` to a cell, or None if the address is bad."""
    try:
        from plugin.calc.address_utils import qualify_a1, split_sheet_prefix

        sheet, rest = split_sheet_prefix(code_ref.strip())
        bare = rest.replace("$", "").strip()
        if not bare:
            return None
        addr = qualify_a1(sheet, bare) if sheet else bare
        return CalcBridge(doc).get_cell_by_address(addr)
    except Exception:
        log.debug("python_editor: could not resolve code ref %r", code_ref, exc_info=True)
        return None


def _load_cell_editor_code(cell: Any, formulas: list[str] | None = None) -> tuple[str, PythonFormulaParts | None, str | None]:
    """Return Monaco source: stripped PYTHON code or plain cell text."""
    code, parts, source = _parse_cell_python_formula(cell, formulas=formulas)
    if parts is not None:
        return code, parts, source
    if _cell_has_unparsed_python(cell, formulas=formulas):
        return "", None, None
    plain = cell.getString()
    if plain:
        return str(plain), None, None
    return "", None, None


def _cell_has_unparsed_python(cell: Any, formulas: list[str] | None = None) -> bool:
    """True when the cell looks like PYTHON but strict parse failed (data binding at risk)."""
    f_list = formulas if formulas is not None else _cell_formula_strings(cell)
    for raw in f_list:
        if cell_looks_python_like(raw) and parse_python_formula(raw) is None:
            return True
    return False


def format_cell_a1(cell: Any) -> str:
    """Active-sheet A1 label (``A1``, ``AA100``)."""
    addr = cell.getCellAddress()
    from plugin.calc.address_utils import format_address

    return format_address(int(addr.Column), int(addr.Row))


@main_thread_only
def confirm_unsaved_cell_edit(ctx: Any, cell_addr: str) -> str:
    """Ask Save / Don't save / Cancel. Returns ``save``, ``discard``, or ``cancel``."""
    from plugin.scripting.editor_host import ask_save_discard_cancel

    title = product_display_name(ctx)
    where = cell_addr or _("this cell")
    message = _("Save changes to {0}?\n\nYes saves. No discards and opens the new cell. Cancel keeps editing {0}.").format(where)
    return ask_save_discard_cancel(ctx, title=title, message=message, log_label="confirm_unsaved_cell_edit")


@main_thread_only
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
    from plugin.framework.thread_guard import guard_uno

    model = guard_uno(model)
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
    # Selection sheet and the active sheet can differ. Falling back to
    # get_active_sheet() when getByIndex failed read a different sheet at
    # the same column and row. Refuse; the caller already explains that
    # no cell was selected.
    sheet_idx = int(addr.Sheet)
    sheets = model.getSheets()
    if sheets is None or not (0 <= sheet_idx < int(sheets.getCount())):
        log.warning("python_editor: selection sheet index %s out of range", sheet_idx)
        return None
    sheet = uno_probe(lambda: sheets.getByIndex(sheet_idx), None)
    if sheet is None:
        log.warning("python_editor: selection sheet missing")
        return None
    cell = bridge.get_cell(sheet, addr.StartColumn, addr.StartRow)
    formulas = _cell_formula_strings(cell)
    formula = formulas[0] if formulas else ""
    log.info("python_editor: cell (%s,%s) formulas=%r", addr.StartColumn, addr.StartRow, formulas)
    return model, cell, formula


def _existing_data_args_for_xl_rewrite(data_binding_text: str | None, parsed_parts: PythonFormulaParts | None) -> list[str]:
    """Resolve current data args from the Monaco Data field or the parsed formula."""
    if data_binding_text is not None:
        return parse_data_binding_text(data_binding_text)
    if parsed_parts is not None:
        return parse_data_binding_text(format_data_binding_display(parsed_parts.data_suffix))
    return []


def _maybe_apply_xl_static_rewrite(new_code: str, data_binding_text: str | None, *, parsed_parts: PythonFormulaParts | None = None) -> tuple[str, str | None] | dict[str, Any]:
    """When ``scripting.xl_static_rewrite`` is on, lift ``xl("A1")`` into data args.

    Returns ``(code, data_binding_text)`` or an error dict for the Monaco save path.
    """
    enabled = best_effort(lambda: bool(get_config("scripting.xl_static_rewrite")), "xl_static_rewrite config read", False)
    if not enabled:
        return new_code, data_binding_text
    existing = _existing_data_args_for_xl_rewrite(data_binding_text, parsed_parts)
    result = apply_xl_static_rewrite(new_code, existing)
    if result.issues:
        detail = "; ".join(result.issues[:5])
        return {"type": "error", "message": _("Could not rewrite xl() range literals into =PY data arguments: %s") % detail}
    if not result.changed:
        return new_code, data_binding_text
    return result.code, format_data_binding_text(result.data_args)


def _apply_formula_save(doc: Any, cell: Any, *, parsed_parts: PythonFormulaParts | None, new_code: str, data_binding_text: str | None = None) -> dict[str, Any]:
    rewritten = _maybe_apply_xl_static_rewrite(new_code, data_binding_text, parsed_parts=parsed_parts)
    if isinstance(rewritten, dict):
        return rewritten
    new_code, data_binding_text = rewritten
    new_formula = build_editor_formula_save(
        parsed_parts=parsed_parts,
        new_code=new_code,
        cell_has_unparsed_python=data_binding_text is None and _cell_has_unparsed_python(cell),
        data_binding_text=data_binding_text,
    )
    if isinstance(new_formula, dict):
        return new_formula
    cell.setFormula(new_formula)
    return {"type": "saved", "ok": True, "save_as_plain": False}


def _apply_plain_text_save(doc: Any, cell: Any, *, new_code: str) -> dict[str, Any]:
    cell.setString(new_code)
    return {"type": "saved", "ok": True, "save_as_plain": True, "status_ok_text": _("Saved without =PY().")}


def _apply_followed_ref_save(doc: Any, formula_cell: Any, *, code_cell: Any, code_ref: str, new_code: str, data_binding_text: str | None, parsed_parts: PythonFormulaParts | None = None) -> dict[str, Any]:
    """Write Python to the referenced code cell; keep ``=PY($A$1; …)`` as a ref."""
    from plugin.calc.python.formula_edit import CALC_PYTHON_FN, build_data_suffix

    code_cell.setString(new_code)
    if data_binding_text is not None:
        data_args = parse_data_binding_text(data_binding_text)
        old_args = _existing_data_args_for_xl_rewrite(None, parsed_parts)
        # Leave the formula cell alone when only the code changed. Native follow
        # save always sends Data: '' — rewriting =PY($A$1) used to emit =PY($A$1))
        # (Err:508) and is unnecessary when the ranges did not change.
        if data_args != old_args:
            # Keep the original ref token ($A$1). format_py_data_range strips $
            # which would make an absolute code ref relative after save.
            # build_data_suffix already includes the closing ')'.
            # Bugfix: hardcoding PY rewrote =PYTHON( / =py( / add-in prefixes
            # on a data change; rebuild_python_formula already keeps parts.prefix;
            # this branch did not.
            prefix = parsed_parts.prefix if parsed_parts is not None and parsed_parts.prefix else f"={CALC_PYTHON_FN}("
            formula_cell.setFormula(f"{prefix}{code_ref.strip()}{build_data_suffix(data_args)}")
    return {"type": "saved", "ok": True, "save_as_plain": False, "status_ok_text": _("Saved code in {0}.").format(code_ref)}


def editor_load_save_as_plain(*, parsed_parts: PythonFormulaParts | None, initial_code: str, follow_code_ref: bool = False) -> bool:
    """Default plain-text checkbox on editor load: on for plain cells, off for ``=PY()`` or empty."""
    if follow_code_ref:
        return True
    return parsed_parts is None and bool(initial_code.strip())


def _apply_cell_save(doc: Any, cell: Any, *, parsed_parts: PythonFormulaParts | None, new_code: str, save_as_plain: bool, data_binding_text: str | None = None, code_cell: Any | None = None, code_ref: str | None = None) -> dict[str, Any]:
    # Bugfix: Follow saves rewrite both code and formula cells (plus geometric
    # recalc) without grouping, so undo only reverted the last write. Wrapping
    # in WriterCompoundUndo groups all writes into a single undo step.
    from plugin.doc.compound_undo import WriterCompoundUndo

    with WriterCompoundUndo(doc, _("WriterAgent: Save Python cell")):
        if code_cell is not None and code_ref and not _same_calc_cell(code_cell, cell):
            result = _apply_followed_ref_save(doc, cell, code_cell=code_cell, code_ref=code_ref, new_code=new_code, data_binding_text=data_binding_text, parsed_parts=parsed_parts)
        elif save_as_plain:
            result = _apply_plain_text_save(doc, cell, new_code=new_code)
        else:
            result = _apply_formula_save(doc, cell, parsed_parts=parsed_parts, new_code=new_code, data_binding_text=data_binding_text)
        if result.get("type") == "saved":
            # Bugfix: Plain text saves previously skipped after_py_cell_save, leaving
            # stale geometric registry records for downgraded =PY() cells. Reconciling
            # here prunes them.
            from plugin.calc.python.geometric_recalc import after_py_cell_save

            after_py_cell_save(doc, cell)
            best_effort(doc.calculateAll, "calculateAll after editor save")
        return result


def _launch_editor_with_code(ctx: Any, doc: Any, cell: Any, *, initial_code: str, parsed_parts: PythonFormulaParts | None, exe: str, code_cell: Any | None = None, code_ref: str | None = None) -> None:
    follow = bool(code_ref and code_cell is not None and not _same_calc_cell(code_cell, cell))
    data_binding = format_data_binding_display(parsed_parts.data_suffix) if parsed_parts else ""
    display_cell = code_cell if follow else cell

    def on_save(code: str, save_as_plain: bool, data_binding: str | None = None, _action: str = "cell_save") -> dict[str, Any]:
        if follow:
            return _apply_cell_save(doc, cell, parsed_parts=parsed_parts, new_code=code, save_as_plain=True, data_binding_text=data_binding, code_cell=code_cell, code_ref=code_ref)
        binding = None if save_as_plain else data_binding
        return _apply_cell_save(doc, cell, parsed_parts=parsed_parts, new_code=code, save_as_plain=save_as_plain, data_binding_text=binding)

    def on_closed() -> None:
        log.debug("Python cell editor closed")

    label = format_cell_a1(display_cell)
    from plugin.scripting.document_scripts import document_scripts_identity

    doc_url = best_effort(lambda: document_scripts_identity(doc), "doc_url for session target", "")
    load_msg: dict[str, Any] = {
        "type": "load",
        "mode": "calc_cell",
        "language": "python",
        "code": initial_code,
        "title": _("Python cell editor"),
        "plain_text_label": _("Save without =PY()"),
        "save_as_plain": editor_load_save_as_plain(parsed_parts=parsed_parts, initial_code=initial_code, follow_code_ref=follow),
        "save_label": _("Save"),
        "show_plain_text": True,
        "show_data_binding": True,
        "follow_code_ref": follow,
        "data_binding": data_binding,
        "cell_address": label,
        "doc_url": doc_url,
        "resource": label,
    }
    from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
    from plugin.scripting.editor_errors import editor_error_markers

    entry = best_effort(lambda: get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), initial_code), "diagnostics lookup for editor markers", None)
    if entry is not None and entry.is_error:
        found = editor_error_markers(entry.message, entry.traceback, initial_code)
        if found:
            load_msg["markers"] = found
    # Unsaved calc-cell, Run Script, init-script, and LaTeX buffers are
    # confirmed or flushed inside launch_monaco_editor before this load
    # replaces the window. A calc-only check here dropped the other modes.
    launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save, on_closed=on_closed)


def open_python_cell_editor(ctx: Any) -> None:
    """Launch Monaco or the native cell editor for the active Calc cell."""
    log.info("python_editor: open_python_cell_editor")
    try:
        from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu

        install_calc_cell_context_menu(ctx)
        _open_python_cell_editor_impl(ctx)
    except Exception as e:
        log.exception("python_editor: unhandled failure")
        msg = failure_message(_("The Python editor failed unexpectedly."), exc=e)
        msgbox_with_report(ctx, product_display_name(ctx), msg, box_type=3, reportable=True, report_title="Python cell editor failed", report_extra=msg)


def _open_python_cell_editor_impl(ctx: Any) -> None:
    existing = get_active_session()
    if existing is not None and not existing.is_running:
        set_active_session(None)

    resolved = _get_active_calc_cell(ctx)
    if resolved is None:
        msgbox(ctx, product_display_name(ctx), _("Select a cell in a Calc spreadsheet to edit Python."))
        return
    doc, cell, _formula = resolved

    # Follow =PY($A$1) into A1 (plain code cell). Do not quote the ref on save.
    # Prompting the LLM to emit this two-cell pattern is deferred: auto-imports
    # and helpers usually fit in Calc MAXSTRLEN (1024). Revisit CALC_FORMULA_SYNTAX
    # if Err:513 shows up for generated =PY("…").
    formulas = _cell_formula_strings(cell)
    initial_code, parsed_parts, source_formula = _load_cell_editor_code(cell, formulas=formulas)
    code_cell: Any | None = None
    code_ref: str | None = None
    if parsed_parts is not None and source_formula and py_formula_has_unquoted_code_ref(source_formula):
        followed = _resolve_code_ref_cell(doc, parsed_parts.code)
        if followed is None:
            msgbox(ctx, product_display_name(ctx), _("Could not open the code cell {0}.").format(parsed_parts.code))
            return
        if not _same_calc_cell(followed, cell):
            initial_code = str(followed.getString() or "")
            code_cell = followed
            code_ref = parsed_parts.code

    log.info("python_editor: initial_code len=%s parsed=%s source=%r follow=%s", len(initial_code), parsed_parts is not None, (source_formula or "")[:80], code_ref)

    if parsed_parts is None and _cell_has_unparsed_python(cell, formulas=formulas):
        msgbox(ctx, product_display_name(ctx), _('This PY formula uses a form the editor cannot safely rewrite (e.g. code in another cell). Edit it in the formula bar, or use =PY("code"; range) with quoted code.'))
        return

    exe, monaco_available = monaco_editor_available(ctx)
    if monaco_available:
        assert exe is not None
        log.info("python_editor: using interpreter %s", exe)
        log.info("python_editor: launching editor subprocess")
        _launch_editor_with_code(ctx, doc, cell, initial_code=initial_code, parsed_parts=parsed_parts, exe=exe, code_cell=code_cell, code_ref=code_ref)
        log.info("python_editor: editor session started")
        return

    from plugin.calc.python.cell_editor_ui import show_native_python_cell_editor

    log.info("python_editor: editor unavailable; opening native cell editor")
    opened, detail = show_native_python_cell_editor(ctx, doc=doc, cell=cell, initial_code=initial_code, parsed_parts=parsed_parts, code_cell=code_cell, code_ref=code_ref)
    if opened:
        return
    msg = detail or _("Could not open the built-in Python cell editor.")
    msgbox(ctx, product_display_name(ctx), msg)
