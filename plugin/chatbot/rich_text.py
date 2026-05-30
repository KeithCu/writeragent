# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""Shared rich-text formatting for the RichTextControl sidebar (hidden Writer HTML import)."""

import logging
import os
import re
import tempfile
from typing import Any, cast

log = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(
    r"<(?:"
    r"p[>\s/]"
    r"|br[\s/>]"
    r"|/h[1-6]"
    r"|ul[\s/>]"
    r"|ol[\s/>]"
    r"|li[\s/>]"
    r"|strong[\s/>]"
    r"|em[\s/>]"
    r"|code[\s/>]"
    r"|pre[\s/>]"
    r"|div[\s/>]"
    r"|table[\s/>]"
    r")",
    re.IGNORECASE,
)

# Legacy plain-sidebar prefix; append_rich_text adds "Assistant:" instead.
_LEGACY_AI_LABEL_RE = re.compile(r"^\s*AI:\s*", re.IGNORECASE)


def strip_legacy_ai_label(text: str) -> str:
    """Remove leading ``AI:`` from greeting/assistant text (avoid ``Assistant: AI:``)."""
    if not text:
        return text
    return _LEGACY_AI_LABEL_RE.sub("", text, count=1)


USER_COLOR = 0x2A6099
ASSISTANT_COLOR = 0x1E293B


def get_theme_colors(doc=None, style_window=None):
    """Retrieve theme-aware colors based on StyleSettings from *style_window* or *doc*'s frame.

    Returns (bg_color, user_color, assistant_color).
    """
    win = style_window
    if win is None and doc is not None:
        try:
            controller = doc.getCurrentController()
            if controller:
                frame = controller.getFrame()
                if frame:
                    win = frame.getContainerWindow()
        except Exception as e:
            log.debug("get_theme_colors: doc frame lookup failed: %s", e)
    try:
        if win and hasattr(win, "StyleSettings"):
            style_settings = win.StyleSettings
            if style_settings:
                field_color = getattr(style_settings, "FieldColor", 0xFFFFFF)
                if isinstance(field_color, int):
                    r = (field_color >> 16) & 0xFF
                    g = (field_color >> 8) & 0xFF
                    b = field_color & 0xFF
                    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b

                    if luminance < 128:
                        # Dark mode colors
                        return field_color, 0x60A5FA, 0xE2E8F0
                    else:
                        # Light mode colors
                        # Dynamically darken DialogColor slightly (by 6%) to create a beautiful, soft contrast
                        dialog_color = getattr(style_settings, "DialogColor", 0xEFF0F1)
                        if isinstance(dialog_color, int):
                            r = int(((dialog_color >> 16) & 0xFF) * 0.94)
                            g = int(((dialog_color >> 8) & 0xFF) * 0.94)
                            b = int((dialog_color & 0xFF) * 0.94)
                            light_bg = (r << 16) | (g << 8) | b
                            return light_bg, 0x2A6099, 0x1E293B
                        return 0xE0E1E2, 0x2A6099, 0x1E293B
    except Exception as e:
        log.debug("Failed to resolve theme colors from StyleSettings: %s", e)
    return 0xE0E1E2, 0x2A6099, 0x1E293B


def _tighten_list_indent(body_range):
    """Tighten indentation on list paragraphs within *body_range*.

    The HTML filter imports <ul>/<ol> as indented paragraphs using ParaLeftMargin
    (not Writer's NumberingRules mechanism). This function detects paragraphs with
    non-zero ParaLeftMargin and reduces them to tight values suitable for the
    narrow sidebar.
    """
    import uno
    try:
        enum = body_range.createEnumeration()
    except Exception as e:
        log.debug("_tighten_list_indent: createEnumeration failed: %s", e)
        return

    para_count = 0
    tightened = 0
    processed_levels = set()
    while enum.hasMoreElements():
        para = enum.nextElement()
        para_count += 1
        try:
            if not para.getPropertyValue("NumberingIsNumber"):
                continue
        except Exception:
            continue

        try:
            level = para.getPropertyValue("NumberingLevel")
            list_id = para.getPropertyValue("ListId")
        except Exception:
            continue

        key = (list_id, level)
        if key in processed_levels:
            continue
        processed_levels.add(key)

        try:
            rules = para.getPropertyValue("NumberingRules")
            props = list(rules.getByIndex(level))
            # Read the existing FirstLineOffset so we can position the bullet
            # with a small left gap while preserving the original bullet-to-text spacing
            flo = 0
            for p in props:
                if p.Name == "FirstLineOffset":
                    flo = p.Value
                    break
            for p in props:
                if p.Name == "LeftMargin":
                    log.debug("_tighten_list_indent: level=%d orig LeftMargin=%s text=%r", level, p.Value, para.getString()[:40])
                    p.Value = abs(flo) + 115 + level * 225
            any_props = uno.Any("[]com.sun.star.beans.PropertyValue", cast("Any", tuple(props)))  # type: ignore[attr-defined]
            uno.invoke(rules, "replaceByIndex", (level, any_props))
            para.NumberingRules = rules
            tightened += 1
        except Exception as e:
            log.debug("_tighten_list_indent: failed for level %d: %s", level, e)

    log.debug("_tighten_list_indent: scanned %d paragraphs, tightened %d", para_count, tightened)


