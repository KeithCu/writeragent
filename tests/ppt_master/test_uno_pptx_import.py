# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Notes-page copy picks the NotesShape and clears stale notes."""

from __future__ import annotations

import pytest

from plugin.ppt_master.adapter.uno_pptx_import import _copy_page_notes

_NOTES = "com.sun.star.presentation.NotesShape"
_HEADER = "com.sun.star.presentation.HeaderShape"


class DisposedException(Exception):
    pass


class _Shape:
    def __init__(self, shape_type: str, text: str = "") -> None:
        self.shape_type = shape_type
        self.text = text

    def getShapeType(self) -> str:
        return self.shape_type

    def getString(self) -> str:
        return self.text

    def setString(self, value: str) -> None:
        self.text = value


class _NotesPage:
    def __init__(self, shapes: list[_Shape]) -> None:
        self.shapes = shapes

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> _Shape:
        return self.shapes[index]


class _Page:
    def __init__(self, shapes: list[_Shape]) -> None:
        self.notes = _NotesPage(shapes)

    def getNotesPage(self) -> _NotesPage:
        return self.notes


def _chrome_then_notes(chrome: str, notes: str) -> tuple[_Shape, _Shape, _Page]:
    header = _Shape(_HEADER, chrome)
    body = _Shape(_NOTES, notes)
    return header, body, _Page([header, body])


def test_copy_page_notes_writes_notes_shape_not_header():
    _src_header, src_body, source = _chrome_then_notes("Confidential", "  Cue the demo  ")
    tgt_header, tgt_body, target = _chrome_then_notes("Footer", "old cue")

    _copy_page_notes(source, target)

    assert tgt_body.text == "Cue the demo"
    assert tgt_header.text == "Footer"
    assert src_body.text == "  Cue the demo  "


def test_copy_page_notes_clears_stale_target_when_source_notes_empty():
    src_header, _src_body, source = _chrome_then_notes("Slide 1", "   ")
    tgt_header, tgt_body, target = _chrome_then_notes("Footer", "previous take")

    _copy_page_notes(source, target)

    assert tgt_body.text == ""
    assert tgt_header.text == "Footer"
    assert src_header.text == "Slide 1"


def test_copy_page_notes_ignores_chrome_when_notes_shape_missing_on_source():
    source = _Page([_Shape(_HEADER, "Date")])
    tgt_header, tgt_body, target = _chrome_then_notes("Footer", "stale")

    _copy_page_notes(source, target)

    assert tgt_body.text == ""
    assert tgt_header.text == "Footer"


def test_copy_page_notes_does_not_write_header_when_target_has_no_notes_shape():
    _src_header, _src_body, source = _chrome_then_notes("Header", "Real notes")
    tgt_header = _Shape(_HEADER, "Keep")
    target = _Page([tgt_header])

    _copy_page_notes(source, target)

    assert tgt_header.text == "Keep"


def test_copy_page_notes_reraises_dispose():
    source = _Page([_Shape(_NOTES, "Real")])

    class _DeadPage:
        def getNotesPage(self) -> _NotesPage:
            raise DisposedException("notes page gone")

    with pytest.raises(DisposedException):
        _copy_page_notes(source, _DeadPage())
