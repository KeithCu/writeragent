import sys
from plugin.framework.utils import get_plugin_dir
import os
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

# Add project root to path
sys.path.insert(0, os.path.dirname(get_plugin_dir()))

from plugin.framework.config import (
    get_image_model, set_image_model, get_api_key_for_endpoint, set_api_key_for_endpoint,
    update_lru_history, get_config, endpoint_url_suitable_for_v1_models_fetch,
)
from plugin.framework.event_bus import global_event_bus

class TestConfigSync(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}
        
        # Mock get_config and set_config (get_config no longer takes default; mock supplies "" when missing)
        def mock_get_config(ctx, key):
            return self.config_data.get(key, "")
        
        def mock_set_config(ctx, key, value):
            self.config_data[key] = value
            
        self.get_patcher = patch('plugin.framework.config.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.framework.config.set_config', side_effect=mock_set_config)
        
        self.mock_get = self.get_patcher.start()
        self.mock_set = self.set_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()

    def test_set_image_model_aihorde(self):
        self.config_data["image_provider"] = "aihorde"
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model(self.ctx, "new-horde-model")

            self.assertEqual(self.config_data.get("aihorde_model"), "new-horde-model")
            self.assertIsNone(self.config_data.get("image_model"))
            mock_emit.assert_called_once_with("config:changed", ctx=self.ctx)

    def test_set_image_model_endpoint(self):
        self.config_data["image_provider"] = "endpoint"
        with patch('plugin.framework.config.update_lru_history') as mock_lru, \
             patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model(self.ctx, "new-endpoint-model")
            
            self.assertEqual(self.config_data.get("image_model"), "new-endpoint-model")
            self.assertIsNone(self.config_data.get("aihorde_model"))
            mock_lru.assert_called_once_with(self.ctx, "new-endpoint-model", "image_model_lru", "")
            mock_emit.assert_called_once_with("config:changed", ctx=self.ctx)

    def test_update_lru_history_scoping(self):
        # Test with endpoint
        update_lru_history(self.ctx, "item1", "model_lru", "http://localhost")
        self.assertEqual(self.config_data.get("model_lru@http://localhost"), ["item1"])

        # Test without endpoint
        update_lru_history(self.ctx, "item2", "prompt_lru", "")
        self.assertEqual(self.config_data.get("prompt_lru"), ["item2"])

        # Test clamping to max_items and prepending
        for i in range(5):
            update_lru_history(self.ctx, f"item{i}", "test_lru", "ep", max_items=3)

        self.assertEqual(self.config_data.get("test_lru@ep"), ["item4", "item3", "item2"])

        # Test deduplication
        update_lru_history(self.ctx, "item2", "test_lru", "ep", max_items=3)
        self.assertEqual(self.config_data.get("test_lru@ep"), ["item2", "item4", "item3"])

    def test_get_image_model(self):
        # Test AI Horde
        self.config_data["image_provider"] = "aihorde"
        self.config_data["aihorde_model"] = "horde-1"
        self.assertEqual(get_image_model(self.ctx), "horde-1")
        
        # Test Endpoint
        self.config_data["image_provider"] = "endpoint"
        self.config_data["image_model"] = "end-1"
        self.assertEqual(get_image_model(self.ctx), "end-1")

    def test_get_api_key_for_endpoint_missing(self):
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://localhost:11434"), "")

    def test_get_api_key_for_endpoint_existing(self):
        self.config_data["api_keys_by_endpoint"] = {"http://localhost:11434": "test-key-123"}
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://localhost:11434"), "test-key-123")

        # Test endpoint normalization
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://localhost:11434/"), "test-key-123")

    def test_set_api_key_for_endpoint(self):
        # Starts empty
        set_api_key_for_endpoint(self.ctx, "http://localhost:11434", "new-key")
        self.assertEqual(self.config_data.get("api_keys_by_endpoint", {}).get("http://localhost:11434"), "new-key")

        # Updates existing, normalizes endpoint
        set_api_key_for_endpoint(self.ctx, "http://localhost:11434/", "updated-key")
        self.assertEqual(self.config_data.get("api_keys_by_endpoint", {}).get("http://localhost:11434"), "updated-key")

    def test_event_bus_listener_and_emit(self):
        called = []
        def my_callback(ctx=None, **kwargs):
            called.append(ctx)

        global_event_bus.subscribe("config:changed", my_callback)
        try:
            global_event_bus.emit("config:changed", ctx=self.ctx)
            self.assertEqual(len(called), 1)
            self.assertEqual(called[0], self.ctx)

            # Test exception swallowing (already tested in event_bus tests, but good to verify integration)
            def bad_callback(**kwargs):
                raise ValueError("Simulated error")
            global_event_bus.subscribe("config:changed", bad_callback)

            global_event_bus.emit("config:changed", ctx=self.ctx)
            self.assertEqual(len(called), 2) # First callback still gets called again
        finally:
            global_event_bus.unsubscribe("config:changed", my_callback)
            global_event_bus.unsubscribe("config:changed", bad_callback)


