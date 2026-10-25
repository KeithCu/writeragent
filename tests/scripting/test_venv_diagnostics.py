# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for venv self-check diagnostics."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

from typing import Any

import pytest

from plugin.scripting.config_limits import (
    SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
    VECTOR_SEARCH_PROBE_TIMEOUT_SEC,
    VISION_PROBE_TIMEOUT_SEC,
)
from plugin.scripting.venv_diagnostics import (
    _AUDIO_OPTIONAL_KEYS,
    _AUDIO_PACKAGE_KEYS,
    _AUDIO_PROBE_SCRIPT,
    _DUCKDB_PROBE_SCRIPT,
    _NLP_PROBE_SCRIPT,
    _PROBE_JSON_PREFIX,
    _PROBE_KEY_TO_PIP,
    _UI_PACKAGE_KEYS,
    _UI_PROBE_SCRIPT,
    _VECTOR_SEARCH_PROBE_SCRIPT,
    _VISION_PROBE_SCRIPT,
    _build_probe_display,
    _format_self_check_success,
    _import_probe_script,
    _import_try_block,
    _parse_probe_stdout,
    _probe_key_to_pip,
    _probe_nlp_packages,
    _probe_vector_search_packages,
    probe_venv_path_with_progress,
    probe_vision_packages,
    run_venv_self_check_with_progress,
)

def test_probe_venv_path_with_progress_not_directory():
    shown: list[str] = []
    ok, msg = probe_venv_path_with_progress(__file__, shown.append)
    assert ok is False
    assert "Not a Python executable" in msg
    assert shown == [msg]


def test_probe_venv_path_with_progress_blank_uses_process_python():
    shown: list[str] = []
    with patch("plugin.scripting.venv_diagnostics.resolve_libreoffice_python", return_value="/fake/lo/python") as mock_res:
        with patch("plugin.scripting.venv_diagnostics.run_venv_self_check_with_progress", return_value=(True, "ignored")) as mock_check:
            ok, msg = probe_venv_path_with_progress("  ", shown.append)
    assert ok is True
    assert msg == "LibreOffice process Python (/fake/lo/python) responds OK."
    mock_res.assert_called_once()
    assert mock_check.call_args.args[0] == "/fake/lo/python"
    assert mock_check.call_args.kwargs.get("include_vector_search") is True
    assert mock_check.call_args.kwargs.get("include_audio") is True
    assert mock_check.call_args.kwargs.get("on_status") is None
    assert mock_check.call_args.kwargs.get("extra_lines_after_header") is None
    assert "timeout" not in mock_check.call_args.kwargs


def test_probe_venv_path_with_progress_blank_fails_when_no_process_interpreter():
    shown: list[str] = []
    with patch("plugin.scripting.venv_diagnostics.resolve_libreoffice_python", return_value=None):
        ok, msg = probe_venv_path_with_progress("", shown.append)
    assert ok is False
    assert msg.startswith("No process interpreter:")
    assert shown == [msg]


@pytest.mark.integration
def test_run_venv_self_check_with_progress_success():
    ok, msg = run_venv_self_check_with_progress(sys.executable, lambda text: None)
    assert ok is True
    assert "OK" in msg or "ok" in msg.lower()


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param("nope", "nope", id="test_run_venv_self_check_with_progress_worker_error_response"),
        pytest.param("Python worker failed: Command timed out after 1 seconds", "Timed out", id="test_run_venv_self_check_with_progress_timeout"),
    ],
)
def test_run_venv_self_check_with_progress_worker_error_response(value, value_2):
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {"status": "error", "message": value}
    shown: list[str] = []
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check_with_progress("/x/python", shown.append)
    assert ok is False
    assert value_2 in msg
    assert shown == [msg]


def test_run_venv_self_check_with_progress_unexpected_version_is_displayed():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {"status": "ok", "result": "nope"}
    shown: list[str] = []
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check_with_progress("/x/python", shown.append)
    assert ok is False
    assert msg == "Unexpected output from test run: 'nope'"
    assert shown == [msg]


def test_run_venv_self_check_with_progress_package_worker_error_continues():
    mock_mgr = MagicMock()

    def _execute(script, timeout_sec=None):
        if "platform" in script:
            return {"status": "ok", "result": {"v": "3.12.0", "arch": "x86_64"}}
        if "import pandas\n" in script:
            return {"status": "error", "message": "died"}
        return {"status": "ok", "result": "present"}

    mock_mgr.execute.side_effect = _execute
    shown: list[str] = []
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_ui_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_duckdb", return_value=({}, None)),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", shown.append, include_audio=False)
    assert ok is True
    assert "Warning: pandas: died" in msg
    assert "Scientific Libraries" in msg
    assert "Could not run Python" not in msg
    assert shown[-1] == msg

def test_run_venv_self_check_with_progress_reports_architecture():
    """Self-check success message includes platform.machine() from the worker payload."""
    import platform

    expected_arch = platform.machine()
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {
            "v": "3.12.0",
            "arch": expected_arch,
            "p": {},
            "sci": [],
            "eda": [],
            "ui": [],
        },
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_audio_packages", return_value=({}, None)),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert expected_arch in msg


