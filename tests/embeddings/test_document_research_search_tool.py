# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.document_research_search_tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from plugin.embeddings.document_research_search_tool import SearchEmbeddings


def test_search_embeddings_disabled_returns_error():
    tool = SearchEmbeddings()
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
        result = tool.execute(ctx, query="budget figures")
    assert result["status"] == "error"
    assert result.get("code") == "FOLDER_SEARCH_DISABLED"


def test_search_embeddings_does_not_block_main_thread_for_rpc():
    tool = SearchEmbeddings()
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.services = MagicMock()

    execute_calls = []

    def fake_execute_on_main_thread(fn):
        execute_calls.append(fn.__name__)
        return fn()

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=fake_execute_on_main_thread):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value="/tmp/folder"):
                    with patch(
                        "plugin.embeddings.embeddings_cache.resolve_index_context",
                        return_value=("key", "db_path", MagicMock(), "/tmp/folder"),
                    ):
                        with patch("plugin.framework.config.get_config", return_value="sqlite"):
                            with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                                with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="model"):
                                    with patch("plugin.embeddings.embeddings_service.knn_search", return_value={"hits": []}) as rpc_mock:
                                        result = tool.execute(ctx, query="budget figures")

    assert result.get("status") == "ok"
    rpc_mock.assert_called_once()

    # The RPC call should NOT be wrapped in execute_on_main_thread
    assert "_resolve_uno_context" in execute_calls
    assert "_wakeup" in execute_calls
    # Exclude other calls like _resolve inside knn_search internals if any
    assert len([c for c in execute_calls if c in ("_resolve_uno_context", "_wakeup")]) == 2


def test_search_embeddings_model_change_does_not_query(tmp_path):
    tool = SearchEmbeddings()
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.services = MagicMock()
    meta = tmp_path / "corpus_meta.json"
    db = tmp_path / "corpus.db"
    db.write_text("sqlite", encoding="utf-8")
    meta.write_text(
        json.dumps(
            {
                "schema_version": "6",
                "embedding_model": "old-model",
                "chunk_count": "3",
                "dim": "384",
            }
        ),
        encoding="utf-8",
    )

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
            with patch("plugin.framework.config.get_config", return_value="hybrid"):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=str(tmp_path)):
                    with patch(
                        "plugin.embeddings.embeddings_cache.resolve_index_context",
                        return_value=("key", db, meta, str(tmp_path)),
                    ):
                        with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="new-model"):
                            with patch("plugin.embeddings.embeddings_service.knn_search") as rpc_mock:
                                with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup") as wakeup_mock:
                                    result = tool.execute(ctx, query="budget figures")

    rpc_mock.assert_not_called()
    wakeup_mock.assert_called_once()
    assert result["status"] == "indexing"
    assert result["hits"] == []
    assert result["stale"] is True
