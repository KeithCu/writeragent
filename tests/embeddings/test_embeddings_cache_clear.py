from unittest.mock import patch, MagicMock
from plugin.embeddings.embeddings_cache import clear_folder_cache

@patch("plugin.embeddings.embeddings_cache._remove_path")
@patch("plugin.embeddings.embeddings_cache.folder_cache_dir")
@patch("plugin.embeddings.embeddings_cache.remove_stale_corpus_stores")
@patch("plugin.embeddings.venv.embeddings_zvec.zvec_clear_cache")
def test_clear_folder_cache_clears_zvec_cache(mock_zvec_clear_cache, mock_remove_stale, mock_folder_cache_dir, mock_remove_path):
    mock_base = MagicMock()
    mock_folder_cache_dir.return_value = mock_base

    # Simulate Path / operator returning a string-like or Path-like object
    mock_base.__truediv__.return_value = "mock_path_child"

    clear_folder_cache("/dummy/root")

    # Assert zvec_clear_cache was called with the correct path
    mock_zvec_clear_cache.assert_called_once_with("mock_path_child")
