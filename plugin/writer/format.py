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
"""FormatService — document format conversions (markdown/HTML <-> UNO)."""

import contextlib
import html as html_mod
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request
from typing import Any, cast

import uno
from plugin.framework.errors import ToolExecutionError
from plugin.framework.service import ServiceBase
from plugin.framework.uno_context import get_desktop
from plugin.doc.document_helpers import normalize_linebreaks as _normalize
from .math.html_math_segment import html_fragment_contains_mixed_math, segment_html_with_mixed_math
from .math.math_mml_convert import convert_latex_to_starmath, convert_mathml_to_starmath, insert_writer_math_formula
from .ops import get_selection_range

log = logging.getLogger("writeragent.writer")


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


class FormatService(ServiceBase):
    """Handles exporting and importing documents in various formats.

    Writer documents can be exported to Markdown/HTML, modified by the
    LLM, then imported back. This service wraps the temp-file dance.
    """

    name = "format"

    def export_as_text(self, model, max_chars=None):
        """Export document content as plain text."""
        from plugin.framework.errors import UnoObjectError, safe_call

        try:
            text = safe_call(model.getText, "Get document text")
            cursor = safe_call(text.createTextCursor, "Create text cursor")
            safe_call(cursor.gotoStart, "Cursor gotoStart", False)
            safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
            content = safe_call(cursor.getString, "Cursor getString")
            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + "\n\n[... truncated ...]"
            return content
        except UnoObjectError:
            log.exception("export_as_text failed")
            return ""

    def export_as_html(self, model):
        """Export the document as HTML via UNO filter.

        Returns:
            HTML string, or empty string on error.
        """
        from plugin.framework.errors import UnoObjectError, safe_call

        tmp_path = None
        try:
            import uno
            from com.sun.star.beans import PropertyValue

            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
                tmp_path = tmp.name

            url = uno.systemPathToFileUrl(tmp_path)
            props = (PropertyValue("FilterName", 0, "HTML (StarWriter)", 0), PropertyValue("Overwrite", 0, True, 0))
            safe_call(model.storeToURL, "Store document to HTML", url, props)

            with open(tmp_path, "r", encoding="utf-8") as f:
                html = f.read()
            os.unlink(tmp_path)
            return html
        except (UnoObjectError, Exception):
            log.exception("export_as_html failed")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return ""

    def import_from_html(self, model, html):
        """Replace document content with HTML by importing from a temp file.

        Returns:
            True on success, False on error.
        """
        from plugin.framework.errors import safe_call

        tmp_path = None
        try:
            import uno
            from com.sun.star.beans import PropertyValue

            with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as tmp:
                tmp.write(html)
                tmp_path = tmp.name

            url = uno.systemPathToFileUrl(tmp_path)
            text = safe_call(model.getText, "Get document text")
            cursor = safe_call(text.createTextCursor, "Create text cursor")
            safe_call(cursor.gotoStart, "Cursor gotoStart", False)
            safe_call(cursor.gotoEnd, "Cursor gotoEnd", True)
            safe_call(cursor.insertDocumentFromURL, "Insert document from HTML", url, (PropertyValue("FilterName", 0, "HTML (StarWriter)", 0),))
            os.unlink(tmp_path)
            return True
        except Exception:
            log.exception("import_from_html failed")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return False


# ---------------------------------------------------------------------------
# UNO helpers (import inside functions to avoid import-time dependency)
# ---------------------------------------------------------------------------


