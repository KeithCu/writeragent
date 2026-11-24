# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-side vision runner (graphic export + trusted RPC)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.visual_helpers import AmbiguousGraphicNameError
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.vision.vision_runner import (
    _resolve_locale_language,
    get_selected_image_bytes,
    resolve_vision_image_bytes,
    run_and_insert_vision_for_selection,
    run_trusted_vision,
)


def test_get_selected_image_bytes_returns_raw_bytes():
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    raw = b"fake-png-bytes"
    with patch("plugin.vision.vision_runner.selected_graphic_object", return_value=graphic), patch(
        "plugin.vision.vision_runner.export_graphic_object_to_bytes", return_value=raw
    ):
        assert get_selected_image_bytes(ctx, doc) == raw


def test_get_selected_image_bytes_raises_when_no_selection():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.vision.vision_runner.selected_graphic_object", return_value=None):
        with pytest.raises(ToolExecutionError) as exc:
            get_selected_image_bytes(ctx, doc)
    assert exc.value.code == "NO_IMAGE_SELECTED"


def test_get_selected_image_bytes_raises_when_export_fails():
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    with patch("plugin.vision.vision_runner.selected_graphic_object", return_value=graphic), patch(
        "plugin.vision.vision_runner.export_graphic_object_to_bytes", side_effect=RuntimeError("export failed")
    ):
        with pytest.raises(ToolExecutionError) as exc:
            get_selected_image_bytes(ctx, doc)
    assert exc.value.code == "IMAGE_EXPORT_FAILED"
    assert "Could not export the selected image" in str(exc.value)


def test_get_selected_image_bytes_raises_when_export_returns_none():
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    with patch("plugin.vision.vision_runner.selected_graphic_object", return_value=graphic), patch(
        "plugin.vision.vision_runner.export_graphic_object_to_bytes", return_value=None
    ):
        with pytest.raises(ToolExecutionError) as exc:
            get_selected_image_bytes(ctx, doc)
    assert exc.value.code == "NO_IMAGE_SELECTED"


class DisposedException(Exception):
    pass


@pytest.mark.parametrize(
    ("fn_name", "call_kwargs"),
    [
        ("get_selected_image_bytes", {}),
        ("resolve_vision_image_bytes", {"image_name": "Img1"}),
    ],
)
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: DisposedException("disposed"),
        lambda: DocumentDisposedError("closed"),
    ],
)
def test_image_export_reraises_disposed_document(fn_name, call_kwargs, exc_factory):
    ctx = MagicMock()
    doc = MagicMock()
    fn = get_selected_image_bytes if fn_name == "get_selected_image_bytes" else resolve_vision_image_bytes
    with (
        patch("plugin.vision.vision_runner.selected_graphic_object", return_value=MagicMock()),
        patch("plugin.vision.vision_runner._get_graphic_object", return_value=MagicMock()),
        patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", side_effect=exc_factory()),
    ):
        with pytest.raises(DocumentDisposedError):
            fn(ctx, doc, **call_kwargs)



@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_vision")
@patch("plugin.vision.vision_runner.get_selected_image_bytes")
def test_run_trusted_vision_builds_payload(mock_bytes, mock_run_vision, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    mock_bytes.return_value = b"png"
    mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}

    result = run_trusted_vision(ctx, doc, helper="extract_text", params={"lang": "en"})

    assert result["full_text"] == "hi"
    assert mock_run_vision.call_args.args[1] == {"helper": "extract_text", "params": {"lang": "en"}}
    assert mock_run_vision.call_args.kwargs["context"] == {"source": "selection"}


def test_run_trusted_vision_rejects_unknown_helper():
    ctx = MagicMock()
    doc = MagicMock()
    with pytest.raises(ToolExecutionError) as exc:
        run_trusted_vision(ctx, doc, helper="not_real")
    assert exc.value.code == "VISION_ERROR"


@patch("plugin.vision.vision_runner._get_graphic_object")
@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_vision")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_run_trusted_vision_passes_image_name_context(mock_bytes, mock_run_vision, _mock_merge, mock_get_obj):
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    mock_get_obj.return_value = graphic
    mock_bytes.return_value = b"png"
    mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}

    run_trusted_vision(ctx, doc, helper="extract_text", params={"image_name": "Photo1", "lang": "en"})

    assert mock_run_vision.call_args.args[1]["params"] == {"lang": "en"}
    assert mock_run_vision.call_args.kwargs["context"] == {"source": "graphic_name", "image_name": "Photo1"}


