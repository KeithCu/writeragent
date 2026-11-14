# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for images.py helpers (no LibreOffice required)."""
import base64
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.tests.testing_utils import TestingFactory


from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE
from plugin.doc.document_research_tools import ListNearbyFiles
from plugin.doc.visual_helpers import anchor_type_index
from plugin.framework.client.http_transport import HttpResult
from plugin.framework.errors import DocumentDisposedError, NetworkError
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.tool import ToolContext, ToolRegistry
from plugin.writer.images.image_utils import IMAGE_DOWNLOAD_MAX_BYTES
from plugin.writer.images.images import (
    ImageCropAndHighlight,
    ImageDelete,
    ImageDownload,
    ImageGenerate,
    ImageGetInfo,
    ImageInsert,
    ImageList,
    ImageListNearbyFiles,
    ImageReplace,
    ImageSetProperties,
    _download_image_to_cache,
    _replacement_size_units,
    _resolve_crop_edges,
    _resolve_orient,
)
from plugin.writer.specialized_base import SpecializedWorkflowFinished
from tests.chatbot.test_tool_loop import DummyCalcSpecialTool

def _image_tool_config_side_effect(int_value: int = 1024):
    """Replicate separate typed-getter mocks (last nested patch won for get_config)."""
    from plugin.framework import config_schema as _cs

    def _get(key: str) -> Any:
        schema = _cs.get_config_schema(key) or {}
        typ = schema.get("type")
        if typ == "boolean":
            return False
        if typ == "string":
            return "square"
        return int_value

    return _get


def _image_tool_config_all_false(_key: str) -> bool:
    """Two nested get_config patches: inner False won for every key."""
    return False


_ORIENT_CONSTANTS = {
    "com.sun.star.text.HoriOrientation.LEFT": 3,
    "com.sun.star.text.HoriOrientation.CENTER": 2,
    "com.sun.star.text.HoriOrientation.RIGHT": 1,
    "com.sun.star.text.VertOrientation.TOP": 1,
    "com.sun.star.text.VertOrientation.CENTER": 2,
    "com.sun.star.text.VertOrientation.BOTTOM": 3,
}


@pytest.mark.parametrize(
    ("name", "axis", "expected"),
    [
        ("left", "hori", 3),
        ("LEFT", "hori", 3),
        ("center", "hori", 2),
        ("centre", "hori", 2),
        ("right", "hori", 1),
        ("Right", "hori", 1),
        ("top", "vert", 1),
        ("center", "vert", 2),
        ("bottom", "vert", 3),
    ],
)
def test_resolve_orient_named_positions(name: str, axis: str, expected: int) -> None:
    with patch("uno.getConstantByName", side_effect=_ORIENT_CONSTANTS.__getitem__):
        assert _resolve_orient(name, axis) == (expected, None)


def test_resolve_orient_int_passthrough():
    # Raw UNO integer constants are accepted unchanged (back-compat).
    assert _resolve_orient(3, "hori") == (3, None)
    assert _resolve_orient(0, "vert") == (0, None)


def test_resolve_orient_unknown_name_errors():
    const, err = _resolve_orient("middle", "hori")
    assert const is None
    assert err and "middle" in err and "left" in err  # error lists valid options


def test_resolve_orient_bool_rejected():
    # bool is an int subclass — must not be silently treated as an orientation constant.
    const, err = _resolve_orient(True, "hori")
    assert const is None and err is not None


def test_resolve_orient_vert_rejects_hori_name():
    # 'left' is not a vertical position.
    const, err = _resolve_orient("left", "vert")
    assert const is None and err is not None and "top" in err


@pytest.mark.parametrize("target", ["header_first", "footer_first"])
def test_image_insert_routes_first_page_target_to_region_helper(target: str):
    # Shared target=header never reaches HeaderTextFirst; the tool must
    # pass header_first/footer_first through to the same helper page_get/set use.
    graphic = MagicMock()
    graphic.getName.return_value = "Graphic1"
    placed = {
        "graphic": graphic,
        "style_name": "Standard",
        "region": target,
        "auto_height": True,
    }
    ctx = TestingFactory.create_context(doc_type="writer")
    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.insert_image_into_header_footer", return_value=placed) as insert,
    ):
        res = ImageInsert().execute(
            ctx, path="/tmp/logo.png", target=target, width_mm=40, height_mm=20,
        )
    assert res["status"] == "ok"
    assert res["target"] == target
    assert insert.call_args.args[2] == target


def test_image_generate_default_base_size_is_1024():
    assert ImageGenerate.parameters["properties"]["base_size"]["default"] == 1024
    assert DEFAULT_IMAGE_BASE_SIZE == 1024


def test_image_generate_forwards_stop_checker():
    from plugin.framework.send_cancellation import SendCancellation

    ctx = TestingFactory.create_context(doc_type="writer")
    scope = SendCancellation()
    ctx.send_cancellation = scope

    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image"),
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(1024)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a stop checked cat")
    assert res["status"] == "ok"
    assert svc.generate_image.call_args.kwargs.get("cancel") is scope


def test_image_generate_inserts_even_if_stopped_after_download():
    from plugin.framework.send_cancellation import SendCancellation

    ctx = TestingFactory.create_context(doc_type="writer")
    scope = SendCancellation()
    scope.cancel()
    ctx.send_cancellation = scope

    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    calls = []

    def _run(fn, *args, **kwargs):
        calls.append(kwargs)
        return fn(*args)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image") as mock_insert,
        patch("plugin.writer.images.images.replace_graphic_source"),
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(1024)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a cat")
    assert res["status"] == "ok"
    assert "inserted" in res["message"]
    mock_insert.assert_called_once()
    assert len(calls) == 2
    assert calls[1].get("bound_scope") is None
    assert "bound_scope" in calls[1]


def test_image_generate_stop_is_user_stopped():
    from plugin.framework.errors import ToolExecutionError

    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.side_effect = ToolExecutionError("Image generation stopped by user.", code="USER_STOPPED")

    with (
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image") as mock_insert,
        patch("plugin.writer.images.images.replace_graphic_source") as mock_replace,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(1024)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a cat")

    assert res["status"] != "ok"
    assert res.get("code") == "USER_STOPPED"
    mock_insert.assert_not_called()
    mock_replace.assert_not_called()


def test_image_generate_rejects_non_integer_base_size():
    ok, err = ImageGenerate().validate(prompt="a tabby cat", base_size="nope")
    assert ok is False
    assert err is not None
    assert "integer" in err


def test_resolve_image_generate_is_edit_defaults_to_selection():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/out.png"], None)

    target_obj = MagicMock()
    target_obj.getSize.return_value = MagicMock(Width=6400, Height=4800)
    graphic_model = MagicMock()
    graphic_model.getPropertyValue.side_effect = (
        lambda prop: SimpleNamespace(Width=1024, Height=1024) if prop == "SizePixel" else None
    )

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    for raw in ("", "  "):
        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
            patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=target_obj),
            patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"pngsel"),
            patch("plugin.doc.visual_helpers.graphic_from_object", return_value=graphic_model),
            patch("plugin.writer.images.images.ImageService", return_value=svc),
            patch("plugin.writer.images.images.replace_graphic_source", return_value=True) as replace,
            patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(512)),
            patch("plugin.writer.images.images.get_image_model", return_value=""),
        ):
            res = ImageGenerate().execute(ctx, prompt="a tabby cat", source_image=raw)
            assert res["status"] == "ok"
            assert "edited" in res["message"].lower()
            replace.assert_called_once()
            insert.assert_not_called()

        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
            patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
            patch("plugin.writer.images.images.ImageService", return_value=svc),
            patch("plugin.writer.images.images.replace_graphic_source") as replace,
            patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(512)),
            patch("plugin.writer.images.images.get_image_model", return_value=""),
        ):
            res = ImageGenerate().execute(ctx, prompt="a tabby cat", source_image=raw)
            assert res["status"] == "ok"
            assert "generated" in res["message"].lower()
            insert.assert_called_once()
            replace.assert_not_called()

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_all_false),
    ):
        res = ImageGenerate().execute(ctx, prompt="a tabby cat", source_image="SELECTION")
        assert res["status"] == "error"
        assert res["code"] == "NO_SELECTION"