def _insert_html_at_cursor(doc, cursor, html_fragment):
    """Import an HTML fragment into *doc* at *cursor* using Writer's HTML filter.

    Writes the fragment to a temp file and imports via ``insertDocumentFromURL``
    with the ``HTML (StarWriter)`` filter -- the same mechanism used by
    ``apply_document_content`` for document edits.
    """
    import uno
    from com.sun.star.beans import PropertyValue

    css = "ul, ol { margin-left: 0.2cm; padding-left: 0.3cm; }"
    wrapped = '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>' % (css, html_fragment)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as tmp:
            tmp.write(wrapped)
            tmp_path = tmp.name

        url = uno.systemPathToFileUrl(tmp_path)
        filter_props = (PropertyValue("FilterName", 0, "HTML (StarWriter)", 0),)
        cursor.insertDocumentFromURL(url, filter_props)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def append_rich_text(doc, text, role="assistant", style_window=None):
    """Append a complete message to a Writer document (hidden doc for RichTextControl copy).

    Inserts a bold, colored role prefix (``You:`` / ``Assistant:``) then
    imports *text* as HTML via Writer's StarWriter HTML filter so that
    ``<strong>``, ``<em>``, ``<code>``, ``<ul>`` etc. render natively.
    """
    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoEnd(False)

        _bg_color, user_color, assistant_color = get_theme_colors(doc, style_window=style_window)

        if text and text.strip():
            text = strip_legacy_ai_label(text) if role == "assistant" else text

        if text_obj.getString():
            text_obj.insertString(cursor, "\n\n", False)

        # Bold colored role prefix
        start_pos = cursor.getStart()
        prefix = "You: " if role == "user" else "Assistant: "
        text_obj.insertString(cursor, prefix, False)

        prefix_range = text_obj.createTextCursorByRange(start_pos)
        prefix_range.gotoRange(cursor.getStart(), True)
        prefix_range.CharHeight = 10.0
        prefix_range.CharWeight = 150.0  # BOLD
        prefix_range.CharColor = user_color if role == "user" else assistant_color

        # Body content via HTML import
        cursor.gotoEnd(False)
        cursor.CharWeight = 100.0  # Reset to normal after bold prefix
        pre_len = doc.CharacterCount

        if text and text.strip():
            looks_html = bool(_HTML_TAG_RE.search(text))
            log.debug("append_rich_text: looks_html=%s len=%d snippet=%r", looks_html, len(text), text[:120])

            used_html_import = False
            if looks_html:
                try:
                    _insert_html_at_cursor(doc, cursor, text)
                    used_html_import = True
                except Exception:
                    log.debug("HTML import failed, falling back to plain text insert")
                    cursor.gotoEnd(False)
                    text_obj.insertString(cursor, text, False)
            else:
                text_obj.insertString(cursor, text, False)

            # Build a range covering only the newly inserted content
            body_range = text_obj.createTextCursor()
            body_range.gotoStart(False)
            body_range.goRight(pre_len, False)
            body_range.gotoEnd(True)
            # Plain text (and HTML-import fallback) get the role tint; successful HTML import
            # keeps per-span CharColor from the filter (red/blue runs, etc.).
            if not used_html_import:
                body_range.CharColor = user_color if role == "user" else assistant_color
            _tighten_list_indent(body_range)

    except Exception as e:
        log.exception("Error in append_rich_text: %s", e)


def finalize_sidebar_assistant_response(listener) -> None:
    """Re-import the last assistant message as HTML when rich sidebar is active."""
    listener.rerender_rich_text_session()
