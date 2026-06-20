# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run imported Writer notebook code cells against the shared ``notebook:…`` venv kernel."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from plugin.chatbot.dialogs import msgbox
from plugin.doc.document_helpers import is_writer
from plugin.framework.async_stream import run_blocking_in_thread
from plugin.framework.i18n import _
from plugin.framework.uno_context import get_active_document
from plugin.notebook.cell_registry import (
    NotebookCodeCell,
    NotebookDocState,
    cell_id_to_hex,
    find_cell_by_hex,
    load_registry,
    save_registry,
)
from plugin.notebook.writer_importer import (
    _STYLE_NOTEBOOK_IN,
    _STYLE_OUTPUT,
    _append_body_text_block,
    _format_in_prompt,
    _insert_image_in_flow,
    _prepare_display_text,
    _resolve_para_style,
    _strip_ansi,
    flush_ui_idle,
)
from plugin.scripting.payload_codec import host_unpack_data, is_image_payload, find_image_payloads
from plugin.scripting.session_manager import notebook_session_id
from plugin.scripting.venv_worker import run_code_in_user_venv

log = logging.getLogger("writeragent.notebook")

NOTEBOOK_RUN_CELL_URL_PREFIX = "org.extension.writeragent:notebook.run_cell."


@dataclass
class RunResult:
    status: str
    execution_count: int | None
    message: str = ""


def format_run_output_text(result: dict[str, Any]) -> str:
    """Plain-text body for a cell output block (stdout, errors, scalar result)."""
    parts: list[str] = []
    stdout = (result.get("stdout") or "").strip()
    if stdout:
        parts.append(stdout)
    if result.get("status") == "error":
        tb = result.get("traceback") or result.get("message") or "Error"
        parts.append(_strip_ansi(str(tb)))
    elif result.get("status") == "ok":
        wire = result.get("result")
        def is_only_images(obj: Any) -> bool:
            if is_image_payload(obj):
                return True
            if isinstance(obj, list) and obj and all(is_only_images(x) for x in obj):
                return True
            if isinstance(obj, dict) and obj.get("__wa_payload__") == "multi_data":
                items = obj.get("items")
                if isinstance(items, list) and items and all(is_only_images(x) for x in items):
                    return True
            return False
        if wire is not None and not is_only_images(wire):
            try:
                value = host_unpack_data(wire)
            except Exception:
                log.debug("notebook run: host_unpack_data failed", exc_info=True)
                value = wire
            parts.append(repr(value))
    return "\n\n".join(p for p in parts if p.strip())


def read_code_from_field(doc: Any, field_name: str) -> str:
    """Read multiline source from an in-flow form ``TextField`` by control name."""
    from plugin.notebook.form_lookup import find_form_control_model_by_name

    model = find_form_control_model_by_name(doc, field_name)
    if model is not None and hasattr(model, "Text"):
        return str(model.Text or "")
    return ""


def execute_code(ctx: Any, doc: Any, code: str) -> dict[str, Any]:
    """Run *code* in the notebook kernel; always pumps the UI via ``run_blocking_in_thread``."""
    session_id = notebook_session_id(ctx, doc)
    if not session_id:
        return {"status": "error", "message": "Could not resolve notebook Python session."}

    def _run() -> dict[str, Any]:
        return run_code_in_user_venv(ctx, code, session_id=session_id)

    return run_blocking_in_thread(ctx, _run)


def _cursor_after_bookmark(doc: Any, bookmark_name: str) -> Any | None:
    if not bookmark_name or not hasattr(doc, "getBookmarks"):
        return None
    try:
        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(bookmark_name):
            return None
        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()
        text = doc.getText()
        cursor = text.createTextCursorByRange(anchor)
        cursor.collapseToEnd()
        return cursor
    except Exception:
        log.debug("notebook run: bookmark %r not usable", bookmark_name, exc_info=True)
        return None


def _is_next_cell_boundary(para_style: str, content: str, notebook_in_resolved: str | None) -> bool:
    if notebook_in_resolved and para_style == notebook_in_resolved:
        return True
    stripped = (content or "").strip()
    return stripped.startswith("[In [") and ": Code" in stripped


def clear_cell_output(doc: Any, cell: NotebookCodeCell) -> None:
    """Remove body content after the output bookmark through the next cell boundary."""
    start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if start is None:
        return
    text = doc.getText()
    if not start.gotoNextParagraph(False):
        return
    start.gotoStartOfParagraph(False)
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    end = text.createTextCursorByRange(start.getStart())
    end.gotoStartOfParagraph(False)
    while end.gotoNextParagraph(False):
        if _is_next_cell_boundary(end.ParaStyleName, end.getString(), notebook_in):
            end.gotoStartOfParagraph(False)
            break
    else:
        end.gotoEnd(False)
    sel = text.createTextCursor()
    sel.gotoRange(start.getStart(), False)
    sel.gotoRange(end.getStart(), True)
    if not (sel.getString() or "").strip():
        return
    try:
        text.deleteContents(sel, False)
    except Exception:
        log.exception("notebook run: failed to clear output for cell %d", cell.index)


def _insert_run_image(doc: Any, payload: dict[str, Any], *, ctx: Any, images_before: int) -> bool:
    raw = payload.get("data")
    if not isinstance(raw, (bytes, bytearray)):
        return False
    fmt = str(payload.get("format") or "png").lower()
    if fmt == "svg":
        mime = "image/svg+xml"
    elif fmt in ("jpg", "jpeg"):
        mime = "image/jpeg"
    else:
        mime = "image/png"
    return _insert_image_in_flow(doc, raw=bytes(raw), mime=mime, images_before=images_before, ctx=ctx)


