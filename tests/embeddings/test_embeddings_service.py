# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.embeddings_service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings import embeddings_service
from plugin.framework.constants import DEFAULT_EMBEDDING_MODEL, WORKER_POOL_EMBEDDINGS
from plugin.framework.errors import ToolExecutionError


@pytest.fixture
def ctx():
    return MagicMock()


@pytest.mark.parametrize(
    "fn, helper_name, extra_args, extra_kwargs",
    [
        pytest.param(embeddings_service.hybrid_search, "hybrid_search", ("dspy", 20), {"near_slop": 10}, id="hybrid_search"),
        pytest.param(embeddings_service.knn_search, "knn_search", ("query", 5), {}, id="knn_search"),
    ],
)
def test_search_happy_path(ctx, tmp_path, fn, helper_name, extra_args, extra_kwargs):
    corpus_db = str(tmp_path / "corpus.db")
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]}
    with (
        patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value=worker_payload) as mock_run,
        patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120),
        patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value="sqlite"),
        patch("plugin.framework.constants.folder_rerank_enabled", return_value=False),
    ):
        result = fn(ctx, corpus_db, *extra_args, model=DEFAULT_EMBEDDING_MODEL, **extra_kwargs)
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert mock_run.call_args.kwargs["helper"] == helper_name
    assert mock_run.call_args.kwargs["params"]["db_path"] == corpus_db


@pytest.mark.parametrize(
    "invoke, mode_value",
    [
        pytest.param(
            lambda ctx, corpus_db, meta_json, _folder: embeddings_service.index_paragraphs(
                ctx, corpus_db, meta_json, [{"text": "hi"}], model=DEFAULT_EMBEDDING_MODEL
            ),
            "zvec",
            id="index_paragraphs_zvec",
        ),
        pytest.param(
            lambda ctx, corpus_db, meta_json, _folder: embeddings_service.delete_paragraphs(
                ctx, corpus_db, meta_json, [{"doc_url": "file:///a.odt", "para_index": 0}], model=DEFAULT_EMBEDDING_MODEL
            ),
            "lancedb",
            id="delete_paragraphs_lancedb",
        ),
        pytest.param(
            lambda ctx, _corpus_db, _meta_json, folder: embeddings_service.maintain_folder_index(
                ctx, folder, model=DEFAULT_EMBEDDING_MODEL
            ),
            "llama_index",
            id="maintain_folder_index_llama_index",
        ),
        pytest.param(
            lambda ctx, _corpus_db, _meta_json, folder: embeddings_service.maintain_folder_index(
                ctx, folder, model=DEFAULT_EMBEDDING_MODEL
            ),
            "lancedb",
            id="maintain_folder_index_lancedb",
        ),
        pytest.param(
            lambda ctx, corpus_db, _meta_json, _folder: embeddings_service.hybrid_search(
                ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL
            ),
            "llama_index",
            id="hybrid_search_llama_index",
        ),
    ],
)
def test_actions_pass_configured_search_mode(ctx, tmp_path, invoke, mode_value):
    corpus_db = str(tmp_path / "corpus.db")
    meta_json = str(tmp_path / "meta.json")
    folder = str(tmp_path / "folder")
    with (
        patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value={"hits": [], "mode": "cold"}) as mock_run,
        patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120),
        patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value=mode_value),
    ):
        invoke(ctx, corpus_db, meta_json, folder)
    assert mock_run.call_args.kwargs["params"]["search_mode"] == mode_value


def test_index_paragraphs_worker_error(ctx, tmp_path):
    corpus_db = str(tmp_path / "corpus.db")
    meta_json = str(tmp_path / "meta.json")
    with patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", side_effect=ToolExecutionError("boom", code="EMBEDDING_INDEX_ERROR")):
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value="sqlite"):
                with pytest.raises(ToolExecutionError, match="boom"):
                    embeddings_service.index_paragraphs(
                        ctx,
                        corpus_db,
                        meta_json,
                        [],
                        model=DEFAULT_EMBEDDING_MODEL,
                    )


def test_collection_stats_rpc(ctx, tmp_path):
    from plugin.embeddings.embedding_client import embedding_session_id

    corpus_db = str(tmp_path / "corpus.db")
    meta_json = str(tmp_path / "meta.json")
    with patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value={"chunk_count": 5}) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            result = embeddings_service.collection_stats(ctx, corpus_db, meta_json)
    assert result["chunk_count"] == 5
    assert mock_run.call_args.kwargs["session_id"] == embedding_session_id("corpus")


