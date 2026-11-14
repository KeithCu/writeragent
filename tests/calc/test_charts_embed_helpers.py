# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Unit tests for chart CLSID / Writer embed helpers (no UNO required).

from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from plugin.app import get_tools
from plugin.framework.errors import ToolExecutionError

from plugin.calc.charts import (
    CHART_CLSID,
    CHART_CLSID_DRAW_OLE,
    DrawPage,
    _apply_chart_styling,
    _await_writer_chart_document,
    _axis_title_shape_string,
    _chart_document_from_host,
    _chart_summary,
    _is_chart_clsid,
    _is_chart_host,
    _listed_chart_name,
    _normalize_clsid_value,
    _pick_chart_name,
    _supports_service,
    chart_host,
)


def _tool(name: str):
    return get_tools().get(name)


def test_normalize_clsid_value_bytes():
    assert _normalize_clsid_value(b"abc") == "abc"


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
    assert _is_chart_host(o, is_embed=True) is True


def test_writer_embed_is_chart_by_diagram():
    o = MagicMock(spec=["CLSID", "getEmbeddedObject"])
    o.CLSID = ""
    diagram = MagicMock()
    chart_doc = MagicMock()
    chart_doc.getDiagram.return_value = diagram
    o.getEmbeddedObject.return_value = chart_doc
    assert _is_chart_host(o, is_embed=True) is True


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


class _GroupShape:
    def __init__(self, *children: Any, name: str = "", shape_type: str = "com.sun.star.drawing.GroupShape", name_error: BaseException | None = None) -> None:
        self._children = list(children)
        self._name = name
        self._shape_type = shape_type
        self._name_error = name_error

    def getShapeType(self) -> str:
        return self._shape_type

    def getCount(self) -> int:
        return len(self._children)

    def getByIndex(self, idx: int) -> Any:
        return self._children[idx]

    def remove(self, child: Any) -> None:
        if child not in self._children:
            raise RuntimeException("Child not in group")
        self._children.remove(child)

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
    assert _is_chart_host(host, is_embed=True) is True


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


@pytest.mark.parametrize(
    ("host_kind", "shape_error", "include_named_dead"),
    [
        pytest.param("draw", _DisposedException("gone"), True, id="draw-disposed"),
        pytest.param("draw", ValueError("bad shape"), False, id="draw-bad-shape-type"),
        pytest.param("writer", ValueError("bad shape"), False, id="writer-bad-shape-type"),
        pytest.param("writer", _DisposedException("gone"), False, id="writer-disposed"),
    ],
)
def test_resolve_chart_skips_broken_shapes(host_kind, shape_error, include_named_dead):
    bad = MagicMock()
    bad.getShapeType.side_effect = shape_error
    chart_name = "Chart_1" if host_kind == "draw" else "Chart_9"
    late = _OleShape(chart_name)
    shapes = [bad, late]
    if include_named_dead:
        shapes.insert(1, _OleShape("ignored", name_error=_DisposedException("name gone")))
    page = _page(*shapes)
    doc = MagicMock()
    if host_kind == "draw":
        pages = MagicMock()
        pages.getCount.return_value = 1
        pages.getByIndex.return_value = page
        doc.getDrawPages.return_value = pages
    else:
        doc.getEmbeddedObjects.return_value.hasByName.return_value = False
        doc.getDrawPage.return_value = page
    with patch("plugin.calc.charts._supports_service", side_effect=_supports(host_kind)):
        ref = chart_host(doc).find(chart_name)
        assert ref is not None
        assert ref.host is late


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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        result = _tool("chart_list").execute_safe(ctx)
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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        result = _tool("chart_list").execute_safe(ctx)
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


