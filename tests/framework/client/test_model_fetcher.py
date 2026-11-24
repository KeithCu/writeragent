from plugin.framework.queue_executor import main
import os
import json
import tempfile
from unittest.mock import patch
from plugin.framework.client.model_fetcher import endpoint_url_suitable_for_v1_models_fetch
import pytest
from tests.harness.strip_bundle import requires_asserts

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
    '_catalog_memos is process-wide; same normalized endpoint hits HTTP once.'

    def teardown_method(self):
        import plugin.framework.client.model_fetcher as cfg
        keys_to_del = [k for k in cfg._catalog_memos if any(sub in k for sub in (('127.0.0.1:5890'), 'openwebui.example', 'notopenwebui.example', '127.0.0.1:3000'))]
        for k in keys_to_del:
            cfg._catalog_memos.pop(k, None)

    def test_openwebui_host_catalog_url_strips_pasted_api(self):
        from plugin.framework.client.model_fetcher import fetch_available_models

        payload = {'data': [{'id': 'm1'}]}
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config', return_value=False):
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
             patch('plugin.framework.client.model_fetcher.get_config', return_value=True):
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

    def test_catalog_memo_key_differs_for_override(self):
        from plugin.framework.client import model_fetcher as cfg
        endpoint = 'http://127.0.0.1:58906'
        with patch.object(cfg, 'get_api_key_for_endpoint', return_value='saved'):
            k_saved = cfg._catalog_memo_key(endpoint, None)
            k_a = cfg._catalog_memo_key(endpoint, 'typed-a')
            k_b = cfg._catalog_memo_key(endpoint, 'typed-b')
        assert k_saved.startswith(f'{endpoint}/v1\x1f')
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

    def test_fetch_available_models_unexpected_body_returns_none(self):
        from plugin.framework.client import model_fetcher as cfg
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'unexpected': True}
            result = cfg.fetch_available_models('http://127.0.0.1:58909')
            assert result is None


