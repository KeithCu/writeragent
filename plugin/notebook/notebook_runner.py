# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run imported Writer notebook code cells against the shared ``notebook:…`` venv kernel."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from plugin.chatbot.dialogs import msgbox
from plugin.doc.doc_type import is_writer
from plugin.framework.async_stream import run_blocking_in_thread
from plugin.framework.i18n import _
from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
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
    _PARAGRAPH_BREAK,
    _STYLE_CELL_HEADING,
    _STYLE_NOTEBOOK_IN,
    _STYLE_OUTPUT,
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

NOTEBOOK_RUN_CELL_URL_PREFIX = f"{EXTENSION_ID_WRITERAGENT}:notebook.run_cell."


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


def _plain_text(value: Any) -> str:
    """UNO ``getString()`` is a str; MagicMock probes must not look non-empty."""
    return value if isinstance(value, str) else ""


def _paragraph_string(cursor: Any) -> str:
    """Text of the paragraph containing *cursor*.

    Writer ``XTextCursor.getString()`` is the **selection**, so a collapsed
    cursor (the kind ``gotoNextParagraph`` leaves) returns ``""``. The markdown
    chrome check then never matched ``Cell N: Markdown``, and ``clear_cell_output``
    walked until the next code gutter — deleting the markdown cells between
    code cells on the small NumPy fixture. Expand to the enclosing paragraph
    when the selection is empty. Mocks that already return paragraph text from
    ``getString()`` keep working.
    """
    selected = ""
    try:
        selected = _plain_text(cursor.getString() or "")
    except Exception:
        selected = ""
    if selected.strip():
        return selected
    try:
        probe = cursor.getText().createTextCursorByRange(cursor)
        probe.gotoStartOfParagraph(False)
        probe.gotoEndOfParagraph(True)
        return _plain_text(probe.getString() or "")
    except Exception:
        return selected


def _paragraph_is_empty(cursor: Any) -> bool:
    return not _paragraph_string(cursor).strip()


def _para_style_name(cursor: Any) -> str:
    try:
        return str(cursor.ParaStyleName or "")
    except Exception:
        return ""


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


def _bookmark_exists(doc: Any, bookmark_name: str) -> bool:
    if not bookmark_name or not hasattr(doc, "getBookmarks"):
        return False
    try:
        return bool(doc.getBookmarks().hasByName(bookmark_name))
    except Exception:
        return False


_ENUM_SAFETY_CAP = 10000


def _find_cell_output_heading_end(doc: Any, cell: NotebookCodeCell) -> Any | None:
    """Cursor at the end of this cell's Heading 4 ``Output`` paragraph, or None."""
    marker = f"Cell {cell.index + 1}: Code"
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    try:
        text = doc.getText()
        enum = text.createEnumeration()
    except Exception:
        return None
    seen_code = False
    first_output_end: Any | None = None
    matched_output_end: Any | None = None
    steps = 0
    while steps < _ENUM_SAFETY_CAP:
        more = enum.hasMoreElements()
        # PyUNO may return 1/0; MagicMock is neither True nor int 1 — stop.
        if more is not True and more != 1:
            break
        steps += 1
        para = enum.nextElement()
        try:
            if hasattr(para, "supportsService") and not para.supportsService("com.sun.star.text.Paragraph"):
                continue
            content = _plain_text(para.getString() or "")
            style = str(para.getPropertyValue("ParaStyleName") or "")
        except Exception:
            continue
        if marker in content:
            seen_code = True
            continue
        if content.strip() == "Output":
            try:
                # After the last character of "Output", still in this paragraph.
                # para.getEnd() / a mid-heading goRight(1) are wrong: the former
                # is the break (setString of the next range deletes nb_out_*),
                # the latter splits the heading when we insert PARAGRAPH_BREAK
                # ("O" / stdout / "utput").
                cursor = text.createTextCursorByRange(para.getStart())
                n = min(len("Output"), 32767)
                if not cursor.goRight(n, False):
                    cursor.gotoEndOfParagraph(False)
            except Exception:
                continue
            if first_output_end is None:
                first_output_end = cursor
            if seen_code:
                matched_output_end = cursor
            continue
        if _is_next_cell_boundary(style, content, notebook_in):
            if seen_code:
                return matched_output_end
            # Earlier chrome (``Cell 1: Markdown`` before this code cell) is not
            # this cell's Output. Keep scanning. Synthetic docs with no
            # ``Cell N: Code`` fall through to first_output_end at EOF.
            continue
    return matched_output_end if seen_code else first_output_end


