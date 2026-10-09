# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for images.py helpers (no LibreOffice required)."""
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import TestingFactory

from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE
from plugin.doc.document_research_tools import ListNearbyFiles
from plugin.framework.tool import ToolContext, ToolRegistry
from plugin.writer.images.images import (
    ImageDownload,
    ImageGenerate,
    ImageGetInfo,
    ImageInsert,
    ImageListNearbyFiles,
    ImageReplace,
    _download_image_to_cache,
    _object_size,
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


def test_image_generate_invalid_base_size_falls_back_to_1024():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, bound_scope=None, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value=None),
        patch("plugin.writer.images.images.ImageService", return_value=svc),
        patch("plugin.writer.images.images.replace_image_in_place"),
        patch("plugin.writer.images.images.insert_image"),
        patch("plugin.writer.images.images.get_config_int", return_value=1024),
        patch("plugin.writer.images.images.get_config_bool", return_value=False),
        patch("plugin.writer.images.images.get_config_str", return_value="square"),
        patch("plugin.writer.images.images.get_image_model", return_value=""),
    ):
        res = ImageGenerate().execute(ctx, prompt="a tabby cat", base_size="nope")
    assert res["status"] == "ok"
    assert svc.generate_image.call_args.kwargs.get("width") == 1024
    assert svc.generate_image.call_args.kwargs.get("height") == 1024


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

    monkeypatch.setattr(images_mod, "_IMAGE_CACHE_DIR", str(tmp_path))
    seen = {}

    class _Resp:
        def read(self):
            return b"\x89PNG"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _open(request, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(images_mod.urllib.request, "urlopen", _open)
    path = _download_image_to_cache("https://example.com/logo.png")
    assert path.startswith(str(tmp_path))
    assert seen["context"] is None
    assert not isinstance(seen["context"], ssl.SSLContext)


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