class TestTextModelPlaceholderGuards:

    def test_get_text_model_skips_connection_failed_placeholder(self):
        from plugin.framework.client.model_fetcher import get_text_model
        from plugin.framework.default_models import get_provider_defaults

        with patch('plugin.framework.client.model_fetcher.get_config', return_value='(Connection failed)'):
            with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value='https://api.z.ai/api/paas'):
                assert get_text_model() == get_provider_defaults("zai")["text_model"]

    def test_set_text_model_ignores_placeholder(self):
        from plugin.framework.client.model_fetcher import set_text_model

        with patch('plugin.framework.client.model_fetcher.save') as mock_set:
            set_text_model('(Connection failed)', update_lru=False)
            mock_set.assert_not_called()

    def test_cerebras_and_deepseek_presets_resolve_a_tool_chat_default(self):
        # Fresh Cerebras configs resolve text_model to "" unless Cerebras has a
        # default_text catalog entry; DeepSeek must resolve to a chat+tools default too.
        from plugin.framework.constants import ModelCapability
        from plugin.framework.client.model_fetcher import ENDPOINT_PRESETS, get_text_model
        from plugin.framework.default_models import get_model_capability

        targets = {label: url for label, url in ENDPOINT_PRESETS if label in ("Cerebras", "DeepSeek")}
        assert set(targets) == {"Cerebras", "DeepSeek"}
        for url in targets.values():
            with patch('plugin.framework.client.model_fetcher.get_config', return_value=''):
                with patch('plugin.framework.client.model_fetcher.get_current_endpoint', return_value=url):
                    model_id = get_text_model()
            assert model_id
            caps = get_model_capability(model_id, url)
            assert isinstance(caps, int)
            assert caps & ModelCapability.CHAT
            assert caps & ModelCapability.TOOLS

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
                for k in list(cfg._catalog_memos):
                    if ('58904' in k):
                        cfg._catalog_memos.pop(k, None)
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
                for k in list(cfg._catalog_memos):
                    if ('58905' in k):
                        cfg._catalog_memos.pop(k, None)
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
            pytest.param('mistralai/voxtral-mini-transcribe', 'https://openrouter.ai/api', False, id="audio-only-stt"),
            pytest.param('whisper-1', 'https://example.invalid/v1', False, id="uncatalogued-whisper"),
            pytest.param('some-chat-model', 'https://example.invalid/v1', None, id="unknown-model"),
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
        self._clear_catalog_memos()

    def teardown_method(self):
        self._clear_catalog_memos()

    @staticmethod
    def _clear_catalog_memos():
        import plugin.framework.client.model_fetcher as cfg

        for k in list(cfg._catalog_memos):
            if '58907' in k or '58908' in k or 'together.xyz' in k or 'openrouter.ai' in k:
                cfg._catalog_memos.pop(k, None)

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

    def test_vision_declarations_read_capabilities_vision_key_and_aliases(self):
        from plugin.framework.client.model_fetcher import _vision_declarations_from_v1_entries

        entries = [
            {'id': 'mistral-small-2506', 'aliases': ['mistral-small-latest'], 'capabilities': {'vision': True}},
            {'id': 'codestral-2508', 'capabilities': {'vision': False}},
            {'key': 'google/gemma-4-26b-a4b', 'capabilities': {'vision': True}},
            {'id': 'odd', 'capabilities': {'vision': 'yes'}},
            {'id': 'both', 'input_modalities': ['text'], 'capabilities': {'vision': True}},
            {'id': 'claude-opus-5-5', 'capabilities': {'image_input': {'supported': True}}},
            {'id': 'claude-text-only', 'capabilities': {'image_input': {'supported': False}}},
        ]
        assert _vision_declarations_from_v1_entries(entries) == {
            'mistral-small-2506': True,
            'mistral-small-latest': True,
            'codestral-2508': False,
            'google/gemma-4-26b-a4b': True,
            'both': False,
            'claude-opus-5-5': True,
            'claude-text-only': False,
        }

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
        self._patch_main_thread = patch.object(main, "is_current", return_value=False)
        self._patch_main_thread.start()
        import plugin.framework.client.model_fetcher as mf
        mf._catalog_memos.clear()
        mf._vision_in_flight.clear()

    def teardown_method(self):
        self._patch_main_thread.stop()

    @pytest.fixture
    def vision_config(self):
        saved: dict = {}

        def mock_get_config(key):
            if key == "vision_support_map":
                return dict(saved)
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == "vision_support_map":
                mutator(saved)

        with patch("plugin.framework.client.model_fetcher.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.client.model_fetcher.update_config_mapping", side_effect=mock_update_config_mapping):
            yield saved

    def test_config_cache_has_vision(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision, set_native_vision_support

        set_native_vision_support('my-custom-model', 'http://localhost:11434', True)
        assert (has_native_vision('my-custom-model', 'http://localhost:11434'))

        set_native_vision_support('my-custom-model', 'http://localhost:11434', False)
        assert not (has_native_vision('my-custom-model', 'http://localhost:11434'))

    def test_openrouter_dynamic_modality_detection(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        with patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''), \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}), \
             patch('plugin.framework.client.model_fetcher.save'):

            import plugin.framework.client.model_fetcher as mf
            memo = mf._get_catalog_memo('https://openrouter.ai/api')
            memo.vision_declarations = {'custom-openrouter-vision-model': True}

            assert (has_native_vision('custom-openrouter-vision-model', 'https://openrouter.ai/api'))
            assert not (has_native_vision('some-other-model', 'https://openrouter.ai/api'))

    def test_ollama_api_show_capabilities(self):
        from plugin.framework.client.model_fetcher import has_native_vision
        
        with patch('plugin.framework.client.requests.sync_request') as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_config', return_value={}):
            mock_sync.return_value = {"capabilities": ["vision"]}
            assert has_native_vision('llava', 'http://localhost:11434') is True
            mock_sync.assert_called_once()

    def test_openrouter_cache_miss_fetches_modalities_for_uncatalogued_gemini(self, vision_config):
        # google/gemini-3.8-flash is not a DEFAULT_MODELS row. The sidebar does
        # not fill CatalogMemo.vision_declarations, so has_native_vision must GET
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

        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''):
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is True
            assert mock_sync.call_count == 1
            # Lookups never write vision_support_map: map stays empty.
            assert vision_config == {}
            # In-process memo answers the next call; do not GET the catalog again.
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is True
            assert mock_sync.call_count == 1
            # :nitro is the same catalog row (dynamic OpenRouter suffix).
            assert has_native_vision('google/gemini-3.8-flash:nitro', 'https://openrouter.ai/api') is True
            assert has_native_vision('deepseek/deepseek-chat', 'https://openrouter.ai/api') is False
            assert has_native_vision('inception/mercury-2.5', 'https://openrouter.ai/api') is False
            assert vision_config == {}

    def test_omitted_input_modalities_does_not_persist_false(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision

        payload = {
            'data': [
                {'id': 'google/gemini-3.8-flash'},
                {'id': 'quiet-model', 'architecture': {'output_modalities': ['text']}},
            ]
        }

        endpoint = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''):
            assert has_native_vision('google/gemini-3.8-flash', endpoint) is None
            assert vision_config == {}
            assert mock_sync.call_count == 1
            assert has_native_vision('quiet-model', endpoint) is None
            assert vision_config == {}

    def test_explicit_text_modalities_answer_false_without_persisting(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision

        payload = {
            'data': [
                {'id': 'text-only-a', 'architecture': {'input_modalities': ['text']}},
                {'id': 'also-text', 'input_modalities': []},
            ]
        }

        endpoint = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload), \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''):
            assert has_native_vision('text-only-a', endpoint) is False
            assert has_native_vision('also-text', endpoint) is False
            assert vision_config == {}

    def test_mixed_modalities_answer_only_stated_rows(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision

        payload = {
            'data': [
                {'id': 'sees', 'architecture': {'input_modalities': ['text', 'image']}},
                {'id': 'blind', 'architecture': {'input_modalities': ['text']}},
                {'id': 'quiet'},
            ]
        }

        endpoint = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.requests.sync_request', return_value=payload), \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''):
            assert has_native_vision('sees', endpoint) is True
            assert has_native_vision('blind', endpoint) is False
            assert has_native_vision('quiet', endpoint) is None
            assert has_native_vision('sees:nitro', endpoint) is True
            assert vision_config == {}

    def test_failed_modalities_fetch_does_not_persist_false(self, vision_config):
        # A down catalog is not a text-only answer. Do not write False into
        # vision_support_map or the next process can never recover.
        from plugin.framework.client.model_fetcher import has_native_vision

        with patch('plugin.framework.client.requests.sync_request', side_effect=OSError('down')), \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''):
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is None
            assert vision_config == {}

    def test_vision_support_map_false_skips_modalities_fetch(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision
        vision_config['https://openrouter.ai/api@google/gemini-3.8-flash'] = False

        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            assert has_native_vision('google/gemini-3.8-flash', 'https://openrouter.ai/api') is False
            mock_sync.assert_not_called()

    def test_warm_memo_answers_without_fetch(self):
        from plugin.framework.client import model_fetcher

        saved = dict(model_fetcher._catalog_memos)
        model_fetcher._catalog_memos.clear()
        try:
            memo = model_fetcher._get_catalog_memo("https://openrouter.ai/api")
            memo.vision_declarations = {"known-model": True}
            with patch.object(model_fetcher, "get_config", return_value={}), \
                 patch.object(model_fetcher, "fetch_available_models") as fetch, \
                 patch.object(model_fetcher, "save"):
                assert model_fetcher.has_native_vision(
                    "known-model",
                    "https://openrouter.ai/api",
                ) is True
                fetch.assert_not_called()
        finally:
            model_fetcher._catalog_memos.clear()
            model_fetcher._catalog_memos.update(saved)

    def test_vision_memo_honors_api_key_override(self, vision_config):
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

        with patch('plugin.framework.client.requests.sync_request', return_value=payload) as mock_sync, \
             patch('plugin.framework.client.model_fetcher.get_api_key_for_endpoint', return_value=''):
            fetch_available_models(endpoint, api_key_override='sk-dialog')
            assert mock_sync.call_count == 1

            assert has_native_vision('google/gemini-3.8-flash', endpoint, api_key_override=' sk-dialog ') is True
            assert mock_sync.call_count == 1

    def test_ui_thread_never_fetches_and_schedules_background_job(self, vision_config):
        from plugin.framework.client import model_fetcher
        from plugin.framework.client.model_fetcher import has_native_vision

        scheduled_jobs = []

        def fake_bg(fn, *args, **kwargs):
            scheduled_jobs.append(fn)

        with patch.object(main, "is_current", return_value=True), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=fake_bg), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models") as mock_fetch, \
             patch("plugin.framework.client.model_fetcher.query_ollama_show") as mock_show, \
             patch("plugin.framework.client.requests.sync_request") as mock_sync:

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

            # Now run the captured OpenRouter job (with is_current returning False)
            openrouter_job = scheduled_jobs[0]
            with patch.object(main, "is_current", return_value=False):
                def fake_fetch(ep, api_key_override=None):
                    memo = model_fetcher._get_catalog_memo("https://openrouter.ai/api", api_key_override)
                    memo.vision_declarations = {"google/gemini-3.8-flash": True}
                mock_fetch.side_effect = fake_fetch
                openrouter_job()
                mock_fetch.assert_called_once()

            # Now next UI thread call returns the real answer without scheduling new job
            scheduled_jobs.clear()
            res4 = has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api")
            assert res4 is True
            assert len(scheduled_jobs) == 0
            assert vision_config == {}

    @requires_asserts
    def test_fetch_branch_asserts_off_ui_thread(self):
        from plugin.framework.client.model_fetcher import has_native_vision

        calls = [False, True]
        with patch.object(main, "is_current", side_effect=lambda: calls.pop(0)):
            with pytest.raises(AssertionError):
                has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api")

    def test_retry_after_failure_inline_and_ui(self):
        from plugin.framework.client.model_fetcher import has_native_vision

        # 1. Inline path (off UI thread)
        with patch.object(main, "is_current", return_value=False), \
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
            import plugin.framework.client.model_fetcher as mf
            mf._catalog_memos[mf._catalog_memo_key("https://openrouter.ai/api")].fetch_failed_at -= mf.CATALOG_RETRY_S + 1.0
            assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is True
            assert mock_sync.call_count == 2

        # 2. UI path: failed job clears in-flight key so next UI call schedules again
        mf._catalog_memos.clear()

        scheduled = []
        with patch.object(main, "is_current", return_value=True), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=lambda fn, **kw: scheduled.append(fn)):
            assert has_native_vision("google/gemini-uncatalogued-fail", "https://openrouter.ai/api") is None
            assert len(scheduled) == 1

            job = scheduled.pop()
            with patch.object(main, "is_current", return_value=False), \
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
            if key == "is_openwebui":
                return True
            return {}

        def mock_update_config_mapping(key, mutator, event_key=None):
            if key == "vision_support_map":
                mutator(saved)

        with patch("plugin.framework.client.model_fetcher.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.client.model_fetcher.update_config_mapping", side_effect=mock_update_config_mapping), \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:

            set_native_vision_support("my-model", "http://openwebui.example/api", True)
            assert has_native_vision("my-model", "http://openwebui.example") is True
            mock_bg.assert_not_called()

            set_native_vision_support("google/gemini-3.8-flash:nitro", "https://openrouter.ai/api", False)
            assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is False
            with patch.object(main, "is_current", return_value=True):
                assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is False
                mock_bg.assert_not_called()

            # Static catalog fallback (Z.ai has no live vision API) settles without scheduling.
            with patch.object(main, "is_current", return_value=True):
                assert has_native_vision("glm-5.3-flash", "https://api.z.ai/api/paas/v4") is True
                mock_bg.assert_not_called()

    def test_clear_settings_catalog_cache_drops_vision_memo_and_ollama_show(self):
        from plugin.framework.client import model_fetcher
        from plugin.framework.client.model_fetcher import clear_settings_catalog_cache, has_native_vision

        memo = model_fetcher._get_catalog_memo("https://openrouter.ai/api")
        memo.vision_declarations = {"google/gemini-3.8-flash": True}

        ollama_ep = "http://localhost:11434"
        ollama_memo = model_fetcher._get_catalog_memo(ollama_ep)
        ollama_memo.ollama_show["llava"] = {"capabilities": ["vision"]}

        assert has_native_vision("google/gemini-3.8-flash", "https://openrouter.ai/api") is True
        assert has_native_vision("llava", ollama_ep) is True

        clear_settings_catalog_cache("https://openrouter.ai/api")
        assert model_fetcher._catalog_memo_key("https://openrouter.ai/api") not in model_fetcher._catalog_memos

        clear_settings_catalog_cache(ollama_ep)
        assert model_fetcher._catalog_memo_key(ollama_ep) not in model_fetcher._catalog_memos

    def test_anthropic_live_lookup_capabilities_image_input(self, vision_config):
        from plugin.framework.client.model_fetcher import clear_settings_catalog_cache, has_native_vision

        payload = {
            "data": [
                {"id": "claude-opus-5-5", "capabilities": {"image_input": {"supported": True}}},
                {"id": "claude-text-only", "capabilities": {"image_input": {"supported": False}}},
            ]
        }
        for endpoint in ("https://api.anthropic.com", "https://api.anthropic.com/v1"):
            clear_settings_catalog_cache(endpoint, api_key_override="sk-ant-test")
            with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync, \
                 patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value="sk-ant-test"):
                assert has_native_vision("claude-opus-5-5", endpoint) is True
                assert has_native_vision("claude-text-only", endpoint) is False
                assert has_native_vision("claude-unlisted", endpoint) is None
                assert mock_sync.call_count == 1
                url = mock_sync.call_args.args[0]
                headers = mock_sync.call_args.kwargs["headers"]
                assert url == "https://api.anthropic.com/v1/models"
                assert headers.get("x-api-key") == "sk-ant-test"
                assert "anthropic-version" in headers

            clear_settings_catalog_cache(endpoint, api_key_override="sk-ant-test")
            with patch("plugin.framework.client.requests.sync_request", side_effect=OSError("down")), \
                 patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value="sk-ant-test"):
                assert has_native_vision("claude-opus-5-5", endpoint) is None
        assert vision_config == {}

    def test_mistral_and_deepseek_live_lookup(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision

        mistral_ep = "https://api.mistral.ai"
        mistral_payload = {
            "data": [
                {"id": "mistral-small-2506", "aliases": ["mistral-small-latest"], "capabilities": {"vision": True}},
                {"id": "codestral-2508", "capabilities": {"vision": False}},
            ]
        }
        deepseek_ep = "https://api.deepseek.com"
        deepseek_payload = {
            "data": [
                {"id": "deepseek-flash", "input_modalities": ["text", "image"]},
                {"id": "deepseek-v4-pro", "input_modalities": ["text"]},
            ]
        }

        def fake_sync(url, **kwargs):
            if "mistral.ai" in url:
                return mistral_payload
            return deepseek_payload

        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync) as mock_sync, \
             patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value="sk-test"):
            assert has_native_vision("mistral-small-latest", mistral_ep) is True
            assert has_native_vision("codestral-2508", mistral_ep) is False
            assert has_native_vision("deepseek-flash", deepseek_ep) is True
            assert has_native_vision("deepseek-v4-pro", deepseek_ep) is False
            assert has_native_vision("deepseek-chat", deepseek_ep) is None
            assert mock_sync.call_count == 2
        assert vision_config == {}

    def test_xai_language_models_answer_survives_settings_fetch(self, vision_config):
        from plugin.framework.client.model_fetcher import fetch_available_models, has_native_vision

        xai_ep = "https://api.x.ai/v1"

        def fake_sync(url, **kwargs):
            if url.endswith("/language-models"):
                return {"models": [{"id": "grok-x", "input_modalities": ["text", "image"], "aliases": ["grok-x-latest"]}]}
            if url.endswith("/models"):
                return {"data": [{"id": "grok-x"}]}
            raise AssertionError(f"unexpected URL: {url}")

        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync), \
             patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value="sk-xai"):
            assert has_native_vision("grok-x-latest", xai_ep) is True
            fetch_available_models(xai_ep)
            assert has_native_vision("grok-x-latest", xai_ep) is True

    def test_lmstudio_api_v1_models_capabilities_vision(self, vision_config):
        from plugin.framework.client.model_fetcher import clear_settings_catalog_cache, has_native_vision

        lms_ep = "http://localhost:1234"
        lms_payload = {
            "models": [
                {"type": "llm", "key": "qwen/qwen3-vl-8b", "capabilities": {"vision": True}},
                {"type": "llm", "key": "qwen/qwen3-4b", "capabilities": {"vision": False}},
            ]
        }

        with patch("plugin.framework.client.requests.sync_request", return_value=lms_payload) as mock_sync:
            assert has_native_vision("qwen/qwen3-vl-8b", lms_ep) is True
            assert has_native_vision("qwen/qwen3-4b", lms_ep) is False
            assert mock_sync.call_count == 1
            assert mock_sync.call_args.args[0] == "http://localhost:1234/api/v1/models"

        clear_settings_catalog_cache(lms_ep)
        with patch("plugin.framework.client.requests.sync_request", side_effect=[OSError("conn refused"), lms_payload]) as mock_sync:
            assert has_native_vision("qwen/qwen3-vl-8b", lms_ep) is None
            assert has_native_vision("qwen/qwen3-vl-8b", lms_ep) is True
            assert mock_sync.call_count == 2

    def test_provider_default_after_catalog_and_config(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision, set_native_vision_support

        google_ep = "https://generativelanguage.googleapis.com/v1beta/openai"
        with patch("plugin.framework.client.requests.sync_request") as mock_sync, \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:
            with patch.object(main, "is_current", return_value=True):
                assert has_native_vision("gemini-4-carbon", google_ep) is True
                assert has_native_vision("gemini-3.1-flash-lite-preview", google_ep) is True
            with patch.object(main, "is_current", return_value=False):
                assert has_native_vision("gemini-4-carbon", google_ep) is True
            mock_sync.assert_not_called()
            mock_bg.assert_not_called()

        set_native_vision_support("gemini-4-carbon", google_ep, False)
        assert has_native_vision("gemini-4-carbon", google_ep) is False

    def test_live_answer_beats_conflicting_catalog_and_catalog_used_on_fetch_failure(self, vision_config):
        from plugin.framework.client.model_fetcher import clear_settings_catalog_cache, has_native_vision
        from plugin.framework.constants import ModelCapability

        endpoint = "https://openrouter.ai/api"
        model = "custom/model-x"
        payload = {"data": [{"id": model, "architecture": {"input_modalities": ["text"]}}]}

        with patch("plugin.framework.client.model_fetcher.get_model_capability", return_value=ModelCapability.CHAT | ModelCapability.VISION):
            # Live API says text-only (False), beating the catalog's VISION bit.
            with patch("plugin.framework.client.requests.sync_request", return_value=payload), \
                 patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""):
                assert has_native_vision(model, endpoint) is False

            # When the live fetch fails, fall back to the catalog VISION bit.
            clear_settings_catalog_cache(endpoint)
            with patch("plugin.framework.client.requests.sync_request", side_effect=OSError("down")), \
                 patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""):
                assert has_native_vision(model, endpoint) is True

    def test_ui_thread_schedules_alternate_list_lookup(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision

        xai_ep = "https://api.x.ai/v1"
        scheduled_jobs = []

        def fake_bg(fn, *args, **kwargs):
            scheduled_jobs.append(fn)

        payload = {"models": [{"id": "grok-x", "input_modalities": ["text", "image"], "aliases": ["grok-x-latest"]}]}
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync, \
             patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value="sk-xai"), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=fake_bg):
            with patch.object(main, "is_current", return_value=True):
                assert has_native_vision("grok-x-latest", xai_ep) is None
                mock_sync.assert_not_called()
                assert len(scheduled_jobs) == 1

            with patch.object(main, "is_current", return_value=False):
                scheduled_jobs[0]()
            assert mock_sync.call_count == 1

            with patch.object(main, "is_current", return_value=True):
                assert has_native_vision("grok-x-latest", xai_ep) is True
                assert len(scheduled_jobs) == 1

    def test_unlisted_provider_stays_unknown_without_fetch(self, vision_config):
        from plugin.framework.client.model_fetcher import has_native_vision

        with patch("plugin.framework.client.requests.sync_request") as mock_sync, \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:
            assert has_native_vision("some/uncatalogued", "https://api.groq.com/openai") is None
            assert has_native_vision("meta/llama-3.2-11b-vision-instruct", "https://integrate.api.nvidia.com/v1") is None
            mock_sync.assert_not_called()
            mock_bg.assert_not_called()


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

        mf._catalog_memos.clear()
        show = {
            "capabilities": ["completion"],
            "parameters": "num_ctx 4096\n",
            "model_info": {"qwen2.context_length": 32768},
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=show) as mock_sync:
            assert (mf.query_ollama_runtime_num_ctx("http://localhost:11434", "qwen2.5:7b")) == (4096)
            assert (mock_sync.call_count) == (1)
        mf._catalog_memos.clear()
        with patch("plugin.framework.client.requests.sync_request", side_effect=OSError("down")):
            assert (mf.query_ollama_runtime_num_ctx("http://localhost:11434", "qwen2.5:7b")) is None


