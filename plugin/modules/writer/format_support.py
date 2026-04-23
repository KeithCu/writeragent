# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Format conversion helpers for Writer tools.

High-level behavior
-------------------

- **Document → HTML**: `document_to_content` and `_range_to_content_via_temp_doc`
  export full documents, selections, or character ranges as HTML using the
  Writer HTML filter. `_strip_html_boilerplate` removes `<body>` wrappers so
  callers see just the content.

- **HTML / text → document (import path)**: `insert_content_at_position`,
  `replace_full_document`, `apply_content_at_range`, `apply_content_at_search`,
  and `replace_single_range_with_content` flow through ``insertDocumentFromURL``
  for non-math HTML. When the fragment contains ``<math>`` … ``</math>``,
  `_insert_mixed_or_plain_html` splits prose and MathML, imports prose chunks
  with the same filter, converts each MathML island via LibreOffice Math
  (see `math_mml_convert`), and inserts editable formula objects
  (`math_formula_insert`). `_ensure_html_linebreaks` converts plain text
  (with newlines) into minimal HTML (`<p>`, `<br>`) and `_wrap_html_fragment`
  ensures a full HTML document when needed so the filter behaves consistently.

- **Format-preserving replacement (plain text path)**: For small textual edits,
  callers can keep the content as plain text and use
  `replace_preserving_format` or `_preserving_search_replace`. These walk the
  document character‑by‑character and change only glyphs, not character
  properties, so bold/italic, font, color, background, etc. are preserved even
  when the replacement length differs.

Key gotchas this module protects against
----------------------------------------

- **Raw content vs. HTML wrapping**: The format‑preserving path must receive
  the **raw text** the user sees in the document, *not* an HTML‑wrapped
  version. If you pass the wrapped HTML from `_ensure_html_linebreaks`
  directly into `replace_preserving_format`, you will overwrite the document
  with literal markup characters. Callers (e.g. `ApplyDocumentContent`) should
  therefore capture `raw_content` before any HTML processing and use that
  solely for preserving‑format replacements.

- **Markup detection order**: `content_has_markup` must be run on the original
  input string. Running it after `_ensure_html_linebreaks` or `_wrap_html_fragment`
  will always see `<html>`/`<body>` and force the import path, disabling
  format‑preserving behavior accidentally.

- **LO search vs. paragraphs**: LibreOffice search descriptors do not match
  across paragraph boundaries. `_preserving_search_replace` stays within
  the standard search API, but higher‑level tools (see `content.ApplyDocumentContent`)
  sometimes need a full‑text fallback (`_find_range_by_offset`) when a match
  can span multiple paragraphs.

This file intentionally keeps the *mechanics* of HTML import/export and
format‑preserving replacement here, while higher‑level tool behavior and
prompt guidance live in `content.py` and system prompts.
"""

import contextlib
import logging
import uno
from plugin.framework.uno_context import get_desktop
import os
import re
import tempfile

from plugin.framework.errors import ToolExecutionError
import urllib.parse
import urllib.request
from typing import Any, cast
import html as html_mod
from plugin.modules.writer.html_math_segment import (
    html_fragment_contains_mathml,
    segment_html_with_mathml,
)
from plugin.modules.writer.math_formula_insert import insert_writer_math_formula
from plugin.modules.writer.math_mml_convert import convert_mathml_to_starmath
from plugin.modules.writer.ops import get_selection_range
from plugin.modules.writer.ops import get_text_cursor_at_range

log = logging.getLogger("writeragent.writer")


from plugin.framework.utils import normalize_linebreaks as _normalize


# ---------------------------------------------------------------------------
# Format configuration
# ---------------------------------------------------------------------------

HTML_FILTER = "HTML (StarWriter)"
HTML_EXTENSION = ".html"

# System temp directory (cross-platform).
TEMP_DIR = tempfile.gettempdir()


def _get_format_props(config_svc=None):
    """Return ``(filter_name, file_extension)`` for HTML format."""
    return HTML_FILTER, HTML_EXTENSION


# ---------------------------------------------------------------------------
# UNO helpers (import inside functions to avoid import-time dependency)
# ---------------------------------------------------------------------------

def _file_url(path):
    """Return a ``file://`` URL for *path*."""
    return urllib.parse.urljoin(
        "file:", urllib.request.pathname2url(os.path.abspath(path))
    )