def test_series_colors_continue_across_chart_types():
    series_a = MagicMock()
    series_a.getPropertySetInfo.return_value.hasPropertyByName.return_value = True
    series_b = MagicMock()
    series_b.getPropertySetInfo.return_value.hasPropertyByName.return_value = True
    series_c = MagicMock()
    series_c.getPropertySetInfo.return_value.hasPropertyByName.return_value = True

    type1 = MagicMock(getDataSeries=MagicMock(return_value=[series_a, series_b]))
    type2 = MagicMock(getDataSeries=MagicMock(return_value=[series_c]))

    coord = MagicMock()
    coord.getChartTypes.return_value = [type1, type2]
    diag = MagicMock()
    diag.getCoordinateSystems.return_value = [coord]
    chart_doc = MagicMock()
    chart_doc.getFirstDiagram.return_value = diag

    _apply_chart_styling(chart_doc, colors=["#FF0000", "#00FF00", "#0000FF"])
    assert series_a.setPropertyValue.call_args_list == [
        call("Color", 0xFF0000),
        call("FillColor", 0xFF0000),
        call("LineColor", 0xFF0000),
    ]
    assert series_b.setPropertyValue.call_args_list == [
        call("Color", 0x00FF00),
        call("FillColor", 0x00FF00),
        call("LineColor", 0x00FF00),
    ]
    assert series_c.setPropertyValue.call_args_list == [
        call("Color", 0x0000FF),
        call("FillColor", 0x0000FF),
        call("LineColor", 0x0000FF),
    ]


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
    res = _tool("chart_list").execute_safe(ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"
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
        patch("plugin.calc.charts._supports_service", side_effect=lambda obj, name: name == "com.sun.star.sheet.SpreadsheetDocument"),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        result = _tool("chart_list").execute_safe(ctx)

    assert result["status"] == "ok"
    assert len(result["charts"]) == 1
    assert result["charts"][0]["name"] == "Chart_A"
    assert result["charts"][0]["sheet_name"] == "Sheet1"
    charts0.getByName.assert_called_once_with("Chart_A")
    assert len(result["errors"]) == 1
    assert "bad charts" in result["errors"][0]["message"]
    assert result["errors"][0]["sheet"] == "Sheet2"


def test_list_charts_calc_get_sheets_disposed_propagates_when_impl_name_succeeds():
    doc = MagicMock()
    doc.getImplementationName.return_value = "ScModelObj"
    doc.getSheets.side_effect = _DisposedException("getSheets disposed")
    ctx = MagicMock()
    ctx.doc = doc

    with (
        patch("plugin.calc.charts._supports_service", side_effect=lambda obj, name: name == "com.sun.star.sheet.SpreadsheetDocument"),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        res = _tool("chart_list").execute_safe(ctx)
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_list_charts_calc_get_sheets_runtime_exception_is_tool_error_when_live():
    doc = MagicMock()
    doc.getImplementationName.return_value = "ScModelObj"
    doc.getSheets.side_effect = RuntimeException("temporary failure")
    ctx = MagicMock()
    ctx.doc = doc

    with (
        patch("plugin.calc.charts._supports_service", side_effect=lambda obj, name: name == "com.sun.star.sheet.SpreadsheetDocument"),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        result = _tool("chart_list").execute_safe(ctx)

    assert result["status"] == "error"
    assert result["code"] == "TOOL_EXECUTION_ERROR"
    assert "Could not list charts: RuntimeException: temporary failure" in result["message"]


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

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        result = _tool("chart_list").execute_safe(ctx)

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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        res = _tool("chart_list").execute_safe(ctx)
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


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
        patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")),
        patch("plugin.calc.charts._is_chart_host", return_value=True),
        patch("plugin.calc.charts._chart_document_from_host", return_value=None),
    ):
        result = _tool("chart_list").execute_safe(ctx)

    assert result["status"] == "ok"
    assert [entry["name"] for entry in result["charts"]] == ["Chart_E"]
    assert len(result.get("errors", [])) == 1
    assert result["errors"][0]["source"] == "draw_page"
    assert "no page" in result["errors"][0]["message"]
def test_supports_service_disposal_propagates():
    doc = MagicMock()
    doc.supportsService.side_effect = DisposedException("gone")
    with pytest.raises(DisposedException):
        _supports_service(doc, "com.sun.star.sheet.SpreadsheetDocument")

    doc.supportsService.side_effect = RuntimeException("no")
    assert _supports_service(doc, "com.sun.star.sheet.SpreadsheetDocument") is False


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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        with pytest.raises(DisposedException):
            chart_host(doc).find("Chart_9")


def test_list_charts_writer_draw_page_disposal_propagates():
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc.getDrawPage.side_effect = DisposedException("gone")
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        res = _tool("chart_list").execute_safe(ctx)
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


def test_get_summary_disposal_propagates():
    obj = MagicMock()
    chart_doc = MagicMock()
    chart_doc.HasMainTitle = True
    chart_doc.getTitle.side_effect = DisposedException("gone")
    with patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc):
        with pytest.raises(DisposedException):
            _chart_summary(obj, "Chart_1")

    chart_doc.getTitle.side_effect = RuntimeException("no title")
    with patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc):
        summary = _chart_summary(obj, "Chart_1")
        assert summary == {"name": "Chart_1"}


