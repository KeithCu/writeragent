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
"""Writer and Calc track-changes tools.

Provides the complete suite of specialized track changes tools:
track_changes_start, track_changes_stop, track_changes_list,
track_changes_accept, track_changes_reject, track_changes_accept_all,
track_changes_reject_all, and track_changes_show.

Also includes tools for managing document comments (Annotations) in Writer only.
"""

import logging
from plugin.framework.constants import now_aware

from typing import Any

from plugin.calc.base import ToolCalcSpecialTracking
from .specialized_base import WriterAgentSpecialTracking

log = logging.getLogger("writeragent.writer")

_TRACK_CHANGES_UNO_SERVICES = ["com.sun.star.text.TextDocument", "com.sun.star.sheet.SpreadsheetDocument"]


def _calc_track_changes_show_markup(_ctx: Any, _controller: Any, _show: bool) -> dict[str, Any]:
    """Calc: show/hide tracked-change markup from tools is deferred (no stable UNO path yet).

    INVESTIGATE LATER: spreadsheet controllers lack ``getViewSettings``/``ShowChangesInMargin``.
    A prior attempt scanned the controller as ``XPropertySet`` for boolean props matching
    change/track + show/visible, then dispatched ``.uno:ShowTrackedChanges``; Calc still did
    not reliably expose or toggle markup the way Writer does. Revisit when LibreOffice
    documents a supported API (or headless-safe dispatch with deterministic state).
    """
    return {
        "status": "ok",
        "message": ("Calc: showing or hiding tracked-change markup from WriterAgent is not supported yet—use Edit - Track Changes - Show (or Review in the tabbed UI) in LibreOffice. track_changes_start / track_changes_stop still control recording."),
        "calc_track_changes_show_unsupported": True,
    }