def _reanchor_output_bookmark(doc: Any, cell: NotebookCodeCell) -> Any | None:
    """Keep ``nb_out_*`` at the end of this cell's Output heading.

    ``clear_cell_output`` used ``setString("")`` on a range that started at the
    point bookmark. Writer treats that bookmark as in-range, so the bookmark
    vanished; ``apply_run_result`` then got ``cursor is None`` and appended at
    the document end. Re-attach to the Output heading before clear/insert so
    re-runs replace in-cell stdout like Jupyter.

    Find the heading **after** removing the old bookmark. A cursor captured
    before ``removeTextContent`` is stale and insert then fails, leaving no
    bookmark for the next run. Re-insert with ``gotoEndOfParagraph`` (inside
    the heading), not ``para.getEnd()`` (the paragraph break).
    """
    name = cell.output_start_bookmark
    if not name:
        return None
    current = _cursor_after_bookmark(doc, name)
    if current is not None and _paragraph_string(current).strip() == "Output":
        return current
    try:
        text = doc.getText()
        bookmarks = doc.getBookmarks()
        if bookmarks.hasByName(name):
            text.removeTextContent(bookmarks.getByName(name))
        heading_end = _find_cell_output_heading_end(doc, cell)
        if heading_end is None:
            return _cursor_after_bookmark(doc, name)
        bookmark = doc.createInstance("com.sun.star.text.Bookmark")
        bookmark.Name = name
        text.insertTextContent(heading_end, bookmark, False)
        return _cursor_after_bookmark(doc, name)
    except Exception:
        log.exception("notebook run: failed to reanchor bookmark %r", name)
        return _cursor_after_bookmark(doc, name)


def _delete_paragraph_at(cursor: Any) -> bool:
    """Delete the paragraph containing *cursor*, including its trailing break.

    Select from this paragraph start to the next paragraph start so the range
    is the empty body plus PARAGRAPH_BREAK — not the next paragraph's first
    character (``goRight(1)`` after ``gotoEndOfParagraph`` is version-fragile).
    """
    try:
        text = cursor.getText()
        sel = text.createTextCursorByRange(cursor)
        sel.gotoStartOfParagraph(False)
        nxt = text.createTextCursorByRange(sel)
        if nxt.gotoNextParagraph(False):
            nxt.gotoStartOfParagraph(False)
            sel.gotoRange(nxt.getStart(), True)
        else:
            sel.gotoEndOfParagraph(True)
        sel.setString("")
        return True
    except Exception:
        log.debug("notebook run: delete empty paragraph failed", exc_info=True)
        return False


def _collapse_leading_empty_paragraphs(
    doc: Any, cell: NotebookCodeCell, notebook_in: str | None
) -> None:
    """Remove blank paragraphs between the Output heading and the first stdout line.

    ``_insert_stdout_paragraph`` used to always insert a PARAGRAPH_BREAK (and a
    trailing split). When the bookmark paragraph was already empty, that left
    2–3 blank lines under Output; re-runs accumulated more because
    ``clear_cell_output`` bailed out on whitespace-only ``getString()``.
    """
    for _unused in range(16):
        start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
        if start is None:
            return
        # Bookmark at the paragraph break reports as the *next* para (often a
        # leftover blank). Snap to the Output heading so we delete that blank
        # instead of treating it as the bookmark's home.
        if _paragraph_string(start).strip() != "Output":
            heading = _find_cell_output_heading_end(doc, cell)
            if heading is None:
                return
            start = heading
        nxt = doc.getText().createTextCursorByRange(start)
        if not nxt.gotoNextParagraph(False):
            return
        content = _paragraph_string(nxt)
        if _is_next_cell_boundary(_para_style_name(nxt), content, notebook_in):
            return
        if content.strip():
            return
        if not _delete_paragraph_at(nxt):
            return


