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
    from plugin.scripting.venv.venv_sandbox import _figure_to_image_payload

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
    from plugin.scripting.venv.venv_sandbox import _figure_to_image_payload

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
    from plugin.scripting.venv.venv_sandbox import serialize_result

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
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

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
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

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
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    res = run_sandboxed_code("result = 42", timeout_sec=10)
    assert res["status"] == "ok"
    assert res["result"] == 42
    assert not is_image_payload(res["result"])


def test_multiple_open_figures_captured_individually():
    """Two implicit figures should be captured individually as a multi_data envelope containing two SVG figures."""
    pytest.importorskip("matplotlib")
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code
    from plugin.scripting.payload_codec import is_multi_data

    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.figure()\nplt.plot([1])\n"
        "plt.figure()\nplt.plot([2, 3])\n"
    )
    res = run_sandboxed_code(code, timeout_sec=30)
    assert res["status"] == "ok"
    assert is_multi_data(res["result"])
    items = res["result"]["items"]
    assert len(items) == 2
    assert items[0]["__wa_payload__"] == "image"
    assert items[0]["format"] == "svg"
    assert items[1]["__wa_payload__"] == "image"
    assert items[1]["format"] == "svg"
    assert "Captured 2 open figures" in (res.get("stdout") or "")


def test_serialize_list_of_figures():
    """A list of Figures should be serialized to a list of image payloads recursively."""
    plt = pytest.importorskip("matplotlib.pyplot")
    from plugin.scripting.venv.venv_sandbox import serialize_result

    fig1, ax1 = plt.subplots()
    ax1.plot([1, 2])
    fig2, ax2 = plt.subplots()
    ax2.plot([3, 4])

    res = serialize_result([fig1, fig2])
    plt.close(fig1)
    plt.close(fig2)

    assert isinstance(res, list)
    assert len(res) == 2
    assert res[0]["__wa_payload__"] == "image"
    assert res[0]["format"] == "svg"
    assert res[1]["__wa_payload__"] == "image"
    assert res[1]["format"] == "svg"


def test_seaborn_implicit_figure():
    """Seaborn plotting should produce an image payload via open-figure capture."""
    pytest.importorskip("seaborn")
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    res = run_sandboxed_code(
        "import seaborn as sns\nsns.lineplot(x=[1, 2, 3], y=[1, 4, 9])\n",
        timeout_sec=30,
    )
    assert res["status"] == "ok"
    assert is_image_payload(res["result"])


def test_serialize_result_dataframe_numeric():
    """DataFrame with numeric values should serialize to dataframe envelope with split_grid inner when large."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    from plugin.scripting.venv.venv_sandbox import serialize_result
    from plugin.scripting.payload_codec import is_dataframe_payload, is_split_grid, PAYLOAD_DATAFRAME

    df = pd.DataFrame({"a": np.arange(200), "b": np.arange(200) * 0.5})
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["__wa_payload__"] == PAYLOAD_DATAFRAME
    assert res["columns"] == ["a", "b"]
    inner = res["data"]
    # Above BINARY_MIN_CELLS -> split_grid
    assert is_split_grid(inner)
    assert inner["shape"][0] == 200


def test_serialize_result_dataframe_mixed_and_roundtrip():
    """Mixed DF should keep strings via the inner grid/strings path; host unpack yields lists."""
    pd = pytest.importorskip("pandas")
    from plugin.scripting.venv.venv_sandbox import serialize_result
    from plugin.scripting.payload_codec import host_unpack_data, is_dataframe_payload

    df = pd.DataFrame({"num": [1, 2, 3], "txt": ["x", "y", None]})
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["columns"] == ["num", "txt"]
    unpacked = host_unpack_data(res)
    assert unpacked["columns"] == ["num", "txt"]
    data = unpacked["data"]
    assert data[0] == [1, "x"]
    assert data[2] == [3, None]


def test_run_sandboxed_dataframe_produces_envelope():
    """End-to-end: code returning a DF yields the envelope in the worker response."""
    pytest.importorskip("pandas")
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code
    from plugin.scripting.payload_codec import is_dataframe_payload

    code = "import pandas as pd\ndf = pd.DataFrame({'x': [10, 20], 'y': ['p', 'q']})\nresult = df"
    res = run_sandboxed_code(code, timeout_sec=30)
    assert res["status"] == "ok"
    assert is_dataframe_payload(res["result"])
    assert res["result"]["columns"] == ["x", "y"]


def test_serialize_pil_image():
    """A PIL Image should be serialized to an image payload with format png."""
    Image = pytest.importorskip("PIL.Image")
    from plugin.scripting.venv.venv_sandbox import serialize_result

    img = Image.new("RGB", (100, 100), color="red")
    res = serialize_result(img)
    assert res["__wa_payload__"] == "image"
    assert res["format"] == "png"
    assert isinstance(res["data"], bytes)


def test_serialize_dict_of_dataframes():
    """A dictionary containing DataFrames should serialize its DataFrame values correctly."""
    pd = pytest.importorskip("pandas")
    from plugin.scripting.venv.venv_sandbox import serialize_result
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame({"x": [1, 2]})
    res = serialize_result({"df_key": df})
    assert isinstance(res, dict)
    assert is_dataframe_payload(res["df_key"])
