import os
import json
import tempfile
from unittest.mock import patch
from plugin.framework.client.model_fetcher import endpoint_url_suitable_for_v1_models_fetch
import pytest

class TestEndpointUrlSuitableForModelFetch:

    def test_incomplete_or_invalid_urls_rejected(self):
        assert not (endpoint_url_suitable_for_v1_models_fetch(''))
        assert not (endpoint_url_suitable_for_v1_models_fetch('http:/'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('http://'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('ftp://api.openai.com'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('not-a-url'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('http://localhost:1a'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('http://localhost:99999'))

    def test_complete_urls_accepted(self):
        assert (endpoint_url_suitable_for_v1_models_fetch('http://localhost:1234'))
        assert (endpoint_url_suitable_for_v1_models_fetch('https://api.openai.com/v1'))
        assert (endpoint_url_suitable_for_v1_models_fetch('http://127.0.0.1:11434'))
        assert (endpoint_url_suitable_for_v1_models_fetch('http://[::1]:8080'))

    def test_bad_port_helpers_return_none_or_false_without_raising(self):
        from plugin.framework.client.model_fetcher import (
            fetch_available_models,
            cached_image_models,
            settings_catalog_is_warm,
            fetch_available_image_models,
        )
        bad_url = 'http://localhost:1a'
        assert fetch_available_models(bad_url) is None
        assert cached_image_models(bad_url) is None
        assert settings_catalog_is_warm(bad_url) is False
        assert fetch_available_image_models(bad_url) is None


class TestFetchAvailableModelsCache:
    '_model_fetch_cache is process-wide; same normalized endpoint hits HTTP once.'

    def teardown_method(self):
        import plugin.framework.client.model_fetcher as cfg
        keys_to_del = [k for k in cfg._model_fetch_cache if any(sub in k for sub in (('127.0.0.1:5890'), 'openwebui.example', 'notopenwebui.example', '127.0.0.1:3000'))]
        for k in keys_to_del:
            del cfg._model_fetch_cache[k]
            cfg._model_fetch_image_cache.pop(k, None)
            cfg._model_context_cache.pop(k, None)

    def test_openwebui_host_catalog_url_strips_pasted_api(self):
        from plugin.framework.client.model_fetcher import fetch_available_models

        payload = {'data': [{'id': 'm1'}]}
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config_bool_safe', return_value=False):
            r1 = fetch_available_models('https://chat.openwebui.example/api', api_key_override='sk-test')
            assert r1 == ['m1']
            assert mock_sync.call_count == 1
            assert mock_sync.call_args[0][0] == 'https://chat.openwebui.example/api/models'

            r2 = fetch_available_models('https://chat.openwebui.example', api_key_override='sk-test')
            assert r2 == ['m1']
            assert mock_sync.call_count == 1

            r3 = fetch_available_models('https://notopenwebui.example/api', api_key_override='sk-test')
            assert r3 == ['m1']
            assert mock_sync.call_count == 2
            assert mock_sync.call_args[0][0] == 'https://notopenwebui.example/api/v1/models'

    def test_openwebui_checkbox_does_not_double_api(self):
        from plugin.framework.client.model_fetcher import fetch_available_models

        payload = {'data': [{'id': 'm1'}]}
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config_bool_safe', return_value=True):
            r = fetch_available_models('http://127.0.0.1:3000/api', api_key_override='sk')
            assert r == ['m1']
            assert mock_sync.call_count == 1
            assert mock_sync.call_args[0][0] == 'http://127.0.0.1:3000/api/models'

    def test_second_call_does_not_http(self):
        from plugin.framework.client import model_fetcher as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'alpha'}]}
            r1 = cfg.fetch_available_models('http://127.0.0.1:58901')
            r2 = cfg.fetch_available_models('http://127.0.0.1:58901')
            assert (r1) == (['alpha'])
            assert (r2) == (['alpha'])
            assert (mock_sync.call_count) == (1)

    def test_normalized_url_shares_cache_entry(self):
        from plugin.framework.client import model_fetcher as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'beta'}]}
            cfg.fetch_available_models('http://127.0.0.1:58902/')
            cfg.fetch_available_models('http://127.0.0.1:58902')
            assert (mock_sync.call_count) == (1)

    def test_fetch_available_models_sends_bearer_when_ctx_and_api_key(self):
        'GET /v1/models must use the same per-endpoint key as chat (LocalAI, etc.).'
        from plugin.framework.client import model_fetcher as cfg
        endpoint = 'http://127.0.0.1:58903'
        
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value='secret-token'):
            with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                mock_sync.return_value = {'data': [{'id': 'm1'}]}
                r = cfg.fetch_available_models(endpoint)
                assert (r) == (['m1'])
                mock_sync.assert_called_once()
                (_args, kwargs) = mock_sync.call_args
                headers = kwargs.get('headers')
                assert isinstance(headers, dict)
                assert (headers.get('Authorization')) == ('Bearer secret-token')

    def test_model_fetch_cache_key_differs_for_override(self):
        from plugin.framework.client import model_fetcher as cfg
        url = 'http://127.0.0.1:58906/v1/models'
        base = 'http://127.0.0.1:58906'
        with patch.object(cfg, 'get_api_key_for_endpoint', return_value='saved'):
            k_saved = cfg._model_fetch_cache_key(url, base, None)
            k_a = cfg._model_fetch_cache_key(url, base, 'typed-a')
            k_b = cfg._model_fetch_cache_key(url, base, 'typed-b')
        assert k_saved.startswith(f'{url}\x1f')
        assert 'saved' not in k_saved
        assert 'typed-a' not in k_a
        assert 'typed-b' not in k_b
        assert len({k_saved, k_a, k_b}) == 3

    def test_fetch_available_models_returns_copy_preventing_mutation(self):
        from plugin.framework.client import model_fetcher as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'm1'}, {'id': 'm2'}]}
            r1 = cfg.fetch_available_models('http://127.0.0.1:58902')
            assert r1 == ['m1', 'm2']
            r1.append('mutated')
            r2 = cfg.fetch_available_models('http://127.0.0.1:58902')
            assert r2 == ['m1', 'm2']
            assert 'mutated' not in r2

    def test_fetch_available_models_unexpected_body_logs_warning(self, caplog):
        import logging
        from plugin.framework.client import model_fetcher as cfg
        caplog.set_level(logging.WARNING, logger="plugin.framework.client.model_fetcher")
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'unexpected': True}
            result = cfg.fetch_available_models('http://127.0.0.1:58909')
            assert result is None
            assert any("unexpected body structure" in r.message for r in caplog.records)


