# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_folder_maintain."""

from __future__ import annotations

from unittest.mock import patch

from plugin.embeddings.embeddings_fs import ParagraphChunk, WriterFileEntry
from plugin.embeddings.venv import embeddings_folder_maintain as maintain


def _chunk(doc_url: str, para_index: int, text: str) -> ParagraphChunk:
    return ParagraphChunk(
        doc_url=doc_url,
        para_index=para_index,
        char_start=0,
        char_end=len(text),
        text=text,
        content_hash=f"hash-{para_index}",
        file_mtime=1.0,
        doc_path="",
    )


def test_cold_build_ingests_one_file_at_a_time(tmp_path):
    """Cold build should not accumulate the whole folder before a single ingest RPC."""
    listing_root = str(tmp_path)
    files = [
        WriterFileEntry(path="/a.odt", url="file:///a.odt", modified=1.0, name="a.odt"),
        WriterFileEntry(path="/b.odt", url="file:///b.odt", modified=2.0, name="b.odt"),
    ]
    ingest_calls: list[int] = []

    def _fake_ingest_rows(
        root: str,
        model: str,
        rows: list,
        *,
        delete_keys=None,
        build_fts: bool,
        build_vectors: bool,
    ) -> dict:
        del root, model, delete_keys, build_fts, build_vectors
        ingest_calls.append(len(rows))
        return {"indexed": len(rows), "upserted": len(rows)}

    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    db_path.parent.mkdir(parents=True)

    with (
        patch.object(maintain, "clear_folder_cache"),
        patch.object(maintain, "ensure_corpus_meta"),
        patch.object(maintain, "paragraph_chunks_from_path", side_effect=[[_chunk("file:///a.odt", 0, "a")], [_chunk("file:///b.odt", 0, "b")]]),
        patch.object(maintain, "_ingest_rows", side_effect=_fake_ingest_rows),
        patch.object(maintain, "sync_file_paragraph_state") as sync_mock,
        patch.object(maintain, "corpus_db_path", return_value=db_path),
        patch.object(maintain, "_write_row_count_meta"),
    ):
        result = maintain._cold_build(
            listing_root,
            "test-model",
            files,
            maintain._HeartbeatThrottle(None),
            build_fts=True,
            build_vectors=True,
        )

    assert ingest_calls == [1, 1]
    assert sync_mock.call_count == 2
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] == 2
    assert result["upserted"] == 2
    assert result["files"] == 2


def test_cold_build_skips_ingest_for_empty_files(tmp_path):
    listing_root = str(tmp_path)
    files = [WriterFileEntry(path="/empty.odt", url="file:///empty.odt", modified=1.0, name="empty.odt")]

    with (
        patch.object(maintain, "clear_folder_cache"),
        patch.object(maintain, "ensure_corpus_meta"),
        patch.object(maintain, "paragraph_chunks_from_path", return_value=[]),
        patch.object(maintain, "_ingest_rows") as ingest_mock,
        patch.object(maintain, "sync_file_paragraph_state") as sync_mock,
        patch.object(maintain, "corpus_db_path", return_value=tmp_path / "writeragent_embeddings" / "corpus.db"),
        patch.object(maintain, "_write_row_count_meta"),
    ):
        result = maintain._cold_build(
            listing_root,
            "test-model",
            files,
            maintain._HeartbeatThrottle(None),
            build_fts=False,
            build_vectors=False,
        )

    ingest_mock.assert_not_called()
    sync_mock.assert_called_once()
    assert result["indexed_paragraphs"] == 0