class TestFilterFetchedModels:
    def test_audio_filter_includes_asr_models(self):
        from plugin.framework.client.model_fetcher import filter_fetched_models

        models = ["glm-5.2", "glm-asr-2512", "whisper-1", "gpt-4o"]
        out = filter_fetched_models(models, "audio")
        assert ("glm-asr-2512") in (out)
        assert ("whisper-1") in (out)
        assert ("glm-5.2") not in (out)
        assert ("gpt-4o") not in (out)

    def test_text_filter_keeps_vision_and_coder_chat_models(self):
        from plugin.framework.client.model_fetcher import filter_fetched_models

        models = ["qwen2.5-coder", "llava", "gpt-4-vision", "whisper-1", "text-embedding-3-small", "deepseek-coder", "gpt-4o"]
        out = filter_fetched_models(models, "text")
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

        for key in [k for k in list(mf._catalog_memos) if "58923" in k or "openwebui.example" in k]:
            mf._catalog_memos.pop(key, None)

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
    def test_audio_stt_model_read(self):
        from plugin.framework.client.model_fetcher import get_stt_model

        with patch("plugin.framework.client.model_fetcher.get_config", return_value="speech-new"):
            assert get_stt_model() == "speech-new"

    def test_default_from_provider_when_both_empty(self):
        from plugin.framework.client.model_fetcher import get_stt_model
        from plugin.framework.default_models import get_provider_defaults

        with patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
            with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api/v1"):
                res = get_stt_model()
                assert res and res == get_provider_defaults("openrouter")["stt_model"]


