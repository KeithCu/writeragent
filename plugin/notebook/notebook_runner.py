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
    _IN_PROMPT_RE,
    _PARAGRAPH_BREAK,
    _STYLE_MD_H1,
    _STYLE_MD_H2,
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


def format_run_output_text(result: dict[str, Any], execution_count: int | None = None) -> str:
    """Plain-text body for a cell output block (stdout, errors, scalar result).

    ``Out [n]:`` is only for execute_result / last-line values, not print streams.
    """
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
            rendered = repr(value)
            if execution_count is not None:
                parts.append(f"Out [{execution_count}]: {rendered}")
            else:
                parts.append(rendered)
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
_OUT_PROMPT_RE = re.compile(r"^Out \[[0-9 ]*\]:")
_LEGACY_CHROME_RE = re.compile(r"^Cell \d+: (Markdown|Raw|Code)\b")
_CELL_CHROME_RE = _LEGACY_CHROME_RE


def _style_compact(para_style: str) -> str:
    return (para_style or "").lower().replace(" ", "")


def _style_is_heading12(para_style: str) -> bool:
    return _style_compact(para_style) in ("heading1", "heading2")


def _style_is_preformatted(para_style: str) -> bool:
    return _style_compact(para_style) in ("preformattedtext", "preformatted")


def _same_paragraph(a: Any, b: Any) -> bool:
    """True when *a* and *b* are in the same Writer paragraph."""
    try:
        text = a.getText()
        ca = text.createTextCursorByRange(a)
        cb = text.createTextCursorByRange(b)
        ca.gotoStartOfParagraph(False)
        cb.gotoStartOfParagraph(False)
        return int(text.compareRegionStarts(ca, cb)) == 0
    except Exception:
        return False


def _is_this_cell_field_paragraph(doc: Any, cell: NotebookCodeCell, cursor: Any) -> bool:
    end = _code_field_paragraph_end(doc, cell)
    if end is None:
        return False
    return _same_paragraph(cursor, end)


def _is_foreign_control_paragraph(doc: Any, cell: NotebookCodeCell, cursor: Any) -> bool:
    """True when *cursor* is in another cell's ▶ gutter or code-field paragraph."""
    if not _paragraph_has_frame(cursor):
        return False
    return not _is_this_cell_field_paragraph(doc, cell, cursor)


def _is_output_bookmark_home(
    cursor: Any, doc: Any | None = None, cell: NotebookCodeCell | None = None
) -> bool:
    """True when *cursor* is in *this* cell's code-field row (or leftover Output).

    Any ``In [n]:`` used to count as home. Consecutive code cells (medium In[2]
    then In[3]) drift the bookmark onto the *next* gutter; clear then skipped
    that label and ``setString`` ate In[3]'s ▶ and TextField.
    """
    content = _paragraph_string(cursor).strip()
    if content == "Output":
        return True
    if doc is not None and cell is not None:
        if _is_this_cell_field_paragraph(doc, cell, cursor):
            return True
        # No code field (UNO tests that only insert a bookmark): In [n]: is home.
        if _IN_PROMPT_RE.match(content) and _code_field_paragraph_end(doc, cell) is None:
            return True
        return False
    if _IN_PROMPT_RE.match(content):
        return True
    if _paragraph_has_frame(cursor):
        return True
    return False


def _is_leftover_empty_paragraph(cursor: Any) -> bool:
    """Empty row that is not a heading and not stdout — leftover gap or ▶+field.

    ``getString`` omits ControlShapes, so the ▶+field paragraph looks like this.
    A leftover blank *between* field and markdown looks the same; insert fills
    that blank, while clear skips it so ``setString`` cannot eat frames.
    """
    if _paragraph_string(cursor).strip():
        return False
    style = _para_style_name(cursor)
    if _style_is_preformatted(style) or _style_is_heading12(style):
        return False
    return True


