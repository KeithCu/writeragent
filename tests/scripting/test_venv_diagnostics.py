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

from plugin.scripting.venv_diagnostics import (
    GroupResult,
    ProbeReport,
    _AUDIO_OPTIONAL_KEYS,
    _AUDIO_PACKAGE_KEYS,
    _AUDIO_PROBE_SCRIPT,
    _DUCKDB_PROBE_SCRIPT,
    _NEMOTRON_RUNTIME_ERROR_KEY,
    _NLP_PROBE_SCRIPT,
    _OCR_ENGINE_KEYS,
    _OCR_ENGINE_PROBE_BODY,
    _UI_PACKAGE_KEYS,
    _UI_PROBE_SCRIPT,
    _VECTOR_SEARCH_PROBE_SCRIPT,
    _VISION_OPTIONAL_KEYS,
    _VISION_PACKAGE_KEYS,
    _VISION_PROBE_SCRIPT,
    _import_probe_script,
    _import_try_block,
    _parse_probe_stdout,
    _run_named_json_probe,
    probe_venv_path_with_progress,
    run_venv_self_check_with_progress,
)

orig_named_probe = _run_named_json_probe

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
    assert "responds OK." in msg


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
    from plugin.scripting.venv_worker import ExecRequest

    mock_mgr = MagicMock()

    def _execute(req: ExecRequest):
        script = req.code or ""
        if "platform" in script:
            return {"status": "ok", "result": {"v": "3.12.0", "arch": "x86_64"}}
        if "import pandas\n" in script:
            return {"status": "error", "message": "died"}
        return {"status": "ok", "result": "present"}

    mock_mgr.execute.side_effect = _execute
    shown: list[str] = []
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", return_value=({}, None)),
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
        patch(
            "plugin.scripting.venv_diagnostics._run_named_json_probe",
            side_effect=lambda exe, name, timeout=None: (
                ({}, None) if name in {"audio", "nlp", "vector_search", "vision"} else orig_named_probe(exe, name)
            ),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert expected_arch in msg


@pytest.mark.parametrize(
    ("arch", "expect_paren"),
    [("ARM64", True), ("", False)],
)
def test_probe_report_render_header_arch(arch: str, expect_paren: bool) -> None:
    msg = ProbeReport(version="3.12.0", arch=arch).render()
    first_line = msg.splitlines()[0]
    assert "3.12.0" in first_line
    assert (f"({arch})" in first_line) is expect_paren
    if not expect_paren:
        assert "(" not in first_line


def test_probe_report_render_skips_empty_groups_and_shows_failures() -> None:
    report = ProbeReport(
        version="3.12.0",
        packages={"pint": "present"},
        groups=[
            GroupResult("empty", "Empty Group", packages=()),
            GroupResult("data_eng", "Data Engineering Libraries", packages=("pint", "duckdb"), failure="probe timed out"),
        ],
    )
    msg = report.render(include_install_footer=True)
    assert "Empty Group" not in msg
    assert "Data Engineering Libraries" in msg
    assert "probe timed out" in msg


@pytest.mark.parametrize(
    ("include_footer", "packages", "group_specs", "expect_footer", "expected_in_footer", "excluded_from_footer"),
    [
        pytest.param(
            # Mid-Test refresh must not show a partial uv/pip install recipe.
            False,
            {"numpy": "present", "scipy": None, "sklearn": None},
            [("sci", "Scientific Libraries", ("numpy", "scipy", "sklearn"), ())],
            False,
            (),
            (),
            id="progressive_omits_footer",
        ),
        pytest.param(
            True,
            {"numpy": "present", "scipy": "present"},
            [("sci", "Scientific Libraries", ("numpy", "scipy"), ())],
            False,
            (),
            (),
            id="all_present_omits_footer",
        ),
        pytest.param(
            True,
            {"numpy": "present", "scipy": None, "sklearn": None, "language_tool_python": None},
            [("sci", "Scientific Libraries", ("numpy", "scipy", "sklearn", "language_tool_python"), ("language_tool_python",))],
            True,
            ("scipy", "scikit-learn"),
            ("language_tool_python", "language-tool-python"),
            id="missing_required_shown_optional_excluded",
        ),
        pytest.param(
            True,
            {"numpy": None, "scipy": None, "docling": None, "rapidocr": None, "onnxruntime": None, "css_inline": None},
            [
                ("sci", "Scientific Libraries", ("numpy", "scipy"), ()),
                ("vision", "Vision Libraries", ("docling", "rapidocr", "onnxruntime", "css_inline", "numpy"), ()),
            ],
            True,
            ("numpy", "scipy", '"docling>=2.145"', "rapidocr", "css-inline", "onnxruntime"),
            (),
            id="multiple_groups_single_deduplicated_footer",
        ),
    ],
)
def test_probe_report_install_footer_structure(
    include_footer: bool,
    packages: dict[str, str | None],
    group_specs: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]],
    expect_footer: bool,
    expected_in_footer: tuple[str, ...],
    excluded_from_footer: tuple[str, ...],
) -> None:
    report = ProbeReport(
        version="3.12.0",
        packages=packages,
        groups=[
            GroupResult(key, title, packages=pkgs, optional_keys=opt)
            for key, title, pkgs, opt in group_specs
        ],
    )
    msg = report.render(include_install_footer=include_footer)
    lines = msg.splitlines()
    uv_lines = [line for line in lines if line.startswith("uv pip install ")]
    pip_lines = [line for line in lines if line.startswith("pip install ")]
    if not expect_footer:
        assert not uv_lines
        assert not pip_lines
        return
    assert len(uv_lines) == 1
    assert len(pip_lines) == 1
    assert lines.index(uv_lines[0]) < lines.index(pip_lines[0])
    uv_tokens = uv_lines[0].split()
    pip_tokens = pip_lines[0].split()
    for pkg in expected_in_footer:
        assert uv_tokens.count(pkg) == 1
        assert pip_tokens.count(pkg) == 1
    for pkg in excluded_from_footer:
        assert pkg not in uv_tokens
        assert pkg not in pip_tokens