def _create_property_value(name, value):
    """Create a ``com.sun.star.beans.PropertyValue``."""
    p = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    p.Name = name
    p.Value = value
    return p


@contextlib.contextmanager
def _with_temp_buffer(content=None, config_svc=None):
    """Context manager that yields ``(path, file_url)`` for a temp file
    with the correct format extension.

    If *content* is not ``None`` it is written to the file.
    The file is deleted on exit.
    """
    _, ext = _get_format_props(config_svc)
    fd, path = tempfile.mkstemp(suffix=ext, dir=TEMP_DIR)
    try:
        if content is not None:
            if isinstance(content, list):
                content = "\n".join(str(x) for x in content)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            os.close(fd)
        yield (path, _file_url(path))
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _strip_html_boilerplate(html_string):
    """Extract content between ``<body>`` tags if present."""
    if not html_string or not isinstance(html_string, str):
        return html_string
    match = re.search(
        r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    return html_string


def _wrap_html_fragment(html_content):
    """Wrap an HTML fragment in a full document structure for LO's filter."""
    if not html_content or not isinstance(html_content, str):
        return html_content
    has_html = "<html" in html_content.lower() and "</html>" in html_content.lower()
    has_body = "<body" in html_content.lower() and "</body>" in html_content.lower()
    if has_html and has_body:
        return html_content
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        '<meta charset="UTF-8">\n</head>\n<body>\n'
        "%s\n</body>\n</html>" % html_content
    )


def _ensure_html_linebreaks(content):
    """Convert newlines to ``<br>``/``<p>`` when content is plain text
    and the active format is HTML, so LO's filter preserves them.
    """
    if not isinstance(content, str) or not content:
        return content
    content = _normalize(content)
    unescaped = html_mod.unescape(content)
    html_tags = [
        "<p>", "<br>", "</h1>", "<h2>", "<h3>",
        "</ul>", "</li>", "</div>", "<html>",
    ]
    has_html = any(tag in unescaped.lower() for tag in html_tags)
    if has_html:
        return _wrap_html_fragment(unescaped)

    content = re.sub(r"\n{3,}", "\n\n", content)
    paras = content.split("\n\n")
    out = []
    for p in paras:
        if not p.strip():
            continue
        p_html = p.replace("\n", "<br>\n")
        out.append("<p>%s</p>" % p_html)
    return _wrap_html_fragment("\n".join(out))


def html_to_plain_text(html_string, ctx, config_svc=None):
    """Convert HTML to plain text by loading it into LibreOffice and reading
    the text out. Use this instead of regex stripping so entities, nested
    tags, and whitespace are handled correctly.
    """
    if not html_string or not isinstance(html_string, str):
        return (html_string or "").strip()
    prepared = _wrap_html_fragment(html_string.strip())
    temp_doc = None
    try:
        desktop = get_desktop(ctx)
        load_props = (_create_property_value("Hidden", True),)
        temp_doc = desktop.loadComponentFromURL(
            "private:factory/swriter", "_default", 0, load_props
        )
        if not temp_doc or not hasattr(temp_doc, "getText"):
            return html_string.strip()
        with _with_temp_buffer(prepared, config_svc) as (_path, file_url):
            filter_name, _ = _get_format_props(config_svc)
            filter_props = (_create_property_value("FilterName", filter_name),)
            text = temp_doc.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.insertDocumentFromURL(file_url, filter_props)
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            return cursor.getString().strip()
    except Exception as exc:
        log.debug("html_to_plain_text failed: %s", exc)
        return html_string.strip()
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Document -> content
# ---------------------------------------------------------------------------

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0


