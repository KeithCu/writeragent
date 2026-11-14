# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for search_nearby_files tool."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from plugin.embeddings.document_research_fts_tool import SearchNearbyFiles
from plugin.framework.send_cancellation import SendCancellation

_UNSET = object()


def _ctx():
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.services = MagicMock()
    ctx.send_cancellation = None
    return ctx


def _run_inline(fn, *a, **k):
    return fn()


@contextmanager
def _patched_fts_env(
    *,
    is_current: bool = True,
    main_call_side_effect: Any = _run_inline,
    listing_dir: str = "/tmp/folder",
    index_context: Any = _UNSET,
    search_mode: str = "sqlite",
    index_empty: Any = False,
    embedding_model: Any = "model",
    hybrid_kwargs: Any = _UNSET,
    grep_return: Any = _UNSET,
):
    """Patch FTS search dependencies; pass _UNSET for any optional target to leave it unpatched."""
    if index_context is _UNSET:
        index_context = ("key", "db_path", MagicMock(), listing_dir)
    with ExitStack() as stack:
        stack.enter_context(patch("plugin.framework.constants.folder_search_enabled", return_value=True))
        if main_call_side_effect is not _UNSET:
            stack.enter_context(patch("plugin.framework.queue_executor.main.call", side_effect=main_call_side_effect))
        stack.enter_context(patch("plugin.framework.queue_executor.main.is_current", return_value=is_current))
        stack.enter_context(patch("plugin.doc.document_research.resolve_listing_directory", return_value=listing_dir))
        stack.enter_context(patch("plugin.doc.document_research.get_document_path", return_value="/tmp/folder/a.ods"))
        collect_open_urls_mock = stack.enter_context(
            patch("plugin.doc.document_research._collect_open_file_urls", return_value={})
        )
        stack.enter_context(patch("plugin.embeddings.embeddings_cache.resolve_index_context", return_value=index_context))
        stack.enter_context(patch("plugin.framework.config.get_config", return_value=search_mode))
        if index_empty is not _UNSET:
            stack.enter_context(patch("plugin.embeddings.embeddings_cache.index_is_empty", return_value=index_empty))
        if embedding_model is not _UNSET:
            stack.enter_context(patch("plugin.embeddings.embedding_client.get_embedding_model", return_value=embedding_model))
        if grep_return is not _UNSET:
            stack.enter_context(patch("plugin.doc.document_research_grep.resolve_grep_candidates", return_value=grep_return))
        rpc_mock = (
            stack.enter_context(patch("plugin.embeddings.embeddings_service.hybrid_search", **hybrid_kwargs))
            if hybrid_kwargs is not _UNSET
            else None
        )
        wakeup_mock = stack.enter_context(patch("plugin.embeddings.embeddings_indexer.ensure_index_wakeup"))
        yield SimpleNamespace(rpc=rpc_mock, wakeup=wakeup_mock, collect_open_urls=collect_open_urls_mock)


def test_search_nearby_files_disabled():
    tool = SearchNearbyFiles()
    with patch("plugin.framework.constants.folder_search_enabled", return_value=False):
        result = tool.execute(_ctx(), query="web search")
    assert result.get("status") == "error"
    assert result.get("code") == "FOLDER_SEARCH_DISABLED"


def test_search_nearby_files_indexing_status():
    tool = SearchNearbyFiles()
    with _patched_fts_env(is_current=False, index_empty=True, embedding_model=_UNSET) as env:
        result = tool.execute(MagicMock(), query="test")
    assert result.get("status") == "indexing"
    assert result.get("hits") == []
    env.wakeup.assert_called_once()


def test_search_nearby_files_does_not_block_main_thread_for_rpc():
    tool = SearchNearbyFiles()
    call_depth = [0]
    listing_inside_main_call: list[bool] = []
    wakeup_inside_main_call: list[bool] = []
    rpc_inside_main_call: list[bool] = []

    def fake_call(fn, *args, **kwargs):
        call_depth[0] += 1
        try:
            return fn()
        finally:
            call_depth[0] -= 1

    def fake_hybrid_search(*_args, **_kwargs):
        rpc_inside_main_call.append(call_depth[0] > 0)
        return {"hits": []}

    with _patched_fts_env(
        is_current=False,
        main_call_side_effect=fake_call,
        hybrid_kwargs={"side_effect": fake_hybrid_search},
    ) as env:
        env.collect_open_urls.side_effect = lambda *_a, **_k: (
            listing_inside_main_call.append(call_depth[0] > 0) or {}
        )
        env.wakeup.side_effect = lambda *_a, **_k: wakeup_inside_main_call.append(call_depth[0] > 0)
        result = tool.execute(_ctx(), query="web search")

    assert result.get("status") == "ok"
    env.rpc.assert_called_once()
    env.wakeup.assert_called_once()
    assert listing_inside_main_call and all(listing_inside_main_call)
    assert wakeup_inside_main_call and all(wakeup_inside_main_call)
    assert rpc_inside_main_call == [False]


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

    with _patched_fts_env(
        main_call_side_effect=_UNSET,
        listing_dir=str(tmp_path),
        index_context=("key", db, meta, str(tmp_path)),
        search_mode="hybrid",
        index_empty=_UNSET,
        embedding_model="new-model",
        hybrid_kwargs={},
    ) as env:
        result = tool.execute(_ctx(), query="web search")

    env.rpc.assert_not_called()
    env.wakeup.assert_called_once()
    assert result["status"] == "indexing"
    assert result["hits"] == []
    assert result["stale"] is True


