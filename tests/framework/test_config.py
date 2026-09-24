import pytest
import json
import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

from plugin.framework.config import (
    CONFIG_BACKUP_SUFFIX,
    CONFIG_SCHEMA_COMMENT,
    CONFIG_SCHEMA_DOC_URL,
    get_api_key_for_endpoint,
    set_api_key_for_endpoint,
    get_config,
    get_config_bool,
    get_config_float,
    get_config_int,
    parse_config_json_text,
    reset_config_for_tests,
    set_config,
)
from plugin.framework.errors import ConfigError
from plugin.framework.client.model_fetcher import get_image_model, get_text_model, set_image_model, set_text_model
from plugin.framework.event_bus import global_event_bus
from plugin.framework.constants import get_plugin_dir
import sys

sys.path.insert(0, os.path.dirname(get_plugin_dir()))

class TestConfigSync:

    def setup_method(self):
        reset_config_for_tests()
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

    def teardown_method(self):
        self.get_patcher.stop()
        self.set_patcher.stop()
        self.get_mf_patcher.stop()
        self.set_mf_patcher.stop()
        reset_config_for_tests()


    def test_set_text_model_writes_and_lru(self):
        self.config_data['text_model'] = ''
        with patch('plugin.chatbot.config_ui_helpers.update_lru_history') as mock_lru, patch.object(global_event_bus, 'emit') as mock_emit:
            set_text_model('new-chat-model')
            assert (self.config_data.get('text_model')) == ('new-chat-model')
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
            assert (self.config_data.get('text_model')) == ('chat-only')
            mock_lru.assert_not_called()

    def test_get_text_model_ignores_legacy_model_key(self):
        """Legacy top-level ``model`` in writeragent.json is no longer read."""
        self.config_data['model'] = 'legacy-model'
        self.config_data['text_model'] = ''
        with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value='http://localhost:11434'), \
             patch('plugin.framework.client.model_fetcher.get_provider_from_endpoint', return_value='ollama'), \
             patch('plugin.framework.client.model_fetcher.get_provider_defaults', return_value={'text_model': 'default-model'}):
            assert (get_text_model()) == ('default-model')
        assert (self.config_data.get('text_model')) == ('')
        assert (self.config_data.get('model')) == ('legacy-model')

    def test_model_lru_endpoint_isolation(self):
        endpoint_a = 'http://localhost:11434'
        endpoint_b = 'http://localhost:8080'
        self.config_data[f'model_lru@{endpoint_b}'] = ['other-model']
        with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value=endpoint_a), \
             patch('plugin.chatbot.config_ui_helpers.get_config', side_effect=lambda k: self.config_data.get(k, '')), \
             patch('plugin.chatbot.config_ui_helpers.set_config', side_effect=lambda k, v: self.config_data.__setitem__(k, v)), \
             patch('plugin.chatbot.config_ui_helpers.get_current_endpoint', return_value=endpoint_a):
            set_text_model('model-on-a', update_lru=True)
        assert (self.config_data.get(f'model_lru@{endpoint_a}')) == (['model-on-a'])
        assert (self.config_data.get(f'model_lru@{endpoint_b}')) == (['other-model'])

    def test_set_image_model_endpoint(self):
        self.config_data['image_model'] = ''
        with patch('plugin.chatbot.config_ui_helpers.update_lru_history') as mock_lru, patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model('new-endpoint-model')
            assert (self.config_data.get('image_model')) == ('new-endpoint-model')
            mock_lru.assert_called_once_with('new-endpoint-model', 'image_model_lru', '')
            mock_emit.assert_not_called()

    def test_set_image_model_skips_when_unchanged(self):
        self.config_data['image_model'] = 'same-model'
        self.mock_set.reset_mock()
        set_image_model('same-model')
        self.mock_set.assert_not_called()

    def test_get_image_model(self):
        self.config_data['image_model'] = 'end-1'
        assert (get_image_model()) == ('end-1')

    def test_get_api_key_for_endpoint_missing(self):
        assert (get_api_key_for_endpoint('http://localhost:11434')) == ('')

    def test_get_api_key_for_endpoint_existing(self):
        self.config_data['api_keys_by_endpoint'] = {'http://localhost:11434': 'test-key-123'}
        assert (get_api_key_for_endpoint('http://localhost:11434')) == ('test-key-123')
        assert (get_api_key_for_endpoint('http://localhost:11434/')) == ('test-key-123')

    def test_set_api_key_for_endpoint(self):
        set_api_key_for_endpoint('http://localhost:11434', 'new-key')
        assert (self.config_data.get('api_keys_by_endpoint', {}).get('http://localhost:11434')) == ('new-key')
        set_api_key_for_endpoint('http://localhost:11434/', 'updated-key')
        assert (self.config_data.get('api_keys_by_endpoint', {}).get('http://localhost:11434')) == ('updated-key')

    def test_event_bus_listener_and_emit(self):
        called = []

        def my_callback(ctx=None, **kwargs):
            called.append(ctx)
        global_event_bus.subscribe('config:changed', my_callback)
        try:
            global_event_bus.emit('config:changed', ctx=self.ctx)
            assert (len(called)) == (1)
            assert (called[0]) == (self.ctx)

            def bad_callback(**kwargs):
                raise ValueError('Simulated error')
            global_event_bus.subscribe('config:changed', bad_callback)
            global_event_bus.emit('config:changed', ctx=self.ctx)
            assert (len(called)) == (2)
        finally:
            global_event_bus.unsubscribe('config:changed', my_callback)
            global_event_bus.unsubscribe('config:changed', bad_callback)