class TrackChangesStart(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """Start recording changes."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    name = "track_changes_start"
    description = "Start recording changes (track changes) in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        try:
            ctx.doc.setPropertyValue("RecordChanges", True)
            return {"status": "ok", "message": "Started recording changes."}
        except Exception as e:
            return self._tool_error(f"Failed to start tracking changes: {e}")


class TrackChangesStop(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """Stop recording changes."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    name = "track_changes_stop"
    description = "Stop recording changes (track changes) in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        try:
            ctx.doc.setPropertyValue("RecordChanges", False)
            return {"status": "ok", "message": "Stopped recording changes."}
        except Exception as e:
            return self._tool_error(f"Failed to stop tracking changes: {e}")


class TrackChangesList(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """List all tracked changes (redlines) in the document."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    name = "track_changes_list"
    description = (
        "List all tracked changes (redlines) in the document, including type, author, date, text "
        "and location. NOTE the two flags: 'recording' is the DOCUMENT's own track-changes toggle; "
        "'agent_review_mode' (off/record/wait) is the user's review setting for AGENT edits — in "
        "record/wait your edits become redlines even while recording is false."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    @staticmethod
    def _agent_review_mode(uno_ctx):
        """Best-effort agent review mode, so 'recording: false' is never read as 'my edits are
        not being tracked' while record/wait mode is active."""
        try:
            from plugin.writer.edit_review import get_agent_edit_review_mode

            return get_agent_edit_review_mode(uno_ctx)
        except Exception:
            return None

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        recording = False
        try:
            recording = doc.getPropertyValue("RecordChanges")
        except Exception:
            pass
        agent_mode = self._agent_review_mode(getattr(ctx, "ctx", None))

        if not hasattr(doc, "getRedlines"):
            result = {"status": "ok", "recording": recording, "changes": [], "count": 0, "message": "Document does not expose redlines API."}
            if agent_mode is not None:
                result["agent_review_mode"] = agent_mode
            return result

        try:
            redlines = doc.getRedlines()
            enum = redlines.createEnumeration()
            # Materialize the enumeration FIRST. Calling getAnchor() (which walks the text model)
            # mid-enumeration conflicts with the live iterator, so accept/reject only touches the
            # anchor after the loop -- we do the same by collecting the redlines up front.
            redline_objs = []
            while enum.hasMoreElements():
                redline_objs.append(enum.nextElement())

            from plugin.writer.search import _describe_match_location

            changes = []
            for index, redline in enumerate(redline_objs):
                entry: dict[str, Any] = {"index": index}
                for prop in ("RedlineType", "RedlineAuthor", "RedlineComment", "RedlineIdentifier"):
                    try:
                        entry[prop] = redline.getPropertyValue(prop)
                    except Exception:
                        pass
                try:
                    dt = redline.getPropertyValue("RedlineDateTime")
                    entry["date"] = "%04d-%02d-%02d %02d:%02d" % (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)
                except Exception:
                    pass
                # Text + location: accept/reject take this index, so the model needs to know WHICH
                # change is which. Without them, mapping "reject the change to clause 4.2" onto an
                # index is guesswork. Redline objects from getRedlines() are property sets (no
                # getAnchor) with RedlineStart/RedlineEnd (XTextRange) and RedlineText (deleted
                # content). Each field is best-effort so one failure never drops the other.
                start = None
                try:
                    start = redline.getPropertyValue("RedlineStart")
                except Exception:
                    start = None
                if start is not None:
                    try:
                        entry["location"] = _describe_match_location(start, doc)
                    except Exception:
                        pass
                # Text: for an insertion the live span RedlineStart..RedlineEnd holds it; for a
                # deletion that span is collapsed, so fall back to RedlineText (the removed text).
                txt = ""
                try:
                    end = redline.getPropertyValue("RedlineEnd")
                    stext = start.getText()
                    cur = stext.createTextCursorByRange(start)
                    cur.gotoRange(end, True)
                    txt = cur.getString() or ""
                except Exception:
                    txt = ""
                if not txt:
                    try:
                        rt = redline.getPropertyValue("RedlineText")
                        txt = (rt.getString() if hasattr(rt, "getString") else str(rt)) if rt is not None else ""
                    except Exception:
                        txt = ""
                if txt:
                    entry["text"] = txt if len(txt) <= 300 else txt[:299] + "…"
                changes.append(entry)

            result = {"status": "ok", "recording": recording, "changes": changes, "count": len(changes)}
            if agent_mode is not None:
                result["agent_review_mode"] = agent_mode
            return result
        except Exception as e:
            return self._tool_error(f"Failed to list tracked changes: {e}")


class TrackChangesShow(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """Show or hide change markup."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    name = "track_changes_show"
    description = "Show or hide tracked changes markup in the document view (Writer). On Calc, recording still works; this call returns guidance to use LibreOffice menus for show/hide markup until UNO support is implemented."
    parameters = {"type": "object", "properties": {"show": {"type": "boolean", "description": "True to show changes, False to hide them."}}, "required": ["show"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        show = kwargs.get("show")
        if show is None:
            return self._tool_error("Missing required parameter: show")

        show_b = bool(show)
        controller = ctx.doc.getCurrentController()
        view_getter = getattr(controller, "getViewSettings", None)
        if callable(view_getter):
            try:
                view_settings: Any = view_getter()
                view_settings.setPropertyValue("ShowChangesInMargin", show_b)
                return {"status": "ok", "message": f"{'Showing' if show_b else 'Hiding'} tracked changes markup."}
            except Exception as e:
                return self._tool_error(f"Failed to set track changes visibility: {e}")

        return _calc_track_changes_show_markup(ctx, controller, show_b)


class TrackChangesAcceptAll(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """Accept all tracked changes in the document."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    name = "track_changes_accept_all"
    description = "Accept all tracked changes in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        # Guard: never let the agent bulk-resolve its OWN (wa-review) edits -- those are the human's
        # to review. Allowed when no agent changes are pending (see agent_self_resolution_block_reason).
        from plugin.writer.inline_review import agent_self_resolution_block_reason
        blocked = agent_self_resolution_block_reason(ctx.doc)
        if blocked:
            return self._tool_error(blocked)
        try:
            smgr = ctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx.ctx)
            frame = ctx.doc.getCurrentController().getFrame()

            dispatcher.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
            return {"status": "ok", "message": "All tracked changes accepted."}
        except Exception as e:
            return self._tool_error(f"Failed to accept all changes: {e}")


class TrackChangesRejectAll(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """Reject all tracked changes in the document."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    name = "track_changes_reject_all"
    description = "Reject all tracked changes in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        # Guard: never let the agent bulk-resolve its OWN (wa-review) edits -- those are the human's
        # to review. Allowed when no agent changes are pending (see agent_self_resolution_block_reason).
        from plugin.writer.inline_review import agent_self_resolution_block_reason
        blocked = agent_self_resolution_block_reason(ctx.doc)
        if blocked:
            return self._tool_error(blocked)
        try:
            smgr = ctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx.ctx)
            frame = ctx.doc.getCurrentController().getFrame()

            dispatcher.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())
            return {"status": "ok", "message": "All tracked changes rejected."}
        except Exception as e:
            return self._tool_error(f"Failed to reject all changes: {e}")


class _TrackChangesSingleAction(WriterAgentSpecialTracking, ToolCalcSpecialTracking):
    """Base logic for accepting or rejecting a single tracked change."""

    uno_services = _TRACK_CHANGES_UNO_SERVICES
    is_mutation = True

    def _execute_single(self, ctx, index, is_accept):
        if not hasattr(ctx.doc, "getRedlines"):
            return self._tool_error("Document does not expose redlines API.")

        try:
            redlines = ctx.doc.getRedlines()
            enum = redlines.createEnumeration()

            # Find the target redline
            target_redline = None
            current_idx = 0
            while enum.hasMoreElements():
                redline = enum.nextElement()
                if current_idx == index:
                    target_redline = redline
                    break
                current_idx += 1

            if not target_redline:
                return self._tool_error(f"No tracked change found at index {index}.")

            # Guard: refuse to resolve the agent's OWN edit (a wa-review change). Those are recorded
            # for the human to accept/reject in the review UI; the agent must not do it itself.
            # Fail closed when the change's metadata can't be read.
            from plugin.writer.inline_review import redline_is_agent_change
            is_agent, readable = redline_is_agent_change(target_redline)
            if not readable:
                return self._tool_error(
                    "Couldn't read this change's metadata, so it can't be resolved from here. "
                    "Use Edit > Track Changes > Manage in LibreOffice."
                )
            if is_agent:
                return self._tool_error(
                    "This is an agent edit awaiting your review (WriterAgent tracked change). The "
                    "agent must not accept or reject its own edits -- resolve it in the review popup "
                    "or Edit > Track Changes > Manage."
                )

            # To accept/reject a specific change, we select its text range then use the dispatcher.
            # Redline objects from getRedlines() have NO getAnchor() (live probe: AttributeError) —
            # they are property sets exposing RedlineStart/RedlineEnd (XTextRange). The old
            # getAnchor() call meant every real change died here with "Failed to select".
            try:
                start = target_redline.getPropertyValue("RedlineStart")
                cur = start.getText().createTextCursorByRange(start)
                try:
                    end = target_redline.getPropertyValue("RedlineEnd")
                    if end is not None:
                        cur.gotoRange(end, True)
                except Exception:
                    pass  # collapsed span (e.g. a deletion) — selecting the start point suffices
                ctx.doc.getCurrentController().select(cur)
            except Exception as e:
                return self._tool_error(f"Failed to select tracked change for processing: {e}")

            smgr = ctx.ctx.ServiceManager
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx.ctx)
            frame = ctx.doc.getCurrentController().getFrame()

            cmd = ".uno:AcceptTrackedChange" if is_accept else ".uno:RejectTrackedChange"
            dispatcher.executeDispatch(frame, cmd, "", 0, ())

            action_str = "Accepted" if is_accept else "Rejected"
            return {"status": "ok", "message": f"{action_str} tracked change at index {index}."}

        except Exception as e:
            return self._tool_error(f"Failed to process change {index}: {e}")


class TrackChangesAccept(_TrackChangesSingleAction):
    """Accept a specific tracked change."""

    name = "track_changes_accept"
    description = "Accept a specific tracked change by its index (from track_changes_list)."
    parameters = {"type": "object", "properties": {"index": {"type": "integer", "description": "The zero-based index of the tracked change to accept."}}, "required": ["index"]}

    def execute(self, ctx, **kwargs):
        index = kwargs.get("index")
        if index is None or not isinstance(index, int) or index < 0:
            return self._tool_error("Valid integer index is required.")
        return self._execute_single(ctx, int(index), is_accept=True)


class TrackChangesReject(_TrackChangesSingleAction):
    """Reject a specific tracked change."""

    name = "track_changes_reject"
    description = "Reject a specific tracked change by its index (from track_changes_list)."
    parameters = {"type": "object", "properties": {"index": {"type": "integer", "description": "The zero-based index of the tracked change to reject."}}, "required": ["index"]}

    def execute(self, ctx, **kwargs):
        index = kwargs.get("index")
        if index is None or not isinstance(index, int) or index < 0:
            return self._tool_error("Valid integer index is required.")
        return self._execute_single(ctx, int(index), is_accept=False)


# --- Comments (Annotations) ---


class TrackChangesCommentInsert(WriterAgentSpecialTracking):
    """Insert a comment (Annotation) at the current selection."""

    name = "track_changes_comment_insert"
    description = "Insert a comment (annotation) at the current cursor selection."
    parameters = {"type": "object", "properties": {"content": {"type": "string", "description": "The text content of the comment."}, "author": {"type": "string", "description": "The author's name for the comment (e.g., 'WriterAgent')."}}, "required": ["content", "author"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        content = kwargs.get("content")
        author = kwargs.get("author", "WriterAgent")

        if not content:
            return self._tool_error("Comment content is required.")

        try:
            doc = ctx.doc
            annotation = doc.createInstance("com.sun.star.text.textfield.Annotation")
            annotation.setPropertyValue("Content", str(content))
            annotation.setPropertyValue("Author", str(author))

            # Use current system date
            now = now_aware()
            from com.sun.star.util import Date

            dt = Date()
            dt.Year = now.year
            dt.Month = now.month
            dt.Day = now.day
            annotation.setPropertyValue("Date", dt)

            # Insert at current view cursor
            view_cursor = doc.getCurrentController().getViewCursor()
            text = view_cursor.getText()

            text.insertTextContent(view_cursor, annotation, True)

            return {"status": "ok", "message": "Comment inserted successfully."}
        except Exception as e:
            return self._tool_error(f"Failed to insert comment: {e}")


class TrackChangesCommentList(WriterAgentSpecialTracking):
    """List all comments (Annotations) in the document."""

    name = "track_changes_comment_list"
    description = "List all comments (annotations) currently in the document."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        try:
            doc = ctx.doc
            fields = doc.getTextFields()
            enum = fields.createEnumeration()

            comments = []
            index = 0
            while enum.hasMoreElements():
                field = enum.nextElement()
                if field.supportsService("com.sun.star.text.textfield.Annotation"):
                    entry = {"index": index, "author": field.getPropertyValue("Author"), "content": field.getPropertyValue("Content")}
                    try:
                        dt = field.getPropertyValue("Date")
                        entry["date"] = f"{dt.Year:04d}-{dt.Month:02d}-{dt.Day:02d}"
                    except Exception:
                        pass

                    comments.append(entry)
                    index += 1

            return {"status": "ok", "comments": comments, "count": len(comments)}
        except Exception as e:
            return self._tool_error(f"Failed to list comments: {e}")


class TrackChangesCommentDelete(WriterAgentSpecialTracking):
    """Delete a specific comment by its index."""

    name = "track_changes_comment_delete"
    description = "Delete a specific comment (annotation) by its index (from track_changes_comment_list)."
    parameters = {"type": "object", "properties": {"index": {"type": "integer", "description": "The zero-based index of the comment to delete."}}, "required": ["index"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        index = kwargs.get("index")
        if index is None or not isinstance(index, int) or index < 0:
            return self._tool_error("Valid integer index is required.")

        try:
            doc = ctx.doc
            fields = doc.getTextFields()
            enum = fields.createEnumeration()

            current_idx = 0
            target_field = None

            while enum.hasMoreElements():
                field = enum.nextElement()
                if field.supportsService("com.sun.star.text.textfield.Annotation"):
                    if current_idx == int(index):
                        target_field = field
                        break
                    current_idx += 1

            if not target_field:
                return self._tool_error(f"No comment found at index {index}.")

            target_field.dispose()

            return {"status": "ok", "message": f"Comment at index {index} deleted successfully."}
        except Exception as e:
            return self._tool_error(f"Failed to delete comment: {e}")