def _paragraph_has_frame(cursor: Any) -> bool:
    """True when the paragraph contains an in-flow ControlShape / graphic (▶ or field)."""
    try:
        text = cursor.getText()
        enum = text.createEnumeration()
    except Exception:
        return False
    steps = 0
    while steps < _ENUM_SAFETY_CAP:
        more = enum.hasMoreElements()
        if more is not True and more != 1:
            break
        steps += 1
        para = enum.nextElement()
        try:
            if hasattr(para, "supportsService") and not para.supportsService("com.sun.star.text.Paragraph"):
                continue
            start_cmp = text.compareRegionStarts(para.getStart(), cursor)
            end_cmp = text.compareRegionEnds(cursor, para.getEnd())
            # compareRegionStarts(A,B) is 1 if A starts before B. Cursor is in *para*
            # when para starts at/before the cursor and the cursor ends at/before para.
            if not (start_cmp >= 0 and end_cmp >= 0):
                continue
            portions = para.createEnumeration()
        except Exception:
            continue
        psteps = 0
        while psteps < 64:
            pmore = portions.hasMoreElements()
            if pmore is not True and pmore != 1:
                break
            psteps += 1
            portion = portions.nextElement()
            try:
                ptype = str(portion.getPropertyValue("TextPortionType") or "")
            except Exception:
                ptype = str(getattr(portion, "TextPortionType", "") or "")
            if ptype == "Frame":
                return True
        return False
    return False


def _find_control_shape_by_name(doc: Any, name: str) -> Any | None:
    if not name or not hasattr(doc, "getDrawPage"):
        return None
    try:
        dp = doc.getDrawPage()
        count = int(dp.getCount())
    except Exception:
        return None
    for i in range(count):
        try:
            shape = dp.getByIndex(i)
            if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
                continue
            model = shape.Control
            if str(getattr(model, "Name", "") or "") == name:
                return shape
        except Exception:
            continue
    return None


def _code_field_paragraph_end(doc: Any, cell: NotebookCodeCell) -> Any | None:
    shape = _find_control_shape_by_name(doc, cell.code_field_name)
    if shape is None:
        return None
    try:
        anchor = shape.getAnchor()
        text = doc.getText()
        cursor = text.createTextCursorByRange(anchor)
        cursor.gotoEndOfParagraph(False)
        return cursor
    except Exception:
        log.debug("notebook run: code field anchor unusable for %s", cell.code_field_name, exc_info=True)
        return None


def _find_cell_output_heading_end(doc: Any, cell: NotebookCodeCell) -> Any | None:
    """Cursor at this cell's output bookmark (end of the ▶+field paragraph).

    Named for the old Heading 4 ``Output`` scan. Chrome is gone; the invisible
    ``nb_out_*`` bookmark is the source of truth. Fallback: end of the code-field
    paragraph so re-anchor can restore a deleted bookmark.
    """
    cur = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if cur is not None and _is_output_bookmark_home(cur, doc, cell):
        return cur
    field_end = _code_field_paragraph_end(doc, cell)
    if field_end is not None:
        return field_end
    return cur


def _reanchor_output_bookmark(doc: Any, cell: NotebookCodeCell) -> Any | None:
    """Keep ``nb_out_*`` at the end of this cell's ▶+field paragraph.

    ``clear_cell_output`` used ``setString("")`` on a range that started at the
    point bookmark. Writer treats that bookmark as in-range, so the bookmark
    vanished; ``apply_run_result`` then got ``cursor is None`` and appended at
    the document end. Re-attach before clear/insert so re-runs replace in-cell
    stdout like Jupyter.

    Find the insert point **after** removing the old bookmark. A cursor captured
    before ``removeTextContent`` is stale and insert then fails, leaving no
    bookmark for the next run. Re-insert with ``gotoEndOfParagraph`` (inside
    the control paragraph), not ``para.getEnd()`` (the paragraph break).
    """
    name = cell.output_start_bookmark
    if not name:
        return None
    current = _cursor_after_bookmark(doc, name)
    if current is not None and _is_output_bookmark_home(current, doc, cell):
        return current
    # Bookmark at the paragraph break reports as the *next* cell (markdown /
    # In [n]:). Insert then mashed stdout onto that heading. Move it back
    # inside the ▶+field (or In-prompt) paragraph. Capture the insert point
    # before removeTextContent — a cursor taken from the removed bookmark is stale.
    insert_at = _code_field_paragraph_end(doc, cell)
    if insert_at is None and current is not None:
        notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
        if _is_next_cell_boundary(
            _para_style_name(current), _paragraph_string(current), notebook_in
        ):
            try:
                prev = doc.getText().createTextCursorByRange(current)
                if prev.gotoPreviousParagraph(False):
                    prev.gotoEndOfParagraph(False)
                    insert_at = prev
            except Exception:
                insert_at = None
    if insert_at is None:
        return current
    try:
        text = doc.getText()
        bookmarks = doc.getBookmarks()
        if bookmarks.hasByName(name):
            text.removeTextContent(bookmarks.getByName(name))
        bookmark = doc.createInstance("com.sun.star.text.Bookmark")
        bookmark.Name = name
        text.insertTextContent(insert_at, bookmark, False)
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
        # leftover blank). Snap to the ▶+field bookmark paragraph so we delete
        # that blank instead of treating it as the bookmark's home.
        at_home = _is_output_bookmark_home(start, doc, cell)
        if not at_home:
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
        if _is_foreign_control_paragraph(doc, cell, nxt):
            return
        if content.strip():
            return
        if _paragraph_has_frame(nxt):
            return
        if not _delete_paragraph_at(nxt):
            return


