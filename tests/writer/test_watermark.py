# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin/writer/watermark.py (no live LibreOffice)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugin.tests.testing_utils import TestingFactory
from plugin.writer.watermark import PageGetWatermark, PageSetWatermark


@pytest.fixture(autouse=True)
def _mock_uno_and_review(monkeypatch: pytest.MonkeyPatch) -> None:
    import uno

    monkeypatch.setattr(uno, "createUnoStruct", lambda type_name: SimpleNamespace())
    monkeypatch.setattr("plugin.writer.edit_review.review_recording_enabled", lambda: True)


class _FakeShape:
    def __init__(self, name: str, *, fail_dispose: bool = False, container: list[_FakeShape] | None = None) -> None:
        self._name = name
        self._fail_dispose = fail_dispose
        self._container = container
        self.disposed = False

    def getName(self) -> str:
        return self._name

    def supportsService(self, service: str) -> bool:
        return False

    def dispose(self) -> None:
        if self._fail_dispose:
            raise RuntimeError("dispose failed")
        self.disposed = True
        if self._container is not None and self in self._container:
            self._container.remove(self)


class _FakeDrawPage:
    def __init__(self, shapes: list[_FakeShape] | None = None) -> None:
        self.shapes: list[_FakeShape] = shapes if shapes is not None else []

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, idx: int) -> _FakeShape:
        return self.shapes[idx]


class _FakeURLTransformer:
    def parseStrict(self, url: Any) -> tuple[bool, Any]:
        return True, url


class _FakeDispatch:
    def __init__(self, state: dict[str, Any], *, enabled: bool = True) -> None:
        self._state = state
        self._enabled = enabled
        self.removed = False

    def addStatusListener(self, listener: Any, url: Any) -> None:
        pvs = tuple(SimpleNamespace(Name=k, Value=v) for k, v in self._state.items())
        listener.statusChanged(SimpleNamespace(IsEnabled=self._enabled, State=pvs))

    def removeStatusListener(self, listener: Any, url: Any) -> None:
        self.removed = True


class _FakeDispatchHelper:
    def __init__(self, state: dict[str, Any], *, apply_changes: bool = True) -> None:
        self._state = state
        self._apply_changes = apply_changes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def executeDispatch(self, frame: Any, command: str, target: str, flags: int, args: Any) -> int:
        recorded = {pv.Name: pv.Value for pv in args}
        self.calls.append((command, recorded))
        if self._apply_changes:
            if recorded.get("Text") == "":
                # SwEditShell::GetWatermark returns default SfxWatermarkItem once no shape exists.
                self._state.update(
                    {
                        "Text": "",
                        "Font": "Liberation Sans",
                        "Angle": 45,
                        "Transparency": 50,
                        "Color": 0xC0C0C0,
                    }
                )
            else:
                self._state.update(recorded)
        return 0


def _make_env(
    initial_state: dict[str, Any] | None = None,
    *,
    enabled: bool = True,
    apply_changes: bool = True,
    shapes: list[_FakeShape] | None = None,
) -> tuple[Any, _FakeDispatchHelper, dict[str, Any], _FakeDrawPage, _FakeDispatch]:
    state: dict[str, Any] = dict(
        initial_state
        or {
            "Text": "",
            "Font": "Liberation Sans",
            "Angle": 45,
            "Transparency": 50,
            "Color": 0xC0C0C0,
        }
    )
    helper = _FakeDispatchHelper(state, apply_changes=apply_changes)
    transformer = _FakeURLTransformer()
    dispatch = _FakeDispatch(state, enabled=enabled)
    draw_page = _FakeDrawPage(shapes)

    class _FakeServiceManager:
        def createInstanceWithContext(self, service: str, ctx: Any) -> Any:
            if service == "com.sun.star.util.URLTransformer":
                return transformer
            if service == "com.sun.star.frame.DispatchHelper":
                return helper
            raise AssertionError(f"Unexpected service: {service}")

    uno_ctx = SimpleNamespace(getServiceManager=lambda: _FakeServiceManager())
    frame = SimpleNamespace(queryDispatch=lambda url, target, flags: dispatch)
    controller = SimpleNamespace(getFrame=lambda: frame)
    doc = SimpleNamespace(
        getCurrentController=lambda: controller,
        getDrawPage=lambda: draw_page,
    )
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=uno_ctx)
    return tool_ctx, helper, state, draw_page, dispatch


def test_set_keeps_current_fields_like_dialog() -> None:
    tool_ctx, helper, _, _, _ = _make_env(
        {
            "Text": "DRAFT",
            "Font": "DejaVu Sans",
            "Angle": 0,
            "Transparency": 30,
            "Color": 0xFF0000,
        }
    )
    res = PageSetWatermark().execute(tool_ctx, text="FINAL")
    assert res["status"] == "ok", res
    assert res["style_unreviewed"] is True
    assert len(helper.calls) == 1
    cmd, dispatched = helper.calls[0]
    assert cmd == ".uno:Watermark"
    assert dispatched == {
        "Text": "FINAL",
        "Font": "DejaVu Sans",
        "Angle": 0,
        "Transparency": 30,
        "Color": 0xFF0000,
    }
    assert res["watermark"] == {
        "text": "FINAL",
        "font": "DejaVu Sans",
        "angle": 0,
        "transparency": 30,
        "color": "#ff0000",
    }


