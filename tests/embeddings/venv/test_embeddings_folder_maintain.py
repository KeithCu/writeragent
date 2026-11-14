# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_folder_maintain."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.embeddings.embeddings_fs import ParagraphChunk, WriterFileEntry
from plugin.embeddings.venv import embeddings_folder_maintain
from plugin.embeddings.venv.embeddings_zvec import maintain_folder_zvec
from plugin.embeddings.venv.heartbeat_throttle import HeartbeatThrottle


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

    with patch.object(embeddings_folder_maintain.CorpusMeta, "load", return_value=MagicMock(needs_cold_rebuild=MagicMock(return_value=True))):
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
    # schema_version must match EMBEDDINGS_SCHEMA_VERSION. A legacy version makes
    # maybe_upgrade_legacy_index wipe corpus.db before the incremental pass, so a
    # file that is already indexed looks stale and is re-embedded.
    meta.write_text(
        '{"schema_version":"6","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
        encoding="utf-8",
    )

    from plugin.embeddings.venv.embeddings_sqlite import connect_corpus_db, mark_file_indexed_in_db
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
        mark_file_indexed_in_db(
            conn,
            doc_url,
            doc.stat().st_mtime,
            indexed_at=doc.stat().st_mtime,
        )
    finally:
        conn.close()

    with patch("plugin.embeddings.venv.embeddings_folder_maintain._ingest_rows") as ingest_mock:
        result = embeddings_folder_maintain.maintain_folder_index(
            str(tmp_path),
            embedding_model="all-MiniLM-L6-v2",
            mode="incremental",
        )
    ingest_mock.assert_not_called()
    assert result["mode"] == "incremental"
    assert result["indexed_paragraphs"] == 0


def test_incremental_refresh_marks_stale_on_embed_failure(tmp_path: Path):
    doc = tmp_path / "a.odt"
    _write_min_odt(doc, "new content")
    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        '{"schema_version":"6","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
        encoding="utf-8",
    )
    import sqlite3
    from plugin.embeddings.venv.embeddings_sqlite import ensure_schema, mark_file_indexed_in_db, insert_paragraph_rows, get_file_index_info

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, with_fts=True, with_vec=False)
    doc_url = doc.as_uri()

    # mark it as stale (file_mtime=0)
    mark_file_indexed_in_db(conn, doc_url, 0.0, indexed_at=0.0)
    insert_paragraph_rows(conn, [{"text": "old", "doc_url": doc_url, "para_index": 0, "content_hash": "old"}], with_fts=True)
    conn.close()

    def fake_ingest(*args, **kwargs):
        raise RuntimeError("Embed failed")

    from unittest.mock import patch
    with patch("plugin.embeddings.venv.embeddings_folder_maintain._ingest_rows", side_effect=fake_ingest):
        try:
            embeddings_folder_maintain.maintain_folder_corpus(str(tmp_path), embedding_model="all-MiniLM-L6-v2", search_mode="fts")
        except RuntimeError:
            pass

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    info = get_file_index_info(conn, doc_url)
    conn.close()

    # Because ingest failed, file_mtime should still be 0.0, NOT the new doc's mtime
    assert info["file_mtime"] == 0.0


