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