class TestTextModelPlaceholderGuards:

    def test_get_text_model_skips_connection_failed_placeholder(self):
        from plugin.framework.client.model_fetcher import get_text_model

        with patch('plugin.framework.client.model_fetcher.get_config', return_value='(Connection failed)'):
            with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value='https://api.z.ai/api/paas'):
                assert (get_text_model()) == ('glm-5.2')

    def test_set_text_model_ignores_placeholder(self):
        from plugin.framework.client.model_fetcher import set_text_model

        with patch('plugin.framework.client.model_fetcher.set_config') as mock_set:
            set_text_model('(Connection failed)', update_lru=False)
            mock_set.assert_not_called()

    def test_fetch_available_models_override_used_not_config_file(self):
        'Settings passes live api_key field; override must win over api_keys_by_endpoint.'
        from plugin.framework.client import model_fetcher as cfg
        from plugin.framework.url_utils import normalize_endpoint_url
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'writeragent.json')
            endpoint = 'http://127.0.0.1:58904'
            norm = normalize_endpoint_url(endpoint)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'api_keys_by_endpoint': {norm: 'from-config-only'}}, f)

            def mock_config_path():
                return config_path
            with patch('plugin.framework.config.get_config_file_path', side_effect=mock_config_path):
                from plugin.framework.config import reset_config_for_tests
                reset_config_for_tests()
                for k in list(cfg._model_fetch_cache):
                    if ('58904' in k):
                        del cfg._model_fetch_cache[k]
                        cfg._model_fetch_image_cache.pop(k, None)
                        cfg._model_context_cache.pop(k, None)
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'm1'}]}
                    r = cfg.fetch_available_models(endpoint, api_key_override='from-override')
                    assert (r) == (['m1'])
                    mock_sync.assert_called_once()
                    (_args, kwargs) = mock_sync.call_args
                    headers = kwargs.get('headers')
                    assert isinstance(headers, dict)
                    assert (headers.get('Authorization')) == ('Bearer from-override')

    def test_fetch_override_and_saved_key_separate_cache(self):
        from plugin.framework.client import model_fetcher as cfg
        from plugin.framework.url_utils import normalize_endpoint_url
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'writeragent.json')
            endpoint = 'http://127.0.0.1:58905'
            norm = normalize_endpoint_url(endpoint)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'api_keys_by_endpoint': {norm: 'key-a'}}, f)

            def mock_config_path():
                return config_path
            with patch('plugin.framework.config.get_config_file_path', side_effect=mock_config_path):
                from plugin.framework.config import reset_config_for_tests
                reset_config_for_tests()
                for k in list(cfg._model_fetch_cache):
                    if ('58905' in k):
                        del cfg._model_fetch_cache[k]
                        cfg._model_fetch_image_cache.pop(k, None)
                        cfg._model_context_cache.pop(k, None)
                with patch('plugin.framework.client.requests.sync_request') as mock_sync:
                    mock_sync.return_value = {'data': [{'id': 'x'}]}
                    cfg.fetch_available_models(endpoint)
                    cfg.fetch_available_models(endpoint, api_key_override='key-b')
                    assert (mock_sync.call_count) == (2)


class TestGetModelCapabilityOpenRouter:
    def test_nitro_suffix_matches_curated_default(self):
        from plugin.framework.default_models import get_model_capability
        from plugin.framework.constants import ModelCapability

        caps = get_model_capability('openai/gpt-oss-120b:nitro', 'https://openrouter.ai/api')
        assert (isinstance(caps, int) and (caps & ModelCapability.TOOLS))


class TestHasNativeAudio:
    @pytest.mark.parametrize(
        "value, value_2, expected",
        [
            pytest.param('mistralai/voxtral-mini-transcribe', 'https://openrouter.ai/api', False, id="test_audio_only_stt_model_is_not_native_audio"),
            pytest.param('whisper-1', 'https://example.invalid/v1', False, id="test_uncatalogued_whisper_is_not_native_audio"),
            pytest.param('some-chat-model', 'https://example.invalid/v1', None, id="test_unknown_model_stays_unknown"),
        ],
    )
    def test_audio_only_stt_model_is_not_native_audio(self, value, value_2, expected):
        from plugin.framework.client.model_fetcher import has_native_audio
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            result = has_native_audio(value, value_2)
        assert result is expected

    def test_chat_and_audio_model_is_native_audio(self):
        from plugin.framework.client.model_fetcher import has_native_audio
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            result = has_native_audio('google/gemini-3.1-flash-lite-preview', 'https://openrouter.ai/api')
        assert (result)


