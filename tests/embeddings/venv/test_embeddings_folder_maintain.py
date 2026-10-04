# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.embeddings.venv.embeddings_folder_maintain."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

from plugin.embeddings.venv import embeddings_folder_maintain


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
    # schema_version must match EMBEDDINGS_SCHEMA_VERSION. A legacy version makes
    # maybe_upgrade_legacy_index wipe corpus.db before the incremental pass, so a
    # file that is already indexed looks stale and is re-embedded.
    meta.write_text(
        '{"schema_version":"6","embedding_model":"all-MiniLM-L6-v2","chunk_count":"1"}',
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
    import plugin.embeddings.venv.embeddings_folder_maintain as embeddings_folder_maintain
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
    import plugin.embeddings.venv.embeddings_folder_maintain as embeddings_folder_maintain
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
    import plugin.embeddings.venv.embeddings_folder_maintain as embeddings_folder_maintain
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
