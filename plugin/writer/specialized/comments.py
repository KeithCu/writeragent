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

import logging
from typing import Any, cast

from plugin.framework.constants import WORKFLOW_TASK_PREFIXES, now_aware
from plugin.framework.errors import is_real_disposal, reraise_if_disposed
import uno

from plugin.framework.tool import ToolBase
from ..specialized_base import ToolWriterCommentBase

log = logging.getLogger("writeragent.writer.comments")


def _iter_annotations(doc: Any) -> Any:
    fields = doc.getTextFields()
    enum = fields.createEnumeration()
    while enum.hasMoreElements():
        field = enum.nextElement()
        try:
            if field.supportsService("com.sun.star.text.textfield.Annotation"):
                yield field
        except Exception as exc:
            reraise_if_disposed(exc)


class CommentList(ToolWriterCommentBase):
    """List all comments (annotations) in the document."""

    name: str | None = "comment_list"
    intent: str | None = "review"
    description: str = "List all comments/annotations in the document, including author, content, date, resolved status, and anchor preview. Use author_filter to see only a specific agent's comments."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"author_filter": {"type": "string", "description": ("Filter by author name (e.g. 'Claude', 'AI'). Case-insensitive substring match. Omit for all.")}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        author_filter = kwargs.get("author_filter")
        doc = ctx.doc
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        from plugin.doc.paragraph_search import paragraph_entries

        entries = paragraph_entries(para_ranges)
        label_cache: dict[int, str] = {}

        comments = []
        af = (author_filter or "").lower() if author_filter else None
        for field in _iter_annotations(doc):
            if af is not None:
                try:
                    field_author = field.getPropertyValue("Author") or ""
                except Exception as exc:
                    reraise_if_disposed(exc)
                    field_author = ""
                if af not in field_author.lower():
                    continue

            entry = _read_annotation(
                field, para_ranges, text_obj, doc_svc, doc=doc, entries=entries, label_cache=label_cache
            )
            comments.append(entry)

        result = {"status": "ok", "comments": comments, "count": len(comments)}
        if author_filter:
            result["filtered_by"] = author_filter
        return result


