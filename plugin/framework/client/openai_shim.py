# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenAI-compatible provider shims and registry lookup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.url_utils import get_url_path_and_query
from .base_provider_shim import BaseProviderShim


class OpenAIShim(BaseProviderShim):
    """Shim for standard OpenAI-compatible providers."""


class OllamaShim(BaseProviderShim):
    """Shim for Ollama specifically (handles native /api image endpoints if needed)."""

    def build_image_request(
        self,
        prompt: str,
        model: str | None,
        width: int,
        height: int,
        steps: int | None = None,
        source_image: str | None = None,
        image_url: str | None = None,
    ) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        url = f"{endpoint}/api/generate"
        eff_model = model or "flux"

        data = {"model": eff_model, "prompt": prompt, "stream": False}
        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        images = response_data.get("images")
        if images and isinstance(images, list):
            return images
        if img := response_data.get("image"):
            return [img]
        if "data" in response_data:
            return super().parse_image_responses(response_data)
        return []


class OpenRouterShim(BaseProviderShim):
    """Shim for OpenRouter specifically (handles dedicated /images endpoint)."""

    def build_image_request(
        self,
        prompt: str,
        model: str | None,
        width: int,
        height: int,
        steps: int | None = None,
        source_image: str | None = None,
        image_url: str | None = None,
    ) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images"
        # What was wrong: output_format was hardcoded to webp. Models such as
        # black-forest-labs/flux.2-klein-4b only accept png/jpeg and return HTTP 400.
        # png is the Images API default and is accepted by webp-capable models too.
        data: dict[str, Any] = {"prompt": prompt, "model": model, "n": 1, "output_format": "png"}
        if width and height:
            # Explicit pixel size is authoritative. Sending inferred aspect_ratio
            # alongside size is rejected with HTTP 400 when OpenRouter considers
            # them mismatched (retries then sent other ratios). Size already
            # encodes the intended dimensions.
            data["size"] = f"{width}x{height}"

        if image_url:
            data["image_url"] = image_url
        elif source_image:
            if source_image.startswith("data:image"):
                data["image_url"] = source_image
            else:
                data["image_url"] = "data:image/png;base64," + source_image

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()


def _load_anthropic() -> type[BaseProviderShim]:
    from .anthropic_shim import AnthropicShim

    return AnthropicShim


def _load_grok() -> type[BaseProviderShim]:
    from .grok_shim import GrokShim

    return GrokShim


def _load_google() -> type[BaseProviderShim]:
    from .google_shim import GoogleShim

    return GoogleShim


_SHIM_REGISTRY: dict[str, Callable[[], type[BaseProviderShim]]] = {
    "anthropic": _load_anthropic,
    "google": _load_google,
    "xai": _load_grok,
    "grok": _load_grok,
    "ollama": lambda: OllamaShim,
    "openrouter": lambda: OpenRouterShim,
}


def get_provider_shim_class(provider: str, endpoint: str | None = None) -> type[BaseProviderShim]:
    """Return the provider shim class matching the provider name, defaulting to OpenAIShim.

    Standard OpenAI-compatible providers (DeepSeek, Mistral, Cerebras, Groq, NVIDIA NIM, Z.ai)
    route to OpenAIShim by default. Google routes to GoogleShim (which inherits OpenAIShim for chat/tools
    and implements native REST for image generation).
    """
    loader = _SHIM_REGISTRY.get(provider)
    return loader() if loader else OpenAIShim
