"""FormatService — document format conversions (markdown/HTML <-> UNO)."""

import logging
import os
import tempfile

from plugin.framework.service_base import ServiceBase

log = logging.getLogger("localwriter.format")


class FormatService(ServiceBase):
    """Handles exporting and importing documents in various formats.

    Writer documents can be exported to Markdown/HTML, modified by the
    LLM, then imported back. This service wraps the temp-file dance.
    """

    name = "format"

    def export_as_text(self, model, max_chars=None):
        """Export document content as plain text."""
        try:
            text = model.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            content = cursor.getString()
            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + "\n\n[... truncated ...]"
            return content
        except Exception:
            log.exception("export_as_text failed")
            return ""

    def export_as_html(self, model):
        """Export the document as HTML via UNO filter.

        Returns:
            HTML string, or empty string on error.
        """
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
            model.storeToURL(url, props)

            with open(tmp_path, "r", encoding="utf-8") as f:
                html = f.read()
            os.unlink(tmp_path)
            return html
        except Exception:
            log.exception("export_as_html failed")
            return ""

    def import_from_html(self, model, html):
        """Replace document content with HTML by importing from a temp file.

        Returns:
            True on success, False on error.
        """
        try:
            import uno
            from com.sun.star.beans import PropertyValue

            with tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8"
            ) as tmp:
                tmp.write(html)
                tmp_path = tmp.name

            url = uno.systemPathToFileUrl(tmp_path)
            text = model.getText()
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
            cursor.insertDocumentFromURL(url, (
                PropertyValue("FilterName", 0, "HTML (StarWriter)", 0),
            ))
            os.unlink(tmp_path)
            return True
        except Exception:
            log.exception("import_from_html failed")
            return False