def test_delete_chart_writer_draw_page_disposal_is_not_not_found():
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc.getDrawPage.side_effect = DisposedException("gone")
    ctx = MagicMock()
    ctx.doc = doc
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        res = _tool("chart_delete").execute_safe(ctx, name="Chart_1")
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"


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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        res = _tool("chart_list").execute_safe(ctx_w)
        assert res["charts"] == []
        assert chart_host(doc_writer).find("Formula_1") is None
        del_res = _tool("chart_delete").execute_safe(ctx_w, name="Formula_1")
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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        res = _tool("chart_list").execute_safe(ctx_d)
        assert res["charts"] == []
        assert chart_host(doc_draw).find("Other_1") is None
        del_res = _tool("chart_delete").execute_safe(ctx_d, name="Other_1")
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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        res = _tool("chart_list").execute_safe(ctx)
        assert len(res["charts"]) == 1
        assert res["charts"][0]["name"] == "Chart_W"

        ref = chart_host(doc).find("Chart_W")
        assert ref is not None and ref.host is shape

        del_res = _tool("chart_delete").execute_safe(ctx, name="Chart_W")
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
    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        res = _tool("chart_list").execute_safe(ctx)
        assert len(res["charts"]) == 1
        assert res["charts"][0]["name"] == "Chart_0"

        # Delete removes from embeds container first
        del_res = _tool("chart_delete").execute_safe(ctx, name="Chart_0")
        assert del_res["status"] == "ok"
        embed_obj.getAnchor().getText.return_value.removeTextContent.assert_called_once_with(embed_obj)
        doc.getText.return_value.removeTextContent.assert_not_called()
        embeds.removeByName.assert_not_called()
        page.remove.assert_not_called()


def test_delete_writer_embed_does_not_call_remove_on_collection():
    embed_obj = MagicMock()
    embed_obj.CLSID = CHART_CLSID

    embeds1 = MagicMock()
    embeds1.hasByName.side_effect = lambda n: n == "Chart_0"
    embeds1.getByName.return_value = embed_obj

    embeds2 = MagicMock()
    embeds2.hasByName.return_value = False

    doc = MagicMock()
    doc.getEmbeddedObjects.side_effect = [embeds1, embeds2]
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        del_res = _tool("chart_delete").execute_safe(ctx, name="Chart_0")
        assert del_res["status"] == "ok"
        embed_obj.getAnchor().getText.return_value.removeTextContent.assert_called_once_with(embed_obj)
        doc.getText.return_value.removeTextContent.assert_not_called()
        embeds1.removeByName.assert_not_called()
        embeds2.removeByName.assert_not_called()