def test_incremental_refresh_removes_deleted_files(tmp_path: Path):
    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        '{"schema_version":"6","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
        encoding="utf-8",
    )

    import sqlite3
    from plugin.embeddings.venv.embeddings_sqlite import ensure_schema, mark_file_indexed_in_db, insert_paragraph_rows, get_file_index_info

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, with_fts=True, with_vec=False)

    deleted_doc_url = (tmp_path / "deleted.odt").as_uri()
    mark_file_indexed_in_db(conn, deleted_doc_url, 12345.0, indexed_at=12345.0)
    insert_paragraph_rows(conn, [{"text": "deleted text", "doc_url": deleted_doc_url, "para_index": 0, "content_hash": "del"}], with_fts=True)

    kept_doc = tmp_path / "kept.odt"
    _write_min_odt(kept_doc, "kept content")
    kept_doc_url = kept_doc.as_uri()
    mark_file_indexed_in_db(conn, kept_doc_url, kept_doc.stat().st_mtime, indexed_at=12345.0)
    insert_paragraph_rows(conn, [{"text": "kept text", "doc_url": kept_doc_url, "para_index": 0, "content_hash": "kept"}], with_fts=True)
    conn.close()

    embeddings_folder_maintain.maintain_folder_corpus(str(tmp_path), embedding_model="all-MiniLM-L6-v2", search_mode="fts")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # deleted_doc_url should be removed from indexed_files
    info = get_file_index_info(conn, deleted_doc_url)
    assert info["file_mtime"] == 0.0
    assert info["chunk_count"] == 0

    # kept_doc_url should still be there
    info2 = get_file_index_info(conn, kept_doc_url)
    assert info2["chunk_count"] == 1

    conn.close()


def test_incremental_refresh_handles_empty_text_files(tmp_path: Path):
    doc = tmp_path / "a.odt"
    _write_min_odt(doc, "") # Empty text
    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    db_path = tmp_path / "writeragent_embeddings" / "corpus.db"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        '{"schema_version":"6","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
        encoding="utf-8",
    )

    import sqlite3
    from plugin.embeddings.venv.embeddings_sqlite import ensure_schema, mark_file_indexed_in_db, insert_paragraph_rows, get_file_index_info

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn, with_fts=True, with_vec=False)
    doc_url = doc.as_uri()

    # Mark it as stale with some old chunks
    mark_file_indexed_in_db(conn, doc_url, 0.0, indexed_at=0.0)
    insert_paragraph_rows(conn, [{"text": "old text", "doc_url": doc_url, "para_index": 0, "content_hash": "old"}], with_fts=True)
    conn.close()

    embeddings_folder_maintain.maintain_folder_corpus(str(tmp_path), embedding_model="all-MiniLM-L6-v2", search_mode="fts")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    info = get_file_index_info(conn, doc_url)

    # The file mtime should be updated
    assert info["file_mtime"] == doc.stat().st_mtime
    # The chunk_count should be 0 because the empty file text was extracted and the diff_chunk_rows removed the old chunks
    assert info["chunk_count"] == 0

    conn.close()


def test_zvec_incremental_maintain_skips_matching_mtime_doc(tmp_path: Path):
    """Zvec Doc with .field('file_mtime') matching file mtime skips re-embedding."""
    meta_dir = tmp_path / "writeragent_embeddings"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "corpus_meta.json"
    meta_path.write_text('{"schema_version":"6","embedding_model":"m","chunk_count":5}', encoding="utf-8")

    doc = tmp_path / "test.odt"
    _write_min_odt(doc, "content")

    class _DocWithField:
        def __init__(self, mtime: float):
            self._mtime = mtime

        def field(self, name: str) -> float | None:
            if name == "file_mtime":
                return self._mtime
            return None

    mock_coll = MagicMock()
    mock_coll.query.return_value = [_DocWithField(doc.stat().st_mtime)]
    mock_coll.__len__ = MagicMock(return_value=5)

    with patch("plugin.embeddings.venv.embeddings_zvec.HAS_ZVEC", True), \
         patch("plugin.embeddings.venv.embeddings_zvec.zvec", MagicMock()), \
         patch("plugin.embeddings.venv.embeddings_zvec._COLL_CACHE", {str(meta_dir / "zvec_collection"): mock_coll}), \
         patch("plugin.embeddings.embeddings_cache.zvec_collection_path", return_value=meta_dir / "zvec_collection"), \
         patch("plugin.embeddings.embeddings_fs.indexable_chunks_from_path") as mock_extract:
        res = maintain_folder_zvec(str(tmp_path), "m", mode="incremental")
        assert res["mode"] == "zvec"
        mock_coll.query.assert_called_once()
        # Only called once during initial dim probe; skipped in the file extraction loop
        assert mock_extract.call_count == 1


