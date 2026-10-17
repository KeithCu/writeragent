# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw/Impress master slide tools."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.draw.masters import ListMasterSlides


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
