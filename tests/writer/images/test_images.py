# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for images.py helpers (no LibreOffice required)."""
import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

if "com.sun.star.text" in sys.modules and not hasattr(sys.modules["com.sun.star.text"], "GraphicCrop"):
    setattr(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock)

from plugin.tests.testing_utils import TestingFactory

from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE
from plugin.doc.document_research_tools import ListNearbyFiles
from plugin.doc.visual_helpers import anchor_type_index
from plugin.framework.errors import DocumentDisposedError
from plugin.framework.tool import ToolContext, ToolRegistry
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
    _object_size,
    _replacement_size_units,
    _resolve_crop_edges,
    _resolve_orient,
    resolve_image_generate_is_edit,
)
from plugin.writer.specialized_base import SpecializedWorkflowFinished
from tests.chatbot.test_tool_loop import DummyCalcSpecialTool


def test_resolve_orient_named_positions():
    # Friendly names resolve to a (mocked) UNO constant, never an error.
    for name in ("left", "center", "right", "LEFT", "Right"):
        const, err = _resolve_orient(name, "hori")
        assert err is None and const is not None
    for name in ("top", "center", "bottom"):
        const, err = _resolve_orient(name, "vert")
        assert err is None and const is not None


def test_resolve_orient_centre_british_spelling():
    const, err = _resolve_orient("centre", "hori")
    assert err is None and const is not None


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


def test_image_insert_schema_includes_first_page_targets():
    enum = ImageInsert.parameters["properties"]["target"]["enum"]
    assert "header_first" in enum
    assert "footer_first" in enum
    assert "header" in enum and "footer" in enum


def test_image_insert_routes_header_first_to_region_helper():
    # Shared target=header never reaches HeaderTextFirst; the tool must
    # pass header_first through to the same helper page_get/set use.
    graphic = MagicMock()
    graphic.getName.return_value = "Graphic1"
    placed = {
        "graphic": graphic,
        "style_name": "Standard",
        "region": "header_first",
        "auto_height": True,
    }
    ctx = TestingFactory.create_context(doc_type="writer")
    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.insert_image_into_header_footer", return_value=placed) as insert,
    ):
        res = ImageInsert().execute(
            ctx, path="/tmp/logo.png", target="header_first", width_mm=40, height_mm=20,
        )
    assert res["status"] == "ok"
    assert res["target"] == "header_first"
    assert insert.call_args.args[2] == "header_first"


def test_image_insert_routes_footer_first_to_region_helper():
    graphic = MagicMock()
    graphic.getName.return_value = "Graphic1"
    placed = {
        "graphic": graphic,
        "style_name": "Standard",
        "region": "footer_first",
        "auto_height": True,
    }
    ctx = TestingFactory.create_context(doc_type="writer")
    with (
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.insert_image_into_header_footer", return_value=placed) as insert,
    ):
        res = ImageInsert().execute(ctx, path="/tmp/logo.png", target="footer_first")
    assert res["status"] == "ok"
    assert res["target"] == "footer_first"
    assert insert.call_args.args[2] == "footer_first"


def test_image_generate_default_base_size_is_1024():
    assert ImageGenerate.parameters["properties"]["base_size"]["default"] == 1024
    assert DEFAULT_IMAGE_BASE_SIZE == 1024


def test_image_generate_forwards_stop_checker():
    ctx = TestingFactory.create_context(doc_type="writer")
    stop_checker = MagicMock(return_value=False)
    ctx.stop_checker = stop_checker

    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image"),
        patch("plugin.writer.images.images.get_config_int", return_value=1024),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
        patch("plugin.writer.images.images.get_config_str", return_value="square"),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a stop checked cat")
    assert res["status"] == "ok"
    assert svc.generate_image.call_args.kwargs.get("stop_checker") is stop_checker


def test_image_generate_inserts_even_if_stopped_after_download():
    ctx = TestingFactory.create_context(doc_type="writer")
    stop_checker = MagicMock(return_value=True)
    ctx.stop_checker = stop_checker

    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image") as mock_insert,
        patch("plugin.writer.images.images.replace_image_in_place"),
        patch("plugin.writer.images.images.get_config_int", return_value=1024),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
        patch("plugin.writer.images.images.get_config_str", return_value="square"),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a cat")
    assert res["status"] == "ok"
    assert "inserted" in res["message"]
    mock_insert.assert_called_once()