@pytest.mark.parametrize(
    ("packages", "expect_ready_engine", "expect_footer", "expected_in_footer"),
    [
        pytest.param(
            {"docling": "present", "rapidocr": "present", "onnxruntime": "present", "css_inline": "present"},
            "Docling",
            False,
            (),
            id="docling_ready",
        ),
        pytest.param(
            {"docling": None, "rapidocr": None, "css_inline": "present", "paddleocr": "present", "paddle": "present"},
            "Paddle",
            False,
            (),
            id="paddle_ready_fallback",
        ),
        pytest.param(
            {"docling": "present", "rapidocr": None, "onnxruntime": None, "css_inline": "present"},
            None,
            True,
            ("rapidocr", "onnxruntime"),
            id="docling_without_rapidocr_not_ready",
        ),
        pytest.param(
            {"docling": "present", "rapidocr": "present", "onnxruntime": None, "css_inline": "present"},
            None,
            True,
            ("onnxruntime",),
            id="docling_without_onnxruntime_not_ready",
        ),
        pytest.param(
            {
                "docling": "present",
                "rapidocr": "present",
                "onnxruntime": None,
                "css_inline": "present",
                "paddleocr": "present",
                "paddle": "present",
            },
            None,
            True,
            ("onnxruntime",),
            id="docling_paddle_without_onnxruntime_not_ready",
        ),
        pytest.param(
            {},
            None,
            True,
            ("docling", "rapidocr", "css-inline", "onnxruntime"),
            id="all_ocr_missing_not_ready",
        ),
    ],
)
def test_probe_report_vision_ocr_structure(
    packages: dict[str, str | None],
    expect_ready_engine: str | None,
    expect_footer: bool,
    expected_in_footer: tuple[str, ...],
) -> None:
    report = ProbeReport(
        version="3.12.0",
        packages=packages,
        groups=[
            GroupResult(
                "vision",
                "Vision Libraries",
                packages=_VISION_PACKAGE_KEYS,
                optional_keys=_VISION_OPTIONAL_KEYS,
            )
        ],
    )
    if expect_ready_engine:
        assert report.collect_missing_keys() == []
    else:
        assert report.collect_missing_keys() != []
    msg = report.render(include_install_footer=True)
    if expect_ready_engine:
        assert f"({expect_ready_engine})" in msg
    uv_lines = [line for line in msg.splitlines() if line.startswith("uv pip install ")]
    assert bool(uv_lines) is expect_footer
    if expect_footer:
        for pkg in expected_in_footer:
            assert pkg in uv_lines[0]
        for opt in ("paddleocr", "ultralytics", "skimage"):
            assert opt not in uv_lines[0]


def test_vision_optional_ocr_engines_listed_never_missing():
    report = ProbeReport(
        version="3.12.0",
        packages={
            "docling": "present",
            "rapidocr": "present",
            "onnxruntime": "present",
            "css_inline": "present",
            "tesseract": "present",
        },
        groups=[
            GroupResult(
                "vision",
                "Vision Libraries",
                packages=_VISION_PACKAGE_KEYS,
                optional_keys=_VISION_OPTIONAL_KEYS,
            )
        ],
    )
    assert report.collect_missing_keys() == []
    msg = report.render(include_install_footer=True)
    vision_line = next(line for line in msg.splitlines() if line.startswith("Vision Libraries:"))
    assert "tesseract" in vision_line
    assert "easyocr" not in msg
    assert "ocrmac" not in msg
    assert "uv pip install " not in msg


