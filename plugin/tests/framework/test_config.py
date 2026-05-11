
import json
import pytest
import sys
import os
import json
import tempfile
import unittest
import queue
import time
import pytest
from plugin.framework.config import ConfigService, ConfigAccessError
from plugin.framework.event_bus import EventBus
from plugin.framework.constants import get_plugin_dir
from unittest.mock import MagicMock, patch
from plugin.tests.testing_utils import setup_uno_mocks
from plugin.framework.config import get_image_model, set_image_model, get_api_key_for_endpoint, set_api_key_for_endpoint, update_lru_history, get_config, get_config_int, set_config, endpoint_url_suitable_for_v1_models_fetch
from plugin.framework.event_bus import global_event_bus
from unittest.mock import MagicMock, patch
from plugin.framework.worker_pool import run_in_background
from plugin.framework.errors import WorkerPoolError
from plugin.framework.async_stream import StreamQueueKind, run_stream_drain_loop
from plugin.framework.logging import SafeLogger, safe_log_exception
from plugin.framework.config import normalize_endpoint_url
'Tests for plugin.framework.config (ConfigService + ModuleConfigProxy).'

@pytest.fixture
def config_dir(tmp_path):
    'Provide a temp dir for config file.'
    return tmp_path

@pytest.fixture
def config_svc(config_dir):
    'ConfigService with a temp config path (bypasses UNO).'
    svc = ConfigService()
    svc._config_path = str((config_dir / 'writeragent.json'))
    return svc

@pytest.fixture
def manifest():
    'Sample manifest data.'
    return {'mcp': {'config': {'port': {'type': 'int', 'default': 8766, 'public': True}, 'host': {'type': 'string', 'default': 'localhost', 'public': True}, 'ssl_key': {'type': 'string', 'default': '', 'public': False}}}, 'chatbot': {'config': {'max_tool_rounds': {'type': 'int', 'default': 15, 'public': False}}}}

class TestDefaults():

    def test_get_returns_default(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y: None)
        try:
            assert (config_svc.get('mcp.port') == 8766)
            assert (config_svc.get('mcp.host') == 'localhost')
        finally:
            c.get_config = old_get_config

    def test_get_returns_none_for_unknown(self, config_svc):
        assert (config_svc.get('nonexistent.key') is None)

    def test_register_default(self, config_svc):
        config_svc.register_default('custom.key', 42)
        assert (config_svc.get('custom.key') == 42)

