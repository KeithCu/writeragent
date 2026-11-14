# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Writer image insert (plain body, table cell, first-page letterhead)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.doc.visual_helpers import GENERATED_IMAGE_MAX_DISPLAY_MM, px_to_units
from plugin.writer.images.image_tools import (
    _should_link_image_path,
    insert_image,
    insert_image_at_locator,
    insert_image_into_header_footer,
)
from plugin.writer.page import _scan_region_content


def _logo_path() -> str:
    # Walk up from tests/writer/images/ so this works in the checkout
    # (extension/assets) and in a remapped release tree (assets/).
    here = os.path.abspath(__file__)
    directory = os.path.dirname(here)
    for _unused in range(6):
        for rel in ("extension/assets/logo_32.png", "assets/logo_32.png"):
            path = os.path.join(directory, *rel.split("/"))
            if os.path.isfile(path):
                return path
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return os.path.join(directory, "extension", "assets", "logo_32.png")


def _standard_style(doc):
    return doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")


def _graphic_count(doc) -> int:
    return len(doc.getGraphicObjects().getElementNames())


def _assert_graphic_in_first_not_shared(doc, style, first_prop, shared_prop, graphic):
    first = style.getPropertyValue(first_prop)
    shared = style.getPropertyValue(shared_prop)
    first_scan = _scan_region_content(doc, first)
    shared_scan = _scan_region_content(doc, shared)
    assert first_scan["images"], "expected graphic in %s, scan=%r" % (first_prop, first_scan)
    assert not shared_scan["images"], (
        "graphic leaked into shared %s: first=%r shared=%r" % (shared_prop, first_scan, shared_scan)
    )
    assert graphic.getPropertyValue("AnchorType") == AS_CHARACTER


@native_test
@with_native_doc("writer")
def test_insert_image_header_first_not_shared_header(ctx, doc):
    """FirstIsShared=False: header_first writes HeaderTextFirst, not HeaderText.

    Shared HeaderText never reaches a different-first-page letterhead, so a
    logo inserted with target=header would repeat on every page and miss page 1.
    """
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    style = _standard_style(doc)
    style.setPropertyValue("HeaderIsOn", True)
    style.setPropertyValue("FirstIsShared", False)
    placed = insert_image_into_header_footer(
        doc, logo, "header_first", width_mm=20, height_mm=20, ctx=ctx,
    )
    assert placed["graphic"] is not None
    assert placed["region"] == "header_first"
    assert placed["auto_height"] is True
    assert style.getPropertyValue("HeaderIsDynamicHeight") is True
    _assert_graphic_in_first_not_shared(
        doc, style, "HeaderTextFirst", "HeaderText", placed["graphic"],
    )


@native_test
@with_native_doc("writer")
def test_insert_image_footer_first_not_shared_footer(ctx, doc):
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    style = _standard_style(doc)
    style.setPropertyValue("FooterIsOn", True)
    style.setPropertyValue("FirstIsShared", False)
    placed = insert_image_into_header_footer(
        doc, logo, "footer_first", width_mm=20, height_mm=20, ctx=ctx,
    )
    assert placed["graphic"] is not None
    assert placed["region"] == "footer_first"
    assert style.getPropertyValue("FooterIsDynamicHeight") is True
    _assert_graphic_in_first_not_shared(
        doc, style, "FooterTextFirst", "FooterText", placed["graphic"],
    )


@native_test
@with_native_doc("writer")
def test_insert_image_plain_writer_body(ctx, doc):
    """Regression: clone_text_range(ViewCursor) raises RuntimeException on body.

    PR #796 switched body insert to createTextCursorByRange(view_cursor).
    That fails on a plain Writer body with a bare UNO RuntimeException that
    execute_safe used to map to DOCUMENT_DISPOSED. Body insert must use
    getStart() and land a graphic on a still-live document.
    """
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    before = _graphic_count(doc)
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoStart(False)
    insert_image(ctx, doc, logo, 64, 64, add_to_gallery=False, add_frame=False)
    assert _graphic_count(doc) == before + 1
    assert doc.getImplementationName() == "SwXTextDocument"
    assert vc.getPropertyValue("TextTable") is None


@native_test
@with_native_doc("writer")
def test_insert_image_in_table_cell(ctx, doc):
    """#796 nested XText: insert at a cell view cursor must still land a graphic."""
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.getEnd(), tbl, False)
    cell = tbl.getCellByName("A1")
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(cell, False)
    before = _graphic_count(doc)
    insert_image(ctx, doc, logo, 64, 64, add_to_gallery=False, add_frame=False)
    assert _graphic_count(doc) == before + 1
    restored = vc.getPropertyValue("TextTable")
    assert restored is not None
    assert restored.getName() == tbl.getName()