def _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc):
    """Export a character range to content via a hidden temp document."""
    temp_doc = None
    try:
        ctx.getServiceManager()
        desktop = get_desktop(ctx)
        load_props = (_create_property_value("Hidden", True),)
        temp_doc = desktop.loadComponentFromURL(
            "private:factory/swriter", "_default", 0, load_props
        )
        if not temp_doc or not hasattr(temp_doc, "getText"):
            return ""

        temp_text = temp_doc.getText()
        temp_cursor = temp_text.createTextCursor()
        text = model.getText()
        enum = text.createEnumeration()
        first_para = True
        added_any = False

        while enum.hasMoreElements():
            el = enum.nextElement()
            if not hasattr(el, "getString"):
                continue
            try:
                style = el.getPropertyValue("ParaStyleName")
            except Exception:
                style = ""
            style = style or ""
            para_text = el.getString()

            # Compute paragraph start offset
            start_cursor = model.getText().createTextCursor()
            start_cursor.gotoStart(False)
            start_cursor.gotoRange(el.getStart(), True)
            para_start = len(start_cursor.getString())
            para_end = para_start + len(para_text)

            if para_end <= start or para_start >= end:
                continue
            if para_start < start or para_end > end:
                trim_start = max(0, start - para_start)
                trim_end = len(para_text) - max(0, para_end - end)
                para_text = para_text[trim_start:trim_end]

            if first_para:
                temp_cursor.gotoStart(False)
                temp_cursor.setString(para_text)
                temp_cursor.setPropertyValue("ParaStyleName", style)
                first_para = False
            else:
                temp_cursor.gotoEnd(False)
                temp_text.insertControlCharacter(
                    temp_cursor, _PARAGRAPH_BREAK, False
                )
                temp_cursor.setPropertyValue("ParaStyleName", style)
                temp_cursor.setString(para_text)
            added_any = True

        if not added_any:
            return ""

        filter_name, _ = _get_format_props(config_svc)
        with _with_temp_buffer(None, config_svc) as (path, file_url):
            props = (_create_property_value("FilterName", filter_name),)
            temp_doc.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        content = _strip_html_boilerplate(content)
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "\n\n[... truncated ...]"
        return content
    except Exception as exc:
        log.debug("_range_to_content_via_temp_doc failed: %s", exc)
        return ""
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