class AddComment(ToolBase):
    """Add a comment anchored to a search string, or reply via parent_name."""

    name: str | None = "add_comment"
    intent: str | None = "review"
    description: str = (
        "Add a comment/annotation. For a new root comment, pass search (and optional occurrence) "
        "so the comment SPANS the matched passage. To reply without resolving, pass parent_name "
        "(the 'name' from a prior add_comment success, or from comment_list); search is not required. Replies nest under that immediate "
        "parent (including other replies) and do not mark it resolved — use comment_resolve for "
        "reply-and-resolve. Use author to sign the comment."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {
        "content": {"type": "string", "description": "The comment text."},
        "search": {"type": "string", "description": "Anchor a new root comment to text matching this string. Not required when parent_name is set."},
        "occurrence": {"type": "integer", "description": "0-based match to comment on when search repeats (default 0). Ignored when parent_name is set."},
        "author": {"type": "string", "description": "Comment author (default 'WriterAgent')."},
        "parent_name": {"type": "string", "description": "Reply to this comment. Pass the 'name' from a prior add_comment success, or the 'name' field from comment_list. Immediate parent — not coerced to the thread root. When set, search/occurrence are skipped."},
    }, "required": ["content"]}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        content = kwargs.get("content", "")
        if not content:
            return self._tool_error("Provide content.")

        parent_name = (kwargs.get("parent_name") or "").strip()
        author = (kwargs.get("author") or "WriterAgent").strip() or "WriterAgent"

        # Reply path: look up the immediate parent by Name and insert at its anchor
        # (same shape as CommentResolve) without setting Resolved. Do not walk to the
        # thread root — nesting under a reply matches LO's Reply button (#636).
        if parent_name:
            parent = _find_annotation_by_name(ctx.doc, parent_name)
            if parent is None:
                # Top-level comment_added (not _tool_error details) matches the
                # search-miss payload so clients can branch on one field.
                return {
                    "status": "error",
                    "code": "COMMENT_NOT_FOUND",
                    "message": "Comment '%s' not found. Call comment_list to see the current names." % parent_name,
                    "comment_added": False,
                }
            return self._insert_reply(ctx.doc, parent, parent_name, content, author)

        search_text = kwargs.get("search")
        if not search_text:
            return self._tool_error("Provide search.")
        from ..search import find_occurrence_range, parse_non_negative_int

        # What was wrong: numeric strings, floats, and empty string were rejected.
        # How it happened: parse_non_negative_int without coerce=True rejected non-ints.
        # Why this change: restore int() coercion via coerce=True.
        occ_val, occ_err = parse_non_negative_int(kwargs.get("occurrence", 0), coerce=True)
        if occ_err:
            return self._tool_error(occ_err)
        occurrence = 0 if occ_val is None else occ_val

        doc = ctx.doc

        try:
            found = find_occurrence_range(
                doc,
                search_text,
                occurrence=occurrence,
                case_sensitive_order=(True, False),
            )
        except IndexError:
            found = None

        # status="error" (not "not_found"): an anchor miss is a failure, so the chat FSM and
        # MCP host don't treat a no-op as success. anchor_text is returned on success only.
        if found is None:
            where = (" at occurrence %d" % occurrence) if occurrence else ""
            return {"status": "error", "message": "Text '%s' not found%s." % (search_text, where), "matched": False, "comment_added": False}

        annotation = doc.createInstance("com.sun.star.text.textfield.Annotation")
        annotation.setPropertyValue("Author", author)
        annotation.setPropertyValue("Content", content)
        _set_annotation_date(annotation)
        # Intended spanning-anchor insert (PR #365 Q15): cursor covers the match,
        # absorb=True. Use found.getText() so table cells / frames still work.
        # There is no insert/balloon bug. Writer still shows the comment when
        # View > Comments (ShowAnnotations) is on; a missing balloon is
        # display-off, not a failed insert. PR #493 switched to a point insert
        # on that mistaken diagnosis; this restores the original API and keeps
        # only that PR's field-count check.
        anchor_text = found.getString()
        names_before = _annotation_field_names(doc)
        match_text = found.getText()
        cursor = match_text.createTextCursorByRange(found.getStart())
        cursor.gotoRange(found.getEnd(), True)
        match_text.insertTextContent(cursor, annotation, True)
        names_after = _annotation_field_names(doc)
        if len(names_after) <= len(names_before):
            return {
                "status": "error",
                "message": "Comment insert did not register an annotation.",
                "matched": True,
                "comment_added": False,
                "anchor_text": anchor_text or search_text,
            }

        return {
            "status": "ok",
            "message": "Comment added.",
            "author": author,
            "matched": True,
            "comment_added": True,
            "anchor_text": anchor_text or search_text,
            "name": _name_of_new_annotation(doc, annotation, names_before),
        }

    def _insert_reply(self, doc: Any, parent: Any, parent_name: str, content: str, author: str) -> dict[str, Any]:
        """Insert a threaded reply at the parent's anchor. Does not set Resolved."""
        success, reply_name_or_err = _insert_annotation_reply(doc, parent, parent_name, content, author)
        if not success:
            return {
                "status": "error",
                "message": reply_name_or_err,
                "comment_added": False,
                "parent_name": parent_name,
            }
        return {
            "status": "ok",
            "message": "Reply added.",
            "author": author,
            "comment_added": True,
            "name": reply_name_or_err,
            "parent_name": parent_name,
        }


