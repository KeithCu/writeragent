from pathlib import Path
from unittest.mock import patch, MagicMock
import sqlite3

from plugin.embeddings.venv import embeddings_odf_extract
from plugin.embeddings.venv.embeddings_folder_maintain import _extract_file_chunks, _incremental_refresh, _HeartbeatThrottle, clear_folder_cache
from plugin.embeddings.embeddings_fs import WriterFileEntry
from tests.scripting.ods_fixtures import write_budget_ods

def test_emptied_calc_retains_chunks(tmp_path: Path):
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)

    # fake DB path
    from plugin.embeddings.embeddings_cache import corpus_db_path, _open_index_db
    db_path = corpus_db_path(str(tmp_path))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # We want to test diff_chunk_rows_in_db when chunks == []
    from plugin.embeddings.venv.embeddings_sqlite import diff_chunk_rows_in_db
    conn = _open_index_db(db_path)
    conn.execute("INSERT INTO chunks (doc_url, para_index, char_start, char_end, content_hash, body) VALUES (?, ?, ?, ?, ?, ?)", ("file:///fake.ods", 0, 0, 10, "abc", "bodytext"))
    conn.commit()

    to_index, to_delete = diff_chunk_rows_in_db(conn, [], doc_url="file:///fake.ods")
    assert len(to_delete) == 1
    assert to_delete[0]["doc_url"] == "file:///fake.ods"
    assert to_delete[0]["para_index"] == 0

def test_failed_extract_cold_build_skips(tmp_path: Path):
    from plugin.embeddings.venv.embeddings_folder_maintain import _cold_build
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    entry = WriterFileEntry(path=str(ods), url="file:///fake.ods", modified=1.0, name="Budget.ods")

    hb = _HeartbeatThrottle(lambda x: None)

    with patch("plugin.embeddings.venv.embeddings_folder_maintain._extract_file_chunks", return_value=(0, None)):
        with patch("plugin.embeddings.venv.embeddings_folder_maintain.sync_file_paragraph_state") as mock_sync:
            _cold_build(str(tmp_path), "fake_model", [entry], hb, build_fts=False, build_vectors=False)
            mock_sync.assert_not_called()

def test_failed_extract_incremental_refresh_skips(tmp_path: Path):
    from plugin.embeddings.venv.embeddings_folder_maintain import _incremental_refresh
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    entry = WriterFileEntry(path=str(ods), url="file:///fake.ods", modified=1.0, name="Budget.ods")

    hb = _HeartbeatThrottle(lambda x: None)

    with patch("plugin.embeddings.venv.embeddings_folder_maintain.file_is_stale", return_value=True):
        with patch("plugin.embeddings.venv.embeddings_folder_maintain._extract_file_chunks", return_value=(0, None)):
            with patch("plugin.embeddings.venv.embeddings_folder_maintain.diff_chunk_rows") as mock_diff:
                with patch("plugin.embeddings.venv.embeddings_folder_maintain.mark_file_indexed") as mock_mark:
                    _incremental_refresh(str(tmp_path), "fake_model", [entry], hb, build_fts=False, build_vectors=False)
                    mock_diff.assert_not_called()
                    mock_mark.assert_not_called()