@pytest.mark.parametrize(
    ("backend", "packages", "expect_ready", "expected_lines", "absent_lines"),
    [
        pytest.param(
            "easyocr",
            {"docling": "present", "easyocr": "present", "css_inline": "present"},
            True,
            ("OCR: ready (Docling)", "OCR engine: easyocr"),
            ("Missing (OCR):",),
            id="easyocr_present",
        ),
        pytest.param(
            "easyocr",
            {"docling": "present", "css_inline": "present"},
            False,
            ("OCR: not ready", "OCR engine: easyocr", "Missing (OCR): easyocr", "uv pip install easyocr"),
            (),
            id="easyocr_missing",
        ),
        pytest.param(
            "rapidocr_torch",
            {"docling": "present", "rapidocr": "present", "torch": "present", "css_inline": "present"},
            True,
            ("OCR: ready (Docling)", "OCR engine: rapidocr_torch"),
            ("Missing (OCR):",),
            id="rapidocr_torch_present",
        ),
        pytest.param(
            "rapidocr_torch",
            {"docling": "present", "rapidocr": "present", "css_inline": "present"},
            False,
            ("OCR: not ready", "OCR engine: rapidocr_torch", "Missing (OCR): torch", "uv pip install torch"),
            (),
            id="rapidocr_torch_missing",
        ),
        pytest.param(
            "tesseract",
            {"docling": "present", "tesseract": "present", "css_inline": "present"},
            True,
            ("OCR: ready (Docling)", "OCR engine: tesseract"),
            ("Missing (OCR):",),
            id="tesseract_present",
        ),
        pytest.param(
            "tesseract",
            {"docling": "present", "css_inline": "present"},
            False,
            ("OCR: not ready", "OCR engine: tesseract", "Missing (OCR): tesseract", "Install the Tesseract OCR program"),
            ("uv pip install",),
            id="tesseract_missing",
        ),
        pytest.param(
            "nemotron",
            {"docling": "present", "nemotron_ocr": "present", "css_inline": "present"},
            True,
            ("OCR: ready (Docling)", "OCR engine: nemotron"),
            ("Missing (OCR):",),
            id="nemotron_present_no_error",
        ),
        pytest.param(
            "nemotron",
            {
                "docling": "present",
                "nemotron_ocr": "present",
                "css_inline": "present",
                _NEMOTRON_RUNTIME_ERROR_KEY: "Nemotron OCR requires CUDA",
            },
            False,
            ("OCR: not ready", "OCR engine: nemotron", "Nemotron OCR cannot start in this venv: Nemotron OCR requires CUDA"),
            ("uv pip install",),
            id="nemotron_runtime_error",
        ),
        pytest.param(
            "ocrmac",
            {"docling": "present", "css_inline": "present"},
            False,
            ("OCR: not ready", "OCR engine: ocrmac", "Missing (OCR): ocrmac", "Apple Vision OCR runs only on macOS"),
            ("uv pip install",),
            id="ocrmac_absent",
        ),
        pytest.param(
            "bogus_engine",
            {"docling": "present", "css_inline": "present"},
            False,
            ("OCR: not ready", "OCR engine: bogus_engine", "This OCR engine is not known to WriterAgent."),
            ("Missing (OCR):", "uv pip install"),
            id="unknown_engine",
        ),
        pytest.param(
            "rapidocr_paddle",
            {"docling": "present", "rapidocr": "present", "css_inline": "present"},
            False,
            ("OCR: not ready", "OCR engine: rapidocr_paddle", "Missing (OCR): paddle"),
            (),
            id="rapidocr_paddle_missing_listed_once",
        ),
    ],
)
def test_vision_readiness_by_ocr_backend(backend, packages, expect_ready, expected_lines, absent_lines):
    report = ProbeReport(
        version="3.12.0",
        packages=packages,
        groups=[
            GroupResult(
                "vision",
                "Vision Libraries",
                packages=_VISION_PACKAGE_KEYS,
                optional_keys=_VISION_OPTIONAL_KEYS,
                ocr_backend=backend,
            )
        ],
    )
    if expect_ready:
        assert report.collect_missing_keys() == []
    msg = report.render(include_install_footer=True)
    for fragment in expected_lines:
        assert fragment in msg
    for fragment in absent_lines:
        assert fragment not in msg
    if backend == "rapidocr_paddle":
        optional_lines = [line for line in msg.splitlines() if line.startswith("Optional (not installed):")]
        assert all("paddle" not in line.replace("paddleocr", "") for line in optional_lines)


def test_attach_external_probes_vision_uses_configured_ocr_backend():
    from plugin.scripting.venv_diagnostics import _attach_external_probes

    report = ProbeReport(version="3.12.0")
    with (
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", return_value=({}, None)),
        patch("plugin.scripting.venv_diagnostics.merge_vision_params", return_value={"ocr_backend": "easyocr"}),
    ):
        group = _attach_external_probes("/x/python", report, "vision", "Vision Libraries")
    assert group.ocr_backend == "easyocr"


