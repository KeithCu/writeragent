# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""PPTX import keeps target shapes on failure and copies the NotesShape."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugin.contrib.ppt_master.coords import DEFAULT_SLIDE_HEIGHT_HMM, DEFAULT_SLIDE_WIDTH_HMM
from plugin.framework.errors import DocumentDisposedError
from plugin.ppt_master.adapter import uno_pptx_import
from plugin.ppt_master.adapter.uno_pptx_import import _copy_page_notes, _import_slides_from_source
import os
import tempfile
from unittest.mock import MagicMock

from plugin.draw.shape_clone import (
    SHAPE_CLONE_PROPS,
    ClonedShape,
    clone_shape,
)
from plugin.ppt_master.adapter.uno_pptx_import import copy_shapes_to_page
from tests.harness.strip_bundle import is_release_build, skip_if_release_build

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
    assert result["code"] == "TOOL_EXECUTION_ERROR"
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

    with pytest.raises(DocumentDisposedError):
        _copy_page_notes(source, _DeadPage())

def test_copy_page_notes_skips_non_dispose_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    source = _Page([_Shape(_NOTES, "Real")])

    class _BrokenShape(_Shape):
        def setString(self, value: str) -> None:
            raise ValueError("Some normal failure")

    tgt_shape = _BrokenShape(_NOTES, "old")
    target = _Page([tgt_shape])

    # Must not raise; skips notes for this slide and logs warning
    _copy_page_notes(source, target)
    assert any("Skipping slide notes copy" in record.message for record in caplog.records)


# --- migrated from test_uno_shape_postprocess.py ---

_POOL_ITEM_PROPS = ("Graphic", "GraphicURL", "FillBitmap", "Background")


class _ScFakeShape:
    def __init__(self, shape_type: str, props: dict[str, Any] | None = None, text: str = "") -> None:
        self.shape_type = shape_type
        self.props = dict(props or {})
        self.sets: list[tuple[str, Any]] = []
        self.position = (1, 2)
        self.size = (10, 20)
        self.string = text

    def getShapeType(self) -> str:
        return self.shape_type

    def getPropertyValue(self, name: str) -> Any:
        if name not in self.props:
            raise AttributeError(name)
        return self.props[name]

    def setPropertyValue(self, name: str, value: Any) -> None:
        self.sets.append((name, value))
        self.props[name] = value

    def getPosition(self) -> tuple[int, int]:
        return self.position

    def setPosition(self, value: tuple[int, int]) -> None:
        self.position = value

    def getSize(self) -> tuple[int, int]:
        return self.size

    def setSize(self, value: tuple[int, int]) -> None:
        self.size = value

    def getString(self) -> str:
        return self.string

    def setString(self, value: str) -> None:
        self.string = value


class _ScFakePage:
    def __init__(self, shapes: list[Any] | None = None) -> None:
        self.shapes = list(shapes or [])

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> Any:
        return self.shapes[index]

    def add(self, shape: Any) -> None:
        self.shapes.append(shape)

    def remove(self, shape: Any) -> None:
        self.shapes.remove(shape)


def test_copy_props_omit_hidden_pool_items():
    for name in _POOL_ITEM_PROPS:
        assert name not in SHAPE_CLONE_PROPS
    assert "FillGradient" not in SHAPE_CLONE_PROPS
    assert "FillHatch" not in SHAPE_CLONE_PROPS


def test_graphic_clone_reimports_pixels_instead_of_aliasing_source_pool(monkeypatch):
    live = object()
    owned = object()
    source = _ScFakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"FillColor": 5, "Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
        text="cap",
    )
    dest = _ScFakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    page = _ScFakePage()
    provider = MagicMock()
    provider.queryGraphic.side_effect = [None, owned]
    uno_ctx = MagicMock()
    uno_ctx.ServiceManager.createInstanceWithContext.return_value = provider
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", tracking_mkstemp)

    result = clone_shape(doc, page, source, uno_ctx=uno_ctx)

    assert result.shape is dest
    assert result.reimported is True
    assert result.graphic_shapes == 1
    assert result.graphic_reimports == 1
    graphic_values = [value for name, value in dest.sets if name == "Graphic"]
    assert graphic_values == [owned]
    assert live not in graphic_values
    assert all(name != "GraphicURL" for name, value in dest.sets)
    assert dest.props["FillColor"] == 5
    uno_ctx.ServiceManager.createInstanceWithContext.assert_called_once_with(
        "com.sun.star.graphic.GraphicProvider",
        uno_ctx,
    )
    assert provider.storeGraphic.call_count == 2
    assert provider.storeGraphic.call_args_list[0].args[0] is live
    assert provider.storeGraphic.call_args_list[1].args[0] is live
    assert created
    assert all(not os.path.exists(path) for path in created)