class TestSetGet():

    def test_set_and_get(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.port', 9000)
        assert (config_svc.get('mcp.port') == 9000)

    def test_set_persists_to_file(self, config_svc, config_dir, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.port', 9000)
        with open((config_dir / 'writeragent.json')) as f:
            data = json.load(f)
        assert (data['mcp.port'] == 9000)

    def test_remove(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.port', 9000)
        config_svc.remove('mcp.port')
        assert (config_svc.get('mcp.port') == 8766)

    def test_get_dict(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.port', 9000)
        d = config_svc.get_dict()
        assert (d['mcp.port'] == 9000)

class TestAccessControl():

    def test_read_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert (config_svc.get('mcp.port', caller_module='mcp') == 8766)

    def test_read_public_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert (config_svc.get('mcp.port', caller_module='chatbot') == 8766)

    def test_read_private_key_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        with pytest.raises(ConfigAccessError, match='cannot read private'):
            config_svc.get('mcp.ssl_key', caller_module='chatbot')

    def test_write_own_key_ok(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        config_svc.set('mcp.port', 9000, caller_module='mcp')
        assert (config_svc.get('mcp.port') == 9000)

    def test_write_other_key_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        with pytest.raises(ConfigAccessError, match='cannot write'):
            config_svc.set('mcp.port', 9000, caller_module='chatbot')

    def test_no_caller_no_restriction(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        assert (config_svc.get('mcp.ssl_key') == '')

class TestEvents():

    def test_config_changed_event(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)
        events = []
        bus.subscribe('config:changed', (lambda **kw: events.append(kw)))
        config_svc.set('mcp.port', 9000)
        assert (len(events) == 1)
        assert (events[0]['key'] == 'mcp.port')
        assert (events[0]['value'] == 9000)
        assert (events[0]['old_value'] == 8766)

    def test_no_event_when_value_unchanged(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        bus = EventBus()
        config_svc.set_events(bus)
        config_svc.set('mcp.port', 8766)
        events = []
        bus.subscribe('config:changed', (lambda **kw: events.append(kw)))
        config_svc.set('mcp.port', 8766)
        assert (events == [])

class TestModuleConfigProxy():

    def test_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('mcp')
        assert (proxy.get('port') == 8766)

    def test_set_auto_prefix(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('mcp')
        proxy.set('port', 9000)
        assert (proxy.get('port') == 9000)

    def test_cross_module_read_public(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('chatbot')
        assert (proxy.get('mcp.port') == 8766)

    def test_cross_module_read_private_denied(self, config_svc, manifest):
        config_svc.set_manifest(manifest)
        proxy = config_svc.proxy_for('chatbot')
        with pytest.raises(ConfigAccessError):
            proxy.get('mcp.ssl_key')

    def test_default_fallback(self, config_svc, manifest):
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y: None)
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for('mcp')
            assert (proxy.get('nonexistent', default='fallback') == 'fallback')
        finally:
            c.get_config = old_get_config

    def test_proxy_remove(self, config_svc, manifest):
        'Remove via ModuleConfigProxy (proxy.remove).'
        import plugin.framework.config as c
        old_get_config = c.get_config
        c.get_config = (lambda x, y: None)
        try:
            config_svc.set_manifest(manifest)
            proxy = config_svc.proxy_for('mcp')
            proxy.set('port', 9000)
            proxy.remove('port')
            assert (proxy.get('port') == 8766)
        finally:
            c.get_config = old_get_config
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
        self.mock_get = self.get_patcher.start()
        self.mock_set = self.set_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()

    def test_set_image_model_aihorde(self):
        self.config_data['image_provider'] = 'aihorde'
        with patch.object(global_event_bus, 'emit') as mock_emit:
            set_image_model(self.ctx, 'new-horde-model')
            self.assertEqual(self.config_data.get('aihorde_model'), 'new-horde-model')
            self.assertIsNone(self.config_data.get('image_model'))
            mock_emit.assert_not_called()

    def test_set_image_model_endpoint(self):
        self.config_data['image_provider'] = 'endpoint'
        with patch('plugin.framework.config.update_lru_history') as mock_lru, patch.object(global_event_bus, 'emit') as mock_emit:
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

    def test_update_lru_history_scoping(self):
        update_lru_history(self.ctx, 'item1', 'model_lru', 'http://localhost')
        self.assertEqual(self.config_data.get('model_lru@http://localhost'), ['item1'])
        update_lru_history(self.ctx, 'item2', 'prompt_lru', '')
        self.assertEqual(self.config_data.get('prompt_lru'), ['item2'])
        for i in range(5):
            update_lru_history(self.ctx, f'item{i}', 'test_lru', 'ep', max_items=3)
        self.assertEqual(self.config_data.get('test_lru@ep'), ['item4', 'item3', 'item2'])
        update_lru_history(self.ctx, 'item2', 'test_lru', 'ep', max_items=3)
        self.assertEqual(self.config_data.get('test_lru@ep'), ['item2', 'item4', 'item3'])

    def test_update_lru_history_skips_when_list_unchanged(self):
        self.config_data['prompt_lru'] = ['first', 'second']
        self.mock_set.reset_mock()
        update_lru_history(self.ctx, 'first', 'prompt_lru', '')
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

class TestEndpointUrlSuitableForModelFetch(unittest.TestCase):

    def test_incomplete_or_invalid_urls_rejected(self):
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch(''))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch('http:/'))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch('http://'))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch('ftp://api.openai.com'))
        self.assertFalse(endpoint_url_suitable_for_v1_models_fetch('not-a-url'))

    def test_complete_urls_accepted(self):
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch('http://localhost:1234'))
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch('https://api.openai.com/v1'))
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch('http://127.0.0.1:11434'))
        self.assertTrue(endpoint_url_suitable_for_v1_models_fetch('http://[::1]:8080'))

class TestPopulateComboboxWithLruFetchOptions(unittest.TestCase):
    'populate_combobox_with_lru(skip_remote_fetch / remote_models) must not call fetch_available_models.'

    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}

        def mock_get_config(ctx, key):
            return self.config_data.get(key, '')

        def mock_set_config(ctx, key, value):
            self.config_data[key] = value
        self.get_patcher = patch('plugin.framework.config.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.framework.config.set_config', side_effect=mock_set_config)
        self.get_patcher.start()
        self.set_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()

    def test_skip_remote_fetch_does_not_call_fetch(self):
        from plugin.framework.config import populate_combobox_with_lru
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.config.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'http://localhost:8080', skip_remote_fetch=True)
            mock_fetch.assert_not_called()

    def test_remote_models_does_not_call_fetch(self):
        from plugin.framework.config import populate_combobox_with_lru
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.config.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'http://localhost:8080', remote_models=['m1', 'm2'])
            mock_fetch.assert_not_called()
            ctrl.addItems.assert_called()
            items = ctrl.addItems.call_args[0][0]
            self.assertIn('m1', items)
            self.assertIn('m2', items)

    def test_together_empty_lru_merges_default_text_model(self):
        'Massive providers skip /v1/models in populate_combobox_with_lru; defaults must still appear.'
        from plugin.framework.config import populate_combobox_with_lru
        self.config_data['model_lru@https://api.together.xyz'] = []
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.config.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'https://api.together.xyz', skip_remote_fetch=True)
            mock_fetch.assert_not_called()
        ctrl.addItems.assert_called()
        items = ctrl.addItems.call_args[0][0]
        self.assertIn('openai/gpt-oss-120b', items)

    def test_empty_current_val_uses_lru_head_after_sidebar_style_pick(self):
        'Simulates Settings _apply_dropdowns passing "" — active pick must stay LRU head so setText is not a stale model.'
        from plugin.framework.config import populate_combobox_with_lru
        ep = 'http://localhost:8080'
        self.config_data[f'model_lru@{ep}'] = ['picked-model', 'other-model']
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', ep, skip_remote_fetch=True)
        ctrl.setText.assert_called_with('picked-model')

