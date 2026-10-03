# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for chart CLSID / Writer embed helpers (no UNO required).

import logging
from unittest.mock import MagicMock, call, patch

from plugin.calc.charts import (
    CHART_CLSID,
    CHART_CLSID_DRAW_OLE,
    ListCharts,
    _apply_chart_styling,
    _await_writer_chart_document,
    _axis_title_shape_string,
    _is_chart_clsid,
    _normalize_clsid_value,
    _resolve_chart,
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


class _DisposedException(Exception):
    """Name matches the dispose heuristic without importing UNO exceptions."""


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _ByteSequenceValue:
    """Bridge-style ByteSequence whose payload is ``.Value`` (not ``bytes``)."""

    def __init__(self, value: bytes) -> None:
        self.Value = value

    def __str__(self) -> str:
        return "<ByteSequence instance>"


class _UnoByteSequence:
    """Matches LibreOffice ``uno.ByteSequence`` (payload on ``.value``)."""

    def __init__(self, value: bytes) -> None:
        self.value = value

    def __len__(self) -> int:
        return len(self.value)

    def __str__(self) -> str:
        return "<ByteSequence instance '%s'>" % (self.value,)


class _OleShape:
    def __init__(self, name: str, clsid: str = CHART_CLSID, *, name_error: BaseException | None = None) -> None:
        self._name = name
        self._name_error = name_error
        self.CLSID = clsid

    def getShapeType(self) -> str:
        return "com.sun.star.drawing.OLE2Shape"

    @property
    def Name(self) -> str:
        if self._name_error is not None:
            raise self._name_error
        return self._name


def _page(*shapes: object) -> MagicMock:
    page = MagicMock()
    page.getCount.return_value = len(shapes)
    page.getByIndex.side_effect = list(shapes)
    return page


def _supports(kind: str):
    def supports(obj: object, name: str) -> bool:
        if kind == "draw":
            return name == "com.sun.star.drawing.DrawingDocument"
        if kind == "writer":
            return name == "com.sun.star.text.TextDocument"
        return False

    return supports


def test_normalize_clsid_value_byte_sequence_value_attr():
    raw = _ByteSequenceValue(CHART_CLSID.encode("ascii"))
    assert _normalize_clsid_value(raw) == CHART_CLSID
    assert _is_chart_clsid(raw) is True
    assert _is_chart_clsid(str(raw)) is False


def test_normalize_clsid_value_uno_byte_sequence():
    raw = _UnoByteSequence(CHART_CLSID_DRAW_OLE.encode("ascii"))
    assert not isinstance(raw, (bytes, bytearray))
    assert _normalize_clsid_value(raw) == CHART_CLSID_DRAW_OLE
    assert _is_chart_clsid(raw) is True
    assert _is_chart_clsid(str(raw)) is False
    host = MagicMock()
    host.CLSID = raw
    assert _writer_embed_is_chart(host) is True


def test_axis_title_shape_string_missing_property_is_not_success():
    assert _axis_title_shape_string(None, "Title") is None
    assert _axis_title_shape_string(object(), "Title") is None
    assert _axis_title_shape_string(object(), None) is None


def test_axis_title_shape_string_reads_and_writes_when_present():
    class _Titled:
        def __init__(self) -> None:
            self.String = "old"

    shape = _Titled()
    assert _axis_title_shape_string(shape, "Sales") == "Sales"
    assert shape.String == "Sales"
    assert _axis_title_shape_string(shape, None) == "Sales"


def test_apply_axis_title_does_not_log_success_without_string(caplog):
    class _Diagram:
        HasXAxisTitle = True

        def getXAxisTitle(self) -> object:
            return object()

    class _Chart:
        def getDiagram(self) -> _Diagram:
            return _Diagram()

    caplog.set_level(logging.DEBUG, logger="writeragent.calc")
    _apply_chart_styling(_Chart(), x_axis_title="Sales")
    assert "Set X axis title" not in caplog.text


def test_resolve_chart_skips_disposed_shape_on_draw():
    dead = MagicMock()
    dead.getShapeType.side_effect = _DisposedException("gone")
    late = _OleShape("Chart_1")
    named_dead = _OleShape("ignored", name_error=_DisposedException("name gone"))
    page = _page(dead, named_dead, late)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("draw")):
        assert _resolve_chart(doc, "Chart_1") is late


def test_resolve_chart_draw_propagates_non_dispose_errors():
    bad = MagicMock()
    bad.getShapeType.side_effect = ValueError("bad shape")
    page = _page(bad, _OleShape("Chart_1"))
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("draw")):
        try:
            _resolve_chart(doc, "Chart_1")
        except ValueError as exc:
            assert str(exc) == "bad shape"
        else:
            raise AssertionError("ValueError should abort draw resolve")


