# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_llama_index."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch
import pytest

from plugin.embeddings.venv import embeddings_llama_index
from plugin.embeddings.venv.embeddings_llama_index import HAS_LLAMA_INDEX

pytestmark = pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment"
)


def test_writer_agent_embedding_delegates_to_embed_texts():
    mock_vectors = [[0.1, 0.2, 0.3]]
    with patch("plugin.embeddings.venv.embeddings_index.embed_texts", return_value={"vectors": mock_vectors}) as mock_embed:
        embed_model = embeddings_llama_index.WriterAgentEmbedding(model_name="mock-model")
        
        result_query = embed_model._get_query_embedding("hello")
        assert result_query == [0.1, 0.2, 0.3]
        mock_embed.assert_any_call("mock-model", ["hello"])
        
        result_text = embed_model._get_text_embedding("world")
        assert result_text == [0.1, 0.2, 0.3]
        mock_embed.assert_any_call("mock-model", ["world"])


def test_writer_agent_fts_retriever_converts_inverted_score():
    mock_hits = [
        {"chunk_id": 1, "doc_url": "file:///doc.odt", "para_index": 0, "snippet": "hello world", "score": -5.2}
    ]
    with patch("plugin.embeddings.venv.embeddings_sqlite.fts_corpus_search", return_value=mock_hits):
        retriever = embeddings_llama_index.WriterAgentFTSRetriever(db_path="/tmp/fake.db", near_slop=10)
        from llama_index.core import QueryBundle
        
        nodes = retriever._retrieve(QueryBundle("hello"))
        assert len(nodes) == 1
        assert nodes[0].node.text == "hello world"
        assert nodes[0].node.node_id == "1"
        assert nodes[0].node.metadata["doc_url"] == "file:///doc.odt"
        assert nodes[0].node.metadata["para_index"] == 0
        # BM25 score -5.2 should be inverted to 5.2
        assert nodes[0].score == 5.2


def test_writer_agent_vector_store_delete_delegates_to_sqlite():
    mock_rows = [{"chunk_id": 42}]
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = mock_rows

    with patch("plugin.embeddings.venv.embeddings_sqlite.connect_corpus_db", return_value=mock_conn), \
         patch("plugin.embeddings.venv.embeddings_sqlite._delete_chunk_ids") as mock_delete:
        
        store = embeddings_llama_index.WriterAgentVectorStore(
            db_path="/tmp/fake.db",
            embedding_model="mock-model",
            build_fts=True,
            build_vectors=True
        )
        store.delete(ref_doc_id="file:///doc.odt")
        
        mock_conn.execute.assert_called_with("SELECT chunk_id FROM chunks WHERE doc_url = ?", ("file:///doc.odt",))
        mock_delete.assert_called_with(mock_conn, [42], with_fts=True, with_vec=True)
        mock_conn.commit.assert_called_once()
