# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_llama_index."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.venv import embeddings_llama_index
from plugin.embeddings.venv.embeddings_llama_index import HAS_LLAMA_INDEX


def test_llama_index_retrieval_pool_over_fetches_for_rerank():
    final_k, fetch_k = embeddings_llama_index._llama_index_retrieval_pool(5)
    assert final_k == 5
    assert fetch_k == 20
    final_k, fetch_k = embeddings_llama_index._llama_index_retrieval_pool(10)
    assert final_k == 10
    assert fetch_k == 40


def test_nodes_to_tool_hits_preserves_matched_by():
    node = SimpleNamespace(
        node_id="42",
        text="hello world",
        metadata={
            "doc_url": "file:///doc.odt",
            "para_index": 3,
            "matched_by": ["fts", "vec"],
        },
    )
    scored = SimpleNamespace(node=node, score=0.05)
    hits = embeddings_llama_index._nodes_to_tool_hits([scored])
    assert len(hits) == 1
    assert hits[0]["chunk_id"] == 42
    assert hits[0]["doc_url"] == "file:///doc.odt"
    assert hits[0]["para_index"] == 3
    assert hits[0]["matched_by"] == ["fts", "vec"]


def test_nodes_to_tool_hits_skips_snippet_truncation_for_parent_expanded():
    long_text = "word " * 200
    node = SimpleNamespace(
        node_id="1",
        text=long_text,
        metadata={
            "doc_url": "file:///doc.odt",
            "para_index": 0,
            "parent_expanded": True,
        },
    )
    scored = SimpleNamespace(node=node, score=0.5)
    hits = embeddings_llama_index._nodes_to_tool_hits([scored])
    assert hits[0]["snippet"] == long_text.strip()


def test_apply_postprocessors_truncates_without_rerank():
    nodes = [SimpleNamespace(node=SimpleNamespace(text=f"n{i}", metadata={}), score=1.0) for i in range(5)]
    out = embeddings_llama_index._apply_llama_index_postprocessors(
        nodes,
        SimpleNamespace(query_str="q"),
        final_k=2,
        use_rerank=False,
    )
    assert len(out) == 2


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
def test_writer_agent_embedding_delegates_to_embed_texts():
    mock_vectors = [[0.1, 0.2, 0.3]]
    with patch(
        "plugin.embeddings.venv.embeddings_index.embed_texts",
        return_value={"vectors": mock_vectors},
    ) as mock_embed:
        embed_model = embeddings_llama_index.WriterAgentEmbedding(model_name="mock-model")

        result_query = embed_model._get_query_embedding("hello")
        assert result_query == [0.1, 0.2, 0.3]
        mock_embed.assert_any_call("mock-model", ["hello"])

        result_text = embed_model._get_text_embedding("world")
        assert result_text == [0.1, 0.2, 0.3]
        mock_embed.assert_any_call("mock-model", ["world"])


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
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
        assert nodes[0].node.metadata["matched_by"] == ["fts"]
        assert nodes[0].score == 5.2


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
def test_writer_agent_vector_store_delete_delegates_to_sqlite():
    mock_rows = [{"chunk_id": 42}]
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = mock_rows

    with patch("plugin.embeddings.venv.embeddings_sqlite.connect_corpus_db", return_value=mock_conn), patch(
        "plugin.embeddings.venv.embeddings_sqlite._delete_chunk_ids"
    ) as mock_delete:
        store = embeddings_llama_index.WriterAgentVectorStore(
            db_path="/tmp/fake.db",
            embedding_model="mock-model",
            build_fts=True,
            build_vectors=True,
        )
        store.delete(ref_doc_id="file:///doc.odt")

        mock_conn.execute.assert_called_with("SELECT chunk_id FROM chunks WHERE doc_url = ?", ("file:///doc.odt",))
        mock_delete.assert_called_with(mock_conn, [42], with_fts=True, with_vec=True)
        mock_conn.commit.assert_called_once()


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
def test_build_hybrid_retriever_without_openai_api_key(monkeypatch):
    """QueryFusionRetriever must not require OPENAI_API_KEY when num_queries=1."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mock_vector = MagicMock()
    mock_fts = MagicMock()
    with patch(
        "plugin.embeddings.venv.embeddings_llama_index.WriterAgentVectorStore",
    ), patch(
        "plugin.embeddings.venv.embeddings_llama_index.WriterAgentEmbedding",
    ), patch(
        "plugin.embeddings.venv.embeddings_llama_index.VectorStoreIndex.from_vector_store",
        return_value=MagicMock(as_retriever=MagicMock(return_value=mock_vector)),
    ), patch(
        "plugin.embeddings.venv.embeddings_llama_index.WriterAgentFTSRetriever",
        return_value=mock_fts,
    ), patch(
        "plugin.embeddings.venv.embeddings_llama_index.QueryFusionRetriever",
    ) as mock_qfr:
        embeddings_llama_index.build_writer_agent_hybrid_retriever(
            "/tmp/corpus.db",
            "mock-model",
            fetch_k=20,
        )
        assert mock_qfr.call_args.kwargs.get("llm") is not None
        assert mock_qfr.call_args.kwargs.get("num_queries") == 1


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
def test_run_hybrid_retrieval_pipeline_uses_rerank_postprocessor():
    mock_node = embeddings_llama_index.NodeWithScore(
        node=embeddings_llama_index.TextNode(
            text="snippet text",
            id_="9",
            metadata={"doc_url": "file:///hit.odt", "para_index": 1},
        ),
        score=0.03,
    )
    mock_retriever = MagicMock()
    mock_retriever.retrieve.return_value = [mock_node]
    mock_reranker = MagicMock()
    mock_reranker.postprocess_nodes.return_value = [mock_node]

    with patch(
        "plugin.embeddings.venv.embeddings_llama_index.build_writer_agent_hybrid_retriever",
        return_value=mock_retriever,
    ), patch(
        "plugin.embeddings.venv.embeddings_parent_hits.expand_nodes_to_parent_paragraphs",
        side_effect=lambda _db, nodes: nodes,
    ), patch(
        "plugin.embeddings.venv.embeddings_llama_index.SentenceTransformerRerank",
        return_value=mock_reranker,
    ):
        hits = embeddings_llama_index.run_hybrid_retrieval_pipeline(
            "/tmp/corpus.db",
            "query",
            5,
            model_name="mock-model",
            use_mmr=True,
        )
    assert len(hits) == 1
    assert hits[0]["doc_url"] == "file:///hit.odt"
    mock_reranker.postprocess_nodes.assert_called_once()
