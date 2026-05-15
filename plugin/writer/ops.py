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
"""Writer helper operations used by tools.

Low-level UNO helpers for paragraph navigation, selection ranges,
and text cursor manipulation. Tools delegate here rather than
duplicating UNO details.
"""

import logging
from plugin.framework.errors import WriterError

log = logging.getLogger("writeragent.writer")


from plugin.doc.document_helpers import (
    find_paragraph_for_range as _find_para,
    get_selection_range as _get_sel_range,
    get_text_cursor_at_range as _get_cursor_range,
)

def find_paragraph_for_range(anchor, para_ranges, text_obj):
    """Return the 0-based paragraph index that contains *anchor*.
    Redirects to plugin.doc.document_helpers.find_paragraph_for_range.
    """
    if not anchor:
        raise WriterError("Anchor is None", code="WRITER_ANCHOR_NULL", details={"operation": "find_paragraph_for_range"})
    if not para_ranges:
        raise WriterError("Paragraph ranges list is empty or None", code="WRITER_PARA_RANGES_EMPTY", details={"operation": "find_paragraph_for_range"})
    if not text_obj:
        raise WriterError("Text object is None", code="WRITER_TEXT_OBJ_NULL", details={"operation": "find_paragraph_for_range"})
    
    return _find_para(anchor, para_ranges, text_obj)


def get_selection_range(model):
    """Return ``(start_offset, end_offset)`` character positions of the
    current selection (or cursor insertion point).
    """
    if not model:
        raise WriterError("Document model is None", code="WRITER_MODEL_NULL", details={"operation": "get_selection_range"})
    
    if not hasattr(model, "getCurrentController") or model.getCurrentController() is None:
        raise WriterError("Document controller is None", code="WRITER_CONTROLLER_NULL", details={"operation": "get_selection_range"})

    return _get_sel_range(model)




# goRight(nCount, bExpand) takes a short; max 32767 per call.
_GO_RIGHT_CHUNK = 8192


def insert_html_at_cursor(cursor, html_content):
    """Insert HTML content at the given cursor position.

    Uses the 'HTML (StarWriter)' filter to parse and insert content.
    """
    try:
        if not cursor:
            raise WriterError("Cursor is None", code="WRITER_CURSOR_NULL", details={"operation": "insert_html_at_cursor"})
        if not html_content:
            raise WriterError("HTML content is empty", code="WRITER_HTML_CONTENT_EMPTY", details={"operation": "insert_html_at_cursor"})

        import tempfile
        import os
        from com.sun.star.beans import PropertyValue

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
            tmp.write(html_content.encode("utf-8"))
            tmp_path = tmp.name

        try:
            file_url = "file://" + tmp_path.replace("\\", "/")
            props = (PropertyValue(Name="FilterName", Value="HTML (StarWriter)"),)
            cursor.insertDocumentFromURL(file_url, props)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return True
    except WriterError:
        raise
    except Exception as e:
        log.debug("insert_html_at_cursor: failed", exc_info=True)
        raise WriterError(f"Failed to insert HTML at cursor: {str(e)}", code="WRITER_HTML_INSERT_ERROR", details={"original_error": str(e), "error_type": type(e).__name__}) from e


def get_text_cursor_at_range(model, start, end):
    """Create a text cursor that selects the character range ``[start, end)``.
    """
    if not model:
        raise WriterError("Document model is None", code="WRITER_MODEL_NULL", details={"operation": "get_text_cursor_at_range"})
    if start is None or end is None:
        raise WriterError(f"Invalid start ({start}) or end ({end}) offset", code="WRITER_INVALID_OFFSETS", details={"operation": "get_text_cursor_at_range", "start": start, "end": end})
    if not hasattr(model, "getText") or model.getText() is None:
        raise WriterError("Text object not found in model", code="WRITER_TEXT_NOT_FOUND", details={"operation": "get_text_cursor_at_range"})

    cursor = _get_cursor_range(model, start, end)
    if cursor is None:
        raise WriterError(f"Failed to get text cursor at range [{start}, {end}]", code="WRITER_CURSOR_RANGE_ERROR", details={"start": start, "end": end})
    return cursor