class TestGetTtsModel:
    def test_explicit_config_wins(self):
        from plugin.framework.client.model_fetcher import get_tts_model

        with patch("plugin.framework.client.model_fetcher.get_config", return_value="custom-tts"):
            assert get_tts_model() == "custom-tts"

    def test_default_from_provider(self):
        from plugin.framework.client.model_fetcher import get_tts_model
        from plugin.framework.default_models import get_provider_defaults

        with patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
            with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api/v1"):
                res = get_tts_model()
                assert res and res == get_provider_defaults("openrouter")["tts_model"]


class TestFetchAvailableSpeechModels:
    def setup_method(self):
        self._clear()

    def teardown_method(self):
        self._clear()

    @staticmethod
    def _clear():
        from plugin.framework.client import model_fetcher as cfg

        cfg._catalog_memos.clear()

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
        for k in list(mf._catalog_memos):
            if "11434" in k or "1234" in k:
                mf._catalog_memos.pop(k, None)

    @pytest.mark.parametrize(
        ("endpoint", "api_key", "model_id"),
        [
            pytest.param("http://127.0.0.1:11434", "secret", "llama3", id="ollama"),
            pytest.param("http://127.0.0.1:1234", "lm-secret", "qwen", id="lmstudio"),
        ],
    )
    def test_fetch_available_models_sends_bearer_key_for_local_endpoints(self, endpoint, api_key, model_id):
        from plugin.framework.client.model_fetcher import fetch_available_models
        with patch('plugin.framework.client.requests.sync_request') as mock_sync:
            mock_sync.return_value = {'data': [{'id': model_id}]}
            fetch_available_models(endpoint, api_key_override=api_key)
            mock_sync.assert_called_once()
            headers = mock_sync.call_args[1]["headers"]
            assert headers.get("Authorization") == f"Bearer {api_key}"

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
        for k in list(mf._catalog_memos):
            if "58999" in k:
                mf._catalog_memos.pop(k, None)

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
    from plugin.framework.client.model_fetcher import _get_catalog_memo, clear_settings_catalog_cache, peek_live_ollama_num_ctx

    endpoint = "http://127.0.0.1:11434"
    model = "llama"
    memo = _get_catalog_memo(endpoint)
    memo.ollama_show[model] = {"capabilities": [], "num_ctx": 4096}
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
        clear_settings_catalog_cache(endpoint)


