# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: first-page letterhead logos land in HeaderTextFirst / FooterTextFirst."""

from __future__ import annotations

import os

from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.doc.visual_helpers import GENERATED_IMAGE_MAX_DISPLAY_MM, px_to_units
from plugin.writer.images.image_tools import insert_image, insert_image_into_header_footer
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