def test_format_self_check_success_with_arch():
    data = {"v": "3.12.0", "arch": "ARM64", "p": {}, "sci": [], "ui": []}
    msg = _format_self_check_success(data)
    assert "Python 3.12.0 (ARM64)" in msg
    assert "responds OK" in msg


def test_format_self_check_success_without_arch():
    data = {"v": "3.11.5", "p": {}, "sci": [], "ui": []}
    msg = _format_self_check_success(data)
    assert "Python 3.11.5 responds OK" in msg
    assert "(" not in msg.split("\n")[0]


def test_format_self_check_success_with_data_engineering_group():

    data = {
        "v": "3.12.0",
        "p": {"pint": "present", "duckdb": None},
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "vision": [],
        "data_eng": ["pint", "duckdb"],
    }
    msg = _format_self_check_success(data)
    assert "Data Engineering Libraries" in msg
    assert "Data Engineering Libraries: pint" in msg
    assert "duckdb" in msg
    assert "uv pip install duckdb" in msg
    assert "pip install duckdb" in msg


def test_build_probe_display_includes_nlp_when_keys_present():
    data = {
        "v": "3.12.0",
        "p": {"spacy": "present", "textdescriptives": None, "transformers": None},
        "sci": [],
        "eda": [],
        "ui": [],
        "viz": [],
        "cas": [],
        "quant": [],
        "data_eng": [],
        "nlp": ["spacy", "textdescriptives", "transformers"],
    }
    msg = _build_probe_display(data, completed_groups=8)
    assert "Text / NLP Libraries" in msg
    assert "spacy" in msg


def test_build_probe_display_linux_portaudio_hint_when_sounddevice_missing():
    data = {
        "v": "3.12.0",
        "p": {"sounddevice": None},
        "sci": [],
        "eda": [],
        "ui": [],
        "audio": ["sounddevice"],
        "nlp": [],
        "vision": [],
        "vector_search": [],
        "data_eng": [],
    }
    msg = _build_probe_display(data, completed_groups=8, include_audio=True)
    if sys.platform.startswith("linux"):
        assert "PortAudio" in msg
    else:
        assert "PortAudio" not in msg


def test_build_probe_display_omits_install_footer_while_progressive():
    """Mid-Test refresh must not show a partial uv/pip install recipe."""
    data = {
        "v": "3.12.0",
        "p": {
            "numpy": "present",
            "scipy": None,
            "sklearn": None,
            "sounddevice": "present",
            "input_device": "present",
        },
        "sci": ["numpy", "scipy", "sklearn"],
        "eda": [],
        "ui": [],
        "audio": ["sounddevice", "input_device"],
        "nlp": [],
        "vision": [],
        "vector_search": [],
        "data_eng": [],
    }
    msg = _build_probe_display(data, completed_groups=2, include_audio=True)
    assert "Scientific Libraries" in msg
    assert "Missing: scipy, sklearn" in msg
    assert "To install remaining packages:" not in msg
    assert "uv pip install" not in msg
    assert "pip install" not in msg


def test_format_self_check_success_with_vision_group():

    data = {
        "v": "3.12.0",
        "p": {
            "docling": "present",
            "rapidocr": "present",
            "css_inline": "present",
            "paddleocr": "present",
            "paddle": "present",
            "numpy": "present",
            "ultralytics": None,
            "skimage": None,
        },
        "sci": ["numpy"],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
    }
    msg = _format_self_check_success(data)
    assert "Vision Libraries" in msg
    assert "Vision Libraries: docling, rapidocr, css_inline, paddleocr, paddle" in msg
    assert "OCR: ready (Docling)" in msg
    assert "Optional (not installed): ultralytics, skimage" in msg
    assert "Missing: ultralytics" not in msg
    assert "Missing (OCR)" not in msg
    assert "pip install" not in msg
    assert "Helpers" not in msg