def test_test_connection_keeps_learned_tts_voices_and_response_format():
    """Test Connection clears the endpoint memo, not per-model speech state."""
    import plugin.framework.client.model_fetcher as cfg

    cfg._tts_supported_voices.clear()
    cfg._tts_response_format.clear()
    endpoint = "https://openrouter.ai/api"
    model = "google/gemini-2.5-flash-preview-tts"
    cfg.remember_tts_supported_voices(model, ["Puck", "Kore"])
    cfg.remember_tts_response_format(model, "pcm")
    cfg._catalog_memos[cfg._catalog_memo_key(endpoint, "k")] = cfg.CatalogMemo(ids=["a"])

    cfg.clear_settings_catalog_cache(endpoint, api_key_override="k")

    assert cfg._catalog_memo_key(endpoint, "k") not in cfg._catalog_memos
    assert cfg.cached_tts_supported_voices(model.upper()) == ["Puck", "Kore"]
    assert cfg.cached_tts_response_format(model) == "pcm"
    cfg._tts_supported_voices.clear()
    cfg._tts_response_format.clear()


def test_get_tts_model_sanitizes_placeholder():
    from plugin.framework.client.model_fetcher import get_tts_model

    with patch("plugin.framework.client.model_fetcher.get_config", return_value="(Default for current endpoint)"):
        with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"):
            model = get_tts_model()
            assert model
            assert "(Default for current endpoint)" not in model


def test_is_image_only_model():
    from plugin.framework.client.model_fetcher import is_image_only_model

    # The image-id list is the catalog. A gemini image id is image-only even
    # though the name heuristic would treat "gemini" as chat.
    with patch("plugin.framework.client.model_fetcher.fetch_available_image_models", return_value=["flux", "google/gemini-2.5-flash-image"]):
        assert is_image_only_model("https://openrouter.ai/api", "flux") is True
        assert is_image_only_model("https://openrouter.ai/api", "google/gemini-2.5-flash-image") is True
        assert is_image_only_model("https://openrouter.ai/api", "google/gemini-2.5-flash") is False

    # None means the catalog did not answer; the name heuristic still applies.
    with patch("plugin.framework.client.model_fetcher.fetch_available_image_models", return_value=None):
        assert is_image_only_model("https://openrouter.ai/api", "nonexistent-flux") is True
        assert is_image_only_model("https://openrouter.ai/api", "nonexistent-gemini") is False


def test_ui_thread_skips_provider_memo_until_key_read(fake_keystore):
    from plugin.framework import keystore
    from plugin.framework.client import model_fetcher
    from plugin.framework.config import get_api_key_for_endpoint, save
    from plugin.framework.queue_executor import main

    url = "https://openrouter.ai/api"
    save({"api_keys_by_endpoint": {url: "sk-1"}})
    keystore.reset_for_tests(fake_keystore)
    fake_keystore.get_calls = 0

    with patch.object(model_fetcher, "run_in_background") as bg_lookup:
        with patch.object(main, "is_current", return_value=True):
            model_fetcher.has_native_vision("vendor/cold-vision-model", url)
            assert fake_keystore.get_calls == 0
            assert bg_lookup.call_count == 0

        with patch.object(main, "is_current", return_value=False):
            assert get_api_key_for_endpoint(url) == "sk-1"

        with patch.object(main, "is_current", return_value=True):
            model_fetcher.has_native_vision("vendor/cold-vision-model", url)
            assert bg_lookup.call_count == 1


def test_memo_text_models_or_fetch_warm_returns_ids_without_http():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "http://localhost:11434"
    cfg._catalog_memos.clear()
    try:
        with patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
             patch("plugin.framework.client.requests.sync_request") as mock_http, \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:
            cfg._catalog_memos[cfg._catalog_memo_key(endpoint)] = cfg.CatalogMemo(ids=["llama3", "mistral"])
            got = cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="test")
        assert got == ["llama3", "mistral"]
        mock_http.assert_not_called()
        mock_bg.assert_not_called()
    finally:
        cfg._catalog_memos.clear()


