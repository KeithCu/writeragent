# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for agent edit review config helpers (no UNO)."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.edit_review import (
    edit_review_wait_seconds,
    get_agent_edit_review_mode,
    review_recording_enabled,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("off", "off"),
        ("record", "record"),
        ("wait", "wait"),
        ("RECORD", "record"),
        (" Wait ", "wait"),
        ("bogus", "off"),
        ("", "off"),
        (None, "off"),
    ],
)
def test_get_agent_edit_review_mode(raw, expected):
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config", return_value=raw):
        assert get_agent_edit_review_mode(ctx) == expected


@pytest.mark.parametrize(
    "mode,recording",
    [
        ("off", False),
        ("record", True),
        ("wait", True),
    ],
)
def test_review_recording_enabled(mode, recording):
    ctx = MagicMock()
    with patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value=mode):
        assert review_recording_enabled(ctx) is recording


@pytest.mark.parametrize(
    "mode,timeout,expected",
    [
        ("off", 900, 0),
        ("record", 900, 0),
        ("wait", 900, 900),
        ("wait", 0, 0),
        ("wait", -5, 0),
    ],
)
def test_edit_review_wait_seconds(mode, timeout, expected):
    ctx = MagicMock()
    with patch("plugin.writer.edit_review.get_agent_edit_review_mode", return_value=mode), \
         patch("plugin.framework.config.get_config_int_safe", return_value=timeout):
        assert edit_review_wait_seconds(ctx) == expected


# --------------------------------- snapshot_redline_ids reliability + record_mutation fail-closed

_RAISE = object()  # marker: a redline whose RedlineIdentifier read should raise


class _FakeRl:
    def __init__(self, rid, raise_set=False, comment="", raise_comment=False, raise_revert=False):
        self._rid = rid
        self._raise_set = raise_set
        self._raise_revert = raise_revert  # tagging (set token) succeeds, but reverting (set "") fails
        self._raise_comment = raise_comment
        self.comment = comment  # current RedlineComment: getPropertyValue returns it, setPropertyValue updates it

    def getPropertyValue(self, name):
        if name == "RedlineComment":
            if self._raise_comment:
                raise RuntimeError("comment unreadable")
            return self.comment
        assert name == "RedlineIdentifier"
        if self._rid is _RAISE:
            raise RuntimeError("identifier unreadable")
        return self._rid

    def setPropertyValue(self, name, val):
        assert name == "RedlineComment"
        if self._raise_set:
            raise RuntimeError("set boom")
        if self._raise_revert and val == "":
            raise RuntimeError("revert boom")  # cannot clear the tag -> orphan
        self.comment = val


class _FakeEnum:
    def __init__(self, items, raise_hasmore=False):
        self._items = list(items)
        self._raise_hasmore = raise_hasmore

    def hasMoreElements(self):
        if self._raise_hasmore:
            raise RuntimeError("hasMore boom")
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class _FakeRedlines:
    def __init__(self, items, count=None, raise_enum=False, raise_count=False, raise_hasmore=False):
        # items may be raw ids (wrapped in _FakeRl) or already-built _FakeRl objects
        self._items = [r if isinstance(r, _FakeRl) else _FakeRl(r) for r in items]
        self._count = count
        self._raise_enum = raise_enum
        self._raise_count = raise_count
        self._raise_hasmore = raise_hasmore

    def getCount(self):
        if self._raise_count:
            raise RuntimeError("count boom")
        return len(self._items) if self._count is None else self._count

    def createEnumeration(self):
        if self._raise_enum:
            raise RuntimeError("enum boom")
        return _FakeEnum(self._items, raise_hasmore=self._raise_hasmore)


def _redlines_doc(items, count=None, raise_enum=False, raise_count=False, raise_hasmore=False):
    doc = MagicMock()
    doc.getRedlines.return_value = _FakeRedlines(items, count, raise_enum, raise_count, raise_hasmore)
    return doc


def test_snapshot_redline_ids_reliable_when_complete():
    from plugin.writer.edit_review import snapshot_redline_ids
    ids, ok = snapshot_redline_ids(_redlines_doc(["a", "b"]))
    assert ok is True and ids == {"a", "b"}


def test_snapshot_redline_ids_unreliable_on_silent_truncation():
    # 1 redline enumerated but getCount() reports 2 -> a pre-existing redline may be unseen -> a later
    # edit's new-redline diff could misclassify it -> unreliable.
    from plugin.writer.edit_review import snapshot_redline_ids
    _, ok = snapshot_redline_ids(_redlines_doc(["a"], count=2))
    assert ok is False


def test_snapshot_redline_ids_unreliable_on_unreadable_identifier():
    from plugin.writer.edit_review import snapshot_redline_ids
    _, ok = snapshot_redline_ids(_redlines_doc(["a", _RAISE]))
    assert ok is False


