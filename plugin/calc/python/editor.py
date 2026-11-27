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

from dataclasses import replace
import logging
from typing import Any, TypeGuard

from plugin.calc.address_utils import format_address, qualify_a1, split_sheet_prefix
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_utils import FORMULA
from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu
from plugin.calc.python.formula_edit import (
    CALC_PYTHON_FN,
    PyFormula,
    build_data_suffix,
    build_editor_formula_save,
    format_data_binding_display,
    format_data_binding_text,
    parse_data_binding_text,
)
from plugin.calc.python.geometric_recalc import after_py_cell_save
from plugin.calc.python.xl_static_rewrite import apply_xl_static_rewrite
from plugin.chatbot.dialogs import msgbox, msgbox_with_report
from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.framework.config import get_config
from plugin.framework.doc_type import is_calc
from plugin.framework.errors import best_effort, uno_read
from plugin.framework.i18n import _
from plugin.framework.thread_guard import guard_uno, main_thread_only
from plugin.framework.uno_context import get_desktop, product_display_name
from plugin.scripting.document_scripts import document_scripts_identity
from plugin.scripting.editor_host import (
    ask_save_discard_cancel,
    launch_monaco_editor,
    monaco_editor_available,
)
from plugin.scripting.editor_errors import editor_error_markers, failure_message

log = logging.getLogger(__name__)


def _cell_formula_strings(cell: Any) -> list[str]:
    """Collect formula strings LibreOffice exposes for a formula cell.

    A TEXT cell's getFormula() returns its text, so a PY-looking literal must not open as a formula.
    """
    if cell.getType() != FORMULA:
        return []
    out: list[str] = []
    f = cell.getFormula()
    if f:
        out.append(f)
    local = uno_read(lambda: cell.getPropertyValue("FormulaLocal"), None)
    if local and local not in out:
        out.append(local)
    return out


def _same_calc_cell(left: Any, right: Any) -> bool:
    if left is right:
        return True
    a = left.getCellAddress()
    b = right.getCellAddress()
    return int(a.Column) == int(b.Column) and int(a.Row) == int(b.Row) and int(a.Sheet) == int(b.Sheet)


def is_follow(code_ref: str | None, code_cell: Any, cell: Any) -> TypeGuard[str]:
    """True when saving into a separate referenced cell (``=PY($A$1)`` in B1).

    A self-ref (``=PY(A1)`` in A1) is not a follow, because writing it as one
    would overwrite the cell's own formula with the code text. The ``TypeGuard``
    lets callers pass ``code_ref`` on as ``str``.
    """
    return bool(code_ref) and code_cell is not None and not _same_calc_cell(code_cell, cell)


def _resolve_code_ref_cell(doc: Any, code_ref: str) -> Any | None:
    """Resolve ``$A$1`` / ``Sheet.A1`` to a cell, or None if the address is bad."""
    try:
        sheet, rest = split_sheet_prefix(code_ref.strip())
        bare = rest.replace("$", "").strip()
        if not bare:
            return None
        addr = qualify_a1(sheet, bare) if sheet else bare
        return CalcBridge(doc).get_cell_by_address(addr)
    except Exception:
        log.debug("python_editor: could not resolve code ref %r", code_ref, exc_info=True)
        return None


def _load_cell_editor_code(cell: Any, formulas: list[str]) -> tuple[str, PyFormula | None, str | None]:
    """Return Monaco source: stripped PYTHON code or plain cell text."""
    for raw in formulas:
        parts = PyFormula.parse(raw)
        if parts is not None:
            return parts.code, parts, raw
    return cell.getString(), None, None


def format_cell_a1(cell: Any) -> str:
    """Active-sheet A1 label (``A1``, ``AA100``)."""
    addr = cell.getCellAddress()
    return format_address(int(addr.Column), int(addr.Row))


