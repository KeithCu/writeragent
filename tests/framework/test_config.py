import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from plugin.framework.config import get_api_key_for_endpoint, set_api_key_for_endpoint, get_config, get_config_bool, get_config_int, set_config
from plugin.framework.client.model_fetcher import get_image_model, set_image_model
from plugin.framework.event_bus import global_event_bus
from plugin.tests.testing_utils import setup_uno_mocks
from plugin.framework.constants import get_plugin_dir
import sys

setup_uno_mocks()
sys.path.insert(0, os.path.dirname(get_plugin_dir()))

class TestConfigSync(unittest.TestCase):

    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}

        def mock_get_config(ctx, key):
            return self.config_data.get(key, '')

        def mock_set_config(ctx, key, value):
            self.config_data[key] = value
        self.get_patcher = patch('plugin.framework.config.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.framework.config.set_config', side_effect=mock_set_config)
        self.get_mf_patcher = patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config)
        self.set_mf_patcher = patch('plugin.framework.client.model_fetcher.set_config', side_effect=mock_set_config)
        self.mock_get = self.get_patcher.start()
        self.mock_set = self.set_patcher.start()
        self.get_mf_patcher.start()
        self.set_mf_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()
        self.get_mf_patcher.stop()
        self.set_mf_patcher.stop()

    def test_set_image_model_aihorde(self):
        self.config_data['image_provider'] = 'aihorde'
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model(self.ctx, 'new-horde-model')
            self.assertEqual(self.config_data.get('aihorde_model'), 'new-horde-model')
            self.assertIsNone(self.config_data.get('image_model'))
            mock_emit.assert_not_called()

    def test_set_image_model_endpoint(self):
        self.config_data['image_provider'] = 'endpoint'
        with patch('plugin.chatbot.config_ui_helpers.update_lru_history') as mock_lru, patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model(self.ctx, 'new-endpoint-model')
            self.assertEqual(self.config_data.get('image_model'), 'new-endpoint-model')
            self.assertIsNone(self.config_data.get('aihorde_model'))
            mock_lru.assert_called_once_with(self.ctx, 'new-endpoint-model', 'image_model_lru', '')
            mock_emit.assert_not_called()

    def test_set_image_model_skips_when_unchanged(self):
        self.config_data['image_provider'] = 'endpoint'
        self.config_data['image_model'] = 'same-model'
        self.mock_set.reset_mock()
        set_image_model(self.ctx, 'same-model')
        self.mock_set.assert_not_called()

    def test_get_image_model(self):
        self.config_data['image_provider'] = 'aihorde'
        self.config_data['aihorde_model'] = 'horde-1'
        self.assertEqual(get_image_model(self.ctx), 'horde-1')
        self.config_data['image_provider'] = 'endpoint'
        self.config_data['image_model'] = 'end-1'
        self.assertEqual(get_image_model(self.ctx), 'end-1')

    def test_get_api_key_for_endpoint_missing(self):
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://localhost:11434'), '')

    def test_get_api_key_for_endpoint_existing(self):
        self.config_data['api_keys_by_endpoint'] = {'http://localhost:11434': 'test-key-123'}
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://localhost:11434'), 'test-key-123')
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://localhost:11434/'), 'test-key-123')

    def test_set_api_key_for_endpoint(self):
        set_api_key_for_endpoint(self.ctx, 'http://localhost:11434', 'new-key')
        self.assertEqual(self.config_data.get('api_keys_by_endpoint', {}).get('http://localhost:11434'), 'new-key')
        set_api_key_for_endpoint(self.ctx, 'http://localhost:11434/', 'updated-key')
        self.assertEqual(self.config_data.get('api_keys_by_endpoint', {}).get('http://localhost:11434'), 'updated-key')

    def test_event_bus_listener_and_emit(self):
        called = []

        def my_callback(ctx=None, **kwargs):
            called.append(ctx)
        global_event_bus.subscribe('config:changed', my_callback)
        try:
            global_event_bus.emit('config:changed', ctx=self.ctx)
            self.assertEqual(len(called), 1)
            self.assertEqual(called[0], self.ctx)

            def bad_callback(**kwargs):
                raise ValueError('Simulated error')
            global_event_bus.subscribe('config:changed', bad_callback)
            global_event_bus.emit('config:changed', ctx=self.ctx)
            self.assertEqual(len(called), 2)
        finally:
            global_event_bus.unsubscribe('config:changed', my_callback)
            global_event_bus.unsubscribe('config:changed', bad_callback)