class TestFetchAvailableImageModels:
    def setup_method(self):
        # Module caches live for the process. Under xdist, another file on this
        # worker (e.g. is_image_only_model → fetch_available_image_models) may
        # already have memoized OpenRouter; tearDown-only cleanup is too late.
        self._clear_model_fetch_caches()

    def teardown_method(self):
        self._clear_model_fetch_caches()

    @staticmethod
    def _clear_model_fetch_caches():
        import plugin.framework.client.model_fetcher as cfg

        for k in list(cfg._model_fetch_image_cache):
            if '58907' in k or '58908' in k or 'together.xyz' in k or 'openrouter.ai' in k:
                cfg._model_fetch_image_cache.pop(k, None)
        for k in list(cfg._model_fetch_cache):
            if '58907' in k or '58908' in k or 'together.xyz' in k or 'openrouter.ai' in k:
                del cfg._model_fetch_cache[k]
        for k in list(cfg._model_context_cache):
            if '58907' in k or '58908' in k or 'together.xyz' in k or 'openrouter.ai' in k:
                cfg._model_context_cache.pop(k, None)

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
            assert (mock_sync.call_args[0][0]) == ('https://openrouter.ai/api/v1/images/models')
        assert (image_ids) == (['google/gemini-2.5-flash-image', 'black-forest-labs/flux-schnell'])


    def test_local_endpoint_falls_back_to_keyword_filter(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {'data': [{'id': 'flux'}, {'id': 'llama3.2'}]}
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            image_ids = cfg.fetch_available_image_models('http://127.0.0.1:58908')
        assert (image_ids) == (['flux'])

    def test_local_memo_matches_keyword_image_fetch(self):
        # Settings reads the memo from the one /v1/models GET. painter is
        # type=image, so the old memo was ["painter"] while the image fetch
        # returned ["flux"]. Those lists must match, and the second read must
        # not GET again.
        from plugin.framework.client import model_fetcher as cfg

        payload = {'data': [{'id': 'flux'}, {'id': 'llama3.2'}, {'id': 'painter', 'type': 'image'}]}
        endpoint = 'http://127.0.0.1:58908'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync:
            cfg.fetch_available_models(endpoint)
            cached = cfg.cached_image_models(endpoint)
            fetched = cfg.fetch_available_image_models(endpoint)
            assert (mock_sync.call_count) == (1)
        assert (cached) == (['flux'])
        assert (fetched) == (cached)
        assert (cfg.settings_catalog_is_warm(endpoint)) is True

    def test_vision_declarations_skip_omitted_modalities(self):
        from plugin.framework.client.model_fetcher import _vision_declarations_from_v1_entries

        entries = [
            {'id': 'yes', 'architecture': {'input_modalities': ['text', 'image']}},
            {'id': 'no', 'architecture': {'input_modalities': ['text']}},
            {'id': 'empty', 'architecture': {'input_modalities': []}},
            {'id': 'top', 'input_modalities': ['image']},
            {'id': 'bad-top', 'input_modalities': 'image', 'architecture': {'input_modalities': ['image']}},
            {'id': 'omit'},
            {'id': 'omit-arch', 'architecture': {'output_modalities': ['text']}},
        ]
        assert (_vision_declarations_from_v1_entries(entries)) == ({
            'yes': True,
            'no': False,
            'empty': False,
            'top': True,
        })

    def test_image_output_model_ids_from_v1_entries(self):
        from plugin.framework.client.model_fetcher import _image_output_model_ids_from_v1_entries

        entries = [
            {'id': 'a', 'architecture': {'output_modalities': ['text']}},
            {'id': 'b', 'architecture': {'output_modalities': ['image']}},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
            {'id': 'openai/gpt-oss-120b', 'type': 'chat'},
        ]
        assert (_image_output_model_ids_from_v1_entries(entries)) == (['b', 'google/flash-image-2.5'])

    def test_together_list_response_parses_all_ids(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = [
            {'id': 'openai/gpt-oss-120b', 'type': 'chat'},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
        ]
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            all_ids = cfg.fetch_available_models('https://api.together.xyz')
        assert (all_ids) == (['openai/gpt-oss-120b', 'google/flash-image-2.5'])

    def test_together_image_models_from_type_field(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = [
            {'id': 'openai/gpt-oss-120b', 'type': 'chat'},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
            {'id': 'black-forest-labs/FLUX.1-schnell', 'type': 'image'},
        ]
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            image_ids = cfg.fetch_available_image_models('https://api.together.xyz')
        assert (image_ids) == (['google/flash-image-2.5', 'black-forest-labs/FLUX.1-schnell'])

    def test_together_image_skips_slug_only_models(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = [
            {'id': 'black-forest-labs/FLUX.1-schnell', 'type': 'chat'},
            {'id': 'google/flash-image-2.5', 'type': 'image'},
        ]
        with patch('plugin.framework.client.requests.sync_request', return_value=payload):
            image_ids = cfg.fetch_available_image_models('https://api.together.xyz')
        assert (image_ids) == (['google/flash-image-2.5'])


class TestHasNativeVision:
    def setup_method(self):
        self._patch_main_thread = patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=False)
        self._patch_main_thread.start()
        import plugin.framework.client.model_fetcher as mf
        mf._model_fetch_vision_cache.clear()
        mf._ollama_show_cache.clear()
        mf._vision_in_flight.clear()
        # A prior test may have memoized a failed OpenRouter GET as None.
        # has_native_vision treats that as "already fetched" and will not HTTP again.
        for cache in (mf._model_fetch_cache, mf._model_fetch_image_cache, mf._model_context_cache):
            for key in list(cache):
                if "openrouter.ai" in key or "together.xyz" in key:
                    cache.pop(key, None)

    def teardown_method(self):
        self._patch_main_thread.stop()

    def test_static_default_model_has_vision(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            assert (has_native_vision('google/gemini-3.1-flash-lite-preview', 'https://openrouter.ai/api'))

    def test_config_cache_has_vision(self):
        from plugin.framework.client.model_fetcher import has_native_vision, set_native_vision_support
        cache_dict = {}

        def mock_get_config(key):
            if key == "vision_support_map":
                return cache_dict
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == "vision_support_map":
                mutator(cache_dict)

        with patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            set_native_vision_support('my-custom-model', 'http://localhost:11434', True)
            assert (has_native_vision('my-custom-model', 'http://localhost:11434'))

            set_native_vision_support('my-custom-model', 'http://localhost:11434', False)
            assert not (has_native_vision('my-custom-model', 'http://localhost:11434'))

    def test_openrouter_dynamic_modality_detection(self):
        from plugin.framework.client.model_fetcher import has_native_vision, _model_fetch_vision_cache
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}), \
             patch('plugin.framework.client.model_fetcher.set_config'):
            
            url = 'https://openrouter.ai/api/v1/models'
            from plugin.framework.client.model_fetcher import _model_fetch_cache_key
            ck = _model_fetch_cache_key(url, 'https://openrouter.ai/api')
            _model_fetch_vision_cache[ck] = {'custom-openrouter-vision-model': True}

            assert (has_native_vision('custom-openrouter-vision-model', 'https://openrouter.ai/api'))
            assert not (has_native_vision('some-other-model', 'https://openrouter.ai/api'))

    def test_ollama_api_show_capabilities(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        
        with patch('plugin.framework.client.requests.sync_request') as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            mock_sync.return_value = {"capabilities": ["vision"]}
            assert has_native_vision('llava', 'http://localhost:11434') is True
            mock_sync.assert_called_once()

    def test_name_heuristics_removed(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            assert has_native_vision('unknown-vision-model', 'https://api.openai.com/v1') is None

    def test_openrouter_cache_miss_fetches_modalities_for_uncatalogued_gemini(self):
        # google/gemini-3.8-flash is not a DEFAULT_MODELS row. The sidebar does
        # not fill _model_fetch_vision_cache, so has_native_vision must GET
        # /v1/models once and read architecture.input_modalities.
        from plugin.framework.default_models import DEFAULT_MODELS, resolve_model_id
        from plugin.framework.client.model_fetcher import has_native_vision

        catalog_ids = [resolve_model_id(row, 'openrouter') for row in DEFAULT_MODELS]
        assert ('google/gemini-3.8-flash') not in (catalog_ids)

        payload = {
            'data': [
                {
                    'id': 'google/gemini-3.8-flash',
                    'architecture': {'input_modalities': ['text', 'image', 'audio'], 'output_modalities': ['text']},
                },
                {
                    'id': 'deepseek/deepseek-chat',
                    'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
                },
                {
                    'id': 'inception/mercury-2.5',
                    'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
                },
            ]
        }
        saved = {}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == 'vision_support_map':
                mutator(saved)

        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is True
            assert mock_sync.call_count == 1
            # Lookups never write vision_support_map: map stays empty.
            assert saved == {}
            # In-process memo answers the next call; do not GET the catalog again.
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is True
            assert mock_sync.call_count == 1
            # :nitro is the same catalog row (dynamic OpenRouter suffix).
            assert has_native_vision('google/gemini-3.8-flash:nitro', 'https://openrouter.ai/api') is True
            assert has_native_vision('deepseek/deepseek-chat', 'https://openrouter.ai/api') is False
            assert has_native_vision('inception/mercury-2.5', 'https://openrouter.ai/api') is False
            assert saved == {}

    def test_omitted_input_modalities_does_not_persist_false(self):
        from plugin.framework.client.model_fetcher import has_native_vision

        payload = {
            'data': [
                {'id': 'google/gemini-3.8-flash'},
                {'id': 'quiet-model', 'architecture': {'output_modalities': ['text']}},
            ]
        }
        saved = {}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == 'vision_support_map':
                mutator(saved)

        endpoint = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            assert has_native_vision('google/gemini-3.8-flash', endpoint) is None
            assert saved == {}
            assert mock_sync.call_count == 1
            assert has_native_vision('quiet-model', endpoint) is None
            assert saved == {}

    def test_explicit_text_modalities_answer_false_without_persisting(self):
        from plugin.framework.client.model_fetcher import has_native_vision

        payload = {
            'data': [
                {'id': 'text-only-a', 'architecture': {'input_modalities': ['text']}},
                {'id': 'also-text', 'input_modalities': []},
            ]
        }
        saved = {}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == 'vision_support_map':
                mutator(saved)

        endpoint = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload), \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            assert has_native_vision('text-only-a', endpoint) is False
            assert has_native_vision('also-text', endpoint) is False
            assert saved == {}

    def test_mixed_modalities_answer_only_stated_rows(self):
        from plugin.framework.client.model_fetcher import has_native_vision

        payload = {
            'data': [
                {'id': 'sees', 'architecture': {'input_modalities': ['text', 'image']}},
                {'id': 'blind', 'architecture': {'input_modalities': ['text']}},
                {'id': 'quiet'},
            ]
        }
        saved = {}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == 'vision_support_map':
                mutator(saved)

        endpoint = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload), \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            assert has_native_vision('sees', endpoint) is True
            assert has_native_vision('blind', endpoint) is False
            assert has_native_vision('quiet', endpoint) is None
            assert has_native_vision('sees:nitro', endpoint) is True
            assert saved == {}

    def test_failed_modalities_fetch_does_not_persist_false(self):
        # A down catalog is not a text-only answer. Do not write False into
        # vision_support_map or the next process can never recover.
        from plugin.framework.client.model_fetcher import has_native_vision
        saved = {}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == 'vision_support_map':
                mutator(saved)

        with patch('plugin.framework.client.requests.sync_request', side_effect=OSError('down')), \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is None
            assert (saved) == ({})

    def test_vision_support_map_false_skips_modalities_fetch(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        cache = {'https://openrouter.ai/api@google/gemini-3.8-flash': False}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return cache
            return {}

        with patch('plugin.framework.client.requests.sync_request') as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config):
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is False
            mock_sync.assert_not_called()

    def test_warm_memo_answers_without_fetch(self):
        from plugin.framework.client import model_fetcher

        saved = dict(model_fetcher._model_fetch_vision_cache)
        model_fetcher._model_fetch_vision_cache.clear()
        try:
            url = "https://openrouter.ai/api/v1/models"
            ck = model_fetcher._model_fetch_cache_key(url, "https://openrouter.ai/api")
            model_fetcher._model_fetch_vision_cache[ck] = {"known-model": True}
            with patch.object(model_fetcher, "get_config", return_value={}), \
                 patch.object(model_fetcher, "fetch_available_models") as fetch, \
                 patch.object(model_fetcher, "set_config"):
                assert model_fetcher.has_native_vision(
                    "known-model",
                    "https://openrouter.ai/api",
                ) is True
                fetch.assert_not_called()
        finally:
            model_fetcher._model_fetch_vision_cache.clear()
            model_fetcher._model_fetch_vision_cache.update(saved)

    def test_vision_support_map_is_a_config_field(self):
        from plugin.framework.config_schema import WriterAgentConfig, _resolve_default, is_known_config_key
        assert (is_known_config_key('vision_support_map'))
        assert (_resolve_default('vision_support_map')) == ({})
        cfg = WriterAgentConfig.from_dict({
            'vision_support_map': {'https://openrouter.ai/api@google/gemini-3.8-flash': False},
        })
        cfg.validate()
        dumped = cfg.to_dict()
        assert (dumped['vision_support_map']['https://openrouter.ai/api@google/gemini-3.8-flash']) is False

    def test_vision_memo_honors_api_key_override(self):
        from plugin.framework.client.model_fetcher import fetch_available_models, has_native_vision

        endpoint = 'https://openrouter.ai/api'
        payload = {
            'data': [
                {
                    'id': 'google/gemini-3.8-flash',
                    'architecture': {'input_modalities': ['text', 'image']},
                },
            ]
        }
        saved = {}

        def mock_get_config(key):
            if key == 'vision_support_map':
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == 'vision_support_map':
                mutator(saved)

        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.update_config_mapping', side_effect=mock_update_config_mapping):
            fetch_available_models(endpoint, api_key_override='sk-dialog')
            assert mock_sync.call_count == 1

            assert has_native_vision('google/gemini-3.8-flash', endpoint, api_key_override=' sk-dialog ') is True
            assert mock_sync.call_count == 1

    def test_ui_thread_never_fetches_and_schedules_background_job(self):
        from plugin.framework.client import model_fetcher
        from plugin.framework.client.model_fetcher import has_native_vision

        scheduled_jobs = []

        def fake_bg(fn, *args, **kwargs):
            scheduled_jobs.append(fn)

        saved = {}

        def mock_get_config(key):
            if key == "vision_support_map":
                return dict(saved)
            return {}

        with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=True), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=fake_bg), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models") as mock_fetch, \
             patch("plugin.framework.client.model_fetcher.query_ollama_show") as mock_show, \
             patch("plugin.framework.client.requests.sync_request") as mock_sync, \
             patch("plugin.framework.client.model_fetcher.get_config", side_effect=mock_get_config):

            # Cold OpenRouter
            res1 = has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api")
            assert res1 is None
            mock_fetch.assert_not_called()
            mock_sync.assert_not_called()
            assert len(scheduled_jobs) == 1

            # Called again while in flight -> does not schedule duplicate
            res2 = has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api")
            assert res2 is None
            assert len(scheduled_jobs) == 1

            # Cold Ollama
            res3 = has_native_vision("llava-uncatalogued", "http://localhost:11434")
            assert res3 is None
            mock_show.assert_not_called()
            assert len(scheduled_jobs) == 2

            # Now run the captured OpenRouter job (with on_main_thread returning False)
            openrouter_job = scheduled_jobs[0]
            with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=False):
                def fake_fetch(ep, api_key_override=None):
                    url = "https://openrouter.ai/api/v1/models"
                    ck = model_fetcher._model_fetch_cache_key(url, "https://openrouter.ai/api", api_key_override)
                    model_fetcher._model_fetch_vision_cache[ck] = {"google/gemini-3.8-flash": True}
                mock_fetch.side_effect = fake_fetch
                openrouter_job()
                mock_fetch.assert_called_once()

            # Now next UI thread call returns the real answer without scheduling new job
            scheduled_jobs.clear()
            res4 = has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api")
            assert res4 is True
            assert len(scheduled_jobs) == 0
            assert saved == {}

    def test_fetch_branch_asserts_off_ui_thread(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        import pytest

        calls = [False, True]
        with patch("plugin.framework.client.model_fetcher.on_main_thread", side_effect=lambda: calls.pop(0)):
            with pytest.raises(AssertionError):
                has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api")

    def test_retry_after_failure_inline_and_ui(self):
        from plugin.framework.client.model_fetcher import has_native_vision

        # 1. Inline path (off UI thread)
        with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=False), \
             patch("plugin.framework.client.requests.sync_request") as mock_sync:
            mock_sync.side_effect = OSError("network down")
            assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is None
            assert mock_sync.call_count == 1

            payload = {
                "data": [
                    {"id": "google/gemini-3.8-flash", "architecture": {"input_modalities": ["image"]}}
                ]
            }
            mock_sync.side_effect = None
            mock_sync.return_value = payload
            assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is True
            assert mock_sync.call_count == 2

        # 2. UI path: failed job clears in-flight key so next UI call schedules again
        import plugin.framework.client.model_fetcher as mf
        mf._model_fetch_vision_cache.clear()
        for cache in (mf._model_fetch_cache, mf._model_fetch_image_cache, mf._model_context_cache):
            for key in list(cache):
                if "openrouter.ai" in key:
                    cache.pop(key, None)

        scheduled = []
        with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=True), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=lambda fn, **kw: scheduled.append(fn)):
            assert has_native_vision("google/gemini-uncatalogued-fail", "https://openrouter.ai/api") is None
            assert len(scheduled) == 1

            job = scheduled.pop()
            with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=False), \
                 patch("plugin.framework.client.requests.sync_request", side_effect=OSError("fail")):
                job()

            assert has_native_vision("google/gemini-uncatalogued-fail", "https://openrouter.ai/api") is None
            assert len(scheduled) == 1

    def test_shared_key_openwebui_and_openrouter_nitro_and_no_schedule_on_settled(self):
        from plugin.framework.client.model_fetcher import has_native_vision, set_native_vision_support

        saved = {}

        def mock_get_config(key):
            if key == "vision_support_map":
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == "vision_support_map":
                mutator(saved)

        with patch("plugin.framework.client.model_fetcher.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.client.model_fetcher.update_config_mapping", side_effect=mock_update_config_mapping), \
             patch("plugin.framework.client.model_fetcher.get_config_bool_safe", return_value=True), \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:

            set_native_vision_support("my-model", "http://openwebui.example/api", True)
            assert has_native_vision("my-model", "http://openwebui.example") is True
            mock_bg.assert_not_called()

            set_native_vision_support("google/gemini-3.8-flash:nitro", "https://openrouter.ai/api", False)
            assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is False
            with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=True):
                assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is False
                mock_bg.assert_not_called()

            with patch("plugin.framework.client.model_fetcher.on_main_thread", return_value=True):
                assert has_native_vision("google/gemini-3.1-flash-lite-preview", "https://openrouter.ai/api") is True
                mock_bg.assert_not_called()

    def test_clear_settings_catalog_cache_drops_vision_memo_and_ollama_show(self):
        from plugin.framework.client import model_fetcher
        from plugin.framework.client.model_fetcher import clear_settings_catalog_cache, has_native_vision

        url = "https://openrouter.ai/api/v1/models"
        ck = model_fetcher._model_fetch_cache_key(url, "https://openrouter.ai/api")
        model_fetcher._model_fetch_vision_cache[ck] = {"google/gemini-3.8-flash": True}

        ollama_ep = "http://localhost:11434"
        ollama_key = f"{ollama_ep}@llava"
        model_fetcher._ollama_show_cache[ollama_key] = {"capabilities": ["vision"]}

        assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is True
        assert has_native_vision("llava", ollama_ep) is True

        clear_settings_catalog_cache("https://openrouter.ai/api")
        assert ck not in model_fetcher._model_fetch_vision_cache

        clear_settings_catalog_cache(ollama_ep)
        assert ollama_key not in model_fetcher._ollama_show_cache