@main_thread_only
def confirm_unsaved_cell_edit(ctx: Any, cell_addr: str) -> str:
    """Ask Save / Don't save / Cancel. Returns ``save``, ``discard``, or ``cancel``."""
    title = product_display_name(ctx)
    where = cell_addr or _("this cell")
    message = _("Save changes to {0}?\n\nYes saves. No discards and opens the new cell. Cancel keeps editing {0}.").format(where)
    return ask_save_discard_cancel(ctx, title=title, message=message, log_label="confirm_unsaved_cell_edit")


@main_thread_only
def _get_active_calc_cell(ctx: Any) -> tuple[Any, Any] | None:
    """Return (doc, cell) for the current selection, or None."""
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
    model = guard_uno(controller.getModel())
    if not is_calc(model):
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
    # The selection's sheet can differ from the active sheet. Do not fall back
    # to get_active_sheet(): it would read the same column and row on the wrong
    # sheet. Refuse; the caller already explains that no cell was selected.
    sheet_idx = int(addr.Sheet)
    sheets = model.getSheets()
    sheet = uno_read(lambda: sheets.getByIndex(sheet_idx), None)
    if sheet is None:
        log.warning("python_editor: selection sheet missing")
        return None
    cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
    return model, cell


def data_binding_display(parts: PyFormula | None) -> str:
    return "" if parts is None else format_data_binding_display(parts.data_suffix)


def _existing_data_args(parsed_parts: PyFormula | None) -> list[str]:
    return [] if parsed_parts is None else parsed_parts.data_args()


def _maybe_apply_xl_static_rewrite(new_code: str, data_binding_text: str | None, *, parsed_parts: PyFormula | None = None) -> tuple[str, str | None] | dict[str, Any]:
    """When ``scripting.xl_static_rewrite`` is on, lift ``xl("A1")`` into data args.

    Returns ``(code, data_binding_text)`` or an error dict that every editor shows as the save status.
    """
    enabled = best_effort(lambda: bool(get_config("scripting.xl_static_rewrite")), "xl_static_rewrite config read", False)
    if not enabled:
        return new_code, data_binding_text
    if data_binding_text is not None:
        existing = parse_data_binding_text(data_binding_text)
    else:
        existing = _existing_data_args(parsed_parts)
    result = apply_xl_static_rewrite(new_code, existing)
    if result.issues:
        detail = "; ".join(result.issues[:5])
        return {"type": "error", "message": _("Could not rewrite xl() range literals into =PY data arguments: %s") % detail}
    if not result.changed:
        return new_code, data_binding_text
    return result.code, format_data_binding_text(result.data_args)


def _apply_formula_save(cell: Any, *, parsed_parts: PyFormula | None, new_code: str, data_binding_text: str | None = None) -> dict[str, Any]:
    new_formula = build_editor_formula_save(
        parsed_parts=parsed_parts,
        new_code=new_code,
        data_binding_text=data_binding_text,
    )
    cell.setFormula(new_formula)
    return {"type": "saved", "ok": True, "save_as_plain": False}


def _apply_followed_ref_save(formula_cell: Any, *, code_cell: Any, code_ref: str, new_code: str, data_binding_text: str | None, parsed_parts: PyFormula | None = None) -> dict[str, Any]:
    """Write Python to the referenced code cell; keep ``=PY($A$1; …)`` as a ref."""
    code_cell.setString(new_code)
    if data_binding_text is not None:
        data_args = parse_data_binding_text(data_binding_text)
        old_args = _existing_data_args(parsed_parts)
        # Leave the formula cell alone when only the code changed: save always
        # sends Data, and rewriting =PY($A$1) when the ranges are unchanged gives Err:508.
        if data_args != old_args:
            # Keep the original ref token ($A$1). format_py_data_range strips $
            # which would make an absolute code ref relative after save.
            # build_data_suffix already includes the closing ')'.
            # Keep parsed_parts.prefix (=PYTHON(, add-in names, etc.) instead of hardcoding =PY(.
            prefix = parsed_parts.prefix if parsed_parts is not None and parsed_parts.prefix else f"={CALC_PYTHON_FN}("
            formula_cell.setFormula(f"{prefix}{code_ref.strip()}{build_data_suffix(data_args)}")
    return {"type": "saved", "ok": True, "save_as_plain": False, "status_ok_text": _("Saved code in {0}.").format(code_ref)}


