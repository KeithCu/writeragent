# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_folder_maintain."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

from plugin.embeddings.embeddings_fs import ParagraphChunk, WriterFileEntry
from plugin.embeddings.venv import embeddings_folder_maintain
from plugin.embeddings.venv import embeddings_folder_maintain as maintain


def _write_min_odt(path: Path, text: str = "Hello") -> None:
    content_xml = f"""<?xml version="1.0"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
<office:body><office:text><text:p>{text}</text:p></office:text></office:body>
</office:document-content>""".encode()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", content_xml)


def test_maintain_cold_calls_heartbeat_and_ingest(tmp_path: Path):
    doc = tmp_path / "a.odt"
    _write_min_odt(doc)
    heartbeats: list[dict] = []

    with patch("plugin.embeddings.venv.embeddings_folder_maintain.index_is_empty", return_value=True):
        with patch("plugin.embeddings.venv.embeddings_folder_maintain.needs_cold_rebuild", return_value=True):
            with patch("plugin.embeddings.venv.embeddings_folder_maintain.clear_folder_cache") as clear_mock:
                with patch(
                    "plugin.embeddings.venv.embeddings_folder_maintain._ingest_rows",
                    return_value={"upserted": 1},
                ) as ingest_mock:
                    result = embeddings_folder_maintain.maintain_folder_index(
                        str(tmp_path),
                        embedding_model="all-MiniLM-L6-v2",
                        mode="cold",
                        heartbeat_fn=heartbeats.append,
                    )
    clear_mock.assert_called_once()
    ingest_mock.assert_called_once()
    assert result["mode"] == "cold"
    assert result["indexed_paragraphs"] == 1
    assert any(h.get("phase") == "start" for h in heartbeats)
    assert any(h.get("phase") == "done" for h in heartbeats)


def test_maintain_incremental_skips_fresh_file(tmp_path: Path):
    doc = tmp_path / "a.odt"
    _write_min_odt(doc, "same")
    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        '{"schema_version":"3","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
        encoding="utf-8",
    )

    from plugin.embeddings.embeddings_cache import mark_file_indexed
    from plugin.embeddings.embeddings_fs import content_hash
    from plugin.framework.url_utils import path_to_file_url
    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, ensure_schema, upsert_chunk_with_vector

    doc_url = path_to_file_url(str(doc))
    conn = connect_corpus_db(db_path)
    try:
        ensure_schema(conn, dim=4, with_fts=False, with_vec=True, model="all-MiniLM-L6-v2")
        upsert_chunk_with_vector(
            conn,
            {
                "doc_url": doc_url,
                "para_index": 0,
                "char_start": 0,
                "char_end": 4,
                "content_hash": content_hash("same"),
                "text": "same",
                "file_mtime": doc.stat().st_mtime,
            },
            [0.1, 0.2, 0.3, 0.4],
            model="all-MiniLM-L6-v2",
            with_fts=False,
            with_vec=True,
        )
        conn.commit()
    finally:
        conn.close()
    mark_file_indexed(
        db_path,
        doc_url,
        doc.stat().st_mtime,
        indexed_at=doc.stat().st_mtime,
    )

    with patch("plugin.embeddings.venv.embeddings_folder_maintain._ingest_rows") as ingest_mock:
        result = embeddings_folder_maintain.maintain_folder_index(
            str(tmp_path),
            embedding_model="all-MiniLM-L6-v2",
            mode="incremental",
        )
    ingest_mock.assert_not_called()
    assert result["mode"] == "incremental"
    assert result["indexed_paragraphs"] == 0

# --- cold-build ingest batching (from tests/embeddings/test_embeddings_folder_maintain.py) ---

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
        **kwargs,
    ) -> dict:
        del root, model, delete_keys, build_fts, build_vectors
        ingest_calls.append(len(rows))
        return {"indexed": len(rows), "upserted": len(rows)}

    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    db_path.parent.mkdir(parents=True)

    with (
        patch.object(maintain, "clear_folder_cache"),
        patch.object(maintain, "ensure_corpus_meta"),
        patch.object(maintain, "indexable_chunks_from_path", side_effect=[(1, [_chunk("file:///a.odt", 0, "a")]), (1, [_chunk("file:///b.odt", 0, "b")])]),
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
        patch.object(maintain, "indexable_chunks_from_path", return_value=(0, [])),
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
