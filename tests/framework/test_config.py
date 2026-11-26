import contextlib
import pytest
import json
import logging
import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

from plugin.framework.config import (
    CONFIG_BACKUP_SUFFIX,
    CONFIG_SCHEMA_COMMENT,
    _resolve_config_path_from_ctx,
    atomic_write_text,
    get_api_key_for_endpoint,
    get_config,
    get_config_dict,
    parse_config_json_text,
    reset_config_for_tests,
    save,
    update_config_mapping,
    validate_api_config,
)
from plugin.framework.errors import ConfigError, ConfigValidationError
from plugin.framework.client.model_fetcher import get_image_model, get_text_model, set_image_model, set_text_model


@contextlib.contextmanager
def _patch_manifest(mock_modules):
    from plugin.framework import config_schema
    orig = (
        config_schema.MODULES,
        config_schema.CONFIG_DEFAULTS,
        config_schema.CONFIG_SCHEMAS,
        config_schema.FLAT_TO_MODULE_KEY,
        config_schema.SECRET_CONFIG_KEYS,
        config_schema.SECRET_ENTRY_FIELDS,
    )
    config_schema.set_manifest_modules(mock_modules)
    try:
        yield
    finally:
        (
            config_schema.MODULES,
            config_schema.CONFIG_DEFAULTS,
            config_schema.CONFIG_SCHEMAS,
            config_schema.FLAT_TO_MODULE_KEY,
            config_schema.SECRET_CONFIG_KEYS,
            config_schema.SECRET_ENTRY_FIELDS,
        ) = orig
from plugin.framework.event_bus import get_event_bus
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

        def mock_save(patch, event_key=None):
            self.config_data.update(patch)

        self.get_patcher = patch('plugin.framework.config.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.framework.config.save', side_effect=mock_save)
        self.get_mf_patcher = patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config)
        self.set_mf_patcher = patch('plugin.framework.client.model_fetcher.save', side_effect=mock_save)
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
        with patch('plugin.framework.model_lru.update_lru_history') as mock_lru, patch.object(get_event_bus(), 'emit') as mock_emit:
            set_text_model('new-chat-model')
            assert self.config_data.get('text_model') == 'new-chat-model'
            mock_lru.assert_called_once_with('new-chat-model', 'model_lru', '')
            mock_emit.assert_not_called()

    def test_set_text_model_skips_when_unchanged(self):
        self.config_data['text_model'] = 'same-model'
        self.mock_mf_set.reset_mock()
        set_text_model('same-model')
        self.mock_mf_set.assert_not_called()

    def test_set_text_model_update_lru_false(self):
        self.config_data['text_model'] = ''
        with patch('plugin.framework.model_lru.update_lru_history') as mock_lru:
            set_text_model('chat-only', update_lru=False)
            assert self.config_data.get('text_model') == 'chat-only'
            mock_lru.assert_not_called()

    def test_get_text_model_ignores_legacy_model_key(self):
        """Legacy top-level ``model`` in writeragent.json is no longer read."""
        self.config_data['model'] = 'legacy-model'
        self.config_data['text_model'] = ''
        with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value='http://localhost:11434'), \
             patch('plugin.framework.client.model_fetcher.get_provider_from_endpoint', return_value='ollama'), \
             patch('plugin.framework.client.model_fetcher.get_provider_defaults', return_value={'text_model': 'default-model'}):
            assert get_text_model() == 'default-model'
        assert self.config_data.get('text_model') == ''
        assert self.config_data.get('model') == 'legacy-model'

    def test_model_lru_endpoint_isolation(self):
        endpoint_a = 'http://localhost:11434'
        endpoint_b = 'http://localhost:8080'
        self.config_data[f'model_lru@{endpoint_b}'] = ['other-model']
        with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value=endpoint_a), \
             patch('plugin.framework.model_lru.get_config', side_effect=lambda k: self.config_data.get(k, '')), \
             patch('plugin.framework.model_lru.save', side_effect=lambda p: self.config_data.update(p)), \
             patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value=endpoint_a):
            set_text_model('model-on-a', update_lru=True)
        assert self.config_data.get(f'model_lru@{endpoint_a}') == ['model-on-a']
        assert self.config_data.get(f'model_lru@{endpoint_b}') == ['other-model']

    def test_set_image_model_endpoint(self):
        self.config_data['image_model'] = ''
        with patch('plugin.framework.model_lru.update_lru_history') as mock_lru, patch.object(get_event_bus(), 'emit') as mock_emit:
            set_image_model('new-endpoint-model')
            assert self.config_data.get('image_model') == 'new-endpoint-model'
            mock_lru.assert_called_once_with('new-endpoint-model', 'image_model_lru', '')
            mock_emit.assert_not_called()

    def test_set_image_model_skips_when_unchanged(self):
        self.config_data['image_model'] = 'same-model'
        self.mock_set.reset_mock()
        set_image_model('same-model')
        self.mock_set.assert_not_called()

    def test_get_image_model(self):
        self.config_data['image_model'] = 'end-1'
        assert get_image_model() == 'end-1'

    def test_get_api_key_for_endpoint_missing(self):
        assert get_api_key_for_endpoint('http://localhost:11434') == ''

    def test_get_api_key_for_endpoint_existing(self):
        self.config_data['api_keys_by_endpoint'] = {'http://localhost:11434': 'test-key-123'}
        assert get_api_key_for_endpoint('http://localhost:11434') == 'test-key-123'
        assert get_api_key_for_endpoint('http://localhost:11434/') == 'test-key-123'


