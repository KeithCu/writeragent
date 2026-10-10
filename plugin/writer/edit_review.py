# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit review session: agent edits land as reviewable tracked changes, and the agent
learns the per-change outcome.

We keep the session, streamed sessions and surgical helpers in one module, not split them, because they share the undo/redline helpers and many call sites import from here.

One module owns the whole review story (recording state, author scoping, change tagging,
anchoring, outcome detection, wait-for-review, cleanup), so entry points don't sprinkle
``RecordChanges`` / author toggles around::

    with EditReviewSession(doc, ctx, enabled=flag) as session:
        session.record_mutation(apply_fn)      # one call per logical change
    outcomes = session.wait_for_review(timeout=600)   # polls on the caller's thread

How a change is tracked end to end:

* ``record_mutation`` snapshots the document's redline identifiers, runs the edit, and tags
  every NEW redline's ``RedlineComment`` with a per-change token (``wa-review:<session>:<n>``).
  Completion is a reliable empty intersection of those comments with tokens on registered
  ``ChangeRecord``s — NOT "zero redlines in the document", and NOT "every
  ``wa-review:<session>:`` comment is gone". A tag left behind when the change was not
  registered (tagging failed, or the missing-bookmark clear left the token) must not keep
  ``wait_for_review`` blocked. An incomplete scan still fail-closes. The user's own
  pre-existing redlines never block or confuse it.
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
import itertools
import logging
import os
import time
import uuid
from typing import Any, Callable, ClassVar, Iterator

from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.writer import review_scan as _review_scan

log = logging.getLogger(__name__)

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

    raw = get_config("doc.agent_edit_review_mode")
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
    return max(0, get_config_int_safe("doc.edit_review_timeout"))


def _tag_new_redlines(redlines: list[Any], token: str) -> tuple[bool, int]:
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
    applied: list[Any] = []
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


def tag_agent_redlines(doc: Any, before_ids: set[Any], change_index: int = 0,
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
    new_redlines, after_ok = _review_scan.new_redlines_since(doc, before_ids)
    if not after_ok:
        log.warning("tag_agent_redlines: post-edit redline scan incomplete; not tagging (avoids a "
                    "half-tagged change)")
        return None
    if not new_redlines:
        return None
    token = _review_scan.make_agent_token(uuid.uuid4().hex[:8], change_index)
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
    ``text_helpers.get_string_without_tracked_deletions`` but parameterized.
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

    token: str
    bookmark: str
    accepted_text: str
    rejected_text: str
    original_preview: str
    proposed_preview: str

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

    doc: Any
    ctx: Any
    enabled: bool
    session_id: str
    _active: bool
    _was_recording: bool
    _cleaned: bool

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
        return _review_scan.make_agent_token(self.session_id, n)

    def _session_token_prefix(self) -> str:
        return _review_scan.session_token_prefix(self.session_id)

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

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
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

    def _redline_idents(self) -> tuple[set[Any], bool]:
        """``(current redline keys, reliable)`` -- see ``snapshot_redline_ids``. reliable=False
        means the snapshot is incomplete and must NOT back a new-vs-pre-existing tagging decision."""
        return _review_scan.snapshot_redline_ids(self.doc)

    def record_mutation(self, apply_fn: Callable[[], Any],
                        original_preview: str = "", proposed_preview: str = "") -> Any:
        """Run one logical edit and register it as a reviewable change.

        Call once per logical change (per replaced match, per inserted block) so each gets
        its own accept/reject outcome. Returns ``apply_fn``'s return value.
        """
        if not self._active:
            return apply_fn()

        # We rescan all redlines per edit, not track deltas, because only a complete scan lets us fail closed; batches are capped at _MAX_SURGICAL_RUNS.
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
        new_redlines, after_ok = _review_scan.new_redlines_since(self.doc, before)
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
                # The token can remain on the redline. It is not registered, and
                # _pending_tokens will not treat that leftover as a reason to keep waiting.
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

        # No bookmark → do not register. ChangeRecord with bookmark="" makes _outcome guess
        # rejected/modified (current is None). That is a lie to the model. Same as failed tagging:
        # the edit already applied; it stays untagged. Do not "fix" this by appending and
        # special-casing _outcome — empty bookmark is not the same as a bookmark the user later
        # removed (pure-insert reject).
        # A failed clear can leave wa-review:<session>:<n> on the redline. That orphan is not
        # registered. _pending_tokens ignores tokens that are not on a ChangeRecord, so
        # wait_for_review still finishes once every registered change is resolved. Keep this
        # untag attempt: when it succeeds the redline comment is cleared.
        if not bookmark_name:
            log.warning(
                "EditReviewSession: no review anchor for change %d; leaving this edit untagged "
                "(not a reviewable agent change)",
                n,
            )
            with self._undo_lock():
                _tag_new_redlines(new_redlines, "")
            return result

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

    def _pending_tokens(self) -> tuple[set[str], bool]:
        """``(registered tokens that still have an unresolved redline, reliable)``.

        The scan still collects every ``RedlineComment`` that starts with this session's
        prefix. The returned set is the intersection with tokens on ``ChangeRecord``s.

        A failed tag, or a missing-bookmark clear that left orphans, does
        not register a change, but the redline can still carry
        ``wa-review:<session>:<n>``. ``wait_for_review`` must not loop
        until every such comment is gone. After the user resolves the
        registered change, the orphan tag — which has no review-UI row —
        keeps the wait going until timeout.

        An unregistered token must not block completion. A registered
        token that is still on a redline still waits. ``reliable`` stays
        False when the scan is incomplete (enum/count error, a
        count/enumeration mismatch, or an unreadable comment). An empty
        intersection from a partial scan is not completion — the caller
        fail-closes and keeps waiting, because the unseen tail might
        still hold a registered change.
        """
        prefix = self._session_token_prefix()
        registered = {record.token for record in self.changes}
        seen: set[str] = set()

        def on_item(rl: Any) -> bool:
            try:
                comment = str(rl.getPropertyValue("RedlineComment"))
            except Exception:
                return False
            if comment.startswith(prefix):
                seen.add(comment)
            return True

        reliable = _review_scan.scan_redlines(self.doc, on_item)[0]
        if not reliable:
            log.debug("EditReviewSession: pending check enumeration incomplete", exc_info=False)
        return seen & registered, reliable

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

    def _outcome(self, record: ChangeRecord, pending_tokens: set[str], pending_reliable: bool) -> str:
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
            # the bookmark) with it; an accepted pure deletion does the same; anything else means
            # the user reworked/removed the area.
            if record.rejected_text == "":
                return "rejected"
            if record.accepted_text == "":
                return "accepted"
            return "modified"
        if current == record.accepted_text:
            return "accepted"
        if current == record.rejected_text:
            return "rejected"
        return "modified"

    @contextlib.contextmanager
    def _undo_lock(self) -> Iterator[None]:
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

    def _remove_anchor_bookmarks(self, records: list[ChangeRecord]) -> None:
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
        # We do not reject record_mutation after cleanup, because cleanup runs only from wait_for_review after the last edit.
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

    def _review_payload(self, complete: bool, timed_out: bool) -> dict[str, Any]:
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
                    # We read the anchor again here, not reuse _outcome's read, because this runs once per change after review and keeps _outcome self-contained.
                    "final_text": _preview(self._change_text_at_anchor(record) or ""),
                }
                for record in self.changes
            ],
        }

    def wait_for_review(self, timeout: float, poll: float = 0.3,
                        stop_checker: Callable[[], bool] | None = None,
                        uno_runner: Callable[[Callable[[], Any]], Any] | None = None) -> dict[str, Any]:
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
                # execute_safe skips the pre-execute disposed probe when is_async() (review-wait
                # runs off the main thread). This loop is that probe. Do not add assert_main_thread
                # here and do not force the execute_safe check onto async tools.
                from plugin.framework.errors import is_document_disposed

                if run(lambda: is_document_disposed(self.doc)):
                    # Dead doc, not a user-timeout. Unreliable getRedlines() is a *different*
                    # signal (incomplete enum on a live doc) — never treat that as complete
                    # (would resolve the user's own redlines) and never equate it with dispose.
                    return run(lambda: self._review_payload(complete=False, timed_out=False))
                pending, reliable = run(self._pending_tokens)
                # Done ONLY on a reliable empty intersection of scanned tags with registered
                # change tokens. An orphan tag (not on a ChangeRecord) does not count. An
                # unreliable scan keeps waiting — never complete off a partial read that might
                # have missed an unresolved registered change (fail closed).
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


