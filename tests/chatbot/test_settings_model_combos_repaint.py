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
    from plugin.framework.config import set_api_key_for_endpoint, save
    from plugin.chatbot.dialog_views import EndpointCombinedListener

    save({
        "endpoint": OR,
        "text_model": "openai/gpt-oss-120b:nitro",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    set_api_key_for_endpoint(TG, "dummy-not-a-key")
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
        listener._apply_dropdowns(TG, models=None, skip_fetch=True)
        assert ctrls["text_model"].text == "MiniMaxAI/MiniMax-M3"
        shown_image = ctrls["image_model"].text
        assert shown_image == "black-forest-labs/FLUX.2-dev"

        ctrls["text_model"].setText("Qwen/Qwen3-235B-A22B-Instruct-2507-tput")
        # Catalog lands / key debounce / Test Connection: same provider as last fill.
        listener._apply_dropdowns(TG, models=list(TG_TEXT), skip_fetch=False)

    assert ctrls["text_model"].text == "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    assert ctrls["image_model"].text == shown_image
    assert "black-forest-labs/FLUX.1-schnell" in ctrls["image_model"].items


def test_switching_back_to_saved_provider_drops_ids_from_the_one_just_left():
    """Master compared against the saved provider, so OR -> Together -> OR kept the Together slug."""
    listener, ctrls = _setup()
    with patch("plugin.chatbot.dialog_views.get_optional", side_effect=lambda d, n: ctrls.get(n)):
        ctrls["endpoint"].setText(TG)
        ctrls["api_key"].setText("dummy-not-a-key")
        listener._apply_dropdowns(TG, models=list(TG_TEXT), skip_fetch=False)
        ctrls["text_model"].setText("Qwen/Qwen3-235B-A22B-Instruct-2507-tput")
        # Back to OpenRouter: the Together slug must not be carried over.
        ctrls["endpoint"].setText(OR)
        listener._apply_dropdowns(OR, models=None, skip_fetch=True)

    assert ctrls["text_model"].text != "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
    assert ctrls["image_model"].text not in TG_IMAGES


def test_together_text_combo_lists_only_text_models():
    from plugin.framework.client.model_fetcher import (
        clear_settings_catalog_cache,
        fetch_available_models,
    )
    from plugin.framework.config import save, set_api_key_for_endpoint
    from plugin.chatbot.dialog_views import EndpointCombinedListener
    from tests.framework.client.test_model_fetcher import TOGETHER_MODELS_BODY

    save({
        "endpoint": OR,
        "text_model": "openai/gpt-oss-120b:nitro",
        "image_model": "google/gemini-3.1-flash-lite-image",
    })
    set_api_key_for_endpoint(TG, "dummy-not-a-key")
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
            listener._apply_dropdowns(TG, models=ids, skip_fetch=False)

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