class TestConfigSyncFileIO:

    def setup_method(self):
        reset_config_for_tests()
        self.ctx = MagicMock()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, 'writeragent.json')

        def mock_config_path():
            return self.config_path
        self.path_patcher = patch('plugin.framework.config.get_config_file_path', side_effect=mock_config_path)
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

    def test_get_config_huge_integer_does_not_crash(self):
        """Very large JSON integers must load without OverflowError in float() coercion."""
        huge = 10**400
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"chat_max_tokens": huge, "temperature": huge, "endpoint": "http://localhost:11434"}, handle)
        reset_config_for_tests()
        assert get_config("chat_max_tokens") == huge
        temperature = get_config("temperature")
        assert temperature != huge
        assert get_config("temperature") == float(temperature)

    def test_get_config_dict_is_not_the_cache(self):
        save({"openrouter_chat_extra": {"temperature": 0.2}})
        first = get_config("openrouter_chat_extra")
        first["temperature"] = 9
        first["extra"] = True
        second = get_config("openrouter_chat_extra")
        assert second == {"temperature": 0.2}
        assert "extra" not in second

        whole = get_config_dict()
        extra = whole["openrouter_chat_extra"]
        extra["temperature"] = 9
        assert get_config("openrouter_chat_extra") == {"temperature": 0.2}

    def test_save_api_key_file_io(self):
        save({"api_keys_by_endpoint": {'http://api.openai.com': 'sk-1234'}})
        assert os.path.exists(self.config_path)
        data = self._load_written()
        assert 'api_keys_by_endpoint' in data
        assert data['api_keys_by_endpoint'].get('http://api.openai.com') == 'sk-1234'
        assert get_api_key_for_endpoint('http://api.openai.com') == 'sk-1234'

    def test_reasoning_effort_is_per_endpoint_and_normalized(self):
        from plugin.framework.config import get_api_config, get_reasoning_effort_for_endpoint, set_reasoning_effort_for_endpoint

        set_reasoning_effort_for_endpoint("https://api.openai.com/v1", "high")
        set_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1", "medium")

        data = self._load_written()
        assert "reasoning_effort" in data
        assert data["reasoning_effort"].get("https://api.openai.com") == "high"
        assert data["reasoning_effort"].get("https://openrouter.ai/api") == "medium"

        assert get_reasoning_effort_for_endpoint("https://api.openai.com/v1") == "high"
        assert get_reasoning_effort_for_endpoint("https://api.openai.com") == "high"
        assert get_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1") == "medium"
        assert get_reasoning_effort_for_endpoint("https://openrouter.ai/api") == "medium"

        save({"endpoint": "https://api.openai.com/v1"})
        cfg = get_api_config()
        assert cfg.get("reasoning_effort") == "high"

        save({"endpoint": "https://openrouter.ai/api/v1"})
        cfg_or = get_api_config()
        assert cfg_or.get("reasoning_effort") == "medium"

        save({"endpoint": "https://api.together.xyz/v1"})
        cfg_other = get_api_config()
        assert "reasoning_effort" not in cfg_other

    def test_reasoning_effort_slot_patch_does_not_drop_other_endpoints(self):
        from plugin.framework.config import get_config, set_reasoning_effort_for_endpoint

        set_reasoning_effort_for_endpoint("https://api.openai.com/v1", "high")
        set_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1", "medium")

        save({"reasoning_effort": {"https://api.openai.com": "max"}})
        stored = get_config("reasoning_effort")
        assert stored.get("https://api.openai.com") == "max"
        assert stored.get("https://openrouter.ai/api") == "medium"

    def test_clear_reasoning_effort_drops_slot(self):
        from plugin.framework.config import get_reasoning_effort_for_endpoint, set_reasoning_effort_for_endpoint

        set_reasoning_effort_for_endpoint("https://api.openai.com/v1", "high")
        set_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1", "medium")

        set_reasoning_effort_for_endpoint("https://api.openai.com/v1", "")
        assert get_reasoning_effort_for_endpoint("https://api.openai.com/v1") == ""
        assert get_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1") == "medium"

        set_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1", "")
        assert get_reasoning_effort_for_endpoint("https://openrouter.ai/api/v1") == ""
        data = self._load_written()
        assert not data.get("reasoning_effort")

    def test_non_dict_reasoning_effort_resets(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"reasoning_effort": "high"}, f)
        reset_config_for_tests()
        assert get_config("reasoning_effort") == {}

    def test_get_api_key_file_io_missing_file(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        assert get_api_key_for_endpoint('http://api.missing.com') == ''

    def test_corrupt_config_file_io(self):
        corrupt = '{ invalid json '
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        reset_config_for_tests()
        assert get_api_key_for_endpoint('http://api.openai.com') == ''
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            assert f.read() == corrupt
        with pytest.raises(ConfigError):
            save({"api_keys_by_endpoint": {'http://api.openai.com': 'sk-recovered'}})
        with open(self.config_path, encoding='utf-8') as f:
            assert f.read() == corrupt
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            assert f.read() == corrupt
        backups = [e for e in os.listdir(self.temp_dir.name) if e.startswith(os.path.basename(self.config_path) + CONFIG_BACKUP_SUFFIX)]
        assert len(backups) == 1

    def test_unrepairable_config_backs_up_once(self):
        broken = "{ invalid json 1"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(broken)
        reset_config_for_tests()
        for unused in range(3):
            assert get_config("text_model") == ""
        with open(self.config_path, "r", encoding="utf-8") as f:
            assert f.read() == broken
        backups = [e for e in os.listdir(self.temp_dir.name) if e.startswith(os.path.basename(self.config_path) + CONFIG_BACKUP_SUFFIX)]
        assert len(backups) == 1
        assert backups[0] == os.path.basename(self._backup_path())
        with open(self._backup_path(), "r", encoding="utf-8") as f:
            assert f.read() == broken

        broken2 = "{ invalid json 1 -- extra"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(broken2)
        reset_config_for_tests()
        assert get_config("text_model") == ""
        backups_after = [e for e in os.listdir(self.temp_dir.name) if e.startswith(os.path.basename(self.config_path) + CONFIG_BACKUP_SUFFIX)]
        assert len(backups_after) == 2
        stamped = [b for b in backups_after if b != os.path.basename(self._backup_path())]
        assert len(stamped) == 1
        with open(self._backup_path(), "r", encoding="utf-8") as f:
            assert f.read() == broken
        with open(os.path.join(self.temp_dir.name, stamped[0]), "r", encoding="utf-8") as f:
            assert f.read() == broken2

    def test_broken_file_keeps_last_good_snapshot(self):
        from plugin.framework.config import _cache

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"text_model": "my-snapshot-model"}, f)
        reset_config_for_tests()
        assert get_config("text_model") == "my-snapshot-model"

        broken = "{ invalid json body"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(broken)
        _cache.mtime_last_checked = 0.0
        assert get_config("text_model") == "my-snapshot-model"
        with open(self.config_path, "r", encoding="utf-8") as f:
            assert f.read() == broken

    def test_oserror_on_load_returns_snapshot(self):
        from plugin.framework.config import _cache

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"text_model": "my-snapshot-model"}, f)
        reset_config_for_tests()
        assert get_config("text_model") == "my-snapshot-model"

        _cache.mtime_last_checked = 0.0
        _orig_open = open
        def _mock_open(file, *args, **kwargs):
            if file == self.config_path:
                raise OSError("disk read failure")
            return _orig_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=_mock_open):
            assert get_config("text_model") == "my-snapshot-model"
        assert isinstance(_cache.data, dict)

    def test_unchanged_broken_file_logs_once(self, caplog):
        from plugin.framework import config as config_mod

        broken = "{ invalid json 1"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(broken)
        reset_config_for_tests()
        real_repair = config_mod._try_repair_config_dict
        repair_calls = 0

        def counting_repair(text):
            nonlocal repair_calls
            repair_calls += 1
            return real_repair(text)

        with caplog.at_level(logging.WARNING), patch("plugin.framework.config._try_repair_config_dict", side_effect=counting_repair):
            for unused in range(3):
                assert get_config("text_model") == ""
            errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
            assert len(errors) == 1
            warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING and "could not be auto-repaired" in rec.message]
            assert len(warnings) == 1
            assert repair_calls == 1

            broken2 = "{ invalid json 2 with longer text"
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(broken2)
            assert get_config("text_model") == ""
            errors2 = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
            assert len(errors2) == 2
            warnings2 = [rec for rec in caplog.records if rec.levelno == logging.WARNING and "could not be auto-repaired" in rec.message]
            assert len(warnings2) == 2
            assert repair_calls == 2

    def test_warm_cache_corrupt_file_is_not_reparsed(self):
        from plugin.framework import config as config_mod
        from plugin.framework.config import _cache

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"text_model": "good-model"}, f)
        reset_config_for_tests()
        assert get_config("text_model") == "good-model"

        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("{ not json")

        _cache.mtime_last_checked = 0.0
        assert get_config("text_model") == "good-model"

        real_repair = config_mod._try_repair_config_dict
        repair_calls = 0

        def counting_repair(text):
            nonlocal repair_calls
            repair_calls += 1
            return real_repair(text)

        _cache.mtime_last_checked = 0.0
        with patch("plugin.framework.config._try_repair_config_dict", side_effect=counting_repair):
            assert get_config("text_model") == "good-model"
            assert repair_calls == 0

    def test_config_trailing_comma_auto_repair(self):
        broken = '{"text_model": "gpt",}'
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(broken)
        reset_config_for_tests()
        assert get_config('text_model') == 'gpt'
        with open(self._backup_path(), 'r', encoding='utf-8') as f:
            assert f.read() == broken
        data = self._load_written()
        assert data['text_model'] == 'gpt'

    def test_get_config_repair_persist_does_not_drop_concurrent_set(self):
        """GET-path repair persist must not rewrite over a concurrent save."""
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
                save({"image_model": "flux-keep"})
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
        assert errors == []
        reset_config_for_tests()
        data = self._load_written()
        assert data.get("image_model") == "flux-keep"
        assert data.get("text_model") == "gpt"

    def test_long_malformed_config_repair_falls_through(self):
        """A body over DEAL_MAX_SOURCE must not raise PreContractError out of config load."""
        from plugin.framework.config import _try_repair_config_dict
        from plugin.framework.deal_shim import DEAL_MAX_SOURCE

        body = '{ "text_model": "' + ("x" * (DEAL_MAX_SOURCE + 1))
        # The pytest pre is total, so repair runs (same as a stripped OXT).
        # A contract assertion must not escape config load.
        repaired = _try_repair_config_dict(body)
        assert repaired is None or isinstance(repaired, dict)

    def test_config_read_creates_backup_on_failure(self):
        corrupt = '{ invalid json '
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        reset_config_for_tests()
        assert get_config('calc_prompt_max_tokens') == 4096
        assert os.path.exists(self._backup_path())
        with open(self.config_path, 'r', encoding='utf-8') as f:
            assert f.read() == corrupt

    def test_valid_config_no_backup_on_set(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        reset_config_for_tests()
        save({'text_model': 'other'})
        assert not (os.path.exists(self._backup_path()))
        assert self._load_written()['text_model'] == 'other'

    def test_out_of_range_temperature_does_not_discard_api_keys(self):
        """One invalid numeric field must not collapse the whole config to {}."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                "temperature": 1.5,
                "api_keys_by_endpoint": {"https://api.openai.com": "sk-keep"},
                "text_model": "gpt",
            }, f)
        reset_config_for_tests()
        assert get_config("text_model") == "gpt"
        assert get_api_key_for_endpoint("https://api.openai.com") == "sk-keep"
        assert get_config("temperature") == 1.0
        data = self._load_written()
        assert data["api_keys_by_endpoint"]["https://api.openai.com"] == "sk-keep"
        assert data["temperature"] == 1.0

    def test_empty_temperature_uses_schema_default(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"temperature": ""}, f)
        reset_config_for_tests()
        assert get_config("temperature") == -1.0

    def test_get_api_config_omits_default_temperature(self):
        """Schema default -1 means leave temperature out so the provider picks."""
        from plugin.framework.config import get_api_config

        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        reset_config_for_tests()
        assert get_config("temperature") == -1.0
        cfg = get_api_config()
        assert "temperature" not in cfg

    def test_get_api_config_includes_explicit_temperature(self):
        from plugin.framework.config import get_api_config

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"temperature": 0.3}, f)
        reset_config_for_tests()
        cfg = get_api_config()
        assert cfg.get("temperature") == 0.3

    def test_failed_api_key_write_does_not_leak_into_cache(self):
        reset_config_for_tests()
        with patch("plugin.framework.config._write_config_file", side_effect=OSError("disk full")):
            with pytest.raises(ConfigError):
                save({"api_keys_by_endpoint": {"https://api.openai.com": "sk-new"}})
        assert get_api_key_for_endpoint("https://api.openai.com") == ""

    def test_save_default_heals_out_of_range_neighbor(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({
                "temperature": 1.5,
                "text_model": "keep-me",
                "api_keys_by_endpoint": {"https://api.openai.com": "sk-keep"},
            }, f)
        reset_config_for_tests()
        save({"text_model": ""})
        data = self._load_written()
        assert "text_model" not in data
        assert data.get("temperature") == 1.0
        assert data.get("api_keys_by_endpoint") == {"https://api.openai.com": "sk-keep"}

    def test_set_config_heals_out_of_range_neighbor(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"temperature": 1.5, "text_model": "keep-me"}, f)
        reset_config_for_tests()
        save({"seed": "42"})
        data = self._load_written()
        assert data.get("seed") == "42"
        assert data.get("text_model") == "keep-me"
        assert data.get("temperature") == 1.0

    @pytest.mark.parametrize(
        ("initial_json", "expected_old_value"),
        [
            pytest.param({"text_model": "test-model"}, 120, id="omitted-key-uses-schema-default"),
            pytest.param({"request_timeout": 30}, 30, id="stored-key-uses-stored"),
        ],
    )
    def test_set_config_old_value(self, initial_json, expected_old_value):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(initial_json, f)
        reset_config_for_tests()
        with patch.object(get_event_bus(), "emit") as emit:
            save({"request_timeout": 45})
            assert emit.call_count == 1
            assert emit.call_args.kwargs["key"] == "request_timeout"
            assert emit.call_args.kwargs["value"] == 45
            assert emit.call_args.kwargs["old_value"] == expected_old_value

    def test_get_config_default_resolution(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        from plugin.framework.config_schema import CONFIG_DEFAULTS, WriterAgentConfig

        assert get_config('endpoint') == WriterAgentConfig().endpoint
        assert get_config('model_lru@http://localhost:11434') == []
        assert get_config('chatbot.max_tool_rounds') == CONFIG_DEFAULTS['chatbot.max_tool_rounds']
        with pytest.raises(ConfigError) as err_ctx:
            get_config('unknown_key')
        assert err_ctx.value.details.get('key') == 'unknown_key'
        assert 'unknown_key' in str(err_ctx.value)
        with pytest.raises(ConfigError):
            get_config('some_new_lru')
        with pytest.raises(ConfigError):
            get_config('custom_by_endpoint')

    def test_saved_tts_short_answers_false_is_not_overwritten(self):
        """Schema default is on, but a profile that stored False keeps that choice."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'audio.tts_short_answers': False, 'text_model': 'keep-me'}, f)
        reset_config_for_tests()
        assert get_config('audio.tts_short_answers') is False

        save({'text_model': 'still-me'})
        data = self._load_written()
        assert data.get('audio.tts_short_answers') is False
        assert data.get('text_model') == 'still-me'
        assert get_config('audio.tts_short_answers') is False

    def test_stale_calc_prompt_max_tokens_upgraded_and_persisted(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt', 'calc_prompt_max_tokens': 70}, f)
        reset_config_for_tests()
        assert get_config('calc_prompt_max_tokens') == 4096
        data = self._load_written()
        # Default 4096 is omitted from JSON file on disk
        assert 'calc_prompt_max_tokens' not in data
        assert data['text_model'] == 'gpt'

    def test_calc_prompt_max_tokens_at_or_above_100_preserved(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'calc_prompt_max_tokens': 150}, f)
        reset_config_for_tests()
        assert get_config('calc_prompt_max_tokens') == 150
        data = self._load_written()
        assert data['calc_prompt_max_tokens'] == 150

    def test_writeragent_config_validate_bumps_stale_prompt_tokens(self):
        from plugin.framework.config_schema import validate

        out = validate({"calc_prompt_max_tokens": 70})
        assert out["calc_prompt_max_tokens"] == 4096

        out2 = validate({"calc_prompt_max_tokens": 150})
        assert out2["calc_prompt_max_tokens"] == 150
        with pytest.raises(ConfigError):
            get_config('some_custom_map')

    def test_below_100_calc_prompt_max_tokens_writes_once(self):
        """A stale value becomes 4096 with one write. An already-valid file is not rewritten."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt', 'calc_prompt_max_tokens': 70}, f)
        reset_config_for_tests()
        from plugin.framework import config as config_mod

        writes: list[int] = []
        real_write = config_mod._write_config_file

        def counting_write(path: str, data: dict) -> None:
            writes.append(1)
            real_write(path, data)

        with patch('plugin.framework.config._write_config_file', side_effect=counting_write):
            assert get_config('calc_prompt_max_tokens') == 4096
        assert len(writes) == 1
        data = self._load_written()
        assert 'calc_prompt_max_tokens' not in data
        assert data['text_model'] == 'gpt'

    def test_valid_calc_prompt_file_is_not_rewritten_on_get(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt', 'calc_prompt_max_tokens': 150}, f)
        reset_config_for_tests()
        with patch('plugin.framework.config._write_config_file') as mocked_write:
            assert get_config('calc_prompt_max_tokens') == 150
            assert get_config('text_model') == 'gpt'
        mocked_write.assert_not_called()
        data = self._load_written()
        assert data['calc_prompt_max_tokens'] == 150
        assert data['text_model'] == 'gpt'

    def test_set_config_one_key_does_not_rewrite_other_keys(self):
        # A real change patches that key only. Defaults already stored, and
        # every other key must stay; rewriting the whole blob would drop keys
        # not present in the patch.
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'endpoint': 'http://localhost:11434',
                'chat_max_tokens': 16384,
                'text_model': 'custom-model',
            }, f)
        reset_config_for_tests()
        save({'request_timeout': 60})
        data = self._load_written()
        assert data == {
            'text_model': 'custom-model',
            'request_timeout': 60,
        }

    def test_set_config_event_value_is_normalized_endpoint(self):
        reset_config_for_tests()
        with patch.object(get_event_bus(), "emit") as mock_emit:
            save({"endpoint": "https://api.example.com/v1"})
        assert mock_emit.call_args.kwargs["value"] == "https://api.example.com"
        assert self._load_written().get("endpoint") == "https://api.example.com"

    @staticmethod
    def _run_barrier_pair(fn_a, fn_b) -> None:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _wrap(fn) -> None:
            try:
                barrier.wait(timeout=5)
                fn()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_wrap, args=(fn_a,)), threading.Thread(target=_wrap, args=(fn_b,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert errors == []
        assert not any(thread.is_alive() for thread in threads)

    def test_save_api_key_patch_keeps_other_slots(self):
        """A one-slot save merges under the lock. It does not replace the map.

        A copy-on-write race drops keys for endpoints updated on another thread
        between read and save. Saves must merge under the lock, not replace the
        whole ``api_keys_by_endpoint`` map.
        """
        reset_config_for_tests()
        save({"api_keys_by_endpoint": {"https://api.together.xyz": "sk-keep"}})
        self._run_barrier_pair(
            lambda: save({"api_keys_by_endpoint": {"https://api.openai.com": "sk-a"}}),
            lambda: save({"api_keys_by_endpoint": {"http://localhost:11434": "sk-b"}}),
        )
        reset_config_for_tests()
        keys = get_config("api_keys_by_endpoint")
        assert keys.get("https://api.openai.com") == "sk-a"
        assert keys.get("http://localhost:11434") == "sk-b"
        assert keys.get("https://api.together.xyz") == "sk-keep"

    def test_set_config_skips_identical_value(self):
        reset_config_for_tests()
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'gpt'}, f)
        with patch.object(get_event_bus(), 'emit') as mock_emit:
            save({'text_model': 'gpt'})
            mock_emit.assert_not_called()
        with patch.object(get_event_bus(), 'emit') as mock_emit:
            save({'text_model': 'other'})
            mock_emit.assert_called_once()
            assert mock_emit.call_args.args[0] == "config:changed"
            assert mock_emit.call_args.kwargs["key"] == "text_model"
            assert mock_emit.call_args.kwargs["value"] == "other"
            assert mock_emit.call_args.kwargs["keys"] == ("text_model",)
        data = self._load_written()
        assert data.get('text_model') == 'other'

    def test_set_config_omitted_default_does_not_write_or_emit(self):
        """A key left out of the file is already the schema default.

        Settings OK calls save for every field. An omitted key must compare equal
        to its schema default; treating it as ``None`` would rewrite the file and
        emit ``config:changed`` on every OK with no real edit.
        """
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'custom-model'}, f)
        reset_config_for_tests()
        with open(self.config_path, encoding='utf-8') as f:
            before = f.read()

        with patch.object(get_event_bus(), 'emit') as mock_emit:
            save({'request_timeout': 120})
            mock_emit.assert_not_called()
            # Settings passes the dialog string; coercion must happen first.
            save({'request_timeout': '120'})
            mock_emit.assert_not_called()

        with open(self.config_path, encoding='utf-8') as f:
            assert f.read() == before
        assert get_config('request_timeout') == 120

        with patch.object(get_event_bus(), 'emit') as mock_emit:
            save({'request_timeout': 60})
            mock_emit.assert_called_once()
            assert mock_emit.call_args.args[0] == "config:changed"
            assert mock_emit.call_args.kwargs["key"] == "request_timeout"
            assert mock_emit.call_args.kwargs["value"] == 60
            assert mock_emit.call_args.kwargs["keys"] == ("request_timeout",)
        data = self._load_written()
        assert data.get('request_timeout') == 60
        assert data.get('text_model') == 'custom-model'

    def test_two_writers_keep_both_keys(self):
        """Two writers that each change a different key must not drop either key.

        Each writer must patch only its keys. Loading the full JSON and writing it
        back lets the second writer's stale copy overwrite the first writer's key.
        """
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'custom_unrecognized_key': 'from-earlier-writer'}, f)
        reset_config_for_tests()
        self._run_barrier_pair(
            lambda: save({'text_model': 'from-writer-a'}),
            lambda: save({'image_model': 'from-writer-b'}),
        )
        reset_config_for_tests()
        data = self._load_written()
        assert data.get('text_model') == 'from-writer-a'
        assert data.get('image_model') == 'from-writer-b'
        assert data.get('custom_unrecognized_key') == 'from-earlier-writer'

    def test_update_config_keeps_both_map_keys(self):
        """update_config_mapping applies fn to the value just read, under one lock."""
        reset_config_for_tests()

        def _add(url: str, secret: str):
            def _put(current: object) -> dict[str, str]:
                data = dict(current) if isinstance(current, dict) else {}
                data[url] = secret
                return data
            return lambda: update_config_mapping('api_keys_by_endpoint', _put)

        self._run_barrier_pair(
            _add('https://api.openai.com', 'sk-a'),
            _add('http://localhost:11434', 'sk-b'),
        )
        reset_config_for_tests()
        keys = get_config('api_keys_by_endpoint')
        assert keys.get('https://api.openai.com') == 'sk-a'
        assert keys.get('http://localhost:11434') == 'sk-b'

    def test_save_unchanged_defaults_do_not_rewrite(self):
        """A missing key at its default is not a change, even in a batch."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'custom-model'}, f)
        reset_config_for_tests()
        with open(self.config_path, encoding='utf-8') as f:
            before = f.read()
        with patch.object(get_event_bus(), 'emit') as mock_emit:
            save({'request_timeout': 120, 'temperature': -1, 'text_model': 'custom-model'})
            mock_emit.assert_not_called()
        with open(self.config_path, encoding='utf-8') as f:
            assert f.read() == before

    def test_save_two_changes_leave_other_keys(self):
        """One or two real changes patch those keys and emit once."""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'endpoint': 'http://localhost:8000',
                'text_model': 'keep-me',
                'custom_unrecognized_key': 'stay',
            }, f)
        reset_config_for_tests()
        with patch.object(get_event_bus(), 'emit') as mock_emit:
            save({'image_model': 'flux', 'request_timeout': 60})
        assert mock_emit.call_count == 1
        assert mock_emit.call_args.kwargs['key'] == ''
        assert set(mock_emit.call_args.kwargs['keys']) == {'image_model', 'request_timeout'}
        data = self._load_written()
        assert data == ({
            'endpoint': 'http://localhost:8000',
            'text_model': 'keep-me',
            'custom_unrecognized_key': 'stay',
            'image_model': 'flux',
            'request_timeout': 60,
        })

    def test_get_config_nested_dict_does_not_alias_cache(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"openrouter_chat_extra": {"provider": {"order": ["a"]}}}, f)
        reset_config_for_tests()
        first = get_config("openrouter_chat_extra")
        first["provider"]["order"].append("b")
        second = get_config("openrouter_chat_extra")
        assert second["provider"]["order"] == ["a"]

    def test_get_config_keeps_last_good_cache_when_json_is_unrepairable(self):
        import plugin.framework.config as config_mod

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"text_model": "kept-model"}, f)
        reset_config_for_tests()
        assert get_config("text_model") == "kept-model"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        config_mod._cache.mtime_last_checked = 0
        with patch("plugin.framework.config._try_repair_config_dict", return_value=None):
            assert get_config("text_model") == "kept-model"
        with open(self.config_path, encoding="utf-8") as f:
            assert f.read().startswith("{ not")

    def test_set_config_does_not_replace_unrepairable_json(self):
        original = "{ this is not json, but keep me"
        with open(self.config_path, 'w', encoding='utf-8') as f:
            f.write(original)
        reset_config_for_tests()
        with patch("plugin.framework.config._try_repair_config_dict", return_value=None):
            with pytest.raises(ConfigError):
                save({'text_model': 'should-not-wipe'})
        with open(self.config_path, encoding='utf-8') as f:
            assert f.read() == original

    def test_set_config_invalid_numeric_raises_and_keeps_file(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'chatbot.extend_selection_max_tokens': 1200}, f)
        reset_config_for_tests()

        with pytest.raises(ConfigValidationError):
            save({'chatbot.extend_selection_max_tokens': 'not-a-number'})

        data = self._load_written()
        assert data.get('chatbot.extend_selection_max_tokens') == 1200

    def test_set_config_out_of_range_raises_and_keeps_file(self):
        """module.yaml ints are module keys.

        save coerces with strict=True. An out-of-range number raises
        ConfigValidationError and leaves the file alone.
        """
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump({'text_model': 'keep-me'}, f)
        reset_config_for_tests()

        with pytest.raises(ConfigValidationError):
            save({'chatbot.extend_selection_max_tokens': '1'})
        with pytest.raises(ConfigValidationError):
            save({'chatbot.edit_selection_max_new_tokens': '99999'})

        data = self._load_written()
        assert 'chatbot.extend_selection_max_tokens' not in data
        assert 'chatbot.edit_selection_max_new_tokens' not in data
        assert data.get('text_model') == 'keep-me'
        assert get_config('chatbot.extend_selection_max_tokens') == 1000
        assert get_config('chatbot.edit_selection_max_new_tokens') == 1000
        assert 'temperature' not in data
        assert 'chat_max_tokens' not in data
        assert 'saved_python_scripts' not in data

        # Bounds themselves are still legal, and a real write still omits defaults.
        save({'chatbot.extend_selection_max_tokens': '10'})
        save({'chatbot.edit_selection_max_new_tokens': '4096'})
        data = self._load_written()
        assert data.get('chatbot.extend_selection_max_tokens') == 10
        assert data.get('chatbot.edit_selection_max_new_tokens') == 4096
        assert data.get('text_model') == 'keep-me'
        assert 'temperature' not in data
        assert 'chat_max_tokens' not in data
        assert 'saved_python_scripts' not in data

    def test_set_config_log_level_runtime_default_omitted(self):
        save({'text_model': 'custom-model'})
        save({'core.log_level': get_config('core.log_level')})
        data = self._load_written()
        assert 'core.log_level' not in data
        assert data.get('text_model') == 'custom-model'

    def test_set_config_log_level_non_default_persisted(self):
        save({'core.log_level': 'INFO'})
        data = self._load_written()
        assert data.get('core.log_level') == 'INFO'

    def test_set_config_omits_default_value(self):
        # Setting a value to its default does not write to an empty or non-existent file
        save({'endpoint': 'http://localhost:11434'})
        if os.path.exists(self.config_path):
            data = self._load_written()
            assert 'endpoint' not in data
        assert get_config('endpoint') == 'http://localhost:11434'

    def test_set_config_only_persists_non_defaults(self):
        save({'text_model': 'custom-model'})
        data = self._load_written()
        assert data == {'text_model': 'custom-model'}
        with open(self.config_path, 'r', encoding='utf-8') as f:
            text = f.read()
        assert text.startswith('//')

    def test_parse_config_json_text_strips_schema_comment(self):
        raw = CONFIG_SCHEMA_COMMENT + '{\n    "text_model": "custom-model"\n}\n'
        data = parse_config_json_text(raw)
        assert data == {'text_model': 'custom-model'}

    def test_set_config_reverting_to_default_removes_key(self):
        save({'request_timeout': 60})
        data = self._load_written()
        assert data.get('request_timeout') == 60

        # Resetting back to default (120) removes it from file
        save({'request_timeout': 120})
        data = self._load_written()
        assert 'request_timeout' not in data
        assert get_config('request_timeout') == 120

    def test_is_default_value_types(self):
        from plugin.framework.config_schema import is_default_value

        assert is_default_value('endpoint', 'http://localhost:11434')
        assert is_default_value('endpoint', 'http://localhost:11434/')
        assert not (is_default_value('endpoint', 'https://api.openai.com/v1'))
        assert is_default_value('request_timeout', 120)
        assert is_default_value('request_timeout', '120')
        assert not (is_default_value('request_timeout', 60))
        assert is_default_value('parallel_tool_calls', False)
        assert is_default_value('parallel_tool_calls', 'false')
        assert not (is_default_value('parallel_tool_calls', True))
        assert is_default_value('prompt_lru', [])
        assert not (is_default_value('prompt_lru', ['a']))
        assert not (is_default_value('unknown_key_xyz', 'val'))
        # core.log_level default is DEBUG in a checkout (plugin/tests present), WARN in a shipped OXT
        assert is_default_value('core.log_level', get_config('core.log_level'))
        assert not (is_default_value('core.log_level', 'INFO'))

    def test_future_default_change_applies_when_omitted(self):
        # File only contains user customization for text_model
        save({'text_model': 'my-model'})
        data = self._load_written()
        assert 'chatbot.extend_selection_max_tokens' not in data

        # In current version, get_config returns current default (1000)
        assert get_config('chatbot.extend_selection_max_tokens') == 1000

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
        with _patch_manifest(mock_modules):
            reset_config_for_tests()
            # Because chatbot.extend_selection_max_tokens was not written to disk, the new default is picked up automatically!
            assert get_config('chatbot.extend_selection_max_tokens') == 2000

    def test_set_config_does_not_drop_another_writers_keys(self):
        """A later one-key save must keep keys an earlier writer stored."""
        original = {
            'text_model': 'custom-model',
            'chat_sidebar_mode': 'chat',
            'chat_direct_image': False,
            'writer.track_changes_reviewable': False,
            'writer.require_edit_review': False,
            'writer.edit_review_timeout': 900,
            'doc.edit_review_timeout': 0,
            'scripting.ppt_master_data_path': '',
            'scripting.python_convert_datetime': False,
        }
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(original, f)
        reset_config_for_tests()
        save({'request_timeout': 60})
        data = self._load_written()
        assert data['request_timeout'] == 60
        for key, value in original.items():
            assert data[key] == value

    def test_set_config_non_json_value_raises_config_error(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"text_model": "keep"}, f)
        reset_config_for_tests()
        with pytest.raises(ConfigError) as exc_info:
            save({"custom_blob": {1, 2}})
        assert exc_info.value.code == "CONFIG_SAVE_ERROR"
        data = self._load_written()
        assert data.get("text_model") == "keep"
        assert "custom_blob" not in data


