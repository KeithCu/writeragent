# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for matplotlib figure detection, SVG/PNG serialization, and image payload codec."""

from __future__ import annotations

import sys

import pytest

from plugin.scripting.payload_codec import (
    PAYLOAD_IMAGE,
    is_image_payload,
)

PNG_MAGIC = b"\x89PNG"


# ---------------------------------------------------------------------------
# Figure serialization (requires matplotlib)
# ---------------------------------------------------------------------------


def test_figure_to_image_payload_svg_default():
    """Default format is SVG for crisp rendering in LibreOffice."""
    plt = pytest.importorskip("matplotlib.pyplot")
    from plugin.scripting.venv.to_wire import figure_to_image_payload

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    payload = figure_to_image_payload(fig)
    plt.close(fig)

    assert payload["__wa_payload__"] == PAYLOAD_IMAGE
    assert payload["format"] == "svg"
    assert isinstance(payload["data"], bytes)
    assert b"<svg" in payload["data"]
    assert is_image_payload(payload) is True


def test_figure_to_image_payload_png_explicit():
    """Explicit fmt='png' produces a PNG raster."""
    plt = pytest.importorskip("matplotlib.pyplot")
    from plugin.scripting.venv.to_wire import figure_to_image_payload

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    payload = figure_to_image_payload(fig, fmt="png")
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


@pytest.mark.parametrize(
    "value",
    [
        # Open pyplot figure with no result assignment should produce an SVG image payload (post-run get_fignums).
        pytest.param("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])", id="test_implicit_open_figure_capture"),
        # result = fig should produce an SVG image payload via serialize_result.
        pytest.param("import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.plot([1, 2, 3])\nresult = fig", id="test_explicit_figure_result"),
    ],
)
def test_implicit_open_figure_capture(value):
    pytest.importorskip("matplotlib")
    from tests.scripting.sandbox_support import sandbox_run

    res = sandbox_run(
        value,
        timeout_sec=30,
    )
    assert res["status"] == "ok"
    assert is_image_payload(res["result"])
    assert res["result"]["format"] == "svg"
    assert b"<svg" in res["result"]["data"]


def test_multiple_open_figures_captured_individually():
    """Two implicit figures should be captured individually as a multi_data envelope containing two SVG figures."""
    pytest.importorskip("matplotlib")
    from tests.scripting.sandbox_support import sandbox_run
    from plugin.scripting.payload_codec import is_multi_data

    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.figure()\nplt.plot([1])\n"
        "plt.figure()\nplt.plot([2, 3])\n"
    )
    res = sandbox_run(code, timeout_sec=30)
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
    from tests.scripting.sandbox_support import sandbox_run

    res = sandbox_run(
        "import seaborn as sns\nsns.lineplot(x=[1, 2, 3], y=[1, 4, 9])\n",
        timeout_sec=30,
    )
    assert res["status"] == "ok"
    assert is_image_payload(res["result"])


def test_capture_open_figures_closes_when_render_raises(monkeypatch):
    from plugin.scripting.venv import optional_import as oi
    from plugin.scripting.venv import venv_sandbox as vs

    closed = {"n": 0}

    class _Fig:
        pass

    class _Plt:
        def get_fignums(self):
            return [1]

        def figure(self, num):
            return _Fig()

        def close(self, what):
            assert what == "all"
            closed["n"] += 1

    monkeypatch.setattr(oi, "optional_module", lambda name: _Plt() if name == "matplotlib.pyplot" else None)

    def boom(fig, *, fmt="svg"):
        raise RuntimeError("bad figure")

    monkeypatch.setattr(vs, "figure_to_image_payload", boom)
    # Capture skips work until pyplot is imported; don't depend on test order.
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", _Plt())
    with pytest.raises(RuntimeError, match="bad figure"):
        vs._capture_open_figures_payload()
    assert closed["n"] == 1


def test_non_plot_cell_does_not_import_matplotlib():
    """A non-plot cell must not pay the matplotlib import (font cache costs seconds)."""
    import subprocess
    import sys
    from pathlib import Path

    pytest.importorskip("matplotlib")
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys\n"
        "from plugin.scripting.venv.sandbox_request import SandboxRequest\n"
        "from plugin.scripting.venv.venv_sandbox import run_sandboxed_code\n"
        "res = run_sandboxed_code(SandboxRequest(code='result = 6 * 7', timeout_sec=10))\n"
        "assert res.get('status') == 'ok', res\n"
        "assert res.get('result') == 42, res\n"
        "assert 'matplotlib' not in sys.modules\n"
        "res = run_sandboxed_code(SandboxRequest(code='plt.plot([1, 2])', timeout_sec=10))\n"
        "assert res.get('status') == 'ok', res\n"
        "assert 'matplotlib.pyplot' in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
