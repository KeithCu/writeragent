# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for Impress header/footer master resolution (no LibreOffice required)."""

from unittest.mock import MagicMock

from plugin.modules.draw.headers_footers import _get_page


def test_get_page_master_uses_slides_master_page_not_master_list_index() -> None:
    """Impress getMasterPages() order is not the same as slide.MasterPage; use the latter."""
    slide = MagicMock()
    master = MagicMock()
    slide.MasterPage = master

    wrong_master = MagicMock()
    masters_coll = MagicMock()
    masters_coll.getByIndex.return_value = wrong_master

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = slide

    doc = MagicMock()
    doc.getDrawPages.return_value = pages
    doc.getMasterPages.return_value = masters_coll

    ctx = MagicMock()
    ctx.doc = doc

    resolved = _get_page(ctx, 0, True)
    assert resolved is master
    pages.getByIndex.assert_called_once_with(0)
    masters_coll.getByIndex.assert_not_called()


def test_get_page_slide_returns_slide() -> None:
    slide = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = slide
    doc = MagicMock()
    doc.getDrawPages.return_value = pages
    ctx = MagicMock()
    ctx.doc = doc

    assert _get_page(ctx, 0, False) is slide