def test_memo_text_models_or_fetch_cold_one_job_posts_all_waiters():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "http://localhost:11434"
    cfg._catalog_memos.clear()
    cfg._catalog_fetch_waiters.clear()
    jobs = []
    posted = []
    cb1_calls = []
    cb2_calls = []
    cb3_calls = []

    try:
        with patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch("plugin.framework.client.model_fetcher.main.post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models", return_value=["m"]):
            assert cfg.memo_text_models_or_fetch(endpoint, cb1_calls.append, task_name="t1") is None
            assert cfg.memo_text_models_or_fetch(endpoint, cb2_calls.append, task_name="t2") is None
            assert len(jobs) == 1

            jobs[0]()
            assert len(posted) == 1
            posted[0]()
            assert cb1_calls == [["m"]]
            assert cb2_calls == [["m"]]

            assert cfg.memo_text_models_or_fetch(endpoint, cb3_calls.append, task_name="t3") is None
            assert len(jobs) == 2
    finally:
        cfg._catalog_memos.clear()
        # Reset in case an assertion fails before the job runs.
        cfg._catalog_fetch_waiters.clear()


def test_memo_text_models_or_fetch_failed_fetch_waits_before_retry():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "http://localhost:11434"
    cfg._catalog_memos.clear()
    cfg._catalog_fetch_waiters.clear()
    jobs = []
    posted = []

    try:
        with patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch("plugin.framework.client.model_fetcher.main.post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models", return_value=None):
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t1") is None
            assert len(jobs) == 1
            jobs[0]()
            assert posted == []

            # Both text and image calls wait during the retry backoff window.
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t2") is None
            assert cfg.memo_image_models_or_fetch(endpoint, lambda _ids: None, task_name="t3") is None
            assert len(jobs) == 1

            cfg._catalog_memos[cfg._catalog_memo_key(endpoint)].fetch_failed_at -= cfg.CATALOG_RETRY_S
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t4") is None
            assert len(jobs) == 2
    finally:
        cfg._catalog_memos.clear()
        cfg._catalog_fetch_waiters.clear()


def test_failed_fetch_wait_is_per_key_and_cleared_by_test_connection():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "https://openrouter.ai/api"
    cfg._catalog_memos.clear()
    cfg._catalog_fetch_waiters.clear()
    jobs = []
    key = {"val": "sk-key-1"}

    try:
        with patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", side_effect=lambda _ep: key["val"]), \
             patch("plugin.framework.client.model_fetcher.has_api_key_for_endpoint", return_value=True), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models", return_value=None):
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t1") is None
            assert len(jobs) == 1
            jobs[0]()

            # Different key on the same host starts its own job.
            key["val"] = "sk-key-2"
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t2") is None
            assert len(jobs) == 2
            jobs[1]()

            # Back on the first key: still held off until Test Connection clears the cache.
            key["val"] = "sk-key-1"
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t3") is None
            assert len(jobs) == 2

            cfg.clear_settings_catalog_cache(endpoint)
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t4") is None
            assert len(jobs) == 3
    finally:
        cfg._catalog_memos.clear()
        cfg._catalog_fetch_waiters.clear()


def test_clear_during_failing_fetch_discards_the_failure():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "http://localhost:11434"
    cfg._catalog_memos.clear()
    cfg._catalog_fetch_waiters.clear()
    jobs = []

    def _clear_and_fail(_ep: str) -> None:
        cfg.clear_settings_catalog_cache(endpoint)
        return None

    try:
        with patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
             patch("plugin.framework.client.model_fetcher.run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models", side_effect=_clear_and_fail):
            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t1") is None
            assert len(jobs) == 1
            jobs[0]()

            assert cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t2") is None
            assert len(jobs) == 2
    finally:
        cfg._catalog_memos.clear()
        cfg._catalog_fetch_waiters.clear()


def test_memo_text_models_or_fetch_never_fetches_keyless():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "https://openrouter.ai/api"
    with patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
         patch("plugin.framework.client.model_fetcher.has_api_key_for_endpoint", return_value=False), \
         patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint") as mock_get_key, \
         patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:
        got = cfg.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="test")
    assert got is None
    mock_get_key.assert_not_called()
    mock_bg.assert_not_called()


def test_memo_text_models_or_fetch_uncached_keystore_fetches_off_thread():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "https://api.anthropic.com/v1"
    anthropic_ids = [f"claude-model-{i}" for i in range(14)]
    body = {"data": [{"id": mid, "type": "model"} for mid in anthropic_ids]}
    cached_in_keystore = {"value": False}
    jobs = []
    posted = []
    cb1: list[list[str]] = []
    cb2: list[list[str]] = []

    def fake_needs_read(_slot: str) -> bool:
        return not cached_in_keystore["value"]

    def fake_get_key(_slot: str) -> str:
        cached_in_keystore["value"] = True
        return "sk-ant-secret"

    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    try:
        with patch.object(mf, "api_key_needs_keystore_read", side_effect=fake_needs_read), \
             patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", side_effect=fake_get_key) as mock_get_key, \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch.object(mf.main, "post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.requests.sync_request", return_value=body) as mock_http:
            assert mf.memo_text_models_or_fetch(endpoint, cb1.append, task_name="t1") is None
            assert mf.memo_text_models_or_fetch(endpoint, cb2.append, task_name="t2") is None
            mock_get_key.assert_not_called()
            mock_http.assert_not_called()
            assert len(jobs) == 1

            jobs[0]()
            assert mock_get_key.call_count >= 1
            assert mock_http.call_count == 1
            assert len(posted) == 1
            posted[0]()

            assert cb1 == [anthropic_ids]
            assert cb2 == [anthropic_ids]

            # Key is now cached and memo is warm; next call returns synchronously.
            warm = mf.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t3")
            assert warm == anthropic_ids
            assert len(jobs) == 1
            assert mock_http.call_count == 1
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


def test_memo_text_models_or_fetch_picks_up_settings_memo_with_uncached_keystore():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "https://api.anthropic.com/v1"
    anthropic_ids = [f"claude-model-{i}" for i in range(14)]
    body = {"data": [{"id": mid, "type": "model"} for mid in anthropic_ids]}
    jobs = []
    posted = []
    received: list[list[str]] = []

    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    try:
        # Simulate Settings Test (settings-recheck) filling the memo with api_key_override.
        with patch("plugin.framework.client.requests.sync_request", return_value=body), \
             patch.object(mf, "get_config", return_value=""):
            assert mf.fetch_available_models(endpoint, api_key_override="sk-ant-secret") == anthropic_ids

        # Even if keystore is still reported uncached, the worker reads the key,
        # hits the Settings-filled memo without HTTP, and delivers via on_fetched.
        with patch.object(mf, "api_key_needs_keystore_read", return_value=True), \
             patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", return_value="sk-ant-secret") as mock_get_key, \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch.object(mf.main, "post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.requests.sync_request") as mock_http:
            assert mf.memo_text_models_or_fetch(endpoint, received.append, task_name="sidebar-models") is None
            mock_get_key.assert_not_called()
            assert len(jobs) == 1
            jobs[0]()
            mock_http.assert_not_called()
            assert len(posted) == 1
            posted[0]()

        assert received == [anthropic_ids]
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