def editor_load_save_as_plain(*, parsed_parts: PyFormula | None, initial_code: str, follow_code_ref: bool = False) -> bool:
    """Default plain-text checkbox on editor load: on for plain cells, off for ``=PY()`` or empty."""
    if follow_code_ref:
        return True
    return parsed_parts is None and bool(initial_code.strip())


def apply_cell_save(doc: Any, cell: Any, *, parsed_parts: PyFormula | None, new_code: str, save_as_plain: bool, data_binding_text: str | None = None, code_cell: Any | None = None, code_ref: str | None = None) -> dict[str, Any]:
    # The xl("A1") sugar applies to every save that ends in a =PY() formula: a
    # formula-mode save, and a follow save (code text in the referenced cell,
    # ranges on the =PY($A$1; …) formula cell, where follow ignores the plain box).
    # Monaco, CodeMirror and the built-in dialog all save through here. A plain
    # "Save without =PY()" has no formula to carry the ranges, so it keeps the text.
    refreshed: dict[str, Any] = {}
    if is_follow(code_ref, code_cell, cell) or not save_as_plain:
        typed_code = new_code
        rewritten = _maybe_apply_xl_static_rewrite(new_code, data_binding_text, parsed_parts=parsed_parts)
        if isinstance(rewritten, dict):
            return rewritten
        new_code, data_binding_text = rewritten
        if new_code != typed_code:
            # Every editor replaces its buffer and Data field with these so the next save starts from what is in the cell.
            refreshed = {"code": new_code, "data_binding": data_binding_text or ""}
    # Follow saves touch code and formula cells plus geometric recalc; compound undo groups one undo step.
    with WriterCompoundUndo(doc, _("WriterAgent: Save Python cell")):
        if is_follow(code_ref, code_cell, cell):
            result = _apply_followed_ref_save(cell, code_cell=code_cell, code_ref=code_ref, new_code=new_code, data_binding_text=data_binding_text, parsed_parts=parsed_parts)
        elif save_as_plain:
            cell.setString(new_code)
            result = {"type": "saved", "ok": True, "save_as_plain": True, "status_ok_text": _("Saved without =PY().")}
        else:
            result = _apply_formula_save(cell, parsed_parts=parsed_parts, new_code=new_code, data_binding_text=data_binding_text)
        result.update(refreshed)
        # Plain-text downgrades must run after_py_cell_save to prune stale geometric registry rows.
        after_py_cell_save(doc, cell)
        best_effort(doc.calculateAll, "calculateAll after editor save")
        return result


def parts_after_save(parsed_parts: PyFormula | None, outcome: dict[str, Any], data_binding_text: str | None) -> PyFormula | None:
    """Formula parts whose ranges are the Data text just saved; the next follow save compares with these.

    The Data text is kept as typed (with $). Reading the cell back would pick up shifted or appended refs.
    """
    saved = outcome.get("data_binding", data_binding_text)
    if parsed_parts is None or outcome.get("type") != "saved" or saved is None:
        return parsed_parts
    args = parse_data_binding_text(saved)
    return replace(parsed_parts, data_suffix=f";{format_data_binding_text(args)})" if args else ")")


