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
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from plugin.framework.errors import DocumentDisposedError

from plugin.calc.charts import (
    CHART_CLSID,
    CHART_CLSID_DRAW_OLE,
    DeleteChart,
    ListCharts,
    _apply_chart_styling,
    _await_writer_chart_document,
    _axis_title_shape_string,
    _chart_document_from_host,
    _is_chart_clsid,
    _is_chart_host,
    _normalize_clsid_value,
    _resolve_chart,
    _unique_chart_name,
    _writer_embed_is_chart,
    supportsService,
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


DisposedException = _DisposedException


class RuntimeException(Exception):
    pass


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
    def __init__(self, name: str, clsid: Any = CHART_CLSID, *, name_error: BaseException | None = None) -> None:
        self._name = name
        self._name_error = name_error
        self.CLSID: Any = clsid

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
    page.getByIndex.side_effect = lambda idx: shapes[idx]
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
        # Each real pump takes wall time; the wait itself must not sleep.
        clock.now += 0.05
        return True

    monkeypatch.setattr("plugin.calc.charts._process_events", _pump)
    assert _await_writer_chart_document(object(), object(), timeout=0.2) is None
    # 0.05 is not exact in binary, so the last pump can land just under the deadline.
    assert 100.2 <= clock.now < 100.25
    assert 4 <= pumps["n"] <= 5
    # No time.sleep on the main thread while SolarMutex is held.
    assert clock.sleeps == []


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


def test_list_charts_closed_document_is_disposal():
    doc = MagicMock()
    doc.getImplementationName.side_effect = _DisposedException("doc disposed")
    doc.supportsService.side_effect = _DisposedException("doc disposed")
    ctx = MagicMock()
    ctx.doc = doc
    with pytest.raises(DocumentDisposedError):
        ListCharts().execute(ctx)
    doc.supportsService.assert_not_called()


def test_list_charts_calc_per_sheet_error_is_explicit():
    doc = MagicMock()
    sheet0 = MagicMock()
    sheet0.getName.return_value = "Sheet1"
    charts0 = MagicMock()
    charts0.getElementNames.return_value = ("Chart_A",)
    chart_obj0 = MagicMock()
    charts0.getByName.return_value = chart_obj0
    sheet0.getCharts.return_value = charts0

    sheet1 = MagicMock()
    sheet1.getName.return_value = "Sheet2"
    sheet1.getCharts.side_effect = ValueError("bad charts")

    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheets.getByIndex.side_effect = [sheet0, sheet1]
    doc.getSheets.return_value = sheets

    ctx = MagicMock()
    ctx.doc = doc

    with (
        patch("plugin.calc.charts.supportsService", side_effect=lambda obj, name: name == "com.sun.star.sheet.SpreadsheetDocument"),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        result = ListCharts().execute(ctx)

    assert result["status"] == "ok"
    assert len(result["charts"]) == 1
    assert result["charts"][0]["name"] == "Chart_A"
    assert result["charts"][0]["sheet_name"] == "Sheet1"
    charts0.getByName.assert_called_once_with("Chart_A")
    assert len(result["errors"]) == 1
    assert "bad charts" in result["errors"][0]["message"]
    assert result["errors"][0]["sheet"] == "Sheet2"


def test_list_charts_draw_per_shape_error_keeps_later_chart():
    bad = MagicMock()
    bad.getShapeType.side_effect = ValueError("bad shape")
    live = _OleShape("Chart_2")
    page = _page(bad, live)
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
    assert len(result.get("errors", [])) == 1
    assert result["errors"][0]["index"] == 0
    assert result["errors"][0]["page"] == 0
    assert "bad shape" in result["errors"][0]["message"]


def test_list_charts_draw_closed_mid_scan_raises():
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    disposed = False

    def get_page(idx: int):
        nonlocal disposed
        disposed = True
        raise _DisposedException("page disposed mid-scan")

    pages.getByIndex.side_effect = get_page
    doc.getDrawPages.return_value = pages

    def get_impl():
        if disposed:
            raise _DisposedException("doc disposed mid-scan")
        return "DrawingDocument"

    doc.getImplementationName.side_effect = get_impl

    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("draw")):
        with pytest.raises(_DisposedException):
            ListCharts().execute(ctx)


def test_list_charts_writer_draw_page_error_keeps_embedded_chart():
    doc = MagicMock()
    embed = MagicMock()
    embed.getElementNames.return_value = ("Chart_E",)
    chart_obj = MagicMock()
    embed.getByName.return_value = chart_obj
    doc.getEmbeddedObjects.return_value = embed
    doc.getDrawPage.side_effect = ValueError("no page")
    ctx = MagicMock()
    ctx.doc = doc

    with (
        patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")),
        patch("plugin.calc.charts._writer_embed_is_chart", return_value=True),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        result = ListCharts().execute(ctx)

    assert result["status"] == "ok"
    assert [entry["name"] for entry in result["charts"]] == ["Chart_E"]
    assert len(result.get("errors", [])) == 1
    assert result["errors"][0]["source"] == "draw_page"
    assert "no page" in result["errors"][0]["message"]
def test_supports_service_disposal_propagates():
    doc = MagicMock()
    doc.supportsService.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument")

    doc.supportsService.side_effect = RuntimeException("no")
    assert supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument") is False


def test_chart_document_from_host_disposal_propagates():
    host = MagicMock()
    host.getEmbeddedObject.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _chart_document_from_host(host)

    host.getEmbeddedObject.side_effect = RuntimeException("no")
    host.Model = None
    host.Component = None
    host.Name = None
    assert _chart_document_from_host(host) is None


def test_resolve_chart_writer_draw_page_disposal_propagates():
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc.getDrawPage.side_effect = DisposedException("gone")
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        with pytest.raises(DisposedException):
            _resolve_chart(doc, "Chart_9")


def test_list_charts_writer_draw_page_disposal_propagates():
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc.getDrawPage.side_effect = DisposedException("gone")
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        with pytest.raises(DisposedException):
            ListCharts().execute(ctx)


def test_get_summary_disposal_propagates():
    obj = MagicMock()
    chart_doc = MagicMock()
    chart_doc.HasMainTitle = True
    chart_doc.getTitle.side_effect = DisposedException("gone")
    with patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc):
        with pytest.raises(DisposedException):
            ListCharts()._get_summary(obj, "Chart_1")

    chart_doc.getTitle.side_effect = RuntimeException("no title")
    with patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc):
        summary = ListCharts()._get_summary(obj, "Chart_1")
        assert summary == {"name": "Chart_1"}


