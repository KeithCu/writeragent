# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.embedding_client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client.embedding_client import EmbeddingBatch, embed_texts, get_embedding_model
from plugin.framework.constants import DEFAULT_EMBEDDING_MODEL, EMBEDDINGS_WORKER_SESSION_PREFIX, WORKER_POOL_EMBEDDINGS
from plugin.scripting.config_limits import EMBEDDINGS_WORKER_TIMEOUT_SEC
from plugin.framework.errors import ConfigError, ToolExecutionError
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


@pytest.fixture
def config_data():
    return {"embedding_provider": "local", "embedding_model": DEFAULT_EMBEDDING_MODEL}


def _mock_get_config(config_data):
    def _get(_ctx, key):
        return config_data.get(key, "")

    return _get


def test_get_embedding_model_default(ctx):
    with patch("plugin.framework.client.embedding_client.get_config", return_value=""):
        assert get_embedding_model(ctx) == DEFAULT_EMBEDDING_MODEL


def test_get_embedding_model_override(ctx):
    with patch("plugin.framework.client.embedding_client.get_config", return_value="BAAI/bge-small-en-v1.5"):
        assert get_embedding_model(ctx) == "BAAI/bge-small-en-v1.5"


def test_embed_texts_happy_path(ctx, config_data):
    worker_result = {
        "status": "ok",
        "result": {
            "model": DEFAULT_EMBEDDING_MODEL,
            "dim": 384,
            "vectors": [[0.1, 0.2], [0.3, 0.4]],
            "indices": [0, 2],
        },
    }

    with (
        patch("plugin.framework.client.embedding_client.get_config", side_effect=_mock_get_config(config_data)),
        patch("plugin.framework.client.embedding_client.embeddings_worker_timeout_sec", return_value=EMBEDDINGS_WORKER_TIMEOUT_SEC),
        patch("plugin.framework.client.embedding_client.run_code_in_user_venv", return_value=worker_result) as mock_run,
    ):
        batch = embed_texts(ctx, ["hello", "", "world"])

    assert isinstance(batch, EmbeddingBatch)
    assert batch.model == DEFAULT_EMBEDDING_MODEL
    assert batch.dim == 384
    assert batch.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert batch.indices == [0, 2]

    mock_run.assert_called_once()
    _args, kwargs = mock_run.call_args
    assert _args[0] is ctx
    assert "plugin.embeddings.venv.embeddings_index" in _args[1]
    assert kwargs["data"] == {"model": DEFAULT_EMBEDDING_MODEL, "texts": ["hello", "", "world"]}
    expected_session_slug = DEFAULT_EMBEDDING_MODEL.replace("/", "_").replace(":", "_")
    assert kwargs["session_id"] == f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:{expected_session_slug}"
    assert kwargs["timeout_sec"] == EMBEDDINGS_WORKER_TIMEOUT_SEC
    assert kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS


def test_embed_texts_custom_model_session_slug(ctx, config_data):
    worker_result = {
        "status": "ok",
        "result": {"model": "BAAI/bge-small-en-v1.5", "dim": 384, "vectors": [], "indices": []},
    }

    with (
        patch("plugin.framework.client.embedding_client.get_config", side_effect=_mock_get_config(config_data)),
        patch("plugin.framework.client.embedding_client.embeddings_worker_timeout_sec", return_value=EMBEDDINGS_WORKER_TIMEOUT_SEC),
        patch("plugin.framework.client.embedding_client.run_code_in_user_venv", return_value=worker_result) as mock_run,
    ):
        embed_texts(ctx, [], model="BAAI/bge-small-en-v1.5")

    assert mock_run.call_args.kwargs["session_id"] == f"{EMBEDDINGS_WORKER_SESSION_PREFIX}:BAAI_bge-small-en-v1.5"


def test_embed_texts_worker_error(ctx, config_data):
    with (
        patch("plugin.framework.client.embedding_client.get_config", side_effect=_mock_get_config(config_data)),
        patch("plugin.framework.client.embedding_client.embeddings_worker_timeout_sec", return_value=EMBEDDINGS_WORKER_TIMEOUT_SEC),
        patch(
            "plugin.framework.client.embedding_client.run_code_in_user_venv",
            return_value={"status": "error", "message": "sentence_transformers not installed"},
        ),
    ):
        with pytest.raises(ToolExecutionError, match="sentence_transformers"):
            embed_texts(ctx, ["hello"])


def test_embed_texts_unsupported_provider(ctx):
    with patch("plugin.framework.client.embedding_client.get_config", side_effect=lambda _c, k: "openrouter" if k == "embedding_provider" else ""):
        with pytest.raises(ConfigError, match="not implemented"):
            embed_texts(ctx, ["hello"])


def test_embed_texts_malformed_worker_result(ctx, config_data):
    with (
        patch("plugin.framework.client.embedding_client.get_config", side_effect=_mock_get_config(config_data)),
        patch("plugin.framework.client.embedding_client.embeddings_worker_timeout_sec", return_value=EMBEDDINGS_WORKER_TIMEOUT_SEC),
        patch(
            "plugin.framework.client.embedding_client.run_code_in_user_venv",
            return_value={"status": "ok", "result": {"dim": "bad"}},
        ),
    ):
        with pytest.raises(ToolExecutionError, match="malformed"):
            embed_texts(ctx, ["hello"])
