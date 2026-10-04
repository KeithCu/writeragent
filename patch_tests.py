import re

with open("tests/embeddings/venv/test_embeddings_folder_maintain.py", "r") as f:
    content = f.read()

new_tests = """

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
"""

with open("tests/embeddings/venv/test_embeddings_folder_maintain.py", "w") as f:
    f.write(content + new_tests)