@native_test
@with_native_doc("writer")
def test_insert_image_compound_undo(ctx, doc):
    """Frame insert is multi-step UNO; one undo must remove the whole insert."""
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    before = _graphic_count(doc)
    insert_image(ctx, doc, logo, 64, 64, add_to_gallery=False, add_frame=True)
    assert _graphic_count(doc) == before + 1

    um = doc.getUndoManager()
    if um is None:
        return
    undo_enabled = False
    try:
        undo_enabled = um.isUndoEnabled()
    except Exception:
        try:
            undo_enabled = um.isUndoPossible()
        except Exception:
            pass
    if not undo_enabled:
        return
    um.undo()
    assert _graphic_count(doc) == before, "single undo should remove framed insert"


@native_test
@with_native_doc("writer")
def test_insert_image_caps_1024_display_size(ctx, doc):
    """1024px at 96 DPI is ~10.7\"; insert must stay at the 135mm longer-edge cap."""
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    raw_w, raw_h = px_to_units(1024, 1024)
    assert max(raw_w, raw_h) > GENERATED_IMAGE_MAX_DISPLAY_MM * 100
    before = set(doc.getGraphicObjects().getElementNames())
    insert_image(ctx, doc, logo, 1024, 1024, add_to_gallery=False, add_frame=False)
    names = [n for n in doc.getGraphicObjects().getElementNames() if n not in before]
    assert names, "expected an inserted graphic"
    graphic = doc.getGraphicObjects().getByName(names[0])
    width = int(graphic.getPropertyValue("Width"))
    height = int(graphic.getPropertyValue("Height"))
    # LO can report cap+1 after setSize (13500 set → Width 13501).
    assert max(width, height) <= GENERATED_IMAGE_MAX_DISPLAY_MM * 100 + 2
    assert max(width, height) >= 13000
    assert max(width, height) < min(raw_w, raw_h)


@native_test
@with_native_doc("writer")
def test_image_get_info_reads_hyperlink_url(ctx, doc):
    """draw:a around a graphic is HyperLinkURL. image_get_info must return it."""
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    before = set(doc.getGraphicObjects().getElementNames())
    insert_image(ctx, doc, logo, 64, 64, add_to_gallery=False, add_frame=False)
    names = [n for n in doc.getGraphicObjects().getElementNames() if n not in before]
    assert names, "expected an inserted graphic"
    graphic = doc.getGraphicObjects().getByName(names[0])
    bare = TestingFactory.execute_tool(doc, ctx, "image_get_info", {"name": names[0]}, doc_type="writer")
    assert bare.get("status") == "ok", bare
    assert bare.get("hyperlink_url") == ""
    graphic.setPropertyValue("HyperLinkURL", "#ÍNDEX")
    linked = TestingFactory.execute_tool(doc, ctx, "image_get_info", {"name": names[0]}, doc_type="writer")
    assert linked.get("status") == "ok", linked
    assert linked.get("hyperlink_url") == "#ÍNDEX"


@native_test
@with_native_doc("writer")
def test_writer_graphic_object_get_size_uno(ctx, doc):
    """XShape.getSize() on a Writer GraphicObjectShape returns its Size (REPORT37 I2)."""
    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    before = set(doc.getGraphicObjects().getElementNames())
    insert_image(ctx, doc, logo, 64, 64, add_to_gallery=False, add_frame=False)
    names = [n for n in doc.getGraphicObjects().getElementNames() if n not in before]
    assert names, "expected an inserted graphic"
    graphic = doc.getGraphicObjects().getByName(names[0])
    try:
        size = graphic.getSize()
        assert size.Width > 0
        assert size.Height > 0
    finally:
        # Pooled doc: do not leave the image for later tests.
        graphic.dispose()


