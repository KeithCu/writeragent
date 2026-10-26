# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for review_scan token + fail-closed enumeration (no UNO)."""

from plugin.writer.review_scan import (
    TOKEN_PREFIX,
    document_has_tracked_deletions,
    is_agent_token,
    read_redline_comment,
    make_agent_token,
    redline_is_agent_change,
    scan_redlines,
    session_token_prefix,
    snapshot_redline_ids,
)


def test_make_agent_token_and_prefix():
    assert make_agent_token("abc", 2) == "wa-review:abc:2"
    assert session_token_prefix("abc") == "wa-review:abc:"
    assert session_token_prefix("abc").startswith(TOKEN_PREFIX)


def test_is_agent_token():
    assert is_agent_token("wa-review:s:0") is True
    assert is_agent_token("wa-review: see notes") is False
    assert is_agent_token("wa-review:s:0 trailing") is False
    assert is_agent_token("wa-review:s") is False
    assert is_agent_token("wa-review:s:x") is False
    assert is_agent_token("wa-review:s:1:2") is False
    assert is_agent_token("user note") is False
    assert is_agent_token("") is False
    assert is_agent_token(None) is False


def test_is_agent_token_accepts_odf_reload_trailing_newline():
    # LibreOffice reads <text:p>token</text:p> back with a trailing newline on reopen.
    assert is_agent_token("wa-review:s:0\n") is True
    assert is_agent_token("wa-review:s:0\r\n") is True
    assert is_agent_token("\n") is False
    assert is_agent_token("wa-review:s:0 \n") is False


def test_read_redline_comment_strips_odf_reload_newline():
    from types import SimpleNamespace

    rl = SimpleNamespace(getPropertyValue=lambda name: "wa-review:s:3\n")
    assert read_redline_comment(rl) == ("wa-review:s:3", True)


def test_redline_is_agent_change_unreadable():
    class _Bad:
        def getPropertyValue(self, name):
            raise RuntimeError("gone")

    assert redline_is_agent_change(_Bad()) == (False, False)


def test_scan_redlines_unreliable_when_count_exceeds_enum():
    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Reds:
        def getCount(self):
            return 2

        def createEnumeration(self):
            return _Enum(["only-one"])

    class _Doc:
        def getRedlines(self):
            return _Reds()

    reliable, seen, total = scan_redlines(_Doc(), lambda rl: True)
    assert reliable is False
    assert seen == 1
    assert total == 2


def test_snapshot_redline_ids_complete():
    class _Rl:
        def __init__(self, ident):
            self._ident = ident

        def getPropertyValue(self, name):
            return self._ident

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Reds:
        def __init__(self, items):
            self._items = items

        def getCount(self):
            return len(self._items)

        def createEnumeration(self):
            return _Enum(self._items)

    class _Doc:
        def getRedlines(self):
            return _Reds([_Rl("a"), _Rl("b")])

    ids, ok = snapshot_redline_ids(_Doc())
    assert ok is True
    assert {key[0] for key in ids} == {"a", "b"}


class _Stamp:
    Year, Month, Day, Hours, Minutes, Seconds, NanoSeconds = 2026, 9, 26, 11, 49, 0, 0


class _KeyedRl:
    def __init__(self, rid, rtype="Insert", author="Outro", stamp=_Stamp, comment=""):
        self._props = {"RedlineIdentifier": rid, "RedlineType": rtype, "RedlineAuthor": author,
                       "RedlineDateTime": stamp, "RedlineComment": comment}

    def getPropertyValue(self, name):
        if name == "RedlineSuccessorData":
            return self._props.get("RedlineSuccessorData", ())
        return self._props[name]


class _KeyedDoc:
    def __init__(self, redlines):
        self._redlines = redlines

    def getRedlines(self):
        doc = self

        class _Reds:
            def getCount(self):
                return len(doc._redlines)

            def createEnumeration(self):
                items = list(doc._redlines)

                class _E:
                    def hasMoreElements(self):
                        return bool(items)

                    def nextElement(self):
                        return items.pop(0)
                return _E()
        return _Reds()