def test_format_self_check_success_vision_optional_paddle_when_docling_ready():

    data = {
        "v": "3.12.0",
        "p": {
            "docling": "present",
            "rapidocr": "present",
            "css_inline": "present",
            "paddleocr": None,
            "paddle": None,
            "ultralytics": None,
            "skimage": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
    }
    msg = _format_self_check_success(data)
    assert "OCR: ready (Docling)" in msg
    assert "Optional (not installed): paddleocr, paddle, ultralytics, skimage" in msg
    assert "Missing (OCR)" not in msg
    assert "To install remaining packages:" not in msg
    assert "pip install" not in msg


def test_format_self_check_success_vision_install_hint():

    data = {
        "v": "3.12.0",
        "p": {
            "docling": None,
            "rapidocr": None,
            "css_inline": None,
            "paddleocr": None,
            "paddle": None,
            "numpy": None,
            "ultralytics": None,
            "skimage": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
    }
    msg = _format_self_check_success(data)
    assert "OCR: not ready" in msg
    assert "Missing (OCR): docling, rapidocr, css_inline" in msg
    assert "Optional (not installed): paddleocr, paddle, ultralytics, skimage" in msg
    assert "To install remaining packages:" in msg
    install_idx = msg.index("To install remaining packages:")
    footer = msg[install_idx:]
    assert "uv pip install docling rapidocr-paddle css-inline onnxruntime" in footer
    pip_line = [line for line in footer.splitlines() if line.startswith("pip install ")][0]
    uv_line = [line for line in footer.splitlines() if line.startswith("uv pip install ")][0]
    assert footer.index(uv_line) < footer.index(pip_line)
    assert "paddleocr" not in footer
    assert "ultralytics" not in footer
    assert "Helpers" not in msg


def test_format_self_check_success_vision_paddle_only_ready():

    data = {
        "v": "3.12.0",
        "p": {
            "docling": None,
            "rapidocr": None,
            "css_inline": "present",
            "paddleocr": "present",
            "paddle": "present",
            "ultralytics": None,
            "skimage": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
    }
    msg = _format_self_check_success(data)
    assert "OCR: ready (Paddle)" in msg
    assert "Optional (not installed): ultralytics, skimage, docling, rapidocr" in msg
    assert "Missing (OCR)" not in msg
    assert "pip install" not in msg


def test_format_self_check_success_docling_without_rapidocr_is_not_ready():
    from plugin.scripting.venv_diagnostics import _VISION_PACKAGE_KEYS

    data = {
        "v": "3.12.0",
        "p": {
            "docling": "present",
            "rapidocr": None,
            "css_inline": "present",
            "paddleocr": None,
            "paddle": None,
            "ultralytics": None,
            "skimage": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": list(_VISION_PACKAGE_KEYS),
    }
    msg = _format_self_check_success(data)
    assert "OCR: not ready" in msg
    assert "OCR: ready" not in msg
    assert "Missing (OCR): rapidocr" in msg
    present_line = [line for line in msg.splitlines() if line.startswith("Vision Libraries:")][0]
    assert "docling" in present_line
    assert "uv pip install rapidocr-paddle onnxruntime" in msg
    footer = msg[msg.index("To install remaining packages:") :]
    for pkg in ("docling", "css-inline", "paddleocr", "ultralytics"):
        assert pkg not in footer
    assert "rapidocr-paddle" in footer


def test_format_self_check_success_paddle_ready_when_docling_lacks_rapidocr():
    from plugin.scripting.venv_diagnostics import _VISION_PACKAGE_KEYS

    data = {
        "v": "3.12.0",
        "p": {
            "docling": "present",
            "rapidocr": None,
            "css_inline": "present",
            "paddleocr": "present",
            "paddle": "present",
            "ultralytics": None,
            "skimage": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": list(_VISION_PACKAGE_KEYS),
    }
    msg = _format_self_check_success(data)
    assert "OCR: ready (Paddle)" in msg
    assert "OCR: ready (Docling)" not in msg
    assert "Optional (not installed): ultralytics, skimage, rapidocr" in msg
    assert "Missing (OCR)" not in msg
    assert "pip install" not in msg


def test_format_self_check_success_vision_install_hint_when_numpy_missing():

    data = {
        "v": "3.12.0",
        "p": {
            "docling": "present",
            "rapidocr": "present",
            "css_inline": "present",
            "paddleocr": "present",
            "paddle": "present",
            "numpy": None,
            "ultralytics": "present",
            "skimage": "present",
        },
        "sci": ["numpy"],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
    }
    msg = _format_self_check_success(data)
    assert "OCR: ready (Docling)" in msg
    assert "To install remaining packages:" in msg
    footer = msg[msg.index("To install remaining packages:") :]
    assert "uv pip install numpy" in footer
    assert "pip install numpy" in footer
    assert footer.index("uv pip install") < footer.index("\npip install")
    assert "Helpers" not in msg


def test_format_self_check_success_with_vector_search_group():

    data = {
        "v": "3.12.0",
        "p": {
            "envwrap": "present",
            "sentence_transformers": "present",
            "sqlite_vec": "present",
            "zvec": None,
            "langgraph": None,
            "langchain_core": "present",
            "langchain_text_splitters": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vision": [],
        "data_eng": [],
        "vector_search": [
            "envwrap",
            "sentence_transformers",
            "sqlite_vec",
            "zvec",
            "langgraph",
            "langchain_core",
            "langchain_text_splitters",
        ],
    }
    msg = _format_self_check_success(data)
    assert "Vector Search Libraries" in msg
    assert "Vector Search Libraries: envwrap, sentence_transformers, sqlite_vec, langchain_core" in msg
    assert "Missing: zvec, langgraph, langchain_text_splitters" in msg
    footer = msg[msg.index("To install remaining packages:") :]
    assert "uv pip install zvec langgraph langchain-text-splitters" in footer
    assert "pip install zvec langgraph langchain-text-splitters" in footer


def test_format_self_check_success_vector_search_probe_failure_hint():

    data = {
        "v": "3.12.0",
        "p": {},
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vision": [],
        "data_eng": [],
        "vector_search": [
            "envwrap",
            "sentence_transformers",
            "sqlite_vec",
            "zvec",
            "langgraph",
            "langchain_core",
            "langchain_text_splitters",
        ],
        "vector_search_probe_failure": "Vector Search probe timed out (sentence-transformers import can take 10–30s on first check).",
    }
    msg = _format_self_check_success(data)
    assert "Vector Search probe timed out" in msg
    assert "Missing: envwrap" in msg
    assert "To install remaining packages:" in msg
    assert "sentence-transformers" in msg


def test_run_venv_self_check_with_progress_includes_vision():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {
            "v": "3.12.0",
            "arch": "x86_64",
            "p": {},
            "sci": [],
            "eda": [],
            "ui": [],
        },
    }
    vision_probes = {
        "docling": None,
        "rapidocr": None,
        "css_inline": None,
        "paddleocr": "present",
        "paddle": None,
        "ultralytics": None,
        "skimage": None,
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=(vision_probes, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_audio_packages", return_value=({"sounddevice": "present", "input_device": "present"}, None)),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Text / NLP Libraries" in msg
    assert "Vision Libraries" in msg
    assert "Vector Search Libraries" in msg
    assert "OCR: not ready" in msg
    assert "To install remaining packages:" in msg
    assert "uv pip install" in msg
    assert "Helpers" not in msg


def test_run_venv_self_check_with_progress_includes_vector_search():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {
            "v": "3.12.0",
            "arch": "x86_64",
            "p": {},
            "sci": [],
            "eda": [],
            "ui": [],
        },
    }
    vector_search_probes = {
        "envwrap": "present",
        "sentence_transformers": None,
        "sqlite_vec": "present",
        "zvec": None,
        "langgraph": None,
        "langchain_core": None,
        "langchain_text_splitters": None,
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)),
        patch(
            "plugin.scripting.venv_diagnostics._probe_vector_search_packages",
            return_value=(vector_search_probes, None),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Vector Search Libraries" in msg
    assert "Vector Search Libraries: envwrap, sqlite_vec" in msg
    assert "Missing: sentence_transformers" in msg


def test_run_venv_self_check_with_progress_uses_vision_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)) as mock_vision_probe,
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        run_venv_self_check_with_progress("/x/python", lambda text: None)
    mock_vision_probe.assert_called_once_with("/x/python", timeout=float(VISION_PROBE_TIMEOUT_SEC))


def test_run_venv_self_check_with_progress_uses_vector_search_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)) as mock_probe,
    ):
        run_venv_self_check_with_progress("/x/python", lambda text: None)
    mock_probe.assert_called_once_with("/x/python", timeout=float(VECTOR_SEARCH_PROBE_TIMEOUT_SEC))


def test_probe_vision_packages_timeout_reports_failure():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {
            "v": "3.12.0",
            "arch": "x86_64",
            "p": {},
            "sci": [],
            "eda": [],
            "ui": [],
        },
    }
    timeout_hint = "Vision probe timed out (Docling import can take 10–30s on first check)."
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, timeout_hint)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch(
            "plugin.scripting.venv_diagnostics._probe_audio_packages",
            return_value=({"sounddevice": "present", "input_device": "present"}, None),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Vision Libraries" in msg
    assert timeout_hint in msg
    assert "Missing (OCR): docling" in msg
    assert "To install remaining packages:" in msg


def test_probe_vector_search_packages_subprocess_timeout():

    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _probe_vector_search_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_format_self_check_success_vision_probe_failure_hint():

    data = {
        "v": "3.12.0",
        "p": {},
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
        "vision_probe_failure": "Vision probe timed out (Docling import can take 10–30s on first check).",
    }
    msg = _format_self_check_success(data)
    assert "Vision probe timed out" in msg
    assert "Missing (OCR): docling, rapidocr, css_inline" in msg
    assert "To install remaining packages:" in msg
    assert "uv pip install docling rapidocr-paddle css-inline onnxruntime" in msg


def test_probe_vision_packages_subprocess_timeout():

    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = probe_vision_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_format_self_check_success_analysis_install_hint():

    data = {
        "v": "3.12.0",
        "p": {"data_profiling": None, "statsmodels": "present", "pandas_montecarlo": None},
        "sci": [],
        "eda": ["data_profiling", "statsmodels", "pandas_montecarlo"],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": [],
    }
    msg = _format_self_check_success(data)
    assert "To install remaining packages:" in msg
    footer = msg[msg.index("To install remaining packages:") :]
    assert "uv pip install fg-data-profiling pandas-montecarlo" in footer
    assert "pip install fg-data-profiling pandas-montecarlo" in footer
    assert footer.index("uv pip install") < footer.index("\npip install")
    assert "Helpers" not in msg


def test_format_self_check_success_no_analysis_hint_when_complete():

    data = {
        "v": "3.12.0",
        "p": {
            "data_profiling": "present",
            "statsmodels": "present",
            "pandas_montecarlo": "present",
        },
        "sci": [],
        "eda": ["data_profiling", "statsmodels", "pandas_montecarlo"],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": [],
    }
    msg = _format_self_check_success(data)
    assert "To install remaining packages:" not in msg
    assert "pip install" not in msg
    assert "Helpers" not in msg


def test_format_self_check_success_global_install_footer_mixed_groups():

    data = {
        "v": "3.12.0",
        "p": {
            "numpy": "present",
            "scipy": None,
            "sklearn": None,
            "docling": None,
            "rapidocr": None,
            "css_inline": None,
        },
        "sci": ["numpy", "scipy", "sklearn"],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "data_eng": [],
        "vision": ["docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage"],
    }
    msg = _format_self_check_success(data)
    footer = msg[msg.index("To install remaining packages:") :]
    assert "uv pip install scipy scikit-learn docling rapidocr-paddle css-inline onnxruntime" in footer
    assert "pip install scipy scikit-learn docling rapidocr-paddle css-inline onnxruntime" in footer
    lines = footer.splitlines()
    uv_idx = next(i for i, line in enumerate(lines) if line.startswith("uv pip install "))
    pip_idx = next(i for i, line in enumerate(lines) if line.startswith("pip install "))
    assert uv_idx < pip_idx


def test_duckdb_probe_script_is_not_sandboxed():
    from plugin.scripting.venv_diagnostics import _SANDBOX_SELF_CHECK_GROUPS, _VERSION_PROBE_SCRIPT, _package_probe_script

    assert "import duckdb" not in _VERSION_PROBE_SCRIPT
    sandbox_pkgs = [pkg for _title, pkgs in _SANDBOX_SELF_CHECK_GROUPS for pkg in pkgs]
    assert "duckdb" not in sandbox_pkgs
    with pytest.raises(ValueError, match="unsupported probe module"):
        _package_probe_script("duckdb")


def test_run_venv_self_check_with_progress_reports_duckdb():
    def fake_execute(script, timeout_sec=10):
        if "platform" in script:
            return {"status": "ok", "result": {"v": "3.12.0", "arch": "x86_64"}}
        if "pint" in script:
            return {"status": "ok", "result": "present"}
        return {"status": "ok", "result": None}

    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = fake_execute
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Data Engineering Libraries: pint" in msg
    assert "duckdb" in msg


def test_probe_hints_translate_at_call_time(monkeypatch) -> None:
    """Module-level _() used to freeze the catalog from import, before init_i18n(ctx)."""
    import plugin.scripting.venv_diagnostics as diag

    def fake_translate(message: str) -> str:
        return f"LOCALE:{message}"

    monkeypatch.setattr(diag, "_", fake_translate)
    hint = diag._nlp_probe_timeout_hint()
    assert hint.startswith("LOCALE:")
    assert "timed out" in hint
    assert diag._audio_linux_portaudio_hint().startswith("LOCALE:")


def test_probe_nlp_packages_subprocess_timeout():
    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _probe_nlp_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_run_venv_self_check_with_progress_uses_nlp_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)) as mock_nlp_probe,
        patch("plugin.scripting.venv_diagnostics.probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        run_venv_self_check_with_progress("/x/python", lambda text: None)
    mock_nlp_probe.assert_called_once_with("/x/python", timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC))


