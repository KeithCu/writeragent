# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit review session: agent edits land as reviewable tracked changes, and the agent
learns the per-change outcome.

One module owns the whole review story (recording state, author scoping, change tagging,
anchoring, outcome detection, wait-for-review, cleanup), so entry points don't sprinkle
``RecordChanges`` / author toggles around::

    with EditReviewSession(doc, ctx, enabled=flag) as session:
        session.record_mutation(apply_fn)      # one call per logical change
    outcomes = session.wait_for_review(timeout=600)   # polls on the caller's thread

How a change is tracked end to end:

* ``record_mutation`` snapshots the document's redline identifiers, runs the edit, and tags
  every NEW redline's ``RedlineComment`` with a per-change token (``wa-review:<session>:<n>``).
  Completion is "no redline carrying this session's token remains" -- NOT "zero redlines in
  the document" -- so the user's own pre-existing redlines never block or confuse it.
* Each change is anchored with a bookmark (``wa_review_<session>_<n>``) spanning the affected
  range, so it survives positions shifting as other changes are resolved. Bookmarks are
  always removed when the review finishes (success, timeout, or error).
* Outcome detection must survive the redline disappearing on BOTH accept and reject, and the
  user editing the text during review. At record time we derive, from the tracked state, the
  paragraph text as it would read after an accept (skip tracked deletions) and after a reject
  (skip tracked insertions). At review end the anchored paragraph is compared against both:
  equal to the accept form -> ``accepted``; the reject form -> ``rejected``; anything else ->
  ``modified`` (the agent must not assume either text survived).

The session is inert when ``enabled`` is False (edits apply directly, ``wait_for_review``
returns an empty complete result), and degrades to inert if tracking cannot be enabled.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from typing import Any, Callable

log = logging.getLogger(__name__)

# Public: every redline recorded by an EditReviewSession carries a RedlineComment starting
# with this prefix (full form: "wa-review:<session>:<n>"). The review UIs key off it.
TOKEN_PREFIX = "wa-review:"
_BOOKMARK_PREFIX = "wa_review_"
_PREVIEW_MAX_CHARS = 300


def _preview(text: str) -> str:
    """Bound a preview string (a full-document replace would otherwise ship megabytes)."""
    text = text or ""
    if len(text) <= _PREVIEW_MAX_CHARS:
        return text
    return text[: _PREVIEW_MAX_CHARS - 1] + "…"


def review_recording_enabled(ctx: Any) -> bool:
    """True when agent edits must be recorded as reviewable tracked changes.

    writer.require_edit_review implies recording, so EVERY agent edit path must check both
    flags through this helper -- not just writer.track_changes_reviewable.
    """
    from plugin.framework.config import get_config_bool_safe

    return (get_config_bool_safe(ctx, "writer.track_changes_reviewable")
            or get_config_bool_safe(ctx, "writer.require_edit_review"))


def snapshot_redline_ids(doc: Any) -> set:
    """Set of current RedlineIdentifiers -- snapshot before an edit to find the ones it adds."""
    ids = set()
    try:
        enum = doc.getRedlines().createEnumeration()
        while enum.hasMoreElements():
            rl = enum.nextElement()
            try:
                ids.add(rl.getPropertyValue("RedlineIdentifier"))
            except Exception:
                continue
    except Exception:
        pass
    return ids


def tag_agent_redlines(doc: Any, before_ids: set, change_index: int = 0) -> str | None:
    """Stamp a fresh ``wa-review:<session>:<n>`` token on every redline created since
    *before_ids*, marking them as ONE agent change so the inline review UI recognizes them.

    For edit paths that produce their own redlines and only need them reviewable (e.g. the
    streamed extend-selection rewrite, which already collapses to a single tracked change) --
    no anchor/outcome/wait, which the streamed chat path doesn't use. Returns the token, or
    None if nothing new was tagged.
    """
    token = "%s%s:%d" % (TOKEN_PREFIX, uuid.uuid4().hex[:8], change_index)
    tagged = 0
    try:
        enum = doc.getRedlines().createEnumeration()
        while enum.hasMoreElements():
            rl = enum.nextElement()
            try:
                if rl.getPropertyValue("RedlineIdentifier") not in before_ids:
                    rl.setPropertyValue("RedlineComment", token)
                    tagged += 1
            except Exception:
                continue
    except Exception:
        log.debug("tag_agent_redlines: failed", exc_info=True)
        return None
    return token if tagged else None