def test_memo_text_models_or_fetch_locked_keystore_skips_later_populates():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "https://api.anthropic.com/v1"
    locked = {"value": False}
    jobs = []

    def fake_get_key(_slot: str) -> str:
        locked["value"] = True
        return ""

    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    try:
        with patch.object(mf, "api_key_locked_for_endpoint", side_effect=lambda _ep: locked["value"]), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=True), \
             patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", side_effect=fake_get_key) as mock_get_key, \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch("plugin.framework.client.requests.sync_request", return_value=None):
            assert mf.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t1") is None
            assert len(jobs) == 1
            jobs[0]()
            assert locked["value"] is True
            calls_after_worker = mock_get_key.call_count

            # Next populate after locked/cancelled read starts no new job or keystore read.
            assert mf.memo_text_models_or_fetch(endpoint, lambda _ids: None, task_name="t2") is None
            assert len(jobs) == 1
            assert mock_get_key.call_count == calls_after_worker
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


TOGETHER_MODELS_BODY = [
    {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "type": "chat", "context_length": 131072},
    {"id": "meta-llama/Llama-3.1-8B-Reference", "type": "language"},
    {"id": "Qwen/Qwen2.5-Coder-32B-Instruct", "type": "code"},
    {"id": "black-forest-labs/FLUX.1-schnell", "type": "image"},
    {"id": "google/flash-image-2.5", "type": "image"},
    {"id": "cartesia/sonic-2", "type": "audio"},
    {"id": "hexgrad/Kokoro-82M", "type": "audio"},
    {"id": "canopylabs/orpheus-3b-0.1-ft", "type": "audio"},
    {"id": "openai/whisper-large-v3", "type": "transcribe"},
    {"id": "Wan-AI/Wan2.2-T2V-A14B", "type": "video"},
    {"id": "BAAI/bge-large-en-v1.5", "type": "embedding"},
    {"id": "meta-llama/Llama-Guard-4-12B", "type": "moderation"},
    {"id": "Salesforce/Llama-Rank-V1", "type": "rerank"},
]

_TOGETHER_TEXT_IDS = [
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "meta-llama/Llama-3.1-8B-Reference",
    "Qwen/Qwen2.5-Coder-32B-Instruct",
]


def test_together_non_text_rows_left_out_of_text_list():
    import plugin.framework.client.model_fetcher as mf

    tg = "https://api.together.xyz/v1"
    key = "test-tg-key"
    mf.clear_settings_catalog_cache(tg, api_key_override=key)
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value=TOGETHER_MODELS_BODY), \
             patch.object(mf, "get_config", return_value=""):
            ids = mf.fetch_available_models(tg, api_key_override=key)
        assert ids == [row["id"] for row in TOGETHER_MODELS_BODY]
        assert mf.cached_image_models(tg, api_key_override=key) == [
            "black-forest-labs/FLUX.1-schnell",
            "google/flash-image-2.5",
        ]
        assert mf.drop_non_text_models(tg, ids, api_key_override=key) == _TOGETHER_TEXT_IDS
    finally:
        mf.clear_settings_catalog_cache(tg, api_key_override=key)


def test_drop_non_text_models_shared_rule_across_providers():
    import plugin.framework.client.model_fetcher as mf

    ep = "https://openrouter.ai/api/v1"
    key = "test-or-key"
    body = {
        "data": [
            {"id": "or/image-only", "architecture": {"output_modalities": ["image"]}},
            {"id": "or/image-and-text", "architecture": {"output_modalities": ["image", "text"]}},
            {"id": "or/audio-only", "output_modalities": ["audio"]},
            {"id": "anthropic/claude-sonnet-4", "type": "model"},
            {"id": "mistral/mistral-large", "type": "base"},
            {"id": "plain/untyped-model"},
        ],
    }
    raw_ids = [row["id"] for row in body["data"]]
    mf.clear_settings_catalog_cache(ep, api_key_override=key)
    try:
        # Cold memo returns the input unchanged.
        assert mf.drop_non_text_models(ep, raw_ids, api_key_override=key) == raw_ids
        with patch("plugin.framework.client.requests.sync_request", return_value=body), \
             patch.object(mf, "get_config", return_value=""):
            fetched = mf.fetch_available_models(ep, api_key_override=key)
        assert fetched == raw_ids
        assert mf.drop_non_text_models(ep, raw_ids, api_key_override=key) == [
            "or/image-and-text",
            "anthropic/claude-sonnet-4",
            "mistral/mistral-large",
            "plain/untyped-model",
        ]
    finally:
        mf.clear_settings_catalog_cache(ep, api_key_override=key)


def test_memo_text_models_or_fetch_together_delivers_text_only():
    import plugin.framework.client.model_fetcher as mf

    tg = "https://api.together.xyz/v1"
    jobs = []
    posted = []
    received: list[list[str]] = []
    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    try:
        with patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", return_value="k"), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=False), \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, name=None: jobs.append(fn)), \
             patch.object(mf.main, "post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.requests.sync_request", return_value=TOGETHER_MODELS_BODY):
            first = mf.memo_text_models_or_fetch(tg, received.append, task_name="test-tg")
            assert first is None
            assert len(jobs) == 1
            jobs[0]()
            assert len(posted) == 1
            posted[0]()

        assert received == [_TOGETHER_TEXT_IDS]

        with patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", return_value="k"), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=False):
            warm = mf.memo_text_models_or_fetch(tg, lambda unused: None, task_name="test-tg")
        assert warm == _TOGETHER_TEXT_IDS
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


def test_memo_image_models_or_fetch_warm_returns_image_ids_without_non_text_filter():
    import plugin.framework.client.model_fetcher as cfg

    endpoint = "http://localhost:11434"
    cfg._catalog_memos.clear()
    try:
        with patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint", return_value=""), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=False), \
             patch("plugin.framework.client.requests.sync_request") as mock_http, \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:
            cfg._catalog_memos[cfg._catalog_memo_key(endpoint)] = cfg.CatalogMemo(
                ids=["llama3"],
                image_ids=["flux-dev"],
                non_text_ids={"flux-dev"},
            )
            got = cfg.memo_image_models_or_fetch(endpoint, lambda _ids: None, task_name="test")
        assert got == ["flux-dev"]
        mock_http.assert_not_called()
        mock_bg.assert_not_called()
    finally:
        cfg._catalog_memos.clear()


def test_memo_text_and_image_models_share_one_cold_job_and_deliver_separate_slices():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "http://localhost:11434"
    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    jobs = []
    posted = []
    text_calls: list[list[str]] = []
    image_calls: list[list[str]] = []
    body = {"data": [{"id": "llama3"}, {"id": "flux-schnell"}]}

    try:
        with patch.object(mf, "get_api_key_for_endpoint", return_value=""), \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=False), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch.object(mf.main, "post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.requests.sync_request", return_value=body) as mock_http:
            assert mf.memo_text_models_or_fetch(endpoint, text_calls.append, task_name="sidebar-models") is None
            assert mf.memo_image_models_or_fetch(endpoint, image_calls.append, task_name="sidebar-image-models") is None
            assert len(jobs) == 1

            jobs[0]()
            assert mock_http.call_count == 1
            assert len(posted) == 1
            posted[0]()
            assert text_calls == [["llama3", "flux-schnell"]]
            assert image_calls == [["flux-schnell"]]
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