class TestConfigSyncFileIO(unittest.TestCase):

    def setUp(self):
        self.ctx = MagicMock()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, 'writeragent.json')

        def mock_config_path(ctx):
            return self.config_path
        self.path_patcher = patch('plugin.framework.config._config_path', side_effect=mock_config_path)
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self.temp_dir.cleanup()
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def test_set_api_key_file_io(self):
        set_api_key_for_endpoint(self.ctx, 'http://api.openai.com', 'sk-1234')
        self.assertTrue(os.path.exists(self.config_path))
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn('api_keys_by_endpoint', data)
        self.assertEqual(data['api_keys_by_endpoint'].get('http://api.openai.com'), 'sk-1234')
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://api.openai.com'), 'sk-1234')

    def test_get_api_key_file_io_missing_file(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://api.missing.com'), '')

    def test_corrupt_config_file_io(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write('{ invalid json ')
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://api.openai.com'), '')
        set_api_key_for_endpoint(self.ctx, 'http://api.openai.com', 'sk-recovered')
        self.assertEqual(get_api_key_for_endpoint(self.ctx, 'http://api.openai.com'), 'sk-recovered')
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['api_keys_by_endpoint']['http://api.openai.com'], 'sk-recovered')

    def test_get_config_default_resolution(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        from plugin.framework.errors import ConfigError
        self.assertEqual(get_config(self.ctx, 'calc_prompt_max_tokens'), 70)
        self.assertEqual(get_config(self.ctx, 'chat_direct_image'), False)
        self.assertEqual(get_config(self.ctx, 'prompt_lru'), [])
        self.assertEqual(get_config(self.ctx, 'model_lru@http://127.0.0.1:5000'), [])
        self.assertEqual(get_config_int(self.ctx, 'extension_update_check_epoch'), 0)
        # Module-yaml keys (no WriterAgentConfig dataclass field; defaults from MODULES schema)
        self.assertEqual(get_config_int(self.ctx, 'web_cache_max_mb'), 50)
        self.assertEqual(get_config_int(self.ctx, 'web_cache_validity_days'), 30)
        self.assertEqual(get_config_int(self.ctx, 'extend_selection_max_tokens'), 1000)
        self.assertEqual(get_config_bool(self.ctx, 'chatbot.show_search_thinking'), False)
        self.assertEqual(get_config_bool(self.ctx, 'web_research_cache_enabled'), True)
        self.assertEqual(get_config(self.ctx, 'embeddings.folder_search_mode'), 'none')
        self.assertEqual(get_config_int(self.ctx, 'web_research_cache_jaccard_percent'), 40)
        self.assertEqual(get_config_int(self.ctx, 'web_research_cache_min_overlap'), 8)
        self.assertEqual(get_config(self.ctx, 'log_level'), 'DEBUG')
        with self.assertRaises(ConfigError) as err_ctx:
            get_config(self.ctx, 'unknown_key')
        self.assertEqual(err_ctx.exception.details.get('key'), 'unknown_key')
        self.assertIn('unknown_key', str(err_ctx.exception))
        with self.assertRaises(ConfigError):
            get_config(self.ctx, 'some_new_lru')
        with self.assertRaises(ConfigError):
            get_config(self.ctx, 'custom_by_endpoint')
        with self.assertRaises(ConfigError):
            get_config(self.ctx, 'some_custom_map')

    def test_set_config_skips_identical_value(self):
        import plugin.framework.config as cfg
        cfg._cached_config_dict = None
        cfg._cached_config_mtime = 0
        cfg._cached_config_mtime_last_checked = 0.0
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config(self.ctx, 'text_model', 'gpt')
            mock_emit.assert_not_called()
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config(self.ctx, 'text_model', 'other')
            mock_emit.assert_called_once_with('config:changed', ctx=self.ctx)
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data.get('text_model'), 'other')


class TestRobustNumericParsing(unittest.TestCase):

    def test_parse_int_robust(self):
        from plugin.framework.config import parse_int_robust

        # Test standard integers
        self.assertEqual(parse_int_robust(8765), 8765)
        self.assertEqual(parse_int_robust(0), 0)
        self.assertEqual(parse_int_robust(-42), -42)

        # Test standard floats
        self.assertEqual(parse_int_robust(8765.0), 8765)
        self.assertEqual(parse_int_robust(8765.99), 8765)

        # Test string integers
        self.assertEqual(parse_int_robust("8765"), 8765)
        self.assertEqual(parse_int_robust(" 8765 "), 8765)

        # Test string floats
        self.assertEqual(parse_int_robust("8765.0"), 8765)
        self.assertEqual(parse_int_robust("8765.00"), 8765)
        self.assertEqual(parse_int_robust("8765.7"), 8765)

        # Test European decimal commas (like German locale)
        self.assertEqual(parse_int_robust("8765,0"), 8765)
        self.assertEqual(parse_int_robust("8765,00"), 8765)
        self.assertEqual(parse_int_robust("8765,5"), 8765)

        # Test invalid inputs raise ValueError
        with self.assertRaises(ValueError):
            parse_int_robust(None)
        with self.assertRaises(ValueError):
            parse_int_robust("")
        with self.assertRaises(ValueError):
            parse_int_robust("   ")
        with self.assertRaises(ValueError):
            parse_int_robust("invalid")

    def test_parse_float_robust(self):
        from plugin.framework.config import parse_float_robust

        # Test standard floats
        self.assertEqual(parse_float_robust(7.5), 7.5)
        self.assertEqual(parse_float_robust(0.0), 0.0)

        # Test standard integers
        self.assertEqual(parse_float_robust(7), 7.0)

        # Test string floats
        self.assertEqual(parse_float_robust("7.5"), 7.5)
        self.assertEqual(parse_float_robust(" 7.5 "), 7.5)

        # Test European decimal commas
        self.assertEqual(parse_float_robust("7,5"), 7.5)
        self.assertEqual(parse_float_robust("0,25"), 0.25)

        # Test invalid inputs raise ValueError
        with self.assertRaises(ValueError):
            parse_float_robust(None)
        with self.assertRaises(ValueError):
            parse_float_robust("")
        with self.assertRaises(ValueError):
            parse_float_robust("   ")
        with self.assertRaises(ValueError):
            parse_float_robust("invalid")

    def test_config_validate_type_casting(self):
        from plugin.framework.config import WriterAgentConfig

        # Test standard dataclass type casting
        config = WriterAgentConfig.from_dict({
            "temperature": "0,7",  # String with European decimal comma
            "chat_max_tokens": 16384.0,  # Float instead of int
            "image_steps": "30",  # String int
        })
        config.validate()

        self.assertEqual(config.temperature, 0.7)
        self.assertEqual(config.chat_max_tokens, 16384)
        self.assertEqual(config.image_steps, 30)

        # Test _extra_config dynamic YAML schema type casting (e.g. mcp.mcp_port)
        # First let's patch MODULES to contain a mock module schema
        mock_modules = [{
            "name": "mcp",
            "config": {
                "mcp_port": {
                    "type": "int",
                    "default": 8765
                },
                "mcp_host": {
                    "type": "string",
                    "default": "localhost"
                }
            }
        }]
        with patch("plugin.framework.config.MODULES", mock_modules):
            config_with_extra = WriterAgentConfig.from_dict({
                "mcp.mcp_port": "8765,00",  # German locale format
            })
            config_with_extra.validate()

            self.assertEqual(config_with_extra._extra_config.get("mcp.mcp_port"), 8765)

    def test_yaml_backed_key_extra_config_type_casting(self):
        from plugin.framework.config import WriterAgentConfig

        config = WriterAgentConfig.from_dict({"web_cache_max_mb": "50,0"})
        config.validate()
        self.assertEqual(config._extra_config.get("web_cache_max_mb"), 50)

    def test_chat_sidebar_mode_validation(self):
        from plugin.framework.config import WriterAgentConfig

        # Valid modes
        for mode in ("chat", "image", "web_research", "brainstorming", "writing_plan"):
            config = WriterAgentConfig.from_dict({"chat_sidebar_mode": mode})
            config.validate()
            self.assertEqual(config.chat_sidebar_mode, mode)

        # Invalid mode resets to chat
        config = WriterAgentConfig.from_dict({"chat_sidebar_mode": "invalid_mode"})
        config.validate()
        self.assertEqual(config.chat_sidebar_mode, "chat")
