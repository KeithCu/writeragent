from unittest.mock import patch

import pytest

from plugin.framework.config import LRU_MAX_ITEMS
from plugin.framework.model_lru import (
    _is_model_combobox_placeholder,
    drop_typed_partials,
    sanitize_model_combobox_value,
    lru_config_key,
    next_lru_list,
    next_model_lru_list,
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
        assert sanitize_model_combobox_value("gpt-4o") == "gpt-4o"
        assert sanitize_model_combobox_value("  claude-3-5-sonnet  ") == "claude-3-5-sonnet"
        assert sanitize_model_combobox_value("(Connection failed)") == ""
        assert sanitize_model_combobox_value("") == ""
        assert sanitize_model_combobox_value(None) == ""

    def test_lru_config_key(self):
        assert lru_config_key("model_lru", "http://localhost:11434") == "model_lru@http://localhost:11434"
        assert lru_config_key("prompt_lru", "") == "prompt_lru"

    def test_next_lru_list_skips_blank_and_head_and_promotes(self):
        assert next_lru_list(["a"], None) is None
        assert next_lru_list(["first"], "  ") is None
        assert next_lru_list(["first", "second"], "first") is None
        assert next_lru_list(["first", "second"], "second") == ["second", "first"]
        assert next_lru_list("", "new") == ["new"]
        full = [f"old{i}" for i in range(LRU_MAX_ITEMS)]
        assert next_lru_list(full, "new") == ["new"] + full[:LRU_MAX_ITEMS - 1]
        assert next_lru_list(["a", "b", "a"], "c") == ["c", "a", "b"]
        assert next_lru_list(["a", "b", "a"], "b") == ["b", "a"]

    def test_update_lru_history(self):
        config_data = {}

        def mock_get_config(key):
            return config_data.get(key, "")

        def mock_save(data):
            config_data.update(data)

        with patch("plugin.framework.model_lru.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.model_lru.save", side_effect=mock_save) as mock_set:
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

        def mock_save(data):
            config_data.update(data)

        with patch("plugin.framework.model_lru.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.model_lru.save", side_effect=mock_save):
            update_lru_history("item1", "model_lru", "http://localhost")
            assert config_data.get("model_lru@http://localhost") == ["item1"]
            update_lru_history("item2", "prompt_lru", "")
            assert config_data.get("prompt_lru") == ["item2"]
            for i in range(LRU_MAX_ITEMS + 2):
                update_lru_history(f"item{i}", "test_lru", "ep")
            assert config_data.get("test_lru@ep") == [f"item{i}" for i in range(LRU_MAX_ITEMS + 1, 1, -1)]
            update_lru_history("item2", "test_lru", "ep")
            assert config_data.get("test_lru@ep") == ["item2"] + [f"item{i}" for i in range(LRU_MAX_ITEMS + 1, 2, -1)]

    def test_update_lru_history_skips_when_list_unchanged(self):
        config_data = {"prompt_lru": ["first", "second"]}

        def mock_get_config(key):
            return config_data.get(key, "")

        def mock_save(data):
            config_data.update(data)

        with patch("plugin.framework.model_lru.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.model_lru.save", side_effect=mock_save) as mock_set:
            update_lru_history("first", "prompt_lru", "")
            mock_set.assert_not_called()

    def test_drop_typed_partials_collapses_keystroke_chains_keeps_real_ids(self):
        assert drop_typed_partials(
            ["claude-opus-5-5", "claude-opu-5-5", "claude-opus-5-", "claude-opus-5", "claude-opus-", "claude-opus"]
        ) == ["claude-opus-5-5"]
        assert drop_typed_partials(
            ["openai/gpt-oss-20b:nitro", "openai/gpt-oss-20b:nitr", "openai/gpt-oss-20b:nit", "openai/gpt-oss-20b"]
        ) == ["openai/gpt-oss-20b:nitro", "openai/gpt-oss-20b"]
        real_ids = ["gpt-4o-mini", "gpt-4o", "o3", "o1", "claude-opus-4-1", "claude-opus-4"]
        assert drop_typed_partials(real_ids) == real_ids

    def test_next_model_lru_list_cleans_even_when_head_unchanged(self):
        assert next_model_lru_list(
            ["claude-opus-5-5", "claude-opus-5-", "claude-opus-5"],
            "claude-opus-5-5",
        ) == ["claude-opus-5-5"]
        assert next_model_lru_list(["claude-opus-5-5", "gpt-4o"], "claude-opus-5-5") is None
        assert next_model_lru_list("not-a-list", "x") == ["x"]

    @pytest.mark.parametrize(
        "entries",
        [
            ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
            ["openai/gpt-oss-20b", "openai/gpt-oss-120b"],
            ["meta-llama/Llama-2-70b-chat-hf", "meta-llama/Llama-2-7b-chat-hf"],
            ["Qwen/Qwen3-32B", "Qwen/Qwen3-3B"],
            ["gpt-4o", "gpt-4"],
            ["gpt-4o-mini", "gpt-4o", "gpt-4"],
            ["openai/gpt-oss-120b", "x/y", "openai/gpt-oss-20b"],
        ],
    )
    def test_drop_typed_partials_keeps_real_models_one_char_apart(self, entries):
        assert drop_typed_partials(entries) == entries

    def test_drop_typed_partials_collapses_real_typing_run(self):
        assert drop_typed_partials(
            ["gpt-4o", "gpt-4", "gpt-", "gpt", "gp", "g", "claude-opus-4"]
        ) == ["gpt-4o", "claude-opus-4"]

    def test_next_model_lru_list_keeps_20b_120b_switch(self):
        lru = next_model_lru_list([], "openai/gpt-oss-20b")
        assert lru == ["openai/gpt-oss-20b"]
        lru = next_model_lru_list(lru, "openai/gpt-oss-120b")
        assert lru == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
        lru = next_model_lru_list(lru, "openai/gpt-oss-20b")
        assert lru == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]

    def test_next_model_lru_list_cleans_before_trim(self):
        cur = [
            "claude-opus-5-5",
            "claude-opus-5-",
            "claude-opus-5",
            "claude-opus-",
            "r1",
            "r2",
            "r3",
            "r4",
            "r5",
            "r6",
        ]
        assert next_model_lru_list(cur, "new") == [
            "new",
            "claude-opus-5-5",
            "r1",
            "r2",
            "r3",
            "r4",
            "r5",
            "r6",
        ]
