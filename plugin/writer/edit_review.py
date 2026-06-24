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


_AGENT_EDIT_REVIEW_MODES = frozenset({"off", "record", "wait"})


def get_agent_edit_review_mode(ctx: Any) -> str:
    """Read ``doc.agent_edit_review_mode`` (off / record / wait); unknown values → off."""
    from plugin.framework.config import get_config

    raw = get_config(ctx, "doc.agent_edit_review_mode")
    if isinstance(raw, str):
        mode = raw.strip().lower()
        if mode in _AGENT_EDIT_REVIEW_MODES:
            return mode
    return "off"


def review_recording_enabled(ctx: Any) -> bool:
    """True when agent edits must be recorded as reviewable tracked changes.

    Every agent edit path must use this helper (or ``get_agent_edit_review_mode``) — not the
    raw config key.
    """
    return get_agent_edit_review_mode(ctx) in ("record", "wait")


def edit_review_wait_seconds(ctx: Any) -> int:
    """Max seconds ``apply_document_content`` may block waiting for review; 0 = don't wait."""
    from plugin.framework.config import get_config_int_safe

    if get_agent_edit_review_mode(ctx) != "wait":
        return 0
    return max(0, get_config_int_safe(ctx, "doc.edit_review_timeout"))


def snapshot_redline_ids(doc: Any) -> tuple[set, bool]:
    """``(set of current RedlineIdentifiers, reliable)`` -- snapshot BEFORE an edit so the edit's NEW
    redlines can be found by difference (ids not in this set).

    ``reliable`` is False when the snapshot is INCOMPLETE: the enumeration can't start/finish, stops
    short of getCount() (yields fewer items than getCount() reports), or a redline's identifier can't
    be read. A partial BEFORE snapshot is a data-loss hazard -- a pre-existing USER redline missing
    from it would look "new" after the edit and be stamped with an agent token, after which
    Accept/Reject All would resolve the user's own change. So callers MUST refuse to tag on an
    unreliable snapshot.

    On the count/enumeration mismatch (seen < getCount()): this is a DEFENSIVE INVARIANT, not an
    observed bug. getRedlines() is one flat table and the mismatch has NOT been seen in real
    LibreOffice (a native test confirms count == enumeration length, even across body and
    table-cell containers). It is kept as cheap fail-closed insurance only because IF it ever
    happened the cost would be the silent loss of a user's own redline. The same neutral
    "fewer items than getCount() reports" framing is used by the sibling scan helpers below."""
    ids: set = set()
    try:
        redlines = doc.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        return ids, False  # can't enumerate/count -> can't tell new from pre-existing -> unreliable
    reliable = True
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
        except Exception:
            return ids, False  # can't advance the enumeration -> incomplete -> unreliable
        seen += 1
        try:
            ids.add(rl.getPropertyValue("RedlineIdentifier"))
        except Exception:
            reliable = False  # an existing redline we can't identify -> can't exclude it from "new"
    if seen != total:
        reliable = False  # enumeration shorter than getCount() -> a pre-existing redline may be unseen
    return ids, reliable


def _new_redlines_complete(doc: Any, before_ids: set) -> tuple[list, bool]:
    """``([redlines whose RedlineIdentifier is NOT in before_ids], reliable)`` -- the redlines an edit
    just added. ``reliable`` is False when the post-edit scan is INCOMPLETE (enum/count error, a
    count/enumeration mismatch, or an unreadable identifier), so a caller can refuse to tag a
    HALF-found change rather than tag only part of a Delete/Insert pair."""
    out: list = []
    try:
        redlines = doc.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        return out, False
    reliable = True
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
        except Exception:
            return out, False
        seen += 1
        try:
            rid = rl.getPropertyValue("RedlineIdentifier")
        except Exception:
            reliable = False  # can't classify new-vs-pre-existing for this one -> incomplete
            continue
        if rid not in before_ids:
            out.append(rl)
    if seen != total:
        reliable = False  # enumeration shorter than getCount() -> a new redline of this change may be unseen
    return out, reliable


