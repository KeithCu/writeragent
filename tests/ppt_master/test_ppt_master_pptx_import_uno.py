# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from com.sun.star.awt import Point, Size

from plugin.draw import shape_clone
from plugin.framework.scratch_writer import close_scratch_document, is_scratch_document
from plugin.framework.uno_context import create_property_value, enum_has_more, get_desktop, get_runtime_uid, resolve_document_by_url, uno_same
from plugin.ppt_master.adapter import uno_pptx_import
from plugin.ppt_master.adapter.uno_pptx_import import (
    _import_slides_from_source,
    import_pptx_slide_to_odp,
    import_pptx_to_doc,
    load_pptx_as_impress_doc,
)
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _add_text_slide(doc, index: int) -> None:
    pages = doc.getDrawPages()
    if index > 0:
        pages.insertNewByIndex(index)
    page = pages.getByIndex(index)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    page.add(shape)
    shape.setPosition(Point(1000, 1000))
    shape.setSize(Size(10000, 2000))
    shape.setString(f"Slide {index}")


def _build_deck(ctx, path: Path, slides: int) -> None:
    desktop = get_desktop(ctx)
    doc = desktop.loadComponentFromURL(
        "private:factory/simpress",
        "_blank",
        0,
        (create_property_value("Hidden", True),),
    )
    try:
        for i in range(slides):
            _add_text_slide(doc, i)
        doc.storeToURL(
            path.resolve().as_uri(),
            (create_property_value("FilterName", "Impress MS PowerPoint 2007 XML"),),
        )
    finally:
        doc.close(True)


@native_test
@with_native_doc("impress")
def test_lo_import_minimal_pptx_multi_slide(ctx, doc):
    if sys.platform == "win32":
        # Leftover hidden Impress loads hang on Windows.
        raise unittest.SkipTest("skipped on win32")
    with tempfile.TemporaryDirectory(prefix="wa_pptx_import_") as tmp_dir:
        tmp = Path(tmp_dir)
        pptx_path = tmp / "deck.pptx"
        mirror_path = tmp / "mirror.odp"
        _build_deck(ctx, pptx_path, 3)

        result = import_pptx_to_doc(ctx, doc, pptx_path, clear_existing=True, save_mirror_odp=mirror_path)
        assert result.get("status") == "ok", result
        assert result.get("slides") == 3
        assert result.get("route") == "pptx_to_odp"
        pages = doc.getDrawPages()
        assert pages.getCount() >= 3
        for i in range(3):
            page = pages.getByIndex(i)
            texts = [str(getattr(page.getByIndex(j), "String", "") or "") for j in range(page.getCount())]
            assert f"Slide {i}" in texts, (i, texts)
        with zipfile.ZipFile(mirror_path) as zf:
            assert zf.read("mimetype") == b"application/vnd.oasis.opendocument.presentation"