def test_delete_writer_embed_frame_anchored():
    embed_obj = MagicMock()
    embed_obj.CLSID = CHART_CLSID
    frame_text = MagicMock()
    embed_obj.getAnchor.return_value.getText.return_value = frame_text

    embeds = MagicMock()
    embeds.hasByName.side_effect = lambda n: n == "Chart_Frame"
    embeds.getByName.return_value = embed_obj

    doc = MagicMock()
    doc.getEmbeddedObjects.return_value = embeds
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        ref = chart_host(doc).find("Chart_Frame")
        assert ref is not None
        assert ref.kind == "writer_embed"

        del_res = _tool("chart_delete").execute_safe(ctx, name="Chart_Frame")
        assert del_res["status"] == "ok"
        assert del_res["deleted"] == "Chart_Frame"
        frame_text.removeTextContent.assert_called_once_with(embed_obj)
        doc.getText.return_value.removeTextContent.assert_not_called()
        embeds.removeByName.assert_not_called()


def test_delete_writer_embed_fallback_to_embedded_objects():
    embed_obj = MagicMock()
    embed_obj.CLSID = CHART_CLSID
    embed_obj.getAnchor.return_value.getText.return_value.removeTextContent.side_effect = RuntimeException("not anchored")

    embeds = MagicMock()
    embeds.hasByName.side_effect = lambda n: n == "Chart_Fallback"
    embeds.getByName.return_value = embed_obj

    doc = MagicMock()
    doc.getEmbeddedObjects.return_value = embeds
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        del_res = _tool("chart_delete").execute_safe(ctx, name="Chart_Fallback")
        assert del_res["status"] == "ok"
        assert del_res["deleted"] == "Chart_Fallback"
        embeds.removeByName.assert_called_once_with("Chart_Fallback")
        doc.getText.return_value.removeTextContent.assert_not_called()


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("chart_list", {}),
        ("chart_get_info", {"name": "Chart_1"}),
        ("chart_delete", {"name": "Chart_1"}),
        ("chart_upsert", {"chart_type": "bar", "headers": ["Cat", "A"], "rows": [["x", 1]]}),
    ],
)
def test_chart_tools_unsupported_document_type(tool_name: str, kwargs: dict[str, Any]):
    doc = MagicMock()
    doc.supportsService.return_value = False
    ctx = MagicMock(doc=doc)

    res = _tool(tool_name).execute_safe(ctx, **kwargs)
    assert res["status"] == "error"
    assert res["code"] == "UNSUPPORTED_DOCUMENT"
    assert res["message"] == "Unsupported document type for charts."
    if tool_name == "chart_upsert":
        doc.createInstance.assert_not_called()


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
        _is_chart_host(embed_host, is_embed=True)


def test_pick_chart_name():
    # Lowest available index is selected when no name is requested
    assert _pick_chart_name({"Chart_1", "Formula_0"}, {}) == "Chart_0"
    assert _pick_chart_name({"Chart_0", "Chart_1"}, {}) == "Chart_2"
    assert _pick_chart_name({"Chart_2"}, {}) == "Chart_0"
    # Blank requested name falls back to lowest free index
    assert _pick_chart_name({"Chart_0"}, {"name": "  "}) == "Chart_1"
    # Free requested name is returned
    assert _pick_chart_name({"Chart_0"}, {"name": "Sales"}) == "Sales"
    # Taken requested name raises INVALID_PARAM
    with pytest.raises(ToolExecutionError) as ei:
        _pick_chart_name({"Sales"}, {"name": "Sales"})
    assert ei.value.code == "INVALID_PARAM"


def test_resolve_and_list_finds_chart_in_top_level_group_draw():
    chart = _OleShape("Chart_G")
    group = _GroupShape(chart, name="Group_1")
    page = _page(group)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        ref = chart_host(doc).find("Chart_G")
        assert ref is not None
        assert ref.host is chart
        res = _tool("chart_list").execute_safe(ctx)
    assert res["status"] == "ok"
    assert [e["name"] for e in res["charts"]] == ["Chart_G"]