# ---------------------------------------------------------------------------
# Surgical Diff, Atomic Redline Undo Sessions, & Post-Edit Context Echo
# ---------------------------------------------------------------------------


def _env_num(name: str, default: Any, cast: Any, ok: Any) -> Any:
    """Read tuning knob *name* from the environment, *cast* it, and return it only if *ok(v)*;
    otherwise the fixed *default*. Never raises -- a bad env value must never break an edit."""
    try:
        v = cast(os.environ[name])
        return v if ok(v) else default
    except (KeyError, ValueError, TypeError):
        return default


_WORD_DIFF_THRESHOLD = _env_num(
    "WRITERAGENT_AGENT_EDIT_DIFF_THRESHOLD", 0.6, float, lambda v: 0.0 <= v <= 1.0)

_MAX_SURGICAL_RUNS = _env_num(
    "WRITERAGENT_AGENT_EDIT_MAX_SURGICAL_RUNS", 40, int, lambda v: v >= 1)

_SPLIT_AUTHOR_COLORS = os.environ.get(
    "WRITERAGENT_AGENT_EDIT_SPLIT_AUTHOR_COLORS", "1").strip().lower() not in ("0", "false", "no", "off", "")

_GO_RIGHT_CHUNK = 8192


def _go_right(cursor: Any, n: int, expand: bool) -> bool:
    """Move (expand=False) or extend (expand=True) the cursor right by *n* chars, in chunks of
    _GO_RIGHT_CHUNK (UNO caps the count at a C++ short). Returns True only if the FULL *n* was
    consumed; False if goRight stopped early (end of text / an unexpected stop), so the caller can
    refuse to edit at a wrong offset instead of silently landing short."""
    while n > 0:
        step = n if n < _GO_RIGHT_CHUNK else _GO_RIGHT_CHUNK
        if not cursor.goRight(step, expand):
            return False
        n -= step
    return True


_OFFSET_SAFE_PORTION_TYPES = frozenset({"Text", "SoftPageBreak"})