def test_rectangle_clone_does_not_touch_graphic_provider():
    source = _ScFakeShape("com.sun.star.drawing.RectangleShape", {"FillColor": 9}, text="hi")
    dest = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    uno_ctx = MagicMock()

    result = clone_shape(doc, _ScFakePage(), source, uno_ctx=uno_ctx)

    assert result.shape is dest
    assert result.reimported is None
    assert dest.props["FillColor"] == 9
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    uno_ctx.ServiceManager.createInstanceWithContext.assert_not_called()


def test_graphic_clone_without_context_does_not_alias_live_graphic():
    live = object()
    source = _ScFakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
    )
    dest = _ScFakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest

    result = clone_shape(doc, _ScFakePage(), source, uno_ctx=None)

    assert result.shape is dest
    assert result.reimported is False
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    assert all(name != "Graphic" or value is not live for name, value in dest.sets)


def test_copy_shapes_to_page_forwards_uno_ctx(monkeypatch):
    seen: dict[str, Any] = {}
    source = _ScFakeShape("com.sun.star.drawing.RectangleShape", {"FillColor": 1})
    dest_page = _ScFakePage()
    doc = MagicMock()

    def fake_clone(
        target_doc: Any,
        target_page: Any,
        source_shape: Any,
        *,
        uno_ctx: Any | None = None,
        **kwargs: Any,
    ) -> ClonedShape:
        seen["ctx"] = uno_ctx
        seen["source"] = source_shape
        seen["doc"] = target_doc
        seen["page"] = target_page
        return ClonedShape(
            shape=object(),
            shape_type="com.sun.star.drawing.RectangleShape",
            reimported=None,
            graphic_shapes=0,
            graphic_reimports=0,
            empty_group_message=None,
        )

    monkeypatch.setattr(uno_pptx_import, "clone_shape", fake_clone)
    marker = object()
    copied = copy_shapes_to_page(_ScFakePage([source]), doc, dest_page, uno_ctx=marker)

    assert copied == 1
    assert seen["ctx"] is marker
    assert seen["source"] is source
    assert seen["doc"] is doc
    assert seen["page"] is dest_page


def test_clone_shape_copies_fill_gradient_and_hatch():
    doc = MagicMock()
    page = _ScFakePage()

    # 1. Gradient with value="GRADIENT"
    dest1 = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest1
    grad_val = MagicMock()
    grad_val.value = "GRADIENT"
    grad_struct = MagicMock()
    source1 = _ScFakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": grad_val, "FillGradient": grad_struct},
    )
    clone_shape(doc, page, source1)
    assert dest1.props.get("FillGradient") is grad_struct

    # 2. Gradient with int 2
    dest2 = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest2
    source2 = _ScFakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": 2, "FillGradient": grad_struct},
    )
    clone_shape(doc, page, source2)
    assert dest2.props.get("FillGradient") is grad_struct

    # 3. Hatch
    dest3 = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest3
    hatch_struct = MagicMock()
    source3 = _ScFakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": 3, "FillHatch": hatch_struct},
    )
    clone_shape(doc, page, source3)
    assert dest3.props.get("FillHatch") is hatch_struct


