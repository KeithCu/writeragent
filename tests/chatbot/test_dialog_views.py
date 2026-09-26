from unittest.mock import MagicMock, patch


class TestInputBoxExtraTokens:
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

        assert (result) == (("", ""))
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

        assert (text) == ("rewrite this")
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


class TestSettingsInitialModelsFetch:
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


class TestEndpointCombinedListener:
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

        assert (len(apply_calls)) == (1)
        assert (apply_calls[0][0][0]) == ('https://openrouter.ai/api')
        assert (apply_calls[0][1].get('skip_fetch'))
        assert (len(bg_calls)) == (1)

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

        assert (len(apply_calls)) == (1)
        assert not (apply_calls[0][1].get('skip_fetch'))

    def test_apply_dropdowns_openrouter_stt_uses_transcription_models(self):
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
            return {'text_model': text_ctrl, 'audio__stt_model': stt_ctrl, 'image_model': image_ctrl, 'api_key': None}.get(name)

        populate_calls = []

        def track_populate(ctx, ctrl, current, lru_key, endpoint, **kwargs):
            populate_calls.append({'lru_key': lru_key, 'remote_models': kwargs.get('remote_models')})

        with patch('plugin.chatbot.dialog_views.get_optional', side_effect=get_optional_side_effect):
            with patch('plugin.framework.config.get_config_str', return_value='endpoint'):
                with patch('plugin.framework.config.get_config', return_value=''):
                    with patch('plugin.framework.config.get_current_endpoint', return_value='http://localhost:11434'):
                        speech_ids = ['mistralai/voxtral-mini-transcribe', 'openai/whisper-large-v3']
                        listener.fetch_available_stt_models = lambda endpoint, api_key_override=None: list(speech_ids)
                        listener.fetch_available_image_models = lambda endpoint, api_key_override=None: []
                        listener.populate_combobox_with_lru = track_populate
                        listener._apply_dropdowns(
                            'https://openrouter.ai/api',
                            models=['openrouter/fusion', 'openai/gpt-oss-120b'],
                            skip_fetch=False,
                        )

        stt_calls = [c for c in populate_calls if c['lru_key'] == 'audio_model_lru']
        text_calls = [c for c in populate_calls if c['lru_key'] == 'model_lru']
        assert (len(stt_calls)) == (1)
        assert (stt_calls[0]['remote_models']) == (speech_ids)
        assert ('openrouter/fusion') not in (stt_calls[0]['remote_models'])
        assert (len(text_calls)) == (1)
        assert (text_calls[0]['remote_models']) is not None

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
            return {'text_model': text_ctrl, 'audio__stt_model': stt_ctrl, 'image_model': image_ctrl, 'api_key': None}.get(name)

        populate_calls = []

        def track_populate(ctx, ctrl, current, lru_key, endpoint, **kwargs):
            populate_calls.append({'lru_key': lru_key, 'current': current})

        with patch('plugin.chatbot.dialog_views.get_optional', side_effect=get_optional_side_effect):
            with patch('plugin.framework.config.get_current_endpoint', return_value='http://localhost:11434'):
                with patch('plugin.framework.client.model_fetcher.get_provider_from_endpoint', return_value='ollama'):
                    listener.populate_combobox_with_lru = track_populate
                    listener._apply_dropdowns('http://localhost:11434', models=['llama3'], skip_fetch=True)

        text_calls = [c for c in populate_calls if c['lru_key'] == 'model_lru']
        assert (len(text_calls)) == (1)
        assert (text_calls[0]['current']) == ('user-typed-model')

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
        stt_ctrl.getText.return_value = 'mistralai/voxtral-mini-transcribe'
        image_ctrl = MagicMock()
        image_ctrl.getText.return_value = 'openai/gpt-5-image'
        api_key_ctrl = MagicMock()
        api_key_ctrl.getText.return_value = 'tg-key'

        def get_optional_side_effect(dlg, name):
            return {
                'text_model': text_ctrl,
                'audio__stt_model': stt_ctrl,
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
        assert (by_key['model_lru']['current']) == ('')
        assert (by_key['audio_model_lru']['current']) == ('')
        assert (by_key['image_model_lru']['current']) == ('')
        assert (by_key['model_lru']['endpoint']) == ('https://api.together.xyz')

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
        # Voxtral is OpenRouter's STT default. Whisper Large v3 is a Together catalog id.
        stt_sticky = 'mistralai/voxtral-mini-transcribe'
        stt_ctrl.getText.return_value = stt_sticky
        stt_ctrl.getItemCount.return_value = 0
        image_ctrl = MagicMock()
        image_ctrl.getText.return_value = 'openai/gpt-5-image'
        image_ctrl.getItemCount.return_value = 0
        api_key_ctrl = MagicMock()
        api_key_ctrl.getText.return_value = 'tg-key'

        def get_optional_side_effect(dlg, name):
            return {
                'text_model': text_ctrl,
                'audio__stt_model': stt_ctrl,
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
        assert (sticky) not in (text_items)
        assert (text_ctrl.setText.call_args[0][0]) != (sticky)
        assert ('MiniMaxAI/MiniMax-M3') in (text_items)

        stt_items = list(stt_ctrl.addItems.call_args[0][0])
        assert (stt_sticky) not in (stt_items)
        assert ('nvidia/parakeet-tdt-0.6b-v3') in (stt_items)
        assert ('openai/whisper-large-v3') in (stt_items)
        image_items = list(image_ctrl.addItems.call_args[0][0])
        assert ('openai/gpt-5-image') not in (image_items)


class TestSettingsEnhancements:
    def test_get_signup_url_for_endpoint(self):
        from plugin.chatbot.config_ui_helpers import get_signup_url_for_endpoint

        assert (get_signup_url_for_endpoint("https://openrouter.ai/api")) == ("https://openrouter.ai/keys")
        assert (get_signup_url_for_endpoint("https://api.together.xyz")) == ("https://api.together.ai/settings/api-keys")
        assert (get_signup_url_for_endpoint("https://api.groq.com/openai")) == ("https://console.groq.com/keys")
        assert (get_signup_url_for_endpoint("https://integrate.api.nvidia.com/v1")) == ("https://build.nvidia.com/settings/api-keys")
        assert (get_signup_url_for_endpoint("http://localhost:11434")) is None
        assert (get_signup_url_for_endpoint("http://127.0.0.1:1234")) is None

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
        assert (parsed["mcpServers"]["libreoffice"]["url"]) == ("http://localhost:18765/mcp")

    def test_build_mcp_config_snippet_custom_port(self):
        import json
        from plugin.chatbot.dialog_views import build_mcp_config_snippet

        snippet = build_mcp_config_snippet(9000)
        parsed = json.loads(snippet)
        assert (parsed["mcpServers"]["libreoffice"]["url"]) == ("http://localhost:9000/mcp")

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
        assert (btn_model.Label) == ("✓ Copied!")

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
        assert (args[0]) == (snippet_ctrl)
        assert ("19999") in (args[1])


class TestProviderButtonIcons:
    def test_provider_icon_filename_picks_nearest_shipped_size(self):
        from plugin.chatbot.dialog_views import provider_icon_filename

        # Explicit px= is the post-menu-map target (16 / 32 / 48).
        assert (provider_icon_filename("openrouter", px=16)) == ("openrouter_16.png")
        assert (provider_icon_filename("huggingface", px=16)) == ("huggingface_16.png")
        assert (provider_icon_filename("together", px=32)) == ("together_32.png")
        assert (provider_icon_filename("nvidia", px=48)) == ("nvidia_48.png")

    def test_provider_icon_maps_menu_dpi_to_48_on_hidpi(self):
        from plugin.chatbot.dialog_views import provider_icon_filename

        with patch(
            "plugin.framework.menu_icon_dpi.resolve_menu_icon_pixel_size", return_value=32
        ):
            assert (provider_icon_filename("openrouter", ctx=MagicMock())) == ("openrouter_48.png")
        with patch(
            "plugin.framework.menu_icon_dpi.resolve_menu_icon_pixel_size", return_value=16
        ):
            assert (provider_icon_filename("openrouter", ctx=MagicMock())) == ("openrouter_16.png")
        with patch(
            "plugin.framework.menu_icon_dpi.resolve_menu_icon_pixel_size", return_value=26
        ):
            assert (provider_icon_filename("openrouter", ctx=MagicMock())) == ("openrouter_32.png")

    def test_apply_sets_image_url_from_extension_assets(self):
        from plugin.chatbot.dialog_views import apply_provider_button_icon

        ctx = MagicMock()
        ctrl = MagicMock()
        model = MagicMock()
        ctrl.getModel.return_value = model

        with patch("plugin.chatbot.dialog_views.get_extension_url", return_value="file:///tmp/oxt"), patch(
            "plugin.chatbot.dialog_views.provider_icon_filename", return_value="openrouter_16.png"
        ):
            apply_provider_button_icon(ctrl, ctx, "openrouter")

        assert (model.ImageURL) == ("file:///tmp/oxt/assets/openrouter_16.png")
        assert (model.ImagePosition) == (1)

    def test_apply_uses_dpi_selected_asset(self):
        from plugin.chatbot.dialog_views import apply_provider_button_icon

        ctx = MagicMock()
        ctrl = MagicMock()
        model = MagicMock()
        ctrl.getModel.return_value = model

        with patch("plugin.chatbot.dialog_views.get_extension_url", return_value="file:///tmp/oxt"), patch(
            "plugin.chatbot.dialog_views.provider_icon_filename", return_value="nvidia_32.png"
        ) as pick:
            apply_provider_button_icon(ctrl, ctx, "nvidia")

        pick.assert_called_once_with("nvidia", ctx=ctx)
        assert (model.ImageURL) == ("file:///tmp/oxt/assets/nvidia_32.png")


class TestRecheckGrammarListener:
    def test_click_calls_recheck_helper(self) -> None:
        from plugin.chatbot.dialog_views import RecheckGrammarListener

        ctx = MagicMock()
        with patch(
            "plugin.writer.locale.grammar_proofread_cache.recheck_active_document_grammar"
        ) as recheck:
            RecheckGrammarListener(ctx).on_action_performed(MagicMock())
        recheck.assert_called_once_with(ctx)


def test_ppt_master_data_test_uses_modal_probe() -> None:
    """Settings → Python PPT-Master Test uses the modal probe dialog."""
    from plugin.chatbot.dialog_views import PptMasterDataTestListener

    order: list[str] = []
    fake_ctx = MagicMock()
    fake_dlg = MagicMock()
    probe_displays: list[str] = []

    class _FakeProgress:
        def __init__(self, ctx, parent_dlg=None):
            order.append("progress_ctor")
            assert parent_dlg is fake_dlg

        def run_modal_probe(self, probe_fn, *, title=None):
            order.append("progress_run_modal")
            probe_fn(probe_displays.append, None)
            return True

    def fake_probe(raw, on_display, on_status=None):
        on_display("Data root: /tmp/skills/ppt-master\nSKILL.md: yes")
        if on_status:
            on_status("PPT-Master data root OK")
        return True, "ok"

    listener = PptMasterDataTestListener(fake_ctx, fake_dlg)
    with (
        patch("plugin.chatbot.dialog_views.get_optional", return_value=None),
        patch("plugin.chatbot.dialog_views.VenvProbeProgressDialog", _FakeProgress),
        patch("plugin.ppt_master.paths.probe_data_path_with_progress", side_effect=fake_probe),
    ):
        listener.on_action_performed(MagicMock())

    assert "progress_run_modal" in order
    assert any("SKILL.md" in text for text in probe_displays)


def test_dialog_parent_for_child_prefers_settings_peer() -> None:
    from plugin.chatbot.dialog_views import _dialog_parent_for_child

    parent = MagicMock()
    parent.getPeer.return_value = "settings-peer"
    assert _dialog_parent_for_child(MagicMock(), parent) == "settings-peer"
    parent.getPeer.assert_called_once()


def test_populate_fields_wires_audio_stt_model_lru() -> None:
    from plugin.chatbot.dialog_views import SettingsDialog

    dlg = MagicMock()
    stt_ctrl = MagicMock()
    dlg.getControl.side_effect = lambda name: stt_ctrl if name == "audio__stt_model" else None

    view = SettingsDialog(MagicMock())
    view._dlg = dlg
    field_specs = [{"name": "audio__stt_model", "value": "whisper-1"}]

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru") as mock_lru:
        view._populate_fields(field_specs, "https://openrouter.ai/api")
        mock_lru.assert_called_once_with(
            view._ctx, stt_ctrl, "whisper-1", "audio_model_lru", "https://openrouter.ai/api", api_key_override=""
        )


def test_populate_fields_wires_legacy_stt_control_id() -> None:
    from plugin.chatbot.dialog_views import SettingsDialog

    dlg = MagicMock()
    stt_ctrl = MagicMock()
    dlg.getControl.side_effect = lambda name: stt_ctrl if name == "stt_model" else None

    view = SettingsDialog(MagicMock())
    view._dlg = dlg
    field_specs = [{"name": "stt_model", "value": "whisper-legacy"}]

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru") as mock_lru:
        view._populate_fields(field_specs, "https://openrouter.ai/api")
        mock_lru.assert_called_once_with(
            view._ctx, stt_ctrl, "whisper-legacy", "audio_model_lru", "https://openrouter.ai/api", api_key_override=""
        )


def test_populate_fields_wires_tts_model_lru() -> None:
    from plugin.chatbot.dialog_views import SettingsDialog

    dlg = MagicMock()
    tts_ctrl = MagicMock()
    dlg.getControl.side_effect = lambda name: tts_ctrl if name == "audio__tts_model" else None

    view = SettingsDialog(MagicMock())
    view._dlg = dlg
    field_specs = [{"name": "audio__tts_model", "value": ""}]

    with patch("plugin.chatbot.config_ui_helpers.populate_combobox_with_lru") as mock_lru:
        view._populate_fields(field_specs, "https://openrouter.ai/api")
        mock_lru.assert_called_once_with(
            view._ctx, tts_ctrl, "", "tts_model_lru", "https://openrouter.ai/api", api_key_override=""
        )


def test_apply_dropdowns_updates_tts_combobox() -> None:
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    dlg = MagicMock()
    ctx = MagicMock()
    combo = MagicMock()
    listener = EndpointCombinedListener(dlg, ctx, combo)

    tts_ctrl = MagicMock()
    tts_ctrl.getText.return_value = ""

    def get_optional_side_effect(d, name):
        if name == "audio__tts_model":
            return tts_ctrl
        return None

    populate_calls = []

    def track_populate(c, ctrl, current, lru_key, endpoint, **kwargs):
        populate_calls.append({"lru_key": lru_key, "current": current, "remote_models": kwargs.get("remote_models")})

    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect):
        with patch("plugin.framework.config.get_config_str", return_value="endpoint"):
            with patch("plugin.framework.config.get_config", return_value=""):
                with patch("plugin.framework.config.get_current_endpoint", return_value="https://openrouter.ai/api"):
                    speech_ids = ["hexgrad/kokoro-82m", "microsoft/mai-voice-2"]
                    listener.fetch_available_tts_models = lambda endpoint, api_key_override=None: list(speech_ids)
                    listener.populate_combobox_with_lru = track_populate
                    listener._apply_dropdowns(
                        "https://openrouter.ai/api",
                        models=["openrouter/fusion"],
                        skip_fetch=False,
                    )

    tts_calls = [c for c in populate_calls if c["lru_key"] == "tts_model_lru"]
    assert len(tts_calls) == 1
    assert tts_calls[0]["remote_models"] == speech_ids


def test_apply_dropdowns_together_speech_uses_serverless_catalog() -> None:
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    listener = EndpointCombinedListener(MagicMock(), MagicMock(), MagicMock())
    tts_ctrl = MagicMock()
    tts_ctrl.getText.return_value = ""
    stt_ctrl = MagicMock()
    stt_ctrl.getText.return_value = ""

    def get_optional_side_effect(d, name):
        if name == "audio__tts_model":
            return tts_ctrl
        if name == "audio__stt_model":
            return stt_ctrl
        return None

    populate_calls = []

    def track_populate(c, ctrl, current, lru_key, endpoint, **kwargs):
        populate_calls.append({"lru_key": lru_key, "remote_models": kwargs.get("remote_models")})

    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect):
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.together.xyz"):
            listener.populate_combobox_with_lru = track_populate
            listener.fetch_available_tts_models = lambda *args, **kwargs: ["should-not-be-used"]
            listener.fetch_available_stt_models = lambda *args, **kwargs: ["should-not-be-used"]
            listener._apply_dropdowns(
                "https://api.together.xyz",
                models=["openai/gpt-oss-120b", "cartesia/sonic", "cartesia/sonic-4"],
                skip_fetch=False,
            )

    by_key = {c["lru_key"]: c["remote_models"] for c in populate_calls}
    tts_ids = by_key["tts_model_lru"]
    stt_ids = by_key["audio_model_lru"]
    assert "openai/gpt-oss-120b" not in tts_ids
    assert "should-not-be-used" not in tts_ids
    for mid in (
        "hexgrad/Kokoro-82M",
        "cartesia/sonic",
        "cartesia/sonic-2",
        "cartesia/sonic-3",
        "canopylabs/orpheus-3b-0.1-ft",
        "cartesia/sonic-4",
    ):
        assert mid in tts_ids
    for mid in (
        "nvidia/parakeet-tdt-0.6b-v3",
        "openai/whisper-large-v3",
        "nvidia/nemotron-3-asr-streaming-0.6b",
        "nvidia/nemotron-3.5-asr-streaming-0.6b",
    ):
        assert mid in stt_ids
    assert "hexgrad/Kokoro-82M" not in stt_ids


