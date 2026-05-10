# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for chart CLSID / Writer embed helpers (no UNO required).

from unittest.mock import MagicMock

from plugin.modules.calc.charts import (
    CHART_CLSID,
    CHART_CLSID_DRAW_OLE,
    _is_chart_clsid,
    _normalize_clsid_value,
    _writer_embed_is_chart,
)


def test_normalize_clsid_value_bytes():
    assert _normalize_clsid_value(b"abc") == "abc"
    assert isinstance(_normalize_clsid_value(CHART_CLSID), str)


def test_is_chart_clsid_braced_and_mixed_case():
    g = CHART_CLSID.upper()
    assert _is_chart_clsid("{" + g + "}")
    assert _is_chart_clsid(CHART_CLSID_DRAW_OLE)


def test_is_chart_clsid_rejects_empty():
    assert _is_chart_clsid("") is False
    assert _is_chart_clsid(None) is False


def test_writer_embed_is_chart_by_clsid():
    o = MagicMock()
    o.CLSID = CHART_CLSID
    assert _writer_embed_is_chart(o) is True


def test_writer_embed_is_chart_by_diagram():
    o = MagicMock(spec=["CLSID", "getEmbeddedObject"])
    o.CLSID = ""
    diagram = MagicMock()
    chart_doc = MagicMock()
    chart_doc.getDiagram.return_value = diagram
    o.getEmbeddedObject.return_value = chart_doc
    assert _writer_embed_is_chart(o) is True
