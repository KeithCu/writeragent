import unittest
from unittest.mock import MagicMock, patch
from plugin.chatbot.config_ui_helpers import update_lru_history, populate_combobox_with_lru

class TestConfigUiHelpers(unittest.TestCase):

    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}

        def mock_get_config(ctx, key):
            return self.config_data.get(key, '')

        def mock_set_config(ctx, key, value):
            self.config_data[key] = value

        self.get_patcher = patch('plugin.chatbot.config_ui_helpers.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.chatbot.config_ui_helpers.set_config', side_effect=mock_set_config)
        self.mock_get = self.get_patcher.start()
        self.mock_set = self.set_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()

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

class TestPopulateComboboxWithLruFetchOptions(unittest.TestCase):
    'populate_combobox_with_lru(skip_remote_fetch / remote_models) must not call fetch_available_models.'

    def setUp(self):
        self.ctx = MagicMock()
        self.config_data = {}

        def mock_get_config(ctx, key):
            return self.config_data.get(key, '')

        def mock_set_config(ctx, key, value):
            self.config_data[key] = value

        self.get_patcher = patch('plugin.chatbot.config_ui_helpers.get_config', side_effect=mock_get_config)
        self.set_patcher = patch('plugin.chatbot.config_ui_helpers.set_config', side_effect=mock_set_config)
        self.get_patcher.start()
        self.set_patcher.start()

    def tearDown(self):
        self.get_patcher.stop()
        self.set_patcher.stop()

    def test_skip_remote_fetch_does_not_call_fetch(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'http://localhost:8080', skip_remote_fetch=True)
            mock_fetch.assert_not_called()

    def test_remote_models_does_not_call_fetch(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'http://localhost:8080', remote_models=['m1', 'm2'])
            mock_fetch.assert_not_called()
            ctrl.addItems.assert_called()
            items = ctrl.addItems.call_args[0][0]
            self.assertIn('m1', items)
            self.assertIn('m2', items)

    def test_together_empty_lru_merges_default_text_model(self):
        'Massive providers skip /v1/models in populate_combobox_with_lru; defaults must still appear when keyed.'
        self.config_data['model_lru@https://api.together.xyz'] = []
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(
                self.ctx, ctrl, '', 'model_lru', 'https://api.together.xyz',
                skip_remote_fetch=True, api_key_override='test-key',
            )
            mock_fetch.assert_not_called()
        ctrl.addItems.assert_called()
        items = ctrl.addItems.call_args[0][0]
        self.assertIn('openai/gpt-oss-120b', items)

    def test_empty_current_val_uses_lru_head_after_sidebar_style_pick(self):
        'Simulates Settings _apply_dropdowns passing "" — active pick must stay LRU head so setText is not a stale model.'
        ep = 'http://localhost:8080'
        self.config_data[f'model_lru@{ep}'] = ['picked-model', 'other-model']
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', ep, skip_remote_fetch=True)
        ctrl.setText.assert_called_with('picked-model')

    def test_openrouter_remote_models_merge_nitro_default(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        ep = 'https://openrouter.ai/api'
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                '',
                'model_lru',
                ep,
                remote_models=['some/other-model', 'openai/gpt-oss-120b'],
            )
            mock_fetch.assert_not_called()
        items = ctrl.addItems.call_args[0][0]
        self.assertIn('openai/gpt-oss-120b:nitro', items)
        ctrl.setText.assert_called_with('openai/gpt-oss-120b:nitro')

    def test_openrouter_nitro_current_val_not_stray(self):
        ctx = MagicMock()
        ctrl = MagicMock()
        endpoint = 'https://openrouter.ai/api'
        current_val = 'openai/gpt-oss-120b:nitro'
        with patch('plugin.chatbot.config_ui_helpers.get_config', return_value=[]):
            with patch('plugin.framework.client.model_fetcher.fetch_available_models', return_value=None):
                populate_combobox_with_lru(
                    ctx, ctrl, current_val, 'model_lru', endpoint, api_key_override='test-key',
                )
        call_args = ctrl.addItems.call_args[0][0]
        self.assertIn(current_val, call_args)
        ctrl.setText.assert_called_with(current_val)

    def test_openrouter_no_api_key_shows_placeholder_only(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        ep = 'https://openrouter.ai/api'
        populate_combobox_with_lru(self.ctx, ctrl, 'llama3.2', 'model_lru', ep, api_key_override='')
        items = list(ctrl.addItems.call_args[0][0])
        self.assertEqual(items, ['(Enter API Key to load models)'])
        ctrl.setText.assert_called_with('(Enter API Key to load models)')

    def test_ollama_model_filtered_on_openrouter(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        populate_combobox_with_lru(
            self.ctx, ctrl, 'llama3.2', 'model_lru', 'https://openrouter.ai/api', api_key_override='',
        )
        items = ctrl.addItems.call_args[0][0]
        self.assertNotIn('llama3.2', items)
        self.assertIn('(Enter API Key to load models)', items)

    def test_local_provider_fetch_fail_shows_connection_failed(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.client.model_fetcher.fetch_available_models', return_value=None):
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'http://localhost:1234', api_key_override='')
        items = ctrl.addItems.call_args[0][0]
        self.assertIn('(Connection failed)', items)

    def test_placeholder_current_val_ignored_when_models_available(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.chatbot.config_ui_helpers.fetch_available_models', return_value=['llama3']):
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                '(Enter API Key to load models)',
                'model_lru',
                'http://localhost:11434',
                api_key_override='',
            )
        items = list(ctrl.addItems.call_args[0][0])
        self.assertIn('llama3', items)
        self.assertNotIn('(Enter API Key to load models)', items)
        ctrl.setText.assert_called_with('llama3')

    def test_ollama_image_fetch_ok_no_image_models_shows_specific_placeholder(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        chat_only = ['llama3.2', 'mistral']
        with patch('plugin.chatbot.config_ui_helpers.fetch_available_models', return_value=chat_only):
            populate_combobox_with_lru(
                self.ctx, ctrl, '', 'image_model_lru', 'http://localhost:11434', api_key_override='',
            )
        items = list(ctrl.addItems.call_args[0][0])
        self.assertIn('(No image models on this endpoint)', items)
        self.assertNotIn('(Connection failed)', items)

    def test_connection_failed_only_when_fetch_none(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.chatbot.config_ui_helpers.fetch_available_models', return_value=None):
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'http://localhost:1234', api_key_override='')
        items = list(ctrl.addItems.call_args[0][0])
        self.assertEqual(items, ['(Connection failed)'])

    def test_populate_combobox_stray_model_filtering(self):
        ctx = MagicMock()
        ctrl = MagicMock()
        
        # Scenario: Current val is a Google model, but we just switched to Z.ai.
        # Fetch fails (None).
        current_val = "google/gemini-3.1-flash-lite-preview"
        endpoint = "https://api.z.ai/v4"
        
        # Mock get_config to return empty LRU
        with patch("plugin.chatbot.config_ui_helpers.get_config", return_value=[]):
            # Mock fetch_available_models to return None (fail)
            with patch("plugin.framework.client.model_fetcher.fetch_available_models", return_value=None):
                populate_combobox_with_lru(ctx, ctrl, current_val, "model_lru", endpoint)
                
        # Verify that ctrl.addItems was called with the placeholder, NOT the stray Gemini model
        call_args = ctrl.addItems.call_args[0][0]
        assert "(Enter API Key to load models)" in call_args
        assert "google/gemini-3.1-flash-lite-preview" not in call_args

    def test_openrouter_fetch_picks_nitro_not_fusion(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        ep = 'https://openrouter.ai/api'
        remote = ['openrouter/fusion', 'some/other-model', 'openai/gpt-oss-120b']
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                'llama3.2',
                'model_lru',
                ep,
                remote_models=remote,
                api_key_override='test-key',
            )
            mock_fetch.assert_not_called()
        ctrl.setText.assert_called_with('openai/gpt-oss-120b:nitro')

    def test_openrouter_image_remote_models_not_refiltered_by_slug(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        ep = 'https://openrouter.ai/api'
        image_ids = ['google/gemini-2.5-flash-image', 'openai/gpt-5-image']
        populate_combobox_with_lru(
            self.ctx,
            ctrl,
            '',
            'image_model_lru',
            ep,
            remote_models=image_ids,
            api_key_override='test-key',
        )
        items = list(ctrl.addItems.call_args[0][0])
        self.assertIn('google/gemini-2.5-flash-image', items)
        self.assertIn('openai/gpt-5-image', items)

    def test_openrouter_stt_ignores_remote_catalog(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        ep = 'https://openrouter.ai/api'
        remote = ['inception/mercury-2', 'openai/gpt-oss-120b']
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(
                self.ctx,
                ctrl,
                '',
                'audio_model_lru',
                ep,
                remote_models=remote,
                api_key_override='test-key',
            )
            mock_fetch.assert_not_called()
        items = list(ctrl.addItems.call_args[0][0])
        self.assertNotIn('inception/mercury-2', items)
        self.assertIn('mistralai/voxtral-mini-transcribe', items)

    def test_openrouter_stt_defaults_voxtral(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        ep = 'https://openrouter.ai/api'
        populate_combobox_with_lru(
            self.ctx,
            ctrl,
            '',
            'audio_model_lru',
            ep,
            skip_remote_fetch=True,
            api_key_override='test-key',
        )
        ctrl.setText.assert_called_with('mistralai/voxtral-mini-transcribe')

    def test_populate_combobox_placeholder_no_provider(self):
        ctx = MagicMock()
        ctrl = MagicMock()
        
        # Scenario: Unknown endpoint, fetch fails.
        endpoint = "https://unknown.provider/api"
        
        with patch("plugin.chatbot.config_ui_helpers.get_config", return_value=[]):
            with patch("plugin.framework.client.model_fetcher.fetch_available_models", return_value=None):
                populate_combobox_with_lru(ctx, ctrl, "", "model_lru", endpoint)
                
        call_args = ctrl.addItems.call_args[0][0]
        assert "(Connection failed)" in call_args

    def test_prompt_lru_empty_no_connection_failed(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch("plugin.chatbot.config_ui_helpers.get_config", return_value=[]):
            populate_combobox_with_lru(self.ctx, ctrl, "", "prompt_lru", "")
        ctrl.addItems.assert_not_called()
        ctrl.setText.assert_called_with("")

    def test_image_base_size_lru_empty_no_connection_failed(self):
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch("plugin.chatbot.config_ui_helpers.get_config", return_value=[]):
            populate_combobox_with_lru(self.ctx, ctrl, "", "image_base_size_lru", "")
        ctrl.addItems.assert_not_called()
        ctrl.setText.assert_called_with("")
