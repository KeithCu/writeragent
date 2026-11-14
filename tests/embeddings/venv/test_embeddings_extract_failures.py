from pathlib import Path
from unittest.mock import patch

from plugin.embeddings.venv.embeddings_folder_maintain import _incremental_refresh
from plugin.embeddings.venv.heartbeat_throttle import HeartbeatThrottle
from plugin.embeddings.embeddings_fs import WriterFileEntry
from tests.scripting.ods_fixtures import write_budget_ods

def test_failed_extract_cold_build_skips(tmp_path: Path):
    from plugin.embeddings.venv.embeddings_folder_maintain import _cold_build
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    entry = WriterFileEntry(path=str(ods), url="file:///fake.ods", modified=1.0, name="Budget.ods")

    hb = HeartbeatThrottle(lambda x: None)

    with patch("plugin.embeddings.venv.embeddings_folder_maintain._extract_file_chunks", return_value=(0, None)):
        with patch("plugin.embeddings.venv.embeddings_folder_maintain.sync_file_paragraph_state_in_db") as mock_sync:
            _cold_build(str(tmp_path), "fake_model", [entry], hb, build_fts=False, build_vectors=False)
            mock_sync.assert_not_called()

def test_failed_extract_incremental_refresh_skips(tmp_path: Path):
    ods = tmp_path / "Budget.ods"
    write_budget_ods(ods)
    entry = WriterFileEntry(path=str(ods), url="file:///fake.ods", modified=1.0, name="Budget.ods")

    hb = HeartbeatThrottle(lambda x: None)

    with patch("plugin.embeddings.venv.embeddings_folder_maintain.file_is_stale_in_db", return_value=True):
        with patch("plugin.embeddings.venv.embeddings_folder_maintain._extract_file_chunks", return_value=(0, None)):
            with patch("plugin.embeddings.venv.embeddings_folder_maintain.diff_chunk_rows_in_db") as mock_diff:
                with patch("plugin.embeddings.venv.embeddings_folder_maintain.mark_file_indexed_in_db") as mock_mark:
                    _incremental_refresh(str(tmp_path), "fake_model", [entry], hb, build_fts=False, build_vectors=False)
                    mock_diff.assert_not_called()
                    mock_mark.assert_not_called()