def document_to_content(model, ctx, services, max_chars=None,
                        scope="full", range_start=None, range_end=None):
    """Export a Writer document (or part of it) as HTML.

    Args:
        model: UNO document model.
        ctx: UNO component context.
        services: ServiceRegistry.
        max_chars: Truncate result to this length.
        scope: ``'full'``, ``'selection'``, or ``'range'``.
        range_start: Character offset start (for scope ``'range'``).
        range_end: Character offset end (for scope ``'range'``).

    Returns:
        Content string.
    """
    config_svc = services.get("config") if services else None

    if scope == "selection":
        start, end = get_selection_range(model)
        return _range_to_content_via_temp_doc(
            model, ctx, start, end, max_chars, config_svc
        )

    if scope == "range":
        start = int(range_start) if range_start is not None else 0
        end = int(range_end) if range_end is not None else 0
        doc_len = services.document.get_document_length(model) if services else 0
        start = max(0, min(start, doc_len))
        end = min(end, doc_len)
        return _range_to_content_via_temp_doc(
            model, ctx, start, end, max_chars, config_svc
        )

    # scope == "full"
    try:
        filter_name, _ = _get_format_props(config_svc)
        with _with_temp_buffer(None, config_svc) as (path, file_url):
            props = (_create_property_value("FilterName", filter_name),)
            model.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            content = _strip_html_boilerplate(content)
            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + "\n\n[... truncated ...]"
            return content
    except Exception as exc:
        log.debug("document_to_content (full) failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Content -> Document
# ---------------------------------------------------------------------------

def _cursor_goto_document_end(model, cursor) -> None:
    """Move *cursor* to the end of the document body (``model.getText()``)."""
    end_c = model.getText().createTextCursor()
    end_c.gotoEnd(False)
    cursor.gotoRange(end_c.getStart(), False)


def _insert_starwriter_html_at_cursor(model, cursor, prepared_html, config_svc=None):
    """Import one HTML fragment through the StarWriter HTML filter at *cursor*."""
    with _with_temp_buffer(prepared_html, config_svc) as (_path, file_url):
        filter_name, _ = _get_format_props(config_svc)
        filter_props = (_create_property_value("FilterName", filter_name),)
        cursor.insertDocumentFromURL(file_url, filter_props)
    _cursor_goto_document_end(model, cursor)


def _insert_mixed_html_and_math_at_cursor(
    model, ctx, cursor, unescaped: str, config_svc=None
):
    """Insert alternating HTML (via filter) and MathML (as formula objects)."""
    _segs = segment_html_with_mathml(unescaped)
    if log.isEnabledFor(logging.DEBUG) and html_fragment_contains_mathml(unescaped):
        _math_i = 0
        for _si, _s in enumerate(_segs):
            if _s.kind == "html":
                log.debug(
                    "mixed_html_math: segment[%d] html nl=%d len=%d",
                    _si,
                    _s.text.count("\n"),
                    len(_s.text),
                )
            else:
                _math_i += 1
                log.debug(
                    "mixed_html_math: segment[%d] math#%d display_block=%s "
                    "mathml_nl=%d mathml_len=%d",
                    _si,
                    _math_i,
                    _s.display_block,
                    _s.text.count("\n"),
                    len(_s.text),
                )
    for seg in _segs:
        if seg.kind == "html":
            chunk = seg.text
            if not chunk:
                continue
            if not chunk.strip():
                model.getText().insertString(cursor, chunk, False)
                _cursor_goto_document_end(model, cursor)
                continue
            sub = _ensure_html_linebreaks(chunk)
            _insert_starwriter_html_at_cursor(
                model, cursor, sub, config_svc=config_svc
            )
            continue
        res = convert_mathml_to_starmath(ctx, seg.text)
        if res.ok and res.starmath and log.isEnabledFor(logging.DEBUG):
            log.debug(
                "mixed_html_math: StarMath from converter nl=%d len=%d repr=%r",
                res.starmath.count("\n"),
                len(res.starmath),
                res.starmath[:500],
            )
        if res.ok and res.starmath:
            insert_writer_math_formula(
                model,
                cursor,
                res.starmath,
                display_block=seg.display_block,
            )
            _cursor_goto_document_end(model, cursor)
        else:
            snippet = (seg.text or "").replace("\n", " ")[:120]
            fallback = "[Math import failed] " + snippet
            model.getText().insertString(cursor, fallback, False)
            _cursor_goto_document_end(model, cursor)
            log.debug(
                "math import failed: %s snippet=%r",
                res.error_message,
                snippet,
            )


def _insert_mixed_or_plain_html(model, ctx, cursor, unescaped_content, config_svc=None):
    """HTML import, with an optional MathML preprocessing layer."""
    if html_fragment_contains_mathml(unescaped_content):
        _insert_mixed_html_and_math_at_cursor(
            model, ctx, cursor, unescaped_content, config_svc=config_svc
        )
    else:
        single = _ensure_html_linebreaks(unescaped_content)
        _insert_starwriter_html_at_cursor(
            model, cursor, single, config_svc=config_svc
        )


def insert_content_at_position(model, ctx, content, position,
                               config_svc=None):
    """Insert formatted content at *position* (``'beginning'``,
    ``'end'``, or ``'selection'``) using ``insertDocumentFromURL``.
    """
    content = html_mod.unescape(content)

    text = model.getText()
    cursor = text.createTextCursor()

    if position == "beginning":
        cursor.gotoStart(False)
    elif position == "end":
        cursor.gotoEnd(False)
    elif position == "selection":
        try:
            controller = model.getCurrentController()
            sel = controller.getSelection()
            if sel and sel.getCount() > 0:
                rng = sel.getByIndex(0)
                rng.setString("")
                cursor.gotoRange(rng.getStart(), False)
            else:
                vc = controller.getViewCursor()
                cursor.gotoRange(vc.getStart(), False)
        except Exception:
            cursor.gotoEnd(False)
    else:
        raise ToolExecutionError("Unknown position: %s" % position)

    _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc)


