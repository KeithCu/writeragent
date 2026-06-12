# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted vision runs outside LocalPythonExecutor (docling is not a sandbox import)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.venv_sandbox import run_sandboxed_code

_VISION_STUB = """\
from plugin.vision.venv.vision import run_vision as _run
result = _run(data["spec"], data.get("image"), data.get("context") or {})
"""


def test_trusted_vision_stub_bypasses_local_executor():
    fake_result = {"status": "ok", "helper": "extract_text", "full_text": "hi"}

    with patch("plugin.scripting.venv_sandbox._run_trusted_vision_payload", return_value={"status": "ok", "result": fake_result}) as mock_run:
        response = run_sandboxed_code(_VISION_STUB, data={"spec": {"helper": "extract_text", "params": {}}})

    assert response["status"] == "ok"
    assert response["result"] == fake_result
    mock_run.assert_called_once()


def test_trusted_vision_payload_calls_run_vision():
    with patch("plugin.vision.venv.vision.run_vision", return_value={"status": "ok", "helper": "extract_text", "full_text": "x"}) as mock_run:
        from plugin.scripting.venv_sandbox import _run_trusted_vision_payload

        out = _run_trusted_vision_payload(
            {
                "spec": {"helper": "extract_text", "params": {"lang": "en"}},
                "image": b"png",
                "context": {"source": "selection"},
            }
        )

    assert out["status"] == "ok"
    assert out["result"]["full_text"] == "x"
    mock_run.assert_called_once_with(
        {"helper": "extract_text", "params": {"lang": "en"}},
        b"png",
        context={"source": "selection"},
    )
