# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""T2 (R3): collect_tracked_changes walks text portions and reports pending insertions/deletions
with their text, so a reader can tell new-vs-old and that changes await review. No LibreOffice
required (drives the portion walk with light fakes). Inline live behavior is verified separately."""
from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.doc.document_helpers import collect_tracked_changes


def _enum(items):
    e = MagicMock()
    st = {"i": 0}
    e.hasMoreElements.side_effect = lambda: st["i"] < len(items)

    def _nxt():
        v = items[st["i"]]
        st["i"] += 1
        return v

    e.nextElement.side_effect = _nxt
    return e


class _Range:  # not a Mock, so the helper's mock-guard doesn't short-circuit
    def __init__(self, paras):
        self._paras = paras

    def createEnumeration(self):
        return _enum(self._paras)


class _Para:
    def __init__(self, portions):
        self._portions = portions

    def createEnumeration(self):
        return _enum(self._portions)


def _portion(ptype, rtype=None, s=""):
    p = MagicMock()

    def _gpv(k):
        if k == "TextPortionType":
            return ptype
        if k == "RedlineType":
            return rtype
        raise KeyError(k)

    p.getPropertyValue.side_effect = _gpv
    p.getString.return_value = s
    return p


def _ins_open():
    return _portion("Redline", "Insert")


def _del_open():
    return _portion("Redline", "Delete")


def test_replace_yields_insertion_then_deletion():
    # A replace records an inserted run and a deleted run (each bracketed by Redline portions).
    para = _Para([
        _ins_open(), _portion("Text", s="novo texto"), _ins_open(),
        _del_open(), _portion("Text", s="texto velho"), _del_open(),
    ])
    out = collect_tracked_changes(_Range([para]))
    assert out == [
        {"type": "insertion", "text": "novo texto"},
        {"type": "deletion", "text": "texto velho"},
    ]


def test_plain_text_no_changes():
    para = _Para([_portion("Text", s="só texto normal")])
    assert collect_tracked_changes(_Range([para])) == []


def test_only_insertion():
    para = _Para([_ins_open(), _portion("Text", s="adicionado"), _ins_open()])
    assert collect_tracked_changes(_Range([para])) == [{"type": "insertion", "text": "adicionado"}]


def test_only_deletion():
    para = _Para([_del_open(), _portion("Text", s="removido"), _del_open()])
    assert collect_tracked_changes(_Range([para])) == [{"type": "deletion", "text": "removido"}]


def test_mock_range_short_circuits_to_empty():
    assert collect_tracked_changes(MagicMock()) == []