# Markdown/raw chrome is ``Cell N: Markdown`` (Heading 3). Code gutters use
# ``WriterAgent Notebook In`` and/or ``[In [n]]\tCell N: Code``.
_CELL_CHROME_RE = re.compile(r"^Cell \d+: (Markdown|Raw|Code)\b")


def _is_next_cell_boundary(para_style: str, content: str, notebook_in_resolved: str | None) -> bool:
    stripped = (content or "").strip()
    # The importer puts ▶ / the code field in their own paragraph after the
    # ``[In [n]]`` gutter, still styled WriterAgent Notebook In but empty.
    # Treating that empty row as a cell boundary made ``_find_cell_output_heading_end``
    # return None before the Output heading, so re-anchor failed and the bookmark
    # died on the next clear.
    if notebook_in_resolved and para_style == notebook_in_resolved and stripped:
        return True
    if stripped.startswith("[In [") and ": Code" in stripped:
        return True
    # Stopping only at the next code gutter ate markdown cells between code cells
    # (the small NumPy fixture is markdown/code/markdown/…).
    return bool(_CELL_CHROME_RE.match(stripped))


def clear_cell_output(doc: Any, cell: NotebookCodeCell) -> None:
    """Remove body content after the output bookmark through the next cell boundary.

    Writer ``XText`` has no ``deleteContents`` (PyUNO raises AttributeError, logged as
    ``failed to clear output for cell`` so re-runs appended stdout). House pattern is
    ``cursor.setString("")`` on the selected range (same as ``html_import`` / ``edit_review``).

    The bookmark must not be in that range: a point bookmark at the range start is
    deleted by ``setString``, and the next insert then falls off the end of the
    document. Re-anchor to the Output heading first and start the deletion at the
    *next* paragraph. Still ``setString`` whitespace-only ranges so leftover empty
    paragraphs do not accumulate under Output.
    """
    _reanchor_output_bookmark(doc, cell)
    text = doc.getText()
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    # Prefer the Output heading over the bookmark cursor. After a first run the
    # bookmark often sits on the paragraph break, so collapseToEnd is already
    # in the next para — setString then deletes nb_out_*. Unit mocks have no
    # heading enumeration, so they still clear from the bookmark cursor.
    heading = _find_cell_output_heading_end(doc, cell)
    if heading is not None:
        start = heading
        end = text.createTextCursorByRange(start)
        if not end.gotoNextParagraph(False):
            return
    else:
        start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
        if start is None:
            return
        end = text.createTextCursorByRange(start)
        if _paragraph_string(start).strip() == "Output":
            if not end.gotoNextParagraph(False):
                return
    if _is_next_cell_boundary(_para_style_name(end), _paragraph_string(end), notebook_in):
        return
    range_start = text.createTextCursorByRange(end)
    found_boundary = False
    while end.gotoNextParagraph(False):
        if _is_next_cell_boundary(_para_style_name(end), _paragraph_string(end), notebook_in):
            end.gotoStartOfParagraph(False)
            found_boundary = True
            break
    if not found_boundary:
        end.gotoEnd(False)
    sel = text.createTextCursorByRange(range_start)
    sel.gotoStartOfParagraph(False)
    sel.gotoRange(end.getStart(), True)
    try:
        sel.setString("")
    except Exception:
        log.exception("notebook run: failed to clear output for cell %d", cell.index)
    if not _bookmark_exists(doc, cell.output_start_bookmark):
        _reanchor_output_bookmark(doc, cell)


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


def _enter_paragraph_after_break(cursor: Any) -> None:
    """Move *cursor* past a just-inserted PARAGRAPH_BREAK.

    Writer leaves the cursor **before** the break (``html_export._range_to_content_via_temp_doc``:
    insertControlCharacter then ``gotoNextParagraph``). ``vision_egress`` / math insert use
    ``goRight(1)``. Without this move, ``insertString`` writes into the Output heading or
    prepends onto the next cell's chrome (``NumPy Version: …Cell 3: Markdown``).
    """
    try:
        if cursor.goRight(1, False):
            return
    except Exception:
        log.debug("notebook run: goRight after PARAGRAPH_BREAK failed", exc_info=True)
    try:
        cursor.gotoNextParagraph(False)
    except Exception:
        log.debug("notebook run: gotoNextParagraph after PARAGRAPH_BREAK failed", exc_info=True)