@patch("plugin.vision.vision_runner.get_selected_image_bytes")
def test_resolve_vision_image_bytes_uses_selection_when_name_empty(mock_selected):
    ctx = MagicMock()
    doc = MagicMock()
    mock_selected.return_value = b"sel"
    assert resolve_vision_image_bytes(ctx, doc, image_name="") == b"sel"


@patch("plugin.vision.vision_runner.export_graphic_object_to_bytes")
@patch("plugin.vision.vision_runner._get_graphic_object")
def test_resolve_vision_image_bytes_by_name(mock_get_obj, mock_export):
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    mock_get_obj.return_value = graphic
    mock_export.return_value = b"named"

    assert resolve_vision_image_bytes(ctx, doc, image_name="Photo1") == b"named"


@patch("plugin.vision.vision_runner._get_graphic_object")
def test_resolve_vision_image_bytes_raises_when_name_missing(mock_get_obj):
    ctx = MagicMock()
    doc = MagicMock()
    mock_get_obj.return_value = None
    with pytest.raises(ToolExecutionError) as exc:
        resolve_vision_image_bytes(ctx, doc, image_name="Missing")
    assert exc.value.code == "IMAGE_NOT_FOUND"


@patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", side_effect=RuntimeError("export crash"))
@patch("plugin.vision.vision_runner._get_graphic_object")
def test_resolve_vision_image_bytes_raises_when_export_fails(mock_get_obj, mock_export):
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    mock_get_obj.return_value = graphic
    with pytest.raises(ToolExecutionError) as exc:
        resolve_vision_image_bytes(ctx, doc, image_name="Photo1")
    assert exc.value.code == "IMAGE_EXPORT_FAILED"
    assert "export crash" in str(exc.value)


@patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", return_value=None)
@patch("plugin.vision.vision_runner._get_graphic_object")
def test_resolve_vision_image_bytes_raises_when_export_returns_none(mock_get_obj, mock_export):
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    mock_get_obj.return_value = graphic
    with pytest.raises(ToolExecutionError) as exc:
        resolve_vision_image_bytes(ctx, doc, image_name="Photo1")
    assert exc.value.code == "IMAGE_NOT_FOUND"



@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.framework.i18n.get_lo_locale", return_value="fr_FR")
@patch("plugin.vision.vision_runner.run_vision")
@patch("plugin.vision.vision_runner.get_selected_image_bytes")
def test_run_trusted_vision_resolves_lang_from_locale(mock_bytes, mock_run_vision, mock_get_lo_locale, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    mock_bytes.return_value = b"png"
    mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}

    result = run_trusted_vision(ctx, doc, helper="extract_text", params={})

    assert result["full_text"] == "hi"
    assert mock_run_vision.call_args.args[1]["params"]["lang"] == "fr-FR"


