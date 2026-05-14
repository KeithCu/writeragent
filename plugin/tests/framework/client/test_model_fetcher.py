import unittest
import os
import json
import tempfile
from unittest.mock import MagicMock, patch
from plugin.framework.client.model_fetcher import endpoint_url_suitable_for_v1_models_fetch

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


class TestFetchAvailableModelsCache(unittest.TestCase):
    '_model_fetch_cache is process-wide; same normalized endpoint hits HTTP once.'

    def tearDown(self):
        import plugin.framework.client.model_fetcher as cfg
        keys_to_del = [k for k in cfg._model_fetch_cache if (('127.0.0.1:5890' in k))]
        for k in keys_to_del:
            del cfg._model_fetch_cache[k]

    def test_second_call_does_not_http(self):
        from plugin.framework.client import model_fetcher as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'alpha'}]}
            r1 = cfg.fetch_available_models('http://127.0.0.1:58901')
            r2 = cfg.fetch_available_models('http://127.0.0.1:58901')
            self.assertEqual(r1, ['alpha'])
            self.assertEqual(r2, ['alpha'])
            self.assertEqual(mock_sync.call_count, 1)

    def test_normalized_url_shares_cache_entry(self):
        from plugin.framework.client import model_fetcher as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'beta'}]}
            cfg.fetch_available_models('http://127.0.0.1:58902/')
            cfg.fetch_available_models('http://127.0.0.1:58902')
            self.assertEqual(mock_sync.call_count, 1)

    def test_fetch_available_models_sends_bearer_when_ctx_and_api_key(self):
        'GET /v1/models must use the same per-endpoint key as chat (LocalAI, etc.).'
        from plugin.framework.client import model_fetcher as cfg
        ctx = MagicMock()
        endpoint = 'http://127.0.0.1:58903'
        
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value='secret-token'):
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
        from plugin.framework.client import model_fetcher as cfg
        ctx = MagicMock()
        url = 'http://127.0.0.1:58906/v1/models'
        base = 'http://127.0.0.1:58906'
        with patch.object(cfg, 'get_api_key_for_endpoint', return_value='saved'):
            k_saved = cfg._model_fetch_cache_key(url, ctx, base, None)
            k_a = cfg._model_fetch_cache_key(url, ctx, base, 'typed-a')
            k_b = cfg._model_fetch_cache_key(url, ctx, base, 'typed-b')
        self.assertEqual(k_saved, f'{url}\x1fsaved')
        self.assertEqual(k_a, f'{url}\x1ftyped-a')
        self.assertEqual(k_b, f'{url}\x1ftyped-b')

    def test_fetch_available_models_override_used_not_config_file(self):
        'Settings passes live api_key field; override must win over api_keys_by_endpoint.'
        from plugin.framework.client import model_fetcher as cfg
        from plugin.framework.url_utils import normalize_endpoint_url
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
                import plugin.framework.config as real_config
                real_config._cached_config_dict = None
                real_config._cached_config_mtime = 0
                real_config._cached_config_mtime_last_checked = 0.0
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
        from plugin.framework.client import model_fetcher as cfg
        from plugin.framework.url_utils import normalize_endpoint_url
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
                import plugin.framework.config as real_config
                real_config._cached_config_dict = None
                real_config._cached_config_mtime = 0
                real_config._cached_config_mtime_last_checked = 0.0
                for k in list(cfg._model_fetch_cache):
                    if ('58905' in k):
                        del cfg._model_fetch_cache[k]
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'x'}]}
                    cfg.fetch_available_models(endpoint, ctx)
                    cfg.fetch_available_models(endpoint, ctx, api_key_override='key-b')
                    self.assertEqual(mock_sync.call_count, 2)
