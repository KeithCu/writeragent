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
from unittest.mock import MagicMock

from plugin.draw.shape_clone import ClonedShape
from plugin.ppt_master.adapter.uno_pptx_import import copy_shapes_to_page
from tests.draw.shape_fakes import FailingPosShape, FakePage, FakeShape
from tests.harness.strip_bundle import skip_if_release_build

_NOTES = "com.sun.star.presentation.NotesShape"
_HEADER = "com.sun.star.presentation.HeaderShape"


class FakeSlidePage(FakePage):
    """A draw page that also has page properties and an empty notes page."""

    def __init__(self, shapes: list[Any] | None = None) -> None:
        super().__init__(shapes)
        self.props: dict[str, Any] = {}

    def getNotesPage(self) -> Any:
        return _NotesPage([])

    def setPropertyValue(self, name: str, value: Any) -> None:
        self.props[name] = value


class FakePages:
    def __init__(self, pages: list[FakeSlidePage]) -> None:
        self.pages = pages

    def getCount(self) -> int:
        return len(self.pages)

    def getByIndex(self, index: int) -> FakeSlidePage:
        return self.pages[index]

    def remove(self, page: FakeSlidePage) -> None:
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

    def create_slide(self, index: int | None = None, switch: bool = True) -> tuple[FakeSlidePage, int]:
        del switch
        page = FakeSlidePage([])
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


def _deck(user_slides: list[list[Any]], source_slides: int) -> tuple[Any, Any, list[FakeSlidePage], list[FakeSlidePage]]:
    target_pages = [FakeSlidePage(list(shapes)) for shapes in user_slides]
    source_pages = [FakeSlidePage([f"src-{index}"]) for index in range(source_slides)]
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

def test_copy_page_notes_skips_non_dispose_exceptions() -> None:
    source = _Page([_Shape(_NOTES, "Real")])

    class _BrokenShape(_Shape):
        def setString(self, value: str) -> None:
            raise ValueError("Some normal failure")

    tgt_shape = _BrokenShape(_NOTES, "old")
    target = _Page([tgt_shape])

    # Must not raise; skips notes for this slide.
    _copy_page_notes(source, target)


def test_copy_shapes_to_page_reimports_graphics_with_context():
    live = object()
    owned = object()
    source = FakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
    )
    dest = FakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    provider = MagicMock()
    provider.queryGraphic.side_effect = [None, owned]
    uno_ctx = MagicMock()
    uno_ctx.ServiceManager.createInstanceWithContext.return_value = provider

    copied = copy_shapes_to_page(FakePage([source]), doc, FakePage(), uno_ctx=uno_ctx)

    assert copied == 1
    assert dest.props.get("Graphic") is owned


def test_copy_shapes_to_page_disposal_raises_while_runtime_exception_skips():
    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    doc = MagicMock()
    page = FakePage()

    # Case A: shape 1 raises DisposedException -> copy_shapes_to_page raises, shape 2 not cloned
    shape1_disp = FakeShape("com.sun.star.drawing.RectangleShape")
    shape1_disp.getPropertyValue = MagicMock(side_effect=DisposedException("doc disposed"))
    shape2_ok = FakeShape("com.sun.star.drawing.RectangleShape")
    shape2_ok.getPropertyValue = MagicMock(return_value=None)
    src_page_disp = FakePage([shape1_disp, shape2_ok])

    dest_shape = FakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest_shape

    with pytest.raises(DisposedException):
        copy_shapes_to_page(src_page_disp, doc, page)
    # Shape 1 was added before disposal raised; shape 2 was never cloned
    assert page.getCount() == 1
    shape2_ok.getPropertyValue.assert_not_called()

    # Case B: shape 1 raises RuntimeException -> skipped, shape 2 is cloned, return count is 1
    shape1_err = FakeShape("com.sun.star.drawing.RectangleShape")
    shape1_err.getShapeType = MagicMock(side_effect=RuntimeException("shape corrupted"))
    shape2_ok2 = FakeShape("com.sun.star.drawing.RectangleShape")
    src_page_err = FakePage([shape1_err, shape2_ok2])

    target_page = FakePage()
    copied = copy_shapes_to_page(src_page_err, doc, target_page)
    assert copied == 1
    assert target_page.getCount() == 1


