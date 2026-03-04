import json
from plugin.framework.logging import debug_log
from plugin.modules.core.services.document import get_paragraph_ranges, find_paragraph_for_range

def _err(message):
    return json.dumps({"status": "error", "message": message})

def tool_list_comments(model, ctx, args):
    """List all comments/annotations in the document."""
    try:
        fields = model.getTextFields()
        enum = fields.createEnumeration()
        para_ranges = get_paragraph_ranges(model)
        text_obj = model.getText()
        comments = []
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                author = field.getPropertyValue("Author")
            except Exception:
                author = ""
            content = ""
            try:
                content = field.getPropertyValue("Content")
            except Exception:
                pass
            name = ""
            parent_name = ""
            resolved = False
            try:
                name = field.getPropertyValue("Name")
            except Exception:
                pass
            try:
                parent_name = field.getPropertyValue("ParentName")
            except Exception:
                pass
            try:
                resolved = field.getPropertyValue("Resolved")
            except Exception:
                pass
            date_str = ""
            try:
                dt = field.getPropertyValue("DateTimeValue")
                date_str = "%04d-%02d-%02d %02d:%02d" % (
                    dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)
            except Exception:
                pass
            anchor = field.getAnchor()
            para_idx = find_paragraph_for_range(anchor, para_ranges, text_obj)
            anchor_preview = anchor.getString()[:80]
            entry = {
                "author": author,
                "content": content,
                "date": date_str,
                "resolved": resolved,
                "paragraph_index": para_idx,
                "anchor_preview": anchor_preview,
                "name": name,
                "parent_name": parent_name
            }
            comments.append(entry)
        return json.dumps({"status": "ok", "comments": comments,
                           "count": len(comments)})
    except Exception as e:
        debug_log("tool_list_comments error: %s" % e, context="Chat")
        return _err(str(e))

def tool_add_comment(model, ctx, args):
    """Add a comment to the paragraph containing the given search text."""
    content = args.get("content", "")
    author = args.get("author", "AI")
    search_text = args.get("search_text", "")
    if not content:
        return _err("content is required")
    if not search_text:
        return _err("search_text is required")
    try:
        doc_text = model.getText()
        sd = model.createSearchDescriptor()
        sd.SearchString = search_text
        sd.SearchRegularExpression = False
        found = model.findFirst(sd)
        if found is None:
            return json.dumps({"status": "not_found",
                               "message": "Text '%s' not found" % search_text})
        annotation = model.createInstance("com.sun.star.text.textfield.Annotation")
        annotation.setPropertyValue("Author", author)
        annotation.setPropertyValue("Content", content)
        cursor = doc_text.createTextCursorByRange(found.getStart())
        doc_text.insertTextContent(cursor, annotation, False)
        return json.dumps({"status": "ok", "message": "Comment added",
                           "author": author})
    except Exception as e:
        debug_log("tool_add_comment error: %s" % e, context="Chat")
        return _err(str(e))

def tool_delete_comment(model, ctx, args):
    """Delete a comment and all its replies by comment name."""
    comment_name = args.get("comment_name", "")
    if not comment_name:
        return _err("comment_name is required")
    try:
        fields = model.getTextFields()
        enum = fields.createEnumeration()
        text_obj = model.getText()
        to_delete = []
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                name = field.getPropertyValue("Name")
                parent = field.getPropertyValue("ParentName")
            except Exception:
                continue
            if name == comment_name or parent == comment_name:
                to_delete.append(field)
        for field in to_delete:
            text_obj.removeTextContent(field)
        return json.dumps({"status": "ok", "deleted": len(to_delete),
                           "comment_name": comment_name})
    except Exception as e:
        debug_log("tool_delete_comment error: %s" % e, context="Chat")
        return _err(str(e))

COMMENTS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_comments",
            "description": (
                "List all comments/annotations in the document, including author, content, "
                "date, resolved status, and the text they are anchored to."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_comment",
            "description": (
                "Add a comment/annotation anchored to the paragraph containing search_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The comment text.",
                    },
                    "search_text": {
                        "type": "string",
                        "description": "Anchor the comment to the paragraph containing this text.",
                    },
                    "author": {
                        "type": "string",
                        "description": "Author name shown on the comment. Default: AI.",
                    },
                },
                "required": ["content", "search_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_comment",
            "description": "Delete a comment and all its replies by the comment's name (from list_comments).",
            "parameters": {
                "type": "object",
                "properties": {
                    "comment_name": {
                        "type": "string",
                        "description": "The 'name' field of the comment returned by list_comments.",
                    },
                },
                "required": ["comment_name"],
                "additionalProperties": False,
            },
        },
    },
]