def test_image_generate_omitted_source_edits_when_selected():
    """Omitted source_image + selected graphic → img2img replace, not a new insert."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/edited.png"], None)

    target_obj = MagicMock()
    frame_size = MagicMock(Width=6400, Height=4800)
    target_obj.getSize.return_value = frame_size
    graphic_model = MagicMock()
    graphic_model.getPropertyValue.side_effect = lambda prop: SimpleNamespace(Width=1024, Height=1024) if prop == "SizePixel" else None

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=target_obj),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"pngsel"),
        patch("plugin.doc.visual_helpers.graphic_from_object", return_value=graphic_model),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True) as replace,
        patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(512)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="make it look like a wizard")
    assert res["status"] == "ok"
    assert "edited" in res["message"].lower()
    assert svc.generate_image.call_args.kwargs.get("source_image") == base64.b64encode(b"pngsel").decode("ascii")
    # SizePixel is 1024, but the patched base size is 512. Output pixels follow
    # the base size; the bytes and the on-page frame do not.
    assert svc.generate_image.call_args.kwargs.get("width") == 512
    assert svc.generate_image.call_args.kwargs.get("height") == 512
    assert svc.generate_image.call_args.kwargs.get("aspect_ratio") == "1:1"

    replace.assert_called_once_with(
        ctx.ctx,
        ctx.doc,
        target_obj,
        "/tmp/edited.png",
        6400,
        4800,
        title="make it look like a wizard",
        description="Edited by endpoint",
    )
    insert.assert_not_called()


def test_image_generate_omitted_source_edits_when_selected_fallback_to_display_size():
    """Missing SizePixel uses the frame's aspect and the base size, not the frame's pixel count."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/edited.png"], None)

    target_obj = MagicMock()
    frame_size = MagicMock(Width=6400, Height=4800)
    target_obj.getSize.return_value = frame_size

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=target_obj),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"pngsel"),
        patch("plugin.doc.visual_helpers.graphic_from_object", return_value=None),
        patch("plugin.doc.visual_helpers.units_to_px", return_value=(640, 480)),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True) as replace,
        patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(512)),
    ):
        res = ImageGenerate().execute(ctx, prompt="make it look like a wizard")

    assert res["status"] == "ok"
    assert "edited" in res["message"].lower()
    assert svc.generate_image.call_args.kwargs.get("source_image") == base64.b64encode(b"pngsel").decode("ascii")
    # 640x480 is 4:3. Patched base size 512 is the long edge, so the short edge is 384.
    assert svc.generate_image.call_args.kwargs.get("width") == 512
    assert svc.generate_image.call_args.kwargs.get("height") == 384
    assert svc.generate_image.call_args.kwargs.get("aspect_ratio") == "4:3"

    replace.assert_called_once_with(
        ctx.ctx,
        ctx.doc,
        target_obj,
        "/tmp/edited.png",
        6400,
        4800,
        title="make it look like a wizard",
        description="Edited by endpoint",
    )
    insert.assert_not_called()