def test_delete_chart_writer_draw_page_disposal_is_not_not_found():
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc.getDrawPage.side_effect = DisposedException("gone")
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        with pytest.raises(DisposedException):
            DeleteChart().execute(ctx, name="Chart_1")


def test_non_chart_embed_and_shape_absent_and_not_deleted():
    # Math and non-chart OLE shapes are ignored by list, resolve, and delete
    math_clsid = "078b7aba-54fc-4570-8544-e03884a92cd3"
    math_embed = MagicMock()
    math_embed.CLSID = math_clsid

    doc_writer = MagicMock()
    embeds = MagicMock()
    embeds.getElementNames.return_value = ("Formula_1",)
    embeds.hasByName.side_effect = lambda n: n == "Formula_1"
    embeds.getByName.return_value = math_embed
    doc_writer.getEmbeddedObjects.return_value = embeds
    doc_writer.getDrawPage.return_value.getCount.return_value = 0

    ctx_w = MagicMock(doc=doc_writer)
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        res = ListCharts().execute(ctx_w)
        assert res["charts"] == []
        assert _resolve_chart(doc_writer, "Formula_1") is None
        del_res = DeleteChart().execute(ctx_w, name="Formula_1")
        assert del_res["status"] == "error"
        assert "not found" in del_res["message"]
        embeds.removeByName.assert_not_called()

    # Draw page non-chart shape
    other_shape = _OleShape("Other_1", clsid="11111111-2222-3333-4444-555555555555")
    page = _page(other_shape)
    doc_draw = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc_draw.getDrawPages.return_value = pages

    ctx_d = MagicMock(doc=doc_draw)
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("draw")):
        res = ListCharts().execute(ctx_d)
        assert res["charts"] == []
        assert _resolve_chart(doc_draw, "Other_1") is None
        del_res = DeleteChart().execute(ctx_d, name="Other_1")
        assert del_res["status"] == "error"
        assert "not found" in del_res["message"]
        page.remove.assert_not_called()