class TestConfigSyncFileIO:

    def setup_method(self):
        reset_config_for_tests()
        self.ctx = MagicMock()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, 'writeragent.json')

        def mock_config_path():
            return self.config_path
        self.path_patcher = patch('plugin.framework.config._config_path', side_effect=mock_config_path)
        self.path_patcher.start()

    def teardown_method(self):
        reset_config_for_tests()
        self.path_patcher.stop()
        self.temp_dir.cleanup()
        backup_path = self.config_path + CONFIG_BACKUP_SUFFIX
        if os.path.exists(backup_path):
            os.remove(backup_path)
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def _backup_path(self):
        return self.config_path + CONFIG_BACKUP_SUFFIX

    def _load_written(self):
        with open(self.config_path, 'r', encoding='utf-8') as f:
            text = f.read()
        data = parse_config_json_text(text)
        assert isinstance(data, dict), text[:300]
        return data

    def test_set_api_key_file_io(self):
        set_api_key_for_endpoint('http://api.openai.com', 'sk-1234')
        assert (os.path.exists(self.config_path))
        data = self._load_written()
        assert ('api_keys_by_endpoint') in (data)
        assert (data['api_keys_by_endpoint'].get('http://api.openai.com')) == ('sk-1234')
        assert (get_api_key_for_endpoint('http://api.openai.com')) == ('sk-1234')

    def test_get_api_key_file_io_missing_file(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        assert (get_api_key_for_endpoint('http://api.missing.com')) == ('')

    def test_corrupt_config_file_io(self):
        corrupt = '{ invalid json '
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        reset_config_for_tests()
        assert (get_api_key_for_endpoint('http://api.openai.com')) == ('')
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            assert (f.read()) == (corrupt)
        set_api_key_for_endpoint('http://api.openai.com', 'sk-recovered')
        assert (get_api_key_for_endpoint('http://api.openai.com')) == ('sk-recovered')
        data = self._load_written()
        assert (data['api_keys_by_endpoint']['http://api.openai.com']) == ('sk-recovered')
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            assert (f.read()) == (corrupt)

    def test_config_trailing_comma_auto_repair(self):
        broken = '{"text_model": "gpt",}'
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(broken)
        reset_config_for_tests()
        assert (get_config('text_model')) == ('gpt')
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            assert (f.read()) == (broken)
        data = self._load_written()
        assert (data['text_model']) == ('gpt')

    def test_get_config_repair_persist_does_not_drop_concurrent_set(self):
        """GET-path repair persist must not rewrite over a concurrent set_config."""
        broken = '{"text_model": "gpt",}'
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(broken)
        reset_config_for_tests()
        barrier = threading.Barrier(2)
        errors = []

        def reader():
            try:
                barrier.wait(timeout=5)
                for unused in range(30):
                    get_config("text_model")
            except Exception as exc:
                errors.append(exc)

        def writer():
            try:
                barrier.wait(timeout=5)
                set_config("image_model", "flux-keep")
            except Exception as exc:
                errors.append(exc)

        t_read = threading.Thread(target=reader)
        t_write = threading.Thread(target=writer)
        t_read.start()
        t_write.start()
        t_read.join(timeout=15)
        t_write.join(timeout=15)
        assert not (t_read.is_alive())
        assert not (t_write.is_alive())
        assert (errors) == ([])
        reset_config_for_tests()
        data = self._load_written()
        assert (data.get("image_model")) == ("flux-keep")
        assert (data.get("text_model")) == ("gpt")

    def test_config_read_creates_backup_on_failure(self):
        corrupt = '{ invalid json '
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        reset_config_for_tests()
        assert (get_config('calc_prompt_max_tokens')) == (4096)
        assert (os.path.exists(self._backup_path()))
        with open(self.config_path, 'r', encoding='utf-8') as f:
            assert (f.read()) == (corrupt)

    def test_valid_config_no_backup_on_set(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        reset_config_for_tests()
        set_config('text_model', 'other')
        assert not (os.path.exists(self._backup_path()))
        assert (self._load_written()['text_model']) == ('other')

    def test_out_of_range_temperature_does_not_discard_api_keys(self):
        """One invalid numeric field must not collapse the whole config to {}."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                "temperature": 1.5,
                "api_keys_by_endpoint": {"https://api.openai.com": "sk-keep"},
                "text_model": "gpt",
            }, f)
        reset_config_for_tests()
        assert (get_config("text_model")) == ("gpt")
        assert (get_api_key_for_endpoint("https://api.openai.com")) == ("sk-keep")
        assert (get_config("temperature")) == (1.0)
        data = self._load_written()
        assert (data["api_keys_by_endpoint"]["https://api.openai.com"]) == ("sk-keep")
        assert (data["temperature"]) == (1.0)

    def test_empty_temperature_uses_schema_default(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"temperature": ""}, f)
        reset_config_for_tests()
        assert (get_config_float("temperature")) == (-1.0)

    def test_get_api_config_omits_default_temperature(self):
        """Schema default -1 means leave temperature out so the provider picks."""
        from plugin.framework.config import get_api_config

        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        reset_config_for_tests()
        assert (get_config_float("temperature")) == (-1.0)
        cfg = get_api_config()
        assert ("temperature") not in (cfg)

    def test_get_api_config_includes_explicit_temperature(self):
        from plugin.framework.config import get_api_config

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"temperature": 0.3}, f)
        reset_config_for_tests()
        cfg = get_api_config()
        assert (cfg.get("temperature")) == (0.3)

    def test_failed_api_key_write_does_not_leak_into_cache(self):
        reset_config_for_tests()
        with patch("plugin.framework.config._write_config_file", side_effect=OSError("disk full")):
            with pytest.raises(ConfigError):
                set_api_key_for_endpoint("https://api.openai.com", "sk-new")
        assert (get_api_key_for_endpoint("https://api.openai.com")) == ("")

    def test_remove_config_skips_write_when_remaining_invalid(self):
        from plugin.framework.config import remove_config

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"temperature": 1.5, "text_model": "keep-me"}, f)
        reset_config_for_tests()
        remove_config("text_model")
        data = self._load_written()
        assert (data.get("text_model")) == ("keep-me")
        assert (data.get("temperature")) == (1.5)

    def test_get_config_default_resolution(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        from plugin.framework.errors import ConfigError
        assert (get_config('calc_prompt_max_tokens')) == (4096)
        assert (get_config('prompt_lru')) == ([])
        assert (get_config('slash_command_lru')) == ([])
        assert (get_config('endpoint')) == ('http://localhost:11434')
        assert (get_config('model_lru@http://localhost:11434')) == ([])
        assert (get_config_int('extension_update_check_epoch')) == (0)
        assert (get_config_int('librepy_update_check_epoch')) == (0)
        assert (get_config_int('libreharper_update_check_epoch')) == (0)
        # Module-yaml keys (no WriterAgentConfig dataclass field; defaults from MODULES schema)
        assert (get_config_int('web_cache_max_mb')) == (50)
        assert (get_config_int('web_cache_validity_days')) == (30)
        assert (get_config_int('extend_selection_max_tokens')) == (1000)
        assert (get_config_bool('chatbot.show_search_thinking')) == (False)
        assert (get_config_bool('web_research_cache_enabled')) == (False)
        assert (get_config('embeddings.folder_search_mode')) == ('none')
        assert (get_config('scripting.python_max_data_cells')) == (250000)
        assert (get_config('scripting.python_venv_path')) == ('')
        assert (get_config_bool('scripting.python_auto_spill')) == (True)
        assert (get_config_bool('scripting.python_geometric_recalc_order')) == (False)
        assert (get_config('doc.agent_edit_review_mode')) == ('off')
        assert (get_config_int('chatbot.max_tool_rounds')) == (15)
        assert (get_config_int('web_research_cache_jaccard_percent')) == (60)
        assert (get_config_int('web_research_cache_embedding_percent')) == (75)
        assert (get_config_int('web_research_cache_min_overlap')) == (8)
        assert (get_config('log_level')) == ('DEBUG')
        with pytest.raises(ConfigError) as err_ctx:
            get_config('unknown_key')
        assert (err_ctx.value.details.get('key')) == ('unknown_key')
        assert ('unknown_key') in (str(err_ctx.value))
        with pytest.raises(ConfigError):
            get_config('some_new_lru')
        with pytest.raises(ConfigError):
            get_config('custom_by_endpoint')

    def test_stale_calc_prompt_max_tokens_upgraded_and_persisted(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt', 'calc_prompt_max_tokens': 70}, f)
        reset_config_for_tests()
        assert (get_config('calc_prompt_max_tokens')) == (4096)
        data = self._load_written()
        # Default 4096 is omitted from JSON file on disk
        assert ('calc_prompt_max_tokens') not in (data)
        assert (data['text_model']) == ('gpt')

    def test_calc_prompt_max_tokens_at_or_above_100_preserved(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'calc_prompt_max_tokens': 150}, f)
        reset_config_for_tests()
        assert (get_config('calc_prompt_max_tokens')) == (150)
        data = self._load_written()
        assert (data['calc_prompt_max_tokens']) == (150)

    def test_writeragent_config_validate_bumps_stale_prompt_tokens(self):
        from plugin.framework.config_schema import WriterAgentConfig

        cfg = WriterAgentConfig(calc_prompt_max_tokens=70)
        cfg.validate()
        assert (cfg.calc_prompt_max_tokens) == (4096)

        cfg2 = WriterAgentConfig(calc_prompt_max_tokens=150)
        cfg2.validate()
        assert (cfg2.calc_prompt_max_tokens) == (150)
        with pytest.raises(ConfigError):
            get_config('some_custom_map')

    def test_set_config_real_write_prunes_other_defaults(self):
        # Existing files that still contain default keys are cleaned on the next
        # write of a non-default value, not on a no-op set of an unchanged key.
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'endpoint': 'http://localhost:11434',
                'chat_max_tokens': 16384,
                'text_model': 'custom-model',
            }, f)
        reset_config_for_tests()
        set_config('request_timeout', 60)
        data = self._load_written()
        assert (data) == ({
            'text_model': 'custom-model',
            'request_timeout': 60,
        })

    def test_set_config_identical_value_does_not_prune_other_defaults(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'endpoint': 'http://localhost:11434',
                'text_model': 'custom-model',
            }, f)
        reset_config_for_tests()
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config('text_model', 'custom-model')
            mock_emit.assert_not_called()
        data = self._load_written()
        assert (data.get('endpoint')) == ('http://localhost:11434')
        assert (data.get('text_model')) == ('custom-model')

    def test_set_config_skips_identical_value(self):
        reset_config_for_tests()
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config('text_model', 'gpt')
            mock_emit.assert_not_called()
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_config('text_model', 'other')
            mock_emit.assert_called_once()  # ctx from _emit_config_changed_ctx
        data = self._load_written()
        assert (data.get('text_model')) == ('other')

    def test_set_config_invalid_numeric_falls_back_to_current_value(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'extend_selection_max_tokens': 1200}, f)
        reset_config_for_tests()

        set_config('extend_selection_max_tokens', 'not-a-number')

        data = self._load_written()
        assert (data.get('extend_selection_max_tokens')) == (1200)

    def test_set_config_clamps_schema_bounds(self):
        set_config('extend_selection_max_tokens', '1')
        set_config('edit_selection_max_new_tokens', '99999')

        data = self._load_written()
        assert (data.get('extend_selection_max_tokens')) == (10)
        assert (data.get('edit_selection_max_new_tokens')) == (4096)
        # Verify no default fields leaked into file
        assert ('temperature') not in (data)
        assert ('chat_max_tokens') not in (data)
        assert ('saved_python_scripts') not in (data)

    def test_set_config_log_level_runtime_default_omitted(self):
        set_config('text_model', 'custom-model')
        set_config('log_level', get_config('log_level'))
        data = self._load_written()
        assert ('log_level') not in (data)
        assert (data.get('text_model')) == ('custom-model')

    def test_set_config_log_level_non_default_persisted(self):
        set_config('log_level', 'INFO')
        data = self._load_written()
        assert (data.get('log_level')) == ('INFO')

    def test_set_config_omits_default_value(self):
        # Setting a value to its default does not write to an empty or non-existent file
        set_config('endpoint', 'http://localhost:11434')
        if os.path.exists(self.config_path):
            data = self._load_written()
            assert ('endpoint') not in (data)
        assert (get_config('endpoint')) == ('http://localhost:11434')

    def test_set_config_only_persists_non_defaults(self):
        set_config('text_model', 'custom-model')
        data = self._load_written()
        assert (data) == ({'text_model': 'custom-model'})
        with open(self.config_path, 'r', encoding='utf-8') as f:
            text = f.read()
        assert (text.startswith('//'))
        assert (CONFIG_SCHEMA_DOC_URL) in (text)
        assert ("/blob/master/") in (CONFIG_SCHEMA_DOC_URL)
        assert ("/blob/main/") not in (CONFIG_SCHEMA_DOC_URL)

    def test_parse_config_json_text_strips_schema_comment(self):
        raw = CONFIG_SCHEMA_COMMENT + '{\n    "text_model": "custom-model"\n}\n'
        data = parse_config_json_text(raw)
        assert (data) == ({'text_model': 'custom-model'})

    def test_set_config_reverting_to_default_removes_key(self):
        set_config('request_timeout', 60)
        data = self._load_written()
        assert (data.get('request_timeout')) == (60)

        # Resetting back to default (120) removes it from file
        set_config('request_timeout', 120)
        data = self._load_written()
        assert ('request_timeout') not in (data)
        assert (get_config_int('request_timeout')) == (120)

    def test_is_default_value_types(self):
        from plugin.framework.config_schema import is_default_value

        assert (is_default_value('endpoint', 'http://localhost:11434'))
        assert (is_default_value('endpoint', 'http://localhost:11434/'))
        assert not (is_default_value('endpoint', 'https://api.openai.com/v1'))
        assert (is_default_value('request_timeout', 120))
        assert (is_default_value('request_timeout', '120'))
        assert not (is_default_value('request_timeout', 60))
        assert (is_default_value('parallel_tool_calls', True))
        assert (is_default_value('parallel_tool_calls', 'true'))
        assert not (is_default_value('parallel_tool_calls', False))
        assert (is_default_value('prompt_lru', []))
        assert not (is_default_value('prompt_lru', ['a']))
        assert not (is_default_value('unknown_key_xyz', 'val'))
        # log_level default is DEBUG in a checkout (plugin/tests present), WARN in a shipped OXT
        assert (is_default_value('log_level', get_config('log_level')))
        assert not (is_default_value('log_level', 'INFO'))

    def test_prune_default_values_batch(self):
        from plugin.framework.config_schema import prune_default_values

        data = {
            'endpoint': 'http://localhost:11434',
            'chat_max_tokens': 16384,
            'temperature': -1.0,
            'request_timeout': 60,  # non-default
            'text_model': 'custom-model',  # non-default
            'custom_unrecognized_key': 'custom_val',  # unknown keys dropped
            'chat_sidebar_mode': 'chat',
            'writer.track_changes_reviewable': False,
        }
        pruned = prune_default_values(data)
        assert (pruned) == ({
            'request_timeout': 60,
            'text_model': 'custom-model',
        })

    def test_future_default_change_applies_when_omitted(self):
        # File only contains user customization for text_model
        set_config('text_model', 'my-model')
        data = self._load_written()
        assert ('extend_selection_max_tokens') not in (data)

        # In current version, get_config returns current default (1000)
        assert (get_config_int('extend_selection_max_tokens')) == (1000)

        # Simulate a future update that changes the default in manifest schema (MODULES)
        mock_modules = [{
            "name": "chatbot",
            "config": {
                "extend_selection_max_tokens": {
                    "type": "int",
                    "default": 2000,
                }
            }
        }]
        with patch("plugin.framework.config_schema.MODULES", mock_modules):
            reset_config_for_tests()
            # Because extend_selection_max_tokens was not written to disk, the new default is picked up automatically!
            assert (get_config_int('extend_selection_max_tokens')) == (2000)

    def test_remove_config_prunes_remaining_defaults(self):
        from plugin.framework.config import remove_config

        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'endpoint': 'http://localhost:11434',
                'chat_max_tokens': 16384,
                'request_timeout': 60,
                'text_model': 'custom-model'
            }, f)
        reset_config_for_tests()

        remove_config('request_timeout')

        data = self._load_written()
        # All default keys (endpoint, chat_max_tokens) pruned, only custom text_model remains
        assert (data) == ({'text_model': 'custom-model'})

    def test_set_config_drops_unknown_and_retired_keys(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'text_model': 'custom-model',
                'chat_sidebar_mode': 'chat',
                'chat_direct_image': False,
                'writer.track_changes_reviewable': False,
                'writer.require_edit_review': False,
                'writer.edit_review_timeout': 900,
                'doc.edit_review_timeout': 0,
                'scripting.ppt_master_data_path': '',
                'scripting.python_convert_datetime': False,
            }, f)
        reset_config_for_tests()
        set_config('request_timeout', 60)
        data = self._load_written()
        assert (data) == ({
            'text_model': 'custom-model',
            'doc.edit_review_timeout': 0,
            'request_timeout': 60,
        })


class TestRobustNumericParsing:

    def test_parse_int_robust(self):
        from plugin.framework.config_schema import parse_int_robust

        # Test standard integers
        assert (parse_int_robust(8765)) == (8765)
        assert (parse_int_robust(0)) == (0)
        assert (parse_int_robust(-42)) == (-42)

        # Test standard floats
        assert (parse_int_robust(8765.0)) == (8765)
        assert (parse_int_robust(8765.99)) == (8765)

        # Test string integers
        assert (parse_int_robust("8765")) == (8765)
        assert (parse_int_robust(" 8765 ")) == (8765)

        # Test string floats
        assert (parse_int_robust("8765.0")) == (8765)
        assert (parse_int_robust("8765.00")) == (8765)
        assert (parse_int_robust("8765.7")) == (8765)

        # Test European decimal commas (like German locale)
        assert (parse_int_robust("8765,0")) == (8765)
        assert (parse_int_robust("8765,00")) == (8765)
        assert (parse_int_robust("8765,5")) == (8765)

        # Test invalid inputs raise ValueError
        with pytest.raises(ValueError):
            parse_int_robust(None)
        with pytest.raises(ValueError):
            parse_int_robust("")
        with pytest.raises(ValueError):
            parse_int_robust("   ")
        with pytest.raises(ValueError):
            parse_int_robust("invalid")
        # Non-finite floats must be ValueError (not OverflowError) for @deal.raises
        with pytest.raises(ValueError):
            parse_int_robust(float("inf"))
        with pytest.raises(ValueError):
            parse_int_robust(float("-inf"))
        with pytest.raises(ValueError):
            parse_int_robust(float("nan"))
        with pytest.raises(ValueError):
            parse_int_robust("inf")

    def test_parse_float_robust(self):
        from plugin.framework.config_schema import parse_float_robust

        # Test standard floats
        assert (parse_float_robust(7.5)) == (7.5)
        assert (parse_float_robust(0.0)) == (0.0)

        # Test standard integers
        assert (parse_float_robust(7)) == (7.0)

        # Test string floats
        assert (parse_float_robust("7.5")) == (7.5)
        assert (parse_float_robust(" 7.5 ")) == (7.5)

        # Test European decimal commas
        assert (parse_float_robust("7,5")) == (7.5)
        assert (parse_float_robust("0,25")) == (0.25)

        # Test invalid inputs raise ValueError
        with pytest.raises(ValueError):
            parse_float_robust(None)
        with pytest.raises(ValueError):
            parse_float_robust("")
        with pytest.raises(ValueError):
            parse_float_robust("   ")
        with pytest.raises(ValueError):
            parse_float_robust("invalid")

    def test_config_validate_type_casting(self):
        from plugin.framework.config_schema import WriterAgentConfig

        # Test standard dataclass type casting
        config = WriterAgentConfig.from_dict({
            "temperature": "0,7",  # String with European decimal comma
            "chat_max_tokens": 16384.0,  # Float instead of int
            "image_steps": "30",  # String int
        })
        config.validate()

        assert (config.temperature) == (0.7)
        assert (config.chat_max_tokens) == (16384)
        assert (config.image_steps) == (30)

        # Test _extra_config dynamic YAML schema type casting (e.g. mcp.mcp_port)
        # First let's patch MODULES to contain a mock module schema
        mock_modules = [{
            "name": "mcp",
            "config": {
                "mcp_port": {
                    "type": "int",
                    "default": 18765
                },
                "mcp_host": {
                    "type": "string",
                    "default": "localhost"
                }
            }
        }]
        with patch("plugin.framework.config_schema.MODULES", mock_modules):
            config_with_extra = WriterAgentConfig.from_dict({
                "mcp.mcp_port": "8765,00",  # German locale format
            })
            config_with_extra.validate()

            assert (config_with_extra._extra_config.get("mcp.mcp_port")) == (8765)

    def test_yaml_backed_key_extra_config_type_casting(self):
        from plugin.framework.config_schema import WriterAgentConfig

        config = WriterAgentConfig.from_dict({"web_cache_max_mb": "50,0"})
        config.validate()
        assert (config._extra_config.get("web_cache_max_mb")) == (50)

    def test_yaml_backed_key_schema_bounds_flat_and_dotted(self):
        from plugin.framework.config_schema import WriterAgentConfig

        config = WriterAgentConfig.from_dict({
            "extend_selection_max_tokens": "1",
            "chatbot.edit_selection_max_new_tokens": "99999",
        })
        config.validate()

        assert (config._extra_config.get("extend_selection_max_tokens")) == (10)
        assert (config._extra_config.get("chatbot.edit_selection_max_new_tokens")) == (4096)

    def test_schema_option_label_canonicalization_from_manifest(self):
        from plugin.framework.config_schema import WriterAgentConfig

        mock_modules = [{
            "name": "demo",
            "config": {
                "mode": {
                    "type": "string",
                    "default": "fast",
                    "options": [{"value": "fast", "label": "Fast Mode"}],
                },
            },
        }]

        config = WriterAgentConfig.from_dict({"demo.mode": "Translated Fast"})

        def fake_gettext(msg):
            if msg == "Fast Mode":
                return "Translated Fast"
            return msg

        with patch("plugin.framework.config_schema.MODULES", mock_modules), \
             patch("plugin.framework.config_schema._", side_effect=fake_gettext):
            config.validate()

        assert (config._extra_config.get("demo.mode")) == ("fast")

    def test_config_validation_constraints(self):
        from plugin.framework.config_schema import WriterAgentConfig
        from plugin.framework.errors import ConfigValidationError

        # temperature > 1.0
        config = WriterAgentConfig.from_dict({"temperature": 1.5})
        with pytest.raises(ConfigValidationError) as ctx:
            config.validate()
        assert (ctx.value.code) == ("INVALID_TEMPERATURE")

        # chat_max_tokens < 0
        config = WriterAgentConfig.from_dict({"chat_max_tokens": -5})
        with pytest.raises(ConfigValidationError) as ctx:
            config.validate()
        assert (ctx.value.code) == ("INVALID_CHAT_MAX_TOKENS")

        # request_timeout <= 0
        config = WriterAgentConfig.from_dict({"request_timeout": 0})
        with pytest.raises(ConfigValidationError) as ctx:
            config.validate()
        assert (ctx.value.code) == ("INVALID_REQUEST_TIMEOUT")

        # endpoint preset resolution
        config = WriterAgentConfig.from_dict({"endpoint": "OpenRouter"})
        config.validate()
        assert (config.endpoint) == ("https://openrouter.ai/api")

    def test_validate_falls_back_when_config_ui_helpers_missing(self):
        """LibrePy omits config_ui_helpers; endpoint still normalizes."""
        from plugin.framework.config_schema import WriterAgentConfig
        from plugin.framework.url_utils import normalize_endpoint_url

        config = WriterAgentConfig.from_dict({"endpoint": "http://localhost:11434/"})
        with patch.dict(sys.modules, {"plugin.chatbot.config_ui_helpers": None}):
            config.validate()
        assert (config.endpoint) == (normalize_endpoint_url("http://localhost:11434/"))

    def test_validate_api_config_without_config_ui_helpers(self):
        from plugin.framework.config import validate_api_config

        with patch.dict(sys.modules, {"plugin.chatbot.config_ui_helpers": None}):
            ok, err = validate_api_config({
                "endpoint": "https://example.invalid",
                "model": "glm-5.2",
            })
        assert (ok)
        assert (err) == ("")
