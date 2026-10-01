"""Settings dialog field specs (Image tab aspect dropdown)."""

from unittest.mock import MagicMock, patch

from plugin.chatbot.settings_dialog import IMAGE_ASPECT_RATIO_LABELS, get_settings_field_specs


def test_image_default_aspect_has_sidebar_matching_options():
    """Settings Image tab must list the same five aspect labels as the sidebar."""
    with (
        patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api/v1"),
        patch("plugin.chatbot.settings_dialog.get_config_str", return_value="Square"),
        patch("plugin.chatbot.settings_dialog.get_config_int", return_value=512),
        patch("plugin.chatbot.settings_dialog.get_config_float", return_value=0.7),
        patch("plugin.chatbot.settings_dialog.get_config_bool", return_value=True),
        patch("plugin.chatbot.settings_dialog.get_api_key_for_endpoint", return_value=""),
        patch("plugin.chatbot.settings_dialog.get_text_model", return_value="m"),
        patch("plugin.chatbot.settings_dialog.get_image_model", return_value="img"),
        patch("plugin.chatbot.settings_dialog._get_module_field_specs", return_value=[]),
    ):
        specs = get_settings_field_specs(MagicMock())

    aspect = next(s for s in specs if s["name"] == "image_default_aspect")
    labels = tuple(o["label"] for o in aspect["options"])
    values = tuple(o["value"] for o in aspect["options"])
    assert labels == IMAGE_ASPECT_RATIO_LABELS
    assert values == IMAGE_ASPECT_RATIO_LABELS
    assert aspect["value"] == "Square"
    assert IMAGE_ASPECT_RATIO_LABELS == (
        "Square",
        "Landscape (16:9)",
        "Portrait (9:16)",
        "Landscape (3:2)",
        "Portrait (2:3)",
    )


def test_image_aspect_display_is_translated_value_stays_english():
    """Combo shows gettext labels; Apply still stores the English aspect string."""
    from plugin.chatbot.settings_dialog import apply_settings_result, get_settings_field_specs

    def fake_gettext(message: str) -> str:
        if message == "Square":
            return "正方形"
        return message

    with (
        patch("plugin.chatbot.settings_dialog._", side_effect=fake_gettext),
        patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api/v1"),
        patch("plugin.chatbot.settings_dialog.get_config_str", return_value="Square"),
        patch("plugin.chatbot.settings_dialog.get_config_int", return_value=512),
        patch("plugin.chatbot.settings_dialog.get_config_float", return_value=0.7),
        patch("plugin.chatbot.settings_dialog.get_config_bool", return_value=True),
        patch("plugin.chatbot.settings_dialog.get_api_key_for_endpoint", return_value=""),
        patch("plugin.chatbot.settings_dialog.get_text_model", return_value="m"),
        patch("plugin.chatbot.settings_dialog.get_image_model", return_value="img"),
        patch("plugin.chatbot.settings_dialog._get_module_field_specs", return_value=[]),
        patch("plugin.chatbot.settings_dialog.set_configs") as set_configs,
    ):
        specs = get_settings_field_specs(MagicMock())
        aspect = next(s for s in specs if s["name"] == "image_default_aspect")
        assert aspect["value"] == "正方形"
        assert aspect["options"][0] == {"label": "正方形", "value": "Square"}
        apply_settings_result(MagicMock(), {"image_default_aspect": "正方形"})

    set_configs.assert_called_once()
    saved = set_configs.call_args.args[0]
    assert saved["image_default_aspect"] == "Square"


def test_canonical_aspect_label_maps_translated_and_english():
    from plugin.chatbot.settings_dialog import canonical_aspect_label

    with patch("plugin.chatbot.settings_dialog._", side_effect=lambda message: "正方形" if message == "Square" else message):
        assert canonical_aspect_label("正方形") == "Square"
        assert canonical_aspect_label("Square") == "Square"
        assert canonical_aspect_label("Landscape (16:9)") == "Landscape (16:9)"
        assert canonical_aspect_label("") == "Square"


def test_core_field_specs_omit_stt_model():
    """Audio Model is a Speech-tab yaml field, not a General-page core spec."""
    from plugin.chatbot.settings_dialog import _get_core_field_specs

    with (
        patch("plugin.chatbot.settings_dialog.get_config_str", return_value=""),
        patch("plugin.chatbot.settings_dialog.get_config_int", return_value=0),
        patch("plugin.chatbot.settings_dialog.get_config_float", return_value=0.0),
        patch("plugin.chatbot.settings_dialog.get_api_key_for_endpoint", return_value=""),
        patch("plugin.chatbot.settings_dialog.get_text_model", return_value=""),
    ):
        specs = _get_core_field_specs(MagicMock(), "https://openrouter.ai/api")
    names = {spec["name"] for spec in specs}
    assert "stt_model" not in names
    assert "audio__stt_model" not in names
    # No XDL controls. OK used to write "" over the schema defaults.
    assert "text_analytics_sentiment_model" not in names
    assert "text_analytics_sentiment_engine" not in names


