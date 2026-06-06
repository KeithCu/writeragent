# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-side vision runner (graphic export + trusted RPC)."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.scripting.vision_runner import get_selected_image_bytes, run_trusted_vision
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_get_selected_image_bytes_decodes_png():
    ctx = MagicMock()
    doc = MagicMock()
    raw = b"fake-png-bytes"
    with patch("plugin.scripting.vision_runner.get_selected_image_base64", return_value=base64.b64encode(raw).decode("ascii")):
        assert get_selected_image_bytes(ctx, doc) == raw


def test_get_selected_image_bytes_raises_when_no_selection():
    ctx = MagicMock()
    doc = MagicMock()
    with patch("plugin.scripting.vision_runner.get_selected_image_base64", return_value=None):
        with pytest.raises(ToolExecutionError) as exc:
            get_selected_image_bytes(ctx, doc)
    assert exc.value.code == "NO_IMAGE_SELECTED"


@patch("plugin.scripting.vision_runner.run_vision")
@patch("plugin.scripting.vision_runner.get_selected_image_bytes")
def test_run_trusted_vision_builds_payload(mock_bytes, mock_run_vision):
    ctx = MagicMock()
    doc = MagicMock()
    mock_bytes.return_value = b"png"
    mock_run_vision.return_value = {"status": "ok", "helper": "extract_text", "full_text": "hi"}

    result = run_trusted_vision(ctx, doc, helper="extract_text", params={"lang": "en"})

    assert result["full_text"] == "hi"
    mock_run_vision.assert_called_once_with(
        ctx,
        {"helper": "extract_text", "params": {"lang": "en"}},
        b"png",
        context={"source": "selection"},
    )


def test_run_trusted_vision_rejects_unknown_helper():
    ctx = MagicMock()
    doc = MagicMock()
    with pytest.raises(ToolExecutionError) as exc:
        run_trusted_vision(ctx, doc, helper="not_real")
    assert exc.value.code == "VISION_ERROR"
