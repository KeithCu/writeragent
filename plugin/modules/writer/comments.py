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
"""Writer comment / annotation tools."""

import datetime
import logging
import uno

from plugin.framework.tool_base import ToolBase
from plugin.modules.writer.base import ToolWriterCommentBase
from plugin.modules.writer.ops import find_paragraph_for_range

log = logging.getLogger("writeragent.writer")


class ListComments(ToolWriterCommentBase):
    """List all comments (annotations) in the document."""

    name = "list_comments"
    intent = "review"
    description = "List all comments/annotations in the document, including author, content, date, resolved status, and anchor preview. Use author_filter to see only a specific agent's comments."
    parameters = {
        "type": "object",
        "properties": {
            "author_filter": {
                "type": "string",
                "description": ("Filter by author name (e.g. 'Claude', 'AI'). Case-insensitive substring match. Omit for all."),
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

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
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
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
    """Add a comment anchored to a search string."""

    name = "add_comment"
    intent = "review"
    description = "Add a comment/annotation. Anchor the comment to text matching search_text."
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
        },
        "required": ["content", "search_text"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        content = kwargs.get("content", "")
        search_text = kwargs.get("search_text")
        author = "WriterAgent"

        if not search_text:
            return self._tool_error("Provide search_text.")

        doc = ctx.doc
        doc_text = doc.getText()

        # Determine anchor position
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

        annotation = doc.createInstance("com.sun.star.text.textfield.Annotation")
        annotation.setPropertyValue("Author", author)
        annotation.setPropertyValue("Content", content)
        _set_annotation_date(annotation)
        cursor = doc_text.createTextCursorByRange(anchor_range)
        doc_text.insertTextContent(cursor, annotation, False)

        return {"status": "ok", "message": "Comment added.", "author": author}


class DeleteComment(ToolWriterCommentBase):
    """Delete comments by name or author."""

    name = "delete_comment"
    intent = "review"
    description = (
        "Delete comments by name or author. Use comment_name to delete a specific comment and its replies. Use author to delete ALL comments by that author (e.g. 'MCP-BATCH', 'MCP-WORKFLOW')."
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
                "description": ("Delete ALL comments by this author (e.g. 'MCP-BATCH', 'MCP-WORKFLOW')."),
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        comment_name = kwargs.get("comment_name")
        author = kwargs.get("author")

        if not comment_name and not author:
            return self._tool_error("Provide comment_name or author.")

        doc = ctx.doc
        text_obj = doc.getText()
        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        to_delete = []
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
                continue
            try:
                name = field.getPropertyValue("Name")
                parent = field.getPropertyValue("ParentName")
                field_author = field.getPropertyValue("Author")
            except Exception:
                continue

            if comment_name and (name == comment_name or parent == comment_name):
                to_delete.append(field)
            elif author and field_author == author:
                to_delete.append(field)

        for field in to_delete:
            text_obj.removeTextContent(field)

        return {
            "status": "ok",
            "deleted": len(to_delete),
        }


class ResolveComment(ToolWriterCommentBase):
    """Resolve a comment with an optional reason."""

    name = "resolve_comment"
    intent = "review"
    description = "Resolve a comment with an optional reason. Adds a reply with the resolution text, then marks as resolved."
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
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        comment_name = kwargs.get("comment_name", "")
        resolution = kwargs.get("resolution", "")
        author = kwargs.get("author", "AI")

        doc = ctx.doc
        doc_text = doc.getText()
        fields = doc.getTextFields()
        enum = fields.createEnumeration()

        target = None
        while enum.hasMoreElements():
            field = enum.nextElement()
            if not field.supportsService("com.sun.star.text.textfield.Annotation"):
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
            reply = doc.createInstance("com.sun.star.text.textfield.Annotation")
            reply.setPropertyValue("ParentName", comment_name)
            reply.setPropertyValue("Content", resolution)
            reply.setPropertyValue("Author", author)
            _set_annotation_date(reply)
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


class Workflow(ToolWriterCommentBase):
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
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True  # set_status mutates

    def execute(self, ctx, **kwargs):
        action = kwargs.get("action")
        if action not in ("scan_tasks", "get_status", "set_status", "check_stop"):
            return self._tool_error("Invalid action: %s" % action)

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
            _set_annotation_date(annotation)
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


def _set_annotation_date(annotation):
    """Set DateTimeValue (and Date) to now for a new annotation."""
    now = datetime.datetime.now()
    try:
        dt = uno.createUnoStruct("com.sun.star.util.DateTime")
        dt.Year = now.year  # type: ignore
        dt.Month = now.month  # type: ignore
        dt.Day = now.day  # type: ignore
        dt.Hours = now.hour  # type: ignore
        dt.Minutes = now.minute  # type: ignore
        dt.Seconds = now.second  # type: ignore
        annotation.setPropertyValue("DateTimeValue", dt)
    except Exception:
        pass
    try:
        d = uno.createUnoStruct("com.sun.star.util.Date")
        d.Year = now.year  # type: ignore
        d.Month = now.month  # type: ignore
        d.Day = now.day  # type: ignore
        annotation.setPropertyValue("Date", d)
    except Exception:
        pass


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
            entry[prop.lower() if prop != "ParentName" else "parent_name"] = field.getPropertyValue(prop)
        except Exception:
            key = prop.lower() if prop != "ParentName" else "parent_name"
            entry[key] = default

    # Date
    try:
        dt = field.getPropertyValue("DateTimeValue")
        entry["date"] = "%04d-%02d-%02d %02d:%02d" % (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)
    except Exception:
        entry["date"] = ""

    # Paragraph index and anchor preview.
    try:
        anchor = field.getAnchor()
        entry["paragraph_index"] = find_paragraph_for_range(anchor, para_ranges, text_obj)
        entry["anchor_preview"] = anchor.getString()[:80]
    except Exception:
        entry["paragraph_index"] = 0
        entry["anchor_preview"] = ""

    return entry
