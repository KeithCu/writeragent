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
manage_tracked_changes (accept/reject one or all), and track_changes_show.
"""

from __future__ import annotations

from collections import Counter
import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcSpecialTracking
from plugin.framework.uno_context import uno_same
from plugin.writer.review_scan import is_agent_token, redline_identity_key, redline_is_agent_change
from plugin.writer.search import describe_match_location
from .specialized_base import WriterAgentSpecialTracking

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.writer")

_TRACK_CHANGES_UNO_SERVICES = ["com.sun.star.text.TextDocument", "com.sun.star.sheet.SpreadsheetDocument"]


def _enum_has_more(enum: Any) -> bool:
    try:
        return enum.hasMoreElements() is True
    except Exception:
        log.debug("redline enumeration hasMoreElements failed", exc_info=True)
        return False


def _enumerate_redlines(redlines: Any) -> tuple[list[Any], int]:
    # What was wrong: enumeration could yield fewer elements than getCount() without detection, corrupting indexes.
    # How it happened: loop only checked hasMoreElements() and did not verify length against getCount().
    # Why this change fixes it: read getCount() upfront, cap at total, and return (objs, total) so callers can detect truncation.
    total = int(redlines.getCount())
    enum = redlines.createEnumeration()
    objs: list[Any] = []
    while len(objs) < total and _enum_has_more(enum):
        objs.append(enum.nextElement())
    return objs, total


def _identity_keys(objs: list[Any]) -> list[tuple[Any, ...]] | None:
    keys: list[tuple[Any, ...]] = []
    for r in objs:
        try:
            k = redline_identity_key(r)
            hash(k)
            keys.append(k)
        except Exception:
            return None
    return keys


def _undo_resolve(ctx: ToolContext, log_message: str) -> bool:
    try:
        ctx.doc.getUndoManager().undo()
        return True
    except Exception:
        log.exception(log_message)
        return False


def _overlap_refusal(target_index: int, objs: list[Any], start: Any, end: Any) -> str | None:
    # What was wrong: compareRegionStarts threw IllegalArgumentException when checking a redline in another XText (table cell, footnote, header).
    # How it happened: compareRegionStarts requires both ranges to belong to the same XText.
    # Why this change fixes it: skip overlap comparison if uno_same(target_text, other_text) is False.
    try:
        target_text = start.getText()
        if target_text is None:
            raise ValueError("Target range has no XText")
    except Exception as rb_err:
        return (
            "Failed to read bounds of tracked change at index %d during overlap check. "
            "Please resolve tracked changes manually in LibreOffice. Error: %s"
            % (target_index, rb_err)
        )

    for i, r in enumerate(objs):
        if i == target_index:
            continue
        try:
            r_start = r.getPropertyValue("RedlineStart")
            r_end = r.getPropertyValue("RedlineEnd") or r_start
            other_text = r_start.getText()
            if other_text is None:
                raise ValueError("Other range has no XText")
            if not uno_same(target_text, other_text):
                continue
            if target_text.compareRegionStarts(end, r_start) in (1, 0) or target_text.compareRegionStarts(r_end, start) in (1, 0):
                pass  # No overlap
            else:
                return (
                    "Tracked change at index %d overlaps with another change. "
                    "Resolving it via the agent may clobber adjacent changes. "
                    "Please resolve it manually in LibreOffice."
                    % target_index
                )
        except Exception as rb_err:
            return (
                "Failed to read bounds of tracked change at index %d during overlap check. "
                "Please resolve tracked changes manually in LibreOffice. Error: %s"
                % (i, rb_err)
            )
    return None


def _resolve_outcome(index: int, before_keys: list[tuple[Any, ...]], after_keys: list[tuple[Any, ...]]) -> str | None:
    # What was wrong: checked getCount() or agent counts rather than specific redline identity keys, which could miss wrong resolves or silent failure.
    # How it happened: getCount() reduction alone did not verify that the target redline was the one resolved.
    # Why this change fixes it: compare before and after identity key Counters, requiring target key count to drop by 1 and other keys not to drop.
    before_counts = Counter(before_keys)
    after_counts = Counter(after_keys)
    if before_counts == after_counts:
        return "silent"

    for k, count in before_counts.items():
        if is_agent_token(k[4]) and after_counts[k] < count:
            return "agent"

    target_key = before_keys[index]
    target_dropped = (after_counts[target_key] == before_counts[target_key] - 1)
    other_dropped = any(k != target_key and after_counts[k] < before_counts[k] for k in before_counts)

    if target_dropped and not other_dropped:
        return None

    return "wrong"


def _dispatch(ctx: ToolContext, cmd: str) -> Any:
    smgr = ctx.ctx.ServiceManager
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx.ctx)
    frame = ctx.doc.getCurrentController().getFrame()
    return dispatcher.executeDispatch(frame, cmd, "", 0, ())


def _set_recording(doc: Any, record: bool) -> dict[str, Any]:
    done = "Started" if record else "Stopped"
    doc.setPropertyValue("RecordChanges", record)
    return {"status": "ok", "message": f"{done} recording changes."}


def _describe_redline(redline: Any, index: int, doc: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"index": index}
    for prop in ("RedlineType", "RedlineAuthor", "RedlineComment", "RedlineIdentifier"):
        try:
            entry[prop] = redline.getPropertyValue(prop)
        except Exception:
            log.debug("track_changes_list: redline field unreadable", exc_info=True)
    try:
        dt = redline.getPropertyValue("RedlineDateTime")
        entry["date"] = "%04d-%02d-%02d %02d:%02d" % (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes)
    except Exception:
        log.debug("track_changes_list: redline field unreadable", exc_info=True)
    # Text + location: accept/reject take this index, so the model needs to know WHICH
    # change is which. Without them, mapping "reject the change to clause 4.2" onto an
    # index is guesswork. Redline objects from getRedlines() are property sets (no
    # getAnchor) with RedlineStart/RedlineEnd (XTextRange) and RedlineText (deleted
    # content). Each field is best-effort so one failure never drops the other.
    start = None
    try:
        start = redline.getPropertyValue("RedlineStart")
    except Exception:
        log.debug("track_changes_list: redline field unreadable", exc_info=True)
        start = None
    if start is not None:
        try:
            entry["location"] = describe_match_location(start, doc)
        except Exception:
            log.debug("track_changes_list: redline field unreadable", exc_info=True)
    # Text: for an insertion the live span RedlineStart..RedlineEnd holds it; for a
    # deletion that span is collapsed, so fall back to RedlineText (the removed text).
    txt = ""
    if start is not None:
        try:
            end = redline.getPropertyValue("RedlineEnd")
            stext = start.getText()
            cur = stext.createTextCursorByRange(start)
            cur.gotoRange(end, True)
            txt = cur.getString() or ""
        except Exception:
            log.debug("track_changes_list: redline field unreadable", exc_info=True)
            txt = ""
    if not txt:
        try:
            rt = redline.getPropertyValue("RedlineText")
            txt = (rt.getString() if hasattr(rt, "getString") else str(rt)) if rt is not None else ""
        except Exception:
            log.debug("track_changes_list: redline field unreadable", exc_info=True)
            txt = ""
    if txt:
        entry["text"] = txt if len(txt) <= 300 else txt[:299] + "…"
    return entry


def _calc_track_changes_show_markup(_ctx: Any, _controller: Any, _show: bool) -> dict[str, Any]:
    """Calc: show/hide tracked-change markup from tools is deferred (no stable UNO path yet).

    Writer visibility is document RedlineDisplayType; the missing getter only selects this Calc stub.
    """
    return {
        "status": "ok",
        "message": ("Calc: showing or hiding tracked-change markup from WriterAgent is not supported yet—use Edit - Track Changes - Show (or Review in the tabbed UI) in LibreOffice. track_changes_start / track_changes_stop still control recording."),
        "calc_track_changes_show_unsupported": True,
    }


class TrackChangesStart(WriterAgentSpecialTracking, ToolCalcSpecialTracking):  # type: ignore[misc]
    """Start recording changes."""

    uno_services: list[str] | None = _TRACK_CHANGES_UNO_SERVICES  # type: ignore[assignment]
    name: str | None = "track_changes_start"
    description: str = "Start recording changes (track changes) in the document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _set_recording(ctx.doc, True)


class TrackChangesStop(WriterAgentSpecialTracking, ToolCalcSpecialTracking):  # type: ignore[misc]
    """Stop recording changes."""

    uno_services: list[str] | None = _TRACK_CHANGES_UNO_SERVICES  # type: ignore[assignment]
    name: str | None = "track_changes_stop"
    description: str = "Stop recording changes (track changes) in the document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _set_recording(ctx.doc, False)


class TrackChangesList(WriterAgentSpecialTracking, ToolCalcSpecialTracking):  # type: ignore[misc]
    """List all tracked changes (redlines) in the document."""

    uno_services: list[str] | None = _TRACK_CHANGES_UNO_SERVICES  # type: ignore[assignment]
    name: str | None = "track_changes_list"
    description: str = (
        "List all tracked changes (redlines) in the document, including type, author, date, text "
        "and location. NOTE the two flags: 'recording' is the DOCUMENT's own track-changes toggle; "
        "'agent_review_mode' (off/record/wait) is the user's review setting for AGENT edits — in "
        "record/wait your edits become redlines even while recording is false."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}

    @staticmethod
    def _agent_review_mode(uno_ctx: Any) -> Any | None:
        """Best-effort agent review mode, so 'recording: false' is never read as 'my edits are
        not being tracked' while record/wait mode is active."""
        try:
            from plugin.writer.edit_review import get_agent_edit_review_mode

            return get_agent_edit_review_mode(uno_ctx)
        except Exception:
            log.exception("track_changes_list: could not read agent review mode")
            return None

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        try:
            # What was wrong: failed RecordChanges read was reported as recording: False.
            # How it happened: recording initialized to False and exception swallowed with bare pass.
            # Why this change fixes it: log exception and report recording: None when unreadable.
            recording = doc.getPropertyValue("RecordChanges")
        except Exception:
            log.exception("track_changes_list: could not read RecordChanges")
            recording = None
        agent_mode = self._agent_review_mode(getattr(ctx, "ctx", None))

        if not hasattr(doc, "getRedlines"):
            # What was wrong: documents without getRedlines returned count: 0 and changes: [], looking like empty document.
            # How it happened: returned standard empty list response when API was missing.
            # Why this change fixes it: omit count and changes, set track_changes_list_unsupported: True.
            result: dict[str, Any] = {
                "status": "ok",
                "recording": recording,
                "track_changes_list_unsupported": True,
                "message": (
                    "This document does not expose a redlines API, so this is not an empty change list. "
                    "track_changes_start and track_changes_stop still control recording."
                ),
            }
            if agent_mode is not None:
                result["agent_review_mode"] = agent_mode
            return result

        redline_objs, total = _enumerate_redlines(doc.getRedlines())
        if len(redline_objs) != total:
            return self._tool_error(
                "Couldn't read every tracked change (saw %d of %d). The list was not returned, so indexes would be wrong. Use Edit > Track Changes > Manage."
                % (len(redline_objs), total)
            )
        changes = [_describe_redline(redline, index, doc) for index, redline in enumerate(redline_objs)]
        result = {"status": "ok", "recording": recording, "changes": changes, "count": len(changes)}
        if agent_mode is not None:
            result["agent_review_mode"] = agent_mode
        return result


