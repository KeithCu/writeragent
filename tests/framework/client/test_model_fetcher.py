import os
import json
import tempfile
from unittest.mock import patch
from plugin.framework.client.model_fetcher import endpoint_url_suitable_for_v1_models_fetch

class TestEndpointUrlSuitableForModelFetch:

    def test_incomplete_or_invalid_urls_rejected(self):
        assert not (endpoint_url_suitable_for_v1_models_fetch(''))
        assert not (endpoint_url_suitable_for_v1_models_fetch('http:/'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('http://'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('ftp://api.openai.com'))
        assert not (endpoint_url_suitable_for_v1_models_fetch('not-a-url'))

    def test_complete_urls_accepted(self):
        assert (endpoint_url_suitable_for_v1_models_fetch('http://localhost:1234'))
        assert (endpoint_url_suitable_for_v1_models_fetch('https://api.openai.com/v1'))
        assert (endpoint_url_suitable_for_v1_models_fetch('http://127.0.0.1:11434'))
        assert (endpoint_url_suitable_for_v1_models_fetch('http://[::1]:8080'))


class TestFetchAvailableModelsCache:
    '_model_fetch_cache is process-wide; same normalized endpoint hits HTTP once.'

    def teardown_method(self):
        import plugin.framework.client.model_fetcher as cfg
        keys_to_del = [k for k in cfg._model_fetch_cache if (('127.0.0.1:5890' in k))]
        for k in keys_to_del:
            del cfg._model_fetch_cache[k]
            cfg._model_fetch_image_cache.pop(k, None)
            cfg._model_context_cache.pop(k, None)

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
        assert (k_saved) == (f'{url}\x1fsaved')
        assert (k_a) == (f'{url}\x1ftyped-a')
        assert (k_b) == (f'{url}\x1ftyped-b')


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
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
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
            with patch('plugin.framework.config._config_path', side_effect=mock_config_path):
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
        from plugin.framework.client.model_fetcher import get_model_capability
        from plugin.framework.constants import ModelCapability

        caps = get_model_capability('openai/gpt-oss-120b:nitro', 'https://openrouter.ai/api')
        assert (isinstance(caps, int) and (caps & ModelCapability.TOOLS))


class TestHasNativeAudio:
    def test_audio_only_stt_model_is_not_native_audio(self):
        from plugin.framework.client.model_fetcher import has_native_audio
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            result = has_native_audio('mistralai/voxtral-mini-transcribe', 'https://openrouter.ai/api')
        assert (result) is not (True)

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
        import plugin.framework.client.model_fetcher as mf
        mf._model_fetch_vision_cache.clear()
        mf._ollama_capabilities_cache.clear()

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

        def mock_set_config(key, val):
            if key == "vision_support_map":
                cache_dict.update(val)

        with patch('plugin.framework.client.model_fetcher.get_config', side_effect=mock_get_config), \
             patch('plugin.framework.client.model_fetcher.set_config', side_effect=mock_set_config):
            set_native_vision_support('my-custom-model', 'http://localhost:11434', True)
            assert (has_native_vision('my-custom-model', 'http://localhost:11434'))

            set_native_vision_support('my-custom-model', 'http://localhost:11434', False)
            assert not (has_native_vision('my-custom-model', 'http://localhost:11434'))

    def test_openrouter_dynamic_modality_detection(self):
        from plugin.framework.client.model_fetcher import has_native_vision, _model_fetch_vision_cache
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            
            url = 'https://openrouter.ai/api/v1/models'
            from plugin.framework.client.model_fetcher import _model_fetch_cache_key
            ck = _model_fetch_cache_key(url, 'https://openrouter.ai/api')
            _model_fetch_vision_cache[ck] = ['custom-openrouter-vision-model']

            assert (has_native_vision('custom-openrouter-vision-model', 'https://openrouter.ai/api'))
            assert not (has_native_vision('some-other-model', 'https://openrouter.ai/api'))

    def test_ollama_api_show_capabilities(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        
        with patch('plugin.framework.client.requests.sync_request') as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            mock_sync.return_value = {"capabilities": ["vision"]}
            assert (has_native_vision('llava', 'http://localhost:11434'))
            mock_sync.assert_called_once()

    def test_name_heuristics_removed(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        with patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            assert not (has_native_vision('unknown-vision-model', 'https://api.openai.com/v1'))


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
            assert not (mf.query_ollama_model_capabilities("http://localhost:11434", "qwen2.5:7b"))
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
            for key in [k for k in store if "58923" in k]:
                store.pop(key, None)

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