def test_resolve_and_list_finds_chart_in_top_level_group_writer():
    chart = _OleShape("Chart_W")
    group = _GroupShape(chart, name="Group_W")
    page = _page(group)
    doc = MagicMock()
    doc.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc.getDrawPage.return_value = page
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        ref = chart_host(doc).find("Chart_W")
        assert ref is not None
        assert ref.host is chart
        res = _tool("chart_list").execute_safe(ctx)
    assert res["status"] == "ok"
    assert [e["name"] for e in res["charts"]] == ["Chart_W"]


def test_resolve_chart_finds_chart_nested_in_groups():
    chart = _OleShape("Chart_Deep")
    inner = _GroupShape(chart, name="InnerGroup")
    outer = _GroupShape(inner, name="OuterGroup")
    page = _page(outer)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        ref = chart_host(doc).find("Chart_Deep")
        assert ref is not None
        assert ref.host is chart


def test_resolve_chart_skips_disposed_child_in_live_group():
    dead = MagicMock()
    dead.getShapeType.side_effect = _DisposedException("child gone")
    live = _OleShape("Chart_AfterDead")
    group = _GroupShape(dead, live, name="Group")
    page = _page(group)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        ref = chart_host(doc).find("Chart_AfterDead")
        assert ref is not None
        assert ref.host is live


def test_resolve_chart_skips_corrupt_child_in_group_and_empty_page():
    bad = MagicMock()
    bad.getShapeType.side_effect = ValueError("corrupt child")
    live = _OleShape("Chart_Live")
    group = _GroupShape(bad, live, name="BadGroup")
    page = _page(group)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        ref = chart_host(doc).find("Chart_Live")
        assert ref is not None
        assert ref.host is live

        bad_only = MagicMock()
        bad_only.getShapeType.side_effect = ValueError("only bad shape")
        bad_page = _page(bad_only)
        pages.getByIndex.return_value = bad_page
        assert chart_host(doc).find("Chart_None") is None


def test_draw_create_name_scan_treats_grouped_shape_name_as_taken():
    child_shape = MagicMock()
    child_shape.Name = "Chart_0"
    child_shape.getShapeType.return_value = "com.sun.star.drawing.CustomShape"
    group = _GroupShape(child_shape, name="Group_1")
    page = _page(group)

    new_shape = MagicMock()
    doc = MagicMock()
    doc.createInstance.return_value = new_shape
    doc.getCurrentController.return_value.getCurrentPage.return_value = page
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages

    chart_doc = MagicMock()
    ctx = MagicMock(doc=doc, ctx=MagicMock())
    rect = MagicMock(Width=100, Height=100, X=0, Y=0)

    with (
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._process_events"),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts.uno.createUnoStruct"),
    ):
        res = DrawPage(ctx.doc).create(ctx, rect, "com.sun.star.chart.BarDiagram")

    assert res["status"] == "ok"
    assert res["name"] == "Chart_1"
    assert res["page"] == 0
    assert new_shape.Name == "Chart_1"