def _block_safe_for_surgical(found: Any) -> bool:
    """True only when *found* is a SINGLE paragraph whose portions are all offset-safe (plain text
    or an automatic page break) and which has no tracked changes -- the case where getString() char
    offsets line up with the live cursor's goRight stops. A multi-paragraph block, a struck
    (tracked-deletion) run, or a real content portion (field/footnote/etc.) makes them diverge, so
    the surgical sub-edits would land in the wrong place. Best-effort: any doubt -> False, and the
    caller falls back to the whole-block replace (which handles every case).
    """
    try:
        if found.getString() != _string_skipping_redline(found, "Delete"):
            return False
        paras = found.createEnumeration()
        seen = 0
        while paras.hasMoreElements():
            para = paras.nextElement()
            seen += 1
            if seen > 1 or not para.supportsService("com.sun.star.text.Paragraph"):
                return False  # multi-paragraph or a table/other node
            portions = para.createEnumeration()
            while portions.hasMoreElements():
                if str(portions.nextElement().TextPortionType) not in _OFFSET_SAFE_PORTION_TYPES:
                    return False  # field / footnote / ruby / redline mark -> offsets diverge
        return seen == 1
    except Exception:
        log.debug("edit_review: _block_safe_for_surgical check failed; treating as unsafe", exc_info=True)
        return False


_SURGICAL_UNDO_TITLE = "WriterAgent surgical edit"
_surgical_batch_counter = itertools.count(1)


def next_surgical_undo_title() -> str:
    """A process-unique grouped-undo title for one surgical batch (base + a monotonic counter)."""
    return "%s#%d" % (_SURGICAL_UNDO_TITLE, next(_surgical_batch_counter))


_AGENT_EDIT_UNDO_TITLE = "WriterAgent edit"


def next_agent_edit_undo_title() -> str:
    """A process-unique grouped-undo title for one HTML/import edit (shares the monotonic counter)."""
    return "%s#%d" % (_AGENT_EDIT_UNDO_TITLE, next(_surgical_batch_counter))


def _try_enter_undo_context(doc: Any, title: str) -> Any | None:
    try:
        mgr = doc.getUndoManager()
        if mgr.isLocked():
            raise RuntimeError("undo manager is locked; enterUndoContext would be a no-op")
        mgr.enterUndoContext(title)
        return mgr
    except Exception:
        return None


def close_surgical_context(undo_mgr: Any, session: EditReviewSession, changes_before: int,
                           applied_ok: bool, undo_title: str) -> None:
    """Close the surgical undo context -- pairing the earlier enterUndoContext exactly once -- and,
    on failure, roll the partial batch back."""
    left = False
    try:
        undo_mgr.leaveUndoContext()
        left = True
    except Exception:
        log.warning("edit_review: leaveUndoContext failed; the undo stack may be inconsistent", exc_info=True)

    if applied_ok:
        return  # success: edits stand; a failed leave was already surfaced above

    undone = False
    if left:
        try:
            titles = undo_mgr.getAllUndoActionTitles()  # newest-first per XUndoManager
            if titles and titles[0] == undo_title:
                undo_mgr.undo()
                undone = True
        except Exception:
            log.warning("edit_review: undo of partial surgical batch failed; document may be partially "
                        "edited", exc_info=True)
    # Trim records when the document mutations were reverted, or when nothing was applied at all
    # (empty context -> changes unchanged). Keep them when undo() demonstrably failed (or couldn't
    # run because the context wouldn't close) so a live partial edit keeps a reviewable record.
    kept = len(session.changes) - changes_before
    if undone or kept == 0:
        try:
            session.discard_changes_since(changes_before)
        except Exception:
            log.debug("edit_review: discarding partial surgical change records failed", exc_info=True)
    else:
        log.warning("edit_review: surgical rollback could not undo a partial batch; keeping %d change "
                    "record(s) so the partial edit stays reviewable", kept)


def _apply_in_undo_context(doc: Any, session: EditReviewSession, run: Callable[[bool], Any]) -> None:
    """Run *run(in_undo_context)* -- which must perform exactly ONE session.record_mutation -- inside a
    fresh grouped-undo context when one can be opened, so a split-author delete+insert stays atomic."""
    undo_title = next_surgical_undo_title()
    undo_mgr = _try_enter_undo_context(doc, undo_title)
    if undo_mgr is None:
        log.debug("edit_review: no usable/unlocked undo manager; split-author whole-block edit falls back "
                  "to the single atomic op (one color)")
        run(False)
        return

    changes_before = len(session.changes)
    applied_ok = False
    try:
        run(True)
        applied_ok = True
    finally:
        close_surgical_context(undo_mgr, session, changes_before, applied_ok, undo_title)


def record_html_atomically(session: EditReviewSession, doc: Any, mutate: Callable[[], Any],
                           **record_kwargs: Any) -> Any:
    """Record an HTML/import mutation that DELETES before it inserts."""
    undo_title = next_agent_edit_undo_title()
    try:
        mgr = _try_enter_undo_context(doc, undo_title)
        if mgr is None:
            raise RuntimeError("undo manager unavailable or locked")
    except Exception:
        # We raise without `from e`, not with it, because implicit chaining already keeps the undo-manager error as __context__.
        raise ToolExecutionError(
            "Cannot apply this content edit atomically (no usable undo context); "
            "refusing rather than risk a half-applied edit.")

    changes_before = len(session.changes)
    applied_ok = False
    try:
        result = session.record_mutation(mutate, **record_kwargs)
        applied_ok = True
        return result
    finally:
        close_surgical_context(mgr, session, changes_before, applied_ok, undo_title)


