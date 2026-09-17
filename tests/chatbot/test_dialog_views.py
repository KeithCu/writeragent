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
        mock_set_config.assert_any_call("extend_selection_max_tokens", "1500")
        mock_set_config.assert_any_call("edit_selection_max_new_tokens", "250")

    @patch("plugin.chatbot.dialog_views.translate_dialog")
    @patch("plugin.chatbot.dialog_views.populate_combobox_with_lru")
    @patch("plugin.chatbot.dialog_views.get_extension_url", return_value="vnd.sun.star.expand:/WriterAgent")
    @patch("plugin.chatbot.dialog_views.init_logging")
    def test_input_box_delegates_selection_token_bounds_to_config(
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

        mock_set_config.assert_any_call("extend_selection_max_tokens", "1")
        mock_set_config.assert_any_call("edit_selection_max_new_tokens", "99999")


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

    def test_apply_dropdowns_clears_combo_current_on_provider_switch(self):
        """Provider change must not seed populate with leftover foreign slugs."""
        from plugin.chatbot.dialog_views import EndpointCombinedListener

        dialog = MagicMock()
        ctx = MagicMock()
        combo = MagicMock()
        listener = EndpointCombinedListener(dialog, ctx, combo)

        text_ctrl = MagicMock()
        text_ctrl.getText.return_value = 'inception/mercury-2.5'
        stt_ctrl = MagicMock()
        stt_ctrl.getText.return_value = 'openai/whisper-large-v3'
        image_ctrl = MagicMock()
        image_ctrl.getText.return_value = 'openai/gpt-5-image'
        api_key_ctrl = MagicMock()
        api_key_ctrl.getText.return_value = 'tg-key'

        def get_optional_side_effect(dlg, name):
            return {
                'text_model': text_ctrl,
                'stt_model': stt_ctrl,
                'image_model': image_ctrl,
                'api_key': api_key_ctrl,
            }.get(name)

        populate_calls = []

        def track_populate(ctx, ctrl, current, lru_key, endpoint, **kwargs):
            populate_calls.append({'lru_key': lru_key, 'current': current, 'endpoint': endpoint})

        with patch('plugin.chatbot.dialog_views.get_optional', side_effect=get_optional_side_effect):
            with patch('plugin.framework.config.get_current_endpoint', return_value='https://openrouter.ai/api'):
                listener.populate_combobox_with_lru = track_populate
                listener._apply_dropdowns('https://api.together.xyz', models=None, skip_fetch=True)

        by_key = {c['lru_key']: c for c in populate_calls}
        self.assertEqual(by_key['model_lru']['current'], '')
        self.assertEqual(by_key['audio_model_lru']['current'], '')
        self.assertEqual(by_key['image_model_lru']['current'], '')
        self.assertEqual(by_key['model_lru']['endpoint'], 'https://api.together.xyz')

    def test_apply_dropdowns_drops_openrouter_slug_from_together_list(self):
        """Regression: uncatalogued OpenRouter LRU ids must not reappear on Together."""
        from plugin.chatbot.dialog_views import EndpointCombinedListener

        dialog = MagicMock()
        ctx = MagicMock()
        combo = MagicMock()
        listener = EndpointCombinedListener(dialog, ctx, combo)

        sticky = 'inception/mercury-2.5'
        text_ctrl = MagicMock()
        text_ctrl.getText.return_value = sticky
        text_ctrl.getItemCount.return_value = 0
        stt_ctrl = MagicMock()
        stt_ctrl.getText.return_value = 'openai/whisper-large-v3'
        stt_ctrl.getItemCount.return_value = 0
        image_ctrl = MagicMock()
        image_ctrl.getText.return_value = 'openai/gpt-5-image'
        image_ctrl.getItemCount.return_value = 0
        api_key_ctrl = MagicMock()
        api_key_ctrl.getText.return_value = 'tg-key'

        def get_optional_side_effect(dlg, name):
            return {
                'text_model': text_ctrl,
                'stt_model': stt_ctrl,
                'image_model': image_ctrl,
                'api_key': api_key_ctrl,
            }.get(name)

        def mock_get_config(key, default=None):
            return [] if isinstance(key, str) and 'lru' in key else ('' if default is None else default)

        with patch('plugin.chatbot.dialog_views.get_optional', side_effect=get_optional_side_effect):
            with patch('plugin.framework.config.get_current_endpoint', return_value='https://openrouter.ai/api'):
                with patch('plugin.chatbot.config_ui_helpers.get_config', side_effect=mock_get_config):
                    listener._apply_dropdowns('https://api.together.xyz', models=None, skip_fetch=True)

        text_items = list(text_ctrl.addItems.call_args[0][0])
        self.assertNotIn(sticky, text_items)
        self.assertNotEqual(text_ctrl.setText.call_args[0][0], sticky)
        self.assertIn('MiniMaxAI/MiniMax-M3', text_items)

        stt_items = list(stt_ctrl.addItems.call_args[0][0])
        self.assertNotIn('openai/whisper-large-v3', stt_items)
        image_items = list(image_ctrl.addItems.call_args[0][0])
        self.assertNotIn('openai/gpt-5-image', image_items)


class TestSettingsEnhancements(unittest.TestCase):
    def test_get_signup_url_for_endpoint(self):
        from plugin.chatbot.config_ui_helpers import get_signup_url_for_endpoint

        self.assertEqual(get_signup_url_for_endpoint("https://openrouter.ai/api"), "https://openrouter.ai/keys")
        self.assertEqual(get_signup_url_for_endpoint("https://api.together.xyz"), "https://api.together.ai/settings/api-keys")
        self.assertEqual(get_signup_url_for_endpoint("https://api.groq.com/openai"), "https://console.groq.com/keys")
        self.assertEqual(get_signup_url_for_endpoint("https://integrate.api.nvidia.com/v1"), "https://build.nvidia.com/settings/api-keys")
        self.assertIsNone(get_signup_url_for_endpoint("http://localhost:11434"))
        self.assertIsNone(get_signup_url_for_endpoint("http://127.0.0.1:1234"))

    @patch("plugin.chatbot.dialog_views.open_system_url")
    def test_get_api_key_listener_action(self, mock_open_url):
        from plugin.chatbot.dialog_views import GetApiKeyListener

        ctx = MagicMock()
        dlg = MagicMock()
        endpoint_ctrl = MagicMock()
        endpoint_ctrl.getText.return_value = "https://openrouter.ai/api"
        dlg.getControl.side_effect = lambda name: endpoint_ctrl if name == "endpoint" else None

        listener = GetApiKeyListener(ctx, dlg)
        listener.on_action_performed(MagicMock())

        mock_open_url.assert_called_once_with(ctx, "https://openrouter.ai/keys")

    @patch("plugin.chatbot.quick_setup.check_endpoint_connection", return_value=(True, "✓ Connected (25ms)"))
    def test_test_connection_listener_action(self, mock_check):
        from plugin.chatbot.dialog_views import TestConnectionListener

        ctx = MagicMock()
        dlg = MagicMock()
        btn_ctrl = MagicMock()
        status_ctrl = MagicMock()
        endpoint_ctrl = MagicMock()
        api_key_ctrl = MagicMock()

        endpoint_ctrl.getText.return_value = "https://openrouter.ai/api"
        api_key_ctrl.getText.return_value = "sk-test-key"

        def get_control_side_effect(name):
            return {
                "btn_test_conn": btn_ctrl,
                "lbl_test_status": status_ctrl,
                "endpoint": endpoint_ctrl,
                "api_key": api_key_ctrl,
            }.get(name)

        dlg.getControl.side_effect = get_control_side_effect

        with patch("plugin.framework.worker_pool.run_in_background", side_effect=lambda fn, **kwargs: fn()), \
             patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn: fn()), \
             patch("plugin.chatbot.dialog_views.get_control_text", side_effect=lambda c: c.getText.return_value):
            listener = TestConnectionListener(ctx, dlg)
            listener.on_action_performed(MagicMock())

        mock_check.assert_called_once_with("https://openrouter.ai/api", "sk-test-key")
        status_ctrl.setText.assert_called_with("✓ Connected (25ms)")

    @patch("plugin.chatbot.dialog_views.open_system_url")
    @patch("plugin.framework.config.get_api_key_for_endpoint", return_value="sk-saved-together-key")
    def test_provider_starter_listener_action(self, mock_get_key, mock_open_url):
        from plugin.chatbot.dialog_views import ProviderStarterListener

        ctx = MagicMock()
        dlg = MagicMock()
        endpoint_ctrl = MagicMock()
        api_key_ctrl = MagicMock()

        def get_control_side_effect(name):
            return {
                "endpoint": endpoint_ctrl,
                "api_key": api_key_ctrl,
            }.get(name)

        dlg.getControl.side_effect = get_control_side_effect

        listener = ProviderStarterListener(
            ctx, dlg, "https://api.together.xyz", "https://api.together.ai/settings/api-keys"
        )
        listener.on_action_performed(MagicMock())

        endpoint_ctrl.setText.assert_called_with("https://api.together.xyz")
        mock_get_key.assert_called_once_with("https://api.together.xyz")
        api_key_ctrl.setText.assert_called_with("sk-saved-together-key")
        api_key_ctrl.setFocus.assert_called_once()
        mock_open_url.assert_called_once_with(ctx, "https://api.together.ai/settings/api-keys")

    @patch("plugin.mcp.mcp_ui.get_config_int", return_value=18765)
    def test_build_mcp_config_snippet_default(self, mock_port):
        import json
        from plugin.chatbot.dialog_views import build_mcp_config_snippet

        snippet = build_mcp_config_snippet()
        parsed = json.loads(snippet)
        self.assertEqual(parsed["mcpServers"]["libreoffice"]["url"], "http://localhost:18765/mcp")

    def test_build_mcp_config_snippet_custom_port(self):
        import json
        from plugin.chatbot.dialog_views import build_mcp_config_snippet

        snippet = build_mcp_config_snippet(9000)
        parsed = json.loads(snippet)
        self.assertEqual(parsed["mcpServers"]["libreoffice"]["url"], "http://localhost:9000/mcp")

    @patch("plugin.mcp.mcp_ui.copy_to_clipboard", return_value=True)
    def test_copy_mcp_config_listener_action(self, mock_copy):
        from plugin.chatbot.dialog_views import CopyMcpConfigListener

        ctx = MagicMock()
        dlg = MagicMock()
        snippet_ctrl = MagicMock()
        snippet_ctrl.getText.return_value = '{"mcpServers": {}}'
        btn_ctrl = MagicMock()
        btn_model = MagicMock()
        btn_ctrl.getModel.return_value = btn_model

        def optional_side_effect(d, name):
            if name == "mcp__client_config_snippet":
                return snippet_ctrl
            if name == "mcp__copy_config":
                return btn_ctrl
            return None

        with patch("plugin.mcp.mcp_ui.get_optional", side_effect=optional_side_effect), \
             patch("plugin.mcp.mcp_ui.get_control_text", return_value='{"mcpServers": {}}'):
            listener = CopyMcpConfigListener(ctx, dlg)
            listener.on_action_performed(MagicMock())

        mock_copy.assert_called_once_with(ctx, '{"mcpServers": {}}')
        self.assertEqual(btn_model.Label, "✓ Copied!")

    def test_mcp_port_text_listener_updates_snippet(self):
        from plugin.chatbot.dialog_views import McpPortTextListener

        dlg = MagicMock()
        snippet_ctrl = MagicMock()
        port_ctrl = MagicMock()
        port_ctrl.getValue.return_value = 19999

        def optional_side_effect(d, name):
            if name == "mcp__client_config_snippet":
                return snippet_ctrl
            if name == "mcp__mcp_port":
                return port_ctrl
            return None

        with patch("plugin.mcp.mcp_ui.get_optional", side_effect=optional_side_effect), \
             patch("plugin.mcp.mcp_ui.set_control_text") as mock_set_text:
            listener = McpPortTextListener(dlg)
            listener.textChanged(MagicMock())

        mock_set_text.assert_called_once()
        args = mock_set_text.call_args[0]
        self.assertEqual(args[0], snippet_ctrl)
        self.assertIn("19999", args[1])