# Next-cell boundary: In [n]: gutter, markdown Heading 1/2 / Text Body, or
# leftover Cell N chrome from older imports. Empty ▶+field rows are not
# boundaries (getString omits ControlShapes).
_CELL_CHROME_RE = _LEGACY_CHROME_RE


def _is_next_cell_boundary(para_style: str, content: str, notebook_in_resolved: str | None) -> bool:
    stripped = (content or "").strip()
    # The importer puts ▶ / the code field in their own paragraph after the
    # In [n]: gutter, still possibly styled but empty of text. Treating that
    # empty row as a cell boundary made output-anchor lookup return None.
    if notebook_in_resolved and para_style == notebook_in_resolved and stripped:
        return True
    if _IN_PROMPT_RE.match(stripped):
        return True
    if stripped.startswith("[In [") and ": Code" in stripped:
        return True
    if _OUT_PROMPT_RE.match(stripped) or _style_is_preformatted(para_style):
        return False
    if _LEGACY_CHROME_RE.match(stripped):
        return True
    if _style_is_heading12(para_style) and stripped:
        return True
    compact = _style_compact(para_style)
    if stripped and compact in ("textbody", "textkörper", "bodytext"):
        return True
    return False


def clear_cell_output(doc: Any, cell: NotebookCodeCell) -> None:
    """Remove body content after the output bookmark through the next cell boundary.

    Writer ``XText`` has no ``deleteContents`` (PyUNO raises AttributeError, logged as
    ``failed to clear output for cell`` so re-runs appended stdout). House pattern is
    ``cursor.setString("")`` on the selected range (same as ``html_import`` / ``edit_review``).

    The bookmark must not be in that range: a point bookmark at the range start is
    deleted by ``setString``, and the next insert then falls off the end of the
    document. Re-anchor to the ▶+field paragraph first and start the deletion at the
    *next* paragraph. Still ``setString`` whitespace-only ranges so leftover empty
    paragraphs do not accumulate under the cell.
    """
    _reanchor_output_bookmark(doc, cell)
    text = doc.getText()
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    # Always start from *this* cell's field paragraph. A drifted bookmark on the
    # next In [n]: used to skip that gutter and setString the next ▶+field.
    start = _code_field_paragraph_end(doc, cell)
    if start is None:
        start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if start is None:
        return
    end = text.createTextCursorByRange(start)
    # Bookmark lives in the ▶+field paragraph (getString is empty because frames
    # do not appear). Starting setString there deleted ▶, the TextField, and the
    # bookmark — live UNO runs then logged "output bookmark missing".
    skip_home = _is_output_bookmark_home(start, doc, cell) or (
        _is_leftover_empty_paragraph(start) and not _paragraph_has_frame(start)
    )
    if skip_home:
        if not end.gotoNextParagraph(False):
            return
    if _is_next_cell_boundary(_para_style_name(end), _paragraph_string(end), notebook_in):
        return
    if _is_foreign_control_paragraph(doc, cell, end):
        return
    range_start = text.createTextCursorByRange(end)
    found_boundary = False
    while end.gotoNextParagraph(False):
        if _is_next_cell_boundary(_para_style_name(end), _paragraph_string(end), notebook_in):
            end.gotoStartOfParagraph(False)
            found_boundary = True
            break
        if _is_foreign_control_paragraph(doc, cell, end):
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