def _launch_editor_with_code(ctx: Any, doc: Any, cell: Any, *, initial_code: str, parsed_parts: PyFormula | None, exe: str, code_cell: Any | None = None, code_ref: str | None = None) -> None:
    follow = is_follow(code_ref, code_cell, cell)
    data_binding = data_binding_display(parsed_parts)
    display_cell = code_cell if follow else cell

    def on_save(code: str, save_as_plain: bool, data_binding: str | None = None, _action: str = "cell_save") -> dict[str, Any]:
        nonlocal parsed_parts
        outcome = apply_cell_save(
            doc,
            cell,
            parsed_parts=parsed_parts,
            new_code=code,
            save_as_plain=save_as_plain,
            data_binding_text=data_binding,
            code_cell=code_cell if follow else None,
            code_ref=code_ref if follow else None,
        )
        parsed_parts = parts_after_save(parsed_parts, outcome, data_binding)
        return outcome

    label = format_cell_a1(display_cell)
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
    entry = best_effort(lambda: get_diagnostics_store().latest_for_code(diagnostics_workbook_key(doc), initial_code), "diagnostics lookup for editor markers", None)
    if entry is not None and entry.is_error:
        found = editor_error_markers(entry.message, entry.traceback, initial_code)
        if found:
            load_msg["markers"] = found
    # launch_monaco_editor confirms or flushes every unsaved buffer (calc cell, Run Script, init script, LaTeX) before this load replaces the window.
    launch_monaco_editor(ctx, exe=exe, load_message=load_msg, on_save=on_save)


def open_python_cell_editor(ctx: Any) -> None:
    """Launch Monaco or the native cell editor for the active Calc cell."""
    log.info("python_editor: open_python_cell_editor")
    try:
        install_calc_cell_context_menu(ctx)
        _open_python_cell_editor_impl(ctx)
    except Exception as e:
        log.exception("python_editor: unhandled failure")
        msg = failure_message(_("The Python editor failed unexpectedly."), exc=e)
        msgbox_with_report(ctx, product_display_name(ctx), msg, reportable=True, report_title="Python cell editor failed", report_extra=msg)


def _open_python_cell_editor_impl(ctx: Any) -> None:
    resolved = _get_active_calc_cell(ctx)
    if resolved is None:
        msgbox(ctx, product_display_name(ctx), _("Select a cell in a Calc spreadsheet to edit Python."))
        return
    doc, cell = resolved

    formulas = _cell_formula_strings(cell)
    initial_code, parsed_parts, source_formula = _load_cell_editor_code(cell, formulas=formulas)
    code_cell: Any | None = None
    code_ref: str | None = None
    # Follow =PY($A$1) into A1 (plain code cell). Do not quote the ref on save.
    if parsed_parts is not None and parsed_parts.code_is_ref:
        followed = _resolve_code_ref_cell(doc, parsed_parts.code)
        if followed is None:
            msgbox(ctx, product_display_name(ctx), _("Could not open the code cell {0}.").format(parsed_parts.code))
            return
        if not _same_calc_cell(followed, cell):
            initial_code = followed.getString()
            code_cell = followed
            code_ref = parsed_parts.code

    log.info("python_editor: initial_code len=%s parsed=%s source=%r follow=%s", len(initial_code), parsed_parts is not None, (source_formula or "")[:80], code_ref)

    exe, monaco_available = monaco_editor_available()
    if not monaco_available or exe is None:
        # Lazy: cell_editor_ui imports this module at its top.
        from plugin.calc.python.cell_editor_ui import show_native_python_cell_editor

        log.info("python_editor: editor unavailable; opening native cell editor")
        opened, detail = show_native_python_cell_editor(ctx, doc=doc, cell=cell, initial_code=initial_code, parsed_parts=parsed_parts, code_cell=code_cell, code_ref=code_ref)
        if opened:
            return
        msg = detail or _("Could not open the built-in Python cell editor.")
        msgbox(ctx, product_display_name(ctx), msg)
        return

    _launch_editor_with_code(ctx, doc, cell, initial_code=initial_code, parsed_parts=parsed_parts, exe=exe, code_cell=code_cell, code_ref=code_ref)
