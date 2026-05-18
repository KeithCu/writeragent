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
        'Massive providers skip /v1/models in populate_combobox_with_lru; defaults must still appear.'
        self.config_data['model_lru@https://api.together.xyz'] = []
        ctrl = MagicMock()
        ctrl.getItemCount.return_value = 0
        with patch('plugin.framework.client.model_fetcher.fetch_available_models') as mock_fetch:
            populate_combobox_with_lru(self.ctx, ctrl, '', 'model_lru', 'https://api.together.xyz', skip_remote_fetch=True)
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