def test_zvec_incremental_maintain_does_not_skip_with_existing_meta(tmp_path: Path):
    meta_dir = tmp_path / "writeragent_embeddings"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "corpus_meta.json"
    meta_path.write_text('{"schema_version":"6","embedding_model":"m","chunk_count":5}', encoding="utf-8")

    doc = tmp_path / "test.odt"
    _write_min_odt(doc, "content")

    mock_coll = MagicMock()
    mock_coll.query.return_value = [{"file_mtime": doc.stat().st_mtime}]
    mock_coll.__len__ = MagicMock(return_value=5)
    with patch("plugin.embeddings.venv.embeddings_zvec.HAS_ZVEC", True), \
         patch("plugin.embeddings.venv.embeddings_zvec.zvec", MagicMock()), \
         patch("plugin.embeddings.venv.embeddings_zvec._COLL_CACHE", {str(meta_dir / "zvec_collection"): mock_coll}), \
         patch("plugin.embeddings.embeddings_cache.zvec_collection_path", return_value=meta_dir / "zvec_collection"):
        res = maintain_folder_zvec(str(tmp_path), "m", mode="incremental")
        assert res["mode"] == "zvec"
        mock_coll.query.assert_called()


def test_zvec_incremental_maintain_delete_failure_skips_file(tmp_path: Path):
    """Bug B1: failed delete before re-ingest skips the file so it is retried."""
    meta_dir = tmp_path / "writeragent_embeddings"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "corpus_meta.json"
    meta_path.write_text('{"schema_version":"6","embedding_model":"m","chunk_count":5}', encoding="utf-8")

    doc = tmp_path / "test.odt"
    _write_min_odt(doc, "content")

    mock_coll = MagicMock()
    mock_coll.query.return_value = []
    mock_coll.__len__ = MagicMock(return_value=5)
    mock_coll.delete_by_filter.side_effect = RuntimeError("disk locked")

    with patch("plugin.embeddings.venv.embeddings_zvec.HAS_ZVEC", True), \
         patch("plugin.embeddings.venv.embeddings_zvec.zvec", MagicMock()), \
         patch("plugin.embeddings.venv.embeddings_zvec._COLL_CACHE", {str(meta_dir / "zvec_collection"): mock_coll}), \
         patch("plugin.embeddings.embeddings_cache.zvec_collection_path", return_value=meta_dir / "zvec_collection"), \
         patch("plugin.embeddings.venv.embeddings_zvec.zvec_ingest_rows") as mock_ingest:
        res = maintain_folder_zvec(str(tmp_path), "m", mode="incremental")
        assert res["mode"] == "zvec"
        assert res["indexed_paragraphs"] == 0
        assert res["upserted"] == 0
        mock_coll.delete_by_filter.assert_called_once()
        mock_ingest.assert_not_called()


def test_zvec_maintain_stale_purge_failed_delete_skips_sqlite_removal(tmp_path: Path):
    """Bug B1: failed delete during stale purge skips removing from sqlite index."""
    meta_dir = tmp_path / "writeragent_embeddings"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "corpus_meta.json"
    meta_path.write_text('{"schema_version":"6","embedding_model":"m","chunk_count":0}', encoding="utf-8")

    mock_coll = MagicMock()
    mock_coll.delete_by_filter.side_effect = RuntimeError("purge I/O error")

    with patch("plugin.embeddings.venv.embeddings_zvec.HAS_ZVEC", True), \
         patch("plugin.embeddings.venv.embeddings_zvec.zvec", MagicMock()), \
         patch("plugin.embeddings.venv.embeddings_zvec._COLL_CACHE", {str(meta_dir / "zvec_collection"): mock_coll}), \
         patch("plugin.embeddings.embeddings_cache.zvec_collection_path", return_value=meta_dir / "zvec_collection"), \
         patch("plugin.embeddings.venv.embeddings_sqlite.get_all_indexed_urls_in_db", return_value=["file:///tmp/removed.odt"]), \
         patch("plugin.embeddings.venv.embeddings_sqlite.remove_file_from_index_in_db") as mock_remove:
        res = maintain_folder_zvec(str(tmp_path), "m", mode="incremental")
        assert res["mode"] == "zvec"
        mock_coll.delete_by_filter.assert_called_once()
        mock_remove.assert_not_called()