# --- post-edit echo (edited_context) ---------------------------------------
_EDITED_CONTEXT_MAX_CHARS = 700


def collapsed_anchor(text_range: Any) -> Any | None:
    """A collapsed model cursor at *text_range*'s start."""
    try:
        return text_range.getText().createTextCursorByRange(text_range.getStart())
    except Exception:
        return None


def selection_anchor(doc: Any) -> Any | None:
    """Collapsed anchor at the view cursor's start (the selection insert site)."""
    try:
        vc = doc.getCurrentController().getViewCursor()
        return vc.getText().createTextCursorByRange(vc.getStart())
    except Exception:
        return None


def paragraph_window_text(anchor: Any | None, max_chars: int = _EDITED_CONTEXT_MAX_CHARS) -> str | None:
    """Plain text of the paragraph around *anchor* plus one neighbor each side, read AFTER the edit."""
    if anchor is None:
        return None
    try:
        text = anchor.getText()
        start = text.createTextCursorByRange(anchor.getStart())
        start.gotoStartOfParagraph(False)
        start.gotoPreviousParagraph(False)   # False at the first paragraph -> stays put
        start.gotoStartOfParagraph(False)
        end = text.createTextCursorByRange(anchor.getEnd())
        end.gotoEndOfParagraph(False)
        end.gotoNextParagraph(False)         # False at the last paragraph -> stays put
        end.gotoEndOfParagraph(False)
        span = text.createTextCursorByRange(start.getStart())
        span.gotoRange(end.getEnd(), True)
        s = span.getString()
    except Exception:
        return None
    if not s or not s.strip():
        return None
    if len(s) > max_chars:
        head = int(max_chars * 0.6)
        tail = max_chars - head - 7
        s = s[:head] + " [...] " + s[-tail:]
    return s


def attach_edited_context(result: Any, anchor: Any | None) -> dict[str, Any]:
    """Add edited_context (the touched paragraph(s) as they now read) to a successful result."""
    snippet = paragraph_window_text(anchor)
    if snippet:
        result["edited_context"] = snippet
    return result


def record_preserve_replace(session: EditReviewSession, doc: Any, found: Any, new_text: str,
                            uno_ctx: Any, split: bool) -> None:
    """Record a format-preserving replace as ONE reviewable change, or -- when *split* (review
    recording is on) and only PART of the block changed -- as several SURGICAL sub-changes,
    each its own tracked Delete+Insert with its own accept/reject outcome.
    """
    from . import format as format_support

    split_author = split and _SPLIT_AUTHOR_COLORS

    def _whole() -> None:
        original = found.getString()

        def _run(in_undo_context: bool) -> None:
            session.record_mutation(
                lambda: format_support.replace_preserving_format(
                    doc, found, new_text, uno_ctx,
                    in_undo_context=in_undo_context, split_author=split_author),
                original_preview=_preview(original), proposed_preview=_preview(new_text))

        if split_author:
            _apply_in_undo_context(doc, session, _run)
        else:
            _run(False)

    if not split:
        _whole()
        return

    from plugin.writer.word_diff_split import split_change

    result = split_change(found.getString(), new_text, _WORD_DIFF_THRESHOLD)
    if not result.is_surgical:
        _whole()
        return
    if not result.sub_edits:
        return
    if len(result.sub_edits) > _MAX_SURGICAL_RUNS:
        _whole()
        return
    if not _block_safe_for_surgical(found):
        _whole()
        return

    text = found.getText()
    anchor = found.getStart()

    def _select(se: Any) -> Any | None:
        sub = text.createTextCursorByRange(anchor)
        if se.old_start and not _go_right(sub, se.old_start, False):
            return None
        if se.old_end > se.old_start and not _go_right(sub, se.old_end - se.old_start, True):
            return None
        return sub

    for se in result.sub_edits:
        sub = _select(se)
        if sub is None or sub.getString() != se.old_text:
            log.debug("edit_review: surgical pre-flight offset mismatch; falling back to whole-block")
            _whole()
            return

    undo_title = next_surgical_undo_title()
    undo_mgr = _try_enter_undo_context(doc, undo_title)
    if undo_mgr is None:
        log.debug("edit_review: no usable/unlocked undo manager; surgical edit falls back to whole-block "
                  "for atomicity")
        _whole()
        return

    changes_before = len(session.changes)
    applied_ok = False
    try:
        for se in sorted(result.sub_edits, key=lambda e: e.old_start, reverse=True):
            def apply_se(se: Any = se) -> None:
                sub = _select(se)
                if sub is None or sub.getString() != se.old_text:
                    raise RuntimeError(
                        "surgical sub-edit offset drifted at apply time; aborting to avoid corruption")
                format_support.replace_preserving_format(doc, sub, se.new_text, uno_ctx,
                                                         in_undo_context=True,
                                                         split_author=split_author)

            session.record_mutation(
                apply_se,
                original_preview=_preview(se.old_text),
                proposed_preview=_preview(se.new_text))
        applied_ok = True
    finally:
        close_surgical_context(undo_mgr, session, changes_before, applied_ok, undo_title)