class CommentDelete(ToolWriterCommentBase):
    """Delete comments by name or author."""

    name: str | None = "comment_delete"
    intent: str | None = "review"
    description: str = "Delete comments by name or author. Use name to delete a specific comment and its replies. Use author to delete ALL comments by that author (e.g. 'MCP-BATCH', 'MCP-WORKFLOW')."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "The 'name' field returned by comment_list."}, "author": {"type": "string", "description": ("Delete ALL comments by this author (e.g. 'MCP-BATCH', 'MCP-WORKFLOW').")}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        comment_name = kwargs.get("name")
        author = kwargs.get("author")

        if not comment_name and not author:
            return self._tool_error("Provide name or author.")

        doc = ctx.doc
        rows = []
        for field in _iter_annotations(doc):
            name = field.getPropertyValue("Name")
            parent = field.getPropertyValue("ParentName")
            field_author = field.getPropertyValue("Author")
            rows.append((field, name, parent, field_author))

        parent_to_children: dict[str, list[int]] = {}
        for idx, (_field, _name, parent, _field_author) in enumerate(rows):
            if parent:
                parent_to_children.setdefault(parent, []).append(idx)

        selected_indices: set[int] = set()

        # dispose() removes one field, and ParentName is only the immediate
        # parent, so the name set has to be the chain.
        if comment_name:
            queue = [comment_name]
            seen_names = {comment_name}
            for idx, (_field, name, _parent, _author) in enumerate(rows):
                if name == comment_name:
                    selected_indices.add(idx)
            while queue:
                curr_name = queue.pop()
                for child_idx in parent_to_children.get(curr_name, []):
                    selected_indices.add(child_idx)
                    child_name = rows[child_idx][1]
                    if child_name and child_name not in seen_names:
                        seen_names.add(child_name)
                        queue.append(child_name)

        if author:
            for idx, (_field, _name, _parent, field_author) in enumerate(rows):
                if field_author == author:
                    selected_indices.add(idx)

        to_delete = [rows[idx][0] for idx in sorted(selected_indices)]

        if not to_delete:
            # A miss must be an ERROR: "ok, deleted: 0" reads as success and the agent then
            # reports a deletion that never happened.
            what = ("comment_name '%s'" % comment_name) if comment_name else ("author '%s'" % author)
            return {"status": "error", "code": "COMMENT_NOT_FOUND",
                    "message": "No comment matched %s. Call comment_list to see the current names and authors." % what,
                    "deleted": 0}

        from ..edit_review import next_agent_edit_undo_title
        from plugin.doc.undo import UndoUnavailable, commit_or_rollback

        undo_title = next_agent_edit_undo_title()
        deleted_count = 0

        def mutate() -> dict[str, Any] | None:
            nonlocal deleted_count
            for field in to_delete:
                # The body XText does not own an annotation anchored in a cell or frame.
                field.getAnchor().getText().removeTextContent(field)
                deleted_count += 1
            return None

        try:
            error = commit_or_rollback(doc, undo_title, mutate)
            if error is not None:
                return error
        except UndoUnavailable:
            return self._tool_error(
                "Cannot delete comments atomically (no usable undo context).",
                code="UNDO_UNAVAILABLE",
            )
        except Exception as e:
            if is_real_disposal(e):
                raise
            err = self._tool_error(
                f"Failed to delete comments; the change was rolled back ({e}).",
                deleted=0,
            )
            err["deleted"] = 0
            return err

        return {"status": "ok", "deleted": deleted_count}


