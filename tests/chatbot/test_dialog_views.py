import unittest
from unittest.mock import MagicMock, patch


class TestSettingsInitialModelsFetch(unittest.TestCase):
    def test_schedule_initial_models_fetch_openrouter_with_key(self):
        from plugin.chatbot.dialog_views import SettingsDialog

        dlg = SettingsDialog(MagicMock())
        listener = MagicMock()
        dlg._endpoint_listener = listener
        with patch('plugin.framework.config.get_api_key_for_endpoint', return_value='sk-test'):
            dlg._schedule_initial_models_fetch('https://openrouter.ai/api')
        listener._schedule_debounced_models_fetch.assert_called_once()

    def test_schedule_initial_models_fetch_skips_without_key(self):
        from plugin.chatbot.dialog_views import SettingsDialog

        dlg = SettingsDialog(MagicMock())
        listener = MagicMock()
        dlg._endpoint_listener = listener
        with patch('plugin.framework.config.get_api_key_for_endpoint', return_value=''):
            dlg._schedule_initial_models_fetch('https://openrouter.ai/api')
        listener._schedule_debounced_models_fetch.assert_not_called()

    def test_schedule_initial_models_fetch_skips_ollama(self):
        from plugin.chatbot.dialog_views import SettingsDialog

        dlg = SettingsDialog(MagicMock())
        listener = MagicMock()
        dlg._endpoint_listener = listener
        dlg._schedule_initial_models_fetch('http://localhost:11434')
        listener._schedule_debounced_models_fetch.assert_not_called()


class TestEndpointCombinedListener(unittest.TestCase):
    def test_item_state_changed_applies_dropdowns_before_background_fetch(self):
        from plugin.chatbot.dialog_views import EndpointCombinedListener

        dialog = MagicMock()
        ctx = MagicMock()
        combo = MagicMock()
        combo.getText.return_value = 'https://openrouter.ai/api'
        combo.getItem.return_value = 'OpenRouter'

        listener = EndpointCombinedListener(dialog, ctx, combo)
        apply_calls = []
        bg_calls = []

        def track_apply(*args, **kwargs):
            apply_calls.append((args, kwargs))

        def track_bg(gen, resolved):
            bg_calls.append((gen, resolved))

        listener._apply_dropdowns = track_apply
        listener._bg_fetch = track_bg
        listener.run_in_background = lambda fn, name=None: fn()

        event = MagicMock()
        event.Selected = 0
        listener.itemStateChanged(event)

        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(apply_calls[0][0][0], 'https://openrouter.ai/api')
        self.assertTrue(apply_calls[0][1].get('skip_fetch'))
        self.assertEqual(len(bg_calls), 1)

    def test_ollama_select_does_not_skip_sync_fetch(self):
        from plugin.chatbot.dialog_views import EndpointCombinedListener

        dialog = MagicMock()
        ctx = MagicMock()
        combo = MagicMock()
        combo.getText.return_value = 'http://localhost:11434'
        combo.getItem.return_value = 'Local (Ollama)'

        listener = EndpointCombinedListener(dialog, ctx, combo)
        apply_calls = []

        def track_apply(*args, **kwargs):
            apply_calls.append((args, kwargs))

        listener._apply_dropdowns = track_apply
        listener._bg_fetch = MagicMock()
        listener.run_in_background = lambda fn, name=None: None

        event = MagicMock()
        event.Selected = 0
        listener.itemStateChanged(event)

        self.assertEqual(len(apply_calls), 1)
        self.assertFalse(apply_calls[0][1].get('skip_fetch'))

    def test_apply_dropdowns_openrouter_stt_skips_text_remote_models(self):
        from plugin.chatbot.dialog_views import EndpointCombinedListener

        dialog = MagicMock()
        ctx = MagicMock()
        combo = MagicMock()
        listener = EndpointCombinedListener(dialog, ctx, combo)

        text_ctrl = MagicMock()
        text_ctrl.getText.return_value = ''
        stt_ctrl = MagicMock()
        stt_ctrl.getText.return_value = ''
        image_ctrl = MagicMock()
        image_ctrl.getText.return_value = ''

        def get_optional_side_effect(dlg, name):
            return {'text_model': text_ctrl, 'stt_model': stt_ctrl, 'image_model': image_ctrl, 'api_key': None}.get(name)

        populate_calls = []

        def track_populate(ctx, ctrl, current, lru_key, endpoint, **kwargs):
            populate_calls.append({'lru_key': lru_key, 'remote_models': kwargs.get('remote_models')})

        with patch('plugin.chatbot.dialog_views.get_optional', side_effect=get_optional_side_effect):
            with patch('plugin.framework.config.get_config_str', return_value='endpoint'):
                with patch('plugin.framework.config.get_config', return_value=''):
                    with patch('plugin.framework.config.get_current_endpoint', return_value='http://localhost:11434'):
                        listener.populate_combobox_with_lru = track_populate
                        listener._apply_dropdowns(
                            'https://openrouter.ai/api',
                            models=['openrouter/fusion', 'openai/gpt-oss-120b'],
                            skip_fetch=False,
                        )

        stt_calls = [c for c in populate_calls if c['lru_key'] == 'audio_model_lru']
        text_calls = [c for c in populate_calls if c['lru_key'] == 'model_lru']
        self.assertEqual(len(stt_calls), 1)
        self.assertIsNone(stt_calls[0]['remote_models'])
        self.assertEqual(len(text_calls), 1)
        self.assertIsNotNone(text_calls[0]['remote_models'])


if __name__ == '__main__':
    unittest.main()
