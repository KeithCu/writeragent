"""Writer comment / annotation tools."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.ops import find_paragraph_for_range

log = logging.getLogger("writeragent.writer")


class ListComments(ToolBase):
    """List all comments (annotations) in the document."""

    name = "list_comments"
    intent = "review"
    description = (
        "List all comments/annotations in the document, including "
        "author, content, date, resolved status, and anchor preview. "
        "Use author_filter to see only a specific agent's comments."
    )
    parameters = {
        "type": "object",
        "properties": {
            "author_filter": {
                "type": "string",
                "description": (
                    "Filter by author name (e.g. 'Claude', 'AI'). "
                    "Case-insensitive substring match. Omit for all."
                ),
            },
        },
        "required": [],
    }
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        author_filter = kwargs.get("author_filter")
        doc = ctx.doc
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()

        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        comments = []

        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService(
                "com.sun.star.text.textfield.Annotation"
            ):
                continue

            entry = _read_annotation(field, para_ranges, text_obj)

            if author_filter:
                af = author_filter.lower()
                if af not in entry.get("author", "").lower():
                    continue

            comments.append(entry)

        result = {"status": "ok", "comments": comments, "count": len(comments)}
        if author_filter:
            result["filtered_by"] = author_filter
        return result


class AddComment(ToolBase):
    """Add a comment anchored to a paragraph."""

    name = "add_comment"
    intent = "review"
    description = (
        "Add a comment/annotation. Anchor via search_text, locator, "
        "or paragraph_index. Use your AI name as author for multi-agent "
        "collaboration."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The comment text.",
            },
            "search_text": {
                "type": "string",
                "description": "Anchor the comment to text containing this string.",
            },
            "locator": {
                "type": "string",
                "description": (
                    "Locator: 'paragraph:N', 'bookmark:_mcp_x', "
                    "'heading_text:Title', etc."
                ),
            },
            "paragraph_index": {
                "type": "integer",
                "description": "Paragraph index to anchor to (0-based).",
            },
            "author": {
                "type": "string",
                "description": "Author name shown on the comment. Default: AI.",
            },
        },
        "required": ["content"],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        content = kwargs.get("content", "")
        search_text = kwargs.get("search_text")
        locator = kwargs.get("locator")
        para_index = kwargs.get("paragraph_index")
        author = kwargs.get("author", "AI")

        if not content:
            return {"status": "error", "message": "content is required."}

        doc = ctx.doc
        doc_text = doc.getText()

        # Determine anchor position
        anchor_range = None

        if search_text:
            sd = doc.createSearchDescriptor()
            sd.SearchString = search_text
            sd.SearchRegularExpression = False
            found = doc.findFirst(sd)
            if found is None:
                return {
                    "status": "not_found",
                    "message": "Text '%s' not found." % search_text,
                }
            anchor_range = found.getStart()
        elif locator is not None or para_index is not None:
            if locator is not None and para_index is None:
                doc_svc = ctx.services.document
                resolved = doc_svc.resolve_locator(doc, locator)
                para_index = resolved.get("para_index")
            if para_index is not None:
                doc_svc = ctx.services.document
                para_ranges = doc_svc.get_paragraph_ranges(doc)
                if 0 <= para_index < len(para_ranges):
                    anchor_range = para_ranges[para_index].getStart()
                else:
                    return {"status": "error",
                            "message": "Paragraph %d out of range." % para_index}
        else:
            return {"status": "error",
                    "message": "Provide search_text, locator, or paragraph_index."}

        annotation = doc.createInstance(
            "com.sun.star.text.textfield.Annotation"
        )
        annotation.setPropertyValue("Author", author)
        annotation.setPropertyValue("Content", content)
        cursor = doc_text.createTextCursorByRange(anchor_range)
        doc_text.insertTextContent(cursor, annotation, False)

        return {"status": "ok", "message": "Comment added.", "author": author}


class DeleteComment(ToolBase):
    """Delete comments by name or author."""

    name = "delete_comment"
    intent = "review"
    description = (
        "Delete comments by name or author. "
        "Use comment_name to delete a specific comment and its replies. "
        "Use author to delete ALL comments by that author "
        "(e.g. 'MCP-BATCH', 'MCP-WORKFLOW')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "comment_name": {
                "type": "string",
                "description": "The 'name' field returned by list_comments.",
            },
            "author": {
                "type": "string",
                "description": (
                    "Delete ALL comments by this author "
                    "(e.g. 'MCP-BATCH', 'MCP-WORKFLOW')."
                ),
            },
        },
        "required": [],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        comment_name = kwargs.get("comment_name")
        author = kwargs.get("author")

        if not comment_name and not author:
            return {"status": "error",
                    "message": "Provide comment_name or author."}

        doc = ctx.doc
        text_obj = doc.getText()
        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        to_delete = []
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService(
                "com.sun.star.text.textfield.Annotation"
            ):
                continue
            try:
                name = field.getPropertyValue("Name")
                parent = field.getPropertyValue("ParentName")
                field_author = field.getPropertyValue("Author")
            except Exception:
                continue

            if comment_name and (name == comment_name
                                 or parent == comment_name):
                to_delete.append(field)
            elif author and field_author == author:
                to_delete.append(field)

        for field in to_delete:
            text_obj.removeTextContent(field)

        return {
            "status": "ok",
            "deleted": len(to_delete),
        }


class ResolveComment(ToolBase):
    """Resolve a comment with an optional reason."""

    name = "resolve_comment"
    intent = "review"
    description = (
        "Resolve a comment with an optional reason. Adds a reply "
        "with the resolution text, then marks as resolved."
    )
    parameters = {
        "type": "object",
        "properties": {
            "comment_name": {
                "type": "string",
                "description": "The 'name' field returned by list_comments.",
            },
            "resolution": {
                "type": "string",
                "description": "Optional resolution text added as a reply.",
            },
            "author": {
                "type": "string",
                "description": "Author name for the resolution reply. Default: AI.",
            },
        },
        "required": ["comment_name"],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        comment_name = kwargs.get("comment_name", "")
        resolution = kwargs.get("resolution", "")
        author = kwargs.get("author", "AI")

        if not comment_name:
            return {"status": "error", "message": "comment_name is required."}

        doc = ctx.doc
        doc_text = doc.getText()
        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        target = None
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService(
                "com.sun.star.text.textfield.Annotation"
            ):
                continue
            try:
                name = field.getPropertyValue("Name")
            except Exception:
                continue
            if name == comment_name:
                target = field
                break

        if target is None:
            return {
                "status": "not_found",
                "message": "Comment '%s' not found." % comment_name,
            }

        if resolution:
            reply = doc.createInstance(
                "com.sun.star.text.textfield.Annotation"
            )
            reply.setPropertyValue("ParentName", comment_name)
            reply.setPropertyValue("Content", resolution)
            reply.setPropertyValue("Author", author)
            anchor = target.getAnchor()
            cursor = doc_text.createTextCursorByRange(anchor.getStart())
            doc_text.insertTextContent(cursor, reply, False)

        target.setPropertyValue("Resolved", True)

        return {
            "status": "ok",
            "comment_name": comment_name,
            "resolved": True,
        }


_WORKFLOW_TASK_PREFIXES = ("TODO-AI", "FIX", "QUESTION", "VALIDATION", "NOTE")


class Workflow(ToolBase):
    """Single tool for workflow/task operations: scan tasks, get/set status, check stop conditions."""

    name = "workflow"
    intent = "review"
    description = (
        "Workflow and task operations. action: scan_tasks (find TODO-AI, FIX, etc. in comments), "
        "get_status (read MCP-WORKFLOW dashboard), set_status (write key: value lines), "
        "check_stop (detect STOP/CANCEL comments or workflow stop/pause)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["scan_tasks", "get_status", "set_status", "check_stop"],
                "description": "Operation to perform.",
            },
            "unresolved_only": {
                "type": "boolean",
                "description": "For scan_tasks: only unresolved tasks (default true).",
            },
            "prefix_filter": {
                "type": "string",
                "enum": ["TODO-AI", "FIX", "QUESTION", "VALIDATION", "NOTE"],
                "description": "For scan_tasks: filter by task prefix.",
            },
            "content": {
                "type": "string",
                "description": "For set_status: workflow status as key: value lines.",
            },
        },
        "required": ["action"],
    }
    doc_types = ["writer"]
    is_mutation = True  # set_status mutates

    def execute(self, ctx, **kwargs):
        action = kwargs.get("action")
        if action not in ("scan_tasks", "get_status", "set_status", "check_stop"):
            return {"status": "error", "message": "Invalid action: %s" % action}

        if action == "scan_tasks":
            return self._scan_tasks(ctx, kwargs)
        if action == "get_status":
            return self._get_status(ctx)
        if action == "set_status":
            return self._set_status(ctx, kwargs)
        return self._check_stop(ctx)

    def _scan_tasks(self, ctx, kwargs):
        unresolved_only = kwargs.get("unresolved_only", True)
        prefix_filter = kwargs.get("prefix_filter", None)
        doc = ctx.doc
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        tasks = []
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                content = field.getPropertyValue("Content")
            except Exception:
                continue
            matched_prefix = None
            for prefix in _WORKFLOW_TASK_PREFIXES:
                if content.startswith(prefix):
                    matched_prefix = prefix
                    break
            if matched_prefix is None:
                continue
            if prefix_filter and matched_prefix != prefix_filter:
                continue
            if unresolved_only:
                try:
                    resolved = field.getPropertyValue("Resolved")
                except Exception:
                    resolved = False
                if resolved:
                    continue
            entry = _read_annotation(field, para_ranges, text_obj)
            entry["prefix"] = matched_prefix
            tasks.append(entry)
        return {"status": "ok", "tasks": tasks, "count": len(tasks)}

    def _get_status(self, ctx):
        doc = ctx.doc
        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                author = field.getPropertyValue("Author")
            except Exception:
                continue
            if author != "MCP-WORKFLOW":
                continue
            try:
                content = field.getPropertyValue("Content")
            except Exception:
                content = ""
            workflow = {}
            for line in content.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    workflow[key.strip()] = value.strip()
            return {"status": "ok", "workflow": workflow}
        return {"status": "ok", "workflow": None}

    def _set_status(self, ctx, kwargs):
        content = kwargs.get("content", "")
        if not content:
            return {"status": "error", "message": "content is required for set_status."}
        doc = ctx.doc
        doc_text = doc.getText()
        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        existing = None
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                author = field.getPropertyValue("Author")
            except Exception:
                continue
            if author == "MCP-WORKFLOW":
                existing = field
                break
        if existing is not None:
            existing.setPropertyValue("Content", content)
        else:
            annotation = doc.createInstance("com.sun.star.text.textfield.Annotation")
            annotation.setPropertyValue("Author", "MCP-WORKFLOW")
            annotation.setPropertyValue("Content", content)
            cursor = doc_text.createTextCursor()
            cursor.gotoStart(False)
            doc_text.insertTextContent(cursor, annotation, False)
        return {"status": "ok", "message": "Workflow status updated."}

    def _check_stop(self, ctx):
        doc = ctx.doc
        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        stop_signals = []
        workflow_stop = False
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                content = field.getPropertyValue("Content")
                author = field.getPropertyValue("Author")
                resolved = field.getPropertyValue("Resolved")
            except Exception:
                continue
            if author == "MCP-WORKFLOW" and content:
                lower = content.lower()
                if "stop" in lower or "pause" in lower:
                    workflow_stop = True
            if not resolved and content:
                upper = content.strip().upper()
                if upper.startswith("STOP") or upper.startswith("CANCEL"):
                    stop_signals.append({"author": author, "content": content[:100]})
        should_stop = bool(stop_signals) or workflow_stop
        return {
            "status": "ok",
            "should_stop": should_stop,
            "workflow_stop": workflow_stop,
            "stop_signals": stop_signals,
            "count": len(stop_signals),
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _read_annotation(field, para_ranges, text_obj):
    """Extract annotation properties into a plain dict."""
    entry = {}
    for prop, default in [
        ("Author", ""),
        ("Content", ""),
        ("Name", ""),
        ("ParentName", ""),
        ("Resolved", False),
    ]:
        try:
            entry[prop.lower() if prop != "ParentName" else "parent_name"] = (
                field.getPropertyValue(prop)
            )
        except Exception:
            key = prop.lower() if prop != "ParentName" else "parent_name"
            entry[key] = default

    # Date
    try:
        dt = field.getPropertyValue("DateTimeValue")
        entry["date"] = "%04d-%02d-%02d %02d:%02d" % (
            dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes
        )
    except Exception:
        entry["date"] = ""

    # Paragraph index and anchor preview.
    try:
        anchor = field.getAnchor()
        entry["paragraph_index"] = find_paragraph_for_range(
            anchor, para_ranges, text_obj
        )
        entry["anchor_preview"] = anchor.getString()[:80]
    except Exception:
        entry["paragraph_index"] = 0
        entry["anchor_preview"] = ""

    return entry

class AddAiSummary(ToolBase):
    name = "add_ai_summary"
    intent = "review"
    description = (
        "Add an AI-generated summary as a comment (MCP-AI author) "
        "at a heading or paragraph. Replaces any existing MCP-AI "
        "annotation at the same location."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": "Target location (e.g. 'bookmark:_mcp_xxx', 'paragraph:5')",
            },
            "para_index": {
                "type": "integer",
                "description": "Paragraph index (alternative to locator)",
            },
            "summary": {
                "type": "string",
                "description": "Summary text to add as annotation",
            },
        },
        "required": ["summary"],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        tree_svc = ctx.services.writer_tree
        para_index = kwargs.get("para_index")
        locator = kwargs.get("locator")
        if locator is not None and para_index is None:
            try:
                resolved = ctx.services.document.resolve_locator(ctx.doc, locator)
                para_index = resolved.get("para_index")
            except ValueError as e:
                return {"status": "error", "error": str(e)}
        if para_index is None:
            return {"status": "error", "error": "Provide locator or para_index"}
        try:
            result = tree_svc.add_ai_summary(ctx.doc, para_index, kwargs["summary"])
            return {"status": "ok", **result}
        except ValueError as e:
            return {"status": "error", "error": str(e)}


class GetAiSummaries(ToolBase):
    name = "get_ai_summaries"
    intent = "review"
    description = "List all MCP-AI summary annotations in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    doc_types = ["writer"]

    def execute(self, ctx, **kwargs):
        tree_svc = ctx.services.writer_tree
        summaries = tree_svc.get_ai_summaries(ctx.doc)
        return {"status": "ok", "summaries": summaries, "count": len(summaries)}


class RemoveAiSummary(ToolBase):
    name = "remove_ai_summary"
    intent = "review"
    description = "Remove an MCP-AI summary annotation at a paragraph."
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": "Target location (e.g. 'bookmark:_mcp_xxx')",
            },
            "para_index": {
                "type": "integer",
                "description": "Paragraph index (alternative to locator)",
            },
        },
        "required": [],
    }
    doc_types = ["writer"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        tree_svc = ctx.services.writer_tree
        para_index = kwargs.get("para_index")
        locator = kwargs.get("locator")
        if locator is not None and para_index is None:
            try:
                resolved = ctx.services.document.resolve_locator(ctx.doc, locator)
                para_index = resolved.get("para_index")
            except ValueError as e:
                return {"status": "error", "error": str(e)}
        if para_index is None:
            return {"status": "error", "error": "Provide locator or para_index"}
        removed = tree_svc.remove_ai_summary(ctx.doc, para_index)
        return {"status": "ok", "removed": removed, "para_index": para_index}