def _file_url(path):
    """Return a ``file://`` URL for *path*."""
    return urllib.parse.urljoin("file:", urllib.request.pathname2url(os.path.abspath(path)))


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
    match = re.search(r"<body[^>]*>(.*?)</body>", html_string, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return html_string


def _wrap_html_fragment(html_content, extra_css=None):
    """Wrap an HTML fragment in a full document structure for LO's filter."""
    if not html_content or not isinstance(html_content, str):
        return html_content
    has_html = "<html" in html_content.lower() and "</html>" in html_content.lower()
    has_body = "<body" in html_content.lower() and "</body>" in html_content.lower()
    if has_html and has_body:
        return html_content
    head = '<meta charset="UTF-8">'
    if extra_css:
        head = '<meta charset="UTF-8">\n<style>%s</style>' % extra_css
    return '<!DOCTYPE html>\n<html>\n<head>\n%s\n</head>\n<body>\n%s\n</body>\n</html>' % (head, html_content)


def _ensure_html_linebreaks(content):
    """Convert newlines to ``<br>``/``<p>`` when content is plain text
    and the active format is HTML, so LO's filter preserves them.
    """
    if not isinstance(content, str) or not content:
        return content
    content = _normalize(content)
    unescaped = html_mod.unescape(content)
    html_tags = ["<p>", "<br>", "</h1>", "<h2>", "<h3>", "</ul>", "</li>", "</div>", "<html>"]
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
        temp_doc = desktop.loadComponentFromURL("private:factory/swriter", "_default", 0, load_props)
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
    except Exception:
        log.exception("html_to_plain_text failed")
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
        temp_doc = desktop.loadComponentFromURL("private:factory/swriter", "_default", 0, load_props)
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
                temp_text.insertControlCharacter(temp_cursor, _PARAGRAPH_BREAK, False)
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
    except Exception:
        log.exception("_range_to_content_via_temp_doc failed")
        return ""
    finally:
        if temp_doc is not None:
            try:
                temp_doc.close(True)
            except Exception:
                pass


def document_to_content(model, ctx, services, max_chars=None, scope="full", range_start=None, range_end=None):
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
        return _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc)

    if scope == "range":
        start = int(range_start) if range_start is not None else 0
        end = int(range_end) if range_end is not None else 0
        doc_len = services.document.get_document_length(model) if services else 0
        start = max(0, min(start, doc_len))
        end = min(end, doc_len)
        return _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc)

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
    except Exception:
        log.exception("document_to_content (full) failed")
        return ""


# ---------------------------------------------------------------------------
# Content -> Document
# ---------------------------------------------------------------------------


def _cursor_goto_document_end(model, cursor) -> None:
    """Move *cursor* to the end of the document body (``model.getText()``)."""
    end_c = model.getText().createTextCursor()
    end_c.gotoEnd(False)
    cursor.gotoRange(end_c.getStart(), False)


def insert_html_fragment_at_cursor(
    cursor,
    html_fragment: str,
    *,
    extra_css: str | None = None,
    wrap: bool = True,
    config_svc=None,
    model=None,
) -> None:
    """Import a fragment via the StarWriter HTML filter at *cursor*.

    When *wrap* is True, wraps bare fragments in a full HTML document.
    *extra_css* is injected into ``<head>`` (e.g. sidebar list margins).
    When *model* is provided, moves *cursor* to document end after import
    (needed for multi-segment Writer inserts).
    """
    prepared = _wrap_html_fragment(html_fragment, extra_css=extra_css) if wrap else html_fragment
    with _with_temp_buffer(prepared, config_svc) as (_path, file_url):
        filter_name, _ = _get_format_props(config_svc)
        filter_props = (_create_property_value("FilterName", filter_name),)
        cursor.insertDocumentFromURL(file_url, filter_props)
    if model is not None:
        _cursor_goto_document_end(model, cursor)


def _insert_starwriter_html_at_cursor(model, cursor, prepared_html, config_svc=None):
    """Import one HTML fragment through the StarWriter HTML filter at *cursor*."""
    insert_html_fragment_at_cursor(
        cursor, prepared_html, wrap=False, config_svc=config_svc, model=model
    )