def test_snapshot_redline_ids_unreliable_on_enum_error():
    from plugin.writer.edit_review import snapshot_redline_ids
    _, ok = snapshot_redline_ids(_redlines_doc(["a"], raise_enum=True))
    assert ok is False


def test_snapshot_redline_ids_unreliable_on_count_error():
    from plugin.writer.edit_review import snapshot_redline_ids
    _, ok = snapshot_redline_ids(_redlines_doc(["a"], raise_count=True))
    assert ok is False


def test_snapshot_redline_ids_unreliable_on_hasmore_error():
    # hasMoreElements() throwing must honor the (ids, False) contract, not propagate.
    from plugin.writer.edit_review import snapshot_redline_ids
    _, ok = snapshot_redline_ids(_redlines_doc(["a"], raise_hasmore=True))
    assert ok is False


# ----------------------------------------------- _new_redlines_complete + _tag_new_redlines

def test_new_redlines_complete_finds_new_when_complete():
    from plugin.writer.edit_review import _new_redlines_complete
    new, ok = _new_redlines_complete(_redlines_doc(["old", "new1", "new2"]), {"old"})
    assert ok is True and len(new) == 2


def test_new_redlines_complete_unreliable_on_silent_truncation():
    from plugin.writer.edit_review import _new_redlines_complete
    _, ok = _new_redlines_complete(_redlines_doc(["old", "new1"], count=3), {"old"})
    assert ok is False


def test_new_redlines_complete_unreliable_on_unreadable_id():
    from plugin.writer.edit_review import _new_redlines_complete
    _, ok = _new_redlines_complete(_redlines_doc(["old", _RAISE]), {"old"})
    assert ok is False


def test_new_redlines_complete_unreliable_on_hasmore_error():
    from plugin.writer.edit_review import _new_redlines_complete
    _, ok = _new_redlines_complete(_redlines_doc(["old"], raise_hasmore=True), {"old"})
    assert ok is False


def test_tag_new_redlines_success_tags_all():
    from plugin.writer.edit_review import _tag_new_redlines
    rls = [_FakeRl("a"), _FakeRl("b")]
    assert _tag_new_redlines(rls, "TOKEN") == (True, 0)   # success, no orphans
    assert all(r.comment == "TOKEN" for r in rls)


def test_tag_new_redlines_all_or_nothing_reverts_on_failure():
    # Tagging the second redline fails -> the first tag is REVERTED so the change is never half-tagged.
    # A CLEAN revert is (False, 0) -- failure, no orphan.
    from plugin.writer.edit_review import _tag_new_redlines
    rl1, rl2 = _FakeRl("a"), _FakeRl("b", raise_set=True)
    assert _tag_new_redlines([rl1, rl2], "TOKEN") == (False, 0)
    assert rl1.comment == ""      # reverted (back to a fresh redline's empty comment)
    assert rl2.comment == ""      # never tagged (set always raised)


def test_tag_new_redlines_reports_orphan_count_when_revert_fails():
    # rl1 tags OK but its revert fails; rl2's tag fails (triggering the revert). The revert of rl1
    # cannot remove the token -> (False, 1): failure with one orphan. success and the count are
    # SEPARATE so the caller can never read it as success.
    from plugin.writer.edit_review import _tag_new_redlines
    rl1, rl2 = _FakeRl("a", raise_revert=True), _FakeRl("b", raise_set=True)
    assert _tag_new_redlines([rl1, rl2], "TOKEN") == (False, 1)
    assert rl1.comment == "TOKEN"   # still tagged -- the orphan
    assert rl2.comment == ""        # never tagged (set always raised)


def test_tag_new_redlines_single_redline_orphan_is_failure_not_success():
    # KEY case: ONE redline that mutates-then-throws and can't be reverted. orphans == len
    # == 1, which an ambiguous int return would make look like full success. The (bool, int) return
    # keeps it a FAILURE: (False, 1).
    from plugin.writer.edit_review import _tag_new_redlines

    class _MutateThenThrow(_FakeRl):
        def setPropertyValue(self, name, val):
            if val == "":
                raise RuntimeError("revert cannot clear it")  # revert fails WITHOUT clearing
            self.comment = val                                # tag writes the token...
            raise RuntimeError("set wrote then threw")        # ...then throws

    rl = _MutateThenThrow("a")
    assert _tag_new_redlines([rl], "TOKEN") == (False, 1)   # FAILURE, not (True, ...)
    assert rl.comment == "TOKEN"                             # the orphan tag remains