class CommentResolve(ToolWriterCommentBase):
    """Resolve a comment with an optional reason."""

    name: str | None = "comment_resolve"
    intent: str | None = "review"
    description: str = "Resolve a comment with an optional reason. Adds a reply with the resolution text, then marks as resolved."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The 'name' field returned by comment_list."},
            "resolution": {"type": "string", "description": "Optional resolution text added as a reply."},
            "author": {"type": "string", "description": "Author name for the resolution reply. Default: AI."},
        },
        "required": ["name"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        comment_name = kwargs.get("name", "")
        resolution = kwargs.get("resolution", "")
        author = kwargs.get("author", "AI")

        doc = ctx.doc
        target = None
        for field in _iter_annotations(doc):
            name = field.getPropertyValue("Name")
            if name == comment_name:
                target = field
                break

        if target is None:
            return {
                "status": "error",
                "code": "COMMENT_NOT_FOUND",
                "message": "Comment '%s' not found. Call comment_list to see the current names." % comment_name,
            }

        from ..edit_review import next_agent_edit_undo_title
        from plugin.doc.undo import UndoUnavailable, commit_or_rollback

        undo_title = next_agent_edit_undo_title()
        reply_name = None

        def mutate() -> dict[str, Any] | None:
            nonlocal reply_name
            if resolution:
                success, reply_name_or_err = _insert_annotation_reply(doc, target, comment_name, resolution, author)
                if not success:
                    return {
                        "status": "error",
                        "message": reply_name_or_err,
                        "parent_name": comment_name,
                    }
                reply_name = reply_name_or_err

            target.setPropertyValue("Resolved", True)
            return None

        try:
            error = commit_or_rollback(doc, undo_title, mutate)
            if error is not None:
                return error
        except UndoUnavailable:
            return self._tool_error(
                "Cannot resolve comment atomically (no usable undo context).",
                code="UNDO_UNAVAILABLE",
            )
        except Exception as e:
            if is_real_disposal(e):
                raise
            return self._tool_error(
                f"Failed to resolve comment; the change was rolled back ({e}).",
            )

        result: dict[str, Any] = {"status": "ok", "comment_name": comment_name, "resolved": True}
        if reply_name:
            result["reply_name"] = reply_name
        return result


_WORKFLOW_TASK_PREFIXES = WORKFLOW_TASK_PREFIXES
_COMMENT_UNO = ["com.sun.star.text.TextDocument"]
_ANNOTATION_PROPERTIES = (
    ("Author", "author"),
    ("Content", "content"),
    ("Name", "name"),
    ("ParentName", "parent_name"),
    ("Resolved", "resolved"),
)


def _comment_scan_tasks(ctx: Any, kwargs: Any) -> dict[str, Any]:
    unresolved_only = kwargs.get("unresolved_only", True)
    prefix_filter = kwargs.get("prefix_filter", None)
    doc = ctx.doc
    doc_svc = ctx.services.document
    para_ranges = doc_svc.get_paragraph_ranges(doc)
    text_obj = doc.getText()
    from plugin.doc.paragraph_search import paragraph_entries

    entries = paragraph_entries(para_ranges)
    label_cache: dict[int, str] = {}
    tasks = []
    for field in _iter_annotations(doc):
        try:
            content = field.getPropertyValue("Content") or ""
        except Exception as exc:
            reraise_if_disposed(exc)
            content = ""
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
                if field.getPropertyValue("Resolved"):
                    continue
            except Exception as exc:
                reraise_if_disposed(exc)
                pass
        entry = _read_annotation(
            field, para_ranges, text_obj, doc_svc, doc=doc, entries=entries, label_cache=label_cache
        )
        entry["prefix"] = matched_prefix
        tasks.append(entry)
    return {"status": "ok", "tasks": tasks, "count": len(tasks)}


def _parse_workflow_content(content: str) -> list[tuple[str | None, str]]:
    """Parse workflow lines into (key, value) pairs, with key=None for bare lines."""
    items: list[tuple[str | None, str]] = []
    for line in content.splitlines():
        if ":" in line:
            key, _sep, value = line.partition(":")
            items.append((key.strip(), value.strip()))
        else:
            stripped = line.strip()
            if stripped:
                items.append((None, stripped))
    return items


def _comment_workflow_get(ctx: Any) -> dict[str, Any]:
    doc = ctx.doc
    for field in _iter_annotations(doc):
        author = field.getPropertyValue("Author") or ""
        if author != "MCP-WORKFLOW":
            continue
        content = field.getPropertyValue("Content") or ""
        workflow = {k: v for k, v in _parse_workflow_content(content) if k is not None}
        return {"status": "ok", "workflow": workflow}
    return {"status": "ok", "workflow": None}


def _comment_workflow_set(ctx: Any, kwargs: Any) -> dict[str, Any]:
    content = kwargs.get("content", "")
    doc = ctx.doc
    doc_text = doc.getText()
    existing = None
    for field in _iter_annotations(doc):
        author = field.getPropertyValue("Author") or ""
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


def _comment_check_stop(ctx: Any) -> dict[str, Any]:
    doc = ctx.doc
    stop_signals = []
    workflow_stop = False
    for field in _iter_annotations(doc):
        content = field.getPropertyValue("Content") or ""
        author = field.getPropertyValue("Author") or ""
        resolved = field.getPropertyValue("Resolved")
        if author == "MCP-WORKFLOW" and content:
            for _key, val in _parse_workflow_content(content):
                if val.lower() in ("stop", "pause"):
                    workflow_stop = True
                    break
        if not resolved and content:
            upper = content.strip().upper()
            if upper.startswith("STOP") or upper.startswith("CANCEL"):
                stop_signals.append({"author": author, "content": content[:100]})
    should_stop = bool(stop_signals) or workflow_stop
    return {"status": "ok", "should_stop": should_stop, "workflow_stop": workflow_stop, "stop_signals": stop_signals, "count": len(stop_signals)}


class CommentScanTasks(ToolWriterCommentBase):
    name: str | None = "comment_scan_tasks"
    intent: str | None = "review"
    description: str = f"Find workflow tasks in comments ({', '.join(WORKFLOW_TASK_PREFIXES)} prefixes)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "unresolved_only": {"type": "boolean", "description": "Only unresolved tasks (default true)."},
            "prefix_filter": {"type": "string", "enum": list(WORKFLOW_TASK_PREFIXES), "description": "Filter by task prefix."},
        },
        "required": [],
    }
    uno_services: list[str] | None = _COMMENT_UNO
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _comment_scan_tasks(ctx, kwargs)


