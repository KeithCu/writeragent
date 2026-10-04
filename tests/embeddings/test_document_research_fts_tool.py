# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for search_nearby_files tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from plugin.embeddings.document_research_fts_tool import SearchNearbyFiles


def _ctx():
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.services = MagicMock()
    return ctx


def test_search_nearby_files_disabled():
    tool = SearchNearbyFiles()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
        result = tool.execute(_ctx(), query="web search")
    assert result.get("status") == "error"
    assert result.get("code") == "FOLDER_SEARCH_DISABLED"


def test_search_nearby_files_indexing_status():
    tool = SearchNearbyFiles()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value="/tmp/folder"):
                    with patch("plugin.doc.document_research.get_document_path", return_value="/tmp/folder/a.ods"):
                        with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                            with patch(
                                "plugin.embeddings.embeddings_cache.resolve_index_context",
                                return_value=("key", MagicMock(), MagicMock(), "/tmp/folder"),
                            ):
                                with patch("plugin.framework.config.get_config", return_value="sqlite"):
                                    with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=True):
                                        with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup") as wakeup_mock:
                                            result = tool.execute(MagicMock(), query="test")
    assert result.get("status") == "indexing"
    assert result.get("hits") == []
    wakeup_mock.assert_called_once()


def test_search_nearby_files_does_not_block_main_thread_for_rpc():
    tool = SearchNearbyFiles()

    # We want to ensure execute_on_main_thread is NOT called for hybrid_search
    # It SHOULD be called for _resolve_uno_context and _wakeup
    execute_calls = []

    def fake_execute_on_main_thread(fn):
        execute_calls.append(fn.__name__)
        return fn()

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=fake_execute_on_main_thread):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value="/tmp/folder"):
                    with patch("plugin.doc.document_research.get_document_path", return_value="/tmp/folder/a.ods"):
                        with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                            with patch(
                                "plugin.embeddings.embeddings_cache.resolve_index_context",
                                return_value=("key", "db_path", MagicMock(), "/tmp/folder"),
                            ):
                                with patch("plugin.framework.config.get_config", return_value="sqlite"):
                                    with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                                        with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="model"):
                                            with patch("plugin.embeddings.embeddings_service.hybrid_search", return_value={"hits": []}) as rpc_mock:
                                                with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup") as wakeup_mock:
                                                    result = tool.execute(_ctx(), query="web search")

    assert result.get("status") == "ok"
    rpc_mock.assert_called_once()
    wakeup_mock.assert_called_once()

    # The RPC call should NOT be wrapped in execute_on_main_thread
    assert "_resolve_uno_context" in execute_calls
    assert "_wakeup" in execute_calls
    assert len([c for c in execute_calls if c in ("_resolve_uno_context", "_wakeup")]) == 2


def test_search_nearby_files_model_change_does_not_query(tmp_path):
    tool = SearchNearbyFiles()
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
                    with patch("plugin.doc.document_research.get_document_path", return_value="/tmp/folder/a.ods"):
                        with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                            with patch(
                                "plugin.embeddings.embeddings_cache.resolve_index_context",
                                return_value=("key", db, meta, str(tmp_path)),
                            ):
                                with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="new-model"):
                                    with patch("plugin.embeddings.embeddings_service.hybrid_search") as rpc_mock:
                                        with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup") as wakeup_mock:
                                            result = tool.execute(_ctx(), query="web search")

    rpc_mock.assert_not_called()
    wakeup_mock.assert_called_once()
    assert result["status"] == "indexing"
    assert result["hits"] == []
    assert result["stale"] is True


def test_search_nearby_files_backend_error_surfaced():
    tool = SearchNearbyFiles()
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.services = MagicMock()

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
                with patch(
                    "plugin.embeddings.embeddings_cache.resolve_index_context",
                    return_value=("key", "db_path", MagicMock(), "/tmp/folder"),
                ):
                    with patch("plugin.framework.config.get_config", return_value="sqlite"):
                        with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                            with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="model"):
                                # Simulate the backend returning an error dict (e.g. from LanceDB/zvec worker)
                                with patch("plugin.embeddings.embeddings_service.hybrid_search", return_value={"hits": [], "error": "Backend timeout or failure"}):
                                    result = tool.execute(ctx, query="budget figures")

    assert result.get("status") == "error", f"Expected error status, got {result.get('status')}"
    assert "Backend timeout or failure" in result.get("message", "")

