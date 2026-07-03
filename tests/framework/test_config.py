import json
import os
import tempfile
import unittest
from unittest.mock import ANY, MagicMock, patch

from plugin.framework.config import (
    CONFIG_BACKUP_SUFFIX,
    get_api_key_for_endpoint,
    set_api_key_for_endpoint,
    get_config,
    get_config_bool,
    get_config_int,
    set_config,
)
from plugin.framework.errors import ConfigError
from plugin.framework.client.model_fetcher import get_image_model, get_text_model, set_image_model, set_text_model
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

        def mock_get_config(key):
            return self.config_data.get(key, '')

        def mock_set_config(key, value):
            self.config_data[key] = value

        self.get_patcher = patch('plugin.framework.config.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.framework.config.set_config', side_effect=mock_set_config)
        self.get_mf_patcher = patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config)
        self.set_mf_patcher = patch('plugin.framework.client.model_fetcher.set_config', side_effect=mock_set_config)
        self.mock_get = self.get_patcher.start()
        self.mock_set = self.set_patcher.start()
        self.get_mf_patcher.start()
        self.mock_mf_set = self.set_mf_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()
        self.get_mf_patcher.stop()
        self.set_mf_patcher.stop()

    def test_set_text_model_writes_and_lru(self):
        self.config_data['text_model'] = ''
        with patch('plugin.chatbot.config_ui_helpers.update_lru_history') as mock_lru, patch.object(global_event_bus, 'emit') as mock_emit:
            set_text_model('new-chat-model')
            self.assertEqual(self.config_data.get('text_model'), 'new-chat-model')
            mock_lru.assert_called_once_with('new-chat-model', 'model_lru', '')
            mock_emit.assert_not_called()

    def test_set_text_model_skips_when_unchanged(self):
        self.config_data['text_model'] = 'same-model'
        self.mock_mf_set.reset_mock()
        set_text_model('same-model')
        self.mock_mf_set.assert_not_called()

    def test_set_text_model_update_lru_false(self):
        self.config_data['text_model'] = ''
        with patch('plugin.chatbot.config_ui_helpers.update_lru_history') as mock_lru:
            set_text_model('chat-only', update_lru=False)
            self.assertEqual(self.config_data.get('text_model'), 'chat-only')
            mock_lru.assert_not_called()

    def test_get_text_model_ignores_legacy_model_key(self):
        """Legacy top-level ``model`` in writeragent.json is no longer read."""
        self.config_data['model'] = 'legacy-model'
        self.config_data['text_model'] = ''
        with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value='http://localhost:11434'), \
             patch('plugin.framework.client.model_fetcher.get_provider_from_endpoint', return_value='ollama'), \
             patch('plugin.framework.client.model_fetcher.get_provider_defaults', return_value={'text_model': 'default-model'}):
            self.assertEqual(get_text_model(), 'default-model')
        self.assertEqual(self.config_data.get('text_model'), '')
        self.assertEqual(self.config_data.get('model'), 'legacy-model')

    def test_model_lru_endpoint_isolation(self):
        endpoint_a = 'http://localhost:11434'
        endpoint_b = 'http://localhost:8080'
        self.config_data[f'model_lru@{endpoint_b}'] = ['other-model']
        with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value=endpoint_a), \
             patch('plugin.chatbot.config_ui_helpers.get_config', side_effect=lambda k: self.config_data.get(k, '')), \
             patch('plugin.chatbot.config_ui_helpers.set_config', side_effect=lambda k, v: self.config_data.__setitem__(k, v)), \
             patch('plugin.chatbot.config_ui_helpers.get_current_endpoint', return_value=endpoint_a):
            set_text_model('model-on-a', update_lru=True)
        self.assertEqual(self.config_data.get(f'model_lru@{endpoint_a}'), ['model-on-a'])
        self.assertEqual(self.config_data.get(f'model_lru@{endpoint_b}'), ['other-model'])

    def test_set_image_model_endpoint(self):
        self.config_data['image_model'] = ''
        with patch('plugin.chatbot.config_ui_helpers.update_lru_history') as mock_lru, patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model('new-endpoint-model')
            self.assertEqual(self.config_data.get('image_model'), 'new-endpoint-model')
            mock_lru.assert_called_once_with('new-endpoint-model', 'image_model_lru', '')
            mock_emit.assert_not_called()

    def test_set_image_model_skips_when_unchanged(self):
        self.config_data['image_model'] = 'same-model'
        self.mock_set.reset_mock()
        set_image_model('same-model')
        self.mock_set.assert_not_called()

    def test_get_image_model(self):
        self.config_data['image_model'] = 'end-1'
        self.assertEqual(get_image_model(), 'end-1')

    def test_get_api_key_for_endpoint_missing(self):
        self.assertEqual(get_api_key_for_endpoint('http://localhost:11434'), '')

    def test_get_api_key_for_endpoint_existing(self):
        self.config_data['api_keys_by_endpoint'] = {'http://localhost:11434': 'test-key-123'}
        self.assertEqual(get_api_key_for_endpoint('http://localhost:11434'), 'test-key-123')
        self.assertEqual(get_api_key_for_endpoint('http://localhost:11434/'), 'test-key-123')

    def test_set_api_key_for_endpoint(self):
        set_api_key_for_endpoint('http://localhost:11434', 'new-key')
        self.assertEqual(self.config_data.get('api_keys_by_endpoint', {}).get('http://localhost:11434'), 'new-key')
        set_api_key_for_endpoint('http://localhost:11434/', 'updated-key')
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

        def mock_config_path():
            return self.config_path
        self.path_patcher = patch('plugin.framework.config._config_path', side_effect=mock_config_path)
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self.temp_dir.cleanup()
        backup_path = self.config_path + CONFIG_BACKUP_SUFFIX
        if os.path.exists(backup_path):
            os.remove(backup_path)
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def _reset_config_cache(self):
        import plugin.framework.config as cfg

        cfg._cache.data = None
        cfg._cache.mtime = 0
        cfg._cache.mtime_last_checked = 0.0

    def _backup_path(self):
        return self.config_path + CONFIG_BACKUP_SUFFIX

    def test_set_api_key_file_io(self):
        set_api_key_for_endpoint('http://api.openai.com', 'sk-1234')
        self.assertTrue(os.path.exists(self.config_path))
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn('api_keys_by_endpoint', data)
        self.assertEqual(data['api_keys_by_endpoint'].get('http://api.openai.com'), 'sk-1234')
        self.assertEqual(get_api_key_for_endpoint('http://api.openai.com'), 'sk-1234')

    def test_get_api_key_file_io_missing_file(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        self.assertEqual(get_api_key_for_endpoint('http://api.missing.com'), '')

    def test_corrupt_config_file_io(self):
        corrupt = '{ invalid json '
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        self._reset_config_cache()
        self.assertEqual(get_api_key_for_endpoint('http://api.openai.com'), '')
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), corrupt)
        set_api_key_for_endpoint('http://api.openai.com', 'sk-recovered')
        self.assertEqual(get_api_key_for_endpoint('http://api.openai.com'), 'sk-recovered')
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['api_keys_by_endpoint']['http://api.openai.com'], 'sk-recovered')
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), corrupt)

    def test_config_trailing_comma_auto_repair(self):
        broken = '{"text_model": "gpt",}'
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(broken)
        self._reset_config_cache()
        self.assertEqual(get_config('text_model'), 'gpt')
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), broken)
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['text_model'], 'gpt')

    def test_config_read_creates_backup_on_failure(self):
        corrupt = '{ invalid json '
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        self._reset_config_cache()
        self.assertEqual(get_config('calc_prompt_max_tokens'), 70)
        self.assertTrue(os.path.exists(self._backup_path()))
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), corrupt)

    def test_valid_config_no_backup_on_set(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        self._reset_config_cache()
        set_config('text_model', 'other')
        self.assertFalse(os.path.exists(self._backup_path()))
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f)['text_model'], 'other')

    def test_get_config_default_resolution(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        from plugin.framework.errors import ConfigError
        self.assertEqual(get_config('calc_prompt_max_tokens'), 70)
        self.assertEqual(get_config('prompt_lru'), [])
        self.assertEqual(get_config('endpoint'), 'http://localhost:11434')
        self.assertEqual(get_config('model_lru@http://localhost:11434'), [])
        self.assertEqual(get_config_int('extension_update_check_epoch'), 0)
        # Module-yaml keys (no WriterAgentConfig dataclass field; defaults from MODULES schema)
        self.assertEqual(get_config_int('web_cache_max_mb'), 50)
        self.assertEqual(get_config_int('web_cache_validity_days'), 30)
        self.assertEqual(get_config_int('extend_selection_max_tokens'), 1000)
        self.assertEqual(get_config_bool('chatbot.show_search_thinking'), False)
        self.assertEqual(get_config_bool('web_research_cache_enabled'), False)
        self.assertEqual(get_config('embeddings.folder_search_mode'), 'none')
        self.assertEqual(get_config_int('web_research_cache_jaccard_percent'), 60)
        self.assertEqual(get_config_int('web_research_cache_embedding_percent'), 75)
        self.assertEqual(get_config_int('web_research_cache_min_overlap'), 8)
        self.assertEqual(get_config('log_level'), 'DEBUG')
        with self.assertRaises(ConfigError) as err_ctx:
            get_config('unknown_key')
        self.assertEqual(err_ctx.exception.details.get('key'), 'unknown_key')
        self.assertIn('unknown_key', str(err_ctx.exception))
        with self.assertRaises(ConfigError):
            get_config('some_new_lru')
        with self.assertRaises(ConfigError):
            get_config('custom_by_endpoint')
        with self.assertRaises(ConfigError):
            get_config('some_custom_map')

    def test_set_config_skips_identical_value(self):
        import plugin.framework.config as cfg
        cfg._cached_config_dict = None
        cfg._cached_config_mtime = 0
        cfg._cached_config_mtime_last_checked = 0.0
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config('text_model', 'gpt')
            mock_emit.assert_not_called()
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config('text_model', 'other')
            mock_emit.assert_called_once()  # ctx from _emit_config_changed_ctx
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

    def test_config_validation_constraints(self):
        from plugin.framework.config import WriterAgentConfig
        from plugin.framework.errors import ConfigValidationError

        # temperature > 1.0
        config = WriterAgentConfig.from_dict({"temperature": 1.5})
        with self.assertRaises(ConfigValidationError) as ctx:
            config.validate()
        self.assertEqual(ctx.exception.code, "INVALID_TEMPERATURE")

        # chat_max_tokens < 0
        config = WriterAgentConfig.from_dict({"chat_max_tokens": -5})
        with self.assertRaises(ConfigValidationError) as ctx:
            config.validate()
        self.assertEqual(ctx.exception.code, "INVALID_CHAT_MAX_TOKENS")

        # request_timeout <= 0
        config = WriterAgentConfig.from_dict({"request_timeout": 0})
        with self.assertRaises(ConfigValidationError) as ctx:
            config.validate()
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST_TIMEOUT")

        # endpoint preset resolution
        config = WriterAgentConfig.from_dict({"endpoint": "OpenRouter"})
        config.validate()
        self.assertEqual(config.endpoint, "https://openrouter.ai/api")