class TestFetchAvailableModelsCache(unittest.TestCase):
    '_model_fetch_cache is process-wide; same normalized endpoint hits HTTP once.'

    def tearDown(self):
        import plugin.framework.config as cfg
        keys_to_del = [k for k in cfg._model_fetch_cache if (('127.0.0.1:58901' in k) or ('127.0.0.1:58902' in k) or ('127.0.0.1:58903' in k) or ('127.0.0.1:58904' in k) or ('127.0.0.1:58905' in k))]
        for k in keys_to_del:
            del cfg._model_fetch_cache[k]

    def test_second_call_does_not_http(self):
        from plugin.framework import config as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'alpha'}]}
            r1 = cfg.fetch_available_models('http://127.0.0.1:58901')
            r2 = cfg.fetch_available_models('http://127.0.0.1:58901')
            self.assertEqual(r1, ['alpha'])
            self.assertEqual(r2, ['alpha'])
            self.assertEqual(mock_sync.call_count, 1)

    def test_normalized_url_shares_cache_entry(self):
        from plugin.framework import config as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'beta'}]}
            cfg.fetch_available_models('http://127.0.0.1:58902/')
            cfg.fetch_available_models('http://127.0.0.1:58902')
            self.assertEqual(mock_sync.call_count, 1)

    def test_fetch_available_models_sends_bearer_when_ctx_and_api_key(self):
        'GET /v1/models must use the same per-endpoint key as chat (LocalAI, etc.).'
        from plugin.framework import config as cfg
        from plugin.framework.config import normalize_endpoint_url
        ctx = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'writeragent.json')
            endpoint = 'http://127.0.0.1:58903'
            norm = normalize_endpoint_url(endpoint)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'api_keys_by_endpoint': {norm: 'secret-token'}}, f)

            def mock_config_path(c):
                return config_path
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
                cfg._cached_config_dict = None
                cfg._cached_config_mtime = 0
                cfg._cached_config_mtime_last_checked = 0.0
                for k in list(cfg._model_fetch_cache):
                    if ('58903' in k):
                        del cfg._model_fetch_cache[k]
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'm1'}]}
                    r = cfg.fetch_available_models(endpoint, ctx)
                    self.assertEqual(r, ['m1'])
                    mock_sync.assert_called_once()
                    (_args, kwargs) = mock_sync.call_args
                    headers = kwargs.get('headers')
                    self.assertIsInstance(headers, dict)
                    self.assertEqual(headers.get('Authorization'), 'Bearer secret-token')

    def test_model_fetch_cache_key_differs_for_override(self):
        from plugin.framework import config as cfg
        from unittest.mock import MagicMock
        ctx = MagicMock()
        url = 'http://127.0.0.1:58903/v1/models'
        base = 'http://127.0.0.1:58903'
        with patch.object(cfg, 'get_api_key_for_endpoint', return_value='saved'):
            k_saved = cfg._model_fetch_cache_key(url, ctx, base, None)
            k_a = cfg._model_fetch_cache_key(url, ctx, base, 'typed-a')
            k_b = cfg._model_fetch_cache_key(url, ctx, base, 'typed-b')
        self.assertEqual(k_saved, f'{url}saved')
        self.assertEqual(k_a, f'{url}typed-a')
        self.assertEqual(k_b, f'{url}typed-b')

    def test_fetch_available_models_override_used_not_config_file(self):
        'Settings passes live api_key field; override must win over api_keys_by_endpoint.'
        from plugin.framework import config as cfg
        from plugin.framework.config import normalize_endpoint_url
        ctx = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'writeragent.json')
            endpoint = 'http://127.0.0.1:58904'
            norm = normalize_endpoint_url(endpoint)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'api_keys_by_endpoint': {norm: 'from-config-only'}}, f)

            def mock_config_path(c):
                return config_path
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
                cfg._cached_config_dict = None
                cfg._cached_config_mtime = 0
                cfg._cached_config_mtime_last_checked = 0.0
                for k in list(cfg._model_fetch_cache):
                    if ('58904' in k):
                        del cfg._model_fetch_cache[k]
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'm1'}]}
                    r = cfg.fetch_available_models(endpoint, ctx, api_key_override='from-override')
                    self.assertEqual(r, ['m1'])
                    mock_sync.assert_called_once()
                    (_args, kwargs) = mock_sync.call_args
                    headers = kwargs.get('headers')
                    self.assertIsInstance(headers, dict)
                    self.assertEqual(headers.get('Authorization'), 'Bearer from-override')

    def test_fetch_override_and_saved_key_separate_cache(self):
        from plugin.framework import config as cfg
        from plugin.framework.config import normalize_endpoint_url
        ctx = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'writeragent.json')
            endpoint = 'http://127.0.0.1:58905'
            norm = normalize_endpoint_url(endpoint)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'api_keys_by_endpoint': {norm: 'key-a'}}, f)

            def mock_config_path(c):
                return config_path
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
                cfg._cached_config_dict = None
                cfg._cached_config_mtime = 0
                cfg._cached_config_mtime_last_checked = 0.0
                for k in list(cfg._model_fetch_cache):
                    if ('58905' in k):
                        del cfg._model_fetch_cache[k]
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'x'}]}
                    cfg.fetch_available_models(endpoint, ctx)
                    cfg.fetch_available_models(endpoint, ctx, api_key_override='key-b')
                    self.assertEqual(mock_sync.call_count, 2)

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
        from plugin.framework.config import get_config

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

class TestNormalizeEndpointUrl():

    def test_strips_trailing_v1(self):
        assert (normalize_endpoint_url('https://api.example.com/v1') == 'https://api.example.com')
        assert (normalize_endpoint_url('https://api.example.com/v1/') == 'https://api.example.com')
        assert (normalize_endpoint_url('https://openrouter.ai/api/v1') == 'https://openrouter.ai/api')

    def test_preserves_v1beta_and_similar(self):
        u = 'https://generativelanguage.googleapis.com/v1beta/openai'
        assert (normalize_endpoint_url(u) == u)

    def test_empty_and_whitespace(self):
        assert (normalize_endpoint_url('') == '')
        assert (normalize_endpoint_url('  ') == '')