def _fake_run_vision(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
    name = params["image_name"]
    return {
        "status": "ok",
        "helper": helper,
        "full_text": f"text-{name}",
        "html": f"<p>{name}</p>",
        "metrics": {"line_count": 1},
        "warnings": [],
    }


@pytest.mark.parametrize(
    ("names", "expected_full_text"),
    [
        pytest.param(["Img1", "Img2"], "text-Img1\n\ntext-Img2", id="forward_order"),
        pytest.param(["Img2", "Img1"], "text-Img2\n\ntext-Img1", id="reverse_order"),
    ],
)
@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_fake_run_vision)
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_run_and_insert_vision_for_selection_loops_by_name(mock_pairs, mock_run, _mock_merge, names, expected_full_text):
    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs.return_value = [(n, MagicMock()) for n in names]

    with patch("plugin.vision.vision_egress.insert_vision_result") as insert:
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_text", params={"lang": "en"})

    assert result["status"] == "ok"
    assert result["images_processed"] == 2
    assert result["full_text"] == expected_full_text
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0].kwargs["params"] == {"lang": "en", "image_name": names[0]}
    assert mock_run.call_args_list[1].kwargs["params"]["image_name"] == names[1]
    assert insert.call_count == 2
    for idx, name in enumerate(names):
        res_item = insert.call_args_list[idx].args[2]
        assert res_item["html"] == f"<p>{name}</p>"
        assert res_item["full_text"] == f"text-{name}"
        assert insert.call_args_list[idx].kwargs["params"]["image_name"] == name
        assert result["individual_results"][idx]["html"] == f"<p>{name}</p>"
        assert "images_processed" not in result["individual_results"][idx]
    assert "results" not in result
    assert len(result["individual_results"]) == 2


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_trusted_vision")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_run_and_insert_vision_for_selection_image_name_short_circuits(mock_pairs, mock_run, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    mock_run.return_value = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "only",
        "html": "<p>only</p>",
        "metrics": {},
        "warnings": [],
    }

    with patch("plugin.vision.vision_egress.insert_vision_result") as insert:
        result = run_and_insert_vision_for_selection(
            ctx,
            doc,
            helper="extract_text",
            params={"image_name": "Photo1"},
            insert_into_document=True,
        )

    mock_pairs.assert_not_called()
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["params"]["image_name"] == "Photo1"
    insert.assert_called_once()
    assert result["images_processed"] == 1
    assert "results" not in result
    assert len(result["individual_results"]) == 1
    assert result["individual_results"][0]["html"] == "<p>only</p>"
    assert result["individual_results"][0]["full_text"] == "only"
    assert "images_processed" not in result["individual_results"][0]


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[])
def test_run_and_insert_vision_for_selection_no_images(mock_pairs, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    with pytest.raises(ToolExecutionError) as exc:
        run_and_insert_vision_for_selection(ctx, doc, helper="extract_text")
    assert exc.value.code == "NO_IMAGE_SELECTED"
    mock_pairs.assert_called_once()


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_trusted_vision")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_run_and_insert_continues_past_failed_image(mock_pairs, mock_run, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs.return_value = [("Img1", MagicMock()), ("Img2", MagicMock()), ("Img3", MagicMock())]

    def _run(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        name = params["image_name"]
        if name == "Img2":
            return {"status": "error", "code": "VISION_ERROR", "helper": helper, "message": "boom"}
        return {"status": "ok", "helper": helper, "full_text": name, "html": f"<p>{name}</p>", "metrics": {}, "warnings": []}

    mock_run.side_effect = _run
    with patch("plugin.vision.vision_egress.insert_vision_result") as insert:
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_text")

    # A failed image is reported but does not block the rest: Img3 still runs and inserts.
    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert "Img2" in result["message"] and "boom" in result["message"]
    assert "2 of 3" in result["message"]
    assert result["partial"] is True
    assert result["inserted"] is True
    assert result["images_processed"] == 2
    assert result["image_names"] == ["Img1", "Img3"]
    assert result["failed_image"] == "Img2"
    assert result["failed_images"] == ["Img2"]
    assert [c.kwargs["params"]["image_name"] for c in mock_run.call_args_list] == ["Img1", "Img2", "Img3"]
    assert [c.kwargs["params"]["image_name"] for c in insert.call_args_list] == ["Img1", "Img3"]
    assert [r["image_name"] for r in result["individual_results"]] == ["Img1", "Img3"]


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_vision", return_value={"status": "ok", "full_text": "t"})
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"png")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock())])
def test_cancel_reaches_run_vision(_mock_pairs, _mock_bytes, mock_run_vision, _mock_merge):
    """The tool's stop flag must reach the venv RPC; the UNO ctx carries none."""
    from plugin.framework.send_cancellation import SendCancellation

    ctx = MagicMock(spec=[])  # a UNO component context has no send_cancellation attribute
    scope = SendCancellation()
    with patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **k: fn(*a)), \
            patch("plugin.vision.vision_runner._get_graphic_object", return_value=MagicMock()), \
            patch("plugin.vision.vision_runner._resolve_locale_language", return_value="en"), \
            patch("plugin.vision.vision_egress.insert_vision_result"):
        run_and_insert_vision_for_selection(ctx, MagicMock(), helper="extract_text", cancel=scope)
    assert mock_run_vision.call_args.kwargs["cancel"] is scope


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_trusted_vision")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_run_and_insert_single_image_failure_returns_its_error(mock_pairs, mock_run, _mock_merge):
    ctx = MagicMock()
    mock_pairs.return_value = [("Img1", MagicMock())]
    mock_run.return_value = {"status": "error", "code": "VISION_ERROR", "message": "boom"}
    with patch("plugin.vision.vision_egress.insert_vision_result") as insert:
        result = run_and_insert_vision_for_selection(ctx, MagicMock(), helper="extract_text")
    assert result["message"] == "boom"
    assert result["partial"] is False
    assert result["failed_image"] == "Img1"
    assert insert.call_count == 0