def _write_populated_meta(listing, model: str, *, sqlite: bool = True, collection: str | None = None, chunk_count: str = "4") -> None:
    base = listing / "writeragent_embeddings"
    base.mkdir(parents=True, exist_ok=True)
    if sqlite:
        (base / "corpus.db").write_text("sqlite", encoding="utf-8")
    (base / "corpus_meta.json").write_text(
        json.dumps(
            {
                "schema_version": "6",
                "embedding_model": model,
                "chunk_count": chunk_count,
                "dim": "384",
            }
        ),
        encoding="utf-8",
    )
    if collection:
        store = base / collection
        store.mkdir()
        (store / "segment").write_text("rows", encoding="utf-8")


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
        patch.object(embeddings_folder_maintain, "clear_folder_cache"),
        patch.object(embeddings_folder_maintain.CorpusMeta, "ensure"),
        patch.object(embeddings_folder_maintain, "indexable_chunks_from_path", side_effect=[(1, [_chunk("file:///a.odt", 0, "a")]), (1, [_chunk("file:///b.odt", 0, "b")])]),
        patch.object(embeddings_folder_maintain, "_ingest_rows", side_effect=_fake_ingest_rows),
        patch.object(embeddings_folder_maintain, "sync_file_paragraph_state_in_db") as sync_mock,
        patch.object(embeddings_folder_maintain, "corpus_db_path", return_value=db_path),
        patch.object(embeddings_folder_maintain.CorpusMeta, "write_row_count"),
    ):
        result = embeddings_folder_maintain._cold_build(
            listing_root,
            "test-model",
            files,
            HeartbeatThrottle(None),
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
        patch.object(embeddings_folder_maintain, "clear_folder_cache"),
        patch.object(embeddings_folder_maintain.CorpusMeta, "ensure"),
        patch.object(embeddings_folder_maintain, "indexable_chunks_from_path", return_value=(0, [])),
        patch.object(embeddings_folder_maintain, "_ingest_rows") as ingest_mock,
        patch.object(embeddings_folder_maintain, "sync_file_paragraph_state_in_db") as sync_mock,
        patch.object(embeddings_folder_maintain, "corpus_db_path", return_value=tmp_path / "writeragent_embeddings" / "corpus.db"),
        patch.object(embeddings_folder_maintain.CorpusMeta, "write_row_count"),
    ):
        result = embeddings_folder_maintain._cold_build(
            listing_root,
            "test-model",
            files,
            HeartbeatThrottle(None),
            build_fts=False,
            build_vectors=False,
        )

    ingest_mock.assert_not_called()
    sync_mock.assert_called_once()
    assert result["indexed_paragraphs"] == 0


def test_model_change_forces_cold_even_when_incremental(tmp_path):
    _write_populated_meta(tmp_path, "old-model")
    with (
        patch.object(embeddings_folder_maintain, "guess_indexable_paths", return_value=[]),
        patch.object(embeddings_folder_maintain, "_cold_build", return_value={"mode": "cold"}) as cold,
        patch.object(embeddings_folder_maintain, "_incremental_refresh") as incremental,
    ):
        result = embeddings_folder_maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="new-model",
            search_mode="embeddings",
            mode="incremental",
        )
    cold.assert_called_once()
    incremental.assert_not_called()
    assert result["mode"] == "cold"