def build_writer_rewrite_prompt(original_text: str, instructions: str) -> str:
    """Return a direct rewrite prompt for Writer selection edits."""
    return f"Rewrite the following text according to the instructions below. Output only the rewritten text with no labels, headings, or explanations.\n\nInstructions: {instructions}\n\nText to rewrite:\n{original_text}"


_BLOCKING_REDLINE_TYPES = frozenset({"Insert", "Delete"})
_WALK_FAILED = object()

# Shown by the Extend/Edit Selection message box. editselection.py wraps the same
# words in _() so xgettext can extract them; keep the two copies identical.
TRACKED_SELECTION_MESSAGE = "This selection contains tracked insertions or deletions. Accept or reject those changes first. The selection was not modified."


class TrackedChangesInSelection(Exception):
    """The selection contains a tracked Insert or Delete, so a streamed edit must not write it.

    ``setString`` with ``RecordChanges`` off accepts deletions and flattens insertions.
    """

    def __init__(self) -> None:
        super().__init__(TRACKED_SELECTION_MESSAGE)


def _portion_type(node: Any) -> str | None:
    try:
        portion_type = node.getPropertyValue("TextPortionType")
    except Exception:
        try:
            portion_type = node.TextPortionType
        except Exception:
            return None
    if portion_type is None:
        return None
    return str(portion_type)


def _open_enum(owner: Any) -> Any:
    """Return an enumeration, ``None`` when the object has none, or ``_WALK_FAILED``.

    Missing ``createEnumeration`` is a plain test double (no redlines to protect).
    A method that exists and throws is a walk we could not finish.
    """
    create = getattr(owner, "createEnumeration", None)
    if not callable(create):
        return None
    try:
        return create()
    except Exception:
        return _WALK_FAILED


def _is_blocking_redline(node: Any) -> bool:
    """True for a tracked Insert or Delete, or a redline whose type cannot be read.

    Format (and other) redlines are left alone: this guard exists so ``setString``
    cannot accept a deletion or flatten an insertion. An unreadable type on a
    confirmed redline portion fails closed, because that write would resolve it.
    """
    if _portion_type(node) != "Redline":
        return False
    try:
        kind = str(node.getPropertyValue("RedlineType"))
    except Exception:
        return True
    return kind in _BLOCKING_REDLINE_TYPES


def range_has_tracked_insert_or_delete(text_range: Any) -> bool:
    """True when *text_range* contains a tracked Insert or Delete.

    A text cursor enumerates paragraphs; a paragraph enumerates portions. Portions
    are leaves — calling ``createEnumeration`` on them is not part of this walk.
    ``hasMoreElements() is True`` so a MagicMock enum cannot spin. When the walk
    starts and then fails, the result is True: ``setString`` must not run on a
    range we could not prove is free of Insert/Delete redlines.
    """
    top = _open_enum(text_range)
    if top is None:
        return False
    if top is _WALK_FAILED:
        log.warning(
            "range_has_tracked_insert_or_delete: could not enumerate the selection; "
            "refusing so setString cannot accept an unseen redline")
        return True
    try:
        while top.hasMoreElements() is True:
            node = top.nextElement()
            if _is_blocking_redline(node):
                return True
            # A portion (text, redline mark, …) does not contain nested redlines.
            # Only descend into paragraphs / other containers.
            if _portion_type(node) is not None:
                continue
            sub = _open_enum(node)
            if sub is _WALK_FAILED:
                log.warning(
                    "range_has_tracked_insert_or_delete: could not enumerate a selection "
                    "child; refusing so setString cannot accept an unseen redline")
                return True
            if sub is None:
                continue
            while sub.hasMoreElements() is True:
                if _is_blocking_redline(sub.nextElement()):
                    return True
    except Exception:
        log.warning(
            "range_has_tracked_insert_or_delete: portion walk failed; refusing so "
            "setString cannot accept a redline the walk did not finish",
            exc_info=True)
        return True
    return False


def refuse_tracked_insert_or_delete(text_range: Any) -> None:
    """Raise before any streamed-edit write when the range has an Insert or Delete.

    Extend/Edit Selection reads the selection with
    ``get_string_without_tracked_deletions`` (Delete text dropped,
    Insert text kept as ordinary characters).
    ``WriterStreamedRewriteSession`` then turns ``RecordChanges`` off
    and calls ``setString("")``. ``WriterStreamedAppendSession``
    streams with ``setString(original + continuation)`` and ``finish``
    calls ``setString(original_text)``. With tracking off, ``setString``
    accepts every redline in the range — struck text disappears and
    insertions flatten — before the model returns anything.

    Both session constructors call this before the undo context,
    before ``RecordChanges`` is changed, and before the first
    ``setString``, so the redlines stay untouched even if the menu
    wrapper is skipped. A range with no Insert/Delete keeps the
    rewrite path. An empty model result restores the original text
    and does not record a deletion.
    """
    if range_has_tracked_insert_or_delete(text_range):
        log.warning(
            "streamed selection edit refused: selection contains a tracked Insert or "
            "Delete; RecordChanges was not changed and setString was not called")
        raise TrackedChangesInSelection()