def test_run_trusted_vision_rejects_unimplemented_helper_before_export():
    with patch("plugin.vision.vision_runner.resolve_vision_image_bytes") as export, patch(
        "plugin.vision.vision_runner.run_vision"
    ) as rpc:
        with pytest.raises(ToolExecutionError) as exc:
            run_trusted_vision(MagicMock(), MagicMock(), helper="detect_objects")
    assert exc.value.code == "VISION_ERROR"
    assert "unknown helper" in str(exc.value).lower()
    export.assert_not_called()
    rpc.assert_not_called()


def test_resolve_locale_reraises_disposed_graphic():
    class DisposedException(Exception):
        pass

    graphic = MagicMock()
    graphic.getPropertyValue.side_effect = DisposedException("gone")
    with pytest.raises(DocumentDisposedError):
        _resolve_locale_language(MagicMock(), graphic)


def test_resolve_locale_runtime_exception_graphic_falls_back_to_ui_locale():
    class RuntimeException(Exception):
        pass

    graphic = MagicMock()
    graphic.getPropertyValue.side_effect = RuntimeException("unexpected error")
    with patch("plugin.framework.i18n.get_lo_locale", return_value="en_US"):
        res = _resolve_locale_language(MagicMock(), graphic)
    assert res == "en-US"


def test_named_image_locale_ignores_other_selection():
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    graphic.getPropertyValue.side_effect = lambda prop: SimpleNamespace(Language="fr", Country="FR", Variant="") if prop == "CharLocale" else None
    doc.CurrentController.Selection.getPropertyValue.return_value = SimpleNamespace(Language="de", Country="DE", Variant="")

    with patch("plugin.vision.vision_runner._get_graphic_object", return_value=graphic) as mock_get_obj, \
            patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})), \
            patch("plugin.vision.vision_runner.run_vision") as mock_run_vision, \
            patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", return_value=b"png"):
        mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}
        run_trusted_vision(ctx, doc, helper="extract_text", params={"image_name": "Photo1"})

    mock_get_obj.assert_called_once_with(doc, "Photo1")
    assert mock_run_vision.call_args[0][1]["params"]["lang"] == "fr-FR"


def test_named_image_without_char_locale_uses_ui_locale_not_selection():
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    graphic.getPropertyValue.return_value = None
    doc.CurrentController.Selection.getPropertyValue.return_value = SimpleNamespace(Language="de", Country="DE", Variant="")

    with patch("plugin.vision.vision_runner._get_graphic_object", return_value=graphic) as mock_get_obj, \
            patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})), \
            patch("plugin.vision.vision_runner.run_vision") as mock_run_vision, \
            patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", return_value=b"png"), \
            patch("plugin.framework.i18n.get_lo_locale", return_value="en_US"):
        mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}
        run_trusted_vision(ctx, doc, helper="extract_text", params={"image_name": "Photo1"})

    mock_get_obj.assert_called_once_with(doc, "Photo1")
    assert mock_run_vision.call_args[0][1]["params"]["lang"] == "en-US"


@pytest.mark.parametrize(
    ("char_locale", "lo_locale", "expected"),
    [
        (SimpleNamespace(Language="zh", Country="TW", Variant=""), "en_US", "zh-TW"),
        (SimpleNamespace(Language="ja", Country="", Variant=""), "en_US", "ja"),
        (None, "zh_TW", "zh-TW"),
        (None, "ja", "ja"),
    ],
)
def test_resolve_locale_keeps_region(char_locale, lo_locale, expected):
    graphic = None if char_locale is None else MagicMock()
    if graphic is not None:
        graphic.getPropertyValue.return_value = char_locale
    with patch("plugin.framework.i18n.get_lo_locale", return_value=lo_locale):
        assert _resolve_locale_language(MagicMock(), graphic) == expected


def test_run_trusted_vision_rejects_oversized_image_before_rpc():
    with patch("plugin.vision.vision_runner.VISION_IMAGE_MAX_BYTES", 4), patch(
        "plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"12345"
    ), patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})), patch(
        "plugin.vision.vision_runner.run_vision"
    ) as rpc:
        with pytest.raises(ToolExecutionError) as exc:
            run_trusted_vision(MagicMock(), MagicMock(), helper="extract_text")
    assert exc.value.code == "IMAGE_TOO_LARGE"
    rpc.assert_not_called()


