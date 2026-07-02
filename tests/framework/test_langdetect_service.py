# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.langdetect_service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.langdetect_service import detect_languages
from plugin.framework.constants import EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import long_trusted_worker_timeout_sec
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


def test_detect_languages_happy_path(ctx) -> None:
    worker_result = {
        "status": "ok",
        "result": {"languages": ["fr-FR", None]},
    }
    with (
        patch("plugin.framework.client.langdetect_service.embeddings_worker_timeout_sec", return_value=long_trusted_worker_timeout_sec()),
        patch("plugin.framework.client.langdetect_service.run_code_in_user_venv", return_value=worker_result) as mock_run,
    ):
        out = detect_languages(ctx, ["Bonjour.", ""])

    assert out == ["fr-FR", None]
    mock_run.assert_called_once()
    _args, kwargs = mock_run.call_args
    assert _args[0] is ctx
    assert "langdetect_rpc" in _args[1]
    assert kwargs["data"] == {"texts": ["Bonjour.", ""]}
    assert kwargs["session_id"] == f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:langdetect"
    assert kwargs["timeout_sec"] == long_trusted_worker_timeout_sec()
    assert kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS


def test_detect_languages_worker_error(ctx) -> None:
    with (
        patch("plugin.framework.client.langdetect_service.embeddings_worker_timeout_sec", return_value=long_trusted_worker_timeout_sec()),
        patch(
            "plugin.framework.client.langdetect_service.run_code_in_user_venv",
            return_value={"status": "error", "message": "Embeddings venv not configured"},
        ),
        pytest.raises(ToolExecutionError, match="Embeddings venv not configured"),
    ):
        detect_languages(ctx, ["Hi"])


def test_detect_languages_mismatched_batch(ctx) -> None:
    worker_result = {"status": "ok", "result": {"languages": ["en-US"]}}
    with (
        patch("plugin.framework.client.langdetect_service.embeddings_worker_timeout_sec", return_value=long_trusted_worker_timeout_sec()),
        patch("plugin.framework.client.langdetect_service.run_code_in_user_venv", return_value=worker_result),
        pytest.raises(ToolExecutionError, match="mismatched batch"),
    ):
        detect_languages(ctx, ["a", "b"])
