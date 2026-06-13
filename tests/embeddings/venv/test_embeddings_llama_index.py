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


def test_fetch_pool_k_sizes():
    final_k, fetch_k = embeddings_llama_index._fetch_pool_k(10)
    assert final_k == 10
    assert fetch_k == 30
    final_k, fetch_k = embeddings_llama_index._fetch_pool_k(40)
    assert final_k == 30
    assert fetch_k == 30
    final_k, fetch_k = embeddings_llama_index._fetch_pool_k(5, pool_k=40)
    assert final_k == 5
    assert fetch_k == 40


def test_source_diversity_filter_caps_per_doc_url():
    nodes = [
        SimpleNamespace(node=SimpleNamespace(metadata={"doc_url": "file:///a.odt"})),
        SimpleNamespace(node=SimpleNamespace(metadata={"doc_url": "file:///a.odt"})),
        SimpleNamespace(node=SimpleNamespace(metadata={"doc_url": "file:///a.odt"})),
        SimpleNamespace(node=SimpleNamespace(metadata={"doc_url": "file:///b.odt"})),
    ]
    kept = embeddings_llama_index.source_diversity_filter(nodes, max_per_doc=2)
    assert len(kept) == 3
    urls = [n.node.metadata["doc_url"] for n in kept]
    assert urls.count("file:///a.odt") == 2
    assert urls.count("file:///b.odt") == 1


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
def test_weak_hit_filter_drops_zero_score_nodes():
    good = embeddings_llama_index.NodeWithScore(
        node=embeddings_llama_index.TextNode(
            text="a",
            id_="1",
            metadata={"doc_url": "file:///a.odt", "matched_by": ["vec"]},
        ),
        score=0.02,
    )
    bad = embeddings_llama_index.NodeWithScore(
        node=embeddings_llama_index.TextNode(text="b", id_="2", metadata={"doc_url": "file:///b.odt"}),
        score=0.0,
    )
    processor = embeddings_llama_index.WriterAgentWeakHitFilterPostprocessor(min_score=1e-6)
    kept = processor._postprocess_nodes([good, bad])
    assert len(kept) == 1
    assert kept[0].node.node_id == "1"


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
def test_fusion_retriever_merges_matched_by():
    vec_node = embeddings_llama_index.NodeWithScore(
        node=embeddings_llama_index.TextNode(
            text="same",
            id_="7",
            metadata={"doc_url": "file:///x.odt", "matched_by": ["vec"]},
        ),
        score=0.9,
    )
    fts_node = embeddings_llama_index.NodeWithScore(
        node=embeddings_llama_index.TextNode(
            text="same",
            id_="7",
            metadata={"doc_url": "file:///x.odt", "matched_by": ["fts"]},
        ),
        score=4.0,
    )
    # Force same hash so fusion treats them as one chunk
    vec_node.node.hash = "chunk-7"
    fts_node.node.hash = "chunk-7"

    fusion = embeddings_llama_index.WriterAgentQueryFusionRetriever(
        retrievers=[],
        similarity_top_k=5,
        num_queries=1,
        use_async=False,
    )
    fused = fusion._reciprocal_rerank_fusion({("q", 0): [vec_node], ("q", 1): [fts_node]})
    assert len(fused) == 1
    assert set(fused[0].node.metadata["matched_by"]) == {"fts", "vec"}


@pytest.mark.skipif(
    not HAS_LLAMA_INDEX,
    reason="llama-index-core is not installed in the testing environment",
)
def test_run_hybrid_retrieval_pipeline_returns_tool_hits():
    mock_node = embeddings_llama_index.NodeWithScore(
        node=embeddings_llama_index.TextNode(
            text="snippet text",
            id_="9",
            metadata={"doc_url": "file:///hit.odt", "para_index": 1, "matched_by": ["fts", "vec"]},
        ),
        score=0.03,
    )
    mock_retriever = MagicMock()
    mock_retriever.retrieve.return_value = [mock_node]

    with patch(
        "plugin.embeddings.venv.embeddings_llama_index.build_writer_agent_hybrid_retriever",
        return_value=mock_retriever,
    ):
        hits = embeddings_llama_index.run_hybrid_retrieval_pipeline(
            "/tmp/corpus.db",
            "query",
            5,
            model_name="mock-model",
            use_mmr=False,
        )
    assert len(hits) == 1
    assert hits[0]["doc_url"] == "file:///hit.odt"
    assert hits[0]["chunk_id"] == 9
    assert hits[0]["matched_by"] == ["fts", "vec"]