class TestParseOllamaRuntimeNumCtx:
    """Issue #570: live PARAMETER num_ctx wins over trained context_length."""

    def test_parameters_num_ctx_wins_over_trained_context_length(self):
        from plugin.framework.client.model_fetcher import parse_ollama_runtime_num_ctx

        body = {
            "parameters": "num_ctx                      4096\nstop                        \"<|im_end|>\"\n",
            "modelfile": "FROM qwen2.5:7b\nPARAMETER num_ctx 8192\n",
            "model_info": {
                "qwen2.context_length": 32768,
                "general.architecture": "qwen2",
            },
        }
        assert (parse_ollama_runtime_num_ctx(body)) == (4096)

    def test_modelfile_used_when_parameters_omit_num_ctx(self):
        from plugin.framework.client.model_fetcher import parse_ollama_runtime_num_ctx

        body = {
            "parameters": "stop                        \"<|im_end|>\"\n",
            "modelfile": "FROM qwen2.5:7b\nPARAMETER num_ctx 4096\n",
            "model_info": {"qwen2.context_length": 32768},
        }
        assert (parse_ollama_runtime_num_ctx(body)) == (4096)

    def test_trained_context_length_is_not_a_fallback(self):
        from plugin.framework.client.model_fetcher import parse_ollama_runtime_num_ctx

        body = {
            "parameters": "stop \"<|im_end|>\"",
            "modelfile": "FROM qwen2.5:7b\n",
            "model_info": {"qwen2.context_length": 32768},
        }
        assert (parse_ollama_runtime_num_ctx(body)) is None

    def test_query_reuses_show_cache_and_survives_missing_show(self):
        from plugin.framework.client import model_fetcher as mf

        mf._ollama_show_cache.clear()
        show = {
            "capabilities": ["completion"],
            "parameters": "num_ctx 4096\n",
            "model_info": {"qwen2.context_length": 32768},
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=show) as mock_sync:
            assert (mf.query_ollama_runtime_num_ctx("http://localhost:11434", "qwen2.5:7b")) == (4096)
            assert (mock_sync.call_count) == (1)
        mf._ollama_show_cache.clear()
        with patch("plugin.framework.client.requests.sync_request", side_effect=OSError("down")):
            assert (mf.query_ollama_runtime_num_ctx("http://localhost:11434", "qwen2.5:7b")) is None