class WriterCompoundUndo:
    """Wrap ``XUndoManager.enterUndoContext`` / ``leaveUndoContext`` for one Ctrl+Z step.

    Prefer ``with WriterCompoundUndo(doc, title):``. Explicit :meth:`close` remains
    for streamed sessions that open in ``__init__`` and finish later. Safe to call
    ``close`` multiple times.

    Known limitation: the streamed sessions keep this undo context open for the whole
    stream, so a keystroke or other edit the user makes in the document while the agent
    is streaming lands in the agent's undo step (one Ctrl+Z reverts both). We deliberately
    do NOT block document input while streaming; closing the context per batch instead
    would split the agent edit into many undo steps.
    """

    _title: str
    _undo_manager: Any | None
    _open: bool

    def __init__(self, doc: Any, title: str) -> None:
        self._title = title
        self._undo_manager = None
        self._open = False
        try:
            if not hasattr(doc, "getUndoManager"):
                log.warning("WriterCompoundUndo: doc has no getUndoManager, undo grouping skipped (title=%r)", title)
                return
            um = doc.getUndoManager()
            if um is None:
                log.warning("WriterCompoundUndo: getUndoManager() returned None, undo grouping skipped (title=%r)", title)
                return
            # Probe undo manager state to detect prior unclosed contexts (best-effort; UNO may not expose these).
            try:
                is_in_ctx = um.isInContext()
                undo_enabled = um.isUndoEnabled()
                log.info("WriterCompoundUndo: pre-enter state isInContext=%s isUndoEnabled=%s (title=%r)", is_in_ctx, undo_enabled, title)
            except Exception as probe_e:
                log.debug("WriterCompoundUndo: could not probe undo manager state: %s", probe_e)
            um.enterUndoContext(title)
            self._undo_manager = um
            self._open = True
            # Log after success so we always see this when the context is live.
            log.info("WriterCompoundUndo: context entered %r", title)
        except Exception as e:
            # Upgrade from debug to warning so failures are visible without debug logging.
            # "Insert $1" in the undo menu means this context was never opened.
            log.warning("WriterCompoundUndo: enterUndoContext failed, undo grouping disabled (title=%r): %s", title, e)

    def __enter__(self) -> "WriterCompoundUndo":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        # Do not swallow exceptions; only close the undo group.
        self.close()

    def close(self) -> None:
        """End the compound undo context if :meth:`__init__` opened one."""
        if not self._open:
            log.debug("WriterCompoundUndo.close: already closed or never opened (title=%r)", self._title)
            return
        self._open = False
        um = self._undo_manager
        self._undo_manager = None
        if um is None:
            return
        try:
            log.info("WriterCompoundUndo: leaving context %r", self._title)
            um.leaveUndoContext()
        except Exception:
            log.exception("leaveUndoContext failed (title=%r)", self._title)


@contextlib.contextmanager
def _collapse_review_context(doc: Any, track_reviewable: bool, was_recording: bool, label: str) -> Iterator[None]:
    before_ids = None
    before_ids_ok = False
    prior_author = None
    if track_reviewable:
        try:
            from plugin.framework.uno_context import get_ctx
            from plugin.writer import review_authors
            from plugin.writer.review_scan import snapshot_redline_ids

            before_ids, before_ids_ok = snapshot_redline_ids(doc)
            prior_author = review_authors.begin(get_ctx())
            # Make the markup visible so a reviewable change isn't left invisible
            # when the user has Track Changes display off (matches
            # EditReviewSession.__enter__). Review mode only -- never when the user
            # merely has their own Track Changes on (we respect their view setting).
            try:
                doc.setPropertyValue("ShowChanges", True)
            except Exception:
                log.debug("%s: could not force ShowChanges", label, exc_info=True)
        except Exception:
            log.debug("%s: review tagging setup failed", label, exc_info=True)
    try:
        yield
    finally:
        if prior_author is not None:
            try:
                from plugin.framework.uno_context import get_ctx
                from plugin.writer import review_authors

                review_authors.end(get_ctx(), prior_author)
            except Exception:
                log.warning("%s: author restore failed", label, exc_info=True)
        # Restore the user's prior recording state. If they had Track Changes
        # OFF and we only turned it ON to capture this edit as one reviewable
        # redline (track_reviewable flag), turn it back OFF so their later
        # manual typing is not tracked. Existing redlines persist regardless.
        if not was_recording:
            try:
                doc.setPropertyValue("RecordChanges", False)
            except Exception:
                pass
        # Tag the collapsed redline(s) with a session token so the inline review UI
        # (click popup / context menu) treats this streamed edit as an agent change.
        if before_ids is not None:
            try:
                tag_agent_redlines(doc, before_ids, before_reliable=before_ids_ok)
            except Exception:
                log.debug("%s: redline tagging failed", label, exc_info=True)


