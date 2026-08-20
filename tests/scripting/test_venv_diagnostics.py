# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for venv self-check diagnostics."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.config_limits import (
    SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
    VECTOR_SEARCH_PROBE_TIMEOUT_SEC,
    VISION_PROBE_TIMEOUT_SEC,
)
from plugin.scripting.venv_diagnostics import (
    _build_probe_display,
    _format_self_check_success,
    _probe_nlp_packages,
    _probe_vector_search_packages,
    _probe_vision_packages,
    probe_venv_path,
    run_venv_self_check,
)

def test_probe_venv_path_not_directory():
    ok, msg = probe_venv_path(__file__)
    assert ok is False
    assert "Not a Python executable" in msg


def test_probe_venv_path_blank_uses_process_python():
    with patch("plugin.scripting.venv_diagnostics.resolve_libreoffice_python", return_value="/fake/lo/python") as mock_res:
        with patch("plugin.scripting.venv_diagnostics.run_venv_self_check", return_value=(True, "ignored")) as mock_check:
            ok, msg = probe_venv_path("  ")
    assert ok is True
    assert "LibreOffice process Python" in msg
    assert "/fake/lo/python" in msg
    mock_res.assert_called_once()
    mock_check.assert_called_once_with("/fake/lo/python", timeout=None)


def test_probe_venv_path_blank_fails_when_no_process_interpreter():
    with patch("plugin.scripting.venv_diagnostics.resolve_libreoffice_python", return_value=None):
        ok, msg = probe_venv_path("")
    assert ok is False
    assert "No process interpreter" in msg


@pytest.mark.integration
def test_run_venv_self_check_success():
    ok, msg = run_venv_self_check(sys.executable, timeout=10.0)
    assert ok is True
    assert "OK" in msg or "ok" in msg.lower()


def test_run_venv_self_check_worker_start_error():
    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = OSError("boom")
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/fake/python", timeout=1.0)
    assert ok is False
    assert "boom" in msg


def test_run_venv_self_check_worker_error_response():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {"status": "error", "message": "nope"}
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "nope" in msg


def test_run_venv_self_check_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "error",
        "message": "Python worker failed: Command timed out after 1 seconds",
    }
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "Timed out" in msg


def test_run_venv_self_check_reports_architecture():
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
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_audio_packages", return_value=({}, None)),
    ):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
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
    assert "uv pip install docling rapidocr-paddle css-inline" in footer
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


def test_run_venv_self_check_includes_vision():
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
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=(vision_probes, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_audio_packages", return_value=({"sounddevice": "present", "input_device": "present"}, None)),
    ):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is True
    assert "Text / NLP Libraries" in msg
    assert "Vision Libraries" in msg
    assert "Vector Search Libraries" in msg
    assert "OCR: not ready" in msg
    assert "To install remaining packages:" in msg
    assert "uv pip install" in msg
    assert "Helpers" not in msg


def test_run_venv_self_check_includes_vector_search():
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
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)),
        patch(
            "plugin.scripting.venv_diagnostics._probe_vector_search_packages",
            return_value=(vector_search_probes, None),
        ),
    ):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is True
    assert "Vector Search Libraries" in msg
    assert "Vector Search Libraries: envwrap, sqlite_vec" in msg
    assert "Missing: sentence_transformers" in msg


def test_run_venv_self_check_uses_vision_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)) as mock_vision_probe,
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        run_venv_self_check("/x/python", timeout=1.0)
    mock_vision_probe.assert_called_once_with("/x/python", timeout=float(VISION_PROBE_TIMEOUT_SEC))


def test_run_venv_self_check_uses_vector_search_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)) as mock_probe,
    ):
        run_venv_self_check("/x/python", timeout=1.0)
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
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, timeout_hint)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
        patch(
            "plugin.scripting.venv_diagnostics._probe_audio_packages",
            return_value=({"sounddevice": "present", "input_device": "present"}, None),
        ),
    ):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
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
    assert "uv pip install docling rapidocr-paddle css-inline" in msg


def test_probe_vision_packages_subprocess_timeout():

    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _probe_vision_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_format_self_check_success_analysis_install_hint():

    data = {
        "v": "3.12.0",
        "p": {"ydata_profiling": None, "statsmodels": "present", "pandas_montecarlo": None},
        "sci": [],
        "eda": ["ydata_profiling", "statsmodels", "pandas_montecarlo"],
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
    assert "uv pip install ydata-profiling pandas-montecarlo" in footer
    assert "pip install ydata-profiling pandas-montecarlo" in footer
    assert footer.index("uv pip install") < footer.index("\npip install")
    assert "Helpers" not in msg


def test_format_self_check_success_no_analysis_hint_when_complete():

    data = {
        "v": "3.12.0",
        "p": {
            "ydata_profiling": "present",
            "statsmodels": "present",
            "pandas_montecarlo": "present",
        },
        "sci": [],
        "eda": ["ydata_profiling", "statsmodels", "pandas_montecarlo"],
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
    assert "uv pip install scipy scikit-learn docling rapidocr-paddle css-inline" in footer
    assert "pip install scipy scikit-learn docling rapidocr-paddle css-inline" in footer
    lines = footer.splitlines()
    uv_idx = next(i for i, line in enumerate(lines) if line.startswith("uv pip install "))
    pip_idx = next(i for i, line in enumerate(lines) if line.startswith("pip install "))
    assert uv_idx < pip_idx


def test_run_venv_self_check_default_uses_import_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        run_venv_self_check("/x/python")
    mock_mgr.execute.assert_called_once()
    assert mock_mgr.execute.call_args.kwargs["timeout_sec"] == SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC


def test_run_venv_self_check_batch_path_reports_duckdb():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {
            "v": "3.12.0",
            "p": {"pint": "present", "duckdb": None},
            "sci": [],
            "eda": [],
            "ui": [],
            "data_eng": ["pint", "duckdb"],
        },
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        ok, msg = run_venv_self_check("/x/python")
    assert ok is True
    assert "Data Engineering Libraries: pint" in msg
    assert "duckdb" in msg


def test_probe_nlp_packages_subprocess_timeout():
    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _probe_nlp_packages("/x/python", timeout=30.0)
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_run_venv_self_check_uses_nlp_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._probe_nlp_packages", return_value=({}, None)) as mock_nlp_probe,
        patch("plugin.scripting.venv_diagnostics._probe_vision_packages", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics._probe_vector_search_packages", return_value=({}, None)),
    ):
        run_venv_self_check("/x/python", timeout=1.0)
    mock_nlp_probe.assert_called_once_with("/x/python", timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC))