@pytest.mark.parametrize(
    ("packages", "expect_tts_hint", "expect_whisper_hint", "expect_no_mic_hint"),
    [
        pytest.param(
            {"sounddevice": "present", "input_device": "present", "kokoro_onnx": "present", "soundfile": "present", "piper": "present", "faster_whisper": "present"},
            False,
            False,
            False,
            id="all_audio_present",
        ),
        pytest.param(
            {"sounddevice": "present", "input_device": None, "kokoro_onnx": "present", "soundfile": "present", "piper": "present", "faster_whisper": "present"},
            False,
            False,
            True,
            id="no_mic_detected",
        ),
        pytest.param(
            {"sounddevice": "present", "input_device": "present", "kokoro_onnx": "present", "soundfile": "present", "piper": "present", "faster_whisper": None},
            False,
            True,
            False,
            id="whisper_missing_only",
        ),
        pytest.param(
            {"sounddevice": "present", "input_device": "present", "kokoro_onnx": None, "soundfile": "present", "piper": "present", "faster_whisper": "present"},
            True,
            False,
            False,
            id="tts_missing_only",
        ),
    ],
)
def test_probe_report_audio_structure(
    packages: dict[str, str | None],
    expect_tts_hint: bool,
    expect_whisper_hint: bool,
    expect_no_mic_hint: bool,
) -> None:
    report = ProbeReport(
        version="3.13.0",
        packages=packages,
        groups=[
            GroupResult(
                "audio",
                "Audio Recording & Speech",
                packages=_AUDIO_PACKAGE_KEYS,
                optional_keys=_AUDIO_OPTIONAL_KEYS,
            )
        ],
    )
    msg = report.render(include_install_footer=True)
    assert ("Local TTS engines (optional):" in msg) is expect_tts_hint
    assert ("Local Whisper (optional):" in msg) is expect_whisper_hint
    assert ("No microphone input devices detected." in msg) is expect_no_mic_hint
    assert ("input_device" not in msg) is expect_no_mic_hint
    uv_lines = [line for line in msg.splitlines() if line.startswith("uv pip install ")]
    assert not uv_lines