def test_search_nearby_files_passes_stop_checker():
    tool = SearchNearbyFiles()
    ctx = _ctx()
    ctx.stop_checker = lambda: False
    ctx.send_cancellation = object()

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value="/tmp/folder"):
                    with patch("plugin.doc.document_research.get_document_path", return_value="/tmp/folder/a.ods"):
                        with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                            with patch(
                                "plugin.embeddings.embeddings_cache.resolve_index_context",
                                return_value=("key", "db_path", MagicMock(), "/tmp/folder"),
                            ):
                                with patch("plugin.framework.config.get_config", return_value="sqlite"):
                                    with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                                        with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="model"):
                                            with patch("plugin.embeddings.embeddings_service.hybrid_search", return_value={"hits": []}) as rpc_mock:
                                                with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup"):
                                                    result = tool.execute(ctx, query="test")

    assert result.get("status") == "ok"
    rpc_mock.assert_called_once()
    assert rpc_mock.call_args.kwargs["stop_checker"] is ctx.stop_checker
    assert rpc_mock.call_args.kwargs["cancellation_scope"] is ctx.send_cancellation


def test_search_nearby_files_file_subset_single_match(tmp_path):
    tool = SearchNearbyFiles()
    ctx = _ctx()
    ctx.stop_checker = MagicMock(return_value=False)

    meta = tmp_path / "corpus_meta.json"
    db = tmp_path / "corpus.db"
    db.write_text("sqlite", encoding="utf-8")
    meta.write_text(json.dumps({"schema_version": "6", "embedding_model": "model"}), encoding="utf-8")
    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
                with patch("plugin.framework.config.get_config", return_value="fts"):
                    with patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=("key", db, meta, str(tmp_path))):
                        with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                            with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="model"):
                                with patch("plugin.doc.document_research_grep.resolve_grep_candidates", return_value=([{"url": "file:///path/to/single_file.odt"}], False, None)):
                                    with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup") as wakeup_mock:
                                        with patch("plugin.embeddings.embeddings_service.hybrid_search", return_value={"hits": [{"doc_url": "file:///path/to/single_file.odt"}]}) as rpc_mock:
                                            result = tool.execute(ctx, query="test", file_subset="single")

    assert result["status"] == "ok"
    assert len(result["hits"]) == 1
    rpc_mock.assert_called_once_with(
        ctx.ctx,
        str(db),
        "test",
        10,
        model="model",
        near_slop=10,
        doc_url_filter="file:///path/to/single_file.odt",
        stop_checker=ctx.stop_checker,
        cancellation_scope=ctx.send_cancellation,
    )
    wakeup_mock.assert_called_once()


def test_search_nearby_files_file_subset_multiple_matches(tmp_path):
    tool = SearchNearbyFiles()
    ctx = _ctx()
    ctx.stop_checker = MagicMock(return_value=False)

    meta = tmp_path / "corpus_meta.json"
    db = tmp_path / "corpus.db"
    db.write_text("sqlite", encoding="utf-8")
    meta.write_text(json.dumps({"schema_version": "6", "embedding_model": "model"}), encoding="utf-8")

    backend_hits = [
        {"doc_url": "file:///path/to/file_a.odt"},
        {"doc_url": "file:///path/to/file_b.odt"},
        {"doc_url": "file:///path/to/other.odt"},
    ]

    with patch("plugin.framework.constants.folder_search_enabled", return_value=True):
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
                with patch("plugin.framework.config.get_config", return_value="fts"):
                    with patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=("key", db, meta, str(tmp_path))):
                        with patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=False):
                            with patch("plugin.embeddings.embedding_client.get_embedding_model", return_value="model"):
                                with patch(
                                    "plugin.doc.document_research_grep.resolve_grep_candidates",
                                    return_value=([{"url": "file:///path/to/file_a.odt"}, {"url": "file:///path/to/file_b.odt"}], False, None),
                                ):
                                    with patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup") as wakeup_mock:
                                        with patch("plugin.embeddings.embeddings_service.hybrid_search", return_value={"hits": backend_hits}) as rpc_mock:
                                            result = tool.execute(ctx, query="test", k=2, file_subset="file_")

    assert result["status"] == "ok"
    assert len(result["hits"]) == 2
    rpc_mock.assert_called_once_with(
        ctx.ctx,
        str(db),
        "test",
        100,
        model="model",
        near_slop=10,
        doc_url_filter=None,
        stop_checker=ctx.stop_checker,
        cancellation_scope=ctx.send_cancellation,
    )
    wakeup_mock.assert_called_once()
