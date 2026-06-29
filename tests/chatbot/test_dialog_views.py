import unittest
from unittest.mock import MagicMock, patch


class TestInputBoxExtraTokens(unittest.TestCase):
    def _mock_dialog(self, *, execute_ok=True):
        dlg = MagicMock()
        edit_ctrl = MagicMock()
        prompt_ctrl = MagicMock()
        prompt_ctrl.getText.return_value = ""
        extend_tokens_ctrl = MagicMock()
        extra_tokens_ctrl = MagicMock()

        def get_control(name):
            controls = {
                "label": MagicMock(),
                "edit": edit_ctrl,
                "prompt_selector": prompt_ctrl,
                "extend_max_tokens": extend_tokens_ctrl,
                "edit_extra_tokens": extra_tokens_ctrl,
            }
            return controls[name]

        dlg.getControl.side_effect = get_control
        dlg.execute.return_value = execute_ok

        def optional_side_effect(d, name):
            if name == "model_selector":
                return None
            if name == "extend_max_tokens":
                return extend_tokens_ctrl
            if name == "edit_extra_tokens":
                return extra_tokens_ctrl
            return None

        return dlg, edit_ctrl, extend_tokens_ctrl, extra_tokens_ctrl, optional_side_effect

    @patch("plugin.chatbot.dialog_views.translate_dialog")
    @patch("plugin.chatbot.dialog_views.populate_combobox_with_lru")
    @patch("plugin.chatbot.dialog_views.get_extension_url", return_value="vnd.sun.star.expand:/WriterAgent")
    @patch("plugin.chatbot.dialog_views.init_logging")
    def test_input_box_loads_selection_token_fields_from_config(
        self, _init_log, _ext_url, _populate, _translate,
    ):
        from plugin.chatbot.dialog_views import input_box

        ctx = MagicMock()
        smgr = MagicMock()
        ctx.getServiceManager.return_value = smgr
        dlg, _edit_ctrl, extend_tokens_ctrl, extra_tokens_ctrl, optional_side_effect = self._mock_dialog(execute_ok=False)

        dp = MagicMock()
        dp.createDialog.return_value = dlg
        smgr.createInstanceWithContext.return_value = dp

        def config_int_side_effect(key):
            return {"extend_selection_max_tokens": 1200, "edit_selection_max_new_tokens": 750}[key]

        with patch("plugin.chatbot.dialog_views.get_config_int", side_effect=config_int_side_effect), \
             patch("plugin.chatbot.dialog_views.set_control_text") as mock_set_text, \
             patch("plugin.chatbot.dialog_views.get_optional", side_effect=optional_side_effect):
            result = input_box(ctx, "msg", "title", "")

        self.assertEqual(result, ("", ""))
        mock_set_text.assert_any_call(extend_tokens_ctrl, "1200")
        mock_set_text.assert_any_call(extra_tokens_ctrl, "750")

    @patch("plugin.chatbot.dialog_views.translate_dialog")
    @patch("plugin.chatbot.dialog_views.populate_combobox_with_lru")
    @patch("plugin.chatbot.dialog_views.get_extension_url", return_value="vnd.sun.star.expand:/WriterAgent")
    @patch("plugin.chatbot.dialog_views.init_logging")
    def test_input_box_saves_selection_token_fields_on_ok(
        self, _init_log, _ext_url, _populate, _translate,
    ):
        from plugin.chatbot.dialog_views import input_box

        ctx = MagicMock()
        smgr = MagicMock()
        ctx.getServiceManager.return_value = smgr
        dlg, edit_ctrl, extend_tokens_ctrl, extra_tokens_ctrl, optional_side_effect = self._mock_dialog(execute_ok=True)
        edit_ctrl.getText.return_value = "rewrite this"

        dp = MagicMock()
        dp.createDialog.return_value = dlg
        smgr.createInstanceWithContext.return_value = dp

        def control_text_side_effect(c):
            if c is extend_tokens_ctrl:
                return "1500"
            if c is extra_tokens_ctrl:
                return "250"
            return "rewrite this"

        with patch("plugin.chatbot.dialog_views.get_control_text", side_effect=control_text_side_effect), \
             patch("plugin.chatbot.dialog_views.set_config") as mock_set_config, \
             patch("plugin.chatbot.dialog_views.get_optional", side_effect=optional_side_effect):
            text, prompt = input_box(ctx, "msg", "title", "")

        self.assertEqual(text, "rewrite this")
        mock_set_config.assert_any_call("extend_selection_max_tokens", 1500)
        mock_set_config.assert_any_call("edit_selection_max_new_tokens", 250)

    @patch("plugin.chatbot.dialog_views.translate_dialog")
    @patch("plugin.chatbot.dialog_views.populate_combobox_with_lru")
    @patch("plugin.chatbot.dialog_views.get_extension_url", return_value="vnd.sun.star.expand:/WriterAgent")
    @patch("plugin.chatbot.dialog_views.init_logging")
    def test_input_box_clamps_selection_token_fields(
        self, _init_log, _ext_url, _populate, _translate,
    ):
        from plugin.chatbot.dialog_views import input_box

        ctx = MagicMock()
        smgr = MagicMock()
        ctx.getServiceManager.return_value = smgr
        dlg, edit_ctrl, extend_tokens_ctrl, extra_tokens_ctrl, optional_side_effect = self._mock_dialog(execute_ok=True)
        edit_ctrl.getText.return_value = "go"

        dp = MagicMock()
        dp.createDialog.return_value = dlg
        smgr.createInstanceWithContext.return_value = dp

        def control_text_side_effect(c):
            if c is extend_tokens_ctrl:
                return "1"
            if c is extra_tokens_ctrl:
                return "99999"
            return "go"

        with patch("plugin.chatbot.dialog_views.get_control_text", side_effect=control_text_side_effect), \
             patch("plugin.chatbot.dialog_views.set_config") as mock_set_config, \
             patch("plugin.chatbot.dialog_views.get_optional", side_effect=optional_side_effect):
            input_box(ctx, "msg", "title", "")

        mock_set_config.assert_any_call("extend_selection_max_tokens", 10)
        mock_set_config.assert_any_call("edit_selection_max_new_tokens", 4096)


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

    def test_apply_dropdowns_uses_combobox_text_for_text_model(self):
        """Endpoint refresh must seed text model from combobox text, not empty string."""
        from plugin.chatbot.dialog_views import EndpointCombinedListener

        dialog = MagicMock()
        ctx = MagicMock()
        combo = MagicMock()
        listener = EndpointCombinedListener(dialog, ctx, combo)

        text_ctrl = MagicMock()
        text_ctrl.getText.return_value = 'user-typed-model'
        stt_ctrl = MagicMock()
        stt_ctrl.getText.return_value = ''
        image_ctrl = MagicMock()
        image_ctrl.getText.return_value = ''

        def get_optional_side_effect(dlg, name):
            return {'text_model': text_ctrl, 'stt_model': stt_ctrl, 'image_model': image_ctrl, 'api_key': None}.get(name)

        populate_calls = []

        def track_populate(ctx, ctrl, current, lru_key, endpoint, **kwargs):
            populate_calls.append({'lru_key': lru_key, 'current': current})

        with patch('plugin.chatbot.dialog_views.get_optional', side_effect=get_optional_side_effect):
            with patch('plugin.framework.config.get_current_endpoint', return_value='http://localhost:11434'):
                with patch('plugin.framework.client.model_fetcher.get_provider_from_endpoint', return_value='ollama'):
                    listener.populate_combobox_with_lru = track_populate
                    listener._apply_dropdowns('http://localhost:11434', models=['llama3'], skip_fetch=True)

        text_calls = [c for c in populate_calls if c['lru_key'] == 'model_lru']
        self.assertEqual(len(text_calls), 1)
        self.assertEqual(text_calls[0]['current'], 'user-typed-model')


if __name__ == '__main__':
    unittest.main()
