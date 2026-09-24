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