def test_copy_shapes_to_page_empty_group_removes_clone_and_skips(monkeypatch):
    doc = MagicMock()
    clone1 = FakeShape("com.sun.star.drawing.GroupShape")
    clone2 = FakeShape("com.sun.star.drawing.RectangleShape")
    target_page = FakePage()

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

    src_page = FakePage([FakeShape("s1"), FakeShape("s2")])
    copied = copy_shapes_to_page(src_page, doc, target_page)
    assert copied == 1
    assert target_page.getCount() == 1
    assert target_page.getByIndex(0) is clone2


def test_copy_shapes_to_page_empty_group_remove_fails_returns_zero(monkeypatch):
    doc = MagicMock()
    clone1 = FakeShape("com.sun.star.drawing.GroupShape")
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

    src_page = FakePage([FakeShape("s1")])
    copied = copy_shapes_to_page(src_page, doc, target_page)
    assert copied == 0


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


def test_load_pptx_is_read_only_and_drops_url(monkeypatch, tmp_path: Path) -> None:
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"pptx")

    class _FakeModel:
        def __init__(self, url: str) -> None:
            self._url = url

        def getRuntimeUID(self) -> str:
            return "uid-1"

        def getURL(self) -> str:
            return self._url

        def attachResource(self, url: str, _args: tuple[Any, ...]) -> None:
            self._url = url

    seen: dict[str, Any] = {}

    class _FakeDesktop:
        def loadComponentFromURL(self, url: str, target: str, flags: int, props: tuple[Any, ...]) -> _FakeModel:
            seen["url"] = url
            seen["target"] = target
            seen["flags"] = flags
            seen["props"] = props
            return _FakeModel(url)

    monkeypatch.setattr(uno_pptx_import, "get_desktop", lambda _ctx: _FakeDesktop())
    monkeypatch.setattr(
        uno_pptx_import,
        "create_property_value",
        lambda name, value: SimpleNamespace(Name=name, Value=value),
    )

    doc = uno_pptx_import.load_pptx_as_impress_doc(MagicMock(), pptx)
    assert doc is not None
    assert doc.getURL() == ""
    prop_map = {p.Name: p.Value for p in seen["props"]}
    assert prop_map.get("Hidden") is True
    assert prop_map.get("ReadOnly") is True