def test_lookup_and_delete_nested_groups_and_toplevel():
    # 1. Nested groups on Draw page
    chart_draw = _OleShape("Chart_G")
    inner_group = _GroupShape(chart_draw, name="InnerGroup")
    outer_group = _GroupShape(inner_group, name="OuterGroup")
    page_draw = _page(outer_group)
    doc_draw = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page_draw
    doc_draw.getDrawPages.return_value = pages

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        ref = chart_host(doc_draw).find("Chart_G")
        assert ref is not None
        assert ref.host is chart_draw
        assert ref.parent is inner_group

        del_res = _tool("chart_delete").execute_safe(MagicMock(doc=doc_draw), name="Chart_G")
        assert del_res["status"] == "ok"
        assert chart_draw not in inner_group._children
        page_draw.remove.assert_not_called()

    # 2. Nested groups on Writer draw page
    chart_writer = _OleShape("Chart_WG")
    inner_w = _GroupShape(chart_writer, name="InnerW")
    outer_w = _GroupShape(inner_w, name="OuterW")
    page_writer = _page(outer_w)
    doc_writer = MagicMock()
    doc_writer.getEmbeddedObjects.return_value.hasByName.return_value = False
    doc_writer.getEmbeddedObjects.return_value.getElementNames.return_value = ()
    doc_writer.getDrawPage.return_value = page_writer

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        ref_w = chart_host(doc_writer).find("Chart_WG")
        assert ref_w is not None
        assert ref_w.host is chart_writer
        assert ref_w.parent is inner_w

        del_res_w = _tool("chart_delete").execute_safe(MagicMock(doc=doc_writer), name="Chart_WG")
        assert del_res_w["status"] == "ok"
        assert chart_writer not in inner_w._children
        page_writer.remove.assert_not_called()

    # 3. Top-level OLE chart uses page.remove
    chart_top = _OleShape("Chart_Top")
    page_top = _page(chart_top)
    doc_top = MagicMock()
    pages_top = MagicMock()
    pages_top.getCount.return_value = 1
    pages_top.getByIndex.return_value = page_top
    doc_top.getDrawPages.return_value = pages_top

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        ref_top = chart_host(doc_top).find("Chart_Top")
        assert ref_top is not None
        assert ref_top.parent is page_top

        del_res_top = _tool("chart_delete").execute_safe(MagicMock(doc=doc_top), name="Chart_Top")
        assert del_res_top["status"] == "ok"
        page_top.remove.assert_called_once_with(chart_top)


def test_multipage_draw_charts():
    chart_p0 = _OleShape("Chart_0")
    page0 = _page(chart_p0)
    chart_p1 = _OleShape("Chart_0")
    page1 = _page(chart_p1)

    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda idx: page0 if idx == 0 else page1
    doc.getDrawPages.return_value = pages
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        # List entries both present with page 0 and 1
        list_res = _tool("chart_list").execute_safe(ctx)
        assert list_res["status"] == "ok"
        assert len(list_res["charts"]) == 2
        assert list_res["charts"][0]["name"] == "Chart_0"
        assert list_res["charts"][0]["page"] == 0
        assert list_res["charts"][1]["name"] == "Chart_0"
        assert list_res["charts"][1]["page"] == 1

        # Delete and get_info without page return CHART_AMBIGUOUS, neither page is removed
        del_ambig = _tool("chart_delete").execute_safe(ctx, name="Chart_0")
        assert del_ambig["status"] == "error"
        assert del_ambig.get("code") == "CHART_AMBIGUOUS"
        page0.remove.assert_not_called()
        page1.remove.assert_not_called()

        info_ambig = _tool("chart_get_info").execute_safe(ctx, name="Chart_0")
        assert info_ambig["status"] == "error"
        assert info_ambig.get("code") == "CHART_AMBIGUOUS"

        # page=5 on a 2-page deck returns out-of-range error and removes nothing
        del_oor = _tool("chart_delete").execute_safe(ctx, name="Chart_0", page=5)
        assert del_oor["status"] == "error"
        assert del_oor["code"] == "INVALID_PARAM"
        assert "out of range" in del_oor["message"].lower()
        page0.remove.assert_not_called()
        page1.remove.assert_not_called()

        # Delete with page=1 touches only page 1
        del_p1 = _tool("chart_delete").execute_safe(ctx, name="Chart_0", page=1)
        assert del_p1["status"] == "ok"
        page1.remove.assert_called_once_with(chart_p1)
        page0.remove.assert_not_called()

        # Edit with page=1 touches page 1
        chart_doc = MagicMock()
        with (
            patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
            patch("plugin.calc.charts._apply_chart_styling", return_value=[]) as mock_style,
        ):
            edit_res = _tool("chart_upsert").execute_safe(ctx, name="Chart_0", page=1, title="New Title")
            assert edit_res["status"] == "ok"
            mock_style.assert_called_once()