class TestConfigValidateTypeCasting:

    def test_config_validate_type_casting(self):
        from plugin.framework.config_schema import validate

        # Test standard dataclass type casting
        validated = validate({
            "temperature": "0,7",  # String with European decimal comma
            "chat_max_tokens": 16384.0,  # Float instead of int
            "image_steps": "30",  # String int
        })

        assert validated["temperature"] == 0.7
        assert validated["chat_max_tokens"] == 16384
        assert validated["image_steps"] == 30

        # Test dynamic YAML schema type casting (e.g. mcp.mcp_port)
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
        with _patch_manifest(mock_modules):
            validated_extra = validate({
                "mcp.mcp_port": "8765,00",  # German locale format
            })
            assert validated_extra["mcp.mcp_port"] == 8765

    def test_yaml_backed_key_extra_config_type_casting(self):
        from plugin.framework.config_schema import validate

        validated = validate({"chatbot.web_cache_max_mb": "50,0"})
        assert validated["chatbot.web_cache_max_mb"] == 50

    def test_schema_option_label_canonicalization_from_manifest(self):
        from plugin.framework.config_schema import validate

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

        def fake_gettext(msg):
            if msg == "Fast Mode":
                return "Translated Fast"
            return msg

        with _patch_manifest(mock_modules), \
             patch("plugin.framework.config_schema._", side_effect=fake_gettext):
            validated = validate({"demo.mode": "Translated Fast"})

        assert validated["demo.mode"] == "fast"

    def test_config_validation_constraints(self):
        from plugin.framework.config_schema import validate

        # temperature > 1.0
        with pytest.raises(ConfigValidationError) as ctx:
            validate({"temperature": 1.5})
        assert ctx.value.code == "INVALID_TEMPERATURE"

        # chat_max_tokens < 0
        with pytest.raises(ConfigValidationError) as ctx:
            validate({"chat_max_tokens": -5})
        assert ctx.value.code == "INVALID_CHAT_MAX_TOKENS"

        # request_timeout <= 0
        with pytest.raises(ConfigValidationError) as ctx:
            validate({"request_timeout": 0})
        assert ctx.value.code == "INVALID_REQUEST_TIMEOUT"

        # endpoint preset resolution
        validated = validate({"endpoint": "OpenRouter"})
        assert validated["endpoint"] == "https://openrouter.ai/api"

    def test_validate_api_config_without_model_lru(self):
        with patch.dict(sys.modules, {"plugin.framework.model_lru": None}):
            ok, err = validate_api_config({
                "endpoint": "https://example.invalid",
                "model": "glm-5.2",
            })
        assert ok
        assert err == ""