def test_ocr_rpc_runs_outside_main_thread_marshal():
    depth = {"n": 0}
    seen: dict[str, int | None] = {"ocr": None, "export": None, "insert": None}

    def fake_exec(fn, *args, bound_scope=None, **kwargs):
        depth["n"] += 1
        try:
            return fn(*args, **kwargs)
        finally:
            depth["n"] -= 1

    def fake_run_vision(*args, **kwargs):
        seen["ocr"] = depth["n"]
        return {"status": "ok", "helper": "extract_structure", "full_text": "hi", "html": "<p>hi</p>", "warnings": [], "metrics": {}}

    def fake_export(*args, **kwargs):
        seen["export"] = depth["n"]
        return b"png"

    def fake_insert(*args, **kwargs):
        seen["insert"] = depth["n"]

    with patch("plugin.framework.queue_executor.main.call", side_effect=fake_exec), patch(
        "plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision
    ), patch("plugin.vision.vision_runner.resolve_vision_image_bytes", side_effect=fake_export), patch(
        "plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})
    ), patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock())]), patch(
        "plugin.vision.vision_egress.insert_vision_result", side_effect=fake_insert
    ):
        ctx = MagicMock()
        result = run_and_insert_vision_for_selection(ctx, MagicMock(), helper="extract_structure")

    assert result["status"] == "ok"
    assert seen["ocr"] == 0
    assert seen["export"] == 1
    assert seen["insert"] == 1


def test_vision_insert_marshaled_with_unscoped_scope():
    scopes_used: list[Any] = []

    def fake_exec(fn, *args, bound_scope=None, **kwargs):
        scopes_used.append(bound_scope)
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=fake_exec),
        patch("plugin.vision.vision_runner.run_vision", return_value={"status": "ok", "full_text": "text"}),
        patch("plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"png"),
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock())]),
        patch("plugin.vision.vision_egress.insert_vision_result"),
    ):
        ctx = MagicMock()
        result = run_and_insert_vision_for_selection(ctx, MagicMock(), helper="extract_structure")

    assert result["status"] == "ok"
    assert None in scopes_used


def test_stop_break_reports_partial_and_stopped():
    # Stop mid-run must set stopped/partial, truncate image_names to processed count, and report "Stopped after N of M images."
    from plugin.framework.send_cancellation import SendCancellation

    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs = [("Img1", MagicMock()), ("Img2", MagicMock()), ("Img3", MagicMock())]
    scope = SendCancellation()

    def _run(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        name = params["image_name"]
        if name == "Img1":
            scope.cancel()
        return {"status": "ok", "helper": helper, "full_text": name, "html": f"<p>{name}</p>", "metrics": {}, "warnings": []}

    with (
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_run),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=mock_pairs),
        patch("plugin.vision.vision_egress.insert_vision_result"),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_text", cancel=scope)

    assert result["status"] == "ok"
    assert result["stopped"] is True
    assert result["partial"] is True
    assert result["images_processed"] == 1
    assert result["image_names"] == ["Img1"]
    assert "Stopped after 1 of 3 images" in result["message"]


def test_send_cancelled_escapes_discovery_as_user_stopped():
    # SendCancelled during discovery must return USER_STOPPED, not a generic VISION_ERROR.
    from plugin.framework.send_cancellation import SendCancelled

    with patch("plugin.framework.queue_executor.main.call", side_effect=SendCancelled("cancel")):
        result = run_and_insert_vision_for_selection(MagicMock(), MagicMock(), helper="extract_text")

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
    assert "Cancelled by user" in result["message"]


def test_run_and_insert_vision_multi_image_partial_failure_attaches_results():
    """When image N fails in a multi-image run, completed results for 1..N-1 are attached."""
    ctx = MagicMock()
    doc = MagicMock()

    call_count = [0]

    def fake_run_trusted(ctx, doc, *, helper, params, cancel=None, on_exported=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"status": "ok", "helper": helper, "full_text": "text1", "html": "<p>1</p>"}
        return {"status": "error", "message": "OCR model crashed"}

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock()), ("Img2", MagicMock())]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=fake_run_trusted),
    ):
        result = run_and_insert_vision_for_selection(
            ctx, doc, helper="extract_text", insert_into_document=False
        )

    assert result["status"] == "error"
    assert result["message"] == "OCR failed for Img2 (OCR model crashed); 1 of 2 images completed."
    assert result["failed_image"] == "Img2"
    assert result["images_processed"] == 1
    assert result["partial"] is True
    assert result["inserted"] is False
    assert len(result["individual_results"]) == 1
    assert result["individual_results"][0]["full_text"] == "text1"


