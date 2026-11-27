# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the live-document helpers in tests/writer/doc_builders.py (no UNO)."""

import weakref

from tests.writer.doc_builders import list_redlines


class _Redline:
    """Stands in for one SwXRedline proxy; a weakref shows when the caller dropped it."""

    def __init__(self, index: int) -> None:
        self._props = {
            "RedlineType": "Insert",
            "RedlineComment": "wa-review:abcd1234:%d" % index,
            "RedlineAuthor": "WriterAgent",
        }

    def getPropertyValue(self, name: str) -> str:  # noqa: N802 -- UNO name
        return self._props[name]


class _Redlines:
    """Index access only, recording every fetch and whether earlier proxies are still referenced."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.fetched: list[int] = []
        self._handed_out: list[weakref.ref[_Redline]] = []
        self.dropped_before_fetch: list[int] = []

    def getCount(self) -> int:  # noqa: N802 -- UNO name
        return self._count

    def getByIndex(self, index: int) -> _Redline:  # noqa: N802 -- UNO name
        if any(ref() is None for ref in self._handed_out):
            self.dropped_before_fetch.append(index)
        self.fetched.append(index)
        redline = _Redline(index)
        self._handed_out.append(weakref.ref(redline))
        return redline


class _Doc:
    def __init__(self, redlines: _Redlines) -> None:
        self._redlines = redlines

    def getRedlines(self) -> _Redlines:  # noqa: N802 -- UNO name
        return self._redlines


def test_list_redlines_fetches_each_redline_once_and_holds_them_all():
    # Over URP, dropping a redline right before fetching the next one races the SwXRedline
    # destructor against SwXRedlines::GetObject and can crash soffice (see list_redlines).
    redlines = _Redlines(3)

    out = list_redlines(_Doc(redlines))

    assert redlines.fetched == [0, 1, 2]
    assert redlines.dropped_before_fetch == []
    assert [rl["RedlineComment"] for rl in out] == [
        "wa-review:abcd1234:0",
        "wa-review:abcd1234:1",
        "wa-review:abcd1234:2",
    ]
    assert {rl["type"] for rl in out} == {"Insert"}
    assert {rl["RedlineAuthor"] for rl in out} == {"WriterAgent"}