def _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, unescaped: str, config_svc=None):
    """Insert alternating HTML (via filter) and math (MathML or TeX) as formula objects."""
    _segs = segment_html_with_mixed_math(unescaped)
    if log.isEnabledFor(logging.DEBUG) and html_fragment_contains_mixed_math(unescaped):
        _math_i = 0
        for _si, _s in enumerate(_segs):
            if _s.kind == "html":
                log.debug("mixed_html_math: segment[%d] html nl=%d len=%d", _si, _s.text.count("\n"), len(_s.text))
            else:
                _math_i += 1
                log.debug("mixed_html_math: segment[%d] %s#%d display_block=%s src_nl=%d src_len=%d", _si, _s.kind, _math_i, _s.display_block, _s.text.count("\n"), len(_s.text))
    for seg in _segs:
        if seg.kind == "html":
            chunk = seg.text
            if not chunk:
                continue
            if not chunk.strip():
                model.getText().insertString(cursor, chunk, False)
                _cursor_goto_document_end(model, cursor)
                continue

            # Expand literal \n and \t for plain HTML without math
            chunk = chunk.replace("\\n", "\n").replace("\\t", "\t")

            sub = _ensure_html_linebreaks(chunk)
            _insert_starwriter_html_at_cursor(model, cursor, sub, config_svc=config_svc)
            continue
        if seg.kind == "tex":
            res = convert_latex_to_starmath(ctx, seg.text, display_block=seg.display_block)
        else:
            res = convert_mathml_to_starmath(ctx, seg.text)
        if res.ok and res.starmath and log.isEnabledFor(logging.DEBUG):
            log.debug("mixed_html_math: StarMath from converter nl=%d len=%d repr=%r", res.starmath.count("\n"), len(res.starmath), res.starmath[:500])
        if res.ok and res.starmath:
            insert_writer_math_formula(model, cursor, res.starmath, display_block=seg.display_block)
            _cursor_goto_document_end(model, cursor)
        else:
            snippet = (seg.text or "").replace("\n", " ")[:120]
            fallback = "[Math import failed] " + snippet
            model.getText().insertString(cursor, fallback, False)
            _cursor_goto_document_end(model, cursor)
            log.debug("math import failed: %s snippet=%r", res.error_message, snippet)


def _insert_mixed_or_plain_html(model, ctx, cursor, unescaped_content, config_svc=None):
    """HTML import, with an optional MathML + TeX preprocessing layer."""
    if html_fragment_contains_mixed_math(unescaped_content):
        _insert_mixed_html_and_math_at_cursor(model, ctx, cursor, unescaped_content, config_svc=config_svc)
    else:
        # Expand literal \n and \t for plain HTML without math
        unescaped_content = unescaped_content.replace("\\n", "\n").replace("\\t", "\t")

        single = _ensure_html_linebreaks(unescaped_content)
        _insert_starwriter_html_at_cursor(model, cursor, single, config_svc=config_svc)


def insert_content_at_position(model, ctx, content, position, config_svc=None):
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
            if sel and hasattr(sel, "getCount") and sel.getCount() > 0:
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


def replace_single_range_with_content(model, text_range, content, ctx, config_svc=None):
    """Replace the given text range with rendered *content* (HTML path).

    FOLLOW-UP: cursor uses ``text_range.getText()`` but HTML import still calls
    ``_cursor_goto_document_end`` (body) in places — markup search-replace inside
    table cells / nested ``XText`` can raise the same RuntimeException as the
    plain-text bug fixed in ``replace_preserving_format``.
    """
    prepared = html_mod.unescape(content)
    text_obj = text_range.getText()

    # Preserve the target paragraph style for INLINE replacements. The StarWriter
    # HTML import resets the paragraph to a default body style, silently demoting
    # headings (e.g. "Heading 3" -> "Text body"). For inline-only content (no
    # block-level tags, no math), insert without jumping the cursor to the document
    # end so we can reapply the original paragraph style across the inserted range.
    inline_preserve = not _content_has_block_markup(prepared) and not html_fragment_contains_mixed_math(prepared)
    saved_style = None
    if inline_preserve:
        try:
            saved_style = text_obj.createTextCursorByRange(
                text_range.getStart()).getPropertyValue("ParaStyleName")
        except Exception:
            saved_style = None

    cursor = text_obj.createTextCursorByRange(text_range)
    cursor.setString("")

    if saved_style is not None:
        anchor = text_obj.createTextCursorByRange(cursor.getStart())
        # Insert the inline fragment RAW (do not route through _ensure_html_linebreaks:
        # it does not recognise <span> as HTML and would wrap it in <p>, creating an
        # extra body paragraph). model=None leaves the cursor at the end of the
        # INSERTED content (not the document end), so [anchor, cursor] bounds it.
        inline_html = prepared.replace("\\n", "\n").replace("\\t", "\t")
        insert_html_fragment_at_cursor(cursor, inline_html, wrap=False, config_svc=config_svc, model=None)
        try:
            restore = text_obj.createTextCursorByRange(anchor.getStart())
            restore.gotoRange(cursor.getEnd(), True)
            restore.setPropertyValue("ParaStyleName", saved_style)
        except Exception:
            log.debug("replace_single_range_with_content: could not restore ParaStyleName", exc_info=True)
    else:
        _insert_mixed_or_plain_html(model, ctx, cursor, prepared, config_svc=config_svc)