def test_get_config_before_init_warns_once(caplog):
    reset_config_for_tests()
    with patch("plugin.framework.config.get_config_file_path", side_effect=ConfigError("no path", code="CONFIG_PATH_ERROR")):
        with caplog.at_level(logging.WARNING):
            assert get_config("text_model") == ""
            assert get_config("text_model") == ""
            warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING and "before init_config" in rec.message]
            assert len(warnings) == 1

            assert get_config("request_timeout") == 120
            warnings_after = [rec for rec in caplog.records if rec.levelno == logging.WARNING and "before init_config" in rec.message]
            assert len(warnings_after) == 1


def test_set_config_raises_when_path_empty():
    with patch("plugin.framework.config.get_config_file_path", return_value=""):
        with pytest.raises(ConfigError) as exc:
            save({"text_model": "x"})
    assert exc.value.code == "CONFIG_PATH_ERROR"


def test_second_config_backup_keeps_the_first_copy(tmp_path):
    from plugin.framework.config import _backup_config_file

    path = tmp_path / "writeragent.json"
    path.write_text("first", encoding="utf-8")
    first = _backup_config_file(str(path))
    assert first is not None and first.endswith(".bak")
    path.write_text("second", encoding="utf-8")
    second = _backup_config_file(str(path))
    assert second is not None and second != first
    assert (tmp_path / "writeragent.json.bak").read_text(encoding="utf-8") == "first"
    assert open(second, encoding="utf-8").read() == "second"


