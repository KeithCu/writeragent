# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""PPTX import keeps target shapes on failure and copies the NotesShape."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.ppt_master.adapter import uno_pptx_import
from plugin.ppt_master.adapter.uno_pptx_import import _copy_page_notes, _import_slides_from_source

_NOTES = "com.sun.star.presentation.NotesShape"
_HEADER = "com.sun.star.presentation.HeaderShape"


class FakePage:
    def __init__(self, shapes: list[Any] | None = None) -> None:
        self.shapes = list(shapes or [])
        self.props: dict[str, Any] = {}

    def getNotesPage(self) -> Any:
        return _NotesPage([])

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> Any:
        return self.shapes[index]

    def add(self, shape: Any) -> None:
        self.shapes.append(shape)

    def remove(self, shape: Any) -> None:
        self.shapes.remove(shape)

    def setPropertyValue(self, name: str, value: Any) -> None:
        self.props[name] = value


class FakePages:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def getCount(self) -> int:
        return len(self.pages)

    def getByIndex(self, index: int) -> FakePage:
        return self.pages[index]

    def remove(self, page: FakePage) -> None:
        self.pages.remove(page)


class FakeBridge:
    """Honors ``create_slide``'s contract: the new page occupies ``index``."""

    last: FakeBridge | None = None

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self.current: int | None = None
        FakeBridge.last = self

    def get_pages(self) -> FakePages:
        return self.doc.pages

    def create_slide(self, index: int | None = None, switch: bool = True) -> tuple[FakePage, int]:
        del switch
        page = FakePage([])
        pages = self.get_pages()
        count = pages.getCount()
        if index is None or index >= count:
            pages.pages.append(page)
            landed = pages.getCount() - 1
        else:
            pages.pages.insert(index, page)
            landed = index
        return page, landed

    def set_current_page_index(self, index: int) -> bool:
        self.current = index
        return True


def _deck(user_slides: list[list[Any]], source_slides: int) -> tuple[Any, Any, list[FakePage], list[FakePage]]:
    target_pages = [FakePage(list(shapes)) for shapes in user_slides]
    source_pages = [FakePage([f"src-{index}"]) for index in range(source_slides)]
    target = SimpleNamespace(pages=FakePages(target_pages))
    source = SimpleNamespace(getDrawPages=lambda: FakePages(source_pages))
    return target, source, target_pages, source_pages


def _patch_copy(monkeypatch, copy):
    monkeypatch.setattr(uno_pptx_import, "DrawBridge", FakeBridge)
    monkeypatch.setattr(uno_pptx_import, "copy_shapes_to_page", copy)


def test_failed_copy_keeps_existing_shapes_and_size(monkeypatch):
    target, source, target_pages, source_pages = _deck([["USER-A", "USER-B"]], 1)
    seen: dict[str, Any] = {}

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del source_page, target_doc
        seen["page"] = target_page
        seen["ctx"] = uno_ctx
        target_page.add("LEAK")
        return 0

    _patch_copy(monkeypatch, fake_copy)
    ctx = object()
    result = _import_slides_from_source(ctx, target, source, clear_existing=True)

    assert result["status"] == "error"
    assert "No shapes copied" in result["message"]
    assert target_pages[0].shapes == ["USER-A", "USER-B"]
    assert "Width" not in target_pages[0].props
    assert "Height" not in target_pages[0].props
    assert seen["page"] is target_pages[0]
    assert seen["ctx"] is ctx
    assert FakeBridge.last is not None
    assert FakeBridge.last.current is None
    assert len(target.pages.pages) == 1
    assert source_pages[0].getCount() == 1


def test_successful_copy_replaces_previous_shapes(monkeypatch):
    target, source, target_pages, _source_pages = _deck([["USER-A", "USER-B"]], 1)

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del source_page, target_doc, uno_ctx
        target_page.add("NEW-1")
        target_page.add("NEW-2")
        return 2

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source, clear_existing=True)

    assert result["status"] == "ok"
    assert result["slides"] == 1
    assert target_pages[0].shapes == ["NEW-1", "NEW-2"]
    assert target_pages[0].props["Width"] == DEFAULT_SLIDE_WIDTH_HMM
    assert target_pages[0].props["Height"] == DEFAULT_SLIDE_HEIGHT_HMM
    assert FakeBridge.last is not None
    assert FakeBridge.last.current == 0


def test_clear_existing_false_appends_on_first_slide(monkeypatch):
    target, source, target_pages, _source_pages = _deck([["USER"]], 1)

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del source_page, target_doc, uno_ctx
        target_page.add("NEW")
        return 1

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source, clear_existing=False)

    assert result["status"] == "ok"
    assert target_pages[0].shapes == ["USER", "NEW"]


def test_later_slide_failure_keeps_that_slide(monkeypatch):
    target, source, target_pages, source_pages = _deck([["USER-0"], ["USER-1"]], 2)

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del target_doc, uno_ctx
        if source_page is source_pages[1]:
            target_page.add("LEAK")
            return 0
        target_page.add("NEW-0")
        return 1

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source, clear_existing=True)

    assert result["status"] == "error"
    assert target_pages[0].shapes == ["NEW-0"]
    assert target_pages[1].shapes == ["USER-1"]
    assert "Width" not in target_pages[1].props
    assert len(target.pages.pages) == 2
    assert FakeBridge.last is not None
    assert FakeBridge.last.current == 0


def test_failed_copy_drops_a_slide_this_call_created(monkeypatch):
    target, source, target_pages, source_pages = _deck([["USER-0"]], 2)

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del target_doc, uno_ctx
        if source_page is source_pages[1]:
            target_page.add("LEAK")
            return 0
        target_page.add("NEW-0")
        return 1

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source, clear_existing=True)

    assert result["status"] == "error"
    assert target.pages.pages == [target_pages[0]]
    assert target_pages[0].shapes == ["NEW-0"]
    assert all(shape != "LEAK" for shape in target_pages[0].shapes)


def test_out_of_range_index_does_not_touch_the_target(monkeypatch):
    target, source, target_pages, _source_pages = _deck([["USER"]], 1)
    called = {"copy": False}

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del source_page, target_doc, target_page, uno_ctx
        called["copy"] = True
        return 1

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source, slide_indices=[0, 4])

    assert result["status"] == "error"
    assert "out of range" in result["message"]
    assert called["copy"] is False
    assert target_pages[0].shapes == ["USER"]
    assert "Width" not in target_pages[0].props


def test_empty_source_does_not_touch_the_target(monkeypatch):
    target, source, target_pages, _source_pages = _deck([["USER"]], 0)

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del source_page, target_doc, target_page, uno_ctx
        raise AssertionError("copy should not run")

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source)

    assert result["status"] == "error"
    assert "no slides" in result["message"]
    assert target_pages[0].shapes == ["USER"]

def test_stop_checker_cancels_import(monkeypatch):
    target, source, target_pages, _source_pages = _deck([["USER"]], 1)
    called = {"copy": False}
    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        called["copy"] = True
        return 1

    _patch_copy(monkeypatch, fake_copy)

    result = _import_slides_from_source(object(), target, source, stop_checker=lambda: True)
    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
    assert target_pages[0].shapes == ["USER"]


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

def test_copy_page_notes_raises_non_dispose_exceptions():
    source = _Page([_Shape(_NOTES, "Real")])

    class _BrokenShape(_Shape):
        def setString(self, value: str) -> None:
            raise ValueError("Some normal failure")

    tgt_shape = _BrokenShape(_NOTES, "old")
    target = _Page([tgt_shape])

    with pytest.raises(ValueError, match="Some normal failure"):
        _copy_page_notes(source, target)