def _tag_new_redlines(redlines: list, token: str) -> tuple[bool, int]:
    """Stamp *token* (RedlineComment) on every redline -- ALL-OR-NOTHING. Returns
    ``(success, orphans_remaining)`` as TWO separate values so success can never be confused with a
    count (a single int made "n tagged ok" and "n orphans after a failure" collide
    when n == len):

      * ``(True, 0)``  -> every redline tagged (success);
      * ``(False, 0)`` -> a failure that was FULLY reverted (NO redline left carrying the token);
      * ``(False, n)`` -> a failure where ``n`` redlines still carry the token and could not be
        cleared. Only reachable if setPropertyValue fails on a redline we just set (a broken UNO
        state) -- the tag genuinely cannot be removed via the API then.

    Any path through the ``except`` is a FAILURE (``success=False``), even if every redline happened
    to end up tagged -- the caller must not register a change reached through the error path. On
    failure the revert sweep includes the redline whose set JUST raised (setPropertyValue can mutate
    the comment and THEN throw, so it may carry the token though it never entered ``applied``);
    after attempting to clear each it READS the comment back and counts only those that
    still carry the token (or can't be read) as orphans."""
    applied: list = []
    for rl in redlines:
        try:
            rl.setPropertyValue("RedlineComment", token)
            applied.append(rl)
        except Exception:
            log.warning("EditReviewSession: tagging a redline failed; reverting the partial tag set",
                        exc_info=True)
            orphans = 0
            for done in applied + [rl]:  # include the just-failed redline -- its set may have mutated
                try:
                    done.setPropertyValue("RedlineComment", "")
                except Exception:
                    log.warning("EditReviewSession: reverting a redline tag failed", exc_info=True)
                try:
                    if str(done.getPropertyValue("RedlineComment")) == token:
                        orphans += 1  # still carries our token -> a real orphan we could not remove
                except Exception:
                    orphans += 1  # can't confirm it's clean -> count conservatively (fail closed)
            return False, orphans  # FAILURE -- orphans>0 means tag(s) remain we could not remove
    return True, 0


