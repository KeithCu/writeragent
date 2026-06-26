# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_index HF offline-first loading."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.venv import embeddings_index as idx


@pytest.fixture(autouse=True)
def _clear_model_cache() -> None:
    idx._MODEL_CACHE.clear()
    yield
    idx._MODEL_CACHE.clear()


def test_load_sentence_transformers_model_uses_offline_first() -> None:
    offline_model = MagicMock(name="offline_model")
    loader = MagicMock(return_value=offline_model)
    model = idx._load_sentence_transformers_model(loader, "paraphrase-multilingual-MiniLM-L12-v2")
    assert model is offline_model
    loader.assert_called_once_with("paraphrase-multilingual-MiniLM-L12-v2", local_files_only=True)


def test_load_sentence_transformers_model_falls_back_to_online_on_cache_miss() -> None:
    offline = OSError("not in cache")
    online_model = MagicMock(name="online_model")
    loader = MagicMock(side_effect=[offline, online_model])
    model = idx._load_sentence_transformers_model(loader, "all-MiniLM-L6-v2")
    assert model is online_model
    loader.assert_any_call("all-MiniLM-L6-v2", local_files_only=True)
    loader.assert_any_call("all-MiniLM-L6-v2")


def test_load_sentence_transformers_model_reraises_non_cache_errors() -> None:
    loader = MagicMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        idx._load_sentence_transformers_model(loader, "bad-model")


def test_get_embedder_uses_model_cache() -> None:
    sentinel = MagicMock(name="cached_embedder")
    idx._MODEL_CACHE["cached-model"] = sentinel
    assert idx._get_embedder("cached-model") is sentinel


@patch.object(idx, "_load_sentence_transformers_model")
def test_get_embedder_loads_via_offline_helper(mock_load: MagicMock) -> None:
    st_mod = MagicMock()
    embedder = MagicMock(name="embedder")
    mock_load.return_value = embedder

    with patch.object(idx.importlib, "import_module", return_value=st_mod):
        assert idx._get_embedder("my-model") is embedder

    mock_load.assert_called_once_with(st_mod.SentenceTransformer, "my-model")
    assert idx._MODEL_CACHE["my-model"] is embedder



@patch("plugin.embeddings.venv.embeddings_cross_encoder_rerank.cross_encoder_rerank_candidates")
def test_llama_index_postprocessor_uses_shared_cross_encoder(mock_rerank: MagicMock) -> None:
    from plugin.embeddings.venv.embeddings_llama_index import (
        HAS_LLAMA_INDEX,
        NodeWithScore,
        QueryBundle,
        TextNode,
        _apply_llama_index_postprocessors,
    )

    if not HAS_LLAMA_INDEX:
        pytest.skip("llama-index-core not installed")

    node = TextNode(text="snippet one", id_="1", metadata={"doc_url": "file:///a.odt", "para_index": 0})
    nodes = [NodeWithScore(node=node, score=0.5)]
    mock_rerank.return_value = [{"snippet": "snippet one", "score": 0.9, "chunk_id": "1", "doc_url": "file:///a.odt", "para_index": 0}]

    out = _apply_llama_index_postprocessors(
        nodes,
        QueryBundle(query_str="query"),
        final_k=3,
        use_rerank=True,
        rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
    )

    mock_rerank.assert_called_once()
    assert len(out) == 1
    assert out[0].score == pytest.approx(0.9)