def replace_full_document(model, ctx, content, config_svc=None):
    """Clear the document and insert *content*."""
    content = html_mod.unescape(content)

    text = model.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    cursor.gotoStart(False)
    _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc)


def apply_content_at_range(model, ctx, content, start, end,
                           config_svc=None):
    """Replace character range ``[start, end)`` with rendered *content*."""

    cursor = get_text_cursor_at_range(model, start, end)
    if cursor is None:
        raise ToolExecutionError(
            "Invalid range or could not create cursor for (%d, %d)" % (start, end)
        )

    content = html_mod.unescape(content)
    cursor.setString("")
    _insert_mixed_or_plain_html(model, ctx, cursor, content, config_svc=config_svc)


def apply_content_at_search(model, ctx, content, search,
                            all_matches=False, case_sensitive=True,
                            config_svc=None):
    """Find *search* in the document and replace with rendered *content*.

    Returns the number of replacements made.
    """
    prepared = html_mod.unescape(content)

    sd = model.createSearchDescriptor()
    sd.SearchString = search
    sd.SearchRegularExpression = False
    sd.SearchCaseSensitive = case_sensitive

    count = 0
    found = model.findFirst(sd)
    while found:
        text_obj = found.getText()
        cursor = text_obj.createTextCursorByRange(found)
        cursor.setString("")
        _insert_mixed_or_plain_html(model, ctx, cursor, prepared, config_svc=config_svc)
        count += 1
        if not all_matches:
            break
        found = model.findNext(cursor.getEnd(), sd)
        if count > 200:
            break
    return count


def replace_single_range_with_content(model, text_range, content, ctx,
                                      config_svc=None):
    """Replace the given text range with rendered *content* (HTML path)."""
    prepared = html_mod.unescape(content)
    text_obj = text_range.getText()
    cursor = text_obj.createTextCursorByRange(text_range)
    cursor.setString("")
    _insert_mixed_or_plain_html(model, ctx, cursor, prepared, config_svc=config_svc)


def _preserving_search_replace(model, uno_ctx, new_text, search_string,
                               all_matches=False, case_sensitive=True):
    """Find *search_string* and replace with *new_text* using format-preserving
    character-by-character replacement. Returns the number of replacements.
    """
    sd = model.createSearchDescriptor()
    sd.SearchString = search_string
    sd.SearchRegularExpression = False
    sd.SearchCaseSensitive = case_sensitive

    count = 0
    found = model.findFirst(sd)
    while found:
        replace_preserving_format(model, found, new_text, uno_ctx)
        count += 1
        if not all_matches:
            break
        found = model.findFirst(sd)
        if count > 200:
            break
    return count


# ---------------------------------------------------------------------------
# Text search
# ---------------------------------------------------------------------------