def test_apply_dropdowns_openrouter_tts_lists_speech_models() -> None:
    """Settings TTS combo uses the speech-modality fetch, not the curated Kokoro id alone."""
    from plugin.chatbot.dialog_views import EndpointCombinedListener
    from plugin.framework.client import model_fetcher as cfg

    listener = EndpointCombinedListener(MagicMock(), MagicMock(), MagicMock())
    tts_ctrl = MagicMock()
    tts_ctrl.getText.return_value = ""
    tts_ctrl.getItemCount.return_value = 0

    payload = {
        "data": [
            {
                "id": "hexgrad/kokoro-82m",
                "architecture": {"output_modalities": ["speech"]},
                "supported_voices": ["af_bella", {"id": "af_heart"}],
            },
            {
                "id": "x-ai/grok-voice-tts-1.0",
                "architecture": {"output_modalities": ["speech"]},
            },
            {
                "id": "microsoft/mai-voice-2",
                "architecture": {"output_modalities": ["speech"]},
            },
            {
                "id": "google/lyria-3-pro-preview",
                "architecture": {"output_modalities": ["audio"]},
            },
        ]
    }

    for key in list(cfg._model_fetch_tts_cache):
        if "openrouter.ai" in key:
            cfg._model_fetch_tts_cache.pop(key, None)
    cfg._tts_supported_voices.pop("hexgrad/kokoro-82m", None)
    cfg._tts_response_format.pop("hexgrad/kokoro-82m", None)

    def get_optional_side_effect(d, name):
        if name in ("audio__tts_model", "tts_model"):
            return tts_ctrl
        return None

    def mock_get_config(key):
        if isinstance(key, str) and "lru" in key:
            return []
        return ""

    try:
        with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
             patch("plugin.chatbot.dialog_views.get_config", side_effect=mock_get_config), \
             patch("plugin.chatbot.dialog_views.get_current_endpoint", return_value="https://openrouter.ai/api"), \
             patch("plugin.chatbot.config_ui_helpers.get_config", side_effect=mock_get_config), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""), \
             patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"), \
             patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync:
            listener._apply_dropdowns(
                "https://openrouter.ai/api",
                models=["openrouter/fusion"],
                skip_fetch=False,
            )
        urls = [call.args[0] for call in mock_sync.call_args_list]
        assert any("output_modalities=speech" in url for url in urls)
        assert not any("output_modalities=audio" in url for url in urls)
    finally:
        for key in list(cfg._model_fetch_tts_cache):
            if "openrouter.ai" in key:
                cfg._model_fetch_tts_cache.pop(key, None)

    items = list(tts_ctrl.addItems.call_args[0][0])
    assert "hexgrad/kokoro-82m" in items
    assert "x-ai/grok-voice-tts-1.0" in items
    assert "microsoft/mai-voice-2" in items
    assert "hexgrad/Kokoro-82M" not in items
    assert "google/lyria-3-pro-preview" not in items
    assert tts_ctrl.setText.call_args[0][0] == "hexgrad/kokoro-82m"
    assert cfg.cached_tts_supported_voices("hexgrad/Kokoro-82M") == ["af_bella", "af_heart"]