def test_runtime_writer_shape_clsid_listed_and_deleted():
    # 12d37028-... is recognized on string and ByteSequence representations
    runtime_clsid = "12d37028-0b55-463d-863a-211463e2c59f"
    shape = _OleShape("Chart_W", clsid=runtime_clsid)
    page = _page(shape)

    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc.getDrawPage.return_value = page

    ctx = MagicMock(doc=doc)
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        res = ListCharts().execute(ctx)
        assert len(res["charts"]) == 1
        assert res["charts"][0]["name"] == "Chart_W"

        found = _resolve_chart(doc, "Chart_W")
        assert found is shape

        del_res = DeleteChart().execute(ctx, name="Chart_W")
        assert del_res["status"] == "ok"
        page.remove.assert_called_once_with(shape)

    # ByteSequence variant
    shape_bytes = _OleShape("Chart_WB", clsid=_UnoByteSequence(runtime_clsid.encode("ascii")))
    assert _is_chart_clsid(shape_bytes.CLSID) is True


def test_writer_duplicate_name_embed_and_draw_page_deduped():
    # When an embed and a draw-page shape share a name, list emits it once
    embed_obj = MagicMock()
    embed_obj.CLSID = CHART_CLSID
    shape_obj = _OleShape("Chart_0", clsid=CHART_CLSID)

    embeds = MagicMock()
    embeds.getElementNames.return_value = ("Chart_0",)
    embeds.hasByName.side_effect = lambda n: n == "Chart_0"
    embeds.getByName.return_value = embed_obj

    page = _page(shape_obj)
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value = embeds
    doc.getDrawPage.return_value = page

    ctx = MagicMock(doc=doc)
    with patch("plugin.calc.charts.supportsService", side_effect=_supports("writer")):
        res = ListCharts().execute(ctx)
        assert len(res["charts"]) == 1
        assert res["charts"][0]["name"] == "Chart_0"

        # Delete removes from embeds container first
        del_res = DeleteChart().execute(ctx, name="Chart_0")
        assert del_res["status"] == "ok"
        embeds.removeByName.assert_called_once_with("Chart_0")
        page.remove.assert_not_called()


def test_predicate_disposal_propagates():
    # Disposal in property access or fallback diagram check must propagate
    disposed_host = MagicMock()
    type(disposed_host).CLSID = property(fget=MagicMock(side_effect=DisposedException("gone")))
    with pytest.raises(DisposedException):
        _is_chart_host(disposed_host)

    embed_host = MagicMock(spec=["CLSID", "getEmbeddedObject"])
    embed_host.CLSID = None
    chart_doc = MagicMock(spec=["getDiagram"])
    chart_doc.getDiagram.side_effect = DisposedException("gone")
    embed_host.getEmbeddedObject.return_value = chart_doc
    with pytest.raises(DisposedException):
        _writer_embed_is_chart(embed_host)


def test_unique_chart_names_unit():
    # Lowest available index is selected
    assert _unique_chart_name({"Chart_1", "Formula_0"}) == "Chart_0"
    assert _unique_chart_name({"Chart_0", "Chart_1"}) == "Chart_2"
    assert _unique_chart_name({"Chart_2"}) == "Chart_0"

