# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_ingest_graph."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.embeddings.venv import embeddings_ingest_graph


def test_ingest_paragraphs_invokes_graph(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    with patch.object(embeddings_ingest_graph, "_get_ingest_graph") as mock_graph_factory:
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"upserted": 1, "dim": 384}
        mock_graph_factory.return_value = mock_graph
        result = embeddings_ingest_graph.ingest_paragraphs(
            str(tmp_path / "corpus.db"),
            str(meta_path),
            "all-MiniLM-L6-v2",
            [{"text": "hello", "doc_url": "file:///a.odt", "para_index": 0, "content_hash": "h", "file_mtime": 1.0}],
        )
    assert result["indexed"] == 1
    assert result["storage_backend"] == "sqlite_vec"
    mock_graph.invoke.assert_called_once()


def test_ingest_paragraphs_empty_rows_skip_graph_unless_filling_gaps(tmp_path):
    meta_path = tmp_path / "corpus_meta.json"
    db_path = str(tmp_path / "corpus.db")
    with patch.object(embeddings_ingest_graph, "_get_ingest_graph") as mock_graph_factory:
        skipped = embeddings_ingest_graph.ingest_paragraphs(db_path, str(meta_path), "all-MiniLM-L6-v2", [])
        assert skipped["indexed"] == 0
        mock_graph_factory.assert_not_called()

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"upserted": 2, "dim": 4}
        mock_graph_factory.return_value = mock_graph
        filled = embeddings_ingest_graph.ingest_paragraphs(
            db_path,
            str(meta_path),
            "all-MiniLM-L6-v2",
            [],
            fill_vector_gaps=True,
        )
    assert filled["indexed"] == 2
    mock_graph.invoke.assert_called_once()

def test_embed_and_upsert_batches_emits_heartbeats():
    from plugin.embeddings.venv.embeddings_ingest_graph import embed_and_upsert_batches
    import sqlite3

    mock_heartbeats = []
    def _hb(payload):
        mock_heartbeats.append(payload)

    # We mock enough to run the batching logic inside embed_and_upsert_batches.
    with patch("plugin.embeddings.venv.embeddings_ingest_graph.connect_corpus_db") as mock_connect, \
         patch("plugin.embeddings.venv.embeddings_sqlite._load_vec_extension"), \
         patch("plugin.embeddings.venv.embeddings_ingest_graph._dim_from_meta_path", return_value=384), \
         patch("plugin.embeddings.venv.embeddings_ingest_graph.ensure_schema"), \
         patch("plugin.embeddings.venv.embeddings_ingest_graph.embed_texts", return_value={"dim": 384, "vectors": [[0.1]*384]*2}), \
         patch("plugin.embeddings.venv.embeddings_ingest_graph.upsert_chunk_with_vector"), \
         patch("plugin.embeddings.venv.embeddings_ingest_graph.corpus_chunk_count", return_value=2), \
         patch("plugin.embeddings.venv.embeddings_ingest_graph._write_meta"):

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_connect.return_value = mock_conn

        state = {
            "db_path": "dummy.db",
            "meta_path": "dummy_meta.json",
            "model": "dummy-model",
            "build_fts": True,
            "build_vectors": True,
            "chunks": [
                {"doc_url": "file:///a.odt", "para_index": 0, "char_start": 0, "char_end": 10, "text": "hello"},
                {"doc_url": "file:///a.odt", "para_index": 1, "char_start": 0, "char_end": 10, "text": "world"},
            ],
            "heartbeat_fn": _hb
        }

        # Override batch size for test
        with patch("plugin.embeddings.venv.embeddings_ingest_graph.EMBEDDINGS_INGEST_BATCH_SIZE", 1):
            embed_and_upsert_batches(state)

    assert len(mock_heartbeats) == 2
    assert mock_heartbeats[0] == {"phase": "embed", "chunks": 1}
    assert mock_heartbeats[1] == {"phase": "embed", "chunks": 2}