def test_bg_fetch_together_warms_voices() -> None:
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    listener = EndpointCombinedListener(MagicMock(), MagicMock(), MagicMock())
    listener.fetch_available_models = lambda *args, **kwargs: ["cartesia/sonic"]
    listener.post_to_main_thread = lambda fn: None
    listener._debounce_gen = 1

    with patch("plugin.chatbot.dialog_views.get_optional", return_value=None), \
         patch("plugin.framework.client.model_fetcher.fetch_together_tts_voices") as mock_voices:
        listener._bg_fetch(1, "https://api.together.xyz")
        mock_voices.assert_called_once()
        assert mock_voices.call_args.args[0] == "https://api.together.xyz"
        mock_voices.reset_mock()
        listener._bg_fetch(1, "https://openrouter.ai/api")
        mock_voices.assert_not_called()


def test_tts_settings_listener_together_voices_replace_alloy() -> None:
    """Together endpoint Voice combo uses /v1/voices, and alloy is not kept."""
    from plugin.chatbot.dialog_views import TtsSettingsListener
    from plugin.framework.client import model_fetcher as cfg

    dlg = MagicMock()
    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "Current Chat Endpoint (/audio/speech)"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = "cartesia/sonic-2"
    voice_ctrl = MagicMock()
    voice_model = MagicMock()
    voice_model.StringItemList = ()
    voice_ctrl.getModel.return_value = voice_model
    voice_ctrl.getText.return_value = "alloy"
    endpoint_ctrl = MagicMock()
    endpoint_ctrl.getText.return_value = "Together AI"

    payload = {
        "model": "cartesia/sonic-2",
        "voices": [
            {"name": "Narrator", "id": "voice-uuid-2"},
            {"name": "Friendly Sidekick", "id": "voice-uuid-1", "language": "en"},
        ],
    }

    def get_optional_side_effect(d, name):
        del d
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        if name == "endpoint":
            return endpoint_ctrl
        return None

    stored: dict[str, str] = {}
    cfg._tts_supported_voices.clear()
    cfg._together_voices_fetch_cache.clear()
    try:
        with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
             patch("plugin.chatbot.dialog_views.set_control_enabled"), \
             patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync, \
             patch("plugin.audio.tts_service.set_config", side_effect=lambda k, v: stored.__setitem__(k, v)), \
             patch("plugin.chatbot.dialog_views.get_config", return_value=""):
            TtsSettingsListener(dlg, MagicMock()).sync_ui()
            urls = [call.args[0] for call in mock_sync.call_args_list]
            assert urls == ["https://api.together.xyz/v1/voices?model=cartesia%2Fsonic-2"]
        # API order is uuid-2 then uuid-1. The combo sorts labels; the cache does not.
        assert list(voice_model.StringItemList) == ["voice-uuid-1", "voice-uuid-2"]
        assert voice_ctrl.setText.call_args[0][0] == "voice-uuid-1"
        assert stored.get("audio.tts_voice_together") == "voice-uuid-1"
        assert cfg.cached_tts_supported_voices("cartesia/sonic-2") == ["voice-uuid-2", "voice-uuid-1"]
    finally:
        cfg._tts_supported_voices.clear()
        cfg._together_voices_fetch_cache.clear()