class TrackChangesShow(WriterAgentSpecialTracking, ToolCalcSpecialTracking):  # type: ignore[misc]
    """Show or hide change markup."""

    uno_services: list[str] | None = _TRACK_CHANGES_UNO_SERVICES  # type: ignore[assignment]
    name: str | None = "track_changes_show"
    description: str = "Show or hide tracked changes markup in the document view (Writer). On Calc, recording still works; this call returns guidance to use LibreOffice menus for show/hide markup until UNO support is implemented."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"show": {"type": "boolean", "description": "True to show changes, False to hide them."}}, "required": ["show"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        show = kwargs.get("show")
        if show is None:
            return self._tool_error("Missing required parameter: show")

        show_b = bool(show)
        # What was wrong: checked controller.getViewSettings to decide Writer vs Calc, but Calc controllers might also implement getViewSettings.
        # How it happened: relied on view service distinction rather than document capability.
        # Why this change fixes it: branch on hasattr(ctx.doc, 'getRedlines') to distinguish Writer from Calc.
        if hasattr(ctx.doc, "getRedlines"):
            # What was wrong: set view ShowChangesInMargin instead of document RedlineDisplayType.
            # How it happened: ShowChangesInMargin is view's margin preference, not markup visibility toggle.
            # Why this change fixes it: write IDL RedlineDisplayType (LO 26.8.1.1 probe):
            # 2 is INSERTED_AND_REMOVED (IsShowChanges), 1 is INSERTED (IsHideChanges).
            # 0 stores as 1 on LO 26.8, 3 is show-original (REMOVED).
            # Boolean ShowChanges False is a no-op while type is 2.
            target_type = 2 if show_b else 1
            ctx.doc.setPropertyValue("RedlineDisplayType", target_type)

            curr_type = ctx.doc.getPropertyValue("RedlineDisplayType")
            curr_sc = ctx.doc.getPropertyValue("ShowChanges")
            success = (curr_type == 2 and curr_sc is True) if show_b else (curr_type == 1 and curr_sc is False)
            if success:
                return {"status": "ok", "message": f"{'Showing' if show_b else 'Hiding'} tracked changes markup."}
            return self._tool_error(
                "Failed to set tracked-changes markup visibility (requested show=%s, RedlineDisplayType=%r, ShowChanges=%r)."
                % (show_b, curr_type, curr_sc),
                code="TOOL_EXECUTION_ERROR",
            )

        controller = ctx.doc.getCurrentController()
        return _calc_track_changes_show_markup(ctx, controller, show_b)


class ManageTrackedChanges(WriterAgentSpecialTracking, ToolCalcSpecialTracking):  # type: ignore[misc]
    """Accept or reject tracked changes: one by index, or all.

    Charts-style fat tool: the four former skinny accept/reject tools shared the same
    UNO dispatch / redline-select path and only differed by verb + optional index.
    """

    uno_services: list[str] | None = _TRACK_CHANGES_UNO_SERVICES  # type: ignore[assignment]
    name: str | None = "manage_tracked_changes"
    description: str = (
        "Accept or reject tracked changes. action=accept/reject requires index "
        "(from track_changes_list). That index is stale after any resolve; call "
        "track_changes_list again before the next accept or reject. action=accept_all/reject_all "
        "resolves every change."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accept", "reject", "accept_all", "reject_all"],
                "description": "accept/reject one change by index, or accept_all/reject_all.",
            },
            "index": {
                "type": "integer",
                "description": "Zero-based index from track_changes_list (required for accept/reject). Call track_changes_list again after each resolve; indexes shift.",
            },
        },
        "required": ["action"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        if action in ("accept_all", "reject_all"):
            return self._execute_all(ctx, is_accept=(action == "accept_all"))
        index = kwargs.get("index")
        if index is None or not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return self._tool_error("Valid integer index is required for action='%s'." % action)
        return self._execute_single(ctx, index, is_accept=(action == "accept"))

    def _execute_all(self, ctx: ToolContext, is_accept: bool) -> dict[str, Any]:
        # Guard: never let the agent bulk-resolve its OWN (wa-review) edits -- those are the human's
        # to review. Allowed when no agent changes are pending (see agent_self_resolution_block_reason).
        from plugin.writer.inline_review import agent_self_resolution_block_reason
        blocked = agent_self_resolution_block_reason(ctx.doc)
        if blocked:
            return self._tool_error(blocked)
        has_redlines = hasattr(ctx.doc, "getRedlines")
        before = 0
        if has_redlines:
            before = int(ctx.doc.getRedlines().getCount())

        cmd = ".uno:AcceptAllTrackedChanges" if is_accept else ".uno:RejectAllTrackedChanges"
        res = _dispatch(ctx, cmd)

        if not has_redlines:
            if res is None:
                err = self._tool_error(
                    "Accepting or rejecting all tracked changes is not supported on this document.",
                    code="UNSUPPORTED",
                )
                # make_tool_error stores kwargs under details; set the flat key directly for callers.
                err["tracked_changes_bulk_unsupported"] = True
                return err
            msg = "All tracked changes accepted." if is_accept else "All tracked changes rejected."
            return {"status": "ok", "message": msg}

        # What was wrong: bulk accept/reject relied on executeDispatch return value, which returns None on success in Writer.
        # How it happened: executeDispatch returns None ('no result') even when dispatch succeeds.
        # Why this change fixes it: judge bulk resolve success by getCount() before and after dispatch on documents with getRedlines.
        after = int(ctx.doc.getRedlines().getCount())
        if before == 0 or after == 0:
            msg = "All tracked changes accepted." if is_accept else "All tracked changes rejected."
            return {"status": "ok", "message": msg}
        if after == before:
            return self._tool_error("Failed to resolve all tracked changes.")

        undo_ok = _undo_resolve(ctx, "manage_tracked_changes: undo of a bad resolve failed")
        verb_all = "Accept-all" if is_accept else "Reject-all"
        msg = f"{verb_all} did not clear every tracked change (left {after}). The resolve was undone."
        if not undo_ok:
            msg += " Undo failed; the document was still changed."
        return self._tool_error(msg)

    def _execute_single(self, ctx: ToolContext, index: int, is_accept: bool) -> dict[str, Any]:
        if not hasattr(ctx.doc, "getRedlines"):
            return self._tool_error("Document does not expose redlines API.")

        redlines = ctx.doc.getRedlines()
        objs, total = _enumerate_redlines(redlines)
        if len(objs) != total:
            return self._tool_error(
                "Couldn't read every tracked change (saw %d of %d), so nothing was resolved."
                % (len(objs), total)
            )

        if index >= len(objs):
            return self._tool_error(f"No tracked change found at index {index}.")

        target_redline = objs[index]

        # Guard: refuse to resolve the agent's OWN edit (a wa-review change). Those are recorded
        # for the human to accept/reject in the review UI; the agent must not do it itself.
        # Fail closed when the change's metadata can't be read.
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

        before_keys = _identity_keys(objs)
        if before_keys is None:
            return self._tool_error(
                "Couldn't read this change's identity, so it can't be resolved from here. "
                "Use Edit > Track Changes > Manage in LibreOffice."
            )

        # To accept/reject a specific change, we select its text range then use the dispatcher.
        # Redline objects from getRedlines() have NO getAnchor() (live probe: AttributeError) —
        # they are property sets exposing RedlineStart/RedlineEnd (XTextRange). The old
        # getAnchor() call meant every real change died here with "Failed to select".
        start = target_redline.getPropertyValue("RedlineStart")
        cur = start.getText().createTextCursorByRange(start)
        end = start
        try:
            end_val = target_redline.getPropertyValue("RedlineEnd")
            if end_val is not None:
                end = end_val
                cur.gotoRange(end, True)
        except Exception:
            # collapsed span (e.g. a deletion) — selecting the start point suffices
            log.debug("manage_tracked_changes: RedlineEnd unreadable; selecting the start point", exc_info=True)

        refusal = _overlap_refusal(index, objs, start, end)
        if refusal is not None:
            return self._tool_error(refusal)

        controller = ctx.doc.getCurrentController()
        previous = None
        try:
            previous = controller.getSelection()
        except Exception:
            log.debug("manage_tracked_changes: could not read the view selection", exc_info=True)
            previous = None

        controller.select(cur)

        try:
            cmd = ".uno:AcceptTrackedChange" if is_accept else ".uno:RejectTrackedChange"
            _dispatch(ctx, cmd)

            objs2, total2 = _enumerate_redlines(redlines)
            if len(objs2) != total2:
                undo_ok = _undo_resolve(ctx, "manage_tracked_changes: undo of a bad resolve failed")
                msg = (
                    "Couldn't re-read tracked changes after the resolve, so it was undone."
                    if undo_ok
                    else "Couldn't re-read tracked changes after the resolve. Undo failed; the document was still changed."
                )
                return self._tool_error(msg)

            after_keys = _identity_keys(objs2)
            if after_keys is None:
                undo_ok = _undo_resolve(ctx, "manage_tracked_changes: undo of a bad resolve failed")
                msg = (
                    "Couldn't re-read tracked changes after the resolve, so it was undone."
                    if undo_ok
                    else "Couldn't re-read tracked changes after the resolve. Undo failed; the document was still changed."
                )
                return self._tool_error(msg)

            outcome = _resolve_outcome(index, before_keys, after_keys)
            if outcome == "silent":
                return self._tool_error("Failed to resolve tracked change: operation silently failed.")

            if outcome == "agent":
                undo_ok = _undo_resolve(ctx, "manage_tracked_changes: undo of a forbidden agent resolution failed")
                msg = "Agent edit was resolved, which is forbidden."
                if not undo_ok:
                    msg += " Undo failed; the change is still resolved."
                return self._tool_error(msg)

            if outcome == "wrong":
                undo_ok = _undo_resolve(ctx, "manage_tracked_changes: undo of a bad resolve failed")
                msg = (
                    "Resolved a different tracked change than index %d. The resolve was undone." % index
                    if undo_ok
                    else "Resolved a different tracked change than index %d. Undo failed; the document was still changed." % index
                )
                return self._tool_error(msg)

            action_str = "Accepted" if is_accept else "Rejected"
            return {
                "status": "ok",
                "message": (
                    "%s tracked change at index %d. Call track_changes_list again before another accept or reject; indexes shift after each resolve."
                    % (action_str, index)
                ),
            }
        finally:
            if previous is not None:
                try:
                    controller.select(previous)
                except Exception:
                    log.exception("manage_tracked_changes: could not restore the view selection")
