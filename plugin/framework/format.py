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

import logging
import os
import tempfile

from plugin.framework.service_base import ServiceBase

log = logging.getLogger("writeragent.format")


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
        except UnoObjectError as e:
            log.error("export_as_text UnoObjectError: %s", e)
            return ""

    def export_as_html(self, model):
        """Export the document as HTML via UNO filter.

        Returns:
            HTML string, or empty string on error.
        """
        from plugin.framework.errors import UnoObjectError, safe_call

        try:
            import uno
            from com.sun.star.beans import PropertyValue

            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
                tmp_path = tmp.name

            url = uno.systemPathToFileUrl(tmp_path)
            props = (
                PropertyValue("FilterName", 0, "HTML (StarWriter)", 0),
                PropertyValue("Overwrite", 0, True, 0),
            )
            safe_call(model.storeToURL, "Store document to HTML", url, props)

            with open(tmp_path, "r", encoding="utf-8") as f:
                html = f.read()
            os.unlink(tmp_path)
            return html
        except OSError as e:
            log.error("export_as_html file error: %s", e)
            return ""
        except UnoObjectError as e:
            log.error("export_as_html UnoObjectError: %s", e)
            return ""

    def import_from_html(self, model, html):
        """Replace document content with HTML by importing from a temp file.

        Returns:
            True on success, False on error.
        """
        from plugin.framework.errors import UnoObjectError, safe_call

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
        except OSError as e:
            log.error("import_from_html file error: %s", e)
            return False
        except UnoObjectError as e:
            log.error("import_from_html UnoObjectError: %s", e)
            return False