def test_image_generate_edit_scales_single_sided_dimension():
    """Single-sided width or height scales proportionally from the selection geometry on edit."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/edited.png"], None)

    target_obj = MagicMock()
    frame_size = MagicMock(Width=6400, Height=4800)
    target_obj.getSize.return_value = frame_size
    graphic_model = MagicMock()
    graphic_model.getPropertyValue.side_effect = (
        lambda prop: SimpleNamespace(Width=1024, Height=768) if prop == "SizePixel" else None
    )

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=target_obj),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"pngsel"),
        patch("plugin.doc.visual_helpers.graphic_from_object", return_value=graphic_model),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True),
        patch("plugin.writer.images.images.insert_image"),
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(512)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="make it look like a wizard", width=512)

    assert res["status"] == "ok"
    assert svc.generate_image.call_args.kwargs.get("width") == 512
    assert svc.generate_image.call_args.kwargs.get("height") == 384


def test_image_generate_omitted_source_creates_when_no_selection():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_graphic_source") as replace,
        patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(512)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a tabby cat")
    assert res["status"] == "ok"
    assert "generated" in res["message"].lower()
    assert "source_image" not in svc.generate_image.call_args.kwargs
    insert.assert_called_once()
    replace.assert_not_called()
    assert svc.generate_image.call_args.kwargs.get("aspect_ratio") == "1:1"


@pytest.mark.parametrize(
    ("kwargs", "current", "expected"),
    [
        pytest.param({"crop_top_mm": 5}, (0, 200, 300, 400), (500, 200, 300, 400), id="only_top_overrides"),
        pytest.param(
            {"crop_top_mm": 1, "crop_bottom_mm": 2, "crop_left_mm": 3, "crop_right_mm": 4.5},
            (0, 0, 0, 0),
            (100, 200, 300, 450),
            id="all_edges_mm_to_hundredths",
        ),
        pytest.param({}, (10, 20, 30, 40), (10, 20, 30, 40), id="none_keeps_current"),
        pytest.param({"crop_left_mm": 1.234}, (0, 0, 0, 0), (0, 0, 123, 0), id="rounding"),
    ],
)
def test_resolve_crop_edges(kwargs, current, expected):
    # set_image_properties applies this via GraphicCrop; the struct write itself is UNO-only,
    # so only the mm -> 1/100mm edge math and preserve-unspecified-edges behavior are pinned here.
    assert _resolve_crop_edges(kwargs, current) == expected


# --- list nearby image files (from test_list_nearby_image_files.py) ---

def test_list_nearby_image_files_calls_backend_with_images_kind():
    tool = ImageListNearbyFiles()
    ctx = ToolContext(MagicMock(), MagicMock(), "writer", MagicMock())

    expected = {"status": "ok", "files": [], "truncated": False}

    with patch("plugin.writer.images.images.list_nearby_files", return_value=expected) as mock_list:
        with patch("plugin.framework.queue_executor.main.is_current", return_value=False), patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **k: fn()):
            result = tool.execute(ctx, filter="logo")

    mock_list.assert_called_once_with(ctx.ctx, ctx.doc, filter="logo", file_kind="images")
    assert result == expected


def test_images_domain_includes_list_nearby_image_files_not_document_research_list():
    registry = ToolRegistry(services={})
    registry.register(ImageListNearbyFiles())
    registry.register(DummyCalcSpecialTool())
    registry.register(ListNearbyFiles())
    registry.register(SpecializedWorkflowFinished())

    mock_writer = MagicMock()
    mock_writer.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"

    tools = registry.get_tools(doc=mock_writer, active_domain="images", exclude_tiers=())
    names = {t.name for t in tools}

    assert "image_list_nearby_files" in names
    assert "list_nearby_files" not in names
    assert "specialized_workflow_finished" in names


def test_image_download_prepare_tool_call_drops_verify_ssl_for_every_caller():
    from plugin.framework.tool import prepare_tool_call

    tool = ImageDownload()

    # Chat caller: verify_ssl is stripped from arguments
    chat_ctx = TestingFactory.create_context(doc_type="writer")
    chat_ctx.caller = "chat"
    prep_chat = prepare_tool_call(tool, chat_ctx, "image_download", {"url": "https://example.com/a.png", "verify_ssl": False})
    assert prep_chat.error is None
    assert "verify_ssl" not in prep_chat.kwargs
    assert prep_chat.kwargs == {"url": "https://example.com/a.png"}

    # Script caller: verify_ssl is also dropped
    script_ctx = TestingFactory.create_context(doc_type="writer")
    script_ctx.caller = "script"
    prep_script = prepare_tool_call(tool, script_ctx, "image_download", {"url": "https://example.com/a.png", "verify_ssl": False})
    assert prep_script.error is None
    assert "verify_ssl" not in prep_script.kwargs
    assert prep_script.kwargs == {"url": "https://example.com/a.png"}


def test_download_image_to_cache_unlinks_stale_partials(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))

    old_partial = tmp_path / "img-old.partial"
    old_partial.write_bytes(b"old")
    young_partial = tmp_path / "img-young.partial"
    young_partial.write_bytes(b"young")

    now = time.time()
    os.utime(str(old_partial), (now - 1000, now - 1000))
    os.utime(str(young_partial), (now - 10, now - 10))

    with patch(
        "plugin.writer.images.image_utils.sync_request",
        return_value=HttpResult(200, b"\x89PNG\r\n\x1a\nfake", "image/png"),
    ):
        path = _download_image_to_cache("https://example.com/logo.png")
    assert not old_partial.exists()
    assert young_partial.exists()
    assert os.path.exists(path)


def test_image_replace_https_path_downloads_url():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", return_value="/tmp/logo.png") as download,
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True),
    ):
        result = ImageReplace().execute(ctx, name="Logo", path="https://example.com/logo.png")
    assert result["status"] == "ok"
    assert download.call_args.args == ("https://example.com/logo.png",)


def test_image_generate_marshals_insert_unscoped():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/fake.png"], None)
    scopes_passed: list[Any] = []
    timeouts_passed: list[float] = []

    def _track_run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        scopes_passed.append(bound_scope)
        timeouts_passed.append(timeout)
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_track_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image"),
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_side_effect(1024)),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        result = ImageGenerate().execute(ctx, prompt="a red circle")
    assert result["status"] == "ok"
    assert None in scopes_passed
    from plugin.writer.images.images import _DOCUMENT_MARSHAL_TIMEOUT_SEC

    assert _DOCUMENT_MARSHAL_TIMEOUT_SEC in timeouts_passed



def _graphic_for_info(extra: dict[str, Any] | None = None) -> MagicMock:
    """Graphic whose getPropertyValue only answers the keys image_get_info needs."""
    size = MagicMock()
    size.Width = 700
    size.Height = 390
    props: dict[str, Any] = {
        "Size": size,
        "GraphicURL": "",
        "Title": "",
        "Description": "",
        "Graphic": None,
    }
    if extra:
        props.update(extra)
    graphic = MagicMock()
    graphic.Graphic = props.get("Graphic")

    def _get(name: str) -> Any:
        if name not in props:
            raise Exception("missing %s" % name)
        return props[name]

    graphic.getPropertyValue.side_effect = _get
    graphic.getAnchor.side_effect = Exception("no anchor")
    return graphic


def test_image_get_info_reports_hyperlink_url():
    # ODF draw:a href is HyperLinkURL on the graphic. The tool must return it.
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = _graphic_for_info({"HyperLinkURL": "#ÍNDEX"})
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="IndexButton")
    assert res["status"] == "ok"
    assert res["hyperlink_url"] == "#ÍNDEX"


def test_image_get_info_hyperlink_url_empty_when_absent():
    # Calc/Draw shapes often have no HyperLinkURL. Missing means no link, not an error.
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = _graphic_for_info()
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="Plain")
    assert res["status"] == "ok"
    assert res["hyperlink_url"] == ""


def test_replacement_size_units_preserves_aspect():
    assert _replacement_size_units(2000, 1000, 40, None) == (4000, 2000)
    assert _replacement_size_units(2000, 1000, None, 10) == (2000, 1000)
    assert _replacement_size_units(2000, 1000, 40, 10) == (4000, 1000)
    assert _replacement_size_units(0, 1000, 40, None) == (4000, 1000)


def test_image_replace_one_side_scales():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    size = MagicMock()
    size.Width = 2000
    size.Height = 1000
    graphic.getSize.return_value = size
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True) as mock_replace,
    ):
        res = ImageReplace().execute(ctx, name="Graphic1", path="/tmp/new.png", width_mm=40)
    assert res["status"] == "ok"
    mock_replace.assert_called_once_with(
        ctx.ctx, ctx.doc, graphic, "/tmp/new.png", width_units=4000, height_units=2000
    )


def test_image_replace_size_read_failure_is_tool_execution_error():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getSize.side_effect = RuntimeError("cannot read size")
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source") as mock_replace,
    ):
        res = ImageReplace().execute_safe(ctx, name="Graphic1", path="/tmp/new.png", width_mm=40)
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "cannot read size" in res.get("message", "")
    mock_replace.assert_not_called()


def test_image_replace_download_failure_is_tool_error():
    import urllib.error

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", side_effect=urllib.error.URLError("timed out")),
    ):
        res = ImageReplace().execute(ctx, name="Graphic1", path="https://example.com/pic.png")
    assert res["status"] != "ok"
    assert res.get("code") == "DOWNLOAD_FAILED"
    assert res.get("message", "").startswith("Could not download image:")

    # A local missing file still returns FILE_NOT_FOUND and does not call the downloader
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache") as mock_download,
        patch("os.path.isfile", return_value=False),
    ):
        res_local = ImageReplace().execute(ctx, name="Graphic1", path="/nonexistent/pic.png")
    assert res_local["status"] != "ok"
    assert res_local.get("code") == "FILE_NOT_FOUND"
    mock_download.assert_not_called()


def test_image_insert_download_failure_is_tool_error():
    import urllib.error

    ctx = TestingFactory.create_context(doc_type="writer")
    with patch("plugin.writer.images.images._download_image_to_cache", side_effect=urllib.error.URLError("timed out")):
        res = ImageInsert().execute(ctx, path="https://example.com/pic.png")
    assert res["status"] != "ok"
    assert res.get("code") == "DOWNLOAD_FAILED"
    assert res.get("message", "").startswith("Could not download image:")

    # A local missing file still returns FILE_NOT_FOUND and does not call the downloader
    with (
        patch("plugin.writer.images.images._download_image_to_cache") as mock_download,
        patch("os.path.isfile", return_value=False),
    ):
        res_local = ImageInsert().execute(ctx, path="/nonexistent/pic.png")
    assert res_local["status"] != "ok"
    assert res_local.get("code") == "FILE_NOT_FOUND"
    mock_download.assert_not_called()


def test_image_download_failure_is_tool_error():
    import urllib.error

    ctx = TestingFactory.create_context(doc_type="writer")
    with patch("plugin.writer.images.images._download_image_to_cache", side_effect=urllib.error.URLError("timed out")):
        res = ImageDownload().execute(ctx, url="https://example.com/pic.png")
    assert res["status"] != "ok"
    assert res.get("code") == "DOWNLOAD_FAILED"
    assert res.get("message", "").startswith("Could not download image:")


def test_image_generate_export_failure_is_tool_execution_error():
    ctx = TestingFactory.create_context(doc_type="writer")

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=MagicMock()),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", side_effect=RuntimeError("export")),
    ):
        res = ImageGenerate().execute_safe(ctx, prompt="make it purple", source_image="selection")
    assert res["status"] == "error"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "export" in res.get("message", "")


def test_image_delete_draw_ambiguous_name():
    ctx = MagicMock()
    graphic = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="draw"),
        patch(
            "plugin.doc.visual_helpers.remove_graphic_from_draw_pages",
            side_effect=ValueError("Several shapes named 'Pic'; not deleted."),
        ),
    ):
        res = ImageDelete().execute(ctx, name="Pic")

    assert res["status"] == "error"
    assert res["code"] == "AMBIGUOUS_NAME"
    assert "Several shapes named 'Pic'" in res["message"]


def test_download_cache_hit_skips_second_request(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"

    with patch(
        "plugin.writer.images.image_utils.sync_request",
        return_value=HttpResult(200, png_bytes, "image/png"),
    ):
        path1 = _download_image_to_cache("https://example.com/logo.png")
    assert os.path.isfile(path1)

    with patch(
        "plugin.writer.images.image_utils.sync_request",
        side_effect=AssertionError("sync_request called on cache hit"),
    ):
        path2 = _download_image_to_cache("https://example.com/logo.png")
    assert path1 == path2


@pytest.mark.parametrize(
    "sync_outcome",
    [
        NetworkError("HTTP response exceeded 33554432 bytes", code="HTTP_ERROR"),
        TimeoutError("HTTP read timed out"),
        HttpResult(200, b"", "image/png"),
        HttpResult(200, b"not-an-image", "image/png"),
    ],
)
def test_download_failure_leaves_no_cache_file(tmp_path, monkeypatch, sync_outcome):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))
    patch_kwargs = (
        {"side_effect": sync_outcome}
        if isinstance(sync_outcome, Exception)
        else {"return_value": sync_outcome}
    )
    with patch("plugin.writer.images.image_utils.sync_request", **patch_kwargs):
        with pytest.raises(Exception):
            _download_image_to_cache("https://example.com/sample.png")
    assert list(tmp_path.iterdir()) == []


def test_image_insert_downloads_before_main_thread():
    ctx = TestingFactory.create_context(doc_type="writer")
    order = []

    def _mock_download(url, **kwargs):
        order.append("download")
        return "/tmp/logo.png"

    def _mock_call_on_main(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        order.append("main-enter")
        try:
            return fn(*args, **kwargs)
        finally:
            order.append("main-exit")

    graphic = MagicMock()
    graphic.getName.return_value = "Pic"

    with (
        patch("plugin.writer.images.images._download_image_to_cache", side_effect=_mock_download),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.insert_image_at_locator", return_value=graphic),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_mock_call_on_main),
    ):
        res = ImageInsert().execute(ctx, path="https://example.com/logo.png")

    assert res["status"] == "ok"
    assert order == ["download", "main-enter", "main-exit"]


def test_image_replace_downloads_between_uno_calls():
    ctx = TestingFactory.create_context(doc_type="writer")
    order = []
    graphic = MagicMock()

    def _mock_download(url, **kwargs):
        order.append("download")
        return "/tmp/logo.png"

    def _mock_call_on_main(fn, *args, **kwargs):
        order.append("main")
        return fn(*args)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", side_effect=_mock_download),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_mock_call_on_main),
    ):
        res = ImageReplace().execute(ctx, name="Pic", path="https://example.com/logo.png")

    assert res["status"] == "ok"
    assert order == ["main", "download", "main"]


def test_image_insert_url_unscoped_after_download():
    from plugin.framework.send_cancellation import SendCancelled

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getName.return_value = "Pic"
    calls = []

    def _run(fn, *args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("bound_scope") is not None:
            raise SendCancelled("cancelled")
        return fn(*args)

    with (
        patch("plugin.writer.images.images._download_image_to_cache", return_value="/tmp/test.png"),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.insert_image_at_locator", return_value=graphic),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
    ):
        res = ImageInsert().execute(ctx, path="https://example.com/logo.png")

    assert res["status"] == "ok"
    assert len(calls) == 1
    assert calls[0].get("bound_scope") is None
    assert "bound_scope" in calls[0]


def test_image_replace_url_existence_scoped_apply_unscoped():
    from plugin.framework.send_cancellation import SendCancelled

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getName.return_value = "Pic"

    # 1. Cancelled existence scope aborts before download
    def _run_cancel_existence(fn, *args, **kwargs):
        if "bound_scope" not in kwargs or kwargs.get("bound_scope") is not None:
            raise SendCancelled("cancelled")
        return fn(*args)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache") as mock_download,
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run_cancel_existence),
    ):
        with pytest.raises(SendCancelled):
            ImageReplace().execute(ctx, name="Pic", path="https://example.com/logo.png")
        mock_download.assert_not_called()

    # 2. Cancelled scope on _apply does not skip replace (bound_scope is None)
    calls = []

    def _run_pass_unscoped(fn, *args, **kwargs):
        calls.append(kwargs)
        if "bound_scope" in kwargs and kwargs.get("bound_scope") is not None:
            raise SendCancelled("cancelled")
        return fn(*args)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", return_value="/tmp/logo.png"),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run_pass_unscoped),
    ):
        res = ImageReplace().execute(ctx, name="Pic", path="https://example.com/logo.png")

    assert res["status"] == "ok"
    assert len(calls) == 2
    # Existence check does not pass bound_scope=None
    assert "bound_scope" not in calls[0] or calls[0]["bound_scope"] is not None
    # _apply passes bound_scope=None
    assert "bound_scope" in calls[1]
    assert calls[1]["bound_scope"] is None


def test_image_get_info_anchor_type_from_enum_name():
    ctx = TestingFactory.create_context(doc_type="writer")
    extra = {
        "AnchorType": SimpleNamespace(value="AT_PAGE"),
        "HoriOrient": 0,
        "VertOrient": 3,
    }
    graphic = _graphic_for_info(extra)
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="Pic")
    assert res["status"] == "ok"
    assert res["anchor_type"] == 2
    assert res["hori_orient"] == 0
    assert res["vert_orient"] == 3

    extra2 = {
        "AnchorType": SimpleNamespace(value="NO_SUCH"),
        "HoriOrient": 0,
        "VertOrient": 3,
    }
    graphic2 = _graphic_for_info(extra2)
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic2):
        res2 = ImageGetInfo().execute(ctx, name="Pic")
    assert res2["status"] == "ok"
    assert res2["anchor_type"] is None

    assert anchor_type_index(SimpleNamespace(value="AT_CHARACTER")) == 4
    assert anchor_type_index(2) == 2
    assert anchor_type_index(True) is None
    assert anchor_type_index(9) is None


def test_image_get_info_ignores_fill_bitmap_repr():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = _graphic_for_info({"GraphicURL": "", "GraphicObjectFillBitmap": object()})
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="Pic")
    assert res["status"] == "ok"
    assert res["graphic_url"] == ""


def test_image_get_info_disposal_propagates():
    import pytest

    ctx = TestingFactory.create_context(doc_type="writer")
    size = MagicMock()
    size.Width = 700
    size.Height = 390
    props: dict[str, Any] = {
        "Size": size,
        "GraphicURL": "",
        "Graphic": None,
    }
    graphic = MagicMock()
    graphic.Graphic = None

    def _get(name: str) -> Any:
        if name == "Title":
            raise DocumentDisposedError("closed")
        if name not in props:
            raise Exception("missing %s" % name)
        return props[name]

    graphic.getPropertyValue.side_effect = _get
    graphic.getAnchor.side_effect = Exception("no anchor")

    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        with pytest.raises(DocumentDisposedError):
            ImageGetInfo().execute(ctx, name="Pic")


def test_image_list_reports_unreadable_image():
    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    good = MagicMock()
    bad = MagicMock()

    good.getPropertyValue.side_effect = Exception("no Title")
    good.getAnchor.side_effect = Exception("no anchor")
    bad.getPropertyValue.side_effect = Exception("no Title")
    bad.getAnchor.side_effect = Exception("no anchor")

    good_size = MagicMock()
    good_size.Width = 1000
    good_size.Height = 500
    good.getSize.return_value = good_size
    bad.getSize.side_effect = RuntimeError("boom")

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Good", good), ("Bad", bad)]),
    ):
        res = ImageList().execute(ctx)

    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["images"][0]["name"] == "Good"
    assert res["skipped"] == [{"name": "Bad", "error": "boom"}]


def test_image_list_disposal_is_not_a_short_list():
    import pytest

    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    good = MagicMock()
    good.getSize.side_effect = DocumentDisposedError("closed")
    good.getPropertyValue.side_effect = Exception("no Title")
    good.getAnchor.side_effect = Exception("no anchor")

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Good", good)]),
    ):
        with pytest.raises(DocumentDisposedError):
            ImageList().execute(ctx)


def test_image_set_properties_rejects_anchor_type():
    from plugin.framework.tool import prepare_tool_call

    ctx = TestingFactory.create_context(doc_type="writer")
    tool = ImageSetProperties()
    ok, err = tool.validate(name="G", anchor_type=9)
    assert not ok

    for bad in (9, True):
        prep = prepare_tool_call(tool, ctx, "image_set_properties", {"name": "G", "anchor_type": bad})
        assert prep.error is not None
        assert prep.error["code"] == "VALIDATION_ERROR"
        assert "anchor_type" in prep.error["message"]

    ok2, err2 = tool.validate(name="G", anchor_type=2)
    assert ok2


def test_image_replace_size_disposal_propagates():
    import pytest

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getSize.side_effect = DocumentDisposedError("closed")
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source") as mock_replace,
    ):
        with pytest.raises(DocumentDisposedError):
            ImageReplace().execute(ctx, name="Graphic1", path="/tmp/new.png", width_mm=40)
    mock_replace.assert_not_called()


def test_image_generate_export_disposal_propagates():
    import pytest

    ctx = TestingFactory.create_context(doc_type="writer")

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=MagicMock()),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", side_effect=DocumentDisposedError("closed")),
    ):
        with pytest.raises(DocumentDisposedError):
            ImageGenerate().execute(ctx, prompt="make it purple", source_image="selection")


def test_image_set_properties_size_read_failure_is_tool_execution_error():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getSize.side_effect = RuntimeError("cannot read size")
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
    ):
        res = ImageSetProperties().execute_safe(ctx, name="G", width_mm=10)
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "cannot read size" in res.get("message", "")
    graphic.setPropertyValue.assert_not_called()


def test_image_insert_header_failure_is_tool_execution_error():
    ctx = TestingFactory.create_context(doc_type="writer")
    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.insert_image_into_header_footer", side_effect=RuntimeError("header insert")),
    ):
        res = ImageInsert().execute_safe(ctx, path="/tmp/logo.png", target="header")
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "header insert" in res.get("message", "")


def test_image_crop_bake_failure_is_tool_execution_error():
    ctx = TestingFactory.create_context(doc_type="writer")
    obj = MagicMock()
    size_mock = MagicMock()
    size_mock.Width = 1000
    size_mock.Height = 500
    obj.getSize.return_value = size_mock
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=MagicMock()),
        patch("plugin.doc.visual_helpers.pixel_size", return_value=(100, 80)),
        patch("plugin.writer.images.images.image_mark.bake", side_effect=RuntimeError("bake failed")),
        patch.object(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock, create=True),
    ):
        res = ImageCropAndHighlight().execute_safe(ctx, name="Pic", crop_box=[0, 0, 10, 10])
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "bake failed" in res.get("message", "")
    obj.setPropertyValue.assert_not_called()


def test_image_crop_and_highlight_bake_value_error_is_invalid_parameter():
    ctx = TestingFactory.create_context(doc_type="writer")
    obj = MagicMock()
    obj.getSize.return_value = MagicMock(Width=1000, Height=1000)
    graphic = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=graphic),
        patch("plugin.doc.visual_helpers.pixel_size", return_value=(50000, 50000)),
        patch("plugin.writer.images.images.image_mark.bake", side_effect=ValueError("Region exceeds maximum bake limit")),
        patch.object(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock, create=True),
        patch.object(sys.modules["com.sun.star.awt"], "Size", MagicMock, create=True),
    ):
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 45000, 45000])
    assert res["status"] != "ok"
    assert res.get("code") == "INVALID_PARAM"
    assert res.get("details", {}).get("width_px") == 50000
    assert res.get("details", {}).get("height_px") == 50000
    obj.setPropertyValue.assert_not_called()


def test_image_crop_rejects_non_positive_width_mm():
    ctx = TestingFactory.create_context(doc_type="writer")
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_mgr.getAllUndoActionTitles.return_value = []
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    obj = MagicMock()
    size_mock = MagicMock(Width=1000, Height=500)
    obj.getSize.return_value = size_mock
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=MagicMock()),
        patch("plugin.doc.visual_helpers.pixel_size", return_value=(100, 80)),
        patch("plugin.writer.images.images.image_mark.bake", return_value=MagicMock()),
        patch("plugin.writer.images.images.visual_helpers.safe_set_property", return_value=True),
        patch("plugin.writer.images.images.visual_helpers.has_uno_property", return_value=True),
        patch.object(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock, create=True),
        patch.object(sys.modules["com.sun.star.awt"], "Size", MagicMock, create=True),
    ):
        # 1. width_mm = 0 is rejected
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10], width_mm=0)
        assert res["status"] != "ok"
        assert res.get("code") == "INVALID_PARAM"
        assert res.get("details", {}).get("parameter") == "width_mm"

        # 2. width_mm = -5 is rejected
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10], width_mm=-5)
        assert res["status"] != "ok"
        assert res.get("code") == "INVALID_PARAM"
        assert res.get("details", {}).get("parameter") == "width_mm"

        # 3. width_mm = True (bool) is rejected by schema validation
        from plugin.framework.tool import prepare_tool_call

        prep = prepare_tool_call(
            ImageCropAndHighlight(), ctx, "image_crop_and_highlight", {"name": "Pic", "crop_box": [0, 0, 10, 10], "width_mm": True}
        )
        assert prep.error is not None
        assert prep.error["code"] == "VALIDATION_ERROR"
        assert "width_mm" in prep.error["message"]

        # 4. omitted width_mm keeps size.Width (1000 hundredths mm -> 10.0 mm)
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10])
        assert res["status"] == "ok"
        assert res.get("width_mm") == 10.0


def test_image_crop_reports_frame_failures():
    ctx = TestingFactory.create_context(doc_type="writer")
    obj = MagicMock()
    baked_graphic = MagicMock()
    size_mock = MagicMock(Width=1000, Height=500)
    obj.getSize.return_value = size_mock

    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    titles: list[str] = []

    def _enter(title):
        titles.insert(0, title)

    undo_mgr.enterUndoContext.side_effect = _enter
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: list(titles)
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=MagicMock()),
        patch("plugin.doc.visual_helpers.pixel_size", return_value=(100, 80)),
        patch("plugin.writer.images.images.image_mark.bake", return_value=baked_graphic) as mock_bake,
        patch.object(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock, create=True),
        patch.object(sys.modules["com.sun.star.awt"], "Size", MagicMock, create=True),
    ):
        # GraphicCrop present, but safe_set_property returns False -> SET_CROP_FAILED
        obj.reset_mock()
        mock_bake.reset_mock()
        undo_mgr.reset_mock()
        titles.clear()
        with (
            patch("plugin.writer.images.images.visual_helpers.has_uno_property", return_value=True),
            patch("plugin.writer.images.images.visual_helpers.safe_set_property", return_value=False),
        ):
            res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10])
            assert res["status"] != "ok"
            assert res.get("code") == "SET_CROP_FAILED"
            mock_bake.assert_called_once()
            undo_mgr.undo.assert_called_once()
            assert len([c for c in obj.setPropertyValue.call_args_list if c[0][0] == "Graphic"]) == 1

        # Size write fails (both safe_set_property and safe_try_method return False) -> SET_SIZE_FAILED
        obj.reset_mock()
        mock_bake.reset_mock()
        undo_mgr.reset_mock()
        titles.clear()
        with (
            patch("plugin.writer.images.images.visual_helpers.has_uno_property", return_value=True),
            patch("plugin.writer.images.images.visual_helpers.safe_set_property", side_effect=lambda target, prop, val: prop == "GraphicCrop"),
            patch("plugin.writer.images.images.visual_helpers.safe_try_method", return_value=False),
        ):
            res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10])
            assert res["status"] != "ok"
            assert res.get("code") == "SET_SIZE_FAILED"
            mock_bake.assert_called_once()
            undo_mgr.undo.assert_called_once()
            assert len([c for c in obj.setPropertyValue.call_args_list if c[0][0] == "Graphic"]) == 1


def test_image_crop_locked_undo_writes_nothing():
    ctx = TestingFactory.create_context(doc_type="writer")
    obj = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = True
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)
    size_mock = MagicMock(Width=1000, Height=500)
    obj.getSize.return_value = size_mock

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=MagicMock()),
        patch("plugin.doc.visual_helpers.pixel_size", return_value=(100, 80)),
        patch("plugin.writer.images.images.image_mark.bake", return_value=MagicMock()),
        patch.object(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock, create=True),
        patch.object(sys.modules["com.sun.star.awt"], "Size", MagicMock, create=True),
    ):
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10])
        assert res["status"] != "ok"
        assert res.get("code") == "UNDO_UNAVAILABLE"
        obj.setPropertyValue.assert_not_called()


def test_image_crop_and_info_unmeasurable_graphic():
    ctx = TestingFactory.create_context(doc_type="writer")
    obj = MagicMock()
    # Unmeasurable graphic has no SizePixel or Size100thMM
    graphic = SimpleNamespace(getPropertyValue=lambda prop: None)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=graphic),
        patch("plugin.writer.images.images.image_mark.bake") as mock_bake,
    ):
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10])
        assert res["status"] != "ok"
        assert res.get("code") == "INVALID_PARAM"
        mock_bake.assert_not_called()

        info = ImageGetInfo().execute(ctx, name="Pic")
        assert info["status"] == "ok"
        assert info.get("width_px") is None
        assert info.get("height_px") is None


def test_image_list_disposed_anchor_type_raises_document_disposed():
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)

    def _get_prop(name):
        if name == "AnchorType":
            raise DisposedException("disposed")
        return MagicMock()

    graphic.getPropertyValue.side_effect = _get_prop

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("PagePic", graphic)]),
    ):
        with pytest.raises(DocumentDisposedError):
            ImageList().execute(ctx)


def test_image_generate_invalid_source_image_parameter():
    ctx = TestingFactory.create_context(doc_type="writer")
    res = ImageGenerate().execute(ctx, prompt="test", source_image="https://example.com/pic.png")
    assert res["status"] != "ok"
    assert res.get("code") == "INVALID_PARAM"

    res2 = ImageGenerate().execute(ctx, prompt="test", source_image="/tmp/pic.png")
    assert res2["status"] != "ok"
    assert res2.get("code") == "INVALID_PARAM"


def test_image_generate_rejects_huge_selection_export():
    ctx = TestingFactory.create_context(doc_type="writer")
    mock_obj = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=mock_obj),
        patch("plugin.writer.images.images.IMAGE_DOWNLOAD_MAX_BYTES", 10),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"x" * 11),
        patch("plugin.writer.images.images.ImageService") as mock_svc,
    ):
        res = ImageGenerate().execute(ctx, prompt="p", source_image="selection")
        assert res["status"] != "ok"
        assert res.get("code") == "INVALID_PARAM"
        assert res.get("details", {}).get("parameter") == "source_image"
        mock_svc.assert_not_called()


def test_image_list_at_page_never_calls_gotorange():
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)
    graphic.getPropertyValue.side_effect = lambda name: AT_PAGE if name == "AnchorType" else 3 if name == "AnchorPageNo" else MagicMock()
    graphic.getAnchor.return_value = MagicMock()

    vc = MagicMock()
    controller = MagicMock()
    controller.getViewCursor.return_value = vc
    ctx.doc.getCurrentController = MagicMock(return_value=controller)

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("PagePic", graphic)]),
    ):
        res = ImageList().execute(ctx)

    assert res["status"] == "ok"
    assert len(res["images"]) == 1
    assert res["images"][0]["page"] == 3
    vc.gotoRange.assert_not_called()


def test_image_list_at_page_zero_not_reported_as_page():
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)
    graphic.getPropertyValue.side_effect = lambda name: AT_PAGE if name == "AnchorType" else 0 if name == "AnchorPageNo" else MagicMock()
    graphic.getAnchor.return_value = MagicMock()

    vc = MagicMock()
    controller = MagicMock()
    controller.getViewCursor.return_value = vc
    ctx.doc.getCurrentController = MagicMock(return_value=controller)

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("PagePic", graphic)]),
    ):
        res = ImageList().execute(ctx)

    assert res["status"] == "ok"
    assert len(res["images"]) == 1
    assert "page" not in res["images"][0]
    vc.gotoRange.assert_not_called()


def test_image_list_paragraph_anchor_restores_cloned_cursor():

    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    para_anchor_type = MagicMock()
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)
    graphic.getPropertyValue.side_effect = lambda name: para_anchor_type if name == "AnchorType" else MagicMock()
    anchor = MagicMock()
    start_pos = MagicMock()
    anchor.getStart.return_value = start_pos
    graphic.getAnchor.return_value = anchor

    saved_cursor = MagicMock()
    vc = MagicMock()
    vc.getPage.return_value = 2
    controller = MagicMock()
    controller.getViewCursor.return_value = vc
    ctx.doc.getCurrentController = MagicMock(return_value=controller)

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("ParaPic", graphic)]),
        patch("plugin.writer.images.images.cursor_for_writer_view", return_value=(MagicMock(), saved_cursor)),
    ):
        res = ImageList().execute(ctx)

    assert res["status"] == "ok"
    assert res["images"][0]["page"] == 2
    vc.gotoRange.assert_any_call(start_pos, False)
    assert vc.gotoRange.call_args_list[-1] == ((saved_cursor, False),)


def test_image_list_cursor_save_fails_skips_gotorange_and_page():
    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)
    graphic.getPropertyValue.side_effect = lambda name: MagicMock()
    anchor = MagicMock()
    anchor.getStart.return_value = MagicMock()
    graphic.getAnchor.return_value = anchor

    vc = MagicMock()
    controller = MagicMock()
    controller.getViewCursor.return_value = vc
    ctx.doc.getCurrentController = MagicMock(return_value=controller)

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Pic", graphic)]),
        patch("plugin.writer.images.images.cursor_for_writer_view", side_effect=RuntimeError("cannot clone")),
    ):
        res = ImageList().execute(ctx)

    assert res["status"] == "ok"
    assert len(res["images"]) == 1
    assert "page" not in res["images"][0]
    vc.gotoRange.assert_not_called()


def test_image_list_page_zero_omitted():
    ctx = TestingFactory.create_context(doc_type="writer")
    ctx.doc.getGraphicObjects = MagicMock()
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=1000, Height=1000)
    graphic.getPropertyValue.side_effect = lambda name: MagicMock()
    anchor = MagicMock()
    anchor.getStart.return_value = MagicMock()
    graphic.getAnchor.return_value = anchor

    saved_cursor = MagicMock()
    vc = MagicMock()
    vc.getPage.return_value = 0
    controller = MagicMock()
    controller.getViewCursor.return_value = vc
    ctx.doc.getCurrentController = MagicMock(return_value=controller)

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Pic", graphic)]),
        patch("plugin.writer.images.images.cursor_for_writer_view", return_value=(MagicMock(), saved_cursor)),
    ):
        res = ImageList().execute(ctx)

    assert res["status"] == "ok"
    assert len(res["images"]) == 1
    assert "page" not in res["images"][0]


def test_image_set_properties_unknown_hori_orient_no_writes():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageSetProperties().execute(ctx, name="Pic", hori_orient="nowhere")
    assert res["status"] != "ok"
    assert res.get("code") == "INVALID_PARAM"
    graphic.setPropertyValue.assert_not_called()


def test_image_set_properties_rollback_on_failure():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    psi = MagicMock()
    psi.hasPropertyByName.return_value = True
    graphic.getPropertySetInfo.return_value = psi

    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    titles: list[str] = []

    def _enter(title):
        titles.insert(0, title)

    undo_mgr.enterUndoContext.side_effect = _enter
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: list(titles)
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    def _set_prop(name, val):
        if name == "Description":
            raise RuntimeError("disk full on description")

    graphic.setPropertyValue.side_effect = _set_prop
    graphic.getSize.return_value = MagicMock(Width=100, Height=100)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
    ):
        res = ImageSetProperties().execute(ctx, name="Pic", title="New Title", description="New Desc")

    assert res["status"] != "ok"
    assert res.get("code") == "SET_PROPERTIES_FAILED"
    undo_mgr.undo.assert_called_once()


def test_image_set_properties_success_does_not_undo():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=100, Height=100)
    psi = MagicMock()
    psi.hasPropertyByName.return_value = True
    graphic.getPropertySetInfo.return_value = psi

    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    titles = ["WriterAgent: Set image properties"]
    undo_mgr.getAllUndoActionTitles.return_value = titles
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
    ):
        res = ImageSetProperties().execute(ctx, name="Pic", title="New Title")

    assert res["status"] == "ok"
    undo_mgr.undo.assert_not_called()


def test_image_set_properties_enter_fails_is_undo_unavailable():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    graphic.getSize.return_value = MagicMock(Width=100, Height=100)
    psi = MagicMock()
    psi.hasPropertyByName.return_value = True
    graphic.getPropertySetInfo.return_value = psi

    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_mgr.enterUndoContext.side_effect = RuntimeError("cannot enter")
    undo_mgr.getAllUndoActionTitles.return_value = ["User previous action"]
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    graphic.setPropertyValue.side_effect = RuntimeError("set failed")

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
    ):
        res = ImageSetProperties().execute(ctx, name="Pic", title="New Title")

    assert res["status"] != "ok"
    assert res.get("code") == "UNDO_UNAVAILABLE"
    graphic.setPropertyValue.assert_not_called()
    undo_mgr.undo.assert_not_called()


def test_image_set_properties_single_undo_context():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    psi = MagicMock()
    psi.hasPropertyByName.return_value = True
    graphic.getPropertySetInfo.return_value = psi
    size_mock = MagicMock(Width=100, Height=100)
    graphic.getSize.return_value = size_mock

    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_mgr.getAllUndoActionTitles.return_value = []
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
    ):
        res = ImageSetProperties().execute(ctx, name="Pic", width_mm=20, title="New Title")

    assert res["status"] == "ok"
    undo_mgr.enterUndoContext.assert_called_once_with("WriterAgent: Set image properties")
    undo_mgr.leaveUndoContext.assert_called_once()


def test_image_set_properties_shape_without_size_property_uses_setsize():
    ctx = TestingFactory.create_context(doc_type="draw")
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_mgr.getAllUndoActionTitles.return_value = []
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    graphic = MagicMock(spec=["getSize", "setSize", "getPropertySetInfo", "getPropertyValue", "setPropertyValue"])
    graphic.getSize.return_value = MagicMock(Width=2000, Height=1000)
    psi = MagicMock()
    psi.hasPropertyByName.return_value = False
    graphic.getPropertySetInfo.return_value = psi

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images.visual_helpers.has_uno_property", return_value=False),
        patch("plugin.writer.images.images.visual_helpers.safe_set_property", return_value=False),
        patch("plugin.writer.images.images.visual_helpers.safe_try_method", return_value=True) as safe_try,
    ):
        res = ImageSetProperties().execute(ctx, name="DrawShape", width_mm=30)

    assert res["status"] == "ok"
    assert "size" in res["updated"]
    safe_try.assert_called_once()


def test_image_set_properties_draw_shape_unsupported_properties_rejected():
    ctx = TestingFactory.create_context(doc_type="draw")
    graphic = MagicMock()

    def _has_prop(obj, name):
        return name not in ("AnchorType", "GraphicCrop")

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images.visual_helpers.has_uno_property", side_effect=_has_prop),
    ):
        res = ImageSetProperties().execute(ctx, name="DrawShape", anchor_type=0, width_mm=20)
        assert res["status"] != "ok"
        assert res.get("code") == "UNSUPPORTED_DOC_TYPE"
        graphic.setPropertyValue.assert_not_called()

        res2 = ImageSetProperties().execute(ctx, name="DrawShape", crop_top_mm=5, width_mm=20)
        assert res2["status"] != "ok"
        assert res2.get("code") == "UNSUPPORTED_DOC_TYPE"
        graphic.setPropertyValue.assert_not_called()


def test_image_set_properties_rejects_negative_crop():
    from plugin.framework.tool import prepare_tool_call

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageSetProperties().execute(ctx, name="Pic", crop_top_mm=-1)
        assert res["status"] != "ok"
        assert res.get("code") == "INVALID_PARAM"
        assert res.get("details", {}).get("parameter") == "crop_top_mm"
        graphic.setPropertyValue.assert_not_called()

    prep = prepare_tool_call(ImageSetProperties(), ctx, "image_set_properties", {"name": "Pic", "crop_right_mm": True})
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"
    assert "crop_right_mm" in prep.error["message"]


def test_image_set_properties_tiny_width_mm_clamps_to_one_unit():
    ctx = TestingFactory.create_context(doc_type="writer")
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_mgr.getAllUndoActionTitles.return_value = []
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    graphic = MagicMock()
    psi = MagicMock()
    psi.hasPropertyByName.return_value = True
    graphic.getPropertySetInfo.return_value = psi

    recorded_sizes: list[Any] = []

    def _record_size(target, prop, val):
        if prop == "Size":
            recorded_sizes.append(val)
        return True

    graphic.getSize.return_value = MagicMock(Width=100, Height=100)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.writer.images.images.visual_helpers.safe_set_property", side_effect=_record_size),
        patch.object(sys.modules["com.sun.star.awt"], "Size", MagicMock, create=True),
    ):
        res = ImageSetProperties().execute(ctx, name="Pic", width_mm=0.001)
        assert res["status"] == "ok"
        assert len(recorded_sizes) == 1
        assert recorded_sizes[0].Width == 1


def test_image_generate_temp_files_cleaned_up_after_insert():
    ctx = TestingFactory.create_context(doc_type="writer")
    temp_dir = tempfile.gettempdir()
    p1 = os.path.join(temp_dir, "wa-img-test1.png")
    p2 = os.path.join(temp_dir, "wa-img-test2.png")
    with open(p1, "wb") as f:
        f.write(b"data1")
    with open(p2, "wb") as f:
        f.write(b"data2")

    svc = MagicMock()
    svc.generate_image.return_value = ([p1, p2], None)

    def _run(fn, *args, **kwargs):
        return fn(*args)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image"),
    ):
        res = ImageGenerate().execute(ctx, prompt="test")

    assert res["status"] == "ok"
    assert not os.path.exists(p1)
    assert not os.path.exists(p2)


def test_image_generate_temp_files_cleaned_up_on_insert_exception():
    ctx = TestingFactory.create_context(doc_type="writer")
    temp_dir = tempfile.gettempdir()
    p1 = os.path.join(temp_dir, "wa-img-test-exc.png")
    with open(p1, "wb") as f:
        f.write(b"data")

    svc = MagicMock()
    svc.generate_image.return_value = ([p1], None)

    def _run(fn, *args, **kwargs):
        return fn(*args)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.visual_helpers.selected_graphic_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image", side_effect=RuntimeError("insert boom")),
    ):
        try:
            ImageGenerate().execute(ctx, prompt="test")
        except RuntimeError:
            pass

    assert not os.path.exists(p1)


def test_download_image_to_cache_rejects_non_http_schemes():
    with patch("plugin.writer.images.image_utils.sync_request") as mock_sync:
        for bad_url in ("file:///etc/passwd", "ftp://example.com/pic.png", "data:image/png;base64,abc"):
            with pytest.raises(ValueError) as exc_info:
                _download_image_to_cache(bad_url)
            assert "Unsupported URL scheme" in str(exc_info.value)
        mock_sync.assert_not_called()


def test_download_image_to_cache_html_body_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))

    with patch(
        "plugin.writer.images.image_utils.sync_request",
        return_value=HttpResult(200, b"<!DOCTYPE html><html><body>Error</body></html>", "text/html; charset=utf-8"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            _download_image_to_cache("https://example.com/fake.png")
        assert "Invalid Content-Type" in str(exc_info.value)
        assert list(tmp_path.iterdir()) == []


def test_image_insert_non_writer_with_paragraph_or_locator_returns_unsupported():
    ctx = TestingFactory.create_context(doc_type="calc")

    def _run(fn, *args, **kwargs):
        return fn(*args)

    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="calc"),
        patch("plugin.writer.images.images.insert_image_at_locator") as mock_insert,
    ):
        res = ImageInsert().execute(ctx, path="/tmp/test.png", paragraph=2)
        assert res["status"] != "ok"
        assert res.get("code") == "UNSUPPORTED_DOC_TYPE"
        mock_insert.assert_not_called()

        res2 = ImageInsert().execute(ctx, path="/tmp/test.png", locator="heading_text:Introduction")
        assert res2["status"] != "ok"
        assert res2.get("code") == "UNSUPPORTED_DOC_TYPE"
        mock_insert.assert_not_called()


def test_image_generate_preserves_large_frame_units_without_capping():
    """Frame sizes like 16,934 x 12,698 and 13,500 x 10,125 must not be capped to 135 mm on edit."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/edited.png"], None)

    for w_units, h_units in [(16934, 12698), (13500, 10125), (100, 100)]:
        target_obj = MagicMock()
        frame_size = MagicMock(Width=w_units, Height=h_units)
        target_obj.getSize.return_value = frame_size

        def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
            patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=target_obj),
            patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"pngdata"),
            patch("plugin.doc.visual_helpers.graphic_from_object", return_value=None),
            patch("plugin.writer.images.images.ImageService", return_value=svc),
            patch("plugin.writer.images.images.replace_graphic_source", return_value=True) as replace,
            patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_all_false),
        ):
            res = ImageGenerate().execute(ctx, prompt="test")
        assert res["status"] == "ok"
        replace.assert_called_once_with(
            ctx.ctx,
            ctx.doc,
            target_obj,
            "/tmp/edited.png",
            w_units,
            h_units,
            title="test",
            description="Edited by endpoint",
        )
        insert.assert_not_called()


