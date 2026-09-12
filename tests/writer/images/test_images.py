# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for images.py helpers (no LibreOffice required)."""
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import TestingFactory, setup_uno_mocks
setup_uno_mocks()

from plugin.writer.images.images import ImageGenerate, ImageInsert, _resolve_orient, resolve_image_generate_is_edit


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

    def _run(fn, *args, timeout=60.0, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("plugin.writer.images.images._run_on_main", side_effect=_run),
        patch("plugin.writer.images.images.get_selected_image_base64", return_value="b64sel"),
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
    replace.assert_called_once()
    insert.assert_not_called()


def test_image_generate_omitted_source_creates_when_no_selection():
    ctx = TestingFactory.create_context(doc_type="writer")
    svc = MagicMock()
    svc.generate_image.return_value = (["/tmp/new.png"], None)

    def _run(fn, *args, timeout=60.0, **kwargs):
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
