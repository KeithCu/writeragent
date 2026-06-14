# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.embeddings_service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.client import embeddings_service
from plugin.framework.constants import DEFAULT_EMBEDDING_MODEL, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


def test_hybrid_search_happy_path(ctx):
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]}
    with patch("plugin.framework.client.embeddings_service.run_code_in_user_venv", return_value={"status": "ok", "result": worker_payload}) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            result = embeddings_service.hybrid_search(
                ctx,
                "/tmp/corpus.db",
                "dspy",
                20,
                model=DEFAULT_EMBEDDING_MODEL,
                near_slop=10,
            )
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert "hybrid_search" in mock_run.call_args.args[1]


def test_knn_search_happy_path(ctx):
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]}
    with patch("plugin.framework.client.embeddings_service.run_code_in_user_venv", return_value={"status": "ok", "result": worker_payload}) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            result = embeddings_service.knn_search(
                ctx,
                "/tmp/corpus.db",
                "query",
                5,
                model=DEFAULT_EMBEDDING_MODEL,
            )
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    payload = mock_run.call_args.kwargs["data"]
    assert payload["db_path"] == "/tmp/corpus.db"


def test_index_paragraphs_worker_error(ctx):
    with patch("plugin.framework.client.embeddings_service.run_code_in_user_venv", return_value={"status": "error", "message": "boom"}):
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with pytest.raises(ToolExecutionError, match="boom"):
                embeddings_service.index_paragraphs(
                    ctx,
                    "/tmp/corpus.db",
                    "/tmp/meta.json",
                    [],
                    model=DEFAULT_EMBEDDING_MODEL,
                )


def test_collection_stats_rpc(ctx):
    with patch("plugin.framework.client.embeddings_service.run_code_in_user_venv", return_value={"status": "ok", "result": {"chunk_count": 5}}):
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            result = embeddings_service.collection_stats(ctx, "/tmp/corpus.db", "/tmp/meta.json")
    assert result["chunk_count"] == 5


def test_maintain_folder_index_uses_heartbeat_rpc(ctx):
    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"mode": "cold", "indexed_paragraphs": 3}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            result = embeddings_service.maintain_folder_index(
                ctx,
                "/tmp/folder",
                model=DEFAULT_EMBEDDING_MODEL,
                mode="auto",
                search_mode="hybrid",
            )
    assert result["mode"] == "cold"
    assert mock_run.call_args.kwargs["allow_heartbeat"] is True
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert mock_run.call_args.kwargs["data"]["listing_root"] == "/tmp/folder"
    assert mock_run.call_args.kwargs["data"]["search_mode"] == "hybrid"


def test_maintain_folder_index_defaults_to_config_mode(ctx):
    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"mode": "cold"}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="llama_index"):
                embeddings_service.maintain_folder_index(ctx, "/tmp/folder", model=DEFAULT_EMBEDDING_MODEL)
    assert mock_run.call_args.kwargs["data"]["search_mode"] == "llama_index"


def test_hybrid_search_passes_config_search_mode(ctx):
    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"hits": []}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="llama_index"):
                embeddings_service.hybrid_search(ctx, "/tmp/corpus.db", "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    assert mock_run.call_args.kwargs["data"]["search_mode"] == "llama_index"


def test_hybrid_search_passes_rerank_model_when_llama_index_enabled(ctx):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_ENGLISH_SMALL

    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"hits": []}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="llama_index"):
                with patch(
                    "plugin.framework.client.embeddings_service._folder_search_rerank_options",
                    return_value={"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_ENGLISH_SMALL},
                ):
                    embeddings_service.hybrid_search(ctx, "/tmp/corpus.db", "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["data"]
    assert data["rerank_model"] == FOLDER_RERANK_MODEL_ENGLISH_SMALL
    assert data["use_mmr"] is True


def test_hybrid_search_disables_rerank_when_llama_index_rerank_off(ctx):
    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"hits": []}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="llama_index"):
                with patch(
                    "plugin.framework.client.embeddings_service._folder_search_rerank_options",
                    return_value={"use_mmr": False},
                ):
                    embeddings_service.hybrid_search(ctx, "/tmp/corpus.db", "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["data"]
    assert "rerank_model" not in data
    assert data["use_mmr"] is False


def test_hybrid_search_omits_rerank_when_disabled_for_hybrid_backend(ctx):
    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"hits": []}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="hybrid"):
                embeddings_service.hybrid_search(ctx, "/tmp/corpus.db", "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["data"]
    assert "rerank_model" not in data
    assert data["use_mmr"] is False


def test_hybrid_search_passes_rerank_model_when_hybrid_rerank_enabled(ctx):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_ENGLISH_SMALL

    with patch(
        "plugin.framework.client.embeddings_service.run_code_in_user_venv",
        return_value={"status": "ok", "result": {"hits": []}},
    ) as mock_run:
        with patch("plugin.framework.client.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.client.embeddings_service._folder_search_mode", return_value="hybrid"):
                with patch(
                    "plugin.framework.client.embeddings_service._folder_search_rerank_options",
                    return_value={"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_ENGLISH_SMALL},
                ):
                    embeddings_service.hybrid_search(ctx, "/tmp/corpus.db", "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["data"]
    assert data["rerank_model"] == FOLDER_RERANK_MODEL_ENGLISH_SMALL
    assert data["use_mmr"] is True


def test_folder_search_rerank_options_llama_index_enabled(ctx):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_MULTILINGUAL

    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=True):
        with patch(
            "plugin.framework.constants.resolve_folder_rerank_model",
            return_value=FOLDER_RERANK_MODEL_MULTILINGUAL,
        ):
            opts = embeddings_service._folder_search_rerank_options(ctx, "llama_index")
    assert opts == {"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_MULTILINGUAL}


def test_folder_search_rerank_options_llama_index_disabled(ctx):
    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=False):
        opts = embeddings_service._folder_search_rerank_options(ctx, "llama_index")
    assert opts == {"use_mmr": False}


def test_folder_search_rerank_options_hybrid_backend_disabled(ctx):
    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=False):
        opts = embeddings_service._folder_search_rerank_options(ctx, "hybrid")
    assert opts == {"use_mmr": False}


def test_folder_search_rerank_options_hybrid_backend_enabled(ctx):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_ENGLISH_SMALL

    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=True):
        with patch(
            "plugin.framework.constants.resolve_folder_rerank_model",
            return_value=FOLDER_RERANK_MODEL_ENGLISH_SMALL,
        ):
            opts = embeddings_service._folder_search_rerank_options(ctx, "hybrid")
    assert opts == {"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_ENGLISH_SMALL}
