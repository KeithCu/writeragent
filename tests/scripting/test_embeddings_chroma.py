# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.embeddings_chroma."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.scripting import embeddings_chroma


def test_chunk_id_for_stable():
    a = embeddings_chroma.chunk_id_for("file:///a.odt", 0, 0, 5, "hash1")
    b = embeddings_chroma.chunk_id_for("file:///a.odt", 0, 0, 5, "hash1")
    c = embeddings_chroma.chunk_id_for("file:///a.odt", 0, 0, 6, "hash1")
    assert a == b
    assert a != c
    assert len(a) == 32


def test_build_chunk_metadata_types():
    meta = embeddings_chroma.build_chunk_metadata(
        doc_url="file:///a.odt",
        para_index=1,
        char_start=0,
        char_end=10,
        content_hash="abc",
        file_mtime=1.5,
        embedding_model="m",
        chunk_index=0,
    )
    assert meta["para_index"] == 1
    assert meta["file_mtime"] == 1.5


def test_delete_by_doc_para():
    collection = MagicMock()
    collection.get.return_value = {"ids": ["id1", "id2"]}
    deleted = embeddings_chroma.delete_by_doc_para(collection, "file:///a.odt", 0)
    assert deleted == 2
    collection.delete.assert_called_once_with(ids=["id1", "id2"])


def test_collection_count():
    collection = MagicMock()
    collection.count.return_value = 7
    assert embeddings_chroma.collection_count(collection) == 7