def test_resolve_chart_writer_skips_disposed_shape():
    dead = MagicMock()
    dead.getShapeType.side_effect = _DisposedException("gone")
    late = _OleShape("Chart_9")
    page = _page(dead, late)
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc.getDrawPage.return_value = page
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        assert _resolve_chart(doc, "Chart_9") is late


def test_list_charts_skips_disposed_shape():
    dead = MagicMock()
    dead.getShapeType.side_effect = _DisposedException("gone")
    live = _OleShape("Chart_2")
    page = _page(dead, live)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("draw")):
        result = ListCharts().execute(ctx)
    assert result["status"] == "ok"
    assert [entry["name"] for entry in result["charts"]] == ["Chart_2"]


def test_list_charts_sees_byte_sequence_clsid():
    shape = _OleShape("Chart_b")
    shape.CLSID = _UnoByteSequence(CHART_CLSID.encode("ascii"))
    page = _page(shape)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("draw")):
        result = ListCharts().execute(ctx)
    assert [entry["name"] for entry in result["charts"]] == ["Chart_b"]


def test_series_color_skips_disposed_series():
    class _DisposedSeries:
        def getPropertySetInfo(self) -> object:
            raise _DisposedException("series gone")

    live = MagicMock()
    live.getPropertySetInfo.return_value.hasPropertyByName.return_value = True
    coord = MagicMock()
    coord.getChartTypes.return_value = [MagicMock(getDataSeries=MagicMock(return_value=[_DisposedSeries(), live]))]
    diag = MagicMock()
    diag.getCoordinateSystems.return_value = [coord]
    chart_doc = MagicMock()
    chart_doc.getFirstDiagram.return_value = diag

    _apply_chart_styling(chart_doc, colors=["red", "blue"])
    assert live.setPropertyValue.call_args_list == [
        call("Color", 0x0000FF),
        call("FillColor", 0x0000FF),
        call("LineColor", 0x0000FF),
    ]


def test_series_color_still_stops_on_other_errors():
    class _BadSeries:
        def getPropertySetInfo(self) -> object:
            raise ValueError("not dispose")

    live = MagicMock()
    coord = MagicMock()
    coord.getChartTypes.return_value = [MagicMock(getDataSeries=MagicMock(return_value=[_BadSeries(), live]))]
    diag = MagicMock()
    diag.getCoordinateSystems.return_value = [coord]
    chart_doc = MagicMock()
    chart_doc.getFirstDiagram.return_value = diag

    _apply_chart_styling(chart_doc, colors=["red"])
    live.setPropertyValue.assert_not_called()


def test_await_writer_chart_stops_when_idle_never_arrives(monkeypatch):
    clock = _Clock()
    pumps: list[float | None] = []
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    monkeypatch.setattr("plugin.calc.charts._chart_document_from_host", lambda host: None)

    def _pump(ctx: object, deadline: float | None = None) -> bool:
        pumps.append(deadline)
        return False

    monkeypatch.setattr("plugin.calc.charts._process_events", _pump)
    assert _await_writer_chart_document(object(), object(), timeout=5.0) is None
    assert len(pumps) == 1
    assert clock.sleeps == []


def test_await_writer_chart_respects_total_timeout(monkeypatch):
    clock = _Clock()
    pumps = {"n": 0}
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    monkeypatch.setattr("plugin.calc.charts._chart_document_from_host", lambda host: None)

    def _pump(ctx: object, deadline: float | None = None) -> bool:
        pumps["n"] += 1
        return True

    monkeypatch.setattr("plugin.calc.charts._process_events", _pump)
    assert _await_writer_chart_document(object(), object(), timeout=0.2) is None
    # 0.05 is not exact in binary, so the last poll can land just under the deadline.
    assert 100.2 <= clock.now < 100.25
    assert 4 <= pumps["n"] <= 5
    assert clock.sleeps
    assert all(step <= 0.05 for step in clock.sleeps)


def test_await_writer_chart_returns_ready_model_without_pump(monkeypatch):
    model = object()

    def _boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("idle pump should not run when the model is ready")

    monkeypatch.setattr("plugin.calc.charts._process_events", _boom)
    monkeypatch.setattr("plugin.calc.charts._chart_document_from_host", lambda host: model)
    assert _await_writer_chart_document(object(), object()) is model


def test_process_events_past_deadline_does_not_pump(monkeypatch):
    from plugin.calc.charts import _process_events

    called: list[int] = []
    monkeypatch.setattr("time.monotonic", lambda: 50.0)
    monkeypatch.setattr(
        "plugin.framework.uno_context.process_events_to_idle",
        lambda *args, **kwargs: called.append(1) or True,
    )
    assert _process_events(object(), deadline=40.0) is False
    assert called == []
