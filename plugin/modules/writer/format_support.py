"""Format conversion helpers for Writer tools.

Handles exporting documents as markdown/HTML, importing formatted
content via ``insertDocumentFromURL``, format-preserving text
replacement, and text search utilities.
"""

import contextlib
import logging
import os
import re
import tempfile

log = logging.getLogger("localwriter.writer")


def _doc_text_length(doc):
    """Return the total character length of the document."""
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    return len(cursor.getString())


# ---------------------------------------------------------------------------
# Format configuration
# ---------------------------------------------------------------------------

FORMAT_CONFIG = {
    "markdown": {"filter": "Markdown", "extension": ".md"},
    "html": {"filter": "HTML (StarWriter)", "extension": ".html"},
}

# System temp directory (cross-platform).
TEMP_DIR = tempfile.gettempdir()


def _get_format(config_svc=None):
    """Return the active format name (``'html'`` or ``'markdown'``).

    Reads from the config service when available, defaults to ``'html'``.
    """
    if config_svc is not None:
        try:
            fmt = config_svc.get("core.document_format", caller_module=None)
            if fmt and fmt in FORMAT_CONFIG:
                return fmt
        except Exception:
            pass
    return "html"


def _get_format_props(config_svc=None):
    """Return ``(filter_name, file_extension)`` for the active format."""
    fmt = _get_format(config_svc)
    cfg = FORMAT_CONFIG.get(fmt, FORMAT_CONFIG["html"])
    return cfg["filter"], cfg["extension"]


# ---------------------------------------------------------------------------
# UNO helpers (import inside functions to avoid import-time dependency)
# ---------------------------------------------------------------------------

def _file_url(path):
    """Return a ``file://`` URL for *path*."""
    import urllib.parse
    import urllib.request
    return urllib.parse.urljoin(
        "file:", urllib.request.pathname2url(os.path.abspath(path))
    )


def _create_property_value(name, value):
    """Create a ``com.sun.star.beans.PropertyValue``."""
    import uno
    p = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
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
    import html as html_mod

    unescaped = html_mod.unescape(content)
    html_tags = [
        "<p>", "<br>", "</h1>", "</h2>", "</h3>",
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


# ---------------------------------------------------------------------------
# Document -> content
# ---------------------------------------------------------------------------

# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0


def _range_to_content_via_temp_doc(model, ctx, start, end, max_chars, config_svc):
    """Export a character range to content via a hidden temp document."""
    temp_doc = None
    try:
        smgr = ctx.getServiceManager()
        desktop = smgr.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx
        )
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
        fmt = _get_format(config_svc)
        with _with_temp_buffer(None, config_svc) as (path, file_url):
            props = (_create_property_value("FilterName", filter_name),)
            temp_doc.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        if fmt == "html":
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
    """Export a Writer document (or part of it) as markdown/HTML.

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
        from plugin.modules.writer.ops import get_selection_range
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
        fmt = _get_format(config_svc)
        with _with_temp_buffer(None, config_svc) as (path, file_url):
            props = (_create_property_value("FilterName", filter_name),)
            model.storeToURL(file_url, props)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if fmt == "html":
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

def insert_content_at_position(model, ctx, content, position,
                               config_svc=None):
    """Insert formatted content at *position* (``'beginning'``,
    ``'end'``, or ``'selection'``) using ``insertDocumentFromURL``.
    """
    fmt = _get_format(config_svc)
    if fmt == "html":
        import html as html_mod
        content = html_mod.unescape(content)
        content = _ensure_html_linebreaks(content)

    with _with_temp_buffer(content, config_svc) as (_path, file_url):
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
            raise ValueError("Unknown position: %s" % position)

        filter_name, _ = _get_format_props(config_svc)
        filter_props = (_create_property_value("FilterName", filter_name),)
        cursor.insertDocumentFromURL(file_url, filter_props)


def replace_full_document(model, ctx, content, config_svc=None):
    """Clear the document and insert *content*."""
    fmt = _get_format(config_svc)
    if fmt == "html":
        import html as html_mod
        content = html_mod.unescape(content)
        content = _ensure_html_linebreaks(content)

    with _with_temp_buffer(content, config_svc) as (_path, file_url):
        text = model.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.setString("")
        cursor.gotoStart(False)
        filter_name, _ = _get_format_props(config_svc)
        filter_props = (_create_property_value("FilterName", filter_name),)
        cursor.insertDocumentFromURL(file_url, filter_props)


def apply_content_at_range(model, ctx, content, start, end,
                           config_svc=None):
    """Replace character range ``[start, end)`` with rendered *content*."""
    from plugin.modules.writer.ops import get_text_cursor_at_range

    cursor = get_text_cursor_at_range(model, start, end)
    if cursor is None:
        raise ValueError(
            "Invalid range or could not create cursor for (%d, %d)" % (start, end)
        )

    fmt = _get_format(config_svc)
    if fmt == "html":
        import html as html_mod
        content = html_mod.unescape(content)
        content = _ensure_html_linebreaks(content)

    with _with_temp_buffer(content, config_svc) as (_path, file_url):
        cursor.setString("")
        filter_name, _ = _get_format_props(config_svc)
        filter_props = (_create_property_value("FilterName", filter_name),)
        cursor.insertDocumentFromURL(file_url, filter_props)


def apply_content_at_search(model, ctx, content, search,
                            all_matches=False, case_sensitive=True,
                            config_svc=None):
    """Find *search* in the document and replace with rendered *content*.

    Returns the number of replacements made.
    """
    fmt = _get_format(config_svc)
    prepared = content
    if fmt == "html":
        import html as html_mod
        prepared = html_mod.unescape(content)
        prepared = _ensure_html_linebreaks(prepared)

    with _with_temp_buffer(prepared, config_svc) as (_path, file_url):
        filter_name, _ = _get_format_props(config_svc)
        filter_props = (_create_property_value("FilterName", filter_name),)

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
            cursor.insertDocumentFromURL(file_url, filter_props)
            count += 1
            if not all_matches:
                break
            found = model.findNext(cursor.getEnd(), sd)
            if count > 200:
                break
        return count


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
    old_text = target_range.getString()
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

        if new_text[i] != old_text[i]:
            sel = text.createTextCursorByRange(main_cursor)
            sel.goRight(1, True)
            sel.setString(new_text[i])

        main_cursor.goRight(1, False)

    # Handle length changes.
    if new_len > old_len:
        # Extra chars inherit formatting from the predecessor.
        text.insertString(main_cursor, new_text[old_len:], False)
    elif old_len > new_len:
        # Delete remaining original characters.
        # Ensure we don't go out of bounds of the original target_range.
        # target_range might have shrunk if text was deleted, but we replace char-by-char.
        # We need to select from main_cursor to the end of where old_text was.
        # We know we advanced main_cursor by `new_len` positions.
        # The remaining length to delete is `old_len - new_len`.
        remaining_to_del = old_len - new_len
        del_cursor = text.createTextCursorByRange(main_cursor)
        for _ in range(remaining_to_del):
            del_cursor.goRight(1, True)
        del_cursor.setString("")