def test_tts_settings_listener_sync():
    from plugin.chatbot.dialog_views import TtsSettingsListener

    dlg = MagicMock()
    ctx = MagicMock()

    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "Kokoro (Local Neural, ONNX CPU)"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = ""
    voice_ctrl = MagicMock()
    voice_ctrl_model = MagicMock()
    voice_ctrl.getModel.return_value = voice_ctrl_model
    voice_ctrl.getText.return_value = ""

    def get_optional_side_effect(d, name):
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        return None

    def _cfg(key, default=None):
        # sync_ui reads tts_service.get_config, not framework.config.get_config.
        # Return the schema default so a developer profile (af_bella) cannot
        # change which Kokoro voice this test expects.
        del default
        if key == "audio.tts_voice_kokoro":
            return "af_sky"
        return ""

    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
         patch("plugin.chatbot.dialog_views.set_control_enabled") as mock_set_enabled, \
         patch("plugin.audio.tts_service.get_config", side_effect=_cfg):
        listener = TtsSettingsListener(dlg, ctx)
        listener.sync_ui()

        # Model combobox is disabled for non-endpoint
        mock_set_enabled.assert_called_once_with(model_ctrl, False)
        # Voice list is updated with Kokoro voices
        assert any("af_bella" in label for label in voice_ctrl_model.StringItemList)
        # Voice text is set to the Kokoro default (Sky)
        assert any("af_sky" in arg for arg in voice_ctrl.setText.call_args[0])