class CommentWorkflowGet(ToolWriterCommentBase):
    name: str | None = "comment_workflow_get"
    intent: str | None = "review"
    description: str = "Read the MCP-WORKFLOW dashboard comment (key: value lines)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = _COMMENT_UNO

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _comment_workflow_get(ctx)


class CommentWorkflowSet(ToolWriterCommentBase):
    name: str | None = "comment_workflow_set"
    intent: str | None = "review"
    description: str = "Write the MCP-WORKFLOW dashboard comment (key: value lines)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Workflow status as key: value lines."},
        },
        "required": ["content"],
    }
    uno_services: list[str] | None = _COMMENT_UNO
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _comment_workflow_set(ctx, kwargs)


class CommentCheckStop(ToolWriterCommentBase):
    name: str | None = "comment_check_stop"
    intent: str | None = "review"
    description: str = "Detect STOP/CANCEL comments or a dashboard value exactly equal to stop or pause in the MCP-WORKFLOW dashboard."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = _COMMENT_UNO
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return _comment_check_stop(ctx)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _find_annotation_by_name(doc: Any, comment_name: str) -> Any | None:
    """Return the Annotation field whose Name matches *comment_name*, or None."""
    for field in _iter_annotations(doc):
        if field.getPropertyValue("Name") == comment_name:
            return field
    return None


def _annotation_name(field: Any) -> str:
    """LibreOffice-assigned Name (empty when Writer left it unset)."""
    return field.getPropertyValue("Name") or ""


def _annotation_field_names(doc: Any) -> list[str]:
    """Name of each Annotation on *doc* (empty string when Name is unset)."""
    names: list[str] = []
    for field in _iter_annotations(doc):
        names.append(field.getPropertyValue("Name") or "")
    return names


def _ensure_annotation_name(field: Any, doc: Any) -> str:
    """Return *field*'s Name, assigning a unique one if Writer left it empty."""
    name = _annotation_name(field)
    if name:
        return name
    existing = set(n for n in _annotation_field_names(doc) if n)
    stamp = int(now_aware().timestamp())
    n = len(existing) + 1
    candidate = "__Annotation__%d_%d" % (n, stamp)
    while candidate in existing:
        n += 1
        candidate = "__Annotation__%d_%d" % (n, stamp)
    field.setPropertyValue("Name", candidate)
    return _annotation_name(field) or candidate


def _name_of_new_annotation(doc: Any, field: Any, names_before: Any) -> str:
    """Name on *field*, or the new Name that appeared on *doc* after insert.

    Point-insert replies (CommentResolve shape) often leave Name empty on the
    pre-insert instance; the document field enumeration has the assigned id.
    """
    name = _annotation_name(field)
    if name:
        return name
    prior = set(n for n in names_before if n)
    for n in _annotation_field_names(doc):
        if n and n not in prior:
            return n
    return ""


def _set_annotation_date(annotation: Any) -> None:
    """Set DateTimeValue (and Date) to now for a new annotation."""
    now = now_aware()
    dt = cast("Any", uno.createUnoStruct("com.sun.star.util.DateTime"))
    dt.Year = now.year
    dt.Month = now.month
    dt.Day = now.day
    dt.Hours = now.hour
    dt.Minutes = now.minute
    dt.Seconds = now.second
    annotation.setPropertyValue("DateTimeValue", dt)

    d = cast("Any", uno.createUnoStruct("com.sun.star.util.Date"))
    d.Year = now.year
    d.Month = now.month
    d.Day = now.day
    annotation.setPropertyValue("Date", d)