def test_axis_title_setter_warning_and_no_rollback():
    class _DiagramWithAxisError:
        @property
        def HasXAxisTitle(self) -> bool:
            return True

        @HasXAxisTitle.setter
        def HasXAxisTitle(self, val: bool) -> None:
            raise ValueError("axis setter failed")

    class _ChartDoc:
        def getDiagram(self) -> Any:
            return _DiagramWithAxisError()

    # Setter failure returns warning and does not raise
    warnings = _apply_chart_styling(_ChartDoc(), x_axis_title="X Title")
    assert any("X axis title" in w for w in warnings)

    # DisposedException on setter propagates
    class _DiagramDisposed:
        @property
        def HasXAxisTitle(self) -> bool:
            return True

        @HasXAxisTitle.setter
        def HasXAxisTitle(self, val: bool) -> None:
            raise _DisposedException("axis disposed")

    class _ChartDocDisposed:
        def getDiagram(self) -> Any:
            return _DiagramDisposed()

    with pytest.raises(_DisposedException):
        _apply_chart_styling(_ChartDocDisposed(), x_axis_title="X Title")

    # On edit: status ok, warning names X axis, and no rollback
    chart_obj = _OleShape("Chart_0")
    page = _page(chart_obj)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    ctx = MagicMock(doc=doc)

    with (
        patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=_ChartDoc()),
    ):
        edit_res = _tool("chart_upsert").execute_safe(ctx, name="Chart_0", x_axis_title="X Title")
        assert edit_res["status"] == "ok"
        assert any("X axis title" in w for w in edit_res.get("warnings", []))
        page.remove.assert_not_called()


def test_edit_missing_diagram_applies_data_and_warns():
    chart_obj = _OleShape("Chart_0")
    page = _page(chart_obj)
    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page
    doc.getDrawPages.return_value = pages
    ctx = MagicMock(doc=doc)

    chart_doc = MagicMock()
    chart_doc.getDiagram.return_value = None
    chart_data = MagicMock()
    chart_doc.getData.return_value = chart_data

    with (
        patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            
            name="Chart_0",
            headers=["Cat", "Val"],
            rows=[["A", 10.0]],
        )
        assert res["status"] == "ok"
        chart_data.setData.assert_called_once()
        assert any("No diagram found" in w for w in res.get("warnings", []))
        page.remove.assert_not_called()


class _PageObj:
    def __init__(self, *shapes: object) -> None:
        self._shapes = list(shapes)
        self.add = MagicMock()
        self.remove = MagicMock()

    def getCount(self) -> int:
        return len(self._shapes)

    def getByIndex(self, idx: int) -> Any:
        return self._shapes[idx]


def test_listed_chart_name_direct_asserts():
    assert _listed_chart_name(None) == ""
    assert _listed_chart_name("") == ""
    assert _listed_chart_name("Chart_1") == "Chart_1"


def test_draw_page_unnamed_and_named_charts_list_and_lookup():
    ole_unnamed = _OleShape("")
    ole_chart2 = _OleShape("Chart_2")
    page0 = _page(ole_unnamed, ole_chart2)

    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page0
    doc.getDrawPages.return_value = pages
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")):
        list_res = _tool("chart_list").execute_safe(ctx)
        assert list_res["status"] == "ok"
        assert len(list_res["charts"]) == 2
        assert list_res["charts"][0]["name"] == ""
        assert list_res["charts"][0]["page"] == 0
        assert list_res["charts"][1]["name"] == "Chart_2"
        assert list_res["charts"][1]["page"] == 0

        for target in ("Chart_0_0", ""):
            info = _tool("chart_get_info").execute_safe(ctx, name=target)
            assert info["status"] == "error"
            assert "not found" in info["message"].lower()

            del_res = _tool("chart_delete").execute_safe(ctx, name=target)
            assert del_res["status"] == "error"
            assert "not found" in del_res["message"].lower()

        page0.remove.assert_not_called()

        ref = chart_host(doc).find("Chart_2")
        assert ref is not None
        assert ref.host is ole_chart2