class TestEndpointUrlSuitableForModelFetch(unittest.TestCase):
    def test_incomplete_or_invalid_urls_rejected(self):
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch(""))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch("http:/"))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch("http://"))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch("ftp://api.openai.com"))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch("not-a-url"))

    def test_complete_urls_accepted(self):
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch("http://localhost:1234"))
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch("https://api.openai.com/v1"))
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch("http://127.0.0.1:11434"))
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch("http://[::1]:8080"))


class TestPopulateComboboxWithLruFetchOptions(unittest.TestCase):
    """populate_combobox_with_lru(skip_remote_fetch / remote_models) must not call fetch_available_models."""

    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}

        def mock_get_config(ctx, key):
            return self.config_data.get(key, "")

        def mock_set_config(ctx, key, value):
            self.config_data[key] = value

        self.get_patcher = patch("plugin.framework.config.get_config", side_effect=mock_get_config)
        self.set_patcher = patch("plugin.framework.config.set_config", side_effect=mock_set_config)
        self.get_patcher.start()
        self.set_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()

    def test_skip_remote_fetch_does_not_call_fetch(self):
        from plugin.framework.config import populate_combobox_with_lru

        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch("plugin.framework.config.fetch_available_models") as mock_fetch:
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                "",
                "model_lru",
                "http://localhost:8080",
                skip_remote_fetch=True,
            )
            mock_fetch.assert_not_called()

    def test_remote_models_does_not_call_fetch(self):
        from plugin.framework.config import populate_combobox_with_lru

        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch("plugin.framework.config.fetch_available_models") as mock_fetch:
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                "",
                "model_lru",
                "http://localhost:8080",
                remote_models=["m1", "m2"],
            )
            mock_fetch.assert_not_called()
            ctrl.addItems.assert_called()
            items = ctrl.addItems.call_args[0][0]
            self.assertIn("m1", items)
            self.assertIn("m2", items)

    def test_together_empty_lru_merges_default_text_model(self):
        """Massive providers skip /v1/models in populate_combobox_with_lru; defaults must still appear."""
        from plugin.framework.config import populate_combobox_with_lru

        self.config_data["model_lru@https://api.together.xyz"] = []
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch("plugin.framework.config.fetch_available_models") as mock_fetch:
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                "",
                "model_lru",
                "https://api.together.xyz",
                skip_remote_fetch=True,
            )
            mock_fetch.assert_not_called()
        ctrl.addItems.assert_called()
        items = ctrl.addItems.call_args[0][0]
        self.assertIn("openai/gpt-oss-120b", items)


