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

from typing import Any, cast

from plugin.framework.constants import WORKFLOW_TASK_PREFIXES, now_aware
import logging
import uno

from plugin.framework.tool import ToolBase
from ..specialized_base import ToolWriterCommentBase

log = logging.getLogger("writeragent.writer")


class CommentList(ToolWriterCommentBase):
    """List all comments (annotations) in the document."""

    name = "comment_list"
    intent = "review"
    description = "List all comments/annotations in the document, including author, content, date, resolved status, and anchor preview. Use author_filter to see only a specific agent's comments."
    parameters = {"type": "object", "properties": {"author_filter": {"type": "string", "description": ("Filter by author name (e.g. 'Claude', 'AI'). Case-insensitive substring match. Omit for all.")}}, "required": []}
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

            entry = _read_annotation(field, para_ranges, text_obj, doc_svc)

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
    description = (
        "Add a comment/annotation anchored to text matching search. The comment is inserted "
        "at the start of the match (visible in Writer's Comments pane). Use occurrence to "
        "target a later match and author to sign it."
    )
    parameters = {"type": "object", "properties": {
        "content": {"type": "string", "description": "The comment text."},
        "search": {"type": "string", "description": "Anchor the comment to text matching this string."},
        "occurrence": {"type": "integer", "description": "0-based match to comment on when search repeats (default 0)."},
        "author": {"type": "string", "description": "Comment author (default 'WriterAgent')."},
    }, "required": ["content", "search"]}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        content = kwargs.get("content", "")
        search_text = kwargs.get("search")
        author = (kwargs.get("author") or "WriterAgent").strip() or "WriterAgent"

        if not search_text:
            return self._tool_error("Provide search.")
        try:
            occurrence = int(kwargs.get("occurrence", 0) or 0)
        except (TypeError, ValueError):
            return self._tool_error("occurrence must be an integer.")
        if occurrence < 0:
            return self._tool_error("occurrence must be non-negative.")

        doc = ctx.doc

        # Find the requested occurrence.
        sd = doc.createSearchDescriptor()
        sd.SearchString = search_text
        sd.SearchRegularExpression = False
        found = doc.findFirst(sd)
        for _unused in range(occurrence):
            if found is None:
                break
            found = doc.findNext(found.getEnd(), sd)
        if found is None:
            # status="error" (not "not_found"): an anchor miss is a failure, so the chat FSM and
            # MCP host don't treat a no-op as success. anchor_text is returned on success only.
            where = (" at occurrence %d" % occurrence) if occurrence else ""
            return {"status": "error", "message": "Text '%s' not found%s." % (search_text, where), "matched": False, "comment_added": False}

        annotation = doc.createInstance("com.sun.star.text.textfield.Annotation")
        annotation.setPropertyValue("Author", author)
        annotation.setPropertyValue("Content", content)
        _set_annotation_date(annotation)
        # Point insert at the match start (absorb=False), in found.getText() so table
        # cells / frames work. PR #365 Q15 tried to SPAN the passage with absorb=True
        # over found so Writer would highlight which text the comment covers. That is
        # the wrong API: insertTextContent(..., absorb=True) REPLACES the range with
        # the Annotation field (a point PostIt). Writer then draws no balloon, and
        # the matched text can disappear, while the tool still returns comment_added.
        # A real range comment needs a different path — select the match, then
        # dispatch .uno:InsertAnnotation (what Insert > Comment uses on a selection),
        # or ODF annotation-start / annotation-end marks. TODO: research that if we
        # want spanning highlights later. Do not go back to absorb=True.
        anchor_text = ""
        try:
            anchor_text = found.getString()
        except Exception:
            pass
        before = _count_annotations(doc)
        match_text = found.getText()
        cursor = match_text.createTextCursorByRange(found.getStart())
        match_text.insertTextContent(cursor, annotation, False)
        if _count_annotations(doc) <= before:
            return {
                "status": "error",
                "message": "Comment insert did not register an annotation.",
                "matched": True,
                "comment_added": False,
                "anchor_text": anchor_text or search_text,
            }

        return {"status": "ok", "message": "Comment added.", "author": author, "matched": True, "comment_added": True, "anchor_text": anchor_text or search_text}