class TestFilterFetchedModels:
    def test_audio_filter_includes_asr_models(self):
        from plugin.framework.client.model_fetcher import _filter_fetched_models

        models = ["glm-5.2", "glm-asr-2512", "whisper-1", "gpt-4o"]
        out = _filter_fetched_models(models, "audio")
        assert ("glm-asr-2512") in (out)
        assert ("whisper-1") in (out)
        assert ("glm-5.2") not in (out)
        assert ("gpt-4o") not in (out)

    def test_text_filter_keeps_vision_and_coder_chat_models(self):
        from plugin.framework.client.model_fetcher import _filter_fetched_models

        models = ["qwen2.5-coder", "llava", "gpt-4-vision", "whisper-1", "text-embedding-3-small", "deepseek-coder", "gpt-4o"]
        out = _filter_fetched_models(models, "text")
        assert "qwen2.5-coder" in out
        assert "llava" in out
        assert "gpt-4-vision" in out
        assert "gpt-4o" in out
        assert "whisper-1" not in out
        assert "text-embedding-3-small" not in out
        assert "deepseek-coder" not in out


class TestV1ContextHarvest:
    """Harvest context_length / context_window from /v1/models; lookup never HTTP."""

    ENDPOINT = "http://127.0.0.1:58923"

    def setup_method(self):
        self._clear()

    def teardown_method(self):
        self._clear()

    def _clear(self):
        import plugin.framework.client.model_fetcher as mf

        for store in (mf._model_fetch_cache, mf._model_fetch_image_cache, mf._model_context_cache):
            for key in [k for k in store if "58923" in k or "openwebui.example" in k]:
                store.pop(key, None)

    def test_cached_v1_context_tokens_uses_override_across_openwebui_forms(self):
        from plugin.framework.client.model_fetcher import cached_v1_context_tokens, fetch_available_models

        payload = {"data": [{"id": "m", "context_length": 4096}]}
        with patch("plugin.framework.client.requests.sync_request", return_value=payload), \
             patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""):
            fetch_available_models("https://chat.openwebui.example/api", api_key_override="sk-owu")

            assert cached_v1_context_tokens("https://chat.openwebui.example", "m", api_key_override="sk-owu") == 4096
            assert cached_v1_context_tokens("https://chat.openwebui.example", "m", api_key_override=None) is None

    def test_groq_context_window_harvested(self):
        from plugin.framework.client import model_fetcher as mf

        payload = {"data": [{"id": "openai/gpt-oss-120b", "context_window": 131072}]}
        with patch("plugin.framework.client.requests.sync_request", return_value=payload):
            mf.fetch_available_models(self.ENDPOINT)
        assert (mf.cached_v1_context_tokens(self.ENDPOINT, "openai/gpt-oss-120b")) == (131072)

    def test_prefers_context_length_over_context_window(self):
        from plugin.framework.client import model_fetcher as mf

        payload = {
            "data": [
                {"id": "m", "context_length": 1000, "context_window": 2000},
            ]
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload):
            mf.fetch_available_models(self.ENDPOINT)
        assert (mf.cached_v1_context_tokens(self.ENDPOINT, "m")) == (1000)

    def test_ignores_non_positive_and_max_context_length(self):
        from plugin.framework.client import model_fetcher as mf

        payload = {
            "data": [
                {"id": "zero", "context_length": 0},
                {"id": "neg", "context_window": -8},
                {"id": "lms", "max_context_length": 262144},
            ]
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload):
            mf.fetch_available_models(self.ENDPOINT)
        assert (mf.cached_v1_context_tokens(self.ENDPOINT, "zero")) is None
        assert (mf.cached_v1_context_tokens(self.ENDPOINT, "neg")) is None
        assert (mf.cached_v1_context_tokens(self.ENDPOINT, "lms")) is None

    def test_lookup_does_not_http(self):
        from plugin.framework.client import model_fetcher as mf

        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            assert (mf.cached_v1_context_tokens(self.ENDPOINT, "openai/gpt-oss-120b")) is None
            mock_sync.assert_not_called()