class _Later(_Stamp):
    Seconds = 5


def test_new_redlines_since_catches_a_delete_stacked_on_the_same_redline():
    """Deleting exactly an earlier agent insertion stacks the Delete on that SAME redline (same
    identifier); an identifier-only diff never saw it as new, so it was never tagged."""
    from plugin.writer.review_scan import new_redlines_since

    before, ok = snapshot_redline_ids(_KeyedDoc([_KeyedRl("1", author="WriterAgent", comment=TOKEN_PREFIX + "s:0")]))
    assert ok
    stacked = _KeyedRl("1", rtype="Delete", author="WriterAgent (deletions)", stamp=_Later)
    new, ok = new_redlines_since(_KeyedDoc([stacked]), before)
    assert ok and new == [stacked]


def test_new_redlines_since_skips_a_piece_split_off_a_foreign_insert():
    """Deleting mid-way through another author's insertion splits it; the tail piece gets a new
    identifier but keeps type, author, date and comment. Tagging it stamped the user's text as ours."""
    from plugin.writer.review_scan import new_redlines_since

    before, _ok = snapshot_redline_ids(_KeyedDoc([_KeyedRl("1")]))
    ours = _KeyedRl("2", rtype="Delete", author="WriterAgent (deletions)", stamp=_Later)
    piece = _KeyedRl("3")
    new, ok = new_redlines_since(_KeyedDoc([_KeyedRl("1"), ours, piece]), before)
    assert ok and new == [ours]


class _Layer:
    def __init__(self, name, value):
        self.Name, self.Value = name, value


def test_new_redlines_since_leaves_a_stack_on_a_user_change_untagged():
    """Tagging an agent Delete stacked on the user's pending Insert made the user's layer read
    as the agent's; "Reject all agent changes" then rejected the user's Insert too."""
    from plugin.writer.review_scan import new_redlines_since

    before, _ok = snapshot_redline_ids(_KeyedDoc([_KeyedRl("1")]))
    stacked = _KeyedRl("1", rtype="Delete", author="WriterAgent (deletions)", stamp=_Later)
    stacked._props["RedlineSuccessorData"] = (_Layer("RedlineAuthor", "Outro"), _Layer("RedlineComment", ""))
    on_agent = _KeyedRl("2", rtype="Delete", author="WriterAgent (deletions)", stamp=_Later)
    on_agent._props["RedlineSuccessorData"] = (_Layer("RedlineComment", TOKEN_PREFIX + "s:0"),)
    new, ok = new_redlines_since(_KeyedDoc([stacked, on_agent]), before)
    assert ok and new == [on_agent]


class _RaisingDateRl(_KeyedRl):
    def getPropertyValue(self, name):
        if name == "RedlineDateTime":
            raise RuntimeError("date unreadable")
        return super().getPropertyValue(name)


class _RaisingSuccessorRl(_KeyedRl):
    def getPropertyValue(self, name):
        if name == "RedlineSuccessorData":
            raise RuntimeError("successor unreadable")
        return super().getPropertyValue(name)


def test_redline_date_read_error_fails_closed():
    from plugin.writer.review_scan import new_redlines_since

    bad = _RaisingDateRl("1")
    _ids, ok = snapshot_redline_ids(_KeyedDoc([bad]))
    assert ok is False

    new, ok = new_redlines_since(_KeyedDoc([bad]), set())
    assert new == []
    assert ok is False


def test_genuinely_undated_new_redline_is_tagged():
    from plugin.writer.review_scan import new_redlines_since

    rl = _KeyedRl("1", stamp=None)
    new, ok = new_redlines_since(_KeyedDoc([rl]), set())
    assert ok is True
    assert new == [rl]


