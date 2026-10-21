# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.writer.locale.langdetect_service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL
from plugin.writer.locale.langdetect_service import detect_languages
from plugin.framework.constants import EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.config_limits import long_trusted_worker_timeout_sec


@pytest.fixture
def ctx():
    return MagicMock()


def test_detect_languages_happy_path(ctx) -> None:
    worker_result = {"languages": ["fr-FR", None]}
    with (
        patch("plugin.writer.locale.langdetect_service.embeddings_worker_timeout_sec", return_value=long_trusted_worker_timeout_sec()),
        patch("plugin.writer.locale.langdetect_service.run_trusted_worker_action", return_value=worker_result) as mock_run,
    ):
        out = detect_languages(ctx, ["Bonjour.", ""])

    assert out == ["fr-FR", None]
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["domain"] == "langdetect"
    assert kwargs["additional_data"] == {"texts": ["Bonjour.", ""]}
    assert kwargs["session_id"] == f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:langdetect"
    assert kwargs["timeout_sec"] == long_trusted_worker_timeout_sec()
    assert kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS


def test_detect_languages_worker_error(ctx) -> None:
    with (
        patch("plugin.writer.locale.langdetect_service.embeddings_worker_timeout_sec", return_value=long_trusted_worker_timeout_sec()),
        patch(
            "plugin.writer.locale.langdetect_service.run_trusted_worker_action",
            side_effect=ToolExecutionError("Embeddings venv not configured", code="LANGDETECT_ERROR"),
        ),
        pytest.raises(ToolExecutionError, match="Embeddings venv not configured"),
    ):
        detect_languages(ctx, ["Hi"])


def test_detect_languages_mismatched_batch(ctx) -> None:
    worker_result = {"languages": ["en-US"]}
    with (
        patch("plugin.writer.locale.langdetect_service.embeddings_worker_timeout_sec", return_value=long_trusted_worker_timeout_sec()),
        patch("plugin.writer.locale.langdetect_service.run_trusted_worker_action", return_value=worker_result),
        pytest.raises(ToolExecutionError, match="mismatched batch"),
    ):
        detect_languages(ctx, ["a", "b"])


def test_detect_languages_already_contains_pip_install(ctx) -> None:
    msg = f"Missing langdetect package. Install with: {EMBEDDINGS_VENV_PIP_INSTALL}"
    with (
        patch("plugin.writer.locale.langdetect_service.run_trusted_worker_action", side_effect=ToolExecutionError(msg, code="LANGDETECT_ERROR")),
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        detect_languages(ctx, ["Hi"])
    err_msg = str(exc_info.value)
    assert err_msg.count(EMBEDDINGS_VENV_PIP_INSTALL) == 1
    assert err_msg.count("Install with:") == 1


def test_detect_languages_venv_not_configured_appends_hint(ctx) -> None:
    with (
        patch("plugin.writer.locale.langdetect_service.run_trusted_worker_action", side_effect=ToolExecutionError("Embeddings venv not configured", code="LANGDETECT_ERROR")),
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        detect_languages(ctx, ["Hi"])
    err_msg = str(exc_info.value)
    assert err_msg.startswith("Embeddings venv not configured")
    assert err_msg.count("Install with:") == 1
    assert EMBEDDINGS_VENV_PIP_INSTALL in err_msg


def test_detect_languages_unrelated_error_no_hint(ctx) -> None:
    with (
        patch("plugin.writer.locale.langdetect_service.run_trusted_worker_action", side_effect=ToolExecutionError("timeout talking to worker", code="LANGDETECT_ERROR")),
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        detect_languages(ctx, ["Hi"])
    err_msg = str(exc_info.value)
    assert err_msg == "timeout talking to worker"
    assert "Install with:" not in err_msg


def test_detect_languages_malformed_result(ctx) -> None:
    with (
        patch("plugin.writer.locale.langdetect_service.run_trusted_worker_action", return_value={"languages": "fr-FR"}),
        pytest.raises(ToolExecutionError, match="malformed"),
    ):
        detect_languages(ctx, ["Hi"])