def test_resolve_config_path_from_ctx_rejects_mock_and_invalid_user_config():
    # 1. Raw MagicMock context creates MagicMock UserConfig
    with pytest.raises(ConfigError) as exc1:
        _resolve_config_path_from_ctx(MagicMock())
    assert str(exc1.value) == "Invalid or missing UserConfig path setting"
    assert exc1.value.code == "CONFIG_PATH_ERROR"

    # 2. Context returning empty string for UserConfig
    ctx_empty = MagicMock()
    ps = MagicMock()
    ps.UserConfig = ""
    ctx_empty.getServiceManager.return_value.createInstanceWithContext.return_value = ps
    with pytest.raises(ConfigError) as exc2:
        _resolve_config_path_from_ctx(ctx_empty)
    assert exc2.value.code == "CONFIG_PATH_ERROR"

    # 3. Context returning valid string path succeeds
    ctx_valid = MagicMock()
    ps_valid = MagicMock()
    ps_valid.UserConfig = "/home/user/.config/libreoffice/4/user"
    ctx_valid.getServiceManager.return_value.createInstanceWithContext.return_value = ps_valid
    path = _resolve_config_path_from_ctx(ctx_valid)
    assert path == os.path.join("/home/user/.config/libreoffice/4/user", "writeragent.json")


