from unittest.mock import patch

from plugin.framework.constants import folder_search_enabled


def test_folder_search_disabled_by_default():
    with patch("plugin.framework.config.get_config", return_value=None):
        assert folder_search_enabled() is False


def test_folder_search_enabled_when_hybrid():
    with patch("plugin.framework.config.get_config", return_value="hybrid"):
        assert folder_search_enabled() is True


def test_folder_search_disabled_for_other_values():
    with patch("plugin.framework.config.get_config", return_value="embeddings"):
        assert folder_search_enabled() is False