@native_test
@with_native_doc("calc")
def test_calc_insert_image_anchors_to_selection_top_left_uno(ctx, doc):
    """Embedded Calc picture insert anchors to selection top-left, moves on row insert, and uses A1 on another sheet."""
    import shutil
    import tempfile

    sheet0 = doc.getSheets().getByIndex(0)
    draw_page0 = sheet0.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet0)

    tmp_dir = tempfile.mkdtemp(prefix="wa_calc_img_")
    tmp_png = os.path.join(tmp_dir, "logo.png")
    shutil.copyfile(_logo_path(), tmp_png)
    inserted_row = False
    added_sheet = False
    try:
        c5_e7 = sheet0.getCellRangeByPosition(2, 4, 4, 6)
        controller.select(c5_e7)
        before = draw_page0.getCount()
        insert_image_at_locator(ctx, doc, tmp_png, width_mm=30, height_mm=20)
        assert draw_page0.getCount() == before + 1
        shape = draw_page0.getByIndex(before)

        c5 = sheet0.getCellByPosition(2, 4)
        anchor = shape.getPropertyValue("Anchor").getCellAddress()
        assert (anchor.Column, anchor.Row) == (2, 4)
        assert (shape.getPosition().X, shape.getPosition().Y) == (c5.Position.X, c5.Position.Y)
        assert (shape.getSize().Width, shape.getSize().Height) == (3000, 2000)
        assert shape.getPropertyValue("ResizeWithCell") is False
        assert shape.getName().startswith("Picture ")

        sheet0.getRows().insertByIndex(0, 1)
        inserted_row = True
        c6 = sheet0.getCellByPosition(2, 5)
        assert shape.getPosition().Y == c6.Position.Y
        assert (shape.getSize().Width, shape.getSize().Height) == (3000, 2000)

        doc.getSheets().insertNewByName("SecondSheet", 1)
        added_sheet = True
        sheet1 = doc.getSheets().getByIndex(1)
        draw_page1 = sheet1.getDrawPage()
        insert_image_at_locator(ctx, doc, tmp_png, width_mm=30, height_mm=20, page_index=1)
        assert draw_page1.getCount() == 1
        shape1 = draw_page1.getByIndex(0)
        anchor1 = shape1.getPropertyValue("Anchor").getCellAddress()
        assert (anchor1.Sheet, anchor1.Column, anchor1.Row) == (1, 0, 0)
        assert shape1.getName().startswith("Picture ")
        assert shape1.getName() != shape.getName()
    finally:
        while draw_page0.getCount():
            draw_page0.remove(draw_page0.getByIndex(0))
        if added_sheet:
            doc.getSheets().removeByName("SecondSheet")
        if inserted_row:
            sheet0.getRows().removeByIndex(0, 1)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _linked_logo_for_test(logo: str) -> tuple[str, str | None]:
    """Return ``(path, cleanup_dir)`` for a copy of *logo* outside ``tempfile.gettempdir()``.

    ``_should_link_image_path`` embeds files under ``tempfile.gettempdir()``, so
    when the checkout itself lives under ``/tmp`` (as in ``make release``), the
    test must copy the fixture into a writable directory outside tempdir.
    """
    if _should_link_image_path(logo):
        return logo, None

    candidates: list[str] = []
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        candidates.append(xdg_cache)
    home = os.path.expanduser("~")
    if home and home != "~":
        candidates.append(os.path.join(home, ".cache"))
        candidates.append(home)

    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(prefix="wa_linked_img_", dir=base)
        except OSError:
            continue
        dst = os.path.join(tmp_dir, os.path.basename(logo))
        try:
            shutil.copyfile(logo, dst)
            if _should_link_image_path(dst):
                return dst, tmp_dir
        except OSError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    raise unittest.SkipTest("No writable directory outside tempfile.gettempdir() for linked-image test")


@native_test
@with_native_doc("calc")
def test_calc_insert_linked_image_is_one_anchored_picture_uno(ctx, doc):
    """Linked Calc insert creates one linked shape at requested size, anchored to the selected cell."""
    import uno

    logo = _logo_path()
    assert os.path.isfile(logo), "fixture image missing: %s" % logo
    linked_logo, cleanup_dir = _linked_logo_for_test(logo)
    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    c5 = sheet.getCellByPosition(2, 4)
    controller.select(c5)

    before = draw_page.getCount()
    try:
        insert_image_at_locator(ctx, doc, linked_logo, width_mm=30, height_mm=20)
        assert draw_page.getCount() == before + 1
        shape = draw_page.getByIndex(before)
        anchor = shape.getPropertyValue("Anchor").getCellAddress()
        assert (anchor.Column, anchor.Row) == (2, 4)
        assert (shape.getSize().Width, shape.getSize().Height) == (3000, 2000)
        expected_url = uno.systemPathToFileUrl(os.path.abspath(linked_logo))
        assert shape.getPropertyValue("Graphic").OriginURL == expected_url
    finally:
        while draw_page.getCount() > before:
            draw_page.remove(draw_page.getByIndex(draw_page.getCount() - 1))
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


@native_test
@with_native_doc("calc")
def test_calc_insert_image_rtl_right_edge_on_cell_uno(ctx, doc):
    """On an RTL sheet (TableLayout=1), inserted picture's right edge aligns with the cell's right edge."""
    import shutil
    import tempfile

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    orig_layout = sheet.TableLayout

    tmp_dir = tempfile.mkdtemp(prefix="wa_calc_rtl_")
    tmp_png = os.path.join(tmp_dir, "logo.png")
    shutil.copyfile(_logo_path(), tmp_png)
    before = draw_page.getCount()
    try:
        sheet.TableLayout = 1
        c5 = sheet.getCellByPosition(2, 4)
        controller.select(c5)
        insert_image_at_locator(ctx, doc, tmp_png, width_mm=30, height_mm=20)
        assert draw_page.getCount() == before + 1
        shape = draw_page.getByIndex(before)
        pos = shape.getPosition()
        width = shape.getSize().Width
        assert pos.X + width == c5.Position.X + c5.Size.Width
    finally:
        sheet.TableLayout = orig_layout
        while draw_page.getCount() > before:
            draw_page.remove(draw_page.getByIndex(draw_page.getCount() - 1))
        shutil.rmtree(tmp_dir, ignore_errors=True)