def test_tts_settings_listener_promotes_raw_voice_id_to_catalog_label():
    from plugin.chatbot.dialog_views import TtsSettingsListener

    dlg = MagicMock()
    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "Piper (Local Fast Neural, CPU)"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = ""
    voice_ctrl = MagicMock()
    voice_ctrl.getModel.return_value = MagicMock()
    voice_ctrl.getText.return_value = "de_DE-thorsten-medium"

    def get_optional_side_effect(d, name):
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        return None

    def _cfg(key, default=None):
        if key == "audio.tts_voice_piper":
            return "de_DE-thorsten-medium"
        return default

    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
         patch("plugin.chatbot.dialog_views.set_control_enabled"), \
         patch("plugin.audio.tts_service.get_config", side_effect=_cfg):
        TtsSettingsListener(dlg, MagicMock()).sync_ui()

    voice_ctrl.setText.assert_called_once()
    assert "Thorsten" in voice_ctrl.setText.call_args[0][0]


def test_tts_settings_listener_uses_cached_openrouter_voices():
    from plugin.chatbot.dialog_views import TtsSettingsListener
    from plugin.framework.client import model_fetcher as cfg

    model_id = "google/gemini-2.5-flash-preview-tts"
    cfg._tts_supported_voices[model_id] = ["Zephyr", "Puck", "Kore"]
    dlg = MagicMock()
    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "LLM Endpoint"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = model_id
    voice_ctrl = MagicMock()
    voice_model = MagicMock()
    voice_model.StringItemList = ()
    voice_ctrl.getModel.return_value = voice_model
    voice_ctrl.getText.return_value = "alloy (OpenAI Neutral)"

    def get_optional_side_effect(d, name):
        del d
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        return None

    stored = {}
    try:
        with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
             patch("plugin.chatbot.dialog_views.set_control_enabled"), \
             patch("plugin.audio.tts_service.get_config", side_effect=lambda key, default=None: stored.get(key, default)), \
             patch("plugin.audio.tts_service.set_config", side_effect=lambda key, val: stored.__setitem__(key, val)):
            TtsSettingsListener(dlg, MagicMock()).sync_ui()
        assert list(voice_model.StringItemList) == ["Kore", "Puck", "Zephyr"]
        assert voice_ctrl.setText.call_args[0][0] == "Kore"
        assert stored.get("audio.tts_voice_openrouter") == "Kore"
        assert stored.get("audio.tts_voice") == "Kore"
    finally:
        cfg._tts_supported_voices.pop(model_id, None)