class TestFetchAvailableModelsCache(unittest.TestCase):
    """_model_fetch_cache is process-wide; same normalized endpoint hits HTTP once."""

    def tearDown(self):
        import plugin.framework.config as cfg

        keys_to_del = [k for k in cfg._model_fetch_cache if "127.0.0.1:58901" in k or "127.0.0.1:58902" in k]
        for k in keys_to_del:
            del cfg._model_fetch_cache[k]

    def test_second_call_does_not_http(self):
        from plugin.framework import config as cfg

        with patch("plugin.framework.config.sync_request") as mock_sync:
            mock_sync.return_value = {"data": [{"id": "alpha"}]}
            r1 = cfg.fetch_available_models("http://127.0.0.1:58901")
            r2 = cfg.fetch_available_models("http://127.0.0.1:58901")
            self.assertEqual(r1, ["alpha"])
            self.assertEqual(r2, ["alpha"])
            self.assertEqual(mock_sync.call_count, 1)

    def test_normalized_url_shares_cache_entry(self):
        from plugin.framework import config as cfg

        with patch("plugin.framework.config.sync_request") as mock_sync:
            mock_sync.return_value = {"data": [{"id": "beta"}]}
            cfg.fetch_available_models("http://127.0.0.1:58902/")
            cfg.fetch_available_models("http://127.0.0.1:58902")
            self.assertEqual(mock_sync.call_count, 1)


class TestConfigSyncFileIO(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "writeragent.json")

        def mock_config_path(ctx):
            return self.config_path

        self.path_patcher = patch('plugin.framework.config._config_path', side_effect=mock_config_path)
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self.temp_dir.cleanup()
        # Clean up any leftover config mock state
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

        # Ensure we clear config memory if it was patched or imported
        from plugin.framework.config import get_config
        # clear any dict caches or internal references if they exist

    def test_set_api_key_file_io(self):
        # Ensure file is written correctly
        set_api_key_for_endpoint(self.ctx, "http://api.openai.com", "sk-1234")

        self.assertTrue(os.path.exists(self.config_path))
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("api_keys_by_endpoint", data)
        self.assertEqual(data["api_keys_by_endpoint"].get("http://api.openai.com"), "sk-1234")

        # Ensure we can read it back via actual config functions
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://api.openai.com"), "sk-1234")

    def test_get_api_key_file_io_missing_file(self):
        # Ensure file does not exist
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

        # Use a unique endpoint to avoid cross-test contamination if cache exists
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://api.missing.com"), "")

    def test_corrupt_config_file_io(self):
        # Write invalid JSON
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json ")

        # Should handle JSONDecodeError gracefully without crashing
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://api.openai.com"), "")

        # Write operation should overwrite corruption and recover the file
        set_api_key_for_endpoint(self.ctx, "http://api.openai.com", "sk-recovered")
        self.assertEqual(get_api_key_for_endpoint(self.ctx, "http://api.openai.com"), "sk-recovered")

        # Verify file is now valid JSON
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["api_keys_by_endpoint"]["http://api.openai.com"], "sk-recovered")

    def test_get_config_default_resolution(self):
        # Delete config file to ensure we hit default resolution logic
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

        from plugin.framework.errors import ConfigError

        # Test fallback to _CONFIG_DEFAULTS (keys that exist in schema/defaults)
        self.assertEqual(get_config(self.ctx, "calc_prompt_max_tokens"), 70)
        self.assertEqual(get_config(self.ctx, "chat_direct_image"), False)

        # LRU list keys missing from JSON default to [] (including endpoint-scoped keys)
        self.assertEqual(get_config(self.ctx, "prompt_lru"), [])
        self.assertEqual(get_config(self.ctx, "model_lru@http://127.0.0.1:5000"), [])

        # Test that unknown keys now raise ConfigError (strict schema enforcement)
        # Unknown string key -> raises
        with self.assertRaises(ConfigError):
            get_config(self.ctx, "unknown_key")

        # Keys ending in _lru but not in schema -> raises
        with self.assertRaises(ConfigError):
            get_config(self.ctx, "some_new_lru")

        # Keys containing by_endpoint but not in schema -> raises
        with self.assertRaises(ConfigError):
            get_config(self.ctx, "custom_by_endpoint")

        # Keys containing _map but not in schema -> raises
        with self.assertRaises(ConfigError):
            get_config(self.ctx, "some_custom_map")

if __name__ == '__main__':
    unittest.main()