def _paragraph_cursor_for_range(text_range: Any) -> Any:
    """A text cursor expanded to whole paragraphs around *text_range*."""
    text = text_range.getText()
    cur = text.createTextCursorByRange(text_range.getStart())
    cur.gotoStartOfParagraph(False)
    end = text.createTextCursorByRange(text_range.getEnd())
    end.gotoEndOfParagraph(True)
    cur.gotoRange(end.getEnd(), True)
    return cur


def _string_skipping_redline(text_range: Any, skip_type: str) -> str:
    """Text of *text_range* with portions inside tracked *skip_type* redlines removed.

    ``skip_type="Delete"`` yields the text as it would read if everything were ACCEPTED;
    ``skip_type="Insert"`` yields the text as if everything were REJECTED. Mirrors
    ``document_helpers.get_string_without_tracked_deletions`` but parameterized.
    """
    try:
        para_enum = text_range.createEnumeration()
    except Exception:
        return text_range.getString()

    parts: list[str] = []
    try:
        first = True
        while para_enum.hasMoreElements():
            para = para_enum.nextElement()
            if not first:
                parts.append("\n")
            first = False
            try:
                portion_enum = para.createEnumeration()
            except Exception:
                parts.append(para.getString())
                continue
            skipping = False
            while portion_enum.hasMoreElements():
                portion = portion_enum.nextElement()
                try:
                    portion_type = portion.getPropertyValue("TextPortionType")
                except Exception:
                    continue
                if portion_type == "Redline":
                    try:
                        if str(portion.getPropertyValue("RedlineType")) == skip_type:
                            skipping = not skipping
                    except Exception:
                        pass
                    continue
                if skipping:
                    continue
                try:
                    chunk = portion.getString()
                except Exception:
                    continue
                if chunk:
                    parts.append(chunk)
    except Exception:
        return text_range.getString()
    return "".join(parts)


class ChangeRecord:
    """One reviewable change: its token, anchor, and the two expected end states."""

    def __init__(self, token: str, bookmark: str, accepted_text: str, rejected_text: str,
                 original_preview: str, proposed_preview: str) -> None:
        self.token = token
        self.bookmark = bookmark
        self.accepted_text = accepted_text
        self.rejected_text = rejected_text
        self.original_preview = original_preview
        self.proposed_preview = proposed_preview