@native_test
def test_pptx_source_load_keeps_user_lock_and_is_not_adopted_uno(ctx):
    if sys.platform == "win32":
        # Leftover hidden Impress loads hang on Windows.
        raise unittest.SkipTest("skipped on win32")
    with tempfile.TemporaryDirectory(prefix="wa_pptx_lock_") as tmp_dir:
        tmp = Path(tmp_dir)
        pptx_path = tmp / "deck.pptx"
        lock_path = tmp / ".~lock.deck.pptx#"
        _build_deck(ctx, pptx_path, 1)

        desktop = get_desktop(ctx)
        user_doc = desktop.loadComponentFromURL(
            pptx_path.resolve().as_uri(),
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        copy = None
        try:
            assert lock_path.is_file(), "expected LibreOffice lock file for open pptx"
            user_uid = get_runtime_uid(user_doc)

            copy = load_pptx_as_impress_doc(ctx, pptx_path)
            assert copy is not None
            assert copy.getURL() == ""
            assert copy.isReadonly()
            resolved_doc, _doc_type = resolve_document_by_url(ctx, pptx_path.resolve().as_uri())
            assert resolved_doc is not None
            assert get_runtime_uid(resolved_doc) == user_uid

            close_scratch_document(copy)
            copy = None
            assert lock_path.is_file(), "closing read-only import copy removed user lock file"
        finally:
            if copy is not None:
                close_scratch_document(copy)
            user_doc.close(True)

        copy2 = load_pptx_as_impress_doc(ctx, pptx_path)
        assert copy2 is not None
        try:
            assert resolve_document_by_url(ctx, pptx_path.resolve().as_uri()) == (None, None)
        finally:
            close_scratch_document(copy2)


def _desktop_component_count(desktop) -> int:
    enum = desktop.getComponents().createEnumeration()
    count = 0
    while enum_has_more(enum):
        enum.nextElement()
        count += 1
    return count


@native_test
@with_native_doc("impress")
def test_import_replaces_placeholders_and_keeps_imported_title_uno(ctx, doc):
    if sys.platform == "win32":
        # Leftover hidden Impress loads hang on Windows.
        raise unittest.SkipTest("skipped on win32")
    with tempfile.TemporaryDirectory(prefix="wa_pptx_zorder_") as tmp_dir:
        tmp = Path(tmp_dir)
        pptx_path = tmp / "deck.pptx"
        odp_path = tmp / "out.odp"
        desktop = get_desktop(ctx)

        src = desktop.loadComponentFromURL(
            "private:factory/simpress",
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        try:
            src_page = src.getDrawPages().getByIndex(0)
            src_page.getByIndex(0).setString("TITLE TEXT")
            body = src.createInstance("com.sun.star.drawing.TextShape")
            src_page.add(body)
            body.setPosition(Point(1000, 4000))
            body.setSize(Size(10000, 2000))
            body.setString("BODY")
            rect = src.createInstance("com.sun.star.drawing.RectangleShape")
            src_page.add(rect)
            rect.setPosition(Point(1000, 7000))
            rect.setSize(Size(10000, 2000))
            rect.setString("RECT")
            src_count = src_page.getCount()
            src.storeToURL(
                pptx_path.resolve().as_uri(),
                (create_property_value("FilterName", "Impress MS PowerPoint 2007 XML"),),
            )
        finally:
            src.close(True)

        target_page = doc.getDrawPages().getByIndex(0)
        target_page.getByIndex(0).setString("OLD")

        result = import_pptx_to_doc(ctx, doc, pptx_path, clear_existing=True)
        assert result.get("status") == "ok", result
        doc.storeToURL(odp_path.resolve().as_uri(), ())

        reopened = desktop.loadComponentFromURL(
            odp_path.resolve().as_uri(),
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        try:
            rpage = reopened.getDrawPages().getByIndex(0)
            assert rpage.getCount() == src_count
            texts = [str(getattr(rpage.getByIndex(i), "String", "") or "") for i in range(rpage.getCount())]
            non_empty = [t for t in texts if t]
            assert non_empty == ["TITLE TEXT", "BODY", "RECT"], texts
            assert "OLD" not in texts
        finally:
            reopened.close(True)


@native_test
@with_native_doc("impress")
def test_failed_copy_keeps_target_shapes_uno(ctx, doc):
    if sys.platform == "win32":
        # Leftover hidden Impress loads hang on Windows.
        raise unittest.SkipTest("skipped on win32")
    with tempfile.TemporaryDirectory(prefix="wa_pptx_fail_keep_") as tmp_dir:
        tmp = Path(tmp_dir)
        pptx_path = tmp / "deck.pptx"
        desktop = get_desktop(ctx)

        src = desktop.loadComponentFromURL(
            "private:factory/simpress",
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        try:
            src_page = src.getDrawPages().getByIndex(0)
            while src_page.getCount() > 0:
                src_page.remove(src_page.getByIndex(0))
            shape = src.createInstance("com.sun.star.drawing.TextShape")
            src_page.add(shape)
            shape.setPosition(Point(1000, 1000))
            shape.setSize(Size(10000, 2000))
            shape.setString("NEW")
            src.storeToURL(
                pptx_path.resolve().as_uri(),
                (create_property_value("FilterName", "Impress MS PowerPoint 2007 XML"),),
            )
        finally:
            src.close(True)

        target_page = doc.getDrawPages().getByIndex(0)
        target_page.getByIndex(0).setString("USER TITLE")
        original_shapes = [target_page.getByIndex(i) for i in range(target_page.getCount())]

        source_doc = load_pptx_as_impress_doc(ctx, pptx_path)
        assert source_doc is not None
        orig_copy_prop = shape_clone.copy_prop

        def fail_size(dst_shape, src_shape, prop_name):
            if prop_name == "Size":
                raise RuntimeError("forced Size failure")
            return orig_copy_prop(dst_shape, src_shape, prop_name)

        shape_clone.copy_prop = fail_size
        try:
            result = _import_slides_from_source(ctx, doc, source_doc, clear_existing=True)
        finally:
            shape_clone.copy_prop = orig_copy_prop
            close_scratch_document(source_doc)

        assert result.get("status") == "error", result
        after_shapes = [target_page.getByIndex(i) for i in range(target_page.getCount())]
        assert len(after_shapes) == len(original_shapes)
        assert all(uno_same(after, orig) for after, orig in zip(after_shapes, original_shapes))
        assert target_page.getByIndex(0).getString() == "USER TITLE"


@native_test
def test_import_pptx_slide_to_odp_leaves_no_document_open_uno(ctx):
    if sys.platform == "win32":
        # Leftover hidden Impress loads hang on Windows.
        raise unittest.SkipTest("skipped on win32")
    with tempfile.TemporaryDirectory(prefix="wa_pptx_slide_leak_") as tmp_dir:
        tmp = Path(tmp_dir)
        pptx_path = tmp / "deck.pptx"
        odp_path = tmp / "out.odp"
        _build_deck(ctx, pptx_path, 1)

        desktop = get_desktop(ctx)
        before_count = _desktop_component_count(desktop)

        orig_import = uno_pptx_import._import_slides_from_source

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced import failure")

        uno_pptx_import._import_slides_from_source = boom
        try:
            raised = False
            try:
                import_pptx_slide_to_odp(ctx, pptx_path, 0, odp_path)
            except RuntimeError:
                raised = True
            assert raised
        finally:
            uno_pptx_import._import_slides_from_source = orig_import

        assert _desktop_component_count(desktop) == before_count

        imported = import_pptx_slide_to_odp(ctx, pptx_path, 0, odp_path)
        assert imported is not None
        target_doc, _page = imported
        try:
            assert is_scratch_document(target_doc)
            reopened = desktop.loadComponentFromURL(
                odp_path.resolve().as_uri(),
                "_blank",
                0,
                (create_property_value("Hidden", True),),
            )
            try:
                rpage = reopened.getDrawPages().getByIndex(0)
                texts = [str(getattr(rpage.getByIndex(i), "String", "") or "") for i in range(rpage.getCount())]
                assert "Slide 0" in texts, texts
            finally:
                reopened.close(True)
        finally:
            close_scratch_document(target_doc)

        assert _desktop_component_count(desktop) == before_count