def test_tts_settings_listener_prefers_aoede_for_gemini():
    """Settings fallback matches speak: Aoede for Gemini, else the sorted first id."""
    from plugin.chatbot.dialog_views import TtsSettingsListener
    from plugin.framework.client import model_fetcher as cfg

    gemini = "google/gemini-2.5-flash-preview-tts"
    grok = "x-ai/grok-voice-tts-1.0"
    cfg._tts_supported_voices[gemini] = ["Zephyr", "Achernar", "Aoede"]
    cfg._tts_supported_voices[grok] = ["Zephyr", "Achernar", "Aoede"]

    def sync(model_id: str, visible: str, stored_voice: str) -> tuple[str, dict[str, str], tuple[str, ...]]:
        dlg = MagicMock()
        prov_ctrl = MagicMock()
        prov_ctrl.getText.return_value = "LLM Endpoint"
        model_ctrl = MagicMock()
        model_ctrl.getText.return_value = model_id
        voice_ctrl = MagicMock()
        voice_model = MagicMock()
        voice_model.StringItemList = ()
        voice_ctrl.getModel.return_value = voice_model
        voice_ctrl.getText.return_value = visible

        def get_optional_side_effect(d, name):
            del d
            if name == "audio__tts_provider":
                return prov_ctrl
            if name in ("audio__tts_model", "tts_model"):
                return model_ctrl
            if name == "audio__tts_voice":
                return voice_ctrl
            return None

        stored: dict = {"audio.tts_voice_openrouter": stored_voice}
        with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
             patch("plugin.chatbot.dialog_views.set_control_enabled"), \
             patch("plugin.audio.tts_service.get_config", side_effect=lambda key, default=None: stored.get(key, default)), \
             patch("plugin.audio.tts_service.set_config", side_effect=lambda key, val: stored.__setitem__(key, val)):
            TtsSettingsListener(dlg, MagicMock()).sync_ui()
        if voice_ctrl.setText.called:
            shown = voice_ctrl.setText.call_args[0][0]
        else:
            shown = visible
        return shown, stored, tuple(voice_model.StringItemList)

    try:
        shown, stored, labels = sync(gemini, "alloy (OpenAI Neutral)", "alloy")
        assert shown == "Aoede"
        assert stored.get("audio.tts_voice_openrouter") == "Aoede"
        # Preference picks Aoede; the combo stays in label order.
        assert labels == ("Achernar", "Aoede", "Zephyr")

        shown, stored, labels = sync(grok, "alloy (OpenAI Neutral)", "alloy")
        assert shown == "Achernar"
        assert stored.get("audio.tts_voice_openrouter") == "Achernar"

        # Visible in-list voice wins over Aoede.
        shown, stored, labels = sync(gemini, "Zephyr", "alloy")
        assert shown == "Zephyr"
        assert stored.get("audio.tts_voice_openrouter") == "Zephyr"

        # Empty combo: saved in-list id wins, including a saved Aoede.
        shown, stored, labels = sync(gemini, "", "Aoede")
        assert shown == "Aoede"
        assert stored.get("audio.tts_voice_openrouter") == "Aoede"
        shown, stored, labels = sync(gemini, "", "Zephyr")
        assert shown == "Zephyr"
        assert stored.get("audio.tts_voice_openrouter") == "Zephyr"

        cfg._tts_supported_voices[gemini] = ["Zephyr", "Puck", "Kore"]
        shown, stored, labels = sync(gemini, "alloy", "")
        assert shown == "Kore"
        assert stored.get("audio.tts_voice_openrouter") == "Kore"

        cfg._tts_supported_voices[gemini] = ["Puck", "aoede"]
        shown, stored, labels = sync("google/Gemini-2.5-flash-preview-tts", "alloy", "alloy")
        assert shown == "aoede"
        assert stored.get("audio.tts_voice_openrouter") == "aoede"
    finally:
        cfg._tts_supported_voices.pop(gemini, None)
        cfg._tts_supported_voices.pop(grok, None)