# ---------------------------------------------------------------------------
# Paragraph style apply (preserve direct Char* formatting)
# ---------------------------------------------------------------------------


# Other code paths that set ParaStyleName and may need apply_paragraph_style_preserving_direct_char
# (audit each call site before adopting — semantics differ):
#   plugin/writer/content.py  — CloneHeadingBlock style re-apply (~475)
#   plugin/writer/format.py   — replace_single_range_with_content style restore (~595)
#   plugin/notebook/writer_importer.py — resolved paragraph style on import (~621)
#   plugin/notebook/notebook_runner.py — output cell paragraph style (~177)
def apply_paragraph_style_preserving_direct_char(doc, cursor, style_name):
    """Set *cursor*'s ParaStyleName to *style_name* without wiping direct Char* formatting.

    LibreOffice resets hand-set Char* properties to the new paragraph style's defaults
    when ParaStyleName is set — across the WHOLE paragraph, even for a sub-range cursor.
    ``getPropertyState`` is unreliable at the text-portion level, so overrides are
    detected by VALUE (Char* differs from the paragraph's current style default).

    KNOWN LIMITATION: a direct override whose value equals the old style default is not
    captured; applying a style with a different default can change that property visibly.
    """

    def _expand_to_full_paragraphs(cur):
        try:
            text = cur.getText()
            start = text.createTextCursorByRange(cur.getStart())
            end = text.createTextCursorByRange(cur.getEnd())
            start.gotoStartOfParagraph(False)
            end.gotoEndOfParagraph(True)
            expanded = text.createTextCursorByRange(start.getStart())
            expanded.gotoRange(end.getEnd(), True)
            return expanded
        except Exception:
            return None

    def _capture_direct_char_overrides(capture_cursor):
        overrides = []
        try:
            para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        except Exception:
            para_styles = None
        try:
            READONLY = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.READONLY")
        except Exception:
            READONLY = 0
        try:
            para_enum = capture_cursor.createEnumeration()
        except Exception:
            return overrides
        _paras = 0
        while para_enum.hasMoreElements() is True and _paras < 200000:
            _paras += 1
            try:
                para = para_enum.nextElement()
            except Exception:
                break
            if not (hasattr(para, "supportsService") and para.supportsService("com.sun.star.text.Paragraph")):
                continue
            old_style = None
            if para_styles is not None:
                try:
                    old_style = para_styles.getByName(para.getPropertyValue("ParaStyleName"))
                except Exception:
                    old_style = None
            if old_style is None:
                continue
            try:
                portion_enum = para.createEnumeration()
            except Exception:
                continue
            _portions = 0
            while portion_enum.hasMoreElements() is True and _portions < 50000:
                _portions += 1
                try:
                    portion = portion_enum.nextElement()
                    if portion.getPropertyValue("TextPortionType") != "Text":
                        continue
                    portion_props = portion.getPropertySetInfo().getProperties()
                except Exception:
                    continue
                props = {}
                for p in portion_props:
                    name = p.Name
                    if not name.startswith("Char"):
                        continue
                    if READONLY and (p.Attributes & READONLY):
                        continue
                    try:
                        val = portion.getPropertyValue(name)
                    except Exception:
                        continue
                    try:
                        if val == old_style.getPropertyValue(name):
                            continue
                    except Exception:
                        continue
                    props[name] = val
                if props:
                    try:
                        pc = portion.getText().createTextCursorByRange(portion.getStart())
                        pc.gotoRange(portion.getEnd(), True)
                        overrides.append((pc, props))
                    except Exception:
                        continue
        return overrides

    capture_cursor = _expand_to_full_paragraphs(cursor) or cursor
    overrides = _capture_direct_char_overrides(capture_cursor)
    cursor.setPropertyValue("ParaStyleName", style_name)
    for pc, props in overrides:
        for name, val in props.items():
            try:
                pc.setPropertyValue(name, val)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Text search