def apply_run_result(
    doc: Any,
    cell: NotebookCodeCell,
    result: dict[str, Any],
    *,
    ctx: Any | None = None,
) -> None:
    """Write stdout/errors/result and optional image after the output bookmark."""
    out_text = format_run_output_text(result)
    _reanchor_output_bookmark(doc, cell)
    # Insert from the Output heading, not the bookmark cursor. After collapseToEnd
    # a break-anchored bookmark is already in the next paragraph; filling that
    # range can absorb nb_out_* on the next clear.
    cursor = _find_cell_output_heading_end(doc, cell)
    if cursor is None:
        cursor = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    output_style = _resolve_para_style(doc, _STYLE_OUTPUT)
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    if out_text.strip():
        display, _unused = _prepare_display_text(out_text)
        if display.strip():
            if cursor is not None:
                _insert_stdout_paragraph(doc, cell, cursor, display, output_style, notebook_in)
            else:
                # Never dump at the document end — that was the re-click bug.
                log.warning(
                    "notebook run: output bookmark missing for cell %d; not appending at document end",
                    cell.index,
                )
    if result.get("status") == "ok":
        wire = result.get("result")
        images = find_image_payloads(wire)
        for img in images:
            _insert_run_image(doc, img, ctx=ctx, images_before=0)


def _apply_para_style(cursor: Any, style: str | None) -> None:
    if not style:
        return
    try:
        cursor.setPropertyValue("ParaStyleName", style)
    except Exception:
        log.debug("notebook run: ParaStyleName %r not applied", style)


