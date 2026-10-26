# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw/Impress master slide tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.draw.masters import GetSlideMaster, ListMasterSlides, SetSlideMaster
from plugin.framework.errors import DocumentDisposedError


def test_list_master_slides():
    m0 = MagicMock()
    m0.Name = "DefaultMaster"
    m0.Width = 21000
    m0.Height = 29700

    m1 = MagicMock(spec=["Width", "Height"])
    m1.Width = 28000
    m1.Height = 21000

    masters = MagicMock()
    masters.getCount.return_value = 2
    masters.getByIndex.side_effect = lambda idx: [m0, m1][idx]

    doc = MagicMock()
    doc.getMasterPages.return_value = masters

    ctx = MagicMock()
    ctx.doc = doc

    tool = ListMasterSlides()
    res = tool.execute(ctx)

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert res["master_slides"] == [
        {"index": 0, "name": "DefaultMaster", "width_mm": 210, "height_mm": 297},
        {"index": 1, "name": "", "width_mm": 280, "height_mm": 210},
    ]


def test_inherited_uno_services():
    from plugin.draw.masters import GetSlideMaster, ListMasterSlides, SetSlideMaster, ToolDrawSlideMastersBase
    from plugin.draw.notes import GetSpeakerNotes, SetSpeakerNotes, ToolDrawSpeakerNotesBase
    from plugin.draw.transitions import (
        GetSlideLayout,
        GetSlideTransition,
        SetSlideLayout,
        SetSlideTransition,
        ToolDrawSlideLayoutBase,
        ToolDrawSlideTransitionsBase,
    )

    for cls, base in [
        (ListMasterSlides, ToolDrawSlideMastersBase),
        (GetSlideMaster, ToolDrawSlideMastersBase),
        (SetSlideMaster, ToolDrawSlideMastersBase),
        (GetSpeakerNotes, ToolDrawSpeakerNotesBase),
        (SetSpeakerNotes, ToolDrawSpeakerNotesBase),
        (GetSlideTransition, ToolDrawSlideTransitionsBase),
        (SetSlideTransition, ToolDrawSlideTransitionsBase),
        (GetSlideLayout, ToolDrawSlideLayoutBase),
        (SetSlideLayout, ToolDrawSlideLayoutBase),
    ]:
        assert cls.uno_services == base.uno_services


class _Masters:
    def __init__(self, items):
        self.items = items

    def getCount(self):
        return len(self.items)

    def getByIndex(self, i):
        return self.items[i]


class _Doc:
    def __init__(self, masters, pages):
        self._m, self._p = masters, pages

    def getMasterPages(self):
        return self._m

    def getDrawPages(self):
        return self._p


class _Master:
    def __init__(self, name):
        self.Name = name
        self.Width = 21000
        self.Height = 29700


class _Page:
    def __init__(self, master):
        self.MasterPage = master


def test_get_slide_master_explicit_page():
    m0 = _Master("Default")
    m1 = _Master("Sunset")
    p0 = _Page(m0)
    p1 = _Page(m1)
    doc = _Doc(_Masters([m0, m1]), _Masters([p0, p1]))
    ctx = SimpleNamespace(doc=doc, active_page_index=0)
    res = GetSlideMaster().execute(ctx, page=0)
    assert res["status"] == "ok"
    assert res["page"] == 0
    assert res["master"] == "Default"


def test_get_slide_master_omitted_page_uses_active_page_index():
    m0 = _Master("Default")
    m1 = _Master("Sunset")
    p0 = _Page(m0)
    p1 = _Page(m1)
    doc = _Doc(_Masters([m0, m1]), _Masters([p0, p1]))
    ctx = SimpleNamespace(doc=doc, active_page_index=1)
    res = GetSlideMaster().execute(ctx)
    assert res["status"] == "ok"
    assert res["page"] == 1
    assert res["master"] == "Sunset"


def test_get_slide_master_invalid_page_returns_error():
    m0 = _Master("Default")
    p0 = _Page(m0)
    doc = _Doc(_Masters([m0]), _Masters([p0]))
    ctx = SimpleNamespace(doc=doc, active_page_index=0)
    res = GetSlideMaster().execute(ctx, page=5)
    assert res["status"] == "error"
    assert "Invalid page index: 5" in res["message"]


def test_set_slide_master_success():
    m0 = _Master("Default")
    m1 = _Master("Sunset")
    p0 = _Page(m0)
    p1 = _Page(m0)
    pages = [p0, p1]
    doc = _Doc(_Masters([m0, m1]), _Masters(pages))
    ctx = SimpleNamespace(doc=doc, active_page_index=0)
    res = SetSlideMaster().execute(ctx, page=1, master="Sunset")
    assert res["status"] == "ok"
    assert res["page"] == 1
    assert res["master"] == "Sunset"
    assert pages[1].MasterPage is m1


def test_set_slide_master_empty_master_validates_before_uno():
    class _FailingDoc:
        def getMasterPages(self):
            raise AssertionError("getMasterPages should not be called")

        def getDrawPages(self):
            raise AssertionError("getDrawPages should not be called")

    ctx = SimpleNamespace(doc=_FailingDoc(), active_page_index=0)
    res = SetSlideMaster().execute(ctx, master="  ", page=99)
    assert res["status"] == "error"
    assert res["message"] == "master is required."


def test_set_slide_master_unknown_name_lists_available():
    m0 = _Master("Default")
    m1 = _Master("Sunset")
    p0 = _Page(m0)
    doc = _Doc(_Masters([m0, m1]), _Masters([p0]))
    ctx = SimpleNamespace(doc=doc, active_page_index=0)
    res = SetSlideMaster().execute(ctx, page=0, master="Unknown")
    assert res["status"] == "error"
    assert res["details"]["available"] == ["Default", "Sunset"]


def test_set_slide_master_disposal_propagates():
    class _DisposedPage:
        @property
        def MasterPage(self):
            return None

        @MasterPage.setter
        def MasterPage(self, val):
            raise DocumentDisposedError("disposed")

    m0 = _Master("Default")
    doc = _Doc(_Masters([m0]), _Masters([_DisposedPage()]))
    ctx = SimpleNamespace(doc=doc, active_page_index=0)
    with pytest.raises(DocumentDisposedError):
        SetSlideMaster().execute(ctx, page=0, master="Default")