# ---------------------------------------------------------------------------


def find_text_ranges(model, ctx, search, start=0, limit=None, case_sensitive=True):
    """Find occurrences of *search*, returning a list of
    ``{"start": int, "end": int, "text": str}`` dicts.

    FOLLOW-UP: seeds ``findNext`` from body text but measures each hit with
    ``found.getText()``; ``start``/``end`` are offsets within that nested text
    object, not guaranteed global document indices (tables, frames, etc.).
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
            matches.append({"start": m_start, "end": m_end, "text": matched_text})
            if limit and len(matches) >= limit:
                break
            found = model.findNext(found, sd)
        return matches
    except Exception:
        log.exception("find_text_ranges failed")
        return []


# ---------------------------------------------------------------------------
# Markup detection & format-preserving replacement
# ---------------------------------------------------------------------------

_MARKUP_PATTERNS = [
    # Markdown
    "**",
    "__",
    "``",
    "# ",
    "## ",
    "### ",
    "| ",
    "|---",
    "- [ ]",
    # HTML
    "<b>",
    "<i>",
    "<p>",
    "<h1",
    "<h2",
    "<h3",
    "<table",
    "<tr",
    "<td",
    "<ul>",
    "<ol>",
    "<li>",
    "<div",
    "<span",
    "<br",
    "<img",
    "<strong",
    "<em>",
    "</",
    "<html",
    "<body",
    "<!DOCTYPE",
    "<math",
    # TeX (so plain ``\\( … \\)`` / ``$$`` is not misclassified as format-preserving)
    "$$",
    "\\(",
    "\\[",
]


def content_has_markup(content):
    """Return ``True`` if *content* appears to contain Markdown or HTML."""
    if not content or not isinstance(content, str):
        return False
    lower = content.lower()
    return any(p.lower() in lower for p in _MARKUP_PATTERNS)


# Block-level HTML tags: their presence means the content defines its own
# paragraph structure, so we must NOT force the original paragraph style onto it.
_BLOCK_MARKUP_PATTERNS = [
    "<p>", "<p ", "<h1", "<h2", "<h3", "<h4", "<h5", "<h6",
    "<div", "<ul", "<ol", "<li", "<table", "<tr", "<td", "<th",
    "<blockquote", "<pre", "<hr", "<section", "<article",
]


def _content_has_block_markup(content):
    """Return ``True`` if *content* contains block-level HTML (paragraph-defining)."""
    if not content or not isinstance(content, str):
        return False
    lower = content.lower()
    return any(p in lower for p in _BLOCK_MARKUP_PATTERNS)


def replace_preserving_format(model, target_range, new_text, ctx=None):
    """Replace text in *target_range* with *new_text* character by
    character, preserving per-character formatting (bold, italic,
    font, color, etc.).

    Cursors are created on ``target_range.getText()`` (the cell, frame, or body
    ``XText`` that owns the range), not ``model.getText()``. The range must lie
    entirely within that text object.
    """
    # Use the range's OWN text object, not the document body. When target_range
    # lives inside a table cell, model.getText() (the body) is the wrong XText and
    # createTextCursorByRange() raises "End of content node doesn't have the proper
    # start node". target_range.getText() resolves to the cell (or body) correctly,
    # matching the markup path which already uses found.getText().
    text = target_range.getText()
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
            toolkit = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
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
