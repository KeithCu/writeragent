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
            cfg._model_fetch_image_cache.pop(k, None)

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
                r = cfg.fetch_available_models(endpoint)
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
            k_saved = cfg._model_fetch_cache_key(url, base, None)
            k_a = cfg._model_fetch_cache_key(url, base, 'typed-a')
            k_b = cfg._model_fetch_cache_key(url, base, 'typed-b')
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

            def mock_config_path():
                return config_path
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
                import plugin.framework.config as real_config
                real_config._cached_config_dict = None
                real_config._cached_config_mtime = 0
                real_config._cached_config_mtime_last_checked = 0.0
                for k in list(cfg._model_fetch_cache):
                    if ('58904' in k):
                        del cfg._model_fetch_cache[k]
                        cfg._model_fetch_image_cache.pop(k, None)
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'm1'}]}
                    r = cfg.fetch_available_models(endpoint, api_key_override='from-override')
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

            def mock_config_path():
                return config_path
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
                import plugin.framework.config as real_config
                real_config._cached_config_dict = None
                real_config._cached_config_mtime = 0
                real_config._cached_config_mtime_last_checked = 0.0
                for k in list(cfg._model_fetch_cache):
                    if ('58905' in k):
                        del cfg._model_fetch_cache[k]
                        cfg._model_fetch_image_cache.pop(k, None)
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'x'}]}
                    cfg.fetch_available_models(endpoint)
                    cfg.fetch_available_models(endpoint, api_key_override='key-b')
                    self.assertEqual(mock_sync.call_count, 2)


class TestGetModelCapabilityOpenRouter(unittest.TestCase):
    def test_nitro_suffix_matches_curated_default(self):
        from plugin.framework.client.model_fetcher import get_model_capability
        from plugin.framework.constants import ModelCapability

        caps = get_model_capability('openai/gpt-oss-120b:nitro', 'https://openrouter.ai/api')
        self.assertTrue(isinstance(caps, int) and (caps & ModelCapability.TOOLS))


class TestHasNativeAudio(unittest.TestCase):
    def test_audio_only_stt_model_is_not_native_audio(self):
        from plugin.framework.client.model_fetcher import has_native_audio

        ctx = MagicMock()
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            result = has_native_audio('mistralai/voxtral-mini-transcribe', 'https://openrouter.ai/api')
        self.assertIsNot(result, True)

    def test_chat_and_audio_model_is_native_audio(self):
        from plugin.framework.client.model_fetcher import has_native_audio

        ctx = MagicMock()
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            result = has_native_audio('google/gemini-3.1-flash-lite-preview', 'https://openrouter.ai/api')
        self.assertTrue(result)


