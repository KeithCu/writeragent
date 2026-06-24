# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: streamed edits reveal the review toolbar.

Streamed edit paths tag their redlines via ``tag_agent_redlines`` (not via record_mutation), so the
toolbar refresh must live there too — otherwise the fast-travel toolbar never appears for those
edits even though pending agent changes exist.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.edit_review import tag_agent_redlines


def _fake_toolbar_module():
    # review_toolbar.py imports com.sun.star.util (not in the headless UNO mock), so inject a fake
    # module for tag_agent_redlines' lazy ``from plugin.writer.review_toolbar import ...`` to find.
    m = types.ModuleType("plugin.writer.review_toolbar")
    m.refresh_review_toolbar = MagicMock()
    return m


class FakeRedline:
    def __init__(self, rid, raise_set=False, raise_revert=False):
        self.rid = rid
        self.comment = None
        self._raise_set = raise_set
        self._raise_revert = raise_revert  # tagging succeeds, but reverting (set "") fails -> orphan

    def getPropertyValue(self, name):
        if name == "RedlineIdentifier":
            return self.rid
        if name == "RedlineComment":
            return self.comment if self.comment is not None else ""
        raise KeyError(name)

    def setPropertyValue(self, name, val):
        assert name == "RedlineComment"
        if self._raise_set:
            raise RuntimeError("set boom")
        if self._raise_revert and val == "":
            raise RuntimeError("revert boom")
        self.comment = val


class FakeEnum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class FakeRedlines:
    def __init__(self, items, count=None):
        self._items = items
        self._count = count

    def getCount(self):
        return len(self._items) if self._count is None else self._count

    def createEnumeration(self):
        return FakeEnum(self._items)


class FakeModel:
    def __init__(self, redlines):
        self._rl = redlines

    def getRedlines(self):
        return self._rl


def test_refreshes_toolbar_when_new_redline_tagged():
    rl_old, rl_new = FakeRedline("old1"), FakeRedline("new1")
    doc = FakeModel(FakeRedlines([rl_old, rl_new]))
    fake = _fake_toolbar_module()
    with patch.dict(sys.modules, {"plugin.writer.review_toolbar": fake}):
        token = tag_agent_redlines(doc, before_ids={"old1"}, before_reliable=True)
    assert token is not None
    assert rl_new.comment == token   # only the new redline got tagged
    assert rl_old.comment is None
    fake.refresh_review_toolbar.assert_called_once_with(doc)


def test_no_refresh_when_nothing_new_tagged():
    rl = FakeRedline("old1")
    doc = FakeModel(FakeRedlines([rl]))
    fake = _fake_toolbar_module()
    with patch.dict(sys.modules, {"plugin.writer.review_toolbar": fake}):
        token = tag_agent_redlines(doc, before_ids={"old1"}, before_reliable=True)
    assert token is None
    fake.refresh_review_toolbar.assert_not_called()


# The toolbar's only contract on a REFUSAL is: tag_agent_redlines returns None and NEVER refreshes
# the toolbar -- on ANY refusal path. The SPECIFIC refusal/orphan/revert internals (return values,
# all-or-nothing revert, orphan accounting, and the single-orphan==len failure case) are unit-tested
# against _tag_new_redlines / record_mutation in test_edit_review.py; here we only pin that no refusal
# path triggers a toolbar refresh. Each builder returns (doc, kwargs) for one refusal scenario.

def _refusal_default_no_reliable():
    # before_reliable omitted -> defaults to False (an unverified snapshot fails closed).
    return FakeModel(FakeRedlines([FakeRedline("old1"), FakeRedline("new1")])), {"before_ids": {"old1"}}


def _refusal_before_unreliable():
    return (FakeModel(FakeRedlines([FakeRedline("old1"), FakeRedline("new1")])),
            {"before_ids": {"old1"}, "before_reliable": False})


def _refusal_after_scan_incomplete():
    # getCount() reports more than the enumeration yields -> incomplete scan.
    return (FakeModel(FakeRedlines([FakeRedline("old1"), FakeRedline("new1")], count=3)),
            {"before_ids": {"old1"}, "before_reliable": True})


def _refusal_partial_tag_reverted():
    # Two new redlines; tagging the second fails -> the first is reverted (complete-or-none).
    return (FakeModel(FakeRedlines([FakeRedline("old1"), FakeRedline("a"), FakeRedline("b", raise_set=True)])),
            {"before_ids": {"old1"}, "before_reliable": True})


def _refusal_revert_leaves_orphan():
    # First tags OK but its revert fails (orphan); second's tag fails (triggers the revert).
    return (FakeModel(FakeRedlines([FakeRedline("old1"),
                                    FakeRedline("a", raise_revert=True), FakeRedline("b", raise_set=True)])),
            {"before_ids": {"old1"}, "before_reliable": True})


def _refusal_single_orphan():
    # ONE new redline that mutates-then-throws and can't be reverted (orphans == len == 1): an
    # ambiguous int would have read this as success.
    class MutateThenThrow(FakeRedline):
        def setPropertyValue(self, name, val):
            assert name == "RedlineComment"
            if val == "":
                raise RuntimeError("revert cannot clear")
            self.comment = val
            raise RuntimeError("set wrote then threw")

    return (FakeModel(FakeRedlines([FakeRedline("old1"), MutateThenThrow("a")])),
            {"before_ids": {"old1"}, "before_reliable": True})


@pytest.mark.parametrize("build", [
    _refusal_default_no_reliable,
    _refusal_before_unreliable,
    _refusal_after_scan_incomplete,
    _refusal_partial_tag_reverted,
    _refusal_revert_leaves_orphan,
    _refusal_single_orphan,
], ids=["default_no_reliable", "before_unreliable", "after_scan_incomplete",
        "partial_tag_reverted", "revert_leaves_orphan", "single_orphan"])
def test_no_refresh_on_any_refusal_path(build):
    """tag_agent_redlines returns None and never refreshes the toolbar on ANY refusal path."""
    doc, kwargs = build()
    fake = _fake_toolbar_module()
    with patch.dict(sys.modules, {"plugin.writer.review_toolbar": fake}):
        token = tag_agent_redlines(doc, **kwargs)
    assert token is None
    fake.refresh_review_toolbar.assert_not_called()