def find_text_ranges(model, ctx, search, start=0, limit=None,
                     case_sensitive=True):
    """Find occurrences of *search*, returning a list of
    ``{"start": int, "end": int, "text": str}`` dicts.
    """
    try:
        sd = model.createSearchDescriptor()
        sd.SearchString = search
        sd.SearchRegularExpression = False
        sd.SearchCaseSensitive = case_sensitive

        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        if start > 0:
            _GO_RIGHT_CHUNK = 8192
            remaining = start
            while remaining > 0:
                n = min(remaining, _GO_RIGHT_CHUNK)
                cursor.goRight(n, False)
                remaining -= n

        matches = []
        found = model.findNext(cursor, sd)
        while found:
            measure = found.getText().createTextCursor()
            measure.gotoStart(False)
            measure.gotoRange(found.getStart(), True)
            m_start = len(measure.getString())
            matched_text = found.getString()
            m_end = m_start + len(matched_text)
            matches.append({
                "start": m_start,
                "end": m_end,
                "text": matched_text,
            })
            if limit and len(matches) >= limit:
                break
            found = model.findNext(found, sd)
        return matches
    except Exception as exc:
        log.debug("find_text_ranges failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Markup detection & format-preserving replacement
# ---------------------------------------------------------------------------

_MARKUP_PATTERNS = [
    # Markdown
    "**", "__", "``", "# ", "## ", "### ", "| ", "|---", "- [ ]",
    # HTML
    "<b>", "<i>", "<p>", "<h1", "<h2", "<h3", "<table", "<tr", "<td",
    "<ul>", "<ol>", "<li>", "<div", "<span", "<br", "<img",
    "<strong", "<em>", "</",
    "<html", "<body", "<!DOCTYPE",
    "<math",
]


def content_has_markup(content):
    """Return ``True`` if *content* appears to contain Markdown or HTML."""
    if not content or not isinstance(content, str):
        return False
    lower = content.lower()
    return any(p.lower() in lower for p in _MARKUP_PATTERNS)


def replace_preserving_format(model, target_range, new_text, ctx=None):
    """Replace text in *target_range* with *new_text* character by
    character, preserving per-character formatting (bold, italic,
    font, color, etc.).
    """
    text = model.getText()
    old_text = _normalize(target_range.getString())
    new_text = _normalize(new_text)
    old_len = len(old_text)
    new_len = len(new_text)

    if old_len == 0 and new_len == 0:
        return
    if old_len == 0:
        cursor = text.createTextCursorByRange(target_range.getStart())
        text.insertString(cursor, new_text, False)
        return

    overlap = min(old_len, new_len)

    # Optional toolkit for UI responsiveness.
    toolkit = None
    if ctx:
        try:
            toolkit = ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", ctx
            )
        except Exception:
            pass

    # Process overlapping characters one by one.
    # setString on a selected character preserves the range's formatting.
    main_cursor = text.createTextCursorByRange(target_range.getStart())

    for i in range(overlap):
        if i > 0 and i % 500 == 0 and toolkit:
            try:
                toolkit.processEvents()
            except Exception:
                toolkit = None

        # Create a selection for exactly one character to check/replace.
        sel = text.createTextCursorByRange(main_cursor)
        if not sel.goRight(1, True):
            break

        if new_text[i] != old_text[i]:
            sel.setString(new_text[i])

        # Explicitly move main_cursor to the end of the character just processed.
        # This is more robust than goRight(1) because setString() can affect
        # the cursor's logical position in some environments.
        main_cursor.gotoRange(sel.getEnd(), False)

    # Handle length changes.
    if new_len > old_len:
        # Extra chars inherit formatting from the predecessor.
        text.insertString(main_cursor, new_text[old_len:], False)
    elif old_len > new_len:
        # Delete remaining original characters.
        # Ensure we don't go out of bounds of the original target_range.
        remaining_to_del = old_len - new_len
        del_cursor = text.createTextCursorByRange(main_cursor)
        # Use chunks for deletion just in case it's large.
        while remaining_to_del > 0:
            n = min(remaining_to_del, 8192)
            del_cursor.goRight(n, True)
            remaining_to_del -= n
        del_cursor.setString("")