def test_record_mutation_single_orphan_not_registered():
    # A SINGLE new redline that mutates-then-throws (orphan==len==1). record_mutation must
    # NOT register it as a successful change -- it went through the failure path.
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import EditReviewSession

    class _MutateThenThrow(_FakeRl):
        def setPropertyValue(self, name, val):
            if val == "":
                raise RuntimeError("revert cannot clear it")
            self.comment = val
            raise RuntimeError("set wrote then threw")

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    rl = _MutateThenThrow("a")
    with _patch.object(session, "_redline_idents", return_value=(set(), True)), \
         _patch("plugin.writer.edit_review._new_redlines_complete", return_value=([rl], True)):
        result = session.record_mutation(lambda: "r")

    assert result == "r"
    assert session.changes == []   # NOT registered -- the single-orphan failure isn't success


def test_record_mutation_does_not_register_when_revert_leaves_orphan():
    # A dirty revert (orphan remains) must NOT register the change (no half-tagged reviewable change).
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    rl1, rl2 = _FakeRl("a", raise_revert=True), _FakeRl("b", raise_set=True)
    with _patch.object(session, "_redline_idents", return_value=(set(), True)), \
         _patch("plugin.writer.edit_review._new_redlines_complete", return_value=([rl1, rl2], True)):
        result = session.record_mutation(lambda: "r")

    assert result == "r"
    assert session.changes == []   # not registered despite the orphan


# ---------------------------------------- _review_payload / _outcome honor pending reliability

def test_review_payload_reports_pending_on_unreliable_snapshot():
    # An unreliable pending scan must report a non-listed change as "pending", never a guessed outcome
    # from the anchor text.
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import ChangeRecord, EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    session.changes = [ChangeRecord("t", "bm", "ACCEPTED", "REJECTED", "", "")]
    with _patch.object(session, "_pending_tokens", return_value=(set(), False)), \
         _patch.object(session, "_change_text_at_anchor", return_value="ACCEPTED"):
        payload = session._review_payload(complete=False, timed_out=True)
    assert payload["changes"][0]["outcome"] == "pending"   # NOT "accepted" -- snapshot unreliable


def test_review_payload_reports_real_outcome_on_reliable_snapshot():
    # Control: a RELIABLE empty snapshot still classifies the change normally (no over-conservatism).
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import ChangeRecord, EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    session.changes = [ChangeRecord("t", "bm", "ACCEPTED", "REJECTED", "", "")]
    with _patch.object(session, "_pending_tokens", return_value=(set(), True)), \
         _patch.object(session, "_change_text_at_anchor", return_value="ACCEPTED"):
        payload = session._review_payload(complete=True, timed_out=False)
    assert payload["changes"][0]["outcome"] == "accepted"


def test_review_payload_header_and_outcomes_stay_consistent_on_unreliable_rescan():
    # Even if the caller passes complete=True (its loop scan was clean), a transient UNRELIABLE re-scan
    # inside the payload must downgrade complete to False so the header matches the all-"pending"
    # outcomes -- never complete=True alongside pending outcomes (consistency).
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import ChangeRecord, EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    session.changes = [ChangeRecord("t", "bm", "ACCEPTED", "REJECTED", "", "")]
    with _patch.object(session, "_pending_tokens", return_value=(set(), False)), \
         _patch.object(session, "_change_text_at_anchor", return_value="ACCEPTED"):
        payload = session._review_payload(complete=True, timed_out=False)
    assert payload["complete"] is False                      # downgraded -- scan unreliable
    assert payload["changes"][0]["outcome"] == "pending"     # header and body agree


def test_record_mutation_does_not_tag_when_after_scan_incomplete():
    # before-snapshot reliable, but the post-edit scan is incomplete -> leave the edit untagged rather
    # than register a half-tagged change.
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    applied = {"n": 0}

    def apply_fn():
        applied["n"] += 1
        return "r"

    with _patch.object(session, "_redline_idents", return_value=({"x"}, True)), \
         _patch("plugin.writer.edit_review._new_redlines_complete", return_value=([], False)):
        result = session.record_mutation(apply_fn)

    assert result == "r" and applied["n"] == 1 and session.changes == []


# ------------------------------------------- _pending_tokens reliability + wait_for_review fail-closed

def test_pending_tokens_reliable_lists_only_session_tokens():
    from plugin.writer.edit_review import EditReviewSession
    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    mine = session._session_token_prefix() + "0"
    session.doc = _redlines_doc([_FakeRl("i1", comment=mine),
                                 _FakeRl("i2", comment="wa-review:other:0"),
                                 _FakeRl("i3", comment="")])
    pending, ok = session._pending_tokens()
    assert ok is True and pending == {mine}   # other-session + empty comments excluded


def test_pending_tokens_unreliable_on_silent_truncation():
    from plugin.writer.edit_review import EditReviewSession
    session = EditReviewSession(_redlines_doc([_FakeRl("i1", comment="")], count=2), MagicMock(),
                                enabled=True)
    session._active = True
    _, ok = session._pending_tokens()
    assert ok is False