def test_probe_report_audio_linux_portaudio_hint() -> None:
    report = ProbeReport(
        version="3.12.0",
        packages={"sounddevice": None},
        groups=[GroupResult("audio", "Audio Recording & Speech", packages=("sounddevice",))],
    )
    msg = report.render()
    assert ("PortAudio" in msg) is sys.platform.startswith("linux")


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
        patch(
            "plugin.scripting.venv_diagnostics._run_named_json_probe",
            side_effect=lambda exe, name, timeout=None: (
                ({"sounddevice": "present", "input_device": "present"}, None)
                if name == "audio"
                else (
                    (vision_probes, None)
                    if name == "vision"
                    else (({}, None) if name in {"nlp", "vector_search"} else orig_named_probe(exe, name))
                )
            ),
        ),
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
        patch(
            "plugin.scripting.venv_diagnostics._run_named_json_probe",
            side_effect=lambda exe, name, timeout=None: (
                (vector_search_probes, None)
                if name == "vector_search"
                else (({}, None) if name in {"nlp", "vision"} else orig_named_probe(exe, name))
            ),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Vector Search Libraries" in msg
    assert "Vector Search Libraries: envwrap, sqlite_vec" in msg
    assert "Missing: sentence_transformers" in msg


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
        patch(
            "plugin.scripting.venv_diagnostics._run_named_json_probe",
            side_effect=lambda exe, name, timeout=None: (
                ({"sounddevice": "present", "input_device": "present"}, None)
                if name == "audio"
                else (
                    ({}, timeout_hint)
                    if name == "vision"
                    else (({}, None) if name in {"nlp", "vector_search"} else orig_named_probe(exe, name))
                )
            ),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Vision Libraries" in msg
    assert timeout_hint in msg
    assert "Missing (OCR): docling" in msg
    assert "To install remaining packages:" in msg


@pytest.mark.parametrize("group", ["vector_search", "vision", "nlp"])
def test_probe_packages_subprocess_timeout(group):
    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _run_named_json_probe("/x/python", group)
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_run_venv_self_check_with_progress_reports_duckdb():
    from plugin.scripting.venv_worker import ExecRequest

    def fake_execute(req: ExecRequest):
        script = req.code or ""
        if "platform" in script:
            return {"status": "ok", "result": {"v": "3.12.0", "arch": "x86_64"}}
        if "pint" in script:
            return {"status": "ok", "result": "present"}
        return {"status": "ok", "result": None}

    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = fake_execute
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch(
            "plugin.scripting.venv_diagnostics._run_named_json_probe",
            side_effect=lambda exe, name, timeout=None: (
                ({}, None) if name in {"nlp", "vector_search", "vision"} else orig_named_probe(exe, name)
            ),
        ),
    ):
        ok, msg = run_venv_self_check_with_progress("/x/python", lambda text: None)
    assert ok is True
    assert "Data Engineering Libraries: pint" in msg
    assert "duckdb" in msg


def test_probe_hints_translate_at_call_time(monkeypatch) -> None:
    """Hints call _() at call time; a module-level _() would freeze the catalog before init_i18n(ctx)."""
    import plugin.scripting.venv_diagnostics as diag

    def fake_translate(message: str) -> str:
        return f"LOCALE:{message}"

    monkeypatch.setattr(diag, "_", fake_translate)
    monkeypatch.setattr(
        diag.subprocess,
        "run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30)),
    )
    _probes, hint = diag._run_named_json_probe("/x/python", "nlp")
    assert hint is not None
    assert hint.startswith("LOCALE:")
    assert "timed out" in hint
    assert diag._audio_linux_portaudio_hint().startswith("LOCALE:")


def test_attach_external_probes_shares_one_helper(monkeypatch):
    from plugin.scripting import venv_diagnostics as vd

    monkeypatch.setattr(
        vd,
        "_run_named_json_probe",
        lambda exe, name, timeout=None: ({"sounddevice": True}, None) if name == "audio" else ({}, None),
    )

    single_report = vd.ProbeReport()
    vd._attach_external_probes("/usr/bin/python", single_report, "audio", "Audio")
    assert any(g.key == "audio" for g in single_report.groups)
    assert not any(g.key in {"ui", "nlp", "vision", "vector_search"} for g in single_report.groups)

    report = vd.ProbeReport()
    for group in ("audio", "ui", "nlp", "vision", "vector_search"):
        vd._attach_external_probes("/usr/bin/python", report, group, group)
    assert {g.key for g in report.groups} == {"audio", "ui", "nlp", "vision", "vector_search"}


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
        probes, hint = _run_named_json_probe("/x/python", "vision")
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
        probes, hint = _run_named_json_probe("/x/python", "vision")
    assert probes == {}
    assert hint is not None


def test_parse_probe_stdout_accepts_marker_glued_to_banner():
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


def test_probe_scripts_compile_and_ui_probe_round_trips():
    scripts = [
        _AUDIO_PROBE_SCRIPT,
        _NLP_PROBE_SCRIPT,
        _VISION_PROBE_SCRIPT,
        _VECTOR_SEARCH_PROBE_SCRIPT,
        _UI_PROBE_SCRIPT,
        _DUCKDB_PROBE_SCRIPT,
    ]
    for script in scripts:
        compile(script, "<test_probe_script>", "exec")

    proc = subprocess.run(
        [sys.executable, "-c", _UI_PROBE_SCRIPT],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = _parse_probe_stdout(proc.stdout)
    assert parsed is not None
    assert set(_UI_PACKAGE_KEYS) <= set(parsed)

    vision_proc = subprocess.run(
        [sys.executable, "-c", _VISION_PROBE_SCRIPT],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert vision_proc.returncode == 0, vision_proc.stderr
    vision_parsed = _parse_probe_stdout(vision_proc.stdout)
    assert vision_parsed is not None
    assert set(_OCR_ENGINE_KEYS) <= set(vision_parsed)
    from plugin.vision.vision_common import OCR_BACKENDS, ocr_module_package

    expected_engine_pkgs = {ocr_module_package(e) for row in OCR_BACKENDS.values() for e in row.modules}
    assert expected_engine_pkgs <= set(vision_parsed)


def test_run_json_probe_keeps_payload_on_nonzero_exit():
    completed = subprocess.CompletedProcess(
        args=["python"],
        returncode=1,
        stdout='WA_PROBE_JSON {"docling": "present"}\n',
        stderr="atexit failed",
    )
    with patch("plugin.scripting.venv_diagnostics.subprocess.run", return_value=completed):
        probes, hint = _run_named_json_probe("/x/python", "vision")
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
        probes, hint = _run_named_json_probe("/x/python", "vision")
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
            _run_named_json_probe("/x/python", "vision")
    assert recorded_kwargs.get("watch_bus") is True


def test_probe_venv_path_messages_are_translated(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.scripting.venv_diagnostics._", lambda message: f"L:{message}")

    with patch("plugin.scripting.venv_diagnostics.resolve_libreoffice_python", return_value=None):
        ok, msg = probe_venv_path_with_progress("", lambda unused: None)
        assert ok is False
        assert msg.startswith("L:No process interpreter:")

    ok, msg = probe_venv_path_with_progress(str(tmp_path), lambda unused: None)
    assert ok is False
    assert msg.startswith("L:No python found.")

    ok, msg = probe_venv_path_with_progress(str(tmp_path / "missing"), lambda unused: None)
    assert ok is False
    assert msg.startswith("L:Path not found: ")

    ok, msg = probe_venv_path_with_progress(__file__, lambda unused: None)
    assert ok is False
    assert msg.startswith("L:Not a Python executable: ")




@pytest.mark.parametrize(
    ("name", "expected_timeout_attr"),
    [("vision", "VISION_PROBE_TIMEOUT_SEC"), ("vector_search", "VECTOR_SEARCH_PROBE_TIMEOUT_SEC")],
)
def test_named_probe_uses_group_specific_timeout(name, expected_timeout_attr):
    """Vision and vector-search probes import heavy stacks; they run with their own timeouts."""
    import plugin.scripting.config_limits as limits
    import plugin.scripting.venv_diagnostics as diag

    expected = getattr(limits, expected_timeout_attr)
    assert diag._JSON_PROBE_SPECS[name].timeout_sec == expected
    with patch("plugin.scripting.venv_diagnostics._run_json_probe", return_value=({}, None)) as mock_probe:
        diag._run_named_json_probe("/x/python", name)
    assert mock_probe.call_args.args[2] == expected

    # The spec value (not a shared default) is what reaches the subprocess probe.
    sentinel = 987.0
    patched_specs = {**diag._JSON_PROBE_SPECS, name: diag._JSON_PROBE_SPECS[name]._replace(timeout_sec=sentinel)}
    with (
        patch.object(diag, "_JSON_PROBE_SPECS", patched_specs),
        patch("plugin.scripting.venv_diagnostics._run_json_probe", return_value=({}, None)) as mock_probe,
    ):
        diag._run_named_json_probe("/x/python", name)
    assert mock_probe.call_args.args[2] == sentinel


def test_ocr_engine_probe_lists_tesseract_in_default_install_folder():
    ns: dict[str, Any] = {"out": {}}

    def fake_run(cmd, **_kwargs):
        if cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout="tesseract 5.5.4\n leptonica-1.85.0\n", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="List of available languages in \"/usr/share/tesseract-ocr/5/tessdata/\" (2):\neng\nosd\n", stderr=""
        )

    with (
        patch("shutil.which", side_effect=lambda c: c if c == "/opt/homebrew/bin/tesseract" else None),
        patch("subprocess.run", side_effect=fake_run),
    ):
        exec(_OCR_ENGINE_PROBE_BODY, ns)
    assert ns["out"]["tesseract"] == "present"
    assert ns["out"]["tesseract_version"] == "tesseract 5.5.4"
    assert ns["out"]["tesseract_langs"] == ["eng", "osd"]


def test_ocr_engine_probe_reports_broken_tesseract():
    ns: dict[str, Any] = {"out": {}}
    with (
        patch("shutil.which", side_effect=lambda c: c if c == "/opt/homebrew/bin/tesseract" else None),
        patch("subprocess.run", side_effect=OSError("Exec format error")),
    ):
        exec(_OCR_ENGINE_PROBE_BODY, ns)
    assert ns["out"]["tesseract"] is None
    assert "Exec format error" in ns["out"]["tesseract_broken"]


def _nemotron_probe_modules(validate_runtime, *, broken_pipeline: bool = False, broken_v2: bool = False):
    import types

    class _FakeNemotronOcrModel:
        @classmethod
        def validate_runtime(cls, accelerator_options):
            validate_runtime(accelerator_options)

    accel_mod = types.ModuleType("docling.datamodel.accelerator_options")
    accel_mod.AcceleratorOptions = lambda **kw: types.SimpleNamespace(**kw)  # type: ignore[attr-defined]
    nemotron_mod = types.ModuleType("docling.models.stages.ocr.nemotron_ocr_model")
    nemotron_mod.NemotronOcrModel = _FakeNemotronOcrModel  # type: ignore[attr-defined]
    nemo_root = types.ModuleType("nemotron_ocr")
    nemo_inf = types.ModuleType("nemotron_ocr.inference")
    nemo_ckpt = types.ModuleType("nemotron_ocr.inference.pipeline")
    nemo_ckpt.CHECKPOINT_FILES = ()  # type: ignore[attr-defined]
    nemo_pipe = types.ModuleType("nemotron_ocr.inference.pipeline_v2")
    nemo_pipe.NemotronOCRV2 = object  # type: ignore[attr-defined]
    return {
        "docling.datamodel.accelerator_options": accel_mod,
        "docling.models.stages.ocr.nemotron_ocr_model": nemotron_mod,
        "nemotron_ocr": nemo_root,
        "nemotron_ocr.inference": nemo_inf,
        "nemotron_ocr.inference.pipeline": None if broken_pipeline else nemo_ckpt,
        "nemotron_ocr.inference.pipeline_v2": None if broken_v2 else nemo_pipe,
    }


@pytest.mark.parametrize(
    ("raises", "broken_pipeline", "broken_v2", "expected"),
    [
        (RuntimeError("Nemotron OCR requires Python 3.12."), False, False, "Nemotron OCR requires Python 3.12."),
        (None, True, False, "nemotron_ocr.inference.pipeline"),
        (None, False, True, "nemotron_ocr.inference.pipeline_v2"),
        (None, False, False, None),
    ],
)
def test_ocr_engine_probe_reports_nemotron_runtime_error(raises, broken_pipeline, broken_v2, expected):
    def _validate(_options):
        if raises is not None:
            raise raises

    ns: dict[str, Any] = {"out": {"nemotron_ocr": "present"}}
    with (
        patch.object(sys, "argv", ["-c"]),
        patch("shutil.which", return_value=None),
        patch.dict(sys.modules, _nemotron_probe_modules(_validate, broken_pipeline=broken_pipeline, broken_v2=broken_v2)),
    ):
        exec(_OCR_ENGINE_PROBE_BODY, ns)

    assert ns["out"]["nemotron_ocr"] == "present"
    actual = ns["out"].get(_NEMOTRON_RUNTIME_ERROR_KEY)
    if expected is None:
        assert actual is None
    else:
        assert actual is not None and expected in actual


@pytest.mark.parametrize(
    ("argv", "expect_error"),
    [
        (["-c", "cpu"], True),
        (["-c", "cuda"], False),
        (["-c"], False),
    ],
)
def test_ocr_engine_probe_validates_nemotron_with_device(argv, expect_error):
    def _validate(options):
        if getattr(options, "device", "auto") == "cpu":
            raise RuntimeError("Nemotron OCR requires a CUDA accelerator.")

    ns: dict[str, Any] = {"out": {"nemotron_ocr": "present"}}
    with (
        patch.object(sys, "argv", argv),
        patch("shutil.which", return_value=None),
        patch.dict(sys.modules, _nemotron_probe_modules(_validate)),
    ):
        exec(_OCR_ENGINE_PROBE_BODY, ns)

    actual = ns["out"].get(_NEMOTRON_RUNTIME_ERROR_KEY)
    if expect_error:
        assert actual is not None and "CUDA accelerator" in actual
    else:
        assert actual is None


@pytest.mark.parametrize(
    ("group", "vision_params", "expected_tail"),
    [
        ("vision", {"device": " cpu "}, "cpu"),
        ("vision", {}, "script"),
        ("nlp", {"device": "cpu"}, "script"),
    ],
)
def test_named_probe_passes_configured_vision_device(group, vision_params, expected_tail):
    import plugin.scripting.venv_diagnostics as diag

    completed = subprocess.CompletedProcess([], 0, stdout=f"{diag._PROBE_JSON_PREFIX}{{}}\n", stderr="")
    with (
        patch("plugin.scripting.venv_diagnostics.merge_vision_params", return_value=vision_params),
        patch("plugin.scripting.venv_diagnostics.wrap_command_for_sandbox", side_effect=lambda cmd, **_kw: cmd),
        patch("plugin.scripting.venv_diagnostics.subprocess.run", return_value=completed) as mock_run,
    ):
        diag._run_named_json_probe("/x/python", group)

    cmd = mock_run.call_args.args[0]
    if expected_tail == "script":
        assert cmd == ["/x/python", "-c", diag._JSON_PROBE_SPECS[group].script]
    else:
        assert cmd == ["/x/python", "-c", diag._JSON_PROBE_SPECS[group].script, expected_tail]


def test_vision_nemotron_runtime_error_shown_not_missing():
    report = ProbeReport(
        version="3.13.0",
        packages={
            "docling": "present",
            "rapidocr": "present",
            "onnxruntime": "present",
            "css_inline": "present",
            "nemotron_ocr": "present",
            _NEMOTRON_RUNTIME_ERROR_KEY: "Nemotron OCR requires CUDA 13.x, but the current PyTorch runtime reports CUDA '12.8'.",
        },
        groups=[
            GroupResult(
                "vision",
                "Vision Libraries",
                packages=_VISION_PACKAGE_KEYS,
                optional_keys=_VISION_OPTIONAL_KEYS,
            )
        ],
    )
    assert report.collect_missing_keys() == []
    msg = report.render(include_install_footer=True)
    vision_line = next(line for line in msg.splitlines() if line.startswith("Vision Libraries:"))
    assert "nemotron_ocr" in vision_line
    assert "Nemotron OCR cannot start in this venv: Nemotron OCR requires CUDA 13.x" in msg
    assert "uv pip install " not in msg


def test_import_probe_reports_broken_not_absent(tmp_path):
    (tmp_path / "wa_broken_mod.py").write_text("import wa_no_such_dep_xyz\n", encoding="utf-8")
    lazy_pkg = tmp_path / "wa_lazy_pkg"
    lazy_pkg.mkdir()
    (lazy_pkg / "__init__.py").write_text(
        "def __getattr__(name):\n    raise ImportError('libGL.so.1 missing')\n",
        encoding="utf-8",
    )
    script = _import_probe_script(
        _import_try_block("wa_absent_pkg", "wa_absent_pkg", report_broken=True),
        _import_try_block("wa_broken_mod", "wa_broken_mod", report_broken=True),
        _import_try_block("wa_lazy_pkg", "wa_lazy_pkg", name="LazyCls", report_broken=True),
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")},
    )
    assert proc.returncode == 0
    parsed = _parse_probe_stdout(proc.stdout)
    assert parsed is not None
    assert parsed["wa_absent_pkg"] is None
    assert "wa_absent_pkg_broken" not in parsed
    assert parsed["wa_broken_mod"] is None
    assert "wa_no_such_dep_xyz" in parsed["wa_broken_mod_broken"]
    assert parsed["wa_lazy_pkg"] is None
    assert "libGL.so.1 missing" in parsed["wa_lazy_pkg_broken"]


@pytest.mark.parametrize(
    ("extra_probes", "expected_ocr", "expect_upgrade_footer"),
    [
        ({}, "OCR: not ready", True),
        ({"paddleocr": "present", "paddle": "present"}, "OCR: ready (Paddle)", False),
    ],
)
def test_self_check_old_docling_is_missing_with_upgrade_line(
    extra_probes, expected_ocr, expect_upgrade_footer
):
    from plugin.vision.vision_common import DOCLING_MIN_VERSION, DOCLING_REQUIREMENT

    old_ver = "%d.%d.0" % (DOCLING_MIN_VERSION[0], DOCLING_MIN_VERSION[1] - 1)
    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = [
        {"status": "ok", "result": {"v": "3.13.0", "arch": "x86_64"}},
        *({"status": "ok", "result": "present"} for _ in range(50)),
    ]

    def fake_probe(_exe, group):
        if group == "vision":
            return (
                {
                    "docling": "present",
                    "docling_version": old_ver,
                    "rapidocr": "present",
                    "onnxruntime": "present",
                    "css_inline": "present",
                    **extra_probes,
                },
                None,
            )
        return ({}, None)

    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", side_effect=fake_probe),
    ):
        ok, msg = run_venv_self_check_with_progress(
            "/x/python",
            lambda _text: None,
            include_vector_search=False,
            include_audio=False,
        )

    assert ok is True
    assert expected_ocr in msg
    assert f"docling is installed but does not work: version {old_ver}" in msg
    if expect_upgrade_footer:
        assert "Missing (OCR): docling" in msg
        uv_line = next(line for line in msg.splitlines() if line.startswith("uv pip install "))
        assert f'"{DOCLING_REQUIREMENT}"' in uv_line
    else:
        assert "Missing (OCR)" not in msg
        assert DOCLING_REQUIREMENT not in msg


def test_self_check_current_docling_shows_version():
    from plugin.vision.vision_common import DOCLING_MIN_VERSION

    cur_ver = "%d.%d.0" % DOCLING_MIN_VERSION
    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = [
        {"status": "ok", "result": {"v": "3.13.0", "arch": "x86_64"}},
        *({"status": "ok", "result": "present"} for _ in range(50)),
    ]

    def fake_probe(_exe, group):
        if group == "vision":
            return (
                {
                    "docling": "present",
                    "docling_version": cur_ver,
                    "rapidocr": "present",
                    "onnxruntime": "present",
                    "css_inline": "present",
                },
                None,
            )
        return ({}, None)

    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", side_effect=fake_probe),
    ):
        ok, msg = run_venv_self_check_with_progress(
            "/x/python",
            lambda _text: None,
            include_vector_search=False,
            include_audio=False,
        )

    assert ok is True
    assert "OCR: ready (Docling)" in msg
    assert f"Docling: {cur_ver}" in msg


def test_vision_broken_engine_shown_not_optional():
    report = ProbeReport(
        version="3.13.0",
        packages={
            "docling": "present",
            "rapidocr": "present",
            "onnxruntime": "present",
            "css_inline": "present",
            "paddle": None,
            "paddle_broken": "libcuda.so.1 missing",
        },
        groups=[
            GroupResult(
                "vision",
                "Vision Libraries",
                packages=_VISION_PACKAGE_KEYS,
                optional_keys=_VISION_OPTIONAL_KEYS,
            )
        ],
    )
    msg = report.render(include_install_footer=True)
    assert "paddle is installed but does not work: libcuda.so.1 missing" in msg
    optional_line = next(line for line in msg.splitlines() if line.startswith("Optional (not installed):"))
    assert "paddle," not in optional_line and not optional_line.endswith(" paddle")


@pytest.mark.parametrize(
    ("langs", "expected_line"),
    [
        (["eng", "osd"], "tesseract 5.5.4; languages: eng, osd"),
        ([], "tesseract 5.5.4; languages: none"),
    ],
)
def test_vision_tesseract_version_and_langs_rendered(langs, expected_line):
    report = ProbeReport(
        version="3.13.0",
        packages={
            "docling": "present",
            "rapidocr": "present",
            "onnxruntime": "present",
            "css_inline": "present",
            "tesseract": "present",
            "tesseract_version": "tesseract 5.5.4",
            "tesseract_langs": langs,
        },
        groups=[
            GroupResult(
                "vision",
                "Vision Libraries",
                packages=_VISION_PACKAGE_KEYS,
                optional_keys=_VISION_OPTIONAL_KEYS,
            )
        ],
    )
    assert expected_line in report.render()
