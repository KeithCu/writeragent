from unittest.mock import patch

from plugin.framework.model_lru import (
    _is_model_combobox_placeholder,
    _sanitize_model_combobox_value,
    lru_config_key,
    next_lru_list,
    update_lru_history,
)


class TestModelLru:

    def test_is_model_combobox_placeholder(self):
        assert not _is_model_combobox_placeholder("")
        assert not _is_model_combobox_placeholder(None)
        assert not _is_model_combobox_placeholder("gpt-4o")
        assert _is_model_combobox_placeholder("(Enter API Key to load models)")
        assert _is_model_combobox_placeholder("(Connection failed)")
        assert _is_model_combobox_placeholder("  (Default for current endpoint)  ")

    def test_sanitize_model_combobox_value(self):
        assert _sanitize_model_combobox_value("gpt-4o") == "gpt-4o"
        assert _sanitize_model_combobox_value("  claude-3-5-sonnet  ") == "claude-3-5-sonnet"
        assert _sanitize_model_combobox_value("(Connection failed)") == ""
        assert _sanitize_model_combobox_value("") == ""
        assert _sanitize_model_combobox_value(None) == ""

    def test_lru_config_key(self):
        assert lru_config_key("model_lru", "http://localhost:11434") == "model_lru@http://localhost:11434"
        assert lru_config_key("prompt_lru", "") == "prompt_lru"

    def test_next_lru_list_skips_blank_and_head_and_promotes(self):
        assert next_lru_list(["first"], "  ", 10) is None
        assert next_lru_list(["first", "second"], "first", 10) is None
        assert next_lru_list(["first", "second"], "second", 10) == ["second", "first"]
        assert next_lru_list("", "new", 10) == ["new"]
        assert next_lru_list(["a", "b", "c"], "d", 3) == ["d", "a", "b"]

    def test_update_lru_history(self):
        config_data = {}

        def mock_get_config(key):
            return config_data.get(key, "")

        def mock_set_config(key, value):
            config_data[key] = value

        with patch("plugin.framework.model_lru.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.model_lru.set_config", side_effect=mock_set_config) as mock_set:
            update_lru_history("m1", "model_lru", "http://localhost")
            assert config_data["model_lru@http://localhost"] == ["m1"]

            # Head item is no-op
            mock_set.reset_mock()
            update_lru_history("m1", "model_lru", "http://localhost")
            mock_set.assert_not_called()

    def test_update_lru_history_scoping(self):
        config_data = {}

        def mock_get_config(key):
            return config_data.get(key, "")

        def mock_set_config(key, value):
            config_data[key] = value

        with patch("plugin.framework.model_lru.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.model_lru.set_config", side_effect=mock_set_config):
            update_lru_history("item1", "model_lru", "http://localhost")
            assert config_data.get("model_lru@http://localhost") == ["item1"]
            update_lru_history("item2", "prompt_lru", "")
            assert config_data.get("prompt_lru") == ["item2"]
            for i in range(5):
                update_lru_history(f"item{i}", "test_lru", "ep", max_items=3)
            assert config_data.get("test_lru@ep") == ["item4", "item3", "item2"]
            update_lru_history("item2", "test_lru", "ep", max_items=3)
            assert config_data.get("test_lru@ep") == ["item2", "item4", "item3"]

    def test_update_lru_history_skips_when_list_unchanged(self):
        config_data = {"prompt_lru": ["first", "second"]}

        def mock_get_config(key):
            return config_data.get(key, "")

        def mock_set_config(key, value):
            config_data[key] = value

        with patch("plugin.framework.model_lru.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.model_lru.set_config", side_effect=mock_set_config) as mock_set:
            update_lru_history("first", "prompt_lru", "")
            mock_set.assert_not_called()