@pytest.mark.parametrize(
    ("endpoint", "needs_keystore", "has_key", "expect_bg"),
    [
        ("https://openrouter.ai/api", True, True, True),
        ("https://openrouter.ai/api", False, False, False),
    ],
)
def test_memo_image_models_or_fetch_never_reads_locked_key_or_fetches_keyless(
    endpoint: str,
    needs_keystore: bool,
    has_key: bool,
    expect_bg: bool,
):
    import plugin.framework.client.model_fetcher as cfg

    cfg._catalog_fetch_waiters.clear()
    try:
        with patch("plugin.framework.client.model_fetcher.api_key_needs_keystore_read", return_value=needs_keystore), \
             patch("plugin.framework.client.model_fetcher.has_api_key_for_endpoint", return_value=has_key), \
             patch("plugin.framework.client.model_fetcher.api_key_locked_for_endpoint", return_value=False), \
             patch("plugin.framework.client.model_fetcher.get_api_key_for_endpoint") as mock_get_key, \
             patch("plugin.framework.client.model_fetcher.run_in_background") as mock_bg:
            got = cfg.memo_image_models_or_fetch(endpoint, lambda _ids: None, task_name="test")
        assert got is None
        mock_get_key.assert_not_called()
        if expect_bg:
            mock_bg.assert_called_once()
        else:
            mock_bg.assert_not_called()
    finally:
        cfg._catalog_fetch_waiters.clear()


def test_memo_image_models_or_fetch_openrouter_uses_images_models_endpoint():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "https://openrouter.ai/api"
    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    jobs = []
    posted = []
    received: list[list[str]] = []
    body = {"data": [{"id": "google/gemini-2.5-flash-image"}]}

    try:
        with patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", return_value="sk-or-test"), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=False), \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch.object(mf.main, "post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.requests.sync_request", return_value=body) as mock_http:
            assert mf.memo_image_models_or_fetch(endpoint, received.append, task_name="sidebar-image-models") is None
            assert len(jobs) == 1
            jobs[0]()
            assert mock_http.call_count == 1
            assert mock_http.call_args.args[0].endswith("/v1/images/models")
            assert len(posted) == 1
            posted[0]()
            assert received == [["google/gemini-2.5-flash-image"]]

        with patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", return_value="sk-or-test"), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=False):
            warm = mf.memo_image_models_or_fetch(endpoint, lambda _ids: None, task_name="sidebar-image-models")
        assert warm == ["google/gemini-2.5-flash-image"]
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


def test_memo_text_and_image_openrouter_share_one_job_and_fetch_both_endpoints():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "https://openrouter.ai/api"
    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    jobs = []
    posted = []
    text_calls: list[list[str]] = []
    image_calls: list[list[str]] = []
    urls_called: list[str] = []

    def fake_sync_request(url, **_kwargs):
        urls_called.append(url)
        if url.endswith("/images/models"):
            return {"data": [{"id": "google/gemini-2.5-flash-image"}]}
        return {"data": [{"id": "anthropic/claude-sonnet-4"}]}

    try:
        with patch.object(mf, "has_api_key_for_endpoint", return_value=True), \
             patch.object(mf, "get_api_key_for_endpoint", return_value="sk-or-test"), \
             patch.object(mf, "api_key_needs_keystore_read", return_value=False), \
             patch.object(mf, "get_config", return_value=""), \
             patch.object(mf, "run_in_background", side_effect=lambda fn, **_kw: jobs.append(fn)), \
             patch.object(mf.main, "post", side_effect=lambda fn: posted.append(fn)), \
             patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync_request):
            assert mf.memo_text_models_or_fetch(endpoint, text_calls.append, task_name="sidebar-models") is None
            assert mf.memo_image_models_or_fetch(endpoint, image_calls.append, task_name="sidebar-image-models") is None
            assert len(jobs) == 1

            jobs[0]()
            assert len(urls_called) == 2
            assert urls_called[0].endswith("/models")
            assert urls_called[1].endswith("/images/models")
            assert len(posted) == 1
            posted[0]()
            assert text_calls == [["anthropic/claude-sonnet-4"]]
            assert image_calls == [["google/gemini-2.5-flash-image"]]
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


def test_has_native_vision_dead_endpoint_backs_off_and_clears_on_test_connection():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "https://openrouter.ai/api"
    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()
    fetch_calls = 0

    def _fail_fetch(_ep, **_kw):
        nonlocal fetch_calls
        fetch_calls += 1
        return None

    try:
        with (
            patch.object(mf.main, "is_current", return_value=False),
            patch.object(mf, "get_config", return_value={}),
            patch.object(mf, "get_api_key_for_endpoint", return_value=""),
            patch.object(mf, "fetch_available_models", side_effect=_fail_fetch),
        ):
            assert mf.has_native_vision("some/uncatalogued-model", endpoint) is None
            assert mf.has_native_vision("some/uncatalogued-model", endpoint) is None
            assert fetch_calls == 1

            memo = mf._catalog_memos[mf._catalog_memo_key(endpoint)]
            assert memo.fetch_failed_at is not None
            memo.fetch_failed_at -= mf.CATALOG_RETRY_S + 1.0
            assert mf.has_native_vision("some/uncatalogued-model", endpoint) is None
            assert fetch_calls == 2

            mf.clear_settings_catalog_cache(endpoint)
            assert mf.has_native_vision("some/uncatalogued-model", endpoint) is None
            assert fetch_calls == 3
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()


def test_has_native_vision_lmstudio_failure_does_not_set_fetch_failed_at():
    import plugin.framework.client.model_fetcher as mf

    endpoint = "http://127.0.0.1:1234/v1"
    mf._catalog_memos.clear()
    mf._catalog_fetch_waiters.clear()

    try:
        with (
            patch.object(mf.main, "is_current", return_value=False),
            patch.object(mf, "get_config", return_value={}),
            patch.object(mf, "_vision_list_url", return_value="http://127.0.0.1:1234/api/v1/models"),
            patch.object(mf, "_fetch_vision_list", return_value=None),
        ):
            assert mf.has_native_vision("qwen2.5-vl", endpoint) is None
            memo = mf._catalog_memos.get(mf._catalog_memo_key(endpoint))
            assert memo is None or memo.fetch_failed_at is None
    finally:
        mf._catalog_memos.clear()
        mf._catalog_fetch_waiters.clear()