def test_image_generate_replaces_original_target_even_if_selection_changed():
    """If a different graphic is selected while generate_image runs, the original target is still replaced."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    original_target = MagicMock(name="original_target")
    different_target = MagicMock(name="different_target")

    current_selection = [original_target]

    def _mock_generate(*args, **kwargs):
        # Selection changed while generating
        current_selection[0] = different_target
        return (["/tmp/edited.png"], None)

    svc.generate_image.side_effect = _mock_generate

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    frame_size = MagicMock(Width=5000, Height=3000)
    original_target.getSize.return_value = frame_size

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.doc.visual_helpers.selected_graphic_object", side_effect=lambda doc: current_selection[0]),
        patch("plugin.writer.images.images.export_graphic_object_to_bytes", return_value=b"pngdata"),
        patch("plugin.doc.visual_helpers.graphic_from_object", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True) as replace,
        patch("plugin.writer.images.images.get_config", side_effect=_image_tool_config_all_false),
    ):
        res = ImageGenerate().execute(ctx, prompt="wizard")

    assert res["status"] == "ok"
    assert replace.call_args[0][2] is original_target


def test_image_insert_sizing_rules():
    ctx = TestingFactory.create_context(doc_type="writer")

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    inserted_sizes = []

    def _mock_insert(context, doc, path, width_mm=None, height_mm=None, **kwargs):
        inserted_sizes.append((width_mm, height_mm))
        return MagicMock()

    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.insert_image_at_locator", side_effect=_mock_insert),
    ):
        # 1. 200x100 PNG with both sizes omitted lands at 80 x 40 mm
        with patch("plugin.writer.images.image_tools.graphic_pixel_size_from_path", return_value=(200, 100)):
            inserted_sizes.clear()
            res = ImageInsert().execute(ctx, path="/tmp/test.png")
            assert res["status"] == "ok"
            assert inserted_sizes == [(80.0, 40.0)]

        # 2. 100x200 PNG with both sizes omitted lands at 40 x 80 mm
        with patch("plugin.writer.images.image_tools.graphic_pixel_size_from_path", return_value=(100, 200)):
            inserted_sizes.clear()
            res = ImageInsert().execute(ctx, path="/tmp/test.png")
            assert res["status"] == "ok"
            assert inserted_sizes == [(40.0, 80.0)]

        # 3. Only width_mm=40 scales height proportionally (200x100 -> 40 x 20 mm)
        with patch("plugin.writer.images.image_tools.graphic_pixel_size_from_path", return_value=(200, 100)):
            inserted_sizes.clear()
            res = ImageInsert().execute(ctx, path="/tmp/test.png", width_mm=40)
            assert res["status"] == "ok"
            assert inserted_sizes == [(40.0, 20.0)]

        # 4. Both 30 and 50 stay 30 and 50 mm (even if stretching)
        with patch("plugin.writer.images.image_tools.graphic_pixel_size_from_path", return_value=(200, 100)):
            inserted_sizes.clear()
            res = ImageInsert().execute(ctx, path="/tmp/test.png", width_mm=30, height_mm=50)
            assert res["status"] == "ok"
            assert inserted_sizes == [(30.0, 50.0)]

        # 5. Unreadable pixels (e.g. None or 0) stay 80 x 80 mm
        with patch("plugin.writer.images.image_tools.graphic_pixel_size_from_path", return_value=(None, None)):
            inserted_sizes.clear()
            res = ImageInsert().execute(ctx, path="/tmp/test.svg")
            assert res["status"] == "ok"
            assert inserted_sizes == [(80.0, 80.0)]

        # 6. Header insert uses derived size
        with (
            patch("plugin.writer.images.image_tools.graphic_pixel_size_from_path", return_value=(200, 100)),
            patch("plugin.writer.images.images.insert_image_into_header_footer") as mock_hf,
        ):
            res = ImageInsert().execute(ctx, path="/tmp/test.png", target="header")
            assert res["status"] == "ok"
            assert mock_hf.call_args.kwargs["width_mm"] == 80.0
            assert mock_hf.call_args.kwargs["height_mm"] == 40.0


def test_tools_reject_non_positive_sizes():
    ctx = TestingFactory.create_context(doc_type="writer")

    # ImageInsert rejects width_mm / height_mm <= 0
    assert ImageInsert().execute(ctx, path="/tmp/a.png", width_mm=0)["code"] == "INVALID_PARAM"
    assert ImageInsert().execute(ctx, path="/tmp/a.png", width_mm=-1)["code"] == "INVALID_PARAM"
    assert ImageInsert().execute(ctx, path="/tmp/a.png", height_mm=0)["code"] == "INVALID_PARAM"
    assert ImageInsert().execute(ctx, path="/tmp/a.png", height_mm=-5)["code"] == "INVALID_PARAM"

    # ImageReplace rejects width_mm / height_mm <= 0
    assert ImageReplace().execute(ctx, name="Pic", path="/tmp/a.png", width_mm=0)["code"] == "INVALID_PARAM"
    assert ImageReplace().execute(ctx, name="Pic", path="/tmp/a.png", width_mm=-2)["code"] == "INVALID_PARAM"
    assert ImageReplace().execute(ctx, name="Pic", path="/tmp/a.png", height_mm=0)["code"] == "INVALID_PARAM"
    assert ImageReplace().execute(ctx, name="Pic", path="/tmp/a.png", height_mm=-10)["code"] == "INVALID_PARAM"

    # ImageSetProperties rejects width_mm / height_mm <= 0
    assert ImageSetProperties().execute(ctx, name="Pic", width_mm=0)["code"] == "INVALID_PARAM"
    assert ImageSetProperties().execute(ctx, name="Pic", width_mm=-1)["code"] == "INVALID_PARAM"
    assert ImageSetProperties().execute(ctx, name="Pic", height_mm=0)["code"] == "INVALID_PARAM"
    assert ImageSetProperties().execute(ctx, name="Pic", height_mm=-3)["code"] == "INVALID_PARAM"

    # ImageGenerate rejects base_size, width, height <= 0
    assert ImageGenerate().execute(ctx, prompt="p", base_size=0)["code"] == "INVALID_PARAM"
    assert ImageGenerate().execute(ctx, prompt="p", base_size=-50)["code"] == "INVALID_PARAM"
    assert ImageGenerate().execute(ctx, prompt="p", width=0)["code"] == "INVALID_PARAM"
    assert ImageGenerate().execute(ctx, prompt="p", width=-10)["code"] == "INVALID_PARAM"
    assert ImageGenerate().execute(ctx, prompt="p", height=0)["code"] == "INVALID_PARAM"
    assert ImageGenerate().execute(ctx, prompt="p", height=-10)["code"] == "INVALID_PARAM"


def test_image_insert_vector_proportional_fallback_from_100th_mm():
    ctx = TestingFactory.create_context(doc_type="writer")
    inserted_sizes: list[tuple[Any, Any]] = []

    def _mock_insert(context, doc, path, width_mm=None, height_mm=None, **kwargs):
        inserted_sizes.append((width_mm, height_mm))
        return MagicMock()

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    provider_graphic = MagicMock()
    provider_graphic.getPropertyValue.side_effect = lambda prop: (
        SimpleNamespace(Width=0, Height=0)
        if prop == "SizePixel"
        else SimpleNamespace(Width=2540, Height=5080)
        if prop == "Size100thMM"
        else None
    )
    gp_mock = MagicMock()
    gp_mock.queryGraphic.return_value = provider_graphic
    ctx.ctx.ServiceManager = MagicMock()
    ctx.ctx.ServiceManager.createInstanceWithContext.return_value = gp_mock

    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.framework.queue_executor.main.is_current", return_value=False),
        patch("plugin.framework.queue_executor.main.call", side_effect=_run),
        patch("plugin.writer.images.images.insert_image_at_locator", side_effect=_mock_insert),
    ):
        res = ImageInsert().execute(ctx, path="/tmp/vector.svg")
        assert res["status"] == "ok"
        # 2540x5080 1/100mm -> 96x192 px -> height 80mm, width 40mm
        assert inserted_sizes == [(40.0, 80.0)]


def test_image_tools_calc_sheet_index_and_ambiguity():
    from plugin.doc.visual_helpers import AmbiguousGraphicNameError

    ctx = TestingFactory.create_context(doc_type="calc")
    ambig_err = AmbiguousGraphicNameError("Several shapes named 'Pic' found on sheets [0, 1]", sheet_indices=[0, 1])

    # 1. ImageGetInfo returns AMBIGUOUS_NAME with sheet_indices
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=ambig_err):
        res = ImageGetInfo().execute(ctx, name="Pic")
        assert res["status"] != "ok"
        assert res.get("code") == "AMBIGUOUS_NAME"
        assert res.get("details", {}).get("sheet_indices") == [0, 1]

    # 2. ImageSetProperties returns AMBIGUOUS_NAME with sheet_indices
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=ambig_err):
        res = ImageSetProperties().execute(ctx, name="Pic", width_mm=20)
        assert res["status"] != "ok"
        assert res.get("code") == "AMBIGUOUS_NAME"
        assert res.get("details", {}).get("sheet_indices") == [0, 1]

    # 3. ImageCropAndHighlight returns AMBIGUOUS_NAME with sheet_indices
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=ambig_err):
        res = ImageCropAndHighlight().execute(ctx, name="Pic", crop_box=[0, 0, 10, 10])
        assert res["status"] != "ok"
        assert res.get("code") == "AMBIGUOUS_NAME"
        assert res.get("details", {}).get("sheet_indices") == [0, 1]

    # 4. ImageDelete returns AMBIGUOUS_NAME with sheet_indices
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=ambig_err):
        res = ImageDelete().execute(ctx, name="Pic")
        assert res["status"] != "ok"
        assert res.get("code") == "AMBIGUOUS_NAME"
        assert res.get("details", {}).get("sheet_indices") == [0, 1]

    # 5. ImageReplace returns AMBIGUOUS_NAME with sheet_indices
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=ambig_err):
        res = ImageReplace().execute(ctx, name="Pic", path="/tmp/pic.png")
        assert res["status"] != "ok"
        assert res.get("code") == "AMBIGUOUS_NAME"
        assert res.get("details", {}).get("sheet_indices") == [0, 1]

    # 6. With sheet_index passed, get_graphic_object_by_name receives sheet_index
    calls = []

    def _mock_get(doc, name, sheet_index=None):
        calls.append((name, sheet_index))
        mock_obj = MagicMock()
        mock_obj.getSize.return_value = MagicMock(Width=1000, Height=500)
        return mock_obj

    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_mgr.getAllUndoActionTitles.return_value = []
    ctx.doc.getUndoManager = MagicMock(return_value=undo_mgr)

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=_mock_get),
        patch("plugin.doc.visual_helpers.safe_set_property", return_value=True),
    ):
        res = ImageSetProperties().execute(ctx, name="Pic", width_mm=20, sheet_index=1)
        assert res["status"] == "ok"
        assert calls == [("Pic", 1)]


def test_image_get_info_draw_skips_paragraph_lookup():
    ctx = TestingFactory.create_context(doc_type="draw")
    graphic = _graphic_for_info()
    ctx.services.document = MagicMock()
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="Pic")
    assert res["status"] == "ok"
    assert res["paragraph_index"] == -1
    ctx.services.document.get_paragraph_ranges.assert_not_called()


@pytest.mark.parametrize(
    "sync_outcome,cancel_first",
    [
        (NetworkError("LLM request aborted by Stop", code="STOPPED"), False),
        (TimeoutError("timed out"), True),
    ],
)
def test_download_for_tool_stop_returns_user_stopped(tmp_path, monkeypatch, sync_outcome, cancel_first):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))
    scope = SendCancellation()
    if cancel_first:
        scope.cancel()
    ctx = TestingFactory.create_context(doc_type="writer", send_cancellation=scope)

    with patch("plugin.writer.images.image_utils.sync_request", side_effect=sync_outcome):
        res = ImageDownload().execute(ctx, url="https://example.com/pic.png")
    assert res["status"] == "error"
    assert res.get("code") == "USER_STOPPED"
    assert list(tmp_path.iterdir()) == []


def test_download_image_to_cache_expired_after_24h(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))

    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    downloads = {"count": 0}

    def _fake_sync(*_args, **_kwargs):
        downloads["count"] += 1
        return HttpResult(200, png_bytes, "image/png")

    with patch("plugin.writer.images.image_utils.sync_request", side_effect=_fake_sync):
        path1 = _download_image_to_cache("https://example.com/exp.png")
        assert downloads["count"] == 1
        assert path1.endswith(".png")

        path2 = _download_image_to_cache("https://example.com/exp.png")
        assert downloads["count"] == 1
        assert path1 == path2

        old_time = time.time() - (25 * 3600)
        os.utime(path1, (old_time, old_time))

        _download_image_to_cache("https://example.com/exp.png")
        assert downloads["count"] == 2


def test_download_image_to_cache_fetches_through_sync_request(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        "plugin.writer.images.image_utils.get_config",
        lambda key: 42 if key == "request_timeout" else 1024,
    )
    scope = SendCancellation()
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    with patch(
        "plugin.writer.images.image_utils.sync_request",
        return_value=HttpResult(200, png_bytes, "image/png"),
    ) as mock_sync:
        path = _download_image_to_cache("https://example.com/test.png", cancel=scope)
    assert os.path.isfile(path)
    mock_sync.assert_called_once_with(
        "https://example.com/test.png",
        parse_json=False,
        include_meta=True,
        timeout=42,
        max_bytes=IMAGE_DOWNLOAD_MAX_BYTES,
        cancel=scope,
        status_callback=None,
    )


def test_download_image_to_cache_loopback_end_to_end(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    monkeypatch.setattr("plugin.writer.images.image_tools.image_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        "plugin.writer.images.image_utils.get_config",
        lambda key: 15 if key == "request_timeout" else 1024,
    )

    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    redirect_hits = 0

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal redirect_hits
            if self.path == "/start":
                redirect_hits += 1
                self.send_response(302)
                self.send_header("Location", "/image.png")
                self.end_headers()
            elif self.path == "/image.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png_bytes)))
                self.end_headers()
                self.wfile.write(png_bytes)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        url = f"http://{host}:{port}/start"
        path1 = _download_image_to_cache(url)
        assert os.path.isfile(path1)
        assert path1.endswith(".png")
        with open(path1, "rb") as fh:
            assert fh.read() == png_bytes
        assert redirect_hits == 1

        path2 = _download_image_to_cache(url)
        assert path1 == path2
        assert redirect_hits == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def test_image_delete_draw_miss_has_list_hint():
    ctx = TestingFactory.create_context(doc_type="draw")
    graphic = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.doc.visual_helpers.remove_graphic_from_draw_pages", return_value=False),
    ):
        res = ImageDelete().execute(ctx, name="MissingPic")
    assert res["status"] == "error"
    assert res["code"] == "IMAGE_NOT_FOUND"
    assert "details" in res
    assert res["details"]["hint"] == "Call image_list to list names."


def test_prune_image_cache_drops_oldest(tmp_path):
    from plugin.writer.images.images import _prune_image_cache

    f1 = tmp_path / "img1.png"
    f2 = tmp_path / "img2.png"
    f1.write_bytes(b"x" * 1000)
    f2.write_bytes(b"y" * 1000)
    os.utime(str(f1), (1000, 1000))
    os.utime(str(f2), (2000, 2000))

    with patch("plugin.writer.images.images.IMAGE_CACHE_MAX_BYTES", 1500):
        _prune_image_cache(str(tmp_path), keep_path=str(f2))

    assert not f1.exists()
    assert f2.exists()

