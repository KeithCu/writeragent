# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for matplotlib figure detection, SVG/PNG serialization, and image payload codec."""

from __future__ import annotations

import pytest

from plugin.scripting.payload_codec import (
    PAYLOAD_IMAGE,
    describe_wire_value,
    host_unpack_data,
    is_image_payload,
)

PNG_MAGIC = b"\x89PNG"


# ---------------------------------------------------------------------------
# Codec predicate tests (no matplotlib needed)
# ---------------------------------------------------------------------------


def test_is_image_payload_valid_png():
    payload = {"__wa_payload__": "image", "format": "png", "data": b"\x89PNG\r\n\x1a\nfake"}
    assert is_image_payload(payload) is True


def test_is_image_payload_valid_svg():
    payload = {"__wa_payload__": "image", "format": "svg", "data": b"<svg xmlns=...></svg>"}
    assert is_image_payload(payload) is True


def test_is_image_payload_missing_data():
    assert is_image_payload({"__wa_payload__": "image"}) is False


def test_is_image_payload_wrong_type():
    assert is_image_payload({"__wa_payload__": "image", "data": "not bytes"}) is False


def test_is_image_payload_unrelated_dict():
    assert is_image_payload({"foo": "bar"}) is False


def test_is_image_payload_non_dict():
    assert is_image_payload([1, 2, 3]) is False
    assert is_image_payload(42) is False
    assert is_image_payload(None) is False


def test_describe_wire_value_image():
    payload = {"__wa_payload__": "image", "format": "png", "data": b"\x89PNG" * 10}
    desc = describe_wire_value(payload)
    assert "image" in desc
    assert "png" in desc
    assert "40" in desc  # 4 bytes * 10


def test_host_unpack_data_passthrough():
    payload = {"__wa_payload__": "image", "format": "png", "data": b"\x89PNGtest"}
    result = host_unpack_data(payload)
    assert result is payload


# ---------------------------------------------------------------------------
# Figure serialization (requires matplotlib)
# ---------------------------------------------------------------------------


def test_figure_to_image_payload_svg_default():
    """Default format is SVG for crisp rendering in LibreOffice."""
    plt = pytest.importorskip("matplotlib.pyplot")
    from plugin.scripting.venv_sandbox import _figure_to_image_payload

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    payload = _figure_to_image_payload(fig)
    plt.close(fig)

    assert payload["__wa_payload__"] == PAYLOAD_IMAGE
    assert payload["format"] == "svg"
    assert isinstance(payload["data"], bytes)
    assert b"<svg" in payload["data"]
    assert is_image_payload(payload) is True


def test_figure_to_image_payload_png_explicit():
    """Explicit fmt='png' produces a PNG raster."""
    plt = pytest.importorskip("matplotlib.pyplot")
    from plugin.scripting.venv_sandbox import _figure_to_image_payload

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    payload = _figure_to_image_payload(fig, fmt="png")
    plt.close(fig)

    assert payload["__wa_payload__"] == PAYLOAD_IMAGE
    assert payload["format"] == "png"
    assert isinstance(payload["data"], bytes)
    assert payload["data"][:4] == PNG_MAGIC
    assert is_image_payload(payload) is True


def test_serialize_result_figure():
    """serialize_result produces an SVG image payload by default."""
    plt = pytest.importorskip("matplotlib.pyplot")
    from plugin.scripting.venv_sandbox import serialize_result

    fig, ax = plt.subplots()
    ax.bar([1, 2, 3], [4, 5, 6])
    result = serialize_result(fig)
    plt.close(fig)

    assert is_image_payload(result)
    assert result["format"] == "svg"
    assert b"<svg" in result["data"]


# ---------------------------------------------------------------------------
# End-to-end sandbox tests (requires matplotlib)
# ---------------------------------------------------------------------------


def test_implicit_open_figure_capture():
    """Open pyplot figure with no result assignment should produce an SVG image payload (post-run get_fignums)."""
    pytest.importorskip("matplotlib")
    from plugin.scripting.venv_sandbox import run_sandboxed_code

    res = run_sandboxed_code(
        "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])",
        timeout_sec=30,
    )
    assert res["status"] == "ok"
    assert is_image_payload(res["result"])
    assert res["result"]["format"] == "svg"
    assert b"<svg" in res["result"]["data"]


def test_explicit_figure_result():
    """result = fig should produce an SVG image payload via serialize_result."""
    pytest.importorskip("matplotlib")
    from plugin.scripting.venv_sandbox import run_sandboxed_code

    res = run_sandboxed_code(
        "import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.plot([1, 2, 3])\nresult = fig",
        timeout_sec=30,
    )
    assert res["status"] == "ok"
    assert is_image_payload(res["result"])
    assert res["result"]["format"] == "svg"
    assert b"<svg" in res["result"]["data"]


def test_non_matplotlib_code_unaffected():
    """Scalar results should not be wrapped in an image payload."""
    from plugin.scripting.venv_sandbox import run_sandboxed_code

    res = run_sandboxed_code("result = 42", timeout_sec=10)
    assert res["status"] == "ok"
    assert res["result"] == 42
    assert not is_image_payload(res["result"])