def test_run_trusted_vision_calls_on_exported_with_sent_params():
    from plugin.vision.vision_runner import run_trusted_vision

    ctx = MagicMock()
    doc = MagicMock()
    events = []

    def hook(params, data):
        events.append(("hook", dict(params), data))

    def fake_run_vision(_ctx, spec, png_bytes, **kwargs):
        events.append(("run_vision", dict(spec["params"]), png_bytes))
        return {"status": "ok", "helper": spec["helper"]}

    with (
        patch("plugin.vision.vision_runner.merge_vision_params", return_value={"image_name": "Img1", "lang": "fr"}),
        patch("plugin.vision.vision_runner._get_graphic_object", return_value=MagicMock()),
        patch("plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"png-data"),
        patch("plugin.vision.vision_runner.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision),
    ):
        run_trusted_vision(ctx, doc, helper="extract_structure", on_exported=hook)

    assert len(events) == 2
    assert events[0][0] == "hook"
    assert events[1][0] == "run_vision"
    assert events[0][1]["lang"] == "fr"
    assert "image_name" not in events[0][1]
    assert events[0][2] == b"png-data"


def test_run_and_insert_vision_for_selection_forwards_on_exported():
    ctx = MagicMock()
    doc = MagicMock()
    hook = MagicMock()

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock())]),
        patch("plugin.vision.vision_runner.run_trusted_vision", return_value={"status": "ok", "helper": "extract_structure", "full_text": "ok", "html": "<p>ok</p>"}) as mock_run,
    ):
        run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=False, on_exported=hook)

    assert mock_run.call_args.kwargs["on_exported"] is hook


def test_run_and_insert_all_empty_reports_not_inserted():
    ctx = MagicMock()
    doc = MagicMock()
    empty_res = {"status": "ok", "helper": "extract_structure", "html": "<h1>img</h1><p></p>", "full_text": "", "tables": []}

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock())]),
        patch("plugin.vision.vision_runner.run_trusted_vision", return_value=empty_res),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "ok"
    assert result["inserted"] is False
    assert result["message"] == "OCR complete. No text detected."
    assert "text returned" not in result["message"].lower()


@pytest.mark.parametrize(
    ("outcomes", "expected_deleted", "expected_kept"),
    [
        (["image_deleted", "empty", "image_kept"], ["Img1"], ["Img3"]),
        (["inserted", "empty", "image_kept"], [], ["Img3"]),
        (["image_deleted", "empty", "inserted"], ["Img1"], []),
    ],
)
def test_run_and_insert_tracks_deleted_images(outcomes, expected_deleted, expected_kept):
    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2", "Img3"]

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_fake_run_vision),
        patch("plugin.vision.vision_egress.insert_vision_result", side_effect=outcomes),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "ok"
    if expected_deleted:
        assert result["deleted_images"] == expected_deleted
    else:
        assert "deleted_images" not in result
    if expected_kept:
        assert result["kept_images_delete_not_trackable"] == expected_kept
    else:
        assert "kept_images_delete_not_trackable" not in result


@pytest.mark.parametrize(
    ("outcome", "expected_key"),
    [
        ("image_deleted", "deleted_images"),
        ("image_kept", "kept_images_delete_not_trackable"),
    ],
)
def test_run_and_insert_partial_failure_preserves_deleted_images(outcome, expected_key):
    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2"]

    def _run(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        if params["image_name"] == "Img2":
            return {"status": "error", "code": "VISION_ERROR", "helper": helper, "message": "boom"}
        return _fake_run_vision(_ctx, _doc, helper=helper, params=params, cancel=cancel, on_exported=on_exported)

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_run),
        patch("plugin.vision.vision_egress.insert_vision_result", return_value=outcome),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "error"
    assert result["partial"] is True
    assert result[expected_key] == ["Img1"]


@pytest.mark.parametrize(
    ("outcome", "expected_key"),
    [
        ("image_deleted", "deleted_images"),
        ("image_kept", "kept_images_delete_not_trackable"),
    ],
)
def test_run_and_insert_cancelled_after_delete_preserves_deleted_images(outcome, expected_key):
    from plugin.framework.send_cancellation import SendCancelled

    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2"]

    def _run(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        if params["image_name"] == "Img2":
            raise SendCancelled("cancelled")
        return _fake_run_vision(_ctx, _doc, helper=helper, params=params, cancel=cancel, on_exported=on_exported)

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_run),
        patch("plugin.vision.vision_egress.insert_vision_result", return_value=outcome),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
    assert result["partial"] is True
    assert result[expected_key] == ["Img1"]