def test_writer_list_retains_embed_and_unnamed_draw_shape():
    doc = MagicMock()
    embed = MagicMock()
    embed.CLSID = CHART_CLSID
    doc.getEmbeddedObjects.return_value.getElementNames.return_value = ["Chart_0"]
    doc.getEmbeddedObjects.return_value.getByName.return_value = embed

    unnamed_ole = _OleShape("")
    doc.getDrawPage.return_value = _page(unnamed_ole)
    ctx = MagicMock(doc=doc)

    with patch("plugin.calc.charts._supports_service", side_effect=_supports("writer")):
        list_res = _tool("chart_list").execute_safe(ctx)
        assert list_res["status"] == "ok"
        names = [c["name"] for c in list_res["charts"]]
        assert names == ["Chart_0", ""]


def test_draw_create_with_page_and_out_of_range():
    c0 = _OleShape("Chart_0")
    g0 = _GroupShape(c0, name="Group0")
    page0 = _PageObj(g0)
    page1 = _PageObj()

    new_shape = MagicMock()
    doc = MagicMock()
    doc.createInstance.return_value = new_shape
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda idx: page0 if idx == 0 else page1
    doc.getDrawPages.return_value = pages

    chart_doc = MagicMock()
    ctx = MagicMock(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._process_events"),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts.uno.createUnoStruct"),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            page=1,
            chart_type="column",
            headers=["Cat", "Val"],
            rows=[["A", 10]],
        )

        assert res["status"] == "ok"
        assert res["name"] == "Chart_1"
        assert res["page"] == 1
        page1.add.assert_called_once_with(new_shape)
        page0.add.assert_not_called()

        doc.createInstance.reset_mock()
        page0.add.reset_mock()
        page1.add.reset_mock()

        err_p5 = _tool("chart_upsert").execute_safe(
            ctx,
            page=5,
            chart_type="column",
            headers=["Cat", "Val"],
            rows=[["A", 10]],
        )
        assert err_p5["status"] == "error"
        assert err_p5["code"] == "INVALID_PARAM"
        assert "out of range" in err_p5["message"].lower()
        doc.createInstance.assert_not_called()
        page0.add.assert_not_called()
        page1.add.assert_not_called()

        err_neg = _tool("chart_upsert").execute_safe(
            ctx,
            page=-1,
            chart_type="column",
            headers=["Cat", "Val"],
            rows=[["A", 10]],
        )
        assert err_neg["status"] == "error"
        assert err_neg["code"] == "INVALID_PARAM"
        assert "out of range" in err_neg["message"].lower()
        doc.createInstance.assert_not_called()
        page0.add.assert_not_called()
        page1.add.assert_not_called()


def test_draw_create_omitted_page_uses_controller_current_page():
    page0 = _PageObj()
    page1 = _PageObj()

    new_shape = MagicMock()
    doc = MagicMock()
    doc.createInstance.return_value = new_shape
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda idx: page0 if idx == 0 else page1
    doc.getDrawPages.return_value = pages

    controller = MagicMock()
    controller.getCurrentPage.return_value = page1
    doc.getCurrentController.return_value = controller

    chart_doc = MagicMock()
    ctx = MagicMock(doc=doc, ctx=MagicMock())

    with (
        patch("plugin.calc.charts._supports_service", side_effect=_supports("draw")),
        patch("plugin.calc.charts._chart_document_from_host", return_value=chart_doc),
        patch("plugin.calc.charts._process_events"),
        patch("plugin.calc.charts._apply_chart_styling", return_value=[]),
        patch("plugin.calc.charts.uno.createUnoStruct"),
    ):
        res = _tool("chart_upsert").execute_safe(
            ctx,
            chart_type="column",
            headers=["Cat", "Val"],
            rows=[["A", 10]],
        )
        assert res["status"] == "ok"
        assert res["page"] == 1
        page1.add.assert_called_once_with(new_shape)
        page0.add.assert_not_called()