class WriterStreamedRewriteSession:
    """Manage a streamed Writer edit that collapses to one tracked change.

    Refuses with :class:`TrackedChangesInSelection` before any write when the range
    already contains a tracked Insert or Delete. ``finish()`` with an empty
    ``generated_text`` puts ``original_text`` back and records nothing. Characters
    the model actually sent, including whitespace, still collapse as one tracked
    change when recording is on. The undo context spans the whole stream, so user
    edits made while streaming join the agent's undo step (known limitation, see
    :class:`WriterCompoundUndo`).
    """

    _UNDO_CONTEXT_TITLE: ClassVar[str] = "WriterAgent: Edit selection"

    doc: Any
    text_range: Any
    original_text: str
    generated_text: str
    was_recording: bool
    track_reviewable: bool
    _compound_undo: WriterCompoundUndo

    def __init__(self, doc: Any, text_range: Any, original_text: str, track_reviewable: bool = False) -> None:
        self.doc = doc
        self.text_range = text_range
        self.original_text = original_text
        self.generated_text = ""
        self.was_recording = False
        # When True (opt-in flag), the agent's edit is collapsed into one tracked
        # change for the user to review even if they did not have Track Changes on.
        self.track_reviewable = track_reviewable
        self.last_write_error: Exception | None = None
        # Before the undo context, RecordChanges, and setString(""). See
        # refuse_tracked_insert_or_delete: setString with tracking off accepts
        # deletions and flattens insertions in this range.
        refuse_tracked_insert_or_delete(text_range)
        self._compound_undo = WriterCompoundUndo(doc, self._UNDO_CONTEXT_TITLE)

        try:
            self.was_recording = bool(self.doc.getPropertyValue("RecordChanges"))
        except Exception:
            self.was_recording = False

        log.info("WriterStreamedRewriteSession: was_recording=%s, compound_undo open=%s", self.was_recording, self._compound_undo._open)
        try:
            if self.was_recording:
                self.doc.setPropertyValue("RecordChanges", False)
            self.text_range.setString("")
        except Exception:
            if self.was_recording:
                try:
                    self.doc.setPropertyValue("RecordChanges", True)
                except Exception:
                    pass
            self._compound_undo.close()
            raise

    def append_chunk(self, chunk: str) -> None:
        """Append streamed text to the visible range and shadow buffer."""
        if not chunk:
            return
        self.generated_text += chunk
        try:
            self.text_range.setString(self.generated_text)
        except Exception as exc:
            self.last_write_error = exc
            log.warning("streamed rewrite: chunk apply failed", exc_info=True)

    def finish(self) -> str | None:
        """Finalize the rewrite. Returns a warning message on degraded success."""
        try:
            # __init__ clears the range (RecordChanges already off) before any
            # token, so each chunk can paint the whole replacement. Thinking-only
            # output is dropped by do_edit_selection and never appended, so
            # generated_text stays "". The old early return left that clear in
            # place when tracking was off. When tracking was on, the collapse
            # below did setString(original) then setString(""), which records a
            # tracked deletion of the whole selection. Put the original back
            # while tracking is still off, restore the user's RecordChanges, and
            # do not record an empty replacement. Whitespace the model sent is
            # truthy, so it stays on the collapse path.
            if not self.generated_text:
                self.abort_and_restore()
                if self.last_write_error is not None:
                    return _("Failed to write streamed text to the document: {0}").format(self.last_write_error)
                return None

            if not (self.was_recording or self.track_reviewable):
                if self.last_write_error is not None:
                    return _("Failed to write streamed text to the document: {0}").format(self.last_write_error)
                return None

            try:
                with _collapse_review_context(self.doc, self.track_reviewable, self.was_recording, "streamed rewrite"):
                    self.text_range.setString(self.original_text)
                    self.doc.setPropertyValue("RecordChanges", True)
                    self.text_range.setString(self.generated_text)
                return None
            except Exception:
                log.exception("Failed to collapse streamed edit into one tracked change")

                fallback_errors: list[str] = []
                try:
                    self.doc.setPropertyValue("RecordChanges", False)
                except Exception as e:
                    fallback_errors.append(f"disable tracking failed: {e}")
                try:
                    self.text_range.setString(self.generated_text)
                except Exception as e:
                    fallback_errors.append(f"restore generated text failed: {e}")
                try:
                    self.doc.setPropertyValue("RecordChanges", self.was_recording)
                except Exception as e:
                    fallback_errors.append(f"restore recording state failed: {e}")

                if fallback_errors:
                    return _("Failed to finalize the tracked edit and preserve the generated text: {0}").format("; ".join(fallback_errors))
                return _("Failed to collapse the streamed edit into a single tracked change. The generated text was kept, but it may still appear as multiple tracked changes.")
        finally:
            self._compound_undo.close()

    def abort_and_restore(self) -> None:
        """Restore the original text and recording state after an error."""
        try:
            # We close the compound undo first, so we can undo the whole streamed operation
            was_open = self._compound_undo._open
            self._compound_undo.close()
            restored_via_undo = False
            if was_open:
                try:
                    um = self.doc.getUndoManager() if hasattr(self.doc, "getUndoManager") else None
                    if um is not None:
                        titles = um.getAllUndoActionTitles() if hasattr(um, "getAllUndoActionTitles") else ()
                        if titles and titles[0] == self._UNDO_CONTEXT_TITLE:
                            um.undo()
                            restored_via_undo = True
                except Exception:
                    log.warning("streamed rewrite: undo failed during abort_and_restore", exc_info=True)
            if not restored_via_undo:
                log.warning("streamed rewrite: compound undo never opened or undo failed; restoring original text via setString")
                try:
                    self.text_range.setString(self.original_text)
                except Exception:
                    log.exception("streamed rewrite: fallback setString(original_text) failed")
        finally:
            if self.was_recording:
                try:
                    self.doc.setPropertyValue("RecordChanges", True)
                except Exception:
                    pass


