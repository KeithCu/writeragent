# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.client.vision_client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.client import run_vision
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import DOCLING_WORKER_TIMEOUT_SEC, VISION_WORKER_TIMEOUT_SEC
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


def test_run_vision_uses_docling_timeout_by_default(ctx):
    worker_result = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "hi",
        "regions": [],
        "warnings": [],
    }
    spec = {"helper": "extract_text", "params": {}}
    image = b"png-bytes"

    with patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run:
        result = run_vision(ctx, spec, image, context={"source": "selection"})

    assert result["full_text"] == "hi"
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["timeout_sec"] == DOCLING_WORKER_TIMEOUT_SEC
    assert mock_run.call_args.kwargs["timeout_sec"] != VISION_WORKER_TIMEOUT_SEC


def test_run_vision_uses_paddle_timeout_when_engine_paddle(ctx):
    worker_result = {"status": "ok", "helper": "extract_text", "full_text": "hi"}
    spec = {"helper": "extract_text", "params": {"engine": "paddle"}}

    with patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run:
        run_vision(ctx, spec, b"png")

    assert mock_run.call_args.kwargs["timeout_sec"] == VISION_WORKER_TIMEOUT_SEC


def test_run_vision_uses_dedicated_timeout_not_script_timeout(ctx):
    worker_result = {
        "status": "ok",
        "helper": "extract_text",
        "full_text": "hi",
        "regions": [],
        "warnings": [],
    }
    spec = {"helper": "extract_text", "params": {}}
    image = b"png-bytes"

    with patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run:
        result = run_vision(ctx, spec, image, context={"source": "selection"})

    assert result["full_text"] == "hi"
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["timeout_sec"] != 10


def test_run_vision_uses_config_worker_timeout(ctx):
    worker_result = {"status": "ok", "helper": "extract_text", "full_text": "hi"}
    spec = {"helper": "extract_text", "params": {}}

    with (
        patch("plugin.framework.config.get_config_int", return_value=999),
        patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run,
    ):
        run_vision(ctx, spec, b"png")

    assert mock_run.call_args.kwargs["timeout_sec"] == 999


def test_run_vision_worker_error(ctx):
    with patch(
        "plugin.scripting.client.run_trusted_worker_action",
        side_effect=ToolExecutionError("boom", code="VISION_ERROR"),
    ):
        with pytest.raises(ToolExecutionError, match="boom"):
            run_vision(ctx, {"helper": "extract_text", "params": {}}, b"x")