def test_maintain_folder_index_uses_heartbeat_rpc(ctx, tmp_path):
    folder = str(tmp_path / "folder")
    with patch(
        "plugin.embeddings.embeddings_service.run_trusted_worker_action",
        return_value={"mode": "cold", "indexed_paragraphs": 3},
    ) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            result = embeddings_service.maintain_folder_index(
                ctx,
                folder,
                model=DEFAULT_EMBEDDING_MODEL,
                mode="auto",
                search_mode="hybrid",
            )
    assert result["mode"] == "cold"
    assert mock_run.call_args.kwargs["allow_heartbeat"] is True
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert mock_run.call_args.kwargs["params"]["listing_root"] == folder
    assert mock_run.call_args.kwargs["params"]["search_mode"] == "hybrid"


@pytest.mark.parametrize(
    ("search_mode", "rerank_enabled"),
    [
        pytest.param("llama_index", True, id="llama-index-on"),
        pytest.param("hybrid", True, id="hybrid-on"),
        pytest.param("llama_index", False, id="llama-index-off"),
        pytest.param("hybrid", False, id="hybrid-off"),
    ],
)
def test_hybrid_search_rerank_params(ctx, tmp_path, search_mode, rerank_enabled):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_ENGLISH_SMALL

    corpus_db = str(tmp_path / "corpus.db")
    with (
        patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value={"hits": []}) as mock_run,
        patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120),
        patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value=search_mode),
        patch("plugin.framework.constants.folder_rerank_enabled", return_value=rerank_enabled),
        patch("plugin.framework.constants.resolve_folder_rerank_model", return_value=FOLDER_RERANK_MODEL_ENGLISH_SMALL),
    ):
        embeddings_service.hybrid_search(ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["params"]
    assert data["use_mmr"] is rerank_enabled
    if rerank_enabled:
        assert data["rerank_model"] == FOLDER_RERANK_MODEL_ENGLISH_SMALL
    else:
        assert "rerank_model" not in data


@pytest.mark.parametrize("search_mode", ["llama_index", "hybrid", "lancedb"])
def test_folder_search_rerank_options_enabled(ctx, search_mode):
    sentinel = "rerank-sentinel-model"
    with (
        patch("plugin.framework.constants.folder_rerank_enabled", return_value=True),
        patch("plugin.framework.constants.resolve_folder_rerank_model", return_value=sentinel),
    ):
        opts = embeddings_service._folder_search_rerank_options(search_mode)
    assert opts == {"use_mmr": True, "rerank_model": sentinel}


@pytest.mark.parametrize("search_mode", ["llama_index", "hybrid"])
def test_folder_search_rerank_options_disabled(ctx, search_mode):
    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=False):
        opts = embeddings_service._folder_search_rerank_options(search_mode)
    assert opts == {"use_mmr": False}


def test_hybrid_search_forwards_ctx_and_none_cancel_for_worker_resolution(ctx, tmp_path):
    """When cancel is omitted, None is forwarded so run_trusted_worker_action reads ctx.send_cancellation."""
    from plugin.framework.send_cancellation import SendCancellation

    ctx.send_cancellation = SendCancellation()
    corpus_db = str(tmp_path / "corpus.db")
    with patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value={"hits": []}) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.config.get_config", return_value="sqlite"):
                embeddings_service.hybrid_search(ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    assert mock_run.call_args.args[0] is ctx
    assert mock_run.call_args.kwargs["cancel"] is None


def test_hybrid_search_explicit_cancel_overrides_ctx(ctx, tmp_path):
    from plugin.framework.send_cancellation import SendCancellation

    ctx.send_cancellation = SendCancellation()
    explicit = SendCancellation()
    corpus_db = str(tmp_path / "corpus.db")
    with patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value={"hits": []}) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.config.get_config", return_value="sqlite"):
                embeddings_service.hybrid_search(
                    ctx,
                    corpus_db,
                    "q",
                    5,
                    model=DEFAULT_EMBEDDING_MODEL,
                    cancel=explicit,
                )
    assert mock_run.call_args.kwargs["cancel"] is explicit