def tag_agent_redlines(doc: Any, before_ids: set, change_index: int = 0,
                       before_reliable: bool = False) -> str | None:
    """Stamp a fresh ``wa-review:<session>:<n>`` token on every redline created since
    *before_ids*, marking them as ONE agent change so the inline review UI recognizes them.

    For edit paths that produce their own redlines and only need them reviewable (e.g. the
    streamed extend-selection rewrite, which already collapses to a single tracked change) --
    no anchor/outcome/wait, which the streamed chat path doesn't use. Returns the token, or
    None if nothing new was tagged.

    Refuses (returns None) when *before_reliable* is False: a partial BEFORE snapshot could
    misclassify a pre-existing USER redline as new and stamp it as an agent change, after which
    Accept/Reject All would resolve the user's own change. The edit still stands;
    its redlines just stay untagged -> treated as the user's -> never auto-resolved. *before_reliable*
    defaults to False (fail closed): a caller must explicitly assert a verified-complete snapshot
    (the boolean returned by ``snapshot_redline_ids``) to enable tagging."""
    if not before_reliable:
        log.warning("tag_agent_redlines: pre-edit snapshot unreliable; not tagging (avoids "
                    "mis-tagging a user redline as an agent change)")
        return None
    # Find the new redlines with a COMPLETE post-edit scan; if it's incomplete we can't be sure we
    # found the whole change, so refuse rather than tag a fragment.
    new_redlines, after_ok = _new_redlines_complete(doc, before_ids)
    if not after_ok:
        log.warning("tag_agent_redlines: post-edit redline scan incomplete; not tagging (avoids a "
                    "half-tagged change)")
        return None
    if not new_redlines:
        return None
    token = "%s%s:%d" % (TOKEN_PREFIX, uuid.uuid4().hex[:8], change_index)
    success, orphans = _tag_new_redlines(new_redlines, token)  # all-or-nothing (reverts on failure)
    if not success:
        # Not a success path -- do NOT register. orphans == 0 -> clean revert; orphans > 0 -> the
        # revert could not remove every tag, so surface the residual loudly.
        if orphans:
            log.warning("tag_agent_redlines: tagging failed and %d orphan tag(s) could not be "
                        "reverted; not registering this change", orphans)
        return None
    # Full success. Streamed edit paths tag their redlines here instead of via record_mutation, so
    # this is where they must reveal the review fast-travel toolbar (#2) -- otherwise it never appears
    # for those edits. Best-effort/silent; runs on the edit's (main) thread.
    try:
        from plugin.writer.review_toolbar import refresh_review_toolbar

        refresh_review_toolbar(doc)
    except Exception:
        log.debug("tag_agent_redlines: toolbar refresh failed", exc_info=True)
    return token


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

    def _redline_idents(self) -> tuple[set, bool]:
        """``(current RedlineIdentifiers, reliable)`` -- see ``snapshot_redline_ids``. reliable=False
        means the snapshot is incomplete and must NOT back a new-vs-pre-existing tagging decision."""
        return snapshot_redline_ids(self.doc)

    def record_mutation(self, apply_fn: Callable[[], Any],
                        original_preview: str = "", proposed_preview: str = "") -> Any:
        """Run one logical edit and register it as a reviewable change.

        Call once per logical change (per replaced match, per inserted block) so each gets
        its own accept/reject outcome. Returns ``apply_fn``'s return value.
        """
        if not self._active:
            return apply_fn()

        before, before_ok = self._redline_idents()
        result = apply_fn()
        if not before_ok:
            # The pre-edit redline snapshot was incomplete, so we can't reliably tell which redlines
            # are NEW (ours) from pre-existing ones (the user's). Tagging now could stamp a user's
            # redline as an agent change -> Accept/Reject All would later resolve it. Fail closed:
            # the edit still applied, but we DON'T tag or register it -- its redlines stay untagged,
            # so they read as the user's own and are never auto-resolved.
            log.warning("EditReviewSession: pre-edit redline snapshot unreliable; leaving this edit "
                        "untagged (not a reviewable agent change) to avoid mis-tagging user redlines")
            return result

        # Find the new redlines with a COMPLETE post-edit scan. If incomplete we can't be sure we
        # found the whole change (e.g. only one mark of a Delete/Insert pair), so fail closed: leave
        # the edit untagged rather than register a fragment.
        new_redlines, after_ok = _new_redlines_complete(self.doc, before)
        if not after_ok:
            log.warning("EditReviewSession: post-edit redline scan incomplete; leaving this edit "
                        "untagged to avoid registering a half-tagged change")
            return result
        if not new_redlines:
            return result

        n = len(self.changes)
        token = self._token(n)
        with self._undo_lock():
            # All-or-nothing: register ONLY on full success. On any failure the partial set is
            # reverted; orphans==0 is a clean revert, orphans>0 means a tag could not be removed
            # (broken UNO state) -- surface it loudly. Either way leave the edit unregistered.
            success, orphans = _tag_new_redlines(new_redlines, token)
        if not success:
            if orphans:
                log.warning("EditReviewSession: tagging failed and %d orphan tag(s) could not be "
                            "reverted; leaving this edit unregistered", orphans)
            else:
                log.warning("EditReviewSession: tagging failed and was reverted; leaving this edit "
                            "untagged (not a reviewable agent change)")
            return result

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
                # Scope the captured states AND the anchor bookmark to the CHANGE's own redline
                # span, not the whole paragraph -- so when several changes share a paragraph each
                # one's outcome/final_text reflects only itself, and a change deep in a long
                # paragraph is never truncated out of the preview.
                accepted_text = _string_skipping_redline(span, "Delete")
                rejected_text = _string_skipping_redline(span, "Insert")
                bookmark_name = "%s%s_%d" % (_BOOKMARK_PREFIX, self.session_id, n)
                bm = self.doc.createInstance("com.sun.star.text.Bookmark")
                bm.setName(bookmark_name)
                with self._undo_lock():
                    span.getText().insertTextContent(span, bm, True)
            except Exception:
                bookmark_name = ""
                log.debug("EditReviewSession: anchoring failed for change %d", n, exc_info=True)

        self.changes.append(ChangeRecord(
            token, bookmark_name, accepted_text, rejected_text,
            original_preview or rejected_text, proposed_preview or accepted_text))
        # A new pending change exists -> reveal the review fast-travel toolbar (#2). Runs on the
        # main thread (the edit does), so the LayoutManager call is safe; best-effort/silent.
        try:
            from plugin.writer.review_toolbar import refresh_review_toolbar

            refresh_review_toolbar(self.doc)
        except Exception:
            log.debug("EditReviewSession: toolbar refresh after record failed", exc_info=True)
        return result

    # -- review ------------------------------------------------------------------------------

    def _pending_tokens(self) -> tuple[set, bool]:
        """``(tokens of this session's changes that still have an unresolved redline, reliable)``.

        ``reliable`` is False when the scan is INCOMPLETE (enum/count error, a count/enumeration
        mismatch, or an unreadable comment): an under-counted pending set could make ``wait_for_review`` declare
        the review complete while a change is still open, so the caller must treat unreliable as
        "not yet complete" rather than done (guard every enumeration)."""
        prefix = self._session_token_prefix()
        pending: set = set()
        try:
            redlines = self.doc.getRedlines()
            total = int(redlines.getCount())
            enum = redlines.createEnumeration()
        except Exception:
            log.debug("EditReviewSession: pending check could not enumerate/count", exc_info=True)
            return pending, False
        reliable = True
        seen = 0
        while True:
            try:
                if not enum.hasMoreElements():
                    break
                rl = enum.nextElement()
            except Exception:
                log.debug("EditReviewSession: pending check could not advance", exc_info=True)
                return pending, False
            seen += 1
            try:
                comment = str(rl.getPropertyValue("RedlineComment"))
            except Exception:
                reliable = False  # an unreadable comment might be one of ours -> can't confirm done
                continue
            if comment.startswith(prefix):
                pending.add(comment)
        if seen != total:
            reliable = False  # enumeration shorter than getCount() -> an unresolved change may be in the unseen tail
        return pending, reliable

    def _change_text_at_anchor(self, record: ChangeRecord) -> str | None:
        """Current text of the CHANGE's own region (its anchor bookmark span), or None if the anchor
        is gone. Scoped to the change, not the whole paragraph, so a neighbouring change sharing the
        paragraph never contaminates this one's reported text."""
        if not record.bookmark:
            return None
        try:
            bookmarks = self.doc.getBookmarks()
            if not bookmarks.hasByName(record.bookmark):
                return None
            anchor = bookmarks.getByName(record.bookmark).getAnchor()
            # Skip tracked DELETIONS so the text reads as the region WOULD after accepting, instead
            # of gluing struck text to the insertion (e.g. "quickfast"). _outcome is unaffected: a
            # RESOLVED change has no redlines left, so this equals the raw string there; for a
            # pending change _outcome short-circuits to "pending" before comparing text.
            cur = anchor.getText().createTextCursorByRange(anchor)
            return _string_skipping_redline(cur, "Delete")
        except Exception:
            log.debug("EditReviewSession: anchor read failed for %s", record.bookmark, exc_info=True)
            return None

    def _outcome(self, record: ChangeRecord, pending_tokens: set, pending_reliable: bool) -> str:
        if record.token in pending_tokens:
            return "pending"
        if not pending_reliable:
            # The pending scan was incomplete, so "not in pending_tokens" does NOT prove this change
            # is resolved -- it might be unresolved but unseen. Report "pending" rather than guess an
            # accepted/rejected/modified outcome from the anchor text.
            return "pending"
        current = self._change_text_at_anchor(record)
        if current is None:
            # The anchor bookmark is gone: a rejected pure insertion can take its whole span (and
            # the bookmark) with it; anything else means the user reworked/removed the area.
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
                    # A stuck lock can later make a surgical rollback's undo() throw (the manager is
                    # locked), silently degrading atomicity -- surface it, don't bury at debug.
                    log.warning("EditReviewSession: undo manager unlock failed; the undo manager may "
                                "be left locked", exc_info=True)

    def _remove_anchor_bookmarks(self, records) -> None:
        """Remove the anchor bookmarks of *records*, under the undo lock so the removal stays off the
        user's undo stack. Best-effort: any failure is logged at debug, never raised. Shared by
        cleanup() and discard_changes_since()."""
        try:
            bookmarks = self.doc.getBookmarks()
            with self._undo_lock():
                for record in records:
                    if not record.bookmark:
                        continue
                    try:
                        if bookmarks.hasByName(record.bookmark):
                            bm = bookmarks.getByName(record.bookmark)
                            bm.getAnchor().getText().removeTextContent(bm)
                    except Exception:
                        log.debug("EditReviewSession: anchor bookmark removal failed for %s",
                                  record.bookmark, exc_info=True)
        except Exception:
            log.debug("EditReviewSession: anchor bookmark removal failed", exc_info=True)

    def cleanup(self) -> None:
        """Remove this session's anchor bookmarks. Safe to call more than once."""
        if self._cleaned:
            return
        self._cleaned = True
        if not self._active or not self.changes:
            return
        self._remove_anchor_bookmarks(self.changes)

    def discard_changes_since(self, count: int) -> None:
        """Drop change records recorded after index *count*, removing their anchor bookmarks.

        Used to roll back a partially-applied batch (a surgical multi-edit that failed mid-apply):
        the caller undoes the document mutations, then calls this so neither the change
        list nor the document is left holding records/bookmarks for edits that no longer exist.
        Best-effort and self-contained: bookmark removal runs under the undo lock (kept off the
        user's undo stack) and any failure is logged, never raised."""
        if count < 0 or count >= len(self.changes):
            return
        doomed = self.changes[count:]
        del self.changes[count:]
        if not self._active:
            return
        self._remove_anchor_bookmarks(doomed)

    def _review_payload(self, complete: bool, timed_out: bool) -> dict:
        # Derive BOTH the header and the per-change outcomes from THIS one scan so they can never
        # disagree. Carry reliability into _outcome (an unreliable scan -> "pending", not a guessed
        # outcome), AND only report complete when this same scan is reliable and shows nothing pending
        # (never upgrade a False). Otherwise a transient unreliable/non-empty re-scan here could pair
        # complete=True with all-"pending" outcomes -- an internally contradictory report.
        pending, pending_ok = self._pending_tokens()
        complete = complete and pending_ok and not pending
        return {
            "complete": complete,
            "timed_out": timed_out,
            "changes": [
                {
                    "id": record.token,
                    # Three-state outcome (accepted/rejected/modified, or pending on timeout/unverified):
                    # a boolean can't express "the user edited this area during review".
                    "outcome": self._outcome(record, pending, pending_ok),
                    "original_preview": _preview(record.original_preview),
                    "proposed_preview": _preview(record.proposed_preview),
                    # The region's text as it reads NOW (after the user's accept/reject/edit), so the
                    # agent knows what actually resulted -- not just what it proposed. "" if the
                    # anchor paragraph is gone (e.g. a rejected pure insertion removed it).
                    "final_text": _preview(self._change_text_at_anchor(record) or ""),
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
            while True:
                pending, reliable = run(self._pending_tokens)
                # Done ONLY on a reliable, empty scan. An unreliable scan (or remaining tokens) keeps
                # waiting -- never declare the review complete off a partial read that might have
                # missed an unresolved change (fail closed).
                if reliable and not pending:
                    break
                if stop_checker is not None and stop_checker():
                    return run(lambda: self._review_payload(complete=False, timed_out=False))
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(poll)
            return run(lambda: self._review_payload(complete=not timed_out, timed_out=timed_out))
        finally:
            run(self.cleanup)