def test_rename_migrates_model_stt_model_and_flat_keys(tmp_path):
    reset_config_for_tests()
    cfg_file = tmp_path / "writeragent.json"
    with patch("plugin.framework.config.get_config_file_path", return_value=str(cfg_file)):
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({
                "model": "legacy-chat-model",
                "stt_model": "whisper-legacy",
                "deep_research_max_rounds": 5,
            }, f)
        reset_config_for_tests()
        assert get_config("text_model") == "legacy-chat-model"
        assert get_config("audio.stt_model") == "whisper-legacy"
        assert get_config("chatbot.deep_research_max_rounds") == 5

        written = parse_config_json_text(cfg_file.read_text(encoding="utf-8"))
        assert "model" not in written
        assert "stt_model" not in written
        assert "deep_research_max_rounds" not in written
        assert written.get("text_model") == "legacy-chat-model"
        assert written.get("audio.stt_model") == "whisper-legacy"
        assert written.get("chatbot.deep_research_max_rounds") == 5


def test_rename_migrates_agy_model_to_agent_backend_model(tmp_path):
    reset_config_for_tests()
    cfg_file = tmp_path / "writeragent.json"
    with patch("plugin.framework.config.get_config_file_path", return_value=str(cfg_file)):
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({"agent_backend.agy_model": "gemini-4-argon-medium"}, f)
        reset_config_for_tests()
        assert get_config("agent_backend.model") == "gemini-4-argon-medium"
        written = parse_config_json_text(cfg_file.read_text(encoding="utf-8"))
        assert "agent_backend.agy_model" not in written
        assert written.get("agent_backend.model") == "gemini-4-argon-medium"

        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({
                "agent_backend.agy_model": "gemini-4-argon-medium",
                "agent_backend.model": "grok-4.6",
            }, f)
        reset_config_for_tests()
        assert get_config("agent_backend.model") == "grok-4.6"
        written2 = parse_config_json_text(cfg_file.read_text(encoding="utf-8"))
        assert "agent_backend.agy_model" not in written2
        assert written2.get("agent_backend.model") == "grok-4.6"