def test_copy_shapes_to_page_disposal_raises_while_runtime_exception_skips():
    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    doc = MagicMock()
    page = _ScFakePage()

    # Case A: shape 1 raises DisposedException -> copy_shapes_to_page raises, shape 2 not cloned
    shape1_disp = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    shape1_disp.getPropertyValue = MagicMock(side_effect=DisposedException("doc disposed"))
    shape2_ok = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    shape2_ok.getPropertyValue = MagicMock(return_value=None)
    src_page_disp = _ScFakePage([shape1_disp, shape2_ok])

    dest_shape = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest_shape

    with pytest.raises(DisposedException):
        copy_shapes_to_page(src_page_disp, doc, page)
    # Shape 1 was added before disposal raised; shape 2 was never cloned
    assert page.getCount() == 1
    shape2_ok.getPropertyValue.assert_not_called()

    # Case B: shape 1 raises RuntimeException -> skipped, shape 2 is cloned, return count is 1
    shape1_err = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    shape1_err.getShapeType = MagicMock(side_effect=RuntimeException("shape corrupted"))
    shape2_ok2 = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    src_page_err = _ScFakePage([shape1_err, shape2_ok2])

    target_page = _ScFakePage()
    copied = copy_shapes_to_page(src_page_err, doc, target_page)
    assert copied == 1
    assert target_page.getCount() == 1


def test_copy_shapes_to_page_empty_group_removes_clone_and_skips(monkeypatch):
    doc = MagicMock()
    clone1 = _ScFakeShape("com.sun.star.drawing.GroupShape")
    clone2 = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    target_page = _ScFakePage()

    results = [
        ClonedShape(
            shape=clone1,
            shape_type="com.sun.star.drawing.GroupShape",
            reimported=None,
            graphic_shapes=0,
            graphic_reimports=0,
            empty_group_message="Empty group",
        ),
        ClonedShape(
            shape=clone2,
            shape_type="com.sun.star.drawing.RectangleShape",
            reimported=None,
            graphic_shapes=0,
            graphic_reimports=0,
            empty_group_message=None,
        ),
    ]

    def mock_clone(tdoc, tpage, src, **kwargs):
        res = results.pop(0)
        tpage.add(res.shape)
        return res

    monkeypatch.setattr(uno_pptx_import, "clone_shape", mock_clone)

    src_page = _ScFakePage([_ScFakeShape("s1"), _ScFakeShape("s2")])
    copied = copy_shapes_to_page(src_page, doc, target_page)
    assert copied == 1
    assert target_page.getCount() == 1
    assert target_page.getByIndex(0) is clone2


def test_copy_shapes_to_page_empty_group_remove_fails_logs_debug(monkeypatch, caplog):
    import logging

    doc = MagicMock()
    clone1 = _ScFakeShape("com.sun.star.drawing.GroupShape")
    target_page = MagicMock()
    target_page.remove.side_effect = RuntimeError("cannot remove")

    res = ClonedShape(
        shape=clone1,
        shape_type="com.sun.star.drawing.GroupShape",
        reimported=None,
        graphic_shapes=0,
        graphic_reimports=0,
        empty_group_message="Empty group",
    )
    monkeypatch.setattr(uno_pptx_import, "clone_shape", lambda *a, **kw: res)

    src_page = _ScFakePage([_ScFakeShape("s1")])
    with caplog.at_level(logging.DEBUG):
        copied = copy_shapes_to_page(src_page, doc, target_page)
    assert copied == 0
    # Release strip removes log.debug; empty-group WARNING path still fires.
    if not is_release_build():
        assert "remove empty-group clone failed" in caplog.text
    else:
        assert "clone_shape empty group" in caplog.text


def test_import_pptx_public_entry_points_raise_off_main(monkeypatch, tmp_path: Path) -> None:
    skip_if_release_build("skipped for release builds")
    import threading

    from plugin.framework import thread_guard as tg
    from plugin.framework.queue_executor import Policy, main
    from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_slide_to_odp, import_pptx_to_doc

    fake_bg = MagicMock()
    fake_bg.name = "bg-task"
    monkeypatch.setattr(threading, "current_thread", lambda: fake_bg)
    monkeypatch.setattr(main, "is_current", lambda: False)
    main.install(Policy.test_inline)
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        pptx = tmp_path / "deck.pptx"
        pptx.write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError):
            import_pptx_slide_to_odp(MagicMock(), pptx, 0, tmp_path / "out.odp")
        with pytest.raises(RuntimeError):
            import_pptx_to_doc(MagicMock(), MagicMock(), pptx)
    finally:
        tg.GUARD_ON = was