class TestProviderButtonIcons(unittest.TestCase):
    def test_provider_icon_filename_is_always_48(self):
        from plugin.chatbot.dialog_views import provider_icon_filename

        self.assertEqual(provider_icon_filename("openrouter"), "openrouter_48.png")
        self.assertEqual(provider_icon_filename("huggingface"), "huggingface_48.png")

    def test_apply_sets_image_url_from_extension_assets(self):
        from plugin.chatbot.dialog_views import apply_provider_button_icon

        ctx = MagicMock()
        ctrl = MagicMock()
        model = MagicMock()
        ctrl.getModel.return_value = model

        with patch("plugin.chatbot.dialog_views.get_extension_url", return_value="file:///tmp/oxt"):
            apply_provider_button_icon(ctrl, ctx, "openrouter")

        self.assertEqual(model.ImageURL, "file:///tmp/oxt/assets/openrouter_48.png")
        self.assertEqual(model.ImagePosition, 1)

    def test_apply_uses_48px_asset(self):
        from plugin.chatbot.dialog_views import apply_provider_button_icon

        ctx = MagicMock()
        ctrl = MagicMock()
        model = MagicMock()
        ctrl.getModel.return_value = model

        with patch("plugin.chatbot.dialog_views.get_extension_url", return_value="file:///tmp/oxt"):
            apply_provider_button_icon(ctrl, ctx, "nvidia")

        self.assertEqual(model.ImageURL, "file:///tmp/oxt/assets/nvidia_48.png")


class TestRecheckGrammarListener(unittest.TestCase):
    def test_click_calls_recheck_helper(self) -> None:
        from plugin.chatbot.dialog_views import RecheckGrammarListener

        ctx = MagicMock()
        with patch(
            "plugin.writer.locale.grammar_proofread_cache.recheck_active_document_grammar"
        ) as recheck:
            RecheckGrammarListener(ctx).on_action_performed(MagicMock())
        recheck.assert_called_once_with(ctx)


if __name__ == '__main__':
    unittest.main()