def test_bad_save_rolls_back_cache_and_keeps_disk(tmp_path):
    reset_config_for_tests()
    cfg_file = tmp_path / "writeragent.json"
    with patch("plugin.framework.config.get_config_file_path", return_value=str(cfg_file)):
        save({"text_model": "good-model"})
        disk_before = cfg_file.read_text(encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            save({"temperature": 99.0})
        assert cfg_file.read_text(encoding="utf-8") == disk_before
        assert get_config("text_model") == "good-model"


def test_save_fsyncs_before_replace(tmp_path):
    reset_config_for_tests()
    cfg_file = tmp_path / "writeragent.json"
    order: list[str] = []
    replace_srcs: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def _fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def _replace(src: str, dst: str) -> None:
        order.append("replace")
        replace_srcs.append(src)
        real_replace(src, dst)

    with patch("plugin.framework.config.get_config_file_path", return_value=str(cfg_file)), patch(
        "plugin.framework.config.os.fsync", side_effect=_fsync
    ), patch("plugin.framework.config.os.replace", side_effect=_replace):
        save({"text_model": "m"})

    assert order == ["fsync", "replace"]
    assert len(replace_srcs) == 1
    assert os.path.basename(replace_srcs[0]).startswith(".writeragent-")
    parsed = parse_config_json_text(cfg_file.read_text(encoding="utf-8"))
    assert parsed is not None
    assert parsed.get("text_model") == "m"


def test_atomic_write_text_default_does_not_fsync(tmp_path):
    target = tmp_path / "a.txt"
    with patch("plugin.framework.config.os.fsync") as mock_fsync:
        atomic_write_text(str(target), "x")
    mock_fsync.assert_not_called()
    assert target.read_text(encoding="utf-8") == "x"


def test_save_fsync_error_raises_config_error(tmp_path):
    reset_config_for_tests()
    cfg_file = tmp_path / "writeragent.json"
    with patch("plugin.framework.config.get_config_file_path", return_value=str(cfg_file)), patch(
        "plugin.framework.config.os.fsync", side_effect=OSError(5, "io")
    ):
        with pytest.raises(ConfigError) as exc_info:
            save({"text_model": "m"})
        assert exc_info.value.code == "CONFIG_SAVE_ERROR"
    assert not cfg_file.exists()
    assert list(tmp_path.glob(".writeragent-*")) == []


def test_unknown_key_raises():
    reset_config_for_tests()
    with pytest.raises(ConfigError) as exc_info:
        get_config("nosuchkey")
    assert exc_info.value.code == "CONFIG_KEY_NOT_FOUND"


def _config_json(path):
    body = path.read_text(encoding="utf-8")
    return json.loads(body[body.index("{") :])


def test_save_one_write_one_emit_and_validation_writes_nothing(tmp_path):
    """Batch save: one write, one event. A bad value leaves the file untouched."""

    path = tmp_path / "writeragent.json"
    path.write_text(json.dumps({"text_model": "keep", "python_venv_path": "/old"}), encoding="utf-8")
    reset_config_for_tests()
    try:
        with (
            patch("plugin.framework.config.get_config_file_path", return_value=str(path)),
            patch.object(get_event_bus(), "emit") as emit,
        ):
            save({"text_model": "alpha", "request_timeout": 45})
            assert emit.call_count == 1
            assert emit.call_args.args[0] == "config:changed"
            assert emit.call_args.kwargs["key"] == ""
            assert emit.call_args.kwargs["keys"] == ("text_model", "request_timeout")
            assert emit.call_args.kwargs["old_value"] is None
            assert get_config("text_model") == "alpha"
            assert get_config("request_timeout") == 45
            # An unrelated batch leaves the flat alias alone. Only staging the
            # dotted key pops it (checked below).
            assert _config_json(path).get("scripting.python_venv_path") == "/old"

            emit.reset_mock()
            save({"text_model": "beta"})
            emit.assert_called_once()
            assert emit.call_args.kwargs["key"] == "text_model"
            assert emit.call_args.kwargs["value"] == "beta"

            emit.reset_mock()
            before = path.read_text(encoding="utf-8")
            save({"text_model": "beta", "request_timeout": "45"})
            emit.assert_not_called()
            assert path.read_text(encoding="utf-8") == before

            with pytest.raises(ConfigValidationError):
                save({"temperature": 9, "text_model": "should-not-land"})
            emit.assert_not_called()
            assert path.read_text(encoding="utf-8") == before
            assert get_config("text_model") == "beta"

            path.write_text(
                json.dumps({"text_model": "beta", "python_venv_path": "/old"}),
                encoding="utf-8",
            )
            reset_config_for_tests()
            save({"scripting.python_venv_path": "/opt/venv"})
            written = _config_json(path)
            assert written["scripting.python_venv_path"] == "/opt/venv"
            assert "python_venv_path" not in written
    finally:
        reset_config_for_tests()


class TestValidateApiConfigPlaceholders:

    def test_rejects_connection_failed_placeholder(self):
        ok, err = validate_api_config({
            "endpoint": "https://api.z.ai/api/paas",
            "model": "(Connection failed)",
        })
        assert not (ok)
        assert "valid model" in err.lower()

    def test_accepts_real_model(self):
        ok, err = validate_api_config({
            "endpoint": "https://api.z.ai/api/paas",
            "model": "glm-5.2",
        })
        assert ok
        assert err == ""


def test_resolve_config_path_from_ctx_wraps_uno_attribute_error_as_config_error():
    class _FailingPathSettings:
        @property
        def UserConfig(self):
            raise RuntimeError("non-RuntimeException occurred when accessing an interface type attribute")

    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = _FailingPathSettings()
    with pytest.raises(ConfigError) as exc:
        _resolve_config_path_from_ctx(ctx)
    assert exc.value.code == "CONFIG_PATH_ERROR"


def test_get_config_before_init_returns_default_when_uno_user_config_raises():
    reset_config_for_tests()

    class _FailingPathSettings:
        @property
        def UserConfig(self):
            raise RuntimeError("non-RuntimeException occurred when accessing an interface type attribute")

    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = _FailingPathSettings()
    try:
        with patch("plugin.framework.uno_context.get_ctx", return_value=ctx):
            assert get_config("chatbot.rich_text_control_sidebar") is True
    finally:
        reset_config_for_tests()


def test_api_key_saved_to_keystore_not_file(fake_keystore):
    from plugin.framework.config import api_key_slot_value, get_config_file_path

    url = "https://api.example.com"
    save({"api_keys_by_endpoint": {url: "sk-1"}})

    raw_bytes = open(get_config_file_path(), "rb").read()
    assert b"sk-1" not in raw_bytes
    assert b"keystore:" in raw_bytes
    assert api_key_slot_value(url).startswith("keystore:")
    assert get_api_key_for_endpoint(url) == "sk-1"
    assert fake_keystore.store == {"api_key:https://api.example.com": "sk-1"}


def test_resaving_same_key_keeps_ref(fake_keystore):
    from plugin.framework.config import api_key_slot_value

    url = "https://api.example.com"
    save({"api_keys_by_endpoint": {url: "sk-1"}})
    first_ref = api_key_slot_value(url)

    with patch.object(get_event_bus(), "emit") as emit:
        save({"api_keys_by_endpoint": {url: "sk-1"}})
    assert api_key_slot_value(url) == first_ref
    emit.assert_not_called()


def test_clearing_key_deletes_store_item(fake_keystore):
    from plugin.framework.config import api_key_slot_value

    url = "https://api.example.com"
    save({"api_keys_by_endpoint": {url: "sk-1"}})
    assert "api_key:https://api.example.com" in fake_keystore.store

    save({"api_keys_by_endpoint": {url: ""}})
    assert api_key_slot_value(url) == ""
    assert "api_key:https://api.example.com" not in fake_keystore.store
    assert get_api_key_for_endpoint(url) == ""


def test_store_write_failure_falls_back_to_plain_text(fake_keystore):
    from plugin.framework.config import api_key_slot_value, get_config_file_path

    fake_keystore.fail_set = True
    url = "https://api.example.com"
    save({"api_keys_by_endpoint": {url: "sk-1"}})

    raw_bytes = open(get_config_file_path(), "rb").read()
    assert b"sk-1" in raw_bytes
    assert api_key_slot_value(url) == "sk-1"
    assert get_api_key_for_endpoint(url) == "sk-1"


def test_migrate_moves_all_plaintext_secrets():
    from plugin.framework import keystore
    from plugin.framework.config import (
        api_key_slot_value,
        get_config_file_path,
        get_secret_config,
        migrate_secrets_to_keystore,
    )
    from tests.conftest import FakeKeystoreBackend

    save(
        {
            "api_keys_by_endpoint": {
                "https://one.test": "sk-one",
                "https://two.test": "sk-two",
            },
            "mcp.tunnel_provider_token": "tok",
        }
    )
    keystore.reset_for_tests(FakeKeystoreBackend())
    try:
        migrate_secrets_to_keystore()

        raw_bytes = open(get_config_file_path(), "rb").read()
        assert b"sk-one" not in raw_bytes
        assert b"sk-two" not in raw_bytes
        assert b'"tok"' not in raw_bytes
        assert api_key_slot_value("https://one.test").startswith("keystore:")
        assert api_key_slot_value("https://two.test").startswith("keystore:")
        assert get_config("mcp.tunnel_provider_token").startswith("keystore:")
        assert get_api_key_for_endpoint("https://one.test") == "sk-one"
        assert get_api_key_for_endpoint("https://two.test") == "sk-two"
        assert get_secret_config("mcp.tunnel_provider_token") == "tok"
    finally:
        keystore.reset_for_tests()


def test_migrate_merges_legacy_slot_into_normalized_slot(fake_keystore):
    from plugin.framework.config import get_config_file_path, migrate_secrets_to_keystore

    legacy = "https://openrouter.ai/api/v1"
    save({"api_keys_by_endpoint": {legacy: "sk-or"}})
    migrate_secrets_to_keystore()

    slots = get_config("api_keys_by_endpoint")
    assert legacy not in slots
    assert slots["https://openrouter.ai/api"].startswith("keystore:")
    assert fake_keystore.store == {"api_key:https://openrouter.ai/api": "sk-or"}
    assert get_api_key_for_endpoint(legacy) == "sk-or"
    assert b"sk-or" not in open(get_config_file_path(), "rb").read()


def test_migrate_cleans_up_orphaned_legacy_item(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import migrate_secrets_to_keystore

    legacy = "https://openrouter.ai/api/v1"
    norm = "https://openrouter.ai/api"
    # State left by the earlier migration: the legacy slot became a ref
    # whose item nothing reads, next to a normalized slot with its own key.
    save({"api_keys_by_endpoint": {norm: "sk-new"}})
    fake_keystore.store[f"api_key:{legacy}"] = "sk-old"
    save({"api_keys_by_endpoint": {legacy: keystore.new_ref()}})

    migrate_secrets_to_keystore()

    assert legacy not in get_config("api_keys_by_endpoint")
    assert fake_keystore.store == {f"api_key:{norm}": "sk-new"}
    assert get_api_key_for_endpoint(norm) == "sk-new"


def test_migrate_adopts_orphan_when_normalized_slot_empty(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import migrate_secrets_to_keystore

    legacy = "https://openrouter.ai/api/v1"
    fake_keystore.store[f"api_key:{legacy}"] = "sk-old"
    save({"api_keys_by_endpoint": {legacy: keystore.new_ref()}})

    migrate_secrets_to_keystore()

    assert legacy not in get_config("api_keys_by_endpoint")
    assert fake_keystore.store == {"api_key:https://openrouter.ai/api": "sk-old"}
    assert get_api_key_for_endpoint(legacy) == "sk-old"


def test_has_api_key_does_not_read_keystore(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import has_api_key_for_endpoint

    url = "https://openrouter.ai/api"
    save({"api_keys_by_endpoint": {url: "sk-1"}})
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.locked = True
    assert has_api_key_for_endpoint(url) is True
    assert has_api_key_for_endpoint("https://other.test") is False
    assert fake_keystore.get_calls == 0


def test_panel_model_combo_does_not_read_locked_keystore(fake_keystore):
    from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru
    from plugin.framework import keystore

    url = "https://openrouter.ai/api"
    save({"api_keys_by_endpoint": {url: "sk-1"}})
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.locked = True
    ctrl = MagicMock()
    ctrl.getItemCount.return_value = 0
    populate_combobox_with_lru(ctrl, "", "model_lru", url)
    assert fake_keystore.get_calls == 0


def test_locked_keystore_reports_keyring_locked(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.client.auth import AuthError, resolve_auth_for_config
    from plugin.framework.errors import format_error_message

    url = "https://openrouter.ai/api"
    save({"api_keys_by_endpoint": {url: "sk-1"}})
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.locked = True
    assert get_api_key_for_endpoint(url) == ""
    with pytest.raises(AuthError) as exc:
        resolve_auth_for_config({"endpoint": url, "api_key": ""})
    assert exc.value.code == "keyring_locked"
    assert "locked" in format_error_message(exc.value)

    fake_keystore.locked = False
    keystore.reset_for_tests(fake_keystore)
    assert get_api_key_for_endpoint(url) == "sk-1"


def test_migrate_without_store_leaves_file():
    from plugin.framework.config import get_config_file_path, migrate_secrets_to_keystore

    save({"api_keys_by_endpoint": {"https://one.test": "sk-one"}})
    before = open(get_config_file_path(), "rb").read()
    migrate_secrets_to_keystore()
    after = open(get_config_file_path(), "rb").read()
    assert after == before


@pytest.mark.parametrize("cause", ["missing", "fail_get", "locked"])
def test_clearing_unreadable_secret_removes_ref(fake_keystore, cause):
    from pathlib import Path

    from plugin.framework import keystore
    from plugin.framework.config import api_key_slot_value, get_config, get_config_file_path

    url = "https://api.example.com"
    save({
        "api_keys_by_endpoint": {url: "sk-1"},
        "mcp.tunnel_provider_token": "tok-1",
        "mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "srv-1"}},
    })
    api_ref = api_key_slot_value(url)
    tunnel_ref = get_config("mcp.tunnel_provider_token")
    srv_ref = get_config("mcp_client.servers")["home"]["token"]
    assert all(keystore.is_ref(r) for r in (api_ref, tunnel_ref, srv_ref))

    keystore.reset_for_tests(fake_keystore)
    if cause == "missing":
        fake_keystore.store.clear()
    elif cause == "fail_get":
        fake_keystore.fail_get = True
    elif cause == "locked":
        fake_keystore.locked = True

    save({
        "api_keys_by_endpoint": {url: ""},
        "mcp.tunnel_provider_token": "",
        "mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": ""}},
    })

    assert api_key_slot_value(url) == ""
    assert get_config("mcp.tunnel_provider_token") == ""
    assert "token" not in get_config("mcp_client.servers")["home"]
    raw_file = Path(get_config_file_path()).read_text(encoding="utf-8")
    for ref in (api_ref, tunnel_ref, srv_ref):
        assert ref not in raw_file


def test_field_text_shows_ref_only_when_unreadable(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import (
        api_key_field_text,
        api_key_slot_value,
        entry_secret_field_text,
        get_config,
        secret_config_field_text,
    )

    url = "https://api.example.com"
    assert api_key_field_text(url) == ""
    assert secret_config_field_text("mcp.tunnel_provider_token") == ""
    assert entry_secret_field_text("mcp_client.servers", "home", "token", "") == ""

    save({
        "api_keys_by_endpoint": {url: "sk-1"},
        "mcp.tunnel_provider_token": "tok-1",
        "mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "srv-1"}},
    })
    assert api_key_field_text(url) == "sk-1"
    assert secret_config_field_text("mcp.tunnel_provider_token") == "tok-1"
    srv_raw = get_config("mcp_client.servers")["home"]["token"]
    assert entry_secret_field_text("mcp_client.servers", "home", "token", srv_raw) == "srv-1"

    keystore.reset_for_tests(fake_keystore)
    fake_keystore.locked = True
    assert api_key_field_text(url) == api_key_slot_value(url)
    assert secret_config_field_text("mcp.tunnel_provider_token") == get_config("mcp.tunnel_provider_token")
    assert entry_secret_field_text("mcp_client.servers", "home", "token", srv_raw) == srv_raw


def test_migrate_keeps_orphan_when_config_write_fails(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import migrate_secrets_to_keystore

    legacy_url = "https://openrouter.ai/api/v1"
    legacy_name = f"api_key:{legacy_url}"
    fake_keystore.store[legacy_name] = "sk-old"
    save({"api_keys_by_endpoint": {legacy_url: "keystore:1e9acc01"}})
    keystore.reset_for_tests(fake_keystore)

    with (
        patch("plugin.framework.config._write_config_file", side_effect=OSError("disk full")),
        pytest.raises(ConfigError),
    ):
        migrate_secrets_to_keystore()

    assert fake_keystore.store[legacy_name] == "sk-old"


def test_secret_fields_route_server_tokens_through_keystore(fake_keystore):
    from pathlib import Path

    from plugin.framework import keystore
    from plugin.framework.config import get_config, get_config_file_path, resolve_entry_secret

    save({"mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "tok-secret-1"}}})
    raw_file = Path(get_config_file_path()).read_text(encoding="utf-8")
    assert "tok-secret-1" not in raw_file
    servers = get_config("mcp_client.servers")
    assert keystore.is_ref(servers["home"]["token"])
    assert resolve_entry_secret("mcp_client.servers", "home", "token", servers["home"]["token"]) == "tok-secret-1"

    # Saving the same plain text again adds no set_calls.
    calls_before = fake_keystore.set_calls
    save({"mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "tok-secret-1"}}})
    assert fake_keystore.set_calls == calls_before

    # Renaming re-stores under the new name and deletes the old item.
    save({"mcp_client.servers": {"work": {"url": "http://localhost:8000/mcp", "token": "tok-secret-1"}}})
    assert "mcp_client.servers:home.token" not in fake_keystore.store
    assert fake_keystore.store.get("mcp_client.servers:work.token") == "tok-secret-1"

    # Removing the server deletes its item.
    save({"mcp_client.servers": {}})
    assert "mcp_client.servers:work.token" not in fake_keystore.store


def test_migrate_moves_plain_server_tokens(fake_keystore):
    from pathlib import Path

    from plugin.framework import keystore
    from plugin.framework.config import get_config, get_config_file_path, migrate_secrets_to_keystore, resolve_entry_secret

    keystore.reset_for_tests(None)
    save({"mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "tok-plain"}}})
    assert "tok-plain" in Path(get_config_file_path()).read_text(encoding="utf-8")

    keystore.reset_for_tests(fake_keystore)
    migrate_secrets_to_keystore()

    assert "tok-plain" not in Path(get_config_file_path()).read_text(encoding="utf-8")
    servers = get_config("mcp_client.servers")
    assert keystore.is_ref(servers["home"]["token"])
    assert resolve_entry_secret("mcp_client.servers", "home", "token", servers["home"]["token"]) == "tok-plain"


def test_server_token_stays_plain_without_store():
    from pathlib import Path

    from plugin.framework import keystore
    from plugin.framework.config import get_config, get_config_file_path, resolve_entry_secret

    keystore.reset_for_tests(None)
    try:
        save({"mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "tok-plain"}}})
        assert "tok-plain" in Path(get_config_file_path()).read_text(encoding="utf-8")
        servers = get_config("mcp_client.servers")
        assert servers["home"]["token"] == "tok-plain"
        assert resolve_entry_secret("mcp_client.servers", "home", "token", servers["home"]["token"]) == "tok-plain"
    finally:
        keystore.reset_for_tests(None)


def test_server_token_strips_whitespace_from_entry_name(fake_keystore):
    from plugin.framework.config import get_config, resolve_entry_secret

    save({"mcp_client.servers": {" home": {"url": "http://localhost:8000/mcp", "token": "tok-ws"}}})
    servers = get_config("mcp_client.servers")
    assert fake_keystore.store.get("mcp_client.servers:home.token") == "tok-ws"
    assert resolve_entry_secret("mcp_client.servers", "home", "token", servers[" home"]["token"]) == "tok-ws"


def test_renaming_whitespace_entry_to_stripped_name_keeps_secret(fake_keystore):
    from plugin.framework.config import get_config, resolve_entry_secret

    save({"mcp_client.servers": {" home": {"url": "http://localhost:8000/mcp", "token": "t"}}})
    save({"mcp_client.servers": {"home": {"url": "http://localhost:8000/mcp", "token": "t"}}})
    servers = get_config("mcp_client.servers")
    assert resolve_entry_secret("mcp_client.servers", "home", "token", servers["home"]["token"]) == "t"


def test_fill_api_key_reads_only_a_missing_key(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import fill_api_key, save
    from plugin.framework.send_cancellation import SendCancellation

    endpoint = "https://openrouter.ai/api"
    save({"api_keys_by_endpoint": {endpoint: "sk-1"}})
    keystore.reset_for_tests(fake_keystore)

    scope = SendCancellation()
    missing_cfg = {"endpoint": endpoint}
    fill_api_key(missing_cfg, cancel=scope)
    assert missing_cfg["api_key"] == "sk-1"
    assert fake_keystore.get_calls == 1
    assert fake_keystore.last_cancel is scope

    present_cfg = {"endpoint": endpoint, "api_key": ""}
    fill_api_key(present_cfg)
    assert present_cfg["api_key"] == ""
    assert fake_keystore.get_calls == 1


def test_locked_keystore_reported_for_openwebui_flagged_endpoint(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.client.auth import AuthError, resolve_auth_for_config

    save({"api_keys_by_endpoint": {"https://openrouter.ai/api": "sk-1"}})
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.locked = True

    assert get_api_key_for_endpoint("https://openrouter.ai/api") == ""
    with pytest.raises(AuthError) as exc_info:
        resolve_auth_for_config(
            {"endpoint": "https://openrouter.ai/api/v1", "api_key": "", "is_openwebui": True}
        )
    assert exc_info.value.code == "keyring_locked"


def test_field_text_getters_and_cached_api_key_make_zero_backend_calls(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import (
        api_key_field_text,
        cached_api_key_for_endpoint,
        entry_secret_field_text,
        get_config,
        save,
        secret_config_field_text,
    )

    endpoint = "https://openrouter.ai/api"
    save({
        "api_keys_by_endpoint": {endpoint: "sk-ep"},
        "mcp.tunnel_provider_token": "tok-tun",
        "mcp_client.servers": {"s": {"url": "http://localhost:8000", "token": "tok-srv"}},
    })
    raw_srv_token = get_config("mcp_client.servers")["s"]["token"]
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.get_calls = 0

    assert keystore.is_ref(api_key_field_text(endpoint))
    assert keystore.is_ref(secret_config_field_text("mcp.tunnel_provider_token"))
    assert keystore.is_ref(entry_secret_field_text("mcp_client.servers", "s", "token", raw_srv_token))
    assert cached_api_key_for_endpoint(endpoint) == ""
    assert fake_keystore.get_calls == 0


def test_migrate_legacy_slot_under_store_latch_keeps_slot_and_orphan(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.config import get_config, migrate_secrets_to_keystore, save

    fake_keystore.one_lock = True
    legacy = "https://openrouter.ai/api/v1"
    save({
        "api_keys_by_endpoint": {legacy: "SECRET"},
        "mcp.tunnel_provider_token": "tok-tun",
    })
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.locked = True
    assert keystore.get_secret("mcp.tunnel_provider_token") is None

    migrate_secrets_to_keystore()
    slots = get_config("api_keys_by_endpoint")
    assert legacy in slots
    assert f"api_key:{legacy}" in fake_keystore.store


def test_cold_main_thread_get_api_key_for_endpoint_still_reads_os(fake_keystore):
    from plugin.framework import keystore

    endpoint = "https://openrouter.ai/api"
    save({"api_keys_by_endpoint": {endpoint: "sk-real"}})
    keystore.reset_for_tests(fake_keystore)
    assert get_api_key_for_endpoint(endpoint) == "sk-real"