def test_pending_tokens_unreliable_on_unreadable_comment():
    from plugin.writer.edit_review import EditReviewSession
    session = EditReviewSession(_redlines_doc([_FakeRl("i1", raise_comment=True)]), MagicMock(),
                                enabled=True)
    session._active = True
    _, ok = session._pending_tokens()
    assert ok is False


def test_wait_for_review_not_complete_on_unreliable_pending():
    # An unreliable pending scan must NOT be read as "done": wait keeps going until the deadline and
    # reports complete=False / timed_out=True, never a false completion.
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import ChangeRecord, EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    session.changes = [ChangeRecord("t", "", "", "", "", "")]
    with _patch.object(session, "_pending_tokens", return_value=(set(), False)), \
         _patch.object(session, "cleanup"):
        result = session.wait_for_review(timeout=0.0, poll=0.01)
    assert result["complete"] is False and result["timed_out"] is True


def test_wait_for_review_completes_on_reliable_empty():
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import ChangeRecord, EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    session.changes = [ChangeRecord("t", "", "", "", "", "")]
    with _patch.object(session, "_pending_tokens", return_value=(set(), True)), \
         _patch.object(session, "cleanup"):
        result = session.wait_for_review(timeout=1.0, poll=0.01)
    assert result["complete"] is True and result["timed_out"] is False


def test_record_mutation_does_not_tag_when_before_snapshot_unreliable():
    # If the pre-edit redline snapshot is unreliable, the edit still APPLIES but is NOT tagged or
    # registered -- so a pre-existing user redline can never be mis-stamped as an agent change and
    # later resolved by Accept/Reject All.
    from unittest.mock import patch as _patch

    from plugin.writer.edit_review import EditReviewSession

    session = EditReviewSession(MagicMock(), MagicMock(), enabled=True)
    session._active = True
    applied = {"n": 0}

    def apply_fn():
        applied["n"] += 1
        return "result"

    with _patch.object(session, "_redline_idents", return_value=(set(), False)):
        result = session.record_mutation(apply_fn)

    assert result == "result"      # the edit ran
    assert applied["n"] == 1
    assert session.changes == []   # but nothing was registered/tagged


# ----------------------------------------------- discard_changes_since (partial-batch rollback)

def _bookmarks(names, removed):
    """A fake getBookmarks() collection recording every removeTextContent into *removed*."""
    class FakeBookmark:
        def __init__(self, name):
            self.name = name

        def getAnchor(self):
            text = MagicMock()
            text.removeTextContent.side_effect = lambda bm: removed.append(self.name)
            anchor = MagicMock()
            anchor.getText.return_value = text
            return anchor

    class FakeBookmarks:
        def __init__(self):
            self._by = {n: FakeBookmark(n) for n in names}

        def hasByName(self, n):
            return n in self._by

        def getByName(self, n):
            return self._by[n]

    return FakeBookmarks()


def _session_with_changes(doc, tokens_bookmarks):
    from plugin.writer.edit_review import ChangeRecord, EditReviewSession

    session = EditReviewSession(doc, MagicMock(), enabled=True)
    session._active = True
    session.changes = [ChangeRecord(t, b, "", "", "", "") for t, b in tokens_bookmarks]
    return session


def test_discard_changes_since_drops_records_and_removes_bookmarks():
    # A failed surgical batch: trim every change recorded since the pre-batch count, and remove the
    # now-orphaned anchor bookmarks so the document keeps no bookkeeping for edits that were undone.
    removed = []
    doc = MagicMock()
    doc.getBookmarks.return_value = _bookmarks(["bm0", "bm1", "bm2"], removed)
    session = _session_with_changes(doc, [("t0", "bm0"), ("t1", "bm1"), ("t2", "bm2")])

    session.discard_changes_since(1)

    assert [c.token for c in session.changes] == ["t0"]  # records after index 1 dropped
    assert removed == ["bm1", "bm2"]                      # their bookmarks removed, t0's kept


def test_discard_changes_since_noop_when_count_at_or_beyond_length():
    doc = MagicMock()
    session = _session_with_changes(doc, [("t0", "bm0")])

    session.discard_changes_since(1)   # nothing recorded after index 1
    session.discard_changes_since(9)   # out of range
    session.discard_changes_since(-1)  # guarded against negative slice

    assert [c.token for c in session.changes] == ["t0"]
    doc.getBookmarks.assert_not_called()  # never touched the document for a no-op


def test_discard_changes_since_inactive_session_only_trims_list():
    # If recording degraded (never went active), there are no bookmarks in the doc to remove; still
    # trim the in-memory list and do NOT touch the document.
    doc = MagicMock()
    session = _session_with_changes(doc, [("t0", "bm0"), ("t1", "bm1")])
    session._active = False

    session.discard_changes_since(0)

    assert session.changes == []
    doc.getBookmarks.assert_not_called()