def test_resolve_mode_model_change_and_non_dict_meta(tmp_path):
    _write_populated_meta(tmp_path, "old-model")
    assert embeddings_folder_maintain._resolve_mode(str(tmp_path), "old-model", "auto", build_vectors=True) == "incremental"
    assert embeddings_folder_maintain._resolve_mode(str(tmp_path), "new-model", "incremental", build_vectors=True) == "cold"
    assert embeddings_folder_maintain._resolve_mode(str(tmp_path), "new-model", "auto", build_vectors=False) == "incremental"

    meta = tmp_path / "writeragent_embeddings" / "corpus_meta.json"
    meta.write_text("[]", encoding="utf-8")
    assert embeddings_folder_maintain._resolve_mode(str(tmp_path), "new-model", "auto", build_vectors=True) == "incremental"


def test_zvec_model_change_clears_collection_before_maintain(tmp_path):
    _write_populated_meta(tmp_path, "old-model")
    with (
        patch(
            "plugin.embeddings.venv.embeddings_zvec.maintain_folder_zvec",
            return_value={"mode": "zvec"},
        ) as zvec,
        patch.object(embeddings_folder_maintain, "clear_folder_cache") as clear,
    ):
        embeddings_folder_maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="new-model",
            search_mode="zvec",
            mode="auto",
        )
    clear.assert_called_once_with(str(tmp_path))
    zvec.assert_called_once()


def test_zvec_same_model_does_not_clear_when_corpus_db_missing(tmp_path):
    """A zvec store has no corpus.db. Auto maintain must not cold-wipe it."""
    _write_populated_meta(tmp_path, "old-model", sqlite=False, collection="zvec")
    with (
        patch(
            "plugin.embeddings.venv.embeddings_zvec.maintain_folder_zvec",
            return_value={"mode": "zvec"},
        ) as zvec,
        patch.object(embeddings_folder_maintain, "clear_folder_cache") as clear,
    ):
        embeddings_folder_maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="old-model",
            search_mode="zvec",
            mode="auto",
        )
    clear.assert_not_called()
    zvec.assert_called_once()


def test_lancedb_same_model_does_not_clear_when_corpus_db_missing(tmp_path):
    _write_populated_meta(tmp_path, "old-model", sqlite=False, collection="lancedb")
    with (
        patch(
            "plugin.embeddings.venv.embeddings_lancedb.maintain_folder_lancedb",
            return_value={"mode": "lancedb"},
        ) as lance,
        patch.object(embeddings_folder_maintain, "clear_folder_cache") as clear,
    ):
        embeddings_folder_maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="old-model",
            search_mode="lancedb",
            mode="auto",
        )
    clear.assert_not_called()
    lance.assert_called_once()


def test_zvec_missing_collection_is_cold(tmp_path):
    _write_populated_meta(tmp_path, "old-model", sqlite=False)
    with (
        patch(
            "plugin.embeddings.venv.embeddings_zvec.maintain_folder_zvec",
            return_value={"mode": "zvec"},
        ),
        patch.object(embeddings_folder_maintain, "clear_folder_cache") as clear,
    ):
        embeddings_folder_maintain.maintain_folder_corpus(
            str(tmp_path),
            embedding_model="old-model",
            search_mode="zvec",
            mode="auto",
        )
    clear.assert_called_once_with(str(tmp_path))


def test_resolve_mode_zvec_uses_collection_not_corpus_db(tmp_path):
    _write_populated_meta(tmp_path, "m", sqlite=False, collection="zvec")
    assert embeddings_folder_maintain._resolve_mode(str(tmp_path), "m", "auto", build_vectors=True, search_mode="zvec") == "incremental"
    (tmp_path / "writeragent_embeddings" / "zvec" / "segment").unlink()
    (tmp_path / "writeragent_embeddings" / "zvec").rmdir()
    assert embeddings_folder_maintain._resolve_mode(str(tmp_path), "m", "auto", build_vectors=True, search_mode="zvec") == "cold"