class TestFetchAvailableImageModels(unittest.TestCase):
    def tearDown(self):
        import plugin.framework.client.model_fetcher as cfg

        for k in list(cfg._model_fetch_image_cache):
            if '58907' in k or '58908' in k or 'together.xyz' in k or 'openrouter.ai' in k:
                cfg._model_fetch_image_cache.pop(k, None)
        for k in list(cfg._model_fetch_cache):
            if '58907' in k or '58908' in k or 'together.xyz' in k or 'openrouter.ai' in k:
                del cfg._model_fetch_cache[k]

    def test_openrouter_queries_dedicated_images_endpoint(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {
            'data': [
                {'id': 'google/gemini-2.5-flash-image'},
                {'id': 'black-forest-labs/flux-schnell'},
            ]
        }
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync:
            image_ids = cfg.fetch_available_image_models('https://openrouter.ai/api')
            # Verify the correct endpoint URL was requested
            mock_sync.assert_called_once()
            self.assertEqual(mock_sync.call_args[0][0], 'https://openrouter.ai/api/v1/images/models')
        self.assertEqual(image_ids, ['google/gemini-2.5-flash-image', 'black-forest-labs/flux-schnell'])


    def test_local_endpoint_falls_back_to_keyword_filter(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {'data': [{'id': 'flux'}, {'id': 'llama3.2'}]}
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            image_ids = cfg.fetch_available_image_models('http://127.0.0.1:58908')
        self.assertEqual(image_ids, ['flux'])

    def test_image_output_model_ids_from_v1_entries(self):
        from plugin.framework.client.model_fetcher import _image_output_model_ids_from_v1_entries

        entries = [
            {'id': 'a', 'architecture': {'output_modalities': ['text']}},
            {'id': 'b', 'architecture': {'output_modalities': ['image']}},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
            {'id': 'openai/gpt-oss-120b', 'type': 'chat'},
        ]
        self.assertEqual(
            _image_output_model_ids_from_v1_entries(entries),
            ['b', 'google/flash-image-2.5'],
        )

    def test_together_list_response_parses_all_ids(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = [
            {'id': 'openai/gpt-oss-120b', 'type': 'chat'},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
        ]
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            all_ids = cfg.fetch_available_models('https://api.together.xyz')
        self.assertEqual(all_ids, ['openai/gpt-oss-120b', 'google/flash-image-2.5'])

    def test_together_image_models_from_type_field(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = [
            {'id': 'openai/gpt-oss-120b', 'type': 'chat'},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
            {'id': 'black-forest-labs/FLUX.1-schnell', 'type': 'image'},
        ]
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            image_ids = cfg.fetch_available_image_models('https://api.together.xyz')
        self.assertEqual(image_ids, ['google/flash-image-2.5', 'black-forest-labs/FLUX.1-schnell'])

    def test_together_image_skips_slug_only_models(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = [
            {'id': 'black-forest-labs/FLUX.1-schnell', 'type': 'chat'},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
        ]
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            image_ids = cfg.fetch_available_image_models('https://api.together.xyz')
        self.assertEqual(image_ids, ['google/flash-image-2.5'])


class TestHasNativeVision(unittest.TestCase):
    def setUp(self):
        import plugin.framework.client.model_fetcher as mf
        mf._model_fetch_vision_cache.clear()
        mf._ollama_capabilities_cache.clear()

    def test_static_default_model_has_vision(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        ctx = MagicMock()
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            self.assertTrue(has_native_vision('google/gemini-3.1-flash-lite-preview', 'https://openrouter.ai/api'))

    def test_config_cache_has_vision(self):
        from plugin.framework.client.model_fetcher import has_native_vision, set_native_vision_support
        ctx = MagicMock()
        cache_dict = {}

        def mock_get_config(key):
            if key == "vision_support_map":
                return cache_dict
            return {}

        def mock_set_config(key, val):
            if key == "vision_support_map":
                cache_dict.update(val)

        with patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.set_config', side_effect=mock_set_config):
            set_native_vision_support('my-custom-model', 'http://localhost:11434', True)
            self.assertTrue(has_native_vision('my-custom-model', 'http://localhost:11434'))

            set_native_vision_support('my-custom-model', 'http://localhost:11434', False)
            self.assertFalse(has_native_vision('my-custom-model', 'http://localhost:11434'))

    def test_openrouter_dynamic_modality_detection(self):
        from plugin.framework.client.model_fetcher import has_native_vision, _model_fetch_vision_cache
        ctx = MagicMock()
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            
            url = 'https://openrouter.ai/api/v1/models'
            from plugin.framework.client.model_fetcher import _model_fetch_cache_key
            ck = _model_fetch_cache_key(url, 'https://openrouter.ai/api')
            _model_fetch_vision_cache[ck] = ['custom-openrouter-vision-model']

            self.assertTrue(has_native_vision('custom-openrouter-vision-model', 'https://openrouter.ai/api'))
            self.assertFalse(has_native_vision('some-other-model', 'https://openrouter.ai/api'))

    def test_ollama_api_show_capabilities(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        ctx = MagicMock()
        
        with patch('plugin.framework.client.requests.sync_request') as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            mock_sync.return_value = {"capabilities": ["vision"]}
            self.assertTrue(has_native_vision('llava', 'http://localhost:11434'))
            mock_sync.assert_called_once()

    def test_name_heuristics_removed(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        ctx = MagicMock()
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            self.assertFalse(has_native_vision('unknown-vision-model', 'https://api.openai.com/v1'))
