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
    _NLP_PROBE_SCRIPT,
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
            {"numpy": None, "scipy": None, "docling": None, "rapidocr": None, "css_inline": None},
            [
                ("sci", "Scientific Libraries", ("numpy", "scipy"), ()),
                ("vision", "Vision Libraries", ("docling", "rapidocr", "css_inline", "numpy"), ()),
            ],
            True,
            ("numpy", "scipy", "docling", "rapidocr-paddle", "css-inline", "onnxruntime"),
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
            {"docling": "present", "rapidocr": "present", "css_inline": "present"},
            "Docling",
            False,
            (),
            id="docling_ready",
        ),
        pytest.param(
            {"docling": "present", "rapidocr": None, "css_inline": "present", "paddleocr": "present", "paddle": "present"},
            "Paddle",
            False,
            (),
            id="paddle_ready_fallback",
        ),
        pytest.param(
            {"docling": "present", "rapidocr": None, "css_inline": "present"},
            None,
            True,
            ("rapidocr-paddle", "onnxruntime"),
            id="docling_without_rapidocr_not_ready",
        ),
        pytest.param(
            {},
            None,
            True,
            ("docling", "rapidocr-paddle", "css-inline", "onnxruntime"),
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


def test_run_venv_self_check_with_progress_uses_vision_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    mock_probe = MagicMock(
        side_effect=lambda exe, name, timeout=None: (
            ({}, None) if name in {"nlp", "vector_search", "vision"} else orig_named_probe(exe, name)
        )
    )
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", mock_probe),
    ):
        run_venv_self_check_with_progress("/x/python", lambda text: None)
    mock_probe.assert_any_call("/x/python", "vision")


def test_run_venv_self_check_with_progress_uses_vector_search_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    mock_probe = MagicMock(
        side_effect=lambda exe, name, timeout=None: (
            ({}, None) if name in {"nlp", "vector_search", "vision"} else orig_named_probe(exe, name)
        )
    )
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", mock_probe),
    ):
        run_venv_self_check_with_progress("/x/python", lambda text: None)
    mock_probe.assert_any_call("/x/python", "vector_search")


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


def test_probe_vector_search_packages_subprocess_timeout():

    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _run_named_json_probe("/x/python", "vector_search")
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_probe_vision_packages_subprocess_timeout():

    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _run_named_json_probe("/x/python", "vision")
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
    """Module-level _() used to freeze the catalog from import, before init_i18n(ctx)."""
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


def test_probe_nlp_packages_subprocess_timeout():
    with patch(
        "plugin.scripting.venv_diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30),
    ):
        probes, hint = _run_named_json_probe("/x/python", "nlp")
    assert probes == {}
    assert hint is not None
    assert "timed out" in hint.lower()


def test_run_venv_self_check_with_progress_uses_nlp_probe_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "ok",
        "result": {"v": "3.12.0", "p": {}, "sci": [], "eda": [], "ui": []},
    }
    mock_nlp_probe = MagicMock(
        side_effect=lambda exe, name, timeout=None: (
            ({}, None) if name in {"nlp", "vector_search", "vision"} else orig_named_probe(exe, name)
        )
    )
    with (
        patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr),
        patch("plugin.scripting.venv_diagnostics._run_named_json_probe", mock_nlp_probe),
    ):
        run_venv_self_check_with_progress("/x/python", lambda text: None)
    mock_nlp_probe.assert_any_call("/x/python", "nlp")


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


def test_partial_scientific_group_uses_translated_title(monkeypatch) -> None:
    import plugin.scripting.venv_diagnostics as diag

    monkeypatch.setattr(diag, "_", lambda message: f"L:{message}")
    report = diag.ProbeReport(
        version="3.12.0",
        packages={"numpy": "present"},
        groups=[diag.GroupResult("sci", diag._("Scientific Libraries"), packages=("numpy",))],
    )
    msg = report.render()
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