def _split_if_stdout_mashed_onto_chrome(
    doc: Any,
    text: Any,
    cursor: Any,
    display: str,
    output_style: str | None,
    notebook_in: str | None,
) -> None:
    """If insertString prepended onto the next cell heading, split after stdout (PR 461).

    Detect mash by looking at the **rest** of the paragraph after *display*.
    Checking the whole paragraph fails because mashed text starts with stdout
    (``WA_NB_SENTINELCell 3: Markdown``) and no longer matches ``^Cell \\d+:``.
    Do **not** insert a trailing break when the rest is empty — that was the
    extra blank under Output.
    """
    try:
        cursor.gotoStartOfParagraph(False)
        n = min(len(display.encode("utf-16-le")) // 2, 32767)
        rest = text.createTextCursorByRange(cursor)
        if n:
            rest.goRight(n, False)
        rest.gotoEndOfParagraph(True)
        leftover = _plain_text(rest.getString() or "")
        if not leftover.strip():
            _apply_para_style(cursor, output_style)
            return
        if n:
            cursor.goRight(n, False)
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
    except Exception:
        log.debug("notebook run: trailing split after stdout failed", exc_info=True)
        _apply_para_style(cursor, output_style)
        return
    try:
        following = _paragraph_string(cursor)
        if _is_next_cell_boundary(_para_style_name(cursor), following, notebook_in) or _CELL_CHROME_RE.match(
            (following or "").strip()
        ):
            heading = _resolve_para_style(doc, _STYLE_CELL_HEADING)
            _apply_para_style(cursor, heading)
        if output_style:
            prev = text.createTextCursorByRange(cursor)
            if prev.gotoPreviousParagraph(False):
                prev.gotoStartOfParagraph(False)
                _apply_para_style(prev, output_style)
    except Exception:
        log.debug("notebook run: stdout/chrome style restore failed", exc_info=True)


def _insert_stdout_paragraph(
    doc: Any,
    cell: NotebookCodeCell,
    cursor: Any,
    display: str,
    output_style: str | None,
    notebook_in: str | None,
) -> None:
    """Insert *display* as its own paragraph under Output; do not eat the next cell.

    Always inserting a PARAGRAPH_BREAK before ``insertString`` (and another
    trailing split) left a blank paragraph when the bookmark para was already
    empty. Fill an existing empty paragraph; only split when the current para
    has content (Output heading or next-cell chrome). Trailing split only if
    stdout would otherwise share a line with ``Cell N: Markdown`` (PR 461).
    """
    text = doc.getText()

    def _fill(target: Any) -> None:
        try:
            target.gotoStartOfParagraph(False)
        except Exception:
            log.debug("notebook run: gotoStartOfParagraph before stdout failed", exc_info=True)
        _apply_para_style(target, output_style)
        text.insertString(target, display, False)
        _split_if_stdout_mashed_onto_chrome(doc, text, target, display, output_style, notebook_in)

    def _finish() -> None:
        _reanchor_output_bookmark(doc, cell)
        _collapse_leading_empty_paragraphs(doc, cell, notebook_in)

    # Bookmark/find cursor may sit inside "Output". A PARAGRAPH_BREAK there
    # splits the heading into "O" + "utput". Snap to after the last character.
    if _paragraph_string(cursor).strip() == "Output":
        try:
            cursor.gotoStartOfParagraph(False)
            n = min(len("Output"), 32767)
            if not cursor.goRight(n, False):
                cursor.gotoEndOfParagraph(False)
        except Exception:
            log.debug("notebook run: snap to end of Output heading failed", exc_info=True)

    if _paragraph_is_empty(cursor):
        _fill(cursor)
        _finish()
        return

    nxt = text.createTextCursorByRange(cursor)
    if nxt.gotoNextParagraph(False):
        nxt_text = _paragraph_string(nxt)
        if not nxt_text.strip() and not _is_next_cell_boundary(
            _para_style_name(nxt), nxt_text, notebook_in
        ):
            _fill(nxt)
            _finish()
            return

    text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
    _enter_paragraph_after_break(cursor)
    _fill(cursor)
    _finish()


def _leading_text_cursor(text: Any, para: Any) -> Any | None:
    """Cursor over leading Text portions of *para*, stopping before in-flow shapes.

    Importer used to put AS_CHARACTER ▶ / code ``TextField`` in the same paragraph as
    ``[In [n]]\\tCell N: Code``. ``setString`` on ``para.getStart()``–``getEnd()`` then
    deleted those ``ControlShape``s (TextPortionType ``Frame``). Replace text only.
    """
    try:
        enum = para.createEnumeration()
    except Exception:
        return None
    first = None
    last = None
    while enum.hasMoreElements():
        portion = enum.nextElement()
        try:
            ptype = str(portion.getPropertyValue("TextPortionType") or "")
        except Exception:
            ptype = str(getattr(portion, "TextPortionType", "") or "")
        if ptype == "Frame":
            break
        if ptype != "Text":
            continue
        if first is None:
            first = portion
        last = portion
    if first is None:
        return None
    try:
        cursor = text.createTextCursorByRange(first)
        if last is not None:
            cursor.gotoRange(last, True)
        return cursor
    except Exception:
        log.debug("notebook run: could not build leading text cursor", exc_info=True)
        return None


def _gutter_text_cursor(text: Any, para: Any) -> Any | None:
    """Range to rewrite for ``[In [n]]`` — never a range that contains ControlShapes."""
    cursor = _leading_text_cursor(text, para)
    if cursor is not None:
        return cursor
    # Fallback when portion enumeration is unavailable (unit mocks): expand only
    # as far as getString() so we do not cover AS_CHARACTER positions it omits.
    try:
        content = para.getString() or ""
        cursor = text.createTextCursorByRange(para.getStart())
        n = min(len(content), 32767)
        if n:
            cursor.goRight(n, True)
        return cursor
    except Exception:
        log.debug("notebook run: gutter text cursor fallback failed", exc_info=True)
        return None


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
            cursor = _gutter_text_cursor(text, para)
            if cursor is None:
                return
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

    result = execute_code(ctx, doc, code)
    # After execute so live smoke can tell ok from a sandbox dunder deny.
    log.info(
        "notebook run cell index=%d field=%s status=%s",
        cell.index,
        cell.code_field_name,
        result.get("status"),
    )
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
            _("This document has no imported notebook. Use WriterAgent → Debug → Import Jupyter Notebook… first."),
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
    """New kernel starts at 1. Saved ipynb ``execution_count`` values are historical.

    ``max(saved)+1`` made the first live ▶ show ``[In [4]]`` on the small NumPy
    fixture (saved 1, 2, 3). Jupyter starts a new kernel at 1; our ``notebook:…``
    venv session is a new kernel on import. Re-runs still increment by 1.
    """
    state.next_execution_count = 1