class CommentDelete(ToolWriterCommentBase):
    """Delete comments by name or author."""

    name = "comment_delete"
    intent = "review"
    description = "Delete comments by name or author. Use name to delete a specific comment and its replies. Use author to delete ALL comments by that author (e.g. 'MCP-BATCH', 'MCP-WORKFLOW')."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The 'name' field returned by comment_list."}, "author": {"type": "string", "description": ("Delete ALL comments by this author (e.g. 'MCP-BATCH', 'MCP-WORKFLOW').")}}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        comment_name = kwargs.get("name")
        author = kwargs.get("author")

        if not comment_name and not author:
            return self._tool_error("Provide name or author.")

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

        if not to_delete:
            # A miss must be an ERROR: "ok, deleted: 0" reads as success and the agent then
            # reports a deletion that never happened.
            what = ("comment_name '%s'" % comment_name) if comment_name else ("author '%s'" % author)
            return {"status": "error", "code": "COMMENT_NOT_FOUND",
                    "message": "No comment matched %s. Call comment_list to see the current names and authors." % what,
                    "deleted": 0}

        for field in to_delete:
            text_obj.removeTextContent(field)

        return {"status": "ok", "deleted": len(to_delete)}


class CommentResolve(ToolWriterCommentBase):
    """Resolve a comment with an optional reason."""

    name = "comment_resolve"
    intent = "review"
    description = "Resolve a comment with an optional reason. Adds a reply with the resolution text, then marks as resolved."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The 'name' field returned by comment_list."},
            "resolution": {"type": "string", "description": "Optional resolution text added as a reply."},
            "author": {"type": "string", "description": "Author name for the resolution reply. Default: AI."},
        },
        "required": ["name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        comment_name = kwargs.get("name", "")
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
            # TODO(follow-up): align with add_comment — status="error" + structured fields so MCP
            # / chat FSM do not treat a miss as success (not_found is still ok today).
            return {"status": "not_found", "message": "Comment '%s' not found." % comment_name}

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

        return {"status": "ok", "comment_name": comment_name, "resolved": True}


_WORKFLOW_TASK_PREFIXES = WORKFLOW_TASK_PREFIXES
_COMMENT_UNO = ["com.sun.star.text.TextDocument"]
_ANNOTATION_PROPERTIES = (
    ("Author", "author", ""),
    ("Content", "content", ""),
    ("Name", "name", ""),
    ("ParentName", "parent_name", ""),
    ("Resolved", "resolved", False),
)


def _comment_scan_tasks(ctx, kwargs):
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
        entry = _read_annotation(field, para_ranges, text_obj, doc_svc)
        entry["prefix"] = matched_prefix
        tasks.append(entry)
    return {"status": "ok", "tasks": tasks, "count": len(tasks)}


def _comment_workflow_get(ctx):
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
                key, _sep, value = line.partition(":")
                workflow[key.strip()] = value.strip()
        return {"status": "ok", "workflow": workflow}
    return {"status": "ok", "workflow": None}


def _comment_workflow_set(ctx, kwargs):
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


def _comment_check_stop(ctx):
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
    return {"status": "ok", "should_stop": should_stop, "workflow_stop": workflow_stop, "stop_signals": stop_signals, "count": len(stop_signals)}


class CommentScanTasks(ToolWriterCommentBase):
    name = "comment_scan_tasks"
    intent = "review"
    description = f"Find workflow tasks in comments ({', '.join(WORKFLOW_TASK_PREFIXES)} prefixes)."
    parameters = {
        "type": "object",
        "properties": {
            "unresolved_only": {"type": "boolean", "description": "Only unresolved tasks (default true)."},
            "prefix_filter": {"type": "string", "enum": list(WORKFLOW_TASK_PREFIXES), "description": "Filter by task prefix."},
        },
        "required": [],
    }
    uno_services = _COMMENT_UNO

    def execute(self, ctx, **kwargs):
        return _comment_scan_tasks(ctx, kwargs)


class CommentWorkflowGet(ToolWriterCommentBase):
    name = "comment_workflow_get"
    intent = "review"
    description = "Read the MCP-WORKFLOW dashboard comment (key: value lines)."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = _COMMENT_UNO

    def execute(self, ctx, **kwargs):
        return _comment_workflow_get(ctx)


class CommentWorkflowSet(ToolWriterCommentBase):
    name = "comment_workflow_set"
    intent = "review"
    description = "Write the MCP-WORKFLOW dashboard comment (key: value lines)."
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Workflow status as key: value lines."},
        },
        "required": ["content"],
    }
    uno_services = _COMMENT_UNO
    is_mutation = True

    def execute(self, ctx, **kwargs):
        return _comment_workflow_set(ctx, kwargs)