@pytest.mark.parametrize("fail_at", ["ocr", "insert"])
def test_run_and_insert_tool_error_on_second_of_three_continues_and_returns_partial(fail_at):
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2", "Img3"]

    def _run(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        if fail_at == "ocr" and params["image_name"] == "Img2":
            raise ToolExecutionError("Image 'Img2' not found.", code="IMAGE_NOT_FOUND")
        return _fake_run_vision(_ctx, _doc, helper=helper, params=params, cancel=cancel, on_exported=on_exported)

    def _insert(_ctx, _doc, res, *, params=None):
        if fail_at == "insert" and (params or {}).get("image_name") == "Img2":
            raise ToolExecutionError("Image 'Img2' not found.", code="IMAGE_NOT_FOUND")
        return "inserted"

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_run),
        patch("plugin.vision.vision_egress.insert_vision_result", side_effect=_insert),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "error"
    assert result["code"] == "IMAGE_NOT_FOUND"
    assert result["partial"] is True
    assert result["images_processed"] == 2
    assert result["image_names"] == ["Img1", "Img3"]
    assert result["failed_image"] == "Img2"
    assert result["failed_images"] == ["Img2"]
    assert result["inserted"] is True


def test_run_and_insert_first_deleted_then_second_insert_raises_preserves_deleted():
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2"]

    def _insert(_ctx, _doc, res, *, params=None):
        if (params or {}).get("image_name") == "Img2":
            raise ToolExecutionError("Image 'Img2' not found.", code="IMAGE_NOT_FOUND")
        return "image_deleted"

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_fake_run_vision),
        patch("plugin.vision.vision_egress.insert_vision_result", side_effect=_insert),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "error"
    assert result["partial"] is True
    assert result["deleted_images"] == ["Img1"]


def test_run_and_insert_unexpected_exception_stops_after_first_or_raises_when_empty():
    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2", "Img3"]
    seen: list[str] = []

    def _run_second_boom(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        seen.append(params["image_name"])
        if params["image_name"] == "Img2":
            raise RuntimeError("boom")
        return _fake_run_vision(_ctx, _doc, helper=helper, params=params, cancel=cancel, on_exported=on_exported)

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_run_second_boom),
        patch("plugin.vision.vision_egress.insert_vision_result", return_value="inserted"),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert seen == ["Img1", "Img2"]
    assert result["status"] == "error"
    assert result["code"] == "VISION_ERROR"
    assert result["partial"] is True
    assert result["images_processed"] == 1
    assert result["image_names"] == ["Img1"]

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=RuntimeError("first boom")),
    ):
        with pytest.raises(RuntimeError, match="first boom"):
            run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)


def test_run_and_insert_disposed_exception_raises_document_disposed():
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[("Img1", MagicMock())]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=DisposedException("disposed")),
    ):
        with pytest.raises(DocumentDisposedError):
            run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)


def test_run_and_insert_disposed_during_export_ends_the_run():
    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2"]

    def _export(_ctx, graphic_obj):
        if graphic_obj is second_graphic:
            raise DisposedException("closed")
        return b"png"

    first_graphic = MagicMock()
    second_graphic = MagicMock()
    graphics = {"Img1": first_graphic, "Img2": second_graphic}

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, graphics[n]) for n in names]),
        patch("plugin.vision.vision_runner._get_graphic_object", side_effect=lambda _doc, name: graphics[name]),
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, p=None: dict(p or {})),
        patch("plugin.vision.vision_runner._resolve_locale_language", return_value="en"),
        patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", side_effect=_export),
        patch("plugin.vision.vision_runner.run_vision", return_value={"status": "ok", "helper": "extract_structure", "full_text": "ok", "html": "<p>ok</p>"}),
    ):
        with pytest.raises(DocumentDisposedError):
            run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=False)


def test_run_and_insert_cancelled_after_failure_lists_completed_image_names():
    from plugin.framework.errors import ToolExecutionError
    from plugin.framework.send_cancellation import SendCancelled

    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2", "Img3"]

    def _run(_ctx, _doc, *, helper, params, cancel=None, on_exported=None):
        if params["image_name"] == "Img1":
            raise ToolExecutionError("missing", code="IMAGE_NOT_FOUND")
        if params["image_name"] == "Img3":
            raise SendCancelled("cancelled")
        return _fake_run_vision(_ctx, _doc, helper=helper, params=params, cancel=cancel, on_exported=on_exported)

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.run_trusted_vision", side_effect=_run),
        patch("plugin.vision.vision_egress.insert_vision_result", return_value="inserted"),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=True)

    assert result["status"] == "error"
    assert result["code"] == "USER_STOPPED"
    assert result["image_names"] == ["Img2"]


