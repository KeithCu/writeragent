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
    "fn_name, extra_args, extra_kwargs",
    [
        pytest.param("hybrid_search", ("dspy", 20), {"near_slop": 10}, id="hybrid_search"),
        pytest.param("knn_search", ("query", 5), {}, id="knn_search"),
    ],
)
def test_search_happy_path(ctx, tmp_path, fn_name, extra_args, extra_kwargs):
    corpus_db = str(tmp_path / "corpus.db")
    worker_payload = {"hits": [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]}
    with (
        patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value=worker_payload) as mock_run,
        patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120),
        patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value="sqlite"),
        patch("plugin.framework.constants.folder_rerank_enabled", return_value=False),
    ):
        fn = getattr(embeddings_service, fn_name)
        result = fn(ctx, corpus_db, *extra_args, model=DEFAULT_EMBEDDING_MODEL, **extra_kwargs)
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert mock_run.call_args.kwargs["worker_pool"] == WORKER_POOL_EMBEDDINGS
    assert mock_run.call_args.kwargs["helper"] == fn_name
    assert mock_run.call_args.kwargs["params"]["db_path"] == corpus_db


@pytest.mark.parametrize(
    "action, mode_value",
    [
        pytest.param("index_paragraphs", "zvec", id="index_paragraphs_zvec"),
        pytest.param("delete_paragraphs", "lancedb", id="delete_paragraphs_lancedb"),
        pytest.param("maintain_folder_index", "llama_index", id="maintain_folder_index_llama_index"),
        pytest.param("maintain_folder_index", "lancedb", id="maintain_folder_index_lancedb"),
        pytest.param("hybrid_search", "llama_index", id="hybrid_search_llama_index"),
    ],
)
def test_actions_pass_configured_search_mode(ctx, tmp_path, action, mode_value):
    corpus_db = str(tmp_path / "corpus.db")
    meta_json = str(tmp_path / "meta.json")
    folder = str(tmp_path / "folder")
    with (
        patch("plugin.embeddings.embeddings_service.run_trusted_worker_action", return_value={"hits": [], "mode": "cold"}) as mock_run,
        patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120),
        patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value=mode_value),
    ):
        if action == "index_paragraphs":
            embeddings_service.index_paragraphs(ctx, corpus_db, meta_json, [{"text": "hi"}], model=DEFAULT_EMBEDDING_MODEL)
        elif action == "delete_paragraphs":
            embeddings_service.delete_paragraphs(ctx, corpus_db, meta_json, [{"doc_url": "file:///a.odt", "para_index": 0}], model=DEFAULT_EMBEDDING_MODEL)
        elif action == "maintain_folder_index":
            embeddings_service.maintain_folder_index(ctx, folder, model=DEFAULT_EMBEDDING_MODEL)
        else:
            embeddings_service.hybrid_search(ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL)
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
    "return_value",
    [
        pytest.param("llama_index", id="test_hybrid_search_passes_rerank_model_when_llama_index_enabled"),
        pytest.param("hybrid", id="test_hybrid_search_passes_rerank_model_when_hybrid_rerank_enabled"),
    ],
)
def test_hybrid_search_passes_rerank_model_when_llama_index_enabled(ctx, tmp_path, return_value):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_ENGLISH_SMALL

    corpus_db = str(tmp_path / "corpus.db")
    with patch(
        "plugin.embeddings.embeddings_service.run_trusted_worker_action",
        return_value={"hits": []},
    ) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value=return_value):
                with patch(
                    "plugin.embeddings.embeddings_service._folder_search_rerank_options",
                    return_value={"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_ENGLISH_SMALL},
                ):
                    embeddings_service.hybrid_search(ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["params"]
    assert data["rerank_model"] == FOLDER_RERANK_MODEL_ENGLISH_SMALL
    assert data["use_mmr"] is True


def test_hybrid_search_disables_rerank_when_llama_index_rerank_off(ctx, tmp_path):
    corpus_db = str(tmp_path / "corpus.db")
    with patch(
        "plugin.embeddings.embeddings_service.run_trusted_worker_action",
        return_value={"hits": []},
    ) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value="llama_index"):
                with patch(
                    "plugin.embeddings.embeddings_service._folder_search_rerank_options",
                    return_value={"use_mmr": False},
                ):
                    embeddings_service.hybrid_search(ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["params"]
    assert "rerank_model" not in data
    assert data["use_mmr"] is False


def test_hybrid_search_omits_rerank_when_disabled_for_hybrid_backend(ctx, tmp_path):
    corpus_db = str(tmp_path / "corpus.db")
    with patch(
        "plugin.embeddings.embeddings_service.run_trusted_worker_action",
        return_value={"hits": []},
    ) as mock_run:
        with patch("plugin.embeddings.embeddings_service.embeddings_worker_timeout_sec", return_value=120):
            with patch("plugin.framework.constants.folder_rerank_enabled", return_value=False):
                with patch("plugin.embeddings.embeddings_service.configured_folder_search_mode", return_value="hybrid"):
                    embeddings_service.hybrid_search(ctx, corpus_db, "q", 5, model=DEFAULT_EMBEDDING_MODEL)
    data = mock_run.call_args.kwargs["params"]
    assert "rerank_model" not in data
    assert data["use_mmr"] is False

def test_folder_search_rerank_options_llama_index_enabled(ctx):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_MULTILINGUAL

    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=True):
        with patch(
            "plugin.framework.constants.resolve_folder_rerank_model",
            return_value=FOLDER_RERANK_MODEL_MULTILINGUAL,
        ):
            opts = embeddings_service._folder_search_rerank_options("llama_index")
    assert opts == {"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_MULTILINGUAL}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("llama_index", id="test_folder_search_rerank_options_llama_index_disabled"),
        pytest.param("hybrid", id="test_folder_search_rerank_options_hybrid_backend_disabled"),
    ],
)
def test_folder_search_rerank_options_llama_index_disabled(ctx, value):
    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=False):
        opts = embeddings_service._folder_search_rerank_options(value)
    assert opts == {"use_mmr": False}

@pytest.mark.parametrize(
    "value",
    [
        pytest.param("hybrid", id="test_folder_search_rerank_options_hybrid_backend_enabled"),
        pytest.param("lancedb", id="test_folder_search_rerank_options_lancedb_enabled"),
    ],
)
def test_folder_search_rerank_options_hybrid_backend_enabled(ctx, value):
    from plugin.framework.constants import FOLDER_RERANK_MODEL_ENGLISH_SMALL

    with patch("plugin.framework.constants.folder_rerank_enabled", return_value=True):
        with patch(
            "plugin.framework.constants.resolve_folder_rerank_model",
            return_value=FOLDER_RERANK_MODEL_ENGLISH_SMALL,
        ):
            opts = embeddings_service._folder_search_rerank_options(value)
    assert opts == {"use_mmr": True, "rerank_model": FOLDER_RERANK_MODEL_ENGLISH_SMALL}


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