def test_format_self_check_success_nlp_optional_language_tool_python_when_missing():
    data = {
        "v": "3.12.0",
        "p": {
            "spacy": "present",
            "textdescriptives": "present",
            "transformers": "present",
            "language_tool_python": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": ["spacy", "textdescriptives", "transformers", "language_tool_python"],
        "audio": [],
        "vector_search": [],
        "vision": [],
        "data_eng": [],
    }
    msg = _format_self_check_success(data)
    assert "Text / NLP Libraries" in msg
    assert "Text / NLP Libraries: spacy, textdescriptives, transformers" in msg
    assert "Optional (not installed): language_tool_python" in msg
    assert "Missing: language_tool_python" not in msg
    assert "To install remaining packages:" not in msg


def test_format_self_check_success_nlp_language_tool_python_present():
    data = {
        "v": "3.12.0",
        "p": {
            "spacy": "present",
            "textdescriptives": "present",
            "transformers": "present",
            "language_tool_python": "present",
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": ["spacy", "textdescriptives", "transformers", "language_tool_python"],
        "audio": [],
        "vector_search": [],
        "vision": [],
        "data_eng": [],
    }
    msg = _format_self_check_success(data)
    assert "Text / NLP Libraries: spacy, textdescriptives, transformers, language_tool_python" in msg
    assert "Optional (not installed): language_tool_python" not in msg
    assert "Missing:" not in msg


def test_format_self_check_success_audio_tts_optional_installed():
    from plugin.scripting.venv_diagnostics import _format_self_check_success, _AUDIO_PACKAGE_KEYS

    data = {
        "v": "3.13.0",
        "p": {
            "sounddevice": "present",
            "input_device": "present",
            "kokoro_onnx": "present",
            "soundfile": "present",
            "piper": "present",
            "faster_whisper": "present",
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": list(_AUDIO_PACKAGE_KEYS),
        "vector_search": [],
        "vision": [],
        "data_eng": [],
    }
    msg = _format_self_check_success(data)
    assert (
        "Audio Recording & Speech: sounddevice, input_device, kokoro_onnx, soundfile, piper, faster_whisper"
        in msg
    )
    assert "Optional (not installed):" not in msg
    assert "Local Whisper (optional):" not in msg


def test_format_self_check_success_audio_tts_optional_missing_shows_hint():
    from plugin.scripting.venv_diagnostics import _format_self_check_success, _AUDIO_PACKAGE_KEYS

    data = {
        "v": "3.13.0",
        "p": {
            "sounddevice": "present",
            "input_device": "present",
            "kokoro_onnx": None,
            "soundfile": None,
            "piper": None,
            "faster_whisper": None,
        },
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": list(_AUDIO_PACKAGE_KEYS),
        "vector_search": [],
        "vision": [],
        "data_eng": [],
    }
    msg = _format_self_check_success(data)
    assert "Audio Recording & Speech: sounddevice, input_device" in msg
    assert "Optional (not installed): kokoro_onnx, soundfile, piper, faster_whisper" in msg
    assert "Missing: faster_whisper" not in msg
    assert "To install remaining packages:" not in msg
    assert (
        "Local TTS engines (optional): uv pip install kokoro-onnx soundfile piper-tts "
        "'misaki[ja,zh]' phonemizer-fork espeakng-loader"
    ) in msg
    assert 'Local Whisper (optional): uv pip install faster-whisper "av<14"' in msg


def _audio_self_check_data(packages: dict[str, str | None]) -> dict:
    from plugin.scripting.venv_diagnostics import _AUDIO_PACKAGE_KEYS

    return {
        "v": "3.13.0",
        "p": packages,
        "sci": [],
        "eda": [],
        "ui": [],
        "nlp": [],
        "audio": list(_AUDIO_PACKAGE_KEYS),
        "vector_search": [],
        "vision": [],
        "data_eng": [],
    }


def test_format_self_check_success_whisper_missing_does_not_print_tts_recipe():
    from plugin.scripting.venv_diagnostics import _format_self_check_success

    msg = _format_self_check_success(
        _audio_self_check_data(
            {
                "sounddevice": "present",
                "input_device": "present",
                "kokoro_onnx": "present",
                "soundfile": "present",
                "piper": "present",
                "faster_whisper": None,
            }
        )
    )
    assert "Optional (not installed): faster_whisper" in msg
    assert 'Local Whisper (optional): uv pip install faster-whisper "av<14"' in msg
    assert "Local TTS engines (optional):" not in msg
    assert "kokoro-onnx" not in msg


def test_format_self_check_success_tts_missing_does_not_print_whisper_recipe():
    from plugin.scripting.venv_diagnostics import _format_self_check_success

    msg = _format_self_check_success(
        _audio_self_check_data(
            {
                "sounddevice": "present",
                "input_device": "present",
                "kokoro_onnx": None,
                "soundfile": "present",
                "piper": "present",
                "faster_whisper": "present",
            }
        )
    )
    assert "Optional (not installed): kokoro_onnx" in msg
    assert "faster_whisper" not in msg.split("Optional (not installed):", 1)[-1].split("\n", 1)[0]
    assert "Local TTS engines (optional):" in msg
    assert "Local Whisper (optional):" not in msg
    assert 'uv pip install faster-whisper "av<14"' not in msg


def test_audio_probe_lists_faster_whisper_as_optional_pip_package():
    assert _AUDIO_PACKAGE_KEYS[-1] == "faster_whisper"
    assert "faster_whisper" in _AUDIO_OPTIONAL_KEYS
    assert "import faster_whisper" in _AUDIO_PROBE_SCRIPT
    assert "__import__" not in _AUDIO_PROBE_SCRIPT
    assert "import sounddevice as sd" in _AUDIO_PROBE_SCRIPT
    assert "sd.query_devices()" in _AUDIO_PROBE_SCRIPT
    assert 'out["faster_whisper"] = "present"' in _AUDIO_PROBE_SCRIPT
    fw_pos = _AUDIO_PROBE_SCRIPT.index("import faster_whisper")
    prev_try = _AUDIO_PROBE_SCRIPT.rindex("\ntry:", 0, fw_pos)
    next_boundary = _AUDIO_PROBE_SCRIPT.find("\ntry:", fw_pos)
    if next_boundary == -1:
        next_boundary = _AUDIO_PROBE_SCRIPT.find("print(", fw_pos)
    fw_chunk = _AUDIO_PROBE_SCRIPT[prev_try:next_boundary]
    assert fw_chunk.count("import ") == 1
    compile(_AUDIO_PROBE_SCRIPT, "<audio_probe>", "exec")
    assert _PROBE_KEY_TO_PIP["faster_whisper"] == "faster-whisper"
    assert _probe_key_to_pip("faster_whisper") == "faster-whisper"


def test_attach_external_probes_shares_one_helper(monkeypatch):
    from plugin.scripting import venv_diagnostics as vd

    monkeypatch.setattr(vd, "_probe_audio_packages", lambda *args, **kwargs: ({"sounddevice": True}, None))
    monkeypatch.setattr(vd, "_probe_ui_packages", lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(vd, "_probe_nlp_packages", lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(vd, "probe_vision_packages", lambda *args, **kwargs: ({}, None))
    monkeypatch.setattr(vd, "_probe_vector_search_packages", lambda *args, **kwargs: ({}, None))
    data: dict = {}
    vd._attach_external_probes("/usr/bin/python", data)
    assert data["audio"]
    assert "ui" in data
    assert "nlp" in data
    assert "vision" in data
    assert "vector_search" in data


def test_probe_key_to_pip_webview_is_pywebview():
    from plugin.scripting.venv_diagnostics import (
        _UI_PACKAGE_KEYS,
        _format_self_check_success,
        _probe_key_to_pip,
    )

    assert _probe_key_to_pip("webview") == "pywebview"
    p = {k: "present" for k in _UI_PACKAGE_KEYS}
    p["webview"] = None
    data = {
        "v": "3.12.0",
        "p": p,
        "ui": list(_UI_PACKAGE_KEYS),
        "sci": [],
        "eda": [],
        "nlp": [],
        "audio": [],
        "vector_search": [],
        "vision": [],
        "data_eng": [],
    }
    msg = _format_self_check_success(data)
    footer_lines = msg.splitlines()
    uv_line = next(line for line in footer_lines if line.startswith("uv pip install "))
    pip_line = next(line for line in footer_lines if line.startswith("pip install "))
    assert "pywebview" in uv_line.split()
    assert "webview" not in uv_line.split()
    assert "pywebview" in pip_line.split()
    assert "webview" not in pip_line.split()


def test_run_json_probe_ignores_stdout_banner():
    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout='loading paddle\nWA_PROBE_JSON {"docling": "present"}\nbye\n',
            stderr="noise",
        ),
    ):
        probes, hint = probe_vision_packages("/x/python", timeout=30.0)
    assert probes.get("docling") == "present"
    assert hint is None


def test_run_json_probe_rejects_stdout_without_prefix():
    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout='{"docling": "present"}\n',
            stderr="",
        ),
    ):
        probes, hint = probe_vision_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None


def test_parse_probe_stdout_accepts_marker_glued_to_banner():
    from plugin.scripting.venv_diagnostics import _parse_probe_stdout

    res1 = _parse_probe_stdout('banner without newlineWA_PROBE_JSON {"rapidocr": "present"}\n')
    assert res1 == {"rapidocr": "present"}
    res2 = _parse_probe_stdout('WA_PROBE_JSON {"docling": "present"}\nnoise WA_PROBE_JSON not-json\n')
    assert res2 == {"docling": "present"}


def test_run_json_probe_uses_utf8():
    from plugin.scripting.venv_diagnostics import _run_json_probe

    captured_kwargs: dict[str, Any] = {}

    def _fake_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout='café\nWA_PROBE_JSON {"docling": "present"}\n',
            stderr="",
        )

    with patch("plugin.scripting.venv_diagnostics.subprocess.run", side_effect=_fake_run):
        probes, hint = _run_json_probe(
            sys.executable,
            "print(1)\n",
            30,
            timeout_hint="t",
            fail_hint="f",
            log_label="t",
        )
    assert captured_kwargs.get("encoding") == "utf-8"
    assert captured_kwargs.get("errors") == "replace"
    assert "text" not in captured_kwargs
    assert probes == {"docling": "present"}
    assert hint is None


