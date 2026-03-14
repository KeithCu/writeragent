from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.ops import (
    get_text_cursor_at_range,
    get_selection_range,
    insert_html_at_cursor
)

class ApplyDocumentContent(ToolBase):
    """Apply HTML content to the document at a specific target location."""

    name = "apply_document_content"
    description = "Apply HTML content to the document. Targets: 'full', 'range', 'search', 'beginning', 'end', 'selection'."
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The HTML content to insert."},
            "target": {
                "type": "string",
                "enum": ["full", "range", "search", "beginning", "end", "selection"],
                "default": "selection"
            },
            "start": {"type": "integer", "description": "Start offset for 'range' target."},
            "end": {"type": "integer", "description": "End offset for 'range' target."},
            "search_text": {"type": "string", "description": "Text to find and replace for 'search' target."}
        },
        "required": ["content"]
    }
    doc_types = ["writer"]

    def execute(self, context, content, target="selection", start=None, end=None, search_text=None):
        doc = context.doc
        text = doc.getText()
        cursor = None

        if target == "full":
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)
        elif target == "beginning":
            cursor = text.createTextCursor()
            cursor.gotoStart(False)
        elif target == "end":
            cursor = text.createTextCursor()
            cursor.gotoEnd(False)
        elif target == "selection":
            sel_start, sel_end = get_selection_range(doc)
            cursor = get_text_cursor_at_range(doc, sel_start, sel_end)
        elif target == "range":
            if start is not None and end is not None:
                cursor = get_text_cursor_at_range(doc, start, end)
        elif target == "search" and search_text:
            descriptor = doc.createSearchDescriptor()
            descriptor.SearchString = search_text
            found = doc.findFirst(descriptor)
            if found:
                cursor = text.createTextCursorByRange(found)

        if cursor:
            # Clear existing content if target implies replacement
            if target in ["full", "range", "search", "selection"]:
                cursor.setString("")
            
            success = insert_html_at_cursor(cursor, content)
            return {"success": success, "target": target}
        
        return {"success": False, "error": "Target not found or invalid parameters"}