class EditReviewSession:
    """Record agent edits as tagged tracked changes and report per-change outcomes."""

    def __init__(self, doc: Any, ctx: Any, enabled: bool) -> None:
        self.doc = doc
        self.ctx = ctx
        self.enabled = bool(enabled)
        self.session_id = uuid.uuid4().hex[:8]
        self.changes: list[ChangeRecord] = []
        self._active = False
        self._was_recording = False
        self._prior_author: tuple[str, str] | None = None
        self._cleaned = False

    # -- session token helpers -------------------------------------------------------------

    def _token(self, n: int) -> str:
        return "%s%s:%d" % (TOKEN_PREFIX, self.session_id, n)

    def _session_token_prefix(self) -> str:
        return "%s%s:" % (TOKEN_PREFIX, self.session_id)

    # -- author scoping --------------------------------------------------------------------
    #
    # Redlines record the author at creation time (read-only afterward). Split authoring
    # (review_authors) sets the INSERT author as the default and lets the replace primitives
    # author their setString("") deletion as the DELETE author -- two authors so LibreOffice's
    # by-author coloring shows insertions and deletions in two distinct colors.

    def _swap_author(self) -> None:
        from plugin.writer import review_authors

        self._prior_author = review_authors.begin(self.ctx)

    def _restore_author(self) -> None:
        from plugin.writer import review_authors

        review_authors.end(self.ctx, self._prior_author)
        self._prior_author = None

    # -- context manager --------------------------------------------------------------------

    def __enter__(self) -> "EditReviewSession":
        if not self.enabled:
            return self
        try:
            self._was_recording = bool(self.doc.getPropertyValue("RecordChanges"))
        except Exception:
            self._was_recording = False
        if not self._was_recording:
            try:
                self.doc.setPropertyValue("RecordChanges", True)
            except Exception:
                # Cannot track: degrade to a direct (unreviewed) edit rather than failing.
                log.warning("EditReviewSession: could not enable RecordChanges; edits will be unreviewed")
                return self
        self._active = True
        self._swap_author()
        # Make the markup visible so the user actually sees what to review.
        try:
            self.doc.setPropertyValue("ShowChanges", True)
        except Exception:
            log.debug("EditReviewSession: could not force ShowChanges", exc_info=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._active:
            return
        self._restore_author()
        if not self._was_recording:
            try:
                self.doc.setPropertyValue("RecordChanges", False)
            except Exception:
                log.warning(
                    "EditReviewSession: failed to restore RecordChanges=False; "
                    "user may be left with Track Changes ON", exc_info=True)
        # Bookmarks stay until wait_for_review/cleanup: outcomes are read after this block.

    # -- recording ---------------------------------------------------------------------------

    def _redline_idents(self) -> set:
        idents = set()
        enum = self.doc.getRedlines().createEnumeration()
        while enum.hasMoreElements():
            rl = enum.nextElement()
            try:
                idents.add(rl.getPropertyValue("RedlineIdentifier"))
            except Exception:
                continue
        return idents

    def record_mutation(self, apply_fn: Callable[[], Any],
                        original_preview: str = "", proposed_preview: str = "") -> Any:
        """Run one logical edit and register it as a reviewable change.

        Call once per logical change (per replaced match, per inserted block) so each gets
        its own accept/reject outcome. Returns ``apply_fn``'s return value.
        """
        if not self._active:
            return apply_fn()

        before = self._redline_idents()
        result = apply_fn()

        new_redlines = []
        enum = self.doc.getRedlines().createEnumeration()
        while enum.hasMoreElements():
            rl = enum.nextElement()
            try:
                if rl.getPropertyValue("RedlineIdentifier") not in before:
                    new_redlines.append(rl)
            except Exception:
                continue
        if not new_redlines:
            return result

        n = len(self.changes)
        token = self._token(n)
        with self._undo_lock():
            for rl in new_redlines:
                try:
                    rl.setPropertyValue("RedlineComment", token)
                except Exception:
                    log.debug("EditReviewSession: could not tag a redline", exc_info=True)

        # Bounding span across the new redlines -> anchor bookmark + expected end states.
        # Built with cursor.gotoRange(range, expand=True): XTextRangeCompare is unreliable on
        # redline ranges (a tracked DELETE's text sits outside the normal flow), so comparing
        # starts/ends can collapse the span for replace changes.
        ranges = []
        for rl in new_redlines:
            try:
                s = rl.getPropertyValue("RedlineStart")
                e = rl.getPropertyValue("RedlineEnd")
            except Exception:
                continue
            if s is not None and e is not None:
                ranges.append((s, e))
        bookmark_name = ""
        accepted_text = rejected_text = ""
        if ranges:
            try:
                span = ranges[0][0].getText().createTextCursorByRange(ranges[0][0])
                for s, e in ranges:
                    span.gotoRange(s, True)  # expand=True grows the span in either direction
                    span.gotoRange(e, True)
                para = _paragraph_cursor_for_range(span)
                accepted_text = _string_skipping_redline(para, "Delete")
                rejected_text = _string_skipping_redline(para, "Insert")
                bookmark_name = "%s%s_%d" % (_BOOKMARK_PREFIX, self.session_id, n)
                bm = self.doc.createInstance("com.sun.star.text.Bookmark")
                bm.setName(bookmark_name)
                with self._undo_lock():
                    para.getText().insertTextContent(para, bm, True)
            except Exception:
                bookmark_name = ""
                log.debug("EditReviewSession: anchoring failed for change %d", n, exc_info=True)

        self.changes.append(ChangeRecord(
            token, bookmark_name, accepted_text, rejected_text,
            original_preview or rejected_text, proposed_preview or accepted_text))
        return result

    # -- review ------------------------------------------------------------------------------

    def _pending_tokens(self) -> set:
        """Tokens of this session's changes that still have an unresolved redline."""
        prefix = self._session_token_prefix()
        pending = set()
        try:
            enum = self.doc.getRedlines().createEnumeration()
            while enum.hasMoreElements():
                rl = enum.nextElement()
                try:
                    comment = str(rl.getPropertyValue("RedlineComment"))
                except Exception:
                    continue
                if comment.startswith(prefix):
                    pending.add(comment)
        except Exception:
            log.debug("EditReviewSession: pending check failed", exc_info=True)
        return pending

    def _paragraph_text_at_anchor(self, record: ChangeRecord) -> str | None:
        """Current whole-paragraph text at the change's bookmark, or None if the anchor is gone."""
        if not record.bookmark:
            return None
        try:
            bookmarks = self.doc.getBookmarks()
            if not bookmarks.hasByName(record.bookmark):
                return None
            anchor = bookmarks.getByName(record.bookmark).getAnchor()
            return _paragraph_cursor_for_range(anchor).getString()
        except Exception:
            log.debug("EditReviewSession: anchor read failed for %s", record.bookmark, exc_info=True)
            return None

    def _outcome(self, record: ChangeRecord, pending_tokens: set) -> str:
        if record.token in pending_tokens:
            return "pending"
        current = self._paragraph_text_at_anchor(record)
        if current is None:
            # Anchor paragraph gone: a rejected pure insertion removes the paragraph (and
            # the bookmark with it); anything else means the user reworked the area.
            return "rejected" if record.rejected_text == "" else "modified"
        if current == record.accepted_text:
            return "accepted"
        if current == record.rejected_text:
            return "rejected"
        return "modified"

    @contextlib.contextmanager
    def _undo_lock(self):
        """Keep our internal bookkeeping (redline tagging, anchor bookmarks) OFF the user's
        undo stack. Without this, the first one or two Ctrl+Z presses after an agent edit only
        toggle our invisible wa_review bookmarks instead of undoing the visible change. Locking
        the document undo manager around those writes is a no-op if the manager is unavailable."""
        manager = None
        try:
            manager = self.doc.getUndoManager()
            manager.lock()
        except Exception:
            manager = None
        try:
            yield
        finally:
            if manager is not None:
                try:
                    manager.unlock()
                except Exception:
                    log.debug("EditReviewSession: undo manager unlock failed", exc_info=True)

    def cleanup(self) -> None:
        """Remove this session's anchor bookmarks. Safe to call more than once."""
        if self._cleaned:
            return
        self._cleaned = True
        try:
            bookmarks = self.doc.getBookmarks()
            with self._undo_lock():
                for record in self.changes:
                    if not record.bookmark:
                        continue
                    try:
                        if bookmarks.hasByName(record.bookmark):
                            bm = bookmarks.getByName(record.bookmark)
                            bm.getAnchor().getText().removeTextContent(bm)
                    except Exception:
                        log.debug("EditReviewSession: bookmark cleanup failed for %s", record.bookmark, exc_info=True)
        except Exception:
            log.debug("EditReviewSession: bookmark cleanup failed", exc_info=True)

    def _review_payload(self, complete: bool, timed_out: bool) -> dict:
        pending = self._pending_tokens()
        return {
            "complete": complete,
            "timed_out": timed_out,
            "changes": [
                {
                    "id": record.token,
                    # Three-state outcome (accepted/rejected/modified, or pending on timeout):
                    # a boolean can't express "the user edited this area during review".
                    "outcome": self._outcome(record, pending),
                    "original_preview": _preview(record.original_preview),
                    "proposed_preview": _preview(record.proposed_preview),
                }
                for record in self.changes
            ],
        }

    def wait_for_review(self, timeout: float, poll: float = 0.3,
                        stop_checker: Callable[[], bool] | None = None,
                        uno_runner: Callable[[Callable[[], Any]], Any] | None = None) -> dict:
        """Block (on the caller's thread) until every change is resolved, then report outcomes.

        Returns ``{"complete", "timed_out", "changes": [{"id", "outcome", ...}]}``. On timeout
        the still-open entries report ``"pending"`` and ``complete`` is False -- the agent must
        not assume the text's state. *stop_checker* lets the caller abort early (e.g. the
        review feature was toggled off mid-wait); that also returns ``complete=False``.

        Thread placement: poll from a background/HTTP thread so the main thread stays free for
        the user's accept/reject clicks. UNO is not thread-safe, so callers off the main thread
        pass ``uno_runner`` (e.g. ``execute_on_main_thread``) and every document read/cleanup in
        the loop is marshalled through it; the sleep itself stays on the calling thread.
        """
        run = uno_runner if uno_runner is not None else (lambda fn: fn())
        if not self._active or not self.changes:
            try:
                return {"complete": True, "timed_out": False, "changes": []}
            finally:
                run(self.cleanup)
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            timed_out = False
            while run(self._pending_tokens):
                if stop_checker is not None and stop_checker():
                    return run(lambda: self._review_payload(complete=False, timed_out=False))
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(poll)
            return run(lambda: self._review_payload(complete=not timed_out, timed_out=timed_out))
        finally:
            run(self.cleanup)
