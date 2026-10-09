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
from .base_provider_shim import BaseProviderShim, canonical_aspect_ratio, canonical_resolution, coerce_image_data_url, coerce_raw_b64

# api.openai.com o-series and gpt-5 reject max_tokens. Dated snapshots
# (o3-mini-2025-01-31, gpt-5-2025-08-07) and ft:gpt-5-... share the family prefix.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
# Chat Completions default. These models reject every other temperature.
_OPENAI_DEFAULT_TEMPERATURE = 1


def _openai_reasoning_model(model_name: str | None) -> bool:
    """True for OpenAI model families that reject ``max_tokens``."""
    name = str(model_name or "").strip().lower()
    if not name:
        return False
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("ft:"):
        parts = name.split(":")
        if len(parts) > 1 and parts[1]:
            name = parts[1]
    return name.startswith(_OPENAI_REASONING_PREFIXES)


class OpenAIShim(BaseProviderShim):
    """Shim for standard OpenAI-compatible providers.

    Official ``api.openai.com`` image edit is ``POST /v1/images/edits`` JSON
    ``images[].image_url``, not a top-level ``image_url`` on generations
    (https://developers.openai.com/api/reference/resources/images/methods/edit).
    Other hosts keep the generic OpenAI-compat body in ``BaseProviderShim``.
    """

    def build_chat_request(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> tuple[str, str, bytes, dict[str, str]]:
        method, path, body, headers = super().build_chat_request(messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)
        # api.openai.com o1/o3/o4 and gpt-5 reject max_tokens and any
        # temperature other than the default (1), which is HTTP 400. Only
        # provider openai and those families; Groq, OpenRouter, and every
        # other host keep max_tokens. The documented replacement is
        # max_completion_tokens.
        if self.client._get_provider() != "openai" or not _openai_reasoning_model(model_name):
            return method, path, body, headers
        data = json.loads(body.decode("utf-8"))
        if "max_tokens" in data:
            data["max_completion_tokens"] = data.pop("max_tokens")
        temp = data.get("temperature")
        if temp is not None and temp != _OPENAI_DEFAULT_TEMPERATURE:
            data.pop("temperature", None)
        return method, path, json.dumps(data).encode("utf-8"), headers

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        if self.client._get_provider() != "openai":
            return super().build_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)

        ref = coerce_image_data_url(image_url, source_image)
        # dall-e-3 is generations-only. A prompt-only create while a graphic is
        # selected would replace it with a new image (same silent miss as
        # OpenRouter's old image_url / Imagen :predict).
        if ref and model and str(model).lower().startswith("dall-e-3"):
            raise ValueError("dall-e-3 cannot edit an existing image. Pick a GPT Image model or dall-e-2.")

        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + ("/images/edits" if ref else "/images/generations")
        data: dict[str, Any] = {"prompt": prompt, "n": 1, "size": f"{width}x{height}", "response_format": "b64_json"}
        if model:
            data["model"] = model

        lower_model = str(model).lower() if model else ""
        if lower_model.startswith("gpt-image"):
            data.pop("response_format", None)
            ratio = width / height if height else 1.0
            if ratio > 1.2:
                data["size"] = "1536x1024"
            elif ratio < 0.8:
                data["size"] = "1024x1536"
            else:
                data["size"] = "1024x1024"
        elif lower_model.startswith("dall-e-3"):
            ratio = width / height if height else 1.0
            if ratio > 1.2:
                data["size"] = "1792x1024"
            elif ratio < 0.8:
                data["size"] = "1024x1792"
            else:
                data["size"] = "1024x1024"
        elif lower_model.startswith("dall-e"):
            edge = max(width, height)
            if edge <= 256:
                data["size"] = "256x256"
            elif edge <= 512:
                data["size"] = "512x512"
            else:
                data["size"] = "1024x1024"

        if ref:
            data["images"] = [{"image_url": ref}]
        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()