def test_audio_linux_portaudio_hint_has_no_package_manager():
    from plugin.scripting.venv_diagnostics import _audio_linux_portaudio_hint

    hint = _audio_linux_portaudio_hint()
    assert "PortAudio" in hint
    assert "pacman" not in hint
    assert "apt-get" not in hint
    assert "apt " not in hint


def test_probe_scripts_print_json_prefix():
    assert _PROBE_JSON_PREFIX == "WA_PROBE_JSON "
    scripts = [
        _AUDIO_PROBE_SCRIPT,
        _NLP_PROBE_SCRIPT,
        _VISION_PROBE_SCRIPT,
        _VECTOR_SEARCH_PROBE_SCRIPT,
        _UI_PROBE_SCRIPT,
        _DUCKDB_PROBE_SCRIPT,
    ]
    for script in scripts:
        assert 'print("WA_PROBE_JSON " + json.dumps(out))' in script
        assert "print(json.dumps(out))" not in script
        assert "__import__" not in script
        assert ":=" not in script
        assert 'f"' not in script
        assert "f'" not in script
        compile(script, "<test_probe_script>", "exec")

    assert 'out["docling_import_error"] = str(exc)[:200]' in _VISION_PROBE_SCRIPT
    assert "import docling.document_converter" in _VISION_PROBE_SCRIPT
    assert "import rapidocr_onnxruntime" in _VISION_PROBE_SCRIPT
    assert 'out["sentence_transformers_import_error"] = str(exc)[:200]' in _VECTOR_SEARCH_PROBE_SCRIPT
    assert "import odf" in _VECTOR_SEARCH_PROBE_SCRIPT and 'out["odfpy"]' in _VECTOR_SEARCH_PROBE_SCRIPT
    assert "import docx" in _VECTOR_SEARCH_PROBE_SCRIPT and 'out["python_docx"]' in _VECTOR_SEARCH_PROBE_SCRIPT
    assert 'out["duckdb_import_error"] = str(exc)[:200]' in _DUCKDB_PROBE_SCRIPT
    assert "language_tool_python.__file__" not in _NLP_PROBE_SCRIPT
    assert _UI_PROBE_SCRIPT.count("\ntry:") == len(_UI_PACKAGE_KEYS)
    assert _NLP_PROBE_SCRIPT.count("\ntry:") == 4