def test_set_new_uses_lo_defaults() -> None:
    tool_ctx, helper, _, _, _ = _make_env()
    res = PageSetWatermark().execute(tool_ctx, text="CONFIDENTIAL", color="#123456")
    assert res["status"] == "ok"
    assert len(helper.calls) == 1
    _, dispatched = helper.calls[0]
    assert dispatched == {
        "Text": "CONFIDENTIAL",
        "Font": "Liberation Sans",
        "Angle": 45,
        "Transparency": 50,
        "Color": 0x123456,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "X", "angle": 360},
        {"text": "X", "angle": True},
        {"text": "X", "transparency": 101},
        {"text": "X", "color": "nope"},
        {"text": "X", "font": " "},
    ],
)
def test_set_rejects_out_of_range_without_dispatch(kwargs: dict[str, Any]) -> None:
    tool_ctx, helper, _, _, _ = _make_env()
    res = PageSetWatermark().execute(tool_ctx, **kwargs)
    assert res["status"] == "error"
    assert helper.calls == []


def test_set_refuses_when_command_disabled() -> None:
    pic = _FakeShape("WordPictureWatermark1")
    tool_ctx, helper, _, _, _ = _make_env(enabled=False, shapes=[pic])
    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "error"
    assert res["code"] == "UNSUPPORTED"
    assert helper.calls == []
    assert pic.disposed is False


def test_set_fails_closed_when_lo_ignores_dispatch() -> None:
    pic = _FakeShape("WordPictureWatermark1")
    tool_ctx, helper, _, _, _ = _make_env(apply_changes=False, shapes=[pic])
    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "error"
    assert len(helper.calls) == 1
    assert pic.disposed is False


def test_set_fails_closed_when_only_color_ignored() -> None:
    tool_ctx, helper, _, _, _ = _make_env(
        {
            "Text": "DRAFT",
            "Font": "Liberation Sans",
            "Angle": 45,
            "Transparency": 50,
            "Color": 0xC0C0C0,
        },
        apply_changes=False,
    )
    res = PageSetWatermark().execute(tool_ctx, text="DRAFT", color="#ff0000")
    assert res["status"] == "error"
    assert len(helper.calls) == 1


def test_get_normalises_360_angle() -> None:
    # edfcol.cxx lcl_GetAngle returns 360 for a 0-degree watermark shape.
    tool_ctx, _, _, _, dispatch = _make_env(
        {
            "Text": "DRAFT",
            "Font": "Liberation Sans",
            "Angle": 360,
            "Transparency": 50,
            "Color": 0xC0C0C0,
        }
    )
    res = PageGetWatermark().execute(tool_ctx)
    assert res["status"] == "ok"
    assert res["watermark"]["angle"] == 0
    assert res["picture_watermark"] is False
    assert dispatch.removed is True


def test_get_fails_closed_when_status_missing_key() -> None:
    tool_ctx, _, _, _, dispatch = _make_env(
        {
            "Text": "DRAFT",
            "Angle": 45,
            "Transparency": 50,
            "Color": 0xC0C0C0,
        }
    )
    res = PageGetWatermark().execute(tool_ctx)
    assert res["status"] == "error"
    assert res["code"] == "UNSUPPORTED"
    assert dispatch.removed is True


def test_set_and_remove_dispose_only_picture_watermarks() -> None:
    shapes: list[_FakeShape] = []
    pic = _FakeShape("WordPictureWatermark1", container=shapes)
    logo = _FakeShape("Logo", container=shapes)
    shapes.extend([pic, logo])
    tool_ctx, _, _, _, _ = _make_env(shapes=shapes)

    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "ok"
    assert res["picture_watermarks_removed"] == 1
    assert res["picture_watermark"] is False
    assert pic.disposed is True
    assert logo.disposed is False

    rem = PageSetWatermark().execute(tool_ctx, text="")
    assert rem["status"] == "ok"
    assert rem["watermark"] is None
    assert rem["picture_watermarks_removed"] == 0
    assert rem["picture_watermark"] is False
    assert logo.disposed is False


def test_remove_non_default_watermark_succeeds() -> None:
    tool_ctx, _, _, _, _ = _make_env(
        {
            "Text": "CONFIDENTIAL",
            "Font": "DejaVu Sans",
            "Angle": 0,
            "Transparency": 30,
            "Color": 0xFF0000,
        }
    )
    res = PageSetWatermark().execute(tool_ctx, text="")
    assert res["status"] == "ok", res
    assert res["watermark"] is None


def test_set_reports_remaining_picture_watermark_when_dispose_fails() -> None:
    shapes: list[_FakeShape] = []
    ok_pic = _FakeShape("WordPictureWatermark1", container=shapes)
    bad_pic = _FakeShape("WordPictureWatermark2", fail_dispose=True, container=shapes)
    shapes.extend([ok_pic, bad_pic])
    tool_ctx, _, _, _, _ = _make_env(shapes=shapes)

    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "ok"
    assert res["picture_watermarks_removed"] == 1
    assert res["picture_watermark"] is True
    assert ok_pic.disposed is True
    assert bad_pic.disposed is False