class OllamaShim(BaseProviderShim):
    """Shim for Ollama specifically (handles native /api image endpoints if needed)."""

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        url = f"{endpoint}/api/generate"
        eff_model = model or "flux"

        data: dict[str, Any] = {"model": eff_model, "prompt": prompt, "stream": False}
        if width:
            data["width"] = width
        if height:
            data["height"] = height
        # Ollama img2img is the generate endpoint's images[] of raw base64
        # (https://docs.ollama.com/api/generate). Top-level image_url is ignored.
        raw = coerce_raw_b64(image_url, source_image)
        if raw:
            data["images"] = [raw]
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

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images"
        # png is the Images API default and is accepted by models that reject
        # webp (black-forest-labs/flux.2-klein-4b returns HTTP 400 for webp).
        # Include "model" only when set; OpenRouter's /images endpoint rejects
        # "model": null with HTTP 400.
        data: dict[str, Any] = {"prompt": prompt, "n": 1, "output_format": "png"}
        if model:
            data["model"] = model
        if width and height:
            # OpenRouter treats size as authoritative and 400s a paired
            # aspect_ratio it considers mismatched, so Flux works with size
            # alone. Gemini image models ignore pixel size and honor
            # aspect_ratio / resolution, so Square still comes back 4:3 when
            # only 512x512 is sent. Hint with aspect_ratio when WxH maps to a
            # standard ratio; do not also send size (HTTP 400). Fall back to
            # size for odd dimensions.
            ratio = canonical_aspect_ratio(width, height)
            if ratio:
                data["aspect_ratio"] = ratio
                # Gemini-family models ignore pixel size and honor resolution
                # tiers (512 / 1K / 2K / 4K). Pair with aspect_ratio — do not
                # also send size (HTTP 400).
                res = canonical_resolution(width, height)
                if res:
                    data["resolution"] = res
            else:
                data["size"] = f"{width}x{height}"

        # OpenRouter's /api/v1/images ignores a top-level image_url (HTTP 200,
        # prompt_tokens stay text-only), so a selected graphic is never used
        # and Flux generates a new image from the prompt. The documented field
        # is input_references
        # (https://openrouter.ai/docs/guides/overview/multimodal/image-generation);
        # flux.2-klein-4b advertises 0–4 references in supported_parameters.
        ref = coerce_image_data_url(image_url, source_image)
        if ref:
            data["input_references"] = [{"type": "image_url", "image_url": {"url": ref}}]

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()


class TogetherShim(OpenAIShim):
    """Together Images API: Kontext uses image_url; other models use reference_images."""

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        method, path, body, headers = super().build_image_request(prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url)
        data = json.loads(body.decode("utf-8"))
        # Together documents width/height integers (Flash Image, FLUX.2) or
        # aspect_ratio (Kontext), not OpenAI size="WxH". Unknown size is
        # ignored, so Square/16:9 never reach the model.
        # https://docs.together.ai/docs/inference/images/parameters
        data.pop("size", None)
        is_kontext = bool(model and "kontext" in model.lower())
        if is_kontext:
            ratio = canonical_aspect_ratio(width, height)
            if ratio:
                data["aspect_ratio"] = ratio
            data.pop("width", None)
            data.pop("height", None)
        else:
            if width:
                data["width"] = width
            if height:
                data["height"] = height
        # Together's default image model (black-forest-labs/FLUX.2-dev) and
        # other non-Kontext models only accept reference_images[]. A top-level
        # image_url is ignored or rejected — the same silent create-instead-of-edit
        # as OpenRouter's old image_url field. Kontext uses image_url.
        # https://docs.together.ai/docs/inference/images/reference-images
        ref = coerce_image_data_url(image_url, source_image)
        if ref:
            data.pop("image_url", None)
            if is_kontext:
                data["image_url"] = ref
            else:
                data["reference_images"] = [ref]
        return method, path, json.dumps(data).encode("utf-8"), headers


def _load_anthropic() -> type[BaseProviderShim]:
    from .anthropic_shim import AnthropicShim

    return AnthropicShim


def _load_grok() -> type[BaseProviderShim]:
    from .grok_shim import GrokShim

    return GrokShim


def _load_google() -> type[BaseProviderShim]:
    from .google_shim import GoogleShim

    return GoogleShim


# ``grok`` duplicated ``xai``. Detection returns ``xai``; nothing reads ``grok``.
_SHIM_REGISTRY: dict[str, Callable[[], type[BaseProviderShim]]] = {"anthropic": _load_anthropic, "google": _load_google, "xai": _load_grok, "ollama": lambda: OllamaShim, "openrouter": lambda: OpenRouterShim, "together": lambda: TogetherShim}


def get_provider_shim_class(provider: str) -> type[BaseProviderShim]:
    """Return the provider shim class matching the provider name, defaulting to OpenAIShim.

    Standard OpenAI-compatible providers (DeepSeek, Mistral, Cerebras, Groq, NVIDIA NIM, Z.ai)
    route to OpenAIShim by default. Google routes to GoogleShim (which inherits OpenAIShim for chat/tools
    and implements native REST for image generation).
    """
    loader = _SHIM_REGISTRY.get(provider)
    return loader() if loader else OpenAIShim