def test_tts_settings_listener_keeps_voice_when_remote_list_is_sorted():
    """Re-sorting the endpoint combo must not replace a voice that is still listed."""
    from plugin.chatbot.dialog_views import TtsSettingsListener
    from plugin.framework.client import model_fetcher as cfg

    model_id = "google/gemini-2.5-flash-preview-tts"
    cfg._tts_supported_voices[model_id] = ["Zephyr", "Puck", "Kore"]
    dlg = MagicMock()
    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "LLM Endpoint"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = model_id
    voice_ctrl = MagicMock()
    voice_model = MagicMock()
    voice_model.StringItemList = ()
    voice_ctrl.getModel.return_value = voice_model
    voice_ctrl.getText.return_value = "Puck"

    def get_optional_side_effect(d, name):
        del d
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        return None

    stored = {"audio.tts_voice_openrouter": "Puck"}
    try:
        with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
             patch("plugin.chatbot.dialog_views.set_control_enabled"), \
             patch("plugin.audio.tts_service.get_config", side_effect=lambda key, default=None: stored.get(key, default)), \
             patch("plugin.audio.tts_service.set_config", side_effect=lambda key, val: stored.__setitem__(key, val)):
            TtsSettingsListener(dlg, MagicMock()).sync_ui()
        assert list(voice_model.StringItemList) == ["Kore", "Puck", "Zephyr"]
        voice_ctrl.setText.assert_not_called()
        assert stored.get("audio.tts_voice_openrouter") == "Puck"
    finally:
        cfg._tts_supported_voices.pop(model_id, None)


def test_tts_test_voice_status_shows_http_body():
    from plugin.chatbot.dialog_views import TtsTestVoiceListener

    listener = TtsTestVoiceListener(MagicMock(), MagicMock())

    def _speak(*args, **kwargs):
        del args
        kwargs["on_status"]("Speech request failed (400): response_format must be pcm")

    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=_tts_test_controls()), \
         patch("plugin.chatbot.dialog_views.is_checkbox_control", return_value=True), \
         patch("plugin.chatbot.dialog_views.get_checkbox_state", return_value=1), \
         patch("plugin.audio.tts_service.tts_test_sample", return_value="Hello"), \
         patch("plugin.audio.tts_service.speak_text_async", side_effect=_speak), \
         patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)), \
         patch("plugin.chatbot.dialog_views.msgbox") as mock_msgbox:
        listener.on_action_performed(None)

    mock_msgbox.assert_called_once()
    assert "response_format must be pcm" in mock_msgbox.call_args[0][2]