def _insert_run_image(
    doc: Any,
    payload: dict[str, Any],
    *,
    ctx: Any,
    images_before: int,
    text_cursor: Any | None = None,
) -> bool:
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
    return _insert_image_in_flow(
        doc, raw=bytes(raw), mime=mime, images_before=images_before, ctx=ctx, text_cursor=text_cursor
    )


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
    out_text = format_run_output_text(result, cell.execution_count)
    _reanchor_output_bookmark(doc, cell)
    cursor = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if cursor is None:
        cursor = _find_cell_output_heading_end(doc, cell)
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
        if not images:
            return
        img_cursor = _cursor_after_bookmark(doc, cell.output_start_bookmark)
        if img_cursor is None:
            img_cursor = _code_field_paragraph_end(doc, cell)
        text = doc.getText()
        for img in images:
            if img_cursor is None:
                log.warning(
                    "notebook run: image bookmark missing for cell %d; not appending at document end",
                    cell.index,
                )
                break
            if _paragraph_has_frame(img_cursor) or _paragraph_string(img_cursor).strip():
                text.insertControlCharacter(img_cursor, _PARAGRAPH_BREAK, False)
                _enter_paragraph_after_break(img_cursor)
            _insert_run_image(doc, img, ctx=ctx, images_before=0, text_cursor=img_cursor)


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
        leftover_text = leftover.strip()
        if not leftover_text:
            _apply_para_style(cursor, output_style)
            return
        if n:
            cursor.goRight(n, False)
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        _enter_paragraph_after_break(cursor)
    except Exception:
        log.debug("notebook run: trailing split after stdout failed", exc_info=True)
        _apply_para_style(cursor, output_style)
        return
    try:
        # Leftover inherits Preformatted from fill, so _is_next_cell_boundary
        # would skip (stdout style). Restore markdown/gutter from the text.
        if _IN_PROMPT_RE.match(leftover_text) or leftover_text.startswith("[In ["):
            _apply_para_style(cursor, notebook_in)
        else:
            heading = _resolve_para_style(doc, _STYLE_MD_H2) or _resolve_para_style(doc, _STYLE_MD_H1)
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
    """Insert *display* as its own paragraph under the code field; do not eat the next cell.

    Always inserting a PARAGRAPH_BREAK before ``insertString`` (and another
    trailing split) left a blank paragraph when the bookmark para was already
    empty. Fill an existing empty paragraph; only split when the current para
    has content (▶+field or next-cell markdown). Trailing split only if
    stdout would otherwise share a line with the following markdown (PR 461).
    Never write into the ▶+field paragraph — that mixed stdout with controls
    and put a stray bookmark glyph on a visible heading.
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

    def _break_then_fill() -> None:
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        _enter_paragraph_after_break(cursor)
        _fill(cursor)
        _finish()

    # Bookmark on the paragraph break reports as the following markdown. Snap
    # back so PARAGRAPH_BREAK does not split that heading and prepend stdout.
    field_end = _code_field_paragraph_end(doc, cell)
    if field_end is not None:
        try:
            cursor.gotoRange(field_end, False)
        except Exception:
            log.debug("notebook run: snap insert cursor to code field failed", exc_info=True)
    elif _is_next_cell_boundary(_para_style_name(cursor), _paragraph_string(cursor), notebook_in):
        try:
            prev = text.createTextCursorByRange(cursor)
            if prev.gotoPreviousParagraph(False):
                prev.gotoEndOfParagraph(False)
                cursor.gotoRange(prev, False)
        except Exception:
            log.debug("notebook run: snap insert cursor to previous para failed", exc_info=True)

    if _is_output_bookmark_home(cursor, doc, cell):
        try:
            cursor.gotoEndOfParagraph(False)
        except Exception:
            log.debug("notebook run: snap to end of control paragraph failed", exc_info=True)
        nxt = text.createTextCursorByRange(cursor)
        if nxt.gotoNextParagraph(False):
            nxt_text = _paragraph_string(nxt)
            if (
                not nxt_text.strip()
                and not _style_is_heading12(_para_style_name(nxt))
                and not _is_next_cell_boundary(_para_style_name(nxt), nxt_text, notebook_in)
                and not _paragraph_has_frame(nxt)
            ):
                # Import lead_break leaves an empty para after ▶+field. Fill it
                # rather than inserting another break (field | blank | stdout).
                _fill(nxt)
                _finish()
                return
        _break_then_fill()
        return

    if _paragraph_is_empty(cursor) and not _style_is_heading12(_para_style_name(cursor)):
        # Leftover empty gap (Text Body) or empty Preformatted: fill in place.
        # Never write into a ▶+field paragraph (this cell or the next).
        if _paragraph_has_frame(cursor):
            try:
                cursor.gotoEndOfParagraph(False)
            except Exception:
                log.debug("notebook run: snap to end of framed paragraph failed", exc_info=True)
            _break_then_fill()
            return
        _fill(cursor)
        _finish()
        return

    nxt = text.createTextCursorByRange(cursor)
    if nxt.gotoNextParagraph(False):
        nxt_text = _paragraph_string(nxt)
        if not nxt_text.strip() and not _is_next_cell_boundary(
            _para_style_name(nxt), nxt_text, notebook_in
        ) and not _paragraph_has_frame(nxt):
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
    ``[In [n]]``. ``setString`` on ``para.getStart()``–``getEnd()`` then
    deleted those ``ControlShape``s (TextPortionType ``Frame``). ▶ now sits on the
    ``In [n]:`` gutter; rewrite leading Text portions only, never the Frame.
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
    """Update the ``In [n]:`` gutter. Never setString a range that contains ControlShapes."""
    new_line = _format_in_prompt(execution_count)
    try:
        text = doc.getText()
    except Exception:
        log.debug("notebook run: could not get text for in prompt", exc_info=True)
        return

    para = None
    shape = _find_control_shape_by_name(doc, cell.code_field_name)
    if shape is not None:
        try:
            anchor = shape.getAnchor()
            cursor = text.createTextCursorByRange(anchor)
            if cursor.gotoPreviousParagraph(False):
                para_rng = text.createTextCursorByRange(cursor)
                para_rng.gotoStartOfParagraph(False)
                para_rng.gotoEndOfParagraph(True)
                para = para_rng
        except Exception:
            log.debug("notebook run: gutter from code field failed", exc_info=True)
            para = None
    if para is None:
        try:
            enum = text.createEnumeration()
        except Exception:
            log.debug("notebook run: could not enumerate text for in prompt", exc_info=True)
            return
        marker = f"Cell {cell.index + 1}: Code"
        while enum.hasMoreElements():
            candidate = enum.nextElement()
            try:
                content = candidate.getString() or ""
            except Exception:
                continue
            stripped = str(content).strip()
            if marker in stripped or _IN_PROMPT_RE.match(stripped) or stripped.startswith("[In ["):
                para = candidate
                break
    if para is None:
        return
    try:
        cursor = _gutter_text_cursor(text, para)
        if cursor is None:
            return
        cursor.setString(new_line)
    except Exception:
        log.exception("notebook run: failed to update in prompt for cell %d", cell.index)


def _save_view_cursor(doc: Any) -> Any | None:
    try:
        vc = doc.getCurrentController().getViewCursor()
        return doc.getText().createTextCursorByRange(vc)
    except Exception:
        return None


def _restore_view_to_cell(doc: Any, cell: NotebookCodeCell, saved: Any | None = None) -> None:
    """Keep the view on the cell that ran instead of jumping to the document end."""
    vc = None
    try:
        vc = doc.getCurrentController().getViewCursor()
    except Exception:
        return
    shape = _find_control_shape_by_name(doc, cell.code_field_name)
    try:
        if shape is not None:
            vc.gotoRange(shape.getAnchor(), False)
            return
        if saved is not None:
            vc.gotoRange(saved, False)
    except Exception:
        log.debug("notebook run: restore view to cell failed", exc_info=True)


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

    saved_view = _save_view_cursor(doc)
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
    _restore_view_to_cell(doc, cell, saved_view)

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