def test_successor_read_error_leaves_untagged_fail_closed():
    from plugin.writer.review_scan import new_redlines_since

    bad = _RaisingSuccessorRl("1")
    new, ok = new_redlines_since(_KeyedDoc([bad]), set())
    assert ok is True
    assert new == []


def test_agent_change_presence_stop_at_first():
    from plugin.writer.review_scan import agent_change_presence

    calls = []

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            calls.append(1)
            return self._items.pop(0)

    class _Reds:
        def getCount(self):
            return 3

        def createEnumeration(self):
            return _Enum([
                _KeyedRl("1", comment=TOKEN_PREFIX + "s:0"),
                _KeyedRl("2", comment=TOKEN_PREFIX + "s:1"),
                _KeyedRl("3", comment=TOKEN_PREFIX + "s:2"),
            ])

    class _Doc:
        def getRedlines(self):
            return _Reds()

    count, reliable = agent_change_presence(_Doc(), stop_at_first=True)
    assert (count, reliable) == (1, True)
    assert len(calls) == 1


def test_agent_change_presence_get_redlines_raises():
    from plugin.writer.review_scan import agent_change_presence

    class _Doc:
        def getRedlines(self):
            raise RuntimeError("redlines boom")

    count, reliable = agent_change_presence(_Doc())
    assert (count, reliable) == (0, False)


def test_agent_change_presence_clean_document():
    from plugin.writer.review_scan import agent_change_presence

    class _Doc:
        def getRedlines(self):
            return _KeyedDoc([]).getRedlines()

    count, reliable = agent_change_presence(_Doc())
    assert (count, reliable) == (0, True)


def test_agent_change_presence_unreadable_comment():
    from plugin.writer.review_scan import agent_change_presence

    class _BadRl(_KeyedRl):
        def getPropertyValue(self, name):
            if name == "RedlineComment":
                raise RuntimeError("comment unreadable")
            return super().getPropertyValue(name)

    class _Doc:
        def getRedlines(self):
            return _KeyedDoc([_BadRl("1")]).getRedlines()

    count, reliable = agent_change_presence(_Doc())
    assert (count, reliable) == (0, False)


def test_agent_change_presence_unreadable_comment_after_agent_token():
    from plugin.writer.review_scan import agent_change_presence

    class _BadRl(_KeyedRl):
        def getPropertyValue(self, name):
            if name == "RedlineComment":
                raise RuntimeError("comment unreadable")
            return super().getPropertyValue(name)

    class _Doc:
        def getRedlines(self):
            return _KeyedDoc([
                _KeyedRl("1", comment=TOKEN_PREFIX + "s:0"),
                _BadRl("2"),
            ]).getRedlines()

    count, reliable = agent_change_presence(_Doc())
    assert reliable is False
    assert count == 1


def test_document_has_tracked_deletions():
    from unittest.mock import MagicMock

    def doc_with(*types):
        doc = MagicMock()
        reds = doc.getRedlines.return_value
        reds.getCount.return_value = len(types)
        items = [MagicMock(**{"getPropertyValue.return_value": t}) for t in types]
        enum = reds.createEnumeration.return_value
        enum.hasMoreElements.side_effect = [True] * len(items) + [False]
        enum.nextElement.side_effect = items
        return doc

    assert document_has_tracked_deletions(doc_with("Insert", "Delete")) is True
    assert document_has_tracked_deletions(doc_with("Insert")) is False


def test_tracked_deletion_scan_skips_an_unreadable_redline():
    from unittest.mock import MagicMock

    bad = MagicMock()
    bad.getPropertyValue.side_effect = RuntimeError("gone")
    good = MagicMock(**{"getPropertyValue.return_value": "Delete"})
    doc = MagicMock()
    doc.getRedlines.return_value.getCount.return_value = 2
    enum = doc.getRedlines.return_value.createEnumeration.return_value
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [bad, good]
    assert document_has_tracked_deletions(doc) is True