def apply_run_result(
    doc: Any,
    cell: NotebookCodeCell,
    result: dict[str, Any],
    *,
    ctx: Any | None = None,
) -> None:
    """Write stdout/errors/result and optional image after the output bookmark."""
    out_text = format_run_output_text(result)
    cursor = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    output_style = _resolve_para_style(doc, _STYLE_OUTPUT)
    if out_text.strip():
        display, _ = _prepare_display_text(out_text)
        if display.strip():
            if cursor is not None:
                text = doc.getText()
                if output_style:
                    try:
                        cursor.setPropertyValue("ParaStyleName", output_style)
                    except Exception:
                        log.debug("notebook run: ParaStyleName %r not applied", output_style)
                text.insertString(cursor, display, False)
                cursor.gotoEnd(False)
            else:
                _append_body_text_block(doc, display, _STYLE_OUTPUT, lead_break=True)
    if result.get("status") == "ok":
        wire = result.get("result")
        images = find_image_payloads(wire)
        for img in images:
            _insert_run_image(doc, img, ctx=ctx, images_before=0)


def update_in_prompt(doc: Any, cell: NotebookCodeCell, execution_count: int | None) -> None:
    """Update the ``[In [n]]`` gutter prefix on the code cell title line."""
    marker = f"Cell {cell.index + 1}: Code"
    new_line = f"{_format_in_prompt(execution_count)}\t{marker}"
    try:
        text = doc.getText()
        enum = text.createEnumeration()
    except Exception:
        log.debug("notebook run: could not enumerate text for in prompt", exc_info=True)
        return
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            content = para.getString()
        except Exception:
            continue
        if marker not in content:
            continue
        try:
            cursor = text.createTextCursorByRange(para.getStart())
            cursor.gotoRange(para.getEnd(), True)
            cursor.setString(new_line)
        except Exception:
            log.exception("notebook run: failed to update in prompt for cell %d", cell.index)
        return


def run_cell(ctx: Any, doc: Any, cell_id: str) -> RunResult:
    """Execute one code cell on the main thread (venv work uses blocking pump)."""
    state = load_registry(doc)
    if state is None:
        return RunResult("error", None, "No notebook registry on document.")
    cell = next((c for c in state.code_cells if c.cell_id == cell_id), None)
    if cell is None:
        return RunResult("error", None, "Unknown notebook cell.")

    code = read_code_from_field(doc, cell.code_field_name)
    if not (code or "").strip():
        return RunResult("error", None, "Code cell is empty.")

    log.info("notebook run cell index=%d field=%s", cell.index, cell.code_field_name)
    result = execute_code(ctx, doc, code)
    execution_count: int | None = None
    if result.get("status") == "ok":
        cell.last_run_status = "ok"
    else:
        cell.last_run_status = "error"

    execution_count = state.next_execution_count
    cell.execution_count = execution_count
    state.next_execution_count = execution_count + 1

    clear_cell_output(doc, cell)
    apply_run_result(doc, cell, result, ctx=ctx)
    update_in_prompt(doc, cell, execution_count)
    save_registry(doc, state)
    flush_ui_idle(ctx)

    if result.get("status") != "ok":
        msg = result.get("message") or _("Cell execution failed.")
        return RunResult("error", execution_count, str(msg))
    return RunResult("ok", execution_count)


def run_cell_for_doc_hex(ctx: Any, doc: Any, hex_id: str) -> None:
    """Run a cell on a known Writer *doc* (button listener or protocol dispatch)."""
    if not is_writer(doc):
        msgbox(ctx, "WriterAgent", _("Notebook run is only supported in LibreOffice Writer."))
        return
    state = load_registry(doc)
    if state is None or not state.code_cells:
        msgbox(
            ctx,
            "WriterAgent",
            _("This document has no imported notebook. Use Tools → Import Jupyter Notebook… first."),
        )
        return
    cell = find_cell_by_hex(state, hex_id)
    if cell is None:
        msgbox(ctx, "WriterAgent", _("Could not find notebook cell for this control."))
        return
    run_result = run_cell(ctx, doc, cell.cell_id)
    if run_result.status == "error" and run_result.message:
        msgbox(ctx, "WriterAgent", run_result.message)


def run_cell_by_hex(ctx: Any, hex_id: str) -> None:
    """Menu / protocol entry: ``notebook.run_cell.{hex}`` on the active Writer document."""
    doc = get_active_document(ctx)
    if doc is None:
        msgbox(ctx, "WriterAgent", _("Open a Writer document first."))
        return
    run_cell_for_doc_hex(ctx, doc, hex_id)


def run_cell_target_url(cell_id: str) -> str:
    """Build the protocol URL for a play button on a code cell."""
    return f"{NOTEBOOK_RUN_CELL_URL_PREFIX}{cell_id_to_hex(cell_id)}"


def init_registry_execution_counter(state: NotebookDocState) -> None:
    """After import, set ``next_execution_count`` above any ipynb execution numbers."""
    max_ec = 0
    for cell in state.code_cells:
        if cell.execution_count is not None:
            max_ec = max(max_ec, int(cell.execution_count))
    state.next_execution_count = max(max_ec + 1, 1)