def test_incremental_same_model_vector_gap_requests_backfill(tmp_path):
    """has_missing must embed. An empty ingest with no flag returns before the graph."""
    conn = MagicMock()
    with (
        patch.object(embeddings_folder_maintain, "connect_corpus_db", return_value=conn),
        patch.object(embeddings_folder_maintain, "ensure_schema"),
        patch.object(embeddings_folder_maintain, "has_missing_vector_chunks", return_value=True),
        patch.object(embeddings_folder_maintain, "corpus_chunk_count", return_value=2),
        patch.object(embeddings_folder_maintain.CorpusMeta, "write_row_count"),
        patch.object(embeddings_folder_maintain, "_ingest_rows", return_value={"indexed": 2, "upserted": 2}) as ingest,
    ):
        embeddings_folder_maintain._incremental_refresh(
            str(tmp_path),
            "all-MiniLM-L6-v2",
            [],
            HeartbeatThrottle(None),
            build_fts=True,
            build_vectors=True,
            search_mode="embeddings",
        )
    ingest.assert_called_once()
    assert ingest.call_args.args[2] == []
    assert ingest.call_args.kwargs["fill_vector_gaps"] is True
    assert ingest.call_args.kwargs["build_vectors"] is True


def test_incremental_skips_backfill_when_vectors_present(tmp_path):
    conn = MagicMock()
    with (
        patch.object(embeddings_folder_maintain, "connect_corpus_db", return_value=conn),
        patch.object(embeddings_folder_maintain, "ensure_schema"),
        patch.object(embeddings_folder_maintain, "get_all_indexed_urls_in_db", return_value=[]),
        patch.object(embeddings_folder_maintain, "has_missing_vector_chunks", return_value=False),
        patch.object(embeddings_folder_maintain, "corpus_chunk_count", return_value=2),
        patch.object(embeddings_folder_maintain.CorpusMeta, "write_row_count"),
        patch.object(embeddings_folder_maintain, "_ingest_rows") as ingest,
    ):
        embeddings_folder_maintain._incremental_refresh(
            str(tmp_path),
            "all-MiniLM-L6-v2",
            [],
            HeartbeatThrottle(None),
            build_fts=False,
            build_vectors=True,
            search_mode="embeddings",
        )
    ingest.assert_not_called()


def test_incremental_gap_probe_empty_corpus_without_vec_table(tmp_path):
    """No vec table and no chunk rows: gap probe must not load sqlite-vec or start gap-fill."""
    with (
        patch.object(embeddings_folder_maintain, "get_all_indexed_urls_in_db", return_value=[]),
        patch.object(embeddings_folder_maintain, "corpus_chunk_count", return_value=0),
        patch.object(embeddings_folder_maintain.CorpusMeta, "write_row_count"),
        patch.object(embeddings_folder_maintain, "_ingest_rows") as ingest,
        patch(
            "plugin.embeddings.venv.embeddings_sqlite._load_vec_extension",
            side_effect=AssertionError("must not load vec when vec table is absent"),
        ),
    ):
        embeddings_folder_maintain._incremental_refresh(
            str(tmp_path),
            "all-MiniLM-L6-v2",
            [],
            HeartbeatThrottle(None),
            build_fts=False,
            build_vectors=True,
            search_mode="embeddings",
        )
    ingest.assert_not_called()


def test_fill_vector_gaps_uses_sqlite_ingest(tmp_path):
    with (
        patch.object(embeddings_folder_maintain, "ingest_paragraphs", return_value={"indexed": 1}) as ingest,
        patch("plugin.embeddings.venv.embeddings_llama_index.llama_index_ingest") as llama,
    ):
        embeddings_folder_maintain._ingest_rows(
            str(tmp_path),
            "model",
            [],
            build_fts=False,
            build_vectors=True,
            search_mode="llama_index",
            fill_vector_gaps=True,
        )
    ingest.assert_called_once()
    assert ingest.call_args.kwargs["fill_vector_gaps"] is True
    llama.assert_not_called()