def test_image_generate_rejects_non_integer_base_size():
    ok, err = ImageGenerate().validate(prompt="a tabby cat", base_size="nope")
    assert ok is False
    assert err is not None
    assert "integer" in err


def test_resolve_image_generate_is_edit_defaults_to_selection():
    assert resolve_image_generate_is_edit(None, selection_available=True) is True
    assert resolve_image_generate_is_edit("", selection_available=True) is True
    assert resolve_image_generate_is_edit("  ", selection_available=True) is True
    assert resolve_image_generate_is_edit("selection", selection_available=True) is True
    assert resolve_image_generate_is_edit("SELECTION", selection_available=False) is True
    assert resolve_image_generate_is_edit(None, selection_available=False) is False
    assert resolve_image_generate_is_edit("", selection_available=False) is False


def test_image_generate_omitted_source_edits_when_selected():
    """Omitted source_image + selected graphic → img2img replace, not a new insert."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/edited.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value="b64sel"),
        patch("plugin.writer.images.images.get_selected_image_pixel_size", return_value=(1024, 1024)),
        patch("plugin.writer.images.images.get_selected_image_dimensions_px", return_value=(640, 480)),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_image_in_place", return_value=True) as replace,
        patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config_int", return_value=512),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
        patch("plugin.writer.images.images.get_config_str", return_value="square"),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="make it look like a wizard")
    assert res["status"] == "ok"
    assert "edited" in res["message"].lower()
    assert svc.generate_image.call_args.kwargs.get("source_image") == "b64sel"
    assert svc.generate_image.call_args.kwargs.get("width") == 1024
    assert svc.generate_image.call_args.kwargs.get("height") == 1024

    replace_args = replace.call_args[0]
    assert replace_args[3] == 640 # width
    assert replace_args[4] == 480 # height

    replace.assert_called_once()
    insert.assert_not_called()


def test_image_generate_omitted_source_edits_when_selected_fallback_to_display_size():
    """If native size is not available, uses the display size for generating and replacing."""
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/edited.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value="b64sel"),
        patch("plugin.writer.images.images.get_selected_image_pixel_size", return_value=(None, None)),
        patch("plugin.writer.images.images.get_selected_image_dimensions_px", return_value=(640, 480)),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_image_in_place", return_value=True) as replace,
        patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config_int", return_value=512),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
    ):
        res = ImageGenerate().execute(ctx, prompt="make it look like a wizard")

    assert res["status"] == "ok"
    assert "edited" in res["message"].lower()
    assert svc.generate_image.call_args.kwargs.get("source_image") == "b64sel"
    assert svc.generate_image.call_args.kwargs.get("width") == 640
    assert svc.generate_image.call_args.kwargs.get("height") == 480

    replace_args = replace.call_args[0]
    assert replace_args[3] == 640 # width
    assert replace_args[4] == 480 # height

    replace.assert_called_once()
    insert.assert_not_called()


def test_image_generate_omitted_source_creates_when_no_selection():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_image_in_place") as replace,
        patch("plugin.writer.images.images.insert_image") as insert,
        patch("plugin.writer.images.images.get_config_int", return_value=512),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
        patch("plugin.writer.images.images.get_config_str", return_value="square"),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a tabby cat")
    assert res["status"] == "ok"
    assert "generated" in res["message"].lower()
    assert "source_image" not in svc.generate_image.call_args.kwargs
    insert.assert_called_once()
    replace.assert_not_called()
    assert svc.generate_image.call_args.kwargs.get("aspect_ratio") == "square"


def test_only_top_overrides_others_preserved():
    # T3 (R3): set_image_properties really crops now (GraphicCrop). Pin the mm -> 1/100mm edge math
    # and the preserve-unspecified-edges behavior. No LibreOffice required. Applying the struct is live.
    # current crop = (top, bottom, left, right) in 1/100mm
    out = _resolve_crop_edges({"crop_top_mm": 5}, (0, 200, 300, 400))
    assert out == (500, 200, 300, 400)


def test_all_edges_mm_to_hundredths():
    out = _resolve_crop_edges(
        {"crop_top_mm": 1, "crop_bottom_mm": 2, "crop_left_mm": 3, "crop_right_mm": 4.5}, (0, 0, 0, 0)
    )
    assert out == (100, 200, 300, 450)


def test_none_keeps_current():
    assert _resolve_crop_edges({}, (10, 20, 30, 40)) == (10, 20, 30, 40)


def test_rounding():
    assert _resolve_crop_edges({"crop_left_mm": 1.234}, (0, 0, 0, 0)) == (0, 0, 123, 0)


# --- list nearby image files (from test_list_nearby_image_files.py) ---

def test_list_nearby_image_files_calls_backend_with_images_kind():
    tool = ImageListNearbyFiles()
    ctx = ToolContext(MagicMock(), MagicMock(), "writer", MagicMock())

    expected = {"status": "ok", "files": [], "truncated": False}

    with patch("plugin.writer.images.images.list_nearby_files", return_value=expected) as mock_list:
        with patch("plugin.writer.images.images.execute_on_main_thread", side_effect=lambda fn: fn()):
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


def test_image_download_verifies_tls_by_default():
    import inspect

    assert inspect.signature(_download_image_to_cache).parameters["verify_ssl"].default is True
    description = ImageDownload.parameters["properties"]["verify_ssl"]["description"]
    assert "default: true" in description
    assert "default: false" not in description


def test_download_image_to_cache_does_not_disable_tls(tmp_path, monkeypatch):
    import ssl

    from plugin.writer.images import images as images_mod

    monkeypatch.setattr("plugin.writer.images.image_tools._image_cache_dir", lambda: str(tmp_path))
    seen = {}

    class _Resp:
        def __init__(self) -> None:
            self._chunks = [b"\x89PNG"]
            self.headers = {}

        def read(self, _size: int = -1) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    def _open(request, context=None, timeout=None):
        seen["context"] = context
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", _open)
    monkeypatch.setattr(
        "plugin.writer.images.images.get_config_int",
        lambda key, default=0: 15 if key == "request_timeout" else 1024,
    )
    path = _download_image_to_cache("https://example.com/logo.png")
    assert path.startswith(str(tmp_path))
    assert seen["context"] is None
    assert not isinstance(seen["context"], ssl.SSLContext)
    assert seen["timeout"] == 15
    with open(path, "rb") as fh:
        assert fh.read() == b"\x89PNG"


def test_image_download_omitted_verify_ssl_requests_verification():
    ctx = TestingFactory.create_context(doc_type="writer")
    with patch("plugin.writer.images.images._download_image_to_cache", return_value="/tmp/logo.png") as download:
        result = ImageDownload().execute(ctx, url="https://example.com/logo.png")
    assert result["status"] == "ok"
    assert download.call_args.kwargs["verify_ssl"] is True


def test_image_replace_https_download_verifies_tls():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", return_value="/tmp/logo.png") as download,
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True),
    ):
        result = ImageReplace().execute(ctx, name="Logo", path="https://example.com/logo.png")
    assert result["status"] == "ok"
    assert download.call_args.args == ("https://example.com/logo.png",)
    assert download.call_args.kwargs.get("verify_ssl", True) is True


def test_image_generate_marshals_insert_unscoped():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/fake.png"], None)
    scopes_passed: list[Any] = []

    def _track_run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        scopes_passed.append(bound_scope)
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_track_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.insert_image"),
        patch("plugin.writer.images.images.get_config_int", return_value=1024),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
        patch("plugin.writer.images.images.get_config_str", return_value="square"),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        result = ImageGenerate().execute(ctx, prompt="a red circle")
    assert result["status"] == "ok"
    assert None in scopes_passed



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
    with patch("plugin.writer.images.images._get_graphic_object", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="IndexButton")
    assert res["status"] == "ok"
    assert res["hyperlink_url"] == "#ÍNDEX"


def test_image_get_info_hyperlink_url_empty_when_absent():
    # Calc/Draw shapes often have no HyperLinkURL. Missing means no link, not an error.
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = _graphic_for_info()
    with patch("plugin.writer.images.images._get_graphic_object", return_value=graphic):
        res = ImageGetInfo().execute(ctx, name="Plain")
    assert res["status"] == "ok"
    assert res["hyperlink_url"] == ""


def test_object_size_reads_writer_property_and_draw_getsize():
    writer = MagicMock()
    writer.getPropertyValue.return_value = "writer-size"
    assert _object_size(writer) == "writer-size"
    # Draw/Impress shapes have no Size property; image_list used to skip them silently.
    draw = MagicMock()
    draw.getPropertyValue.side_effect = Exception("UnknownPropertyException: Size")
    draw.getSize.return_value = "draw-size"
    assert _object_size(draw) == "draw-size"


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
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images._object_size", return_value=size),
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
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images._object_size", side_effect=RuntimeError("cannot read size")),
        patch("plugin.writer.images.images.replace_graphic_source") as mock_replace,
    ):
        res = ImageReplace().execute_safe(ctx, name="Graphic1", path="/tmp/new.png", width_mm=40)
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "cannot read size" in res.get("message", "")
    assert res.get("code") != "SIZE_UNAVAILABLE"
    mock_replace.assert_not_called()


def test_image_replace_download_failure_is_tool_error():
    import urllib.error

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", side_effect=urllib.error.URLError("timed out")),
    ):
        res = ImageReplace().execute(ctx, name="Graphic1", path="https://example.com/pic.png")
    assert res["status"] != "ok"
    assert res.get("code") == "DOWNLOAD_FAILED"
    assert res.get("message", "").startswith("Could not download image:")

    # A local missing file still returns FILE_NOT_FOUND and does not call the downloader
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
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
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", side_effect=RuntimeError("export")),
    ):
        res = ImageGenerate().execute_safe(ctx, prompt="make it purple", source_image="selection")
    assert res["status"] == "error"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "export" in res.get("message", "")
    assert res.get("code") != "SELECTION_READ_ERROR"


def test_image_delete_draw_ambiguous_name():
    ctx = MagicMock()
    graphic = MagicMock()
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
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


def test_download_cache_hit_skips_second_urlopen(tmp_path, monkeypatch):
    from plugin.writer.images import images as images_mod

    monkeypatch.setattr("plugin.writer.images.image_tools._image_cache_dir", lambda: str(tmp_path))

    class _Resp:
        def __init__(self) -> None:
            self._chunks = [b"\x89PNG"]
            self.headers = {}

        def read(self, _size: int = -1) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", lambda req, context=None, timeout=None: _Resp())
    path1 = _download_image_to_cache("https://example.com/logo.png")
    assert os.path.isfile(path1)

    def _fail_open(*args, **kwargs):
        raise AssertionError("urlopen called on cache hit")

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", _fail_open)
    path2 = _download_image_to_cache("https://example.com/logo.png")
    assert path1 == path2


def test_download_over_cap_is_not_cached(tmp_path, monkeypatch):
    import pytest
    from plugin.writer.images import images as images_mod

    monkeypatch.setattr("plugin.writer.images.image_tools._image_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr("plugin.writer.images.images._IMAGE_DOWNLOAD_MAX_BYTES", 4)

    opens = 0

    class _OverResp:
        def __init__(self) -> None:
            self.headers = {}
            self._chunks = [b"abcdef", b""]

        def read(self, _size: int = -1) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

        def __enter__(self) -> "_OverResp":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    def _open(request, context=None, timeout=None):
        nonlocal opens
        opens += 1
        return _OverResp()

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", _open)

    with pytest.raises(RuntimeError) as exc_info:
        _download_image_to_cache("https://example.com/big.png")
    assert "exceeded" in str(exc_info.value)
    assert list(tmp_path.iterdir()) == []
    assert opens == 1

    with pytest.raises(RuntimeError):
        _download_image_to_cache("https://example.com/big.png")
    assert opens == 2

    # Cover Content-Length: 999999 with the real cap: read must not be called, directory stays empty.
    monkeypatch.setattr("plugin.writer.images.images._IMAGE_DOWNLOAD_MAX_BYTES", 32 * 1024 * 1024)
    read_called = False

    class _CLResp:
        def __init__(self) -> None:
            self.headers = {"Content-Length": "99999999"}

        def read(self, _size: int = -1) -> bytes:
            nonlocal read_called
            read_called = True
            return b"data"

        def __enter__(self) -> "_CLResp":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", lambda req, context=None, timeout=None: _CLResp())
    with pytest.raises(RuntimeError) as exc_info:
        _download_image_to_cache("https://example.com/huge.png")
    assert "exceeded" in str(exc_info.value)
    assert not read_called
    assert list(tmp_path.iterdir()) == []


def test_download_partial_read_is_not_cached(tmp_path, monkeypatch):
    import urllib.error
    import pytest
    from plugin.writer.images import images as images_mod

    monkeypatch.setattr("plugin.writer.images.image_tools._image_cache_dir", lambda: str(tmp_path))
    opens = 0

    class _PartialResp:
        def __init__(self) -> None:
            self.headers = {}
            self._calls = 0

        def read(self, _size: int = -1) -> bytes:
            self._calls += 1
            if self._calls == 1:
                return b"abc"
            raise urllib.error.URLError("reset")

        def __enter__(self) -> "_PartialResp":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    def _open(request, context=None, timeout=None):
        nonlocal opens
        opens += 1
        return _PartialResp()

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", _open)

    with pytest.raises(urllib.error.URLError):
        _download_image_to_cache("https://example.com/part.png")
    assert list(tmp_path.iterdir()) == []
    assert opens == 1

    with pytest.raises(urllib.error.URLError):
        _download_image_to_cache("https://example.com/part.png")
    assert opens == 2


def test_download_empty_body_is_not_cached(tmp_path, monkeypatch):
    import pytest
    from plugin.writer.images import images as images_mod

    monkeypatch.setattr("plugin.writer.images.image_tools._image_cache_dir", lambda: str(tmp_path))

    class _EmptyResp:
        def __init__(self) -> None:
            self.headers = {}

        def read(self, _size: int = -1) -> bytes:
            return b""

        def __enter__(self) -> "_EmptyResp":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", lambda req, context=None, timeout=None: _EmptyResp())

    with pytest.raises(RuntimeError) as exc_info:
        _download_image_to_cache("https://example.com/empty.png")
    assert "empty image download" in str(exc_info.value)
    assert list(tmp_path.iterdir()) == []


def test_image_download_insert_replace_are_async():
    assert ImageDownload().is_async() is True
    assert ImageInsert().is_async() is True
    assert ImageReplace().is_async() is True
    assert ImageList().is_async() is False


def test_image_insert_downloads_before_main_thread():
    ctx = TestingFactory.create_context(doc_type="writer")
    order = []

    def _mock_download(url, **kwargs):
        order.append("download")
        return "/tmp/logo.png"

    def _mock_run_on_main(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
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
        patch("plugin.writer.images.images._run_on_main", side_effect=_mock_run_on_main),
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

    def _mock_run_on_main(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        order.append("main")
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("plugin.writer.images.images._download_image_to_cache", side_effect=_mock_download),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images.replace_graphic_source", return_value=True),
        patch("plugin.writer.images.images._run_on_main", side_effect=_mock_run_on_main),
    ):
        res = ImageReplace().execute(ctx, name="Pic", path="https://example.com/logo.png")

    assert res["status"] == "ok"
    assert order == ["main", "download", "main"]


def test_image_get_info_anchor_type_from_enum_name():
    ctx = TestingFactory.create_context(doc_type="writer")
    extra = {
        "AnchorType": SimpleNamespace(value="AT_PAGE"),
        "HoriOrient": 0,
        "VertOrient": 3,
    }
    graphic = _graphic_for_info(extra)
    with patch("plugin.writer.images.images._get_graphic_object", return_value=graphic):
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
    with patch("plugin.writer.images.images._get_graphic_object", return_value=graphic2):
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
    with patch("plugin.writer.images.images._get_graphic_object", return_value=graphic):
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

    with patch("plugin.writer.images.images._get_graphic_object", return_value=graphic):
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

    def _mock_size(obj):
        if obj is good:
            return good_size
        raise RuntimeError("boom")

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Good", good), ("Bad", bad)]),
        patch("plugin.writer.images.images._object_size", side_effect=_mock_size),
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
    good.getPropertyValue.side_effect = Exception("no Title")
    good.getAnchor.side_effect = Exception("no anchor")

    with (
        patch("plugin.doc.visual_helpers.get_visual_doc_type", return_value="writer"),
        patch("plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Good", good)]),
        patch("plugin.writer.images.images._object_size", side_effect=DocumentDisposedError("closed")),
    ):
        with pytest.raises(DocumentDisposedError):
            ImageList().execute(ctx)


def test_image_set_properties_rejects_anchor_type():
    ctx = TestingFactory.create_context(doc_type="writer")
    ok, err = ImageSetProperties().validate(name="G", anchor_type=9)
    assert not ok

    with patch("plugin.writer.images.images._get_graphic_object") as mock_get:
        res = ImageSetProperties().execute(ctx, name="G", anchor_type=9)
        assert res["code"] == "INVALID_PARAMETER"
        mock_get.assert_not_called()

        res_bool = ImageSetProperties().execute(ctx, name="G", anchor_type=True)
        assert res_bool["code"] == "INVALID_PARAMETER"
        mock_get.assert_not_called()

    ok2, err2 = ImageSetProperties().validate(name="G", anchor_type=2)
    assert ok2


def test_image_replace_size_disposal_propagates():
    import pytest

    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("os.path.isfile", return_value=True),
        patch("plugin.writer.images.images._object_size", side_effect=DocumentDisposedError("closed")),
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
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", side_effect=DocumentDisposedError("closed")),
    ):
        with pytest.raises(DocumentDisposedError):
            ImageGenerate().execute(ctx, prompt="make it purple", source_image="selection")


def test_image_set_properties_size_read_failure_is_tool_execution_error():
    ctx = TestingFactory.create_context(doc_type="writer")
    graphic = MagicMock()
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=graphic),
        patch("plugin.writer.images.images._object_size", side_effect=RuntimeError("cannot read size")),
    ):
        res = ImageSetProperties().execute_safe(ctx, name="G", width_mm=10)
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "cannot read size" in res.get("message", "")
    assert res.get("code") != "SIZE_UNAVAILABLE"
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
    assert res.get("code") != "INSERT_FAILED"


def test_image_crop_bake_failure_is_tool_execution_error():
    if "com.sun.star.text" in sys.modules and not hasattr(sys.modules["com.sun.star.text"], "GraphicCrop"):
        setattr(sys.modules["com.sun.star.text"], "GraphicCrop", MagicMock)
    ctx = TestingFactory.create_context(doc_type="writer")
    obj = MagicMock()
    size_mock = MagicMock()
    size_mock.Width = 1000
    size_mock.Height = 500
    with (
        patch("plugin.writer.images.images._get_graphic_object", return_value=obj),
        patch("plugin.writer.images.images.visual_helpers.graphic_from_object", return_value=MagicMock()),
        patch("plugin.writer.images.images.image_mark.pixel_size", return_value=(100, 80)),
        patch("plugin.writer.images.images.image_mark.bake", side_effect=RuntimeError("bake failed")),
        patch("plugin.writer.images.images._object_size", return_value=size_mock),
    ):
        res = ImageCropAndHighlight().execute_safe(ctx, name="Pic", crop_box=[0, 0, 10, 10])
    assert res["status"] != "ok"
    assert res.get("code") == "TOOL_EXECUTION_ERROR"
    assert "bake failed" in res.get("message", "")
    assert res.get("code") != "IMAGE_MARK_FAILED"
    obj.setPropertyValue.assert_not_called()