def test_partial_scientific_group_uses_translated_title(monkeypatch) -> None:
    import plugin.scripting.venv_diagnostics as diag

    monkeypatch.setattr(diag, "_", lambda message: f"L:{message}")
    data = {
        "v": "3.12.0",
        "p": {"numpy": "present"},
        "sci": ["numpy", "pandas"],
        "eda": [],
        "ui": [],
        "viz": [],
        "cas": [],
        "quant": [],
        "data_eng": [],
        "nlp": [],
        "audio": [],
        "vision": [],
        "vector_search": [],
    }
    msg = diag._build_probe_display(
        data,
        completed_groups=0,
        partial_group_keys=("numpy",),
        partial_group_title="L:Scientific Libraries",
    )
    assert "L:Scientific Libraries: numpy" in msg
    assert "pandas" not in msg


def test_run_json_probe_keeps_payload_on_nonzero_exit():
    completed = subprocess.CompletedProcess(
        args=["python"],
        returncode=1,
        stdout='WA_PROBE_JSON {"docling": "present"}\n',
        stderr="atexit failed",
    )
    with patch("plugin.scripting.venv_diagnostics.subprocess.run", return_value=completed):
        probes, hint = probe_vision_packages("/x/python", timeout=30.0)
    assert probes == {"docling": "present"}
    assert hint is None