def test_search_nearby_files_backend_error_surfaced():
    tool = SearchNearbyFiles()
    # Simulate the backend returning an error dict (e.g. from LanceDB/zvec worker).
    with _patched_fts_env(
        hybrid_kwargs={"return_value": {"hits": [], "error": "Backend timeout or failure"}},
    ):
        result = tool.execute(_ctx(), query="budget figures")

    assert result.get("status") == "error", f"Expected error status, got {result.get('status')}"
    assert "Backend timeout or failure" in result.get("message", "")


def test_search_nearby_files_passes_cancel_scope():
    tool = SearchNearbyFiles()
    ctx = _ctx()
    scope = SendCancellation()
    ctx.send_cancellation = scope

    with _patched_fts_env(hybrid_kwargs={"return_value": {"hits": []}}) as env:
        result = tool.execute(ctx, query="test")

    assert result.get("status") == "ok"
    env.rpc.assert_called_once()
    assert env.rpc.call_args.kwargs["cancel"] is scope
    assert env.rpc.call_args.kwargs.get("doc_url_filter") is None


def _run_search_with_subset(*, grep_return, hybrid_side_effect=None, hybrid_return=None, file_subset="notes.odt"):
    tool = SearchNearbyFiles()
    ctx = _ctx()
    scope = SendCancellation()
    ctx.send_cancellation = scope
    hybrid_kwargs = {"return_value": hybrid_return or {"hits": []}}
    if hybrid_side_effect is not None:
        hybrid_kwargs = {"side_effect": hybrid_side_effect}
    with _patched_fts_env(grep_return=grep_return, hybrid_kwargs=hybrid_kwargs) as env:
        result = tool.execute(ctx, query="test", file_subset=file_subset)
    return result, env.rpc, scope


def test_search_nearby_files_single_subset_uses_doc_url_filter():
    url = "file:///tmp/folder/notes.odt"
    grep_return = ([{"url": url, "path": "/tmp/folder/notes.odt"}], False, None)
    hit = {"doc_url": url, "score": 0.9, "snippet": "hit"}
    result, rpc_mock, scope = _run_search_with_subset(
        grep_return=grep_return,
        hybrid_return={"hits": [hit]},
    )
    assert result["status"] == "ok"
    assert result["hits"] == [hit]
    rpc_mock.assert_called_once()
    assert rpc_mock.call_args.kwargs["doc_url_filter"] == url
    assert rpc_mock.call_args.kwargs["cancel"] is scope


def test_search_nearby_files_two_subset_urls_merge_by_score():
    url_a = "file:///tmp/folder/a.odt"
    url_b = "file:///tmp/folder/b.odt"
    grep_return = (
        [{"url": url_a, "path": "/tmp/a.odt"}, {"url": url_b, "path": "/tmp/b.odt"}],
        False,
        None,
    )

    def _hybrid(_ctx, _path, _query, k, **kwargs):
        filt = kwargs.get("doc_url_filter")
        if filt == url_a:
            return {"hits": [{"doc_url": url_a, "score": 0.5, "snippet": "a"}]}
        if filt == url_b:
            return {"hits": [{"doc_url": url_b, "score": 0.9, "snippet": "b"}]}
        return {"hits": []}

    result, rpc_mock, scope = _run_search_with_subset(grep_return=grep_return, hybrid_side_effect=_hybrid, file_subset="pair")
    assert result["status"] == "ok"
    assert [h["doc_url"] for h in result["hits"]] == [url_b, url_a]
    assert rpc_mock.call_count == 2
    filters = [call.kwargs["doc_url_filter"] for call in rpc_mock.call_args_list]
    assert set(filters) == {url_a, url_b}
    assert all(call.kwargs["cancel"] is scope for call in rpc_mock.call_args_list)


def test_search_nearby_files_empty_subset_skips_hybrid_search():
    result, rpc_mock, _scope = _run_search_with_subset(grep_return=([], False, None))
    assert result["status"] == "ok"
    assert result["hits"] == []
    rpc_mock.assert_not_called()


def test_search_nearby_files_cancels_before_second_subset_url():
    url_a = "file:///tmp/folder/a.odt"
    url_b = "file:///tmp/folder/b.odt"
    grep_return = (
        [{"url": url_a, "path": "/tmp/a.odt"}, {"url": url_b, "path": "/tmp/b.odt"}],
        False,
        None,
    )

    def _hybrid(*_args, **kwargs):
        kwargs["cancel"].cancel()
        return {"hits": [{"doc_url": url_a, "score": 0.5, "snippet": "a"}]}

    result, rpc_mock, _scope = _run_search_with_subset(
        grep_return=grep_return,
        hybrid_side_effect=_hybrid,
        file_subset="pair",
    )
    assert result["status"] == "error"
    assert result["message"] == "Cancelled"
    assert rpc_mock.call_count == 1