def test_run_and_insert_worker_timeout_stops_or_raises_via_real_rpc():
    from plugin.framework.errors import ToolExecutionError

    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2", "Img3"]
    attempts: list[str] = []

    def fake_venv_timeout(*, data, **_kwargs):
        attempts.append(data["context"]["image_name"])
        return {"status": "error", "code": "VENV_TIMEOUT", "message": "Timed out"}

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, p=None: dict(p or {})),
        patch("plugin.vision.vision_runner._get_graphic_object", return_value=MagicMock()),
        patch("plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"png"),
        patch("plugin.scripting.trusted_rpc.run_code_in_user_venv", side_effect=fake_venv_timeout),
    ):
        with pytest.raises(ToolExecutionError, match="Timed out"):
            run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=False)

    assert attempts == ["Img1"]

    attempts.clear()

    def fake_venv_second_timeout(*, data, **_kwargs):
        img = data["context"]["image_name"]
        attempts.append(img)
        if img == "Img2":
            return {"status": "error", "code": "VENV_TIMEOUT", "message": "Timed out"}
        return {"status": "ok", "result": {"status": "ok", "helper": "extract_structure", "html": "<p>ok</p>", "full_text": "ok"}}

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, p=None: dict(p or {})),
        patch("plugin.vision.vision_runner._get_graphic_object", return_value=MagicMock()),
        patch("plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"png"),
        patch("plugin.scripting.trusted_rpc.run_code_in_user_venv", side_effect=fake_venv_second_timeout),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=False)

    assert attempts == ["Img1", "Img2"]
    assert result["status"] == "error"
    assert result["partial"] is True
    assert result["images_processed"] == 1
    assert result["image_names"] == ["Img1"]
    assert result["failed_image"] == "Img2"


def test_run_and_insert_worker_cancelled_returns_stopped_without_failed_image():
    from plugin.framework.send_cancellation import SendCancellation

    ctx = MagicMock()
    doc = MagicMock()
    names = ["Img1", "Img2", "Img3"]
    scope = SendCancellation()

    def fake_venv_cancel_second(*, data, **_kwargs):
        img = data["context"]["image_name"]
        if img == "Img2":
            scope.cancel()
            return {"status": "error", "code": "CANCELLED", "message": "Python worker stopped by user."}
        return {"status": "ok", "result": {"status": "ok", "helper": "extract_structure", "html": "<p>ok</p>", "full_text": "ok"}}

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a)),
        patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[(n, MagicMock()) for n in names]),
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, p=None: dict(p or {})),
        patch("plugin.vision.vision_runner._get_graphic_object", return_value=MagicMock()),
        patch("plugin.vision.vision_runner.resolve_vision_image_bytes", return_value=b"png"),
        patch("plugin.scripting.trusted_rpc.run_code_in_user_venv", side_effect=fake_venv_cancel_second),
    ):
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_structure", insert_into_document=False, cancel=scope)

    assert result["status"] == "ok"
    assert result["stopped"] is True
    assert result["partial"] is True
    assert result["images_processed"] == 1
    assert "failed_image" not in result
    assert "failed_images" not in result


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: DisposedException("SwXTextDocument not valid"),
        lambda: DocumentDisposedError("closed"),
    ],
)
def test_run_trusted_vision_closed_document_raises_document_disposed(exc_factory):
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})),
        patch("plugin.vision.vision_runner._get_graphic_object", side_effect=exc_factory()),
        patch("plugin.vision.vision_runner.run_vision") as mock_run_vision,
    ):
        with pytest.raises(DocumentDisposedError):
            run_trusted_vision(ctx, doc, helper="extract_text", params={"image_name": "Img1"})
    mock_run_vision.assert_not_called()


def test_run_trusted_vision_lookup_error_keeps_its_type():
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {})),
        patch(
            "plugin.vision.vision_runner._get_graphic_object",
            side_effect=AmbiguousGraphicNameError("Several objects named 'Img1'"),
        ),
        patch("plugin.vision.vision_runner.run_vision") as mock_run_vision,
    ):
        with pytest.raises(AmbiguousGraphicNameError, match="Several objects named 'Img1'"):
            run_trusted_vision(ctx, doc, helper="extract_text", params={"image_name": "Img1"})
    mock_run_vision.assert_not_called()