def test_load_pptx_returns_none_when_load_returns_none(monkeypatch, tmp_path: Path) -> None:
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"pptx")

    class _FakeDesktop:
        def loadComponentFromURL(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(uno_pptx_import, "get_desktop", lambda _ctx: _FakeDesktop())
    monkeypatch.setattr(
        uno_pptx_import,
        "create_property_value",
        lambda name, value: SimpleNamespace(Name=name, Value=value),
    )

    assert uno_pptx_import.load_pptx_as_impress_doc(MagicMock(), pptx) is None


def test_copy_that_lands_before_previous_shapes_is_kept(monkeypatch):
    target, source, target_pages, _source_pages = _deck([["USER-A", "USER-B"]], 1)

    def fake_copy(source_page, target_doc, target_page, uno_ctx=None):
        del source_page, target_doc, uno_ctx
        target_page.shapes.insert(0, "NEW")
        return 1

    _patch_copy(monkeypatch, fake_copy)
    result = _import_slides_from_source(object(), target, source, clear_existing=True)

    assert result["status"] == "ok"
    assert target_pages[0].shapes == ["NEW"]


def test_copy_shapes_to_page_removes_a_partial_clone_and_keeps_the_rest():
    src1 = FakeShape("com.sun.star.drawing.RectangleShape", {"Position": (1, 2)})
    src2 = FakeShape("com.sun.star.drawing.RectangleShape", {"Position": (3, 4)})
    clone1 = FailingPosShape("com.sun.star.drawing.RectangleShape")
    clone2 = FakeShape("com.sun.star.drawing.RectangleShape")
    created = [clone1, clone2]
    doc = MagicMock()
    doc.createInstance.side_effect = lambda _t: created.pop(0)
    target_page = FakePage()

    copied = copy_shapes_to_page(FakePage([src1, src2]), doc, target_page)
    assert copied == 1
    assert target_page.shapes == [clone2]


def test_failed_import_with_real_copy_shapes_leaves_previous_shapes_unchanged(monkeypatch):
    target, source, target_pages, source_pages = _deck([["USER-A", "USER-B"]], 1)
    src_shape = FakeShape("com.sun.star.drawing.RectangleShape", {"Position": (1, 2)})
    source_pages[0].shapes = [src_shape]
    target.createInstance = lambda _t: FailingPosShape("com.sun.star.drawing.RectangleShape")

    monkeypatch.setattr(uno_pptx_import, "DrawBridge", FakeBridge)
    result = _import_slides_from_source(object(), target, source, clear_existing=True)

    assert result["status"] == "error"
    assert target_pages[0].shapes == ["USER-A", "USER-B"]


@pytest.mark.parametrize("failure_mode", ["copy_raises", "copy_error", "store_raises"])
def test_import_pptx_slide_to_odp_closes_target_on_failure(monkeypatch, tmp_path: Path, failure_mode: str) -> None:
    from plugin.framework import scratch_writer
    from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_slide_to_odp

    class _FakeDoc:
        def __init__(self, uid: str, *, store_raises: bool = False) -> None:
            self._uid = uid
            self._store_raises = store_raises
            self.closed = False

        def getRuntimeUID(self) -> str:
            return self._uid

        def getDrawPages(self) -> Any:
            return SimpleNamespace(getByIndex=lambda _i: "page-0")

        def storeToURL(self, _url: str, _args: tuple[Any, ...]) -> None:
            if self._store_raises:
                raise RuntimeError("store failed")

        def close(self, _deliver_ownership: bool) -> None:
            self.closed = True

    target_doc = _FakeDoc("target-uid-fail", store_raises=(failure_mode == "store_raises"))
    source_doc = _FakeDoc("source-uid-fail")
    desktop = SimpleNamespace(loadComponentFromURL=lambda *_args: target_doc)

    monkeypatch.setattr(uno_pptx_import, "get_desktop", lambda _ctx: desktop)
    monkeypatch.setattr(uno_pptx_import, "load_pptx_as_impress_doc", lambda _ctx, _path: source_doc)
    monkeypatch.setattr(
        scratch_writer,
        "create_property_value",
        lambda name, value: SimpleNamespace(Name=name, Value=value),
    )

    def fake_import(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if failure_mode == "copy_raises":
            raise RuntimeError("copy boom")
        if failure_mode == "copy_error":
            return {"status": "error", "message": "no shapes"}
        return {"status": "ok"}

    monkeypatch.setattr(uno_pptx_import, "_import_slides_from_source", fake_import)

    odp_path = tmp_path / "out.odp"
    if failure_mode == "copy_error":
        assert import_pptx_slide_to_odp(MagicMock(), tmp_path / "in.pptx", 0, odp_path) is None
    else:
        with pytest.raises(RuntimeError):
            import_pptx_slide_to_odp(MagicMock(), tmp_path / "in.pptx", 0, odp_path)

    assert target_doc.closed is True
    assert source_doc.closed is True
    assert scratch_writer.is_scratch_uid("target-uid-fail") is False


def test_import_pptx_slide_to_odp_returns_open_registered_target(monkeypatch, tmp_path: Path) -> None:
    from plugin.framework import scratch_writer
    from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_slide_to_odp

    class _FakeDoc:
        def __init__(self, uid: str) -> None:
            self._uid = uid
            self.closed = False

        def getRuntimeUID(self) -> str:
            return self._uid

        def getDrawPages(self) -> Any:
            return SimpleNamespace(getByIndex=lambda _i: "page-0")

        def storeToURL(self, _url: str, _args: tuple[Any, ...]) -> None:
            return None

        def close(self, _deliver_ownership: bool) -> None:
            self.closed = True

    target_doc = _FakeDoc("target-uid-ok")
    source_doc = _FakeDoc("source-uid-ok")
    desktop = SimpleNamespace(loadComponentFromURL=lambda *_args: target_doc)

    monkeypatch.setattr(uno_pptx_import, "get_desktop", lambda _ctx: desktop)
    monkeypatch.setattr(uno_pptx_import, "load_pptx_as_impress_doc", lambda _ctx, _path: source_doc)
    monkeypatch.setattr(
        scratch_writer,
        "create_property_value",
        lambda name, value: SimpleNamespace(Name=name, Value=value),
    )
    monkeypatch.setattr(uno_pptx_import, "_import_slides_from_source", lambda *_a, **_kw: {"status": "ok"})

    imported = import_pptx_slide_to_odp(MagicMock(), tmp_path / "in.pptx", 0, tmp_path / "out.odp")
    assert imported is not None
    returned_doc, returned_page = imported
    try:
        assert returned_doc is target_doc
        assert returned_page == "page-0"
        assert target_doc.closed is False
        assert source_doc.closed is True
        assert scratch_writer.is_scratch_uid("target-uid-ok") is True
    finally:
        scratch_writer.close_scratch_document(returned_doc)
    assert scratch_writer.is_scratch_uid("target-uid-ok") is False