def test_update_lru_for_audio_stt_model():
    from plugin.chatbot.settings_dialog import _update_lru_for_key

    with patch("plugin.chatbot.config_ui_helpers.update_lru_history") as mock_lru:
        _update_lru_for_key(MagicMock(), "audio__stt_model", "whisper-1", "https://openrouter.ai/api")
        mock_lru.assert_called_once_with("whisper-1", "audio_model_lru", "https://openrouter.ai/api")


def test_apply_settings_writes_audio_stt_model():
    """Speech tab save stores audio.stt_model and does not write legacy stt_model."""
    from plugin.chatbot.settings_dialog import apply_settings_result

    stored = {}
    specs = [{"name": "audio__stt_model", "value": ""}]

    with patch("plugin.chatbot.settings_dialog.get_settings_field_specs", return_value=specs), \
         patch("plugin.chatbot.settings_dialog.set_configs", side_effect=lambda values: stored.update(values)), \
         patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api"), \
         patch("plugin.chatbot.config_ui_helpers.update_lru_history") as mock_lru:
        apply_settings_result(MagicMock(), {"audio__stt_model": "whisper-1"})

    assert stored.get("audio.stt_model") == "whisper-1"
    assert "stt_model" not in stored
    mock_lru.assert_called_once_with("whisper-1", "audio_model_lru", "https://openrouter.ai/api")


def test_update_lru_for_tts_model():
    from plugin.chatbot.settings_dialog import _update_lru_for_key

    with patch("plugin.chatbot.config_ui_helpers.update_lru_history") as mock_lru:
        _update_lru_for_key(MagicMock(), "audio__tts_model", "hexgrad/Kokoro-82M", "https://openrouter.ai/api")
        mock_lru.assert_called_once_with("hexgrad/Kokoro-82M", "tts_model_lru", "https://openrouter.ai/api")


def test_apply_settings_result_tts_provider_and_voice():
    from plugin.chatbot.settings_dialog import apply_settings_result

    stored = {}
    specs = [
        {
            "name": "audio__tts_provider",
            "options": [
                {"value": "kokoro", "label": "Kokoro (Local Neural, ONNX CPU)"},
                {"value": "piper", "label": "Piper (Local Fast Neural, CPU)"},
            ],
        },
        {
            "name": "audio__tts_voice",
            "options": [
                {"value": "af_bella", "label": "af_bella (Kokoro US Female - Bella)"},
            ],
        },
    ]

    with patch("plugin.chatbot.settings_dialog.get_settings_field_specs", return_value=specs), \
         patch("plugin.chatbot.settings_dialog.set_configs", side_effect=lambda values: stored.update(values)), \
         patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api"), \
         patch("plugin.audio.tts_service.set_config") as tts_set:
        apply_settings_result(MagicMock(), {
            "audio__tts_provider": "Kokoro (Local Neural, ONNX CPU)",
            "audio__tts_voice": "af_bella (Kokoro US Female - Bella)",
        })
        tts_set.assert_not_called()
        assert stored.get("audio.tts_provider") == "kokoro"
        assert stored.get("audio.tts_voice_kokoro") == "af_bella"
        assert stored.get("audio.tts_voice") == "af_bella"


def test_apply_settings_voice_display_label_stores_id_for_that_provider():
    """Parenthetical text stores the id for the provider in this result.

    Field specs can still list the other engine's copy of the same words.
    """
    from plugin.chatbot.settings_dialog import apply_settings_result

    stored = {}
    specs = [
        {
            "name": "audio__tts_provider",
            "options": [
                {"value": "kokoro", "label": "Kokoro (Local Neural, ONNX CPU)"},
                {"value": "piper", "label": "Piper (Local Fast Neural, CPU)"},
            ],
        },
        {
            "name": "audio__tts_voice",
            "options": [
                {"value": "ff_siwis", "label": "French Female - Siwis"},
            ],
        },
    ]

    with patch("plugin.chatbot.settings_dialog.get_settings_field_specs", return_value=specs), \
         patch("plugin.chatbot.settings_dialog.set_configs", side_effect=lambda values: stored.update(values)), \
         patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api"), \
         patch("plugin.audio.tts_service.set_config") as tts_set:
        apply_settings_result(MagicMock(), {
            "audio__tts_provider": "Piper (Local Fast Neural, CPU)",
            "audio__tts_voice": "French Female - Siwis",
        })
        tts_set.assert_not_called()
        assert stored.get("audio.tts_voice_piper") == "fr_FR-siwis-medium"
        assert stored.get("audio.tts_voice") == "fr_FR-siwis-medium"


