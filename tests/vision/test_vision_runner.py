# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-side vision runner (graphic export + trusted RPC)."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.vision.vision_runner import (
    get_selected_image_bytes,
    resolve_vision_image_bytes,
    run_and_insert_vision_for_selection,
    run_trusted_vision,
)


def test_get_selected_image_bytes_decodes_png():
    ctx = MagicMock()
    doc = MagicMock()
    raw = b"fake-png-bytes"
    with patch("plugin.vision.vision_runner.get_selected_image_base64", return_value=base64.b64encode(raw).decode("ascii")):
        assert get_selected_image_bytes(ctx, doc) == raw


def test_get_selected_image_bytes_raises_when_no_selection():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.vision.vision_runner.get_selected_image_base64", return_value=None):
        with pytest.raises(ToolExecutionError) as exc:
            get_selected_image_bytes(ctx, doc)
    assert exc.value.code == "NO_IMAGE_SELECTED"


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
    mock_run_vision.assert_called_once_with(
        ctx,
        {"helper": "extract_text", "params": {"lang": "en"}},
        b"png",
        context={"source": "selection"},
    )


def test_run_trusted_vision_rejects_unknown_helper():
    ctx = MagicMock()
    doc = MagicMock()
    with pytest.raises(ToolExecutionError) as exc:
        run_trusted_vision(ctx, doc, helper="not_real")
    assert exc.value.code == "VISION_ERROR"


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_vision")
@patch("plugin.vision.vision_runner.resolve_vision_image_bytes")
def test_run_trusted_vision_passes_image_name_context(mock_bytes, mock_run_vision, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    mock_bytes.return_value = b"png"
    mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}

    run_trusted_vision(ctx, doc, helper="extract_text", params={"image_name": "Photo1", "lang": "en"})

    mock_bytes.assert_called_once_with(ctx, doc, image_name="Photo1")
    mock_run_vision.assert_called_once_with(
        ctx,
        {"helper": "extract_text", "params": {"image_name": "Photo1", "lang": "en"}},
        b"png",
        context={"source": "graphic_name", "image_name": "Photo1"},
    )


@patch("plugin.vision.vision_runner.get_selected_image_bytes")
def test_resolve_vision_image_bytes_uses_selection_when_name_empty(mock_selected):
    ctx = MagicMock()
    doc = MagicMock()
    mock_selected.return_value = b"sel"
    assert resolve_vision_image_bytes(ctx, doc, image_name="") == b"sel"
    mock_selected.assert_called_once_with(ctx, doc)


@patch("plugin.vision.vision_runner.export_graphic_object_to_bytes")
@patch("plugin.vision.vision_runner._get_graphic_object")
def test_resolve_vision_image_bytes_by_name(mock_get_obj, mock_export):
    ctx = MagicMock()
    doc = MagicMock()
    graphic = MagicMock()
    mock_get_obj.return_value = graphic
    mock_export.return_value = b"named"

    assert resolve_vision_image_bytes(ctx, doc, image_name="Photo1") == b"named"
    mock_get_obj.assert_called_once_with(doc, "Photo1")
    mock_export.assert_called_once_with(ctx, graphic)


@patch("plugin.vision.vision_runner._get_graphic_object")
def test_resolve_vision_image_bytes_raises_when_name_missing(mock_get_obj):
    ctx = MagicMock()
    doc = MagicMock()
    mock_get_obj.return_value = None
    with pytest.raises(ToolExecutionError) as exc:
        resolve_vision_image_bytes(ctx, doc, image_name="Missing")
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
    mock_run_vision.assert_called_once_with(
        ctx,
        {"helper": "extract_text", "params": {"lang": "fr"}},
        b"png",
        context={"source": "selection"},
    )


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_trusted_vision")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_run_and_insert_vision_for_selection_loops_by_name(mock_pairs, mock_run, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs.return_value = [("Img1", MagicMock()), ("Img2", MagicMock())]

    def _run(_ctx, _doc, *, helper, params):
        name = params["image_name"]
        return {
            "status": "ok",
            "helper": helper,
            "full_text": f"text-{name}",
            "html": f"<p>{name}</p>",
            "metrics": {"line_count": 1},
            "warnings": [],
        }

    mock_run.side_effect = _run

    with patch("plugin.vision.vision_egress.insert_vision_result") as insert:
        result = run_and_insert_vision_for_selection(ctx, doc, helper="extract_text", params={"lang": "en"})

    assert result["status"] == "ok"
    assert result["images_processed"] == 2
    assert result["full_text"] == "text-Img1\n\ntext-Img2"
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[0].kwargs["params"]["image_name"] == "Img1"
    assert mock_run.call_args_list[1].kwargs["params"]["image_name"] == "Img2"
    assert insert.call_count == 2
    first_result = insert.call_args_list[0].args[2]
    second_result = insert.call_args_list[1].args[2]
    assert first_result["html"] == "<p>Img1</p>"
    assert second_result["html"] == "<p>Img2</p>"
    assert first_result["full_text"] == "text-Img1"
    assert second_result["full_text"] == "text-Img2"
    assert insert.call_args_list[0].kwargs["params"]["image_name"] == "Img1"
    assert insert.call_args_list[1].kwargs["params"]["image_name"] == "Img2"


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.vision.vision_runner.run_trusted_vision")
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection")
def test_run_and_insert_vision_for_selection_reverse_discovery_order(mock_pairs, mock_run, _mock_merge):
    """When discovery returns reverse click order, the host loop follows that order."""
    ctx = MagicMock()
    doc = MagicMock()
    mock_pairs.return_value = [("Img2", MagicMock()), ("Img1", MagicMock())]

    def _run(_ctx, _doc, *, helper, params):
        name = params["image_name"]
        return {
            "status": "ok",
            "helper": helper,
            "full_text": f"text-{name}",
            "html": f"<p>{name}</p>",
            "metrics": {"line_count": 1},
            "warnings": [],
        }

    mock_run.side_effect = _run

    with patch("plugin.vision.vision_egress.insert_vision_result") as insert:
        run_and_insert_vision_for_selection(ctx, doc, helper="extract_text", params={"lang": "en"})

    assert mock_run.call_args_list[0].kwargs["params"]["image_name"] == "Img2"
    assert mock_run.call_args_list[1].kwargs["params"]["image_name"] == "Img1"
    assert insert.call_count == 2


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


@patch("plugin.vision.vision_runner.merge_vision_params", side_effect=lambda _ctx, params: dict(params or {}))
@patch("plugin.doc.visual_helpers.graphic_objects_in_selection", return_value=[])
def test_run_and_insert_vision_for_selection_no_images(mock_pairs, _mock_merge):
    ctx = MagicMock()
    doc = MagicMock()
    with pytest.raises(ToolExecutionError) as exc:
        run_and_insert_vision_for_selection(ctx, doc, helper="extract_text")
    assert exc.value.code == "NO_IMAGE_SELECTED"
    mock_pairs.assert_called_once()