class TestGetSttModel:
    @pytest.mark.parametrize(
        "value, value_2, expected",
        [
            pytest.param("speech-new", "speech-old", "speech-new", id="test_audio_stt_model_wins_over_legacy"),
            pytest.param("", "whisper-legacy", "whisper-legacy", id="test_legacy_stt_model_when_new_key_empty"),
        ],
    )
    def test_audio_stt_model_wins_over_legacy(self, value, value_2, expected):
        from plugin.framework.client.model_fetcher import get_stt_model

        def fake_get(key):
            if key == "audio.stt_model":
                return value
            if key == "stt_model":
                return value_2
            return ""

        with patch("plugin.framework.client.model_fetcher.get_config", side_effect=fake_get):
            assert get_stt_model() == expected


    def test_default_from_provider_when_both_empty(self):
        from plugin.framework.client.model_fetcher import get_stt_model

        with patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
            with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api/v1"):
                assert get_stt_model() == "mistralai/voxtral-mini-transcribe"


class TestGetTtsModel:
    def test_explicit_config_wins(self):
        from plugin.framework.client.model_fetcher import get_tts_model

        with patch("plugin.framework.client.model_fetcher.get_config", return_value="custom-tts"):
            assert get_tts_model() == "custom-tts"

    def test_default_from_provider(self):
        from plugin.framework.client.model_fetcher import get_tts_model

        with patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
            with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api/v1"):
                assert get_tts_model() == "hexgrad/Kokoro-82M"


