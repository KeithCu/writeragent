# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: ImageList preserves the view cursor position."""

from __future__ import annotations

import os

from plugin.doc.text_helpers import clone_text_range
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.images.image_tools import insert_image_at_locator
from plugin.writer.images.images import ImageList


def _logo_path() -> str:
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


@native_test
@with_native_doc("writer")
def test_image_list_preserves_view_cursor(ctx, doc):
    logo = _logo_path()
    assert os.path.isfile(logo), f"fixture image missing: {logo}"

    text = doc.getText()
    text.setString("")
    cursor = text.createTextCursor()
    text.insertString(cursor, "First paragraph\n", False)
    text.insertString(cursor, "Second paragraph to park on\n", False)
    text.insertString(cursor, "Third paragraph\n", False)

    # Insert two graphics
    g1 = insert_image_at_locator(ctx, doc, logo, width_mm=15, height_mm=15)
    g2 = insert_image_at_locator(ctx, doc, logo, width_mm=15, height_mm=15)
    assert g1 is not None and g2 is not None

    # Park view cursor on the second paragraph
    search = doc.createSearchDescriptor()
    search.setSearchString("Second paragraph to park on")
    found = doc.findFirst(search)
    assert found is not None

    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(found.getStart(), False)
    saved_pos = clone_text_range(vc)

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ImageList().execute(tctx)
    assert res.get("status") == "ok"
    assert len(res.get("images", [])) >= 2

    # View cursor must remain on the parked paragraph
    assert text.compareRegionStarts(vc.getStart(), saved_pos.getStart()) == 0
    doc_svc = tctx.services.document
    para_ranges = doc_svc.get_paragraph_ranges(doc)
    p_idx_vc = doc_svc.find_paragraph_for_range(vc, para_ranges, text)
    p_idx_saved = doc_svc.find_paragraph_for_range(saved_pos, para_ranges, text)
    assert p_idx_vc == p_idx_saved