def test_run_json_probe_nonzero_exit_without_payload_fails():
    completed = subprocess.CompletedProcess(
        args=["python"],
        returncode=1,
        stdout="",
        stderr="atexit failed",
    )
    with patch("plugin.scripting.venv_diagnostics.subprocess.run", return_value=completed):
        probes, hint = probe_vision_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None


def test_probe_import_guard_survives_system_exit(tmp_path):
    (tmp_path / "wa_k157_exit.py").write_text("raise SystemExit(7)\n")
    script = _import_probe_script(
        _import_try_block("wa_k157_exit", "wa_k157_exit"),
        _import_try_block("json", "json"),
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")},
    )
    assert proc.returncode == 0
    assert _parse_probe_stdout(proc.stdout) == {"wa_k157_exit": None, "json": "present"}


def test_run_json_probe_watches_the_flatpak_bus():
    recorded_kwargs: dict[str, Any] = {}

    def fake_wrap(cmd, **kwargs):
        recorded_kwargs.update(kwargs)
        return cmd

    completed = subprocess.CompletedProcess(
        args=["python"],
        returncode=0,
        stdout='WA_PROBE_JSON {"docling": "present"}\n',
        stderr="",
    )
    with patch("plugin.scripting.venv_diagnostics.wrap_command_for_sandbox", side_effect=fake_wrap):
        with patch("plugin.scripting.venv_diagnostics.subprocess.run", return_value=completed):
            probe_vision_packages("/x/python", timeout=30.0)
    assert recorded_kwargs.get("watch_bus") is True