class WriterStreamedAppendSession:
    """Manage a streamed Writer APPEND (extend-selection) that collapses to one tracked insertion.

    Unlike :class:`WriterStreamedRewriteSession` (which REPLACES the range), extend-selection
    keeps the user's original text and streams the agent's continuation AFTER it. Streaming runs
    with tracking OFF (the user sees the text appear without a redline per chunk); ``finish()``
    then converts ONLY the appended continuation into a single tracked INSERTION -- the original
    is never struck through -- authored as the agent and tagged for the inline review UI.
    The undo context spans the whole stream, so user edits made while streaming join the
    agent's undo step (known limitation, see :class:`WriterCompoundUndo`).

    Refuses with :class:`TrackedChangesInSelection` before ``RecordChanges`` is changed and
    before the first ``setString`` when the range already contains a tracked Insert or Delete.
    ``original_text`` is the accept-deletions view, so writing it back would accept those
    redlines.
    """

    _UNDO_CONTEXT_TITLE: ClassVar[str] = "WriterAgent: Extend selection"

    doc: Any
    text_range: Any
    original_text: str
    appended_text: str
    track_reviewable: bool
    _compound_undo: WriterCompoundUndo
    was_recording: bool

    def __init__(self, doc: Any, text_range: Any, original_text: str, track_reviewable: bool = False) -> None:
        self.doc = doc
        self.text_range = text_range
        self.original_text = original_text
        self.appended_text = ""
        self.track_reviewable = track_reviewable
        self.was_recording = False
        self.last_write_error: Exception | None = None
        # Before the undo context and before RecordChanges is turned off. append_chunk's
        # setString(original_text + continuation) and finish()'s setString(original_text)
        # would accept deletions and flatten insertions. See refuse_tracked_insert_or_delete.
        refuse_tracked_insert_or_delete(text_range)
        self._compound_undo = WriterCompoundUndo(doc, self._UNDO_CONTEXT_TITLE)

        try:
            self.was_recording = bool(self.doc.getPropertyValue("RecordChanges"))
        except Exception:
            self.was_recording = False
        # Stream with tracking OFF so the live continuation isn't recorded as a redline per
        # chunk; finish() re-records the whole appended run as one tracked insertion.
        try:
            if self.was_recording:
                self.doc.setPropertyValue("RecordChanges", False)
        except Exception:
            pass

    def append_chunk(self, chunk: str) -> None:
        """Append streamed text after the original (tracking off; one redline created at finish)."""
        if not chunk:
            return
        self.appended_text += chunk
        try:
            self.text_range.setString(self.original_text + self.appended_text)
        except Exception as exc:
            self.last_write_error = exc
            log.warning("streamed append: chunk apply failed", exc_info=True)

    def finish(self) -> str | None:
        """Collapse the appended continuation into one tracked insertion. Returns a warning on degraded success."""
        try:
            if not self.appended_text:
                # We may have turned off the user's own Record Changes in __init__ to avoid a
                # redline per streamed chunk. If the model produced nothing, there is no edit to
                # collapse, but the user's prior tracking state still must be restored.
                if self.was_recording:
                    try:
                        self.doc.setPropertyValue("RecordChanges", True)
                    except Exception:
                        pass
                if self.last_write_error is not None:
                    return _("Failed to write streamed text to the document: {0}").format(self.last_write_error)
                return None
            if not (self.was_recording or self.track_reviewable):
                if self.last_write_error is not None:
                    return _("Failed to write streamed text to the document: {0}").format(self.last_write_error)
                return None

            try:
                with _collapse_review_context(self.doc, self.track_reviewable, self.was_recording, "streamed append"):
                    # Drop the untracked appended run (back to just the original), then re-insert ONLY
                    # that run as a tracked insertion at the end -- so the original carries no redline.
                    self.text_range.setString(self.original_text)
                    self.doc.setPropertyValue("RecordChanges", True)
                    text = self.text_range.getText()
                    end_cursor = text.createTextCursorByRange(self.text_range.getEnd())
                    text.insertString(end_cursor, self.appended_text, False)
                return None
            except Exception:
                log.exception("Failed to collapse streamed append into one tracked change")
                # Degrade: keep the user's continuation (untracked) rather than losing it.
                try:
                    self.doc.setPropertyValue("RecordChanges", False)
                except Exception:
                    pass
                try:
                    self.text_range.setString(self.original_text + self.appended_text)
                except Exception:
                    pass
                try:
                    self.doc.setPropertyValue("RecordChanges", self.was_recording)
                except Exception:
                    pass
                return _("Failed to collapse the streamed continuation into a single tracked change. The text was kept, but may not be reviewable.")
        finally:
            self._compound_undo.close()

    def abort_and_restore(self) -> None:
        """After a streaming error, restore the recording state and close the undo group.

        The partial continuation is left in place, matching the prior extend-selection behavior."""
        try:
            self.doc.setPropertyValue("RecordChanges", bool(self.was_recording))
        except Exception:
            pass
        finally:
            self._compound_undo.close()

