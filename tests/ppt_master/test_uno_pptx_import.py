# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.ppt_master.adapter import uno_pptx_import
from plugin.ppt_master.adapter.uno_pptx_import import _import_slides_from_source


class FakePage:
    def __init__(self, shapes: list[Any] | None = None) -> None:
        self.shapes = list(shapes or [])
        self.props: dict[str, Any] = {}

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