def test_build_probe_display_input_device_is_not_missing():
    data = {
        "p": {
            "sounddevice": "present",
            "input_device": None,
            "kokoro_onnx": None,
            "soundfile": None,
            "piper": None,
            "faster_whisper": None,
        },
        "audio": list(_AUDIO_PACKAGE_KEYS),
        "sci": [],
        "eda": [],
        "ui": [],
        "viz": [],
        "cas": [],
        "quant": [],
        "data_eng": [],
        "nlp": [],
        "vision": [],
        "vector_search": [],
    }
    msg = _build_probe_display(data, completed_groups=8, include_audio=True)
    assert "No microphone input devices detected." in msg
    assert "input_device" not in msg


def test_probe_venv_path_messages_are_translated(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.scripting.venv_diagnostics._", lambda message: f"L:{message}")

    with patch("plugin.scripting.venv_diagnostics.resolve_libreoffice_python", return_value=None):
        ok, msg = probe_venv_path_with_progress("", lambda _: None)
        assert ok is False
        assert msg.startswith("L:No process interpreter:")

    ok, msg = probe_venv_path_with_progress(str(tmp_path), lambda _: None)
    assert ok is False
    assert msg.startswith("L:No python found.")

    ok, msg = probe_venv_path_with_progress(str(tmp_path / "missing"), lambda _: None)
    assert ok is False
    assert msg.startswith("L:Path not found: ")

    ok, msg = probe_venv_path_with_progress(__file__, lambda _: None)
    assert ok is False
    assert msg.startswith("L:Not a Python executable: ")


