# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted vision runs via run_trusted_action dispatch (not sandbox string stubs)."""

from __future__ import annotations

from unittest.mock import patch

from plugin.scripting.venv.trusted_dispatch import dispatch_vision


def test_dispatch_vision_calls_run_vision():
    with patch(
        "plugin.vision.venv.vision.run_vision",
        return_value={"status": "ok", "helper": "extract_text", "full_text": "x"},
    ) as mock_run:
        out = dispatch_vision(
            {
                "helper": "extract_text",
                "params": {"lang": "en"},
                "image": b"png",
                "context": {"source": "selection"},
            }
        )

    assert out["full_text"] == "x"
    mock_run.assert_called_once_with(
        {"helper": "extract_text", "params": {"lang": "en"}},
        b"png",
        {"source": "selection"},
    )