def test_apply_settings_result_tts_speed():
    from plugin.chatbot.settings_dialog import apply_settings_result

    stored = {}
    specs = [
        {
            "name": "audio__tts_speed",
            "options": [
                {"value": "1.0x", "label": "1.0x"},
                {"value": "1.25x", "label": "1.25x"},
            ],
        },
    ]

    with patch("plugin.chatbot.settings_dialog.get_settings_field_specs", return_value=specs), \
         patch("plugin.chatbot.settings_dialog.set_configs", side_effect=lambda values: stored.update(values)), \
         patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api"):
        # Select from dropdown
        apply_settings_result(MagicMock(), {"audio__tts_speed": "1.25x"})
        assert stored.get("audio.tts_speed") == "1.25x"

        # Type custom valid lower number
        apply_settings_result(MagicMock(), {"audio__tts_speed": "0.5"})
        assert stored.get("audio.tts_speed") == "0.5"

        # Type number below 0.25 minimum -> clamped to 0.25
        apply_settings_result(MagicMock(), {"audio__tts_speed": "0.1"})
        assert stored.get("audio.tts_speed") == "0.25"


def test_apply_settings_stt_provider_and_local_model():
    """Speech tab stores canonical provider and Whisper size, including aliases."""
    from plugin.chatbot.settings_dialog import apply_settings_result

    stored = {}
    specs = [
        {
            "name": "audio__stt_provider",
            "options": [
                {"value": "endpoint", "label": "LLM Endpoint"},
                {"value": "local", "label": "Local Whisper (faster-whisper)"},
            ],
        },
        {
            "name": "audio__stt_local_model",
            "options": [
                {"value": "base", "label": "base (~150 MB)"},
                {"value": "small", "label": "small (~500 MB)"},
            ],
        },
    ]

    with patch("plugin.chatbot.settings_dialog.get_settings_field_specs", return_value=specs), \
         patch("plugin.chatbot.settings_dialog.set_configs", side_effect=lambda values: stored.update(values)), \
         patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="https://openrouter.ai/api"):
        apply_settings_result(MagicMock(), {
            "audio__stt_provider": "Local Whisper (faster-whisper)",
            "audio__stt_local_model": "small (~500 MB)",
        })
        assert stored.get("audio.stt_provider") == "local"
        assert stored.get("audio.stt_local_model") == "small"

        apply_settings_result(MagicMock(), {
            "audio__stt_provider": "faster-whisper",
            "audio__stt_local_model": "BASE",
        })
        assert stored.get("audio.stt_provider") == "local"
        assert stored.get("audio.stt_local_model") == "base"


def test_apply_settings_result_one_batch_no_extra_emit():
    """Endpoint, model, API key, and voice are one set_configs call.

    LRU runs after the batch. This function must not emit config:changed
    itself — that unconditional emit refreshed the sidebar mode combo even
    when the batch wrote nothing.
    """
    from plugin.chatbot.config_ui_helpers import endpoint_from_selector_text
    from plugin.chatbot.settings_dialog import apply_settings_result
    from plugin.framework.url_utils import normalize_endpoint_url

    raw_endpoint = "http://127.0.0.1:9/v1"
    current = endpoint_from_selector_text(raw_endpoint)
    key_slot = normalize_endpoint_url(current)
    specs = [
        {"name": "endpoint"},
        {"name": "text_model"},
        {"name": "api_key"},
        {"name": "temperature"},
        {
            "name": "audio__tts_provider",
            "options": [{"value": "kokoro", "label": "Kokoro (Local Neural, ONNX CPU)"}],
        },
        {"name": "audio__tts_voice"},
    ]
    order: list[str] = []

    def _cfg(key: str):
        if key == "text_model":
            return "old-model"
        if key == "api_keys_by_endpoint":
            return {}
        return ""

    with patch("plugin.chatbot.settings_dialog.get_settings_field_specs", return_value=specs), \
         patch("plugin.chatbot.settings_dialog.get_config", side_effect=_cfg), \
         patch("plugin.chatbot.settings_dialog.set_configs", side_effect=lambda values: order.append("batch") or values) as batch, \
         patch("plugin.chatbot.config_ui_helpers.update_lru_history", side_effect=lambda *args: order.append("lru")) as lru, \
         patch("plugin.framework.event_bus.global_event_bus.emit") as emit, \
         patch("plugin.framework.config.set_config") as single, \
         patch("plugin.audio.tts_service.set_config") as tts_set:
        apply_settings_result(MagicMock(), {
            "endpoint": raw_endpoint,
            "text_model": "new-model",
            "api_key": "sk-test",
            "temperature": "0.2",
            "audio__tts_provider": "Kokoro (Local Neural, ONNX CPU)",
            "audio__tts_voice": "af_bella",
        })

    batch.assert_called_once()
    pending = batch.call_args.args[0]
    assert pending["endpoint"] == raw_endpoint
    assert pending["text_model"] == "new-model"
    assert pending["temperature"] == "0.2"
    assert pending["audio.tts_provider"] == "kokoro"
    assert pending["audio.tts_voice"] == "af_bella"
    assert pending["audio.tts_voice_kokoro"] == "af_bella"
    assert pending["api_keys_by_endpoint"][key_slot] == "sk-test"
    assert order[0] == "batch"
    assert "lru" in order
    lru.assert_any_call("new-model", "model_lru", current)
    emit.assert_not_called()
    single.assert_not_called()
    tts_set.assert_not_called()
