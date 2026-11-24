# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings > General: a catalog repaint after a provider switch must keep model edits.

Regression: saved endpoint OpenRouter, user switches the endpoint field to
Together and edits Text model. The next fill (catalog landing, API key typing,
Test Connection) compared against the *saved* provider again, reset Text model
to the Together default and moved Image model to the catalog's first id.
"""
from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.url_utils import normalize_endpoint_url

OR = "https://openrouter.ai/api/v1"
TG = "https://api.together.xyz/v1"
TG_TEXT = ["MiniMaxAI/MiniMax-M3", "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"]
TG_IMAGES = ["black-forest-labs/FLUX.1-schnell", "black-forest-labs/FLUX.2-dev"]


@pytest.fixture(autouse=True)
def _cleanup_patches():
    yield
    patch.stopall()


class FakeCombo:
    def __init__(self, text=""):
        self.text = text
        self.items = []

    def getText(self):
        return self.text

    def setText(self, t):
        self.text = t

    def getItemCount(self):
        return len(self.items)

    def getItem(self, i):
        return self.items[i]

    def removeItems(self, pos, count):
        del self.items[pos:pos + count]

    def addItems(self, items, pos):
        self.items[pos:pos] = list(items)


def _setup():
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    save({
        "endpoint": OR,
        "text_model": "openai/gpt-oss-120b:nitro",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    save({"api_keys_by_endpoint": {normalize_endpoint_url(TG): "dummy-not-a-key"}})
    ctrls = {
        "endpoint": FakeCombo(OR),
        "api_key": FakeCombo(""),
        "text_model": FakeCombo("openai/gpt-oss-120b:nitro"),
        "image_model": FakeCombo("google/gemini-3.1-flash-lite-image"),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])
    patch("plugin.chatbot.dialog_views.cached_image_models", side_effect=lambda ep, api_key_override=None: list(TG_IMAGES)).start()
    return listener, ctrls


def test_text_model_edit_survives_catalog_repaint_after_provider_switch():
    listener, ctrls = _setup()
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
        ctrls["endpoint"].setText(TG)
        ctrls["api_key"].setText("dummy-not-a-key")
        # First fill after the switch drops the OpenRouter ids (unchanged behavior).
        listener._apply_dropdowns(TG, models=None)
        assert ctrls["text_model"].text == "MiniMaxAI/MiniMax-M3"
        shown_image = ctrls["image_model"].text
        assert shown_image == "black-forest-labs/FLUX.2-dev"

        ctrls["text_model"].setText("Qwen/Qwen3-235B-A22B-Instruct-2507-tput")
        # Catalog lands / key debounce / Test Connection: same provider as last fill.
        listener._apply_dropdowns(TG, models=list(TG_TEXT))

    assert ctrls["text_model"].text == "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    assert ctrls["image_model"].text == shown_image
    assert "black-forest-labs/FLUX.1-schnell" in ctrls["image_model"].items


def test_switching_back_to_saved_provider_drops_ids_from_the_one_just_left():
    """Master compared against the saved provider, so OR -> Together -> OR kept the Together slug."""
    listener, ctrls = _setup()
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
        ctrls["endpoint"].setText(TG)
        ctrls["api_key"].setText("dummy-not-a-key")
        listener._apply_dropdowns(TG, models=list(TG_TEXT))
        ctrls["text_model"].setText("Qwen/Qwen3-235B-A22B-Instruct-2507-tput")
        # Back to OpenRouter: the Together slug must not be carried over.
        ctrls["endpoint"].setText(OR)
        listener._apply_dropdowns(OR, models=None)

    assert ctrls["text_model"].text != "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    assert ctrls["image_model"].text not in TG_IMAGES


def test_together_text_combo_lists_only_text_models():
    from plugin.framework.client.model_fetcher import (
        clear_settings_catalog_cache,
        fetch_available_models,
    )
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener
    from tests.framework.client.test_model_fetcher import TOGETHER_MODELS_BODY

    save({
        "endpoint": OR,
        "text_model": "openai/gpt-oss-120b:nitro",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    save({"api_keys_by_endpoint": {normalize_endpoint_url(TG): "dummy-not-a-key"}})
    ctrls = {
        "endpoint": FakeCombo(OR),
        "api_key": FakeCombo(""),
        "text_model": FakeCombo("openai/gpt-oss-120b:nitro"),
        "image_model": FakeCombo("google/gemini-3.1-flash-lite-image"),
        "audio__stt_model": FakeCombo(""),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])

    key = "dummy-not-a-key"
    try:
        with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
            ctrls["endpoint"].setText(TG)
            ctrls["api_key"].setText(key)
            key = listener._api_key_override()
            clear_settings_catalog_cache(TG, api_key_override=key)
            with patch("plugin.framework.client.requests.sync_request", return_value=TOGETHER_MODELS_BODY):
                ids = fetch_available_models(TG, api_key_override=key)
            assert ids is not None
            listener._apply_dropdowns(TG, models=ids)

        non_text_ids = {
            row["id"] for row in TOGETHER_MODELS_BODY
            if row["type"] not in ("chat", "language", "code")
        }
        assert not (set(ctrls["text_model"].items) & non_text_ids)
        assert "meta-llama/Llama-3.3-70B-Instruct-Turbo" in ctrls["text_model"].items
        assert "black-forest-labs/FLUX.1-schnell" in ctrls["image_model"].items
        assert "openai/whisper-large-v3" in ctrls["audio__stt_model"].items
    finally:
        clear_settings_catalog_cache(TG, api_key_override=key)


def test_half_typed_model_stays_in_box_and_out_of_list():
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    save({
        "endpoint": OR,
        "text_model": "openrouter/free",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    save({"api_keys_by_endpoint": {normalize_endpoint_url(OR): "sk-or-test"}})
    ctrls = {
        "endpoint": FakeCombo(OR),
        "api_key": FakeCombo("sk-or-test"),
        "text_model": FakeCombo("vendor/half-ty"),
        "image_model": FakeCombo("img/half-ty"),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])
    patch("plugin.chatbot.dialog_views.cached_image_models", return_value=["img/a"]).start()
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
        listener._apply_dropdowns(OR, models=["vendor/a"])
        assert ctrls["text_model"].text == "vendor/half-ty"
        assert ctrls["image_model"].text == "img/half-ty"
        assert "vendor/half-ty" not in ctrls["text_model"].items
        assert "img/half-ty" not in ctrls["image_model"].items
        assert "vendor/a" in ctrls["text_model"].items

        listener._apply_dropdowns(OR, models=["vendor/a"])
        assert ctrls["text_model"].text == "vendor/half-ty"
        assert ctrls["image_model"].text == "img/half-ty"
        assert "vendor/half-ty" not in ctrls["text_model"].items
        assert "img/half-ty" not in ctrls["image_model"].items


def test_placeholder_after_switch_does_not_bring_back_saved_provider_model():
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    save({
        "endpoint": OR,
        "text_model": "inception/mercury-2.5",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    save({"api_keys_by_endpoint": {normalize_endpoint_url(TG): ""}})
    ctrls = {
        "endpoint": FakeCombo(OR),
        "api_key": FakeCombo(""),
        "text_model": FakeCombo("inception/mercury-2.5"),
        "image_model": FakeCombo("google/gemini-3.1-flash-lite-image"),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])
    patch("plugin.chatbot.dialog_views.cached_image_models", return_value=list(TG_IMAGES)).start()
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
        ctrls["endpoint"].setText(TG)
        listener._apply_dropdowns(TG, models=None)
        assert "Enter API Key" in ctrls["text_model"].text

        ctrls["api_key"].setText("sk-tg-test")
        listener._apply_dropdowns(TG, models=list(TG_TEXT))
        assert ctrls["text_model"].text == "MiniMaxAI/MiniMax-M3"
        assert "inception/mercury-2.5" not in ctrls["text_model"].items


def test_reentering_key_restores_saved_model():
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    save({
        "endpoint": OR,
        "text_model": "openai/gpt-oss-120b:nitro",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    save({"api_keys_by_endpoint": {normalize_endpoint_url(OR): "sk-or-test"}})
    ctrls = {
        "endpoint": FakeCombo(OR),
        "api_key": FakeCombo("sk-or-test"),
        "text_model": FakeCombo("openai/gpt-oss-120b:nitro"),
        "image_model": FakeCombo("google/gemini-3.1-flash-lite-image"),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
        listener._apply_dropdowns(OR, models=["openai/gpt-oss-120b"])
        assert ctrls["text_model"].text == "openai/gpt-oss-120b:nitro"

        ctrls["api_key"].setText("")
        listener._apply_dropdowns(OR, models=None)
        assert "Enter API Key" in ctrls["text_model"].text

        ctrls["api_key"].setText("sk-or-test")
        listener._apply_dropdowns(OR, models=["openai/gpt-oss-120b"])
        assert ctrls["text_model"].text == "openai/gpt-oss-120b:nitro"


def test_untyped_tts_id_takes_catalog_casing():
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    save({
        "endpoint": OR,
        "audio.tts_model": "hexgrad/Kokoro-82M",
    })
    save({"api_keys_by_endpoint": {normalize_endpoint_url(OR): "sk-or-test"}})
    ctrls = {
        "endpoint": FakeCombo(OR),
        "api_key": FakeCombo("sk-or-test"),
        "text_model": FakeCombo("openrouter/free"),
        "audio__tts_model": FakeCombo("hexgrad/Kokoro-82M"),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)), \
         patch("plugin.chatbot.dialog_views.cached_tts_models", return_value=["hexgrad/kokoro-82m"]), \
         patch("plugin.chatbot.dialog_views.cached_image_models", return_value=[]):
        listener._apply_dropdowns(OR, models=["openrouter/free"])
    assert ctrls["audio__tts_model"].text == "hexgrad/kokoro-82m"


def test_custom_url_keeps_edited_text_shows_saved_on_first_fill_and_resets_on_switch():
    from plugin.framework.config import save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    custom_a = "http://gpu.lan:8000/v1"
    custom_b = "http://gpu2.lan:8000/v1"
    save({
        "endpoint": custom_a,
        "text_model": "custom/saved-model",
    })
    ctrls = {
        "endpoint": FakeCombo(custom_a),
        "api_key": FakeCombo(""),
        "text_model": FakeCombo("custom/saved-model"),
    }
    listener = EndpointCombinedListener(MagicMock(), MagicMock(), ctrls["endpoint"])
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)), \
         patch("plugin.chatbot.dialog_views.cached_image_models", return_value=[]):
        # First fill shows the saved model, not LRU head.
        listener._apply_dropdowns(custom_a, models=["custom/other", "custom/saved-model"])
        assert ctrls["text_model"].text == "custom/saved-model"

        # Edited text survives a repaint on the same custom URL.
        ctrls["text_model"].setText("custom/half-ty")
        listener._apply_dropdowns(custom_a, models=["custom/other", "custom/saved-model"])
        assert ctrls["text_model"].text == "custom/half-ty"

        # Switching to a different custom URL drops the old text.
        ctrls["endpoint"].setText(custom_b)
        listener._apply_dropdowns(custom_b, models=["custom/b-model"])
        assert ctrls["text_model"].text != "custom/half-ty"
        assert "custom/b-model" in ctrls["text_model"].items