class TestFetchAvailableSpeechModels:
    def setup_method(self):
        self._clear()

    def teardown_method(self):
        self._clear()

    @staticmethod
    def _clear():
        from plugin.framework.client import model_fetcher as cfg

        for cache in (cfg._model_fetch_tts_cache, cfg._model_fetch_stt_cache):
            for key in list(cache):
                if "openrouter.ai" in key or "together.xyz" in key:
                    cache.pop(key, None)
        cfg._tts_supported_voices.clear()
        cfg._tts_response_format.clear()
        cfg._together_voices_fetch_cache.clear()

    def test_openrouter_tts_queries_speech_modality(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {
            "data": [
                {
                    "id": "hexgrad/kokoro-82m",
                    "architecture": {"output_modalities": ["speech"]},
                    "supported_voices": ["af_bella"],
                },
                {"id": "microsoft/mai-voice-2", "architecture": {"output_modalities": ["speech"]}},
                {"id": "google/lyria-3-pro-preview", "architecture": {"output_modalities": ["audio"]}},
            ]
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync:
            ids = cfg.fetch_available_tts_models("https://openrouter.ai/api", api_key_override="sk-test")
            ids_again = cfg.fetch_available_tts_models("https://openrouter.ai/api", api_key_override="sk-test")
            mock_sync.assert_called_once()
            assert mock_sync.call_args[0][0] == "https://openrouter.ai/api/v1/models?output_modalities=speech"
            headers = mock_sync.call_args.kwargs.get("headers") or {}
            assert headers.get("Authorization") == "Bearer sk-test"
        assert ids == ["hexgrad/kokoro-82m", "microsoft/mai-voice-2"]
        assert ids_again == ids
        assert cfg.cached_tts_supported_voices("hexgrad/kokoro-82m") == ["af_bella"]
        assert cfg.preferred_openrouter_tts_model_id("hexgrad/Kokoro-82M") == "hexgrad/kokoro-82m"

    def test_openrouter_tts_failure_is_not_cached(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {
            "data": [
                {"id": "hexgrad/kokoro-82m", "architecture": {"output_modalities": ["speech"]}},
            ]
        }
        with patch(
            "plugin.framework.client.requests.sync_request",
            side_effect=[OSError("down"), payload],
        ) as mock_sync:
            assert cfg.fetch_available_tts_models("https://openrouter.ai/api", api_key_override="sk-test") is None
            ids = cfg.fetch_available_tts_models("https://openrouter.ai/api", api_key_override="sk-test")
            assert mock_sync.call_count == 2
        assert ids == ["hexgrad/kokoro-82m"]

    def test_response_format_cache_is_case_insensitive(self):
        from plugin.framework.client import model_fetcher as cfg

        model = "google/gemini-2.5-flash-preview-tts"
        cfg.remember_tts_response_format(model, "pcm")
        assert cfg.cached_tts_response_format("Google/Gemini-2.5-Flash-Preview-TTS") == "pcm"
        cfg.remember_tts_response_format(model, "not-a-format")
        assert cfg.cached_tts_response_format(model) == "pcm"
        assert cfg.cached_tts_response_format("x-ai/grok-voice-tts-1.0") is None

    def test_openrouter_stt_queries_transcription_modality(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {
            "data": [
                {"id": "mistralai/voxtral-mini-transcribe", "architecture": {"output_modalities": ["transcription"]}},
                {"id": "openai/whisper-large-v3"},
                {"id": "google/lyria-3-pro-preview", "architecture": {"output_modalities": ["audio"]}},
            ]
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync:
            ids = cfg.fetch_available_stt_models("https://openrouter.ai/api")
            assert mock_sync.call_args[0][0] == "https://openrouter.ai/api/v1/models?output_modalities=transcription"
        assert ids == ["mistralai/voxtral-mini-transcribe", "openai/whisper-large-v3"]

    def test_settings_catalog_warm_requires_provider_lists_and_clear_refetches(self):
        from plugin.framework.client import model_fetcher as cfg

        endpoint = "https://openrouter.ai/api"
        key = "sk-warm-predicate"

        def body(url, **kwargs):
            if url.endswith("/images/models"):
                return {"data": [{"id": "google/gemini-2.5-flash-image"}]}
            if "output_modalities=speech" in url:
                return {"data": [{"id": "hexgrad/kokoro-82m", "architecture": {"output_modalities": ["speech"]}}]}
            if "output_modalities=transcription" in url:
                return {"data": [{"id": "openai/whisper-large-v3"}]}
            if url.endswith("/models"):
                return {"data": [{"id": "openrouter/fusion", "type": "chat"}]}
            raise AssertionError(url)

        cfg.clear_settings_catalog_cache(endpoint, api_key_override=key)
        try:
            with patch("plugin.framework.client.requests.sync_request", side_effect=body) as mock_sync, \
                 patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is False
                cfg.fetch_available_models(endpoint, api_key_override=key)
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is False
                cfg.fetch_available_image_models(endpoint, api_key_override=key)
                cfg.fetch_available_tts_models(endpoint, api_key_override=key)
                cfg.fetch_available_stt_models(endpoint, api_key_override=key)
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is True
                assert cfg.cached_text_models(endpoint, api_key_override=key) == ["openrouter/fusion"]
                assert cfg.cached_image_models(endpoint, api_key_override=key) == ["google/gemini-2.5-flash-image"]
                mock_sync.reset_mock()
                assert cfg.fetch_available_models(endpoint, api_key_override=key) == ["openrouter/fusion"]
                mock_sync.assert_not_called()
                cfg.clear_settings_catalog_cache(endpoint, api_key_override=key)
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is False
                cfg.fetch_available_models(endpoint, api_key_override=key)
                mock_sync.assert_called()
        finally:
            cfg.clear_settings_catalog_cache(endpoint, api_key_override=key)

    def test_together_catalog_warm_includes_voices_list(self):
        from plugin.framework.client import model_fetcher as cfg

        endpoint = "https://api.together.xyz"
        key = "sk-together-warm"

        def body(url, **kwargs):
            if url.endswith("/voices") or "/voices?" in url:
                return {"data": [{"model": "hexgrad/Kokoro-82M", "voices": [{"name": "af_bella"}]}]}
            if url.endswith("/models"):
                return [{"id": "openai/gpt-oss-120b", "type": "chat"}, {"id": "black-forest-labs/FLUX.1-dev", "type": "image"}]
            raise AssertionError(url)

        cfg.clear_settings_catalog_cache(endpoint, api_key_override=key)
        try:
            with patch("plugin.framework.client.requests.sync_request", side_effect=body), \
                 patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
                cfg.fetch_available_models(endpoint, api_key_override=key)
                assert cfg.cached_image_models(endpoint, api_key_override=key) == ["black-forest-labs/FLUX.1-dev"]
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is False
                cfg.fetch_together_tts_voices(endpoint, api_key_override=key)
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is True
                cfg.clear_settings_catalog_cache(endpoint, api_key_override=key)
                cfg.fetch_available_models(endpoint, api_key_override=key)
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is False
                cfg.fetch_together_tts_voices(
                    endpoint, model_id="cartesia/sonic", api_key_override=key,
                )
                assert cfg.settings_catalog_is_warm(endpoint, api_key_override=key) is True
        finally:
            cfg.clear_settings_catalog_cache(endpoint, api_key_override=key)

    def test_together_has_no_speech_list_endpoint(self):
        from plugin.framework.client import model_fetcher as cfg

        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            assert cfg.fetch_available_tts_models("https://api.together.xyz") is None
            assert cfg.fetch_available_stt_models("https://api.together.xyz") is None
            mock_sync.assert_not_called()

    def test_together_voices_list_all_fills_shared_cache(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {
            "data": [
                {
                    "model": "hexgrad/Kokoro-82M",
                    "voices": [
                        {"name": "af_bella", "id": "af_bella", "language": "en"},
                        {"name": "af_sky"},
                    ],
                },
                {
                    "model": "canopylabs/orpheus-3b-0.1-ft",
                    "voices": [
                        {"name": "tara", "id": "do-not-send", "language": "en"},
                        {"name": "leah"},
                    ],
                },
                {
                    "model": "cartesia/sonic-2",
                    "voices": [
                        {"name": "Friendly Sidekick", "id": "694f9389-aac1-45b6-b726-9d9369183238", "language": "en"},
                        {"name": "No Id Voice"},
                    ],
                },
            ]
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync:
            found = cfg.fetch_together_tts_voices("https://api.together.xyz", api_key_override="sk-test")
            again = cfg.fetch_together_tts_voices("https://api.together.xyz", api_key_override="sk-test")
            mock_sync.assert_called_once()
            assert mock_sync.call_args[0][0] == "https://api.together.xyz/v1/voices"
            headers = mock_sync.call_args.kwargs.get("headers") or {}
            assert headers.get("Authorization") == "Bearer sk-test"
        assert found == again
        assert cfg.cached_tts_supported_voices("hexgrad/kokoro-82m") == ["af_bella", "af_sky"]
        assert cfg.cached_tts_supported_voices("canopylabs/orpheus-3b-0.1-ft") == ["tara", "leah"]
        assert cfg.cached_tts_supported_voices("cartesia/sonic-2") == [
            "694f9389-aac1-45b6-b726-9d9369183238",
            "No Id Voice",
        ]

    def test_together_voices_filtered_by_model(self):
        from plugin.framework.client import model_fetcher as cfg

        payload = {
            "model": "cartesia/sonic",
            "voices": [
                {"name": "Customer Service", "id": "voice-id-1"},
                {"name": "Narrator", "id": "voice-id-2", "language": "en"},
            ],
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync:
            found = cfg.fetch_together_tts_voices(
                "https://api.together.xyz",
                model_id="cartesia/sonic",
                api_key_override="",
            )
            assert mock_sync.call_args[0][0] == "https://api.together.xyz/v1/voices?model=cartesia%2Fsonic"
            headers = mock_sync.call_args.kwargs.get("headers") or {}
            assert "Authorization" not in headers
        assert found == {"cartesia/sonic": ["voice-id-1", "voice-id-2"]}
        assert cfg.cached_tts_supported_voices("cartesia/sonic") == ["voice-id-1", "voice-id-2"]
        cfg.clear_settings_catalog_cache("https://api.together.xyz", api_key_override="")
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_again:
            cfg.fetch_together_tts_voices(
                "https://api.together.xyz",
                model_id="cartesia/sonic",
                api_key_override="",
            )
            mock_again.assert_called_once()

    def test_together_voices_skip_non_together_host(self):
        from plugin.framework.client import model_fetcher as cfg

        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            assert cfg.fetch_together_tts_voices("https://openrouter.ai/api", model_id="hexgrad/Kokoro-82M") is None
            mock_sync.assert_not_called()

    def test_modality_and_together_voice_results_are_copies(self):
        from plugin.framework.client.model_fetcher import fetch_available_tts_models, fetch_together_tts_voices

        speech_payload = {
            'data': [
                {
                    'id': 'speech-1',
                    'architecture': {'output_modalities': ['speech']},
                }
            ]
        }
        voices_payload = {
            'data': [
                {'model': 'm1', 'voices': [{'name': 'v1'}]}
            ]
        }
        with patch('plugin.framework.client.requests.sync_request', return_value=speech_payload):
            tts1 = fetch_available_tts_models('https://openrouter.ai/api', api_key_override='sk-or')
            assert tts1 == ['speech-1']
            tts1.append('extra-token')
            tts2 = fetch_available_tts_models('https://openrouter.ai/api', api_key_override='sk-or')
            assert tts2 == ['speech-1']
            assert 'extra-token' not in tts2

        with patch('plugin.framework.client.requests.sync_request', return_value=voices_payload):
            voices1 = fetch_together_tts_voices('https://api.together.xyz', api_key_override='sk-tog')
            assert voices1 == {'m1': ['v1']}
            voices1['m1'].append('extra-voice')
            voices2 = fetch_together_tts_voices('https://api.together.xyz', api_key_override='sk-tog')
            assert voices2 == {'m1': ['v1']}
            assert 'extra-voice' not in voices2['m1']


def test_catalog_error_does_not_include_secret(caplog):
    """A /v1/models failure must not log the API key the provider echoed."""
    import logging
    from unittest.mock import MagicMock, patch

    from plugin.framework.client import model_fetcher as cfg
    from plugin.framework.client.request_controls import reset_host_pacing_for_tests

    secret = "sk-catalog-unique-secret"
    endpoint = "https://catalog-secret.example/v1"
    cfg.clear_settings_catalog_cache(endpoint, api_key_override=secret)
    reset_host_pacing_for_tests()
    response = MagicMock()
    response.status = 401
    response.reason = "Unauthorized"
    response.read.return_value = f'{{"error":{{"message":"bad {secret}"}}}}'.encode()
    response.getheader.return_value = None
    conn = MagicMock()
    conn.getresponse.return_value = response
    with caplog.at_level(logging.DEBUG), patch("http.client.HTTPSConnection", return_value=conn):
        assert cfg.fetch_available_models(endpoint, api_key_override=secret) is None
    assert secret not in caplog.text
    assert "<redacted>" in caplog.text


def test_catalog_truncated_json_is_not_a_model_list():
    """Truncated catalog JSON must not be repaired into a finished model id."""
    from unittest.mock import MagicMock, patch

    from plugin.framework.client import model_fetcher as cfg
    from plugin.framework.client.request_controls import reset_host_pacing_for_tests
    from plugin.framework.json_utils import safe_json_loads

    raw = b'{"data":[{"id":"gpt-secret-model"'
    repaired = safe_json_loads(raw)
    assert repaired["data"][0]["id"] == "gpt-secret-model"
    endpoint = "https://catalog-trunc.example/v1"
    cfg.clear_settings_catalog_cache(endpoint, api_key_override="sk-catalog-trunc")
    reset_host_pacing_for_tests()
    response = MagicMock()
    response.status = 200
    response.reason = "OK"
    response.read.return_value = raw
    response.getheader.return_value = None
    conn = MagicMock()
    conn.getresponse.return_value = response
    with patch("http.client.HTTPSConnection", return_value=conn):
        assert cfg.fetch_available_models(endpoint, api_key_override="sk-catalog-trunc") is None


class TestAuthenticatedLocalModelsFetch:
    def teardown_method(self):
        import plugin.framework.client.model_fetcher as mf
        for k in list(mf._model_fetch_cache):
            if "11434" in k or "1234" in k:
                mf._model_fetch_cache.pop(k, None)
        mf._ollama_show_cache.clear()

    def test_fetch_available_models_sends_bearer_key_for_ollama(self):
        from plugin.framework.client.model_fetcher import fetch_available_models
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'llama3'}]}
            fetch_available_models("http://127.0.0.1:11434", api_key_override="secret")
            mock_sync.assert_called_once()
            headers = mock_sync.call_args[1]["headers"]
            assert headers.get("Authorization") == "Bearer secret"

    def test_fetch_available_models_sends_bearer_key_for_lmstudio(self):
        from plugin.framework.client.model_fetcher import fetch_available_models
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': 'qwen'}]}
            fetch_available_models("http://127.0.0.1:1234", api_key_override="lm-secret")
            mock_sync.assert_called_once()
            headers = mock_sync.call_args[1]["headers"]
            assert headers.get("Authorization") == "Bearer lm-secret"

    def test_query_ollama_show_with_key_sends_bearer(self):
        from plugin.framework.client.model_fetcher import query_ollama_show
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value="ollama-key"), \
             patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {"capabilities": ["vision"], "model_info": {}}
            info = query_ollama_show("http://127.0.0.1:11434", "llava")
            mock_sync.assert_called_once()
            args, kwargs = mock_sync.call_args
            assert args[0] == "http://127.0.0.1:11434/api/show"
            assert kwargs["headers"].get("Authorization") == "Bearer ollama-key"
            assert kwargs["headers"].get("Content-Type") == "application/json"
            assert json.loads(kwargs["data"].decode("utf-8")) == {"model": "llava"}
            assert info["capabilities"] == ["vision"]

    def test_query_ollama_show_without_key_sends_no_auth(self):
        from plugin.framework.client.model_fetcher import query_ollama_show
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=""), \
             patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {"capabilities": []}
            query_ollama_show("http://127.0.0.1:11434", "llama3")
            mock_sync.assert_called_once()
            kwargs = mock_sync.call_args[1]
            assert "Authorization" not in kwargs["headers"]


class TestMalformedArchitectureHandling:
    def teardown_method(self):
        import plugin.framework.client.model_fetcher as mf
        for k in list(mf._model_fetch_cache):
            if "58999" in k:
                mf._model_fetch_cache.pop(k, None)

    def test_parse_v1_models_response_handles_string_architecture(self):
        from plugin.framework.client.model_fetcher import _parse_v1_models_response
        body = {
            "data": [
                {"id": "good", "architecture": {"input_modalities": ["image"]}},
                {"id": "bad-arch", "architecture": "nope"},
                {"id": "text-only", "input_modalities": ["text"]},
            ]
        }
        parsed = _parse_v1_models_response(body)
        assert parsed is not None
        models, image_models, vision_declared = parsed
        assert models == ["good", "bad-arch", "text-only"]
        assert vision_declared.get("good") is True
        assert vision_declared.get("text-only") is False
        assert "bad-arch" not in vision_declared
        assert image_models == []

    def test_fetch_available_models_with_string_architecture_does_not_log_exception(self, caplog):
        import logging
        from plugin.framework.client.model_fetcher import fetch_available_models
        caplog.set_level(logging.ERROR, logger="plugin.framework.client.model_fetcher")
        body = {
            "data": [
                {"id": "good", "architecture": {"input_modalities": ["image"]}},
                {"id": "bad-arch", "architecture": "nope"},
                {"id": "text-only", "input_modalities": ["text"]},
            ]
        }
        with patch('plugin.framework.client.requests.sync_request', return_value=body):
            res = fetch_available_models("http://127.0.0.1:58999")
            assert res == ["good", "bad-arch", "text-only"]
            assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_peek_ollama_num_ctx_reads_show_cache_without_http():
    from unittest.mock import MagicMock
    from plugin.framework.client.model_fetcher import _ollama_show_cache, peek_live_ollama_num_ctx
    from plugin.framework.url_utils import normalize_endpoint_url

    endpoint = "http://127.0.0.1:11434"
    model = "llama"
    cache_key = f"{normalize_endpoint_url(endpoint)}@{model}"
    _ollama_show_cache[cache_key] = {"capabilities": [], "num_ctx": 4096}
    client = MagicMock()
    client._get_provider.return_value = "ollama"
    client.config = {"model": model}
    client._endpoint.return_value = endpoint
    try:
        with patch("plugin.framework.client.model_fetcher.query_ollama_show") as show:
            assert peek_live_ollama_num_ctx(client) == 4096
            assert peek_live_ollama_num_ctx(MagicMock(_get_provider=MagicMock(return_value="ollama"), config={"model": "missing"}, _endpoint=MagicMock(return_value=endpoint))) is None
        show.assert_not_called()
    finally:
        _ollama_show_cache.pop(cache_key, None)