class CommentCheckStop(ToolWriterCommentBase):
    name = "comment_check_stop"
    intent = "review"
    description = "Detect STOP/CANCEL comments or workflow stop/pause in the MCP-WORKFLOW dashboard."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = _COMMENT_UNO

    def execute(self, ctx, **kwargs):
        return _comment_check_stop(ctx)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _count_annotations(doc):
    """How many Annotation text fields are on *doc* (0 if the enumeration is unusable)."""
    n = 0
    try:
        enum = doc.getTextFields().createEnumeration()
    except Exception:
        return 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            field = enum.nextElement()
        except Exception:
            break
        try:
            if field.supportsService("com.sun.star.text.textfield.Annotation"):
                n += 1
        except Exception:
            continue
    return n


def _set_annotation_date(annotation):
    """Set DateTimeValue (and Date) to now for a new annotation."""
    now = now_aware()
    try:
        dt = cast("Any", uno.createUnoStruct("com.sun.star.util.DateTime"))
        dt.Year = now.year
        dt.Month = now.month
        dt.Day = now.day
        dt.Hours = now.hour
        dt.Minutes = now.minute
        dt.Seconds = now.second
        annotation.setPropertyValue("DateTimeValue", dt)
    except Exception:
        pass
    try:
        d = cast("Any", uno.createUnoStruct("com.sun.star.util.Date"))
        d.Year = now.year
        d.Month = now.month
        d.Day = now.day
        annotation.setPropertyValue("Date", d)
    except Exception:
        pass


def _read_annotation(field, para_ranges, text_obj, doc_svc):
    """Extract annotation properties into a plain dict."""
    entry = {}
    for prop, key, default in _ANNOTATION_PROPERTIES:
        try:
            entry[key] = field.getPropertyValue(prop)
        except Exception:
            entry[key] = default

    # Date
    try:
        dt = field.getPropertyValue("DateTimeValue")
        entry["date"] = "%04d-%02d-%02d %02d:%02d" % (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)
    except Exception:
        entry["date"] = ""

    # Paragraph index and anchor preview. An annotation field's getAnchor().getString() often
    # comes back EMPTY (the field anchor reads as a point even for spanning comments), which left
    # anchor_preview blank — the agent could not tell which passage a comment is about. Fall back
    # to the ENCLOSING PARAGRAPH's text and say so via anchor_is_paragraph_context.
    anchor = None
    try:
        anchor = field.getAnchor()
        entry["paragraph_index"] = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)
    except Exception:
        entry["paragraph_index"] = 0
    preview = ""
    if anchor is not None:
        try:
            preview = anchor.getString()[:80]
        except Exception:
            preview = ""
        if not preview:
            try:
                from plugin.writer.search import _enclosing_paragraph_text

                ptxt = (_enclosing_paragraph_text(anchor) or "").strip()
                if ptxt:
                    preview = ptxt[:120]
                    entry["anchor_is_paragraph_context"] = True
            except Exception:
                pass
    entry["anchor_preview"] = preview

    return entry