def _tts_test_controls(*, enabled: int = 1):
    enabled_ctrl = MagicMock()
    enabled_ctrl.getState.return_value = enabled
    enabled_ctrl.supportsService.return_value = True
    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "Kokoro (Local Neural, ONNX CPU)"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = "hexgrad/Kokoro-82M"
    voice_ctrl = MagicMock()
    voice_ctrl.getText.return_value = "jf_alpha (Kokoro JP Female - Alpha)"
    speed_ctrl = MagicMock()
    speed_ctrl.getText.return_value = "1.25x"

    def get_optional_side_effect(dlg, name):
        del dlg
        if name == "audio__tts_enabled":
            return enabled_ctrl
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        if name == "audio__tts_speed":
            return speed_ctrl
        return None

    return get_optional_side_effect


def test_tts_test_voice_listener_speaks_localized_sample():
    from plugin.chatbot.dialog_views import TtsTestVoiceListener

    listener = TtsTestVoiceListener(MagicMock(), MagicMock())
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=_tts_test_controls()), \
         patch("plugin.chatbot.dialog_views.is_checkbox_control", return_value=True), \
         patch("plugin.chatbot.dialog_views.get_checkbox_state", return_value=1), \
         patch("plugin.audio.tts_service.tts_test_sample", return_value="こんにちは、WriterAgent です。"), \
         patch("plugin.audio.tts_service.speak_text_async") as mock_speak, \
         patch("plugin.chatbot.dialog_views.msgbox") as mock_msgbox:
        listener.on_action_performed(None)

    mock_msgbox.assert_not_called()
    mock_speak.assert_called_once()
    args, kwargs = mock_speak.call_args
    assert args[0] == "こんにちは、WriterAgent です。"
    assert kwargs["provider"] == "Kokoro (Local Neural, ONNX CPU)"
    assert kwargs["model"] == "hexgrad/Kokoro-82M"
    assert kwargs["voice"] == "jf_alpha"
    assert kwargs["speed"] == 1.25
    assert kwargs["enabled"] is True
    assert "lang" not in kwargs
    assert kwargs["on_status"] is not None


def test_tts_test_voice_listener_disabled_does_not_speak():
    from plugin.chatbot.dialog_views import TtsTestVoiceListener

    listener = TtsTestVoiceListener(MagicMock(), MagicMock())
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=_tts_test_controls(enabled=0)), \
         patch("plugin.chatbot.dialog_views.is_checkbox_control", return_value=True), \
         patch("plugin.chatbot.dialog_views.get_checkbox_state", return_value=0), \
         patch("plugin.audio.tts_service.speak_text_async") as mock_speak, \
         patch("plugin.chatbot.dialog_views.msgbox") as mock_msgbox:
        listener.on_action_performed(None)

    mock_speak.assert_not_called()
    mock_msgbox.assert_called_once()
    assert "Speech output is off" in mock_msgbox.call_args[0][2]


def test_tts_test_voice_listener_speak_error_stays_in_dialog():
    from plugin.chatbot.dialog_views import TtsTestVoiceListener

    listener = TtsTestVoiceListener(MagicMock(), MagicMock())
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=_tts_test_controls()), \
         patch("plugin.chatbot.dialog_views.is_checkbox_control", return_value=True), \
         patch("plugin.chatbot.dialog_views.get_checkbox_state", return_value=1), \
         patch("plugin.audio.tts_service.tts_test_sample", return_value="Hello"), \
         patch("plugin.audio.tts_service.speak_text_async", side_effect=RuntimeError("no audio")), \
         patch("plugin.chatbot.dialog_views.msgbox") as mock_msgbox:
        listener.on_action_performed(None)

    mock_msgbox.assert_called_once()
    assert mock_msgbox.call_args[0][2] == "Could not play the voice sample."


def test_tts_voice_listener_on_change():
    from plugin.chatbot.dialog_views import TtsSettingsListener, TtsVoiceListener

    dlg = MagicMock()
    ctx = MagicMock()

    prov_ctrl = MagicMock()
    prov_ctrl.getText.return_value = "Piper (Local Fast Neural, CPU)"
    model_ctrl = MagicMock()
    model_ctrl.getText.return_value = ""
    voice_ctrl = MagicMock()
    voice_ctrl.getText.return_value = "en_US-amy-medium (Piper US Female - Amy)"

    def get_optional_side_effect(d, name):
        if name == "audio__tts_provider":
            return prov_ctrl
        if name in ("audio__tts_model", "tts_model"):
            return model_ctrl
        if name == "audio__tts_voice":
            return voice_ctrl
        return None

    settings_listener = TtsSettingsListener(dlg, ctx)
    voice_listener = TtsVoiceListener(dlg, settings_listener)

    stored = {}
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=get_optional_side_effect), \
         patch("plugin.audio.tts_service.set_config", side_effect=lambda k, v: stored.__setitem__(k, v)):
        voice_listener._on_change()
        assert stored.get("audio.tts_voice_piper") == "en_US-amy-medium"
        assert stored.get("audio.tts_voice") == "en_US-amy-medium"