def _insert_annotation_reply(
    doc: Any, parent: Any, parent_name: str, content: str, author: str
) -> tuple[bool, str]:
    """Insert a threaded reply at the parent's anchor. Returns (success, reply_name_or_error_message)."""
    reply = doc.createInstance("com.sun.star.text.textfield.Annotation")
    reply.setPropertyValue("ParentName", parent_name)
    reply.setPropertyValue("Content", content)
    reply.setPropertyValue("Author", author)
    # Point-insert replies (ParentName set) do not get an auto Name from Writer;
    # the pre-insert instance and the document enumeration both stay empty.
    # Setting Name before insert sticks (LO probe: listed ParentName + Name).
    assigned_name = _ensure_annotation_name(reply, doc)
    _set_annotation_date(reply)
    # Why-comment: the body XText does not own an annotation anchored in a cell or frame.
    anchor = parent.getAnchor()
    host = anchor.getText()
    cursor = host.createTextCursorByRange(anchor.getStart())
    names_before = _annotation_field_names(doc)
    host.insertTextContent(cursor, reply, False)
    names_after = _annotation_field_names(doc)
    if len(names_after) <= len(names_before):
        return False, "Reply insert did not register an annotation."
    reply_name = _annotation_name(reply) or assigned_name or _name_of_new_annotation(doc, reply, names_before)
    return True, reply_name


def _read_annotation(
    field: Any,
    para_ranges: Any,
    text_obj: Any,
    doc_svc: Any,
    doc: Any = None,
    entries: Any = None,
    label_cache: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Extract annotation properties into a plain dict."""
    from plugin.framework.uno_context import uno_same
    from plugin.writer.search import describe_match_location

    entry: dict[str, Any] = {}
    name_read: str | None = None
    props_read = 0
    last_exc: Exception | None = None
    try:
        for prop, key in _ANNOTATION_PROPERTIES:
            try:
                val = field.getPropertyValue(prop)
                if key in ("author", "content") and val is None:
                    val = ""
                entry[key] = val
                props_read += 1
                if key == "name" and val:
                    name_read = val
            except Exception as exc:
                reraise_if_disposed(exc)
                last_exc = exc

        try:
            dt = field.getPropertyValue("DateTimeValue")
            if dt is not None:
                entry["date"] = "%04d-%02d-%02d %02d:%02d" % (
                    getattr(dt, "Year", 0),
                    getattr(dt, "Month", 0),
                    getattr(dt, "Day", 0),
                    getattr(dt, "Hours", 0),
                    getattr(dt, "Minutes", 0),
                )
                props_read += 1
            else:
                entry["date"] = ""
        except Exception as exc:
            reraise_if_disposed(exc)
            last_exc = exc

        if props_read == 0:
            err_msg = str(last_exc) if last_exc is not None else "failed to read annotation"
            log.warning("Failed to read annotation: %s", err_msg)
            err_entry: dict[str, Any] = {"error": err_msg, "stale": True}
            if name_read is not None:
                err_entry["name"] = name_read
            return err_entry

        # getAnchor().getString() is often empty (the anchor reads as a point even for
        # spanning comments), so fall back to the enclosing paragraph text and say so via
        # anchor_is_paragraph_context.
        try:
            anchor = field.getAnchor()
            preview = ""
            if anchor is not None:
                entry["location"] = describe_match_location(anchor, doc, label_cache=label_cache)
                doc_text = text_obj
                try:
                    anchor_text = anchor.getText()
                except Exception:
                    anchor_text = None
                if anchor_text is not None and doc_text is not None and uno_same(anchor_text, doc_text):
                    raw_pi = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj, entries=entries)
                    from plugin.doc.paragraph_search import confirm_paragraph_index

                    placed = confirm_paragraph_index(text_obj, anchor, para_ranges, raw_pi)
                    if placed is not None:
                        entry["paragraph_index"] = placed

                try:
                    preview = (anchor.getString() or "")[:80]
                except Exception:
                    preview = ""
                if not preview:
                    from plugin.writer.search import enclosing_paragraph_text

                    ptxt = (enclosing_paragraph_text(anchor) or "").strip()
                    if ptxt:
                        preview = ptxt[:120]
                        entry["anchor_is_paragraph_context"] = True
            else:
                entry["location"] = "body"
            entry["anchor_preview"] = preview
        except Exception as exc:
            reraise_if_disposed(exc)
            last_exc = exc

        return entry
    except Exception as exc:
        reraise_if_disposed(exc)
        log.warning("Failed to read annotation: %s", exc)
        outer_err: dict[str, Any] = {"error": str(exc), "stale": True}
        if name_read is not None:
            outer_err["name"] = name_read
        return outer_err
