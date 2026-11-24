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

from plugin.framework.scratch_writer import close_scratch_document
from plugin.framework.uno_context import create_property_value, get_desktop, get_runtime_uid, resolve_document_by_url
from plugin.ppt_master.adapter.uno_pptx_import import import_pptx_to_doc, load_pptx_as_impress_doc
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
