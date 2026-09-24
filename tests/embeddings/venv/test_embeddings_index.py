# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_index."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from plugin.embeddings.venv import embeddings_index
from plugin.embeddings.venv import embeddings_index as idx


@pytest.fixture(autouse=True)
def clear_model_cache():
    embeddings_index._MODEL_CACHE.clear()
    yield
    embeddings_index._MODEL_CACHE.clear()


def test_embed_texts_empty_input():
    result = embeddings_index.embed_texts("all-MiniLM-L6-v2", [])
    assert result == {"model": "all-MiniLM-L6-v2", "dim": 0, "vectors": [], "indices": []}


def test_embed_texts_skips_blank_strings():
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([1.0, 0.0], dtype=np.float32), np.array([0.0, 1.0], dtype=np.float32)]

    with patch.object(embeddings_index, "_get_embedder", return_value=mock_embedder):
        result = embeddings_index.embed_texts("all-MiniLM-L6-v2", ["  hello  ", "", "world", "   "])

    assert result["indices"] == [0, 2]
    assert result["dim"] == 2
    assert len(result["vectors"]) == 2
    mock_embedder.encode.assert_called_once_with(["hello", "world"], batch_size=2, convert_to_tensor=False, show_progress_bar=False)


def test_embed_texts_normalizes_vectors():
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([3.0, 4.0], dtype=np.float32)]

    with patch.object(embeddings_index, "_get_embedder", return_value=mock_embedder):
        result = embeddings_index.embed_texts("all-MiniLM-L6-v2", ["text"], normalize=True)

    vec = np.array(result["vectors"][0], dtype=np.float32)
    assert pytest.approx(float(np.linalg.norm(vec)), rel=1e-5) == 1.0


def test_embed_texts_reuses_cached_model():
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [np.array([1.0, 0.0], dtype=np.float32)]
    mock_ctor = MagicMock(return_value=mock_embedder)
    fake_mod = MagicMock()
    fake_mod.SentenceTransformer = mock_ctor

    with patch.dict(sys.modules, {"sentence_transformers": fake_mod}):
        embeddings_index.embed_texts("all-MiniLM-L6-v2", ["a"])
        embeddings_index.embed_texts("all-MiniLM-L6-v2", ["b"])

    mock_ctor.assert_called_once_with("all-MiniLM-L6-v2")
    assert mock_embedder.encode.call_count == 2


def test_embed_texts_requires_model_name():
    with pytest.raises(ValueError, match="model name"):
        embeddings_index.embed_texts("", ["text"])


def test_embeddings_venv_pip_install_includes_sqlite_vec():
    assert "envwrap" in embeddings_index.EMBEDDINGS_VENV_PIP_INSTALL
    assert "sentence-transformers" in embeddings_index.EMBEDDINGS_VENV_PIP_INSTALL
    assert "sqlite-vec" in embeddings_index.EMBEDDINGS_VENV_PIP_INSTALL


def test_get_embedder_import_error_includes_install_line():
    with patch("importlib.import_module", side_effect=ImportError("No module named 'envwrap'")):
        with pytest.raises(ImportError, match=embeddings_index.EMBEDDINGS_VENV_PIP_INSTALL):
            embeddings_index._get_embedder("all-MiniLM-L6-v2")


def test_index_paragraphs_delegates_to_ingest_graph():
    with patch("plugin.embeddings.venv.embeddings_ingest_graph.ingest_paragraphs", return_value={"indexed": 2, "dim": 384, "storage_backend": "sqlite_vec"}) as mock_ingest:
        result = embeddings_index.index_paragraphs("/tmp/corpus.db", "/meta.json", "all-MiniLM-L6-v2", [{"text": "hi"}])
    assert result["indexed"] == 2
    mock_ingest.assert_called_once()


def test_knn_search_delegates_to_search_graph():
    hits = [{"doc_url": "file:///a.odt", "para_index": 0, "score": 0.9}]
    with patch("plugin.embeddings.venv.embeddings_search_graph.search_embeddings_graph", return_value={"hits": hits}) as mock_search:
        result = embeddings_index.knn_search("/tmp/corpus.db", "query", 5, model_name="all-MiniLM-L6-v2")
    assert result["hits"] == hits
    mock_search.assert_called_once()


def test_delete_paragraphs_removes_from_corpus(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    meta_path.write_text('{"chunk_count": "1", "dim": "384"}', encoding="utf-8")
    db_path = tmp_path / "corpus.db"

    with patch("plugin.embeddings.venv.embeddings_sqlite.connect_corpus_db") as mock_connect:
        conn = MagicMock()
        mock_connect.return_value = conn
        with patch("plugin.embeddings.venv.embeddings_sqlite.ensure_schema"):
            with patch("plugin.embeddings.venv.embeddings_sqlite.delete_paragraph_keys", return_value=1):
                with patch("plugin.embeddings.venv.embeddings_sqlite.corpus_chunk_count", return_value=0):
                    result = embeddings_index.delete_paragraphs(
                        str(db_path),
                        str(meta_path),
                        [{"doc_url": "file:///a.odt", "para_index": 0}],
                    )
    assert result["deleted"] == 1


def test_is_garbage_hit():
    from plugin.embeddings.venv.embeddings_index import _is_garbage_hit
    assert _is_garbage_hit({"snippet": ""}) is True
    assert _is_garbage_hit({"snippet": "   \n\t "}) is True
    assert _is_garbage_hit({"snippet": ","}) is True
    assert _is_garbage_hit({"snippet": "..."}) is True
    assert _is_garbage_hit({"snippet": "---"}) is True
    assert _is_garbage_hit({"snippet": "123"}) is False
    assert _is_garbage_hit({"snippet": "hello"}) is False
    assert _is_garbage_hit({"snippet": "Thai ก"}) is False


def test_knn_search_filters_garbage():
    hits = [
        {"doc_url": "file:///a.odt", "snippet": "valid result", "score": 0.9},
        {"doc_url": "file:///b.odt", "snippet": ",", "score": 0.8},
        {"doc_url": "file:///c.odt", "snippet": "", "score": 0.7},
        {"doc_url": "file:///d.odt", "snippet": "another valid", "score": 0.6},
    ]
    with patch("plugin.embeddings.venv.embeddings_search_graph.search_embeddings_graph", return_value={"hits": hits}):
        result = embeddings_index.knn_search("/tmp/corpus.db", "query", 5, model_name="all-MiniLM-L6-v2")
    assert len(result["hits"]) == 2
    assert result["hits"][0]["doc_url"] == "file:///a.odt"
    assert result["hits"][1]["doc_url"] == "file:///d.odt"


def test_hybrid_search_filters_garbage():
    hits = [
        {"doc_url": "file:///a.odt", "snippet": "valid", "score": 0.9},
        {"doc_url": "file:///b.odt", "snippet": "...", "score": 0.8},
    ]
    with patch("plugin.embeddings.venv.embeddings_hybrid_search.hybrid_corpus_search", return_value={"hits": hits}):
        result = embeddings_index.hybrid_search("/tmp/corpus.db", "query", 5, model_name="all-MiniLM-L6-v2")
    assert len(result["hits"]) == 1
    assert result["hits"][0]["doc_url"] == "file:///a.odt"

# --- HF offline-first loading (from tests/embeddings/test_embeddings_index.py) ---
# Second autouse cache-clear kept: parent used _clear_model_cache, venv used
# clear_model_cache. Same module cache; both retained so neither side loses setup.

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
