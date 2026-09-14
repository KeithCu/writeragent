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
from .base_provider_shim import BaseProviderShim, canonical_aspect_ratio, coerce_image_data_url, coerce_raw_b64


class OpenAIShim(BaseProviderShim):
    """Shim for standard OpenAI-compatible providers.

    Official ``api.openai.com`` image edit is ``POST /v1/images/edits`` JSON
    ``images[].image_url``, not a top-level ``image_url`` on generations
    (https://developers.openai.com/api/reference/resources/images/methods/edit).
    Other hosts keep the generic OpenAI-compat body in ``BaseProviderShim``.
    """

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
        if self.client._get_provider() != "openai":
            return super().build_image_request(
                prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url
            )

        ref = coerce_image_data_url(image_url, source_image)
        # dall-e-3 is generations-only. A prompt-only create while a graphic is
        # selected would replace it with a new image (same silent miss as
        # OpenRouter's old image_url / Imagen :predict).
        if ref and model and str(model).lower().startswith("dall-e-3"):
            raise ValueError(
                "dall-e-3 cannot edit an existing image. Pick a GPT Image model or dall-e-2."
            )

        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + ("/images/edits" if ref else "/images/generations")
        data: dict[str, Any] = {
            "prompt": prompt,
            "n": 1,
            "size": f"{width}x{height}",
            "response_format": "b64_json",
        }
        if model:
            data["model"] = model
        if ref:
            data["images"] = [{"image_url": ref}]
        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()


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
            # What was wrong: we sent explicit pixel size (512x512) and omitted
            # aspect_ratio. OpenRouter treats size as authoritative and 400s a
            # paired aspect_ratio it considers mismatched, so Flux kept working
            # with size-only. Gemini image models (and the Image API catalog for
            # gemini-*-flash-lite-image) ignore pixel size and honor aspect_ratio
            # / resolution instead, so Square still came back 4:3.
            # Hint with aspect_ratio when WxH maps to a standard ratio; do not
            # also send size (HTTP 400). Fall back to size for odd dimensions.
            ratio = canonical_aspect_ratio(width, height)
            if ratio:
                data["aspect_ratio"] = ratio
            else:
                data["size"] = f"{width}x{height}"

        # What was wrong: img2img sent a top-level image_url. OpenRouter's
        # /api/v1/images API ignores that field (HTTP 200, prompt_tokens stay
        # text-only), so a selected graphic was never used and Flux generated
        # a new image from the prompt. The documented field is input_references
        # (https://openrouter.ai/docs/guides/overview/multimodal/image-generation);
        # flux.2-klein-4b advertises 0–4 references in supported_parameters.
        ref = coerce_image_data_url(image_url, source_image)
        if ref:
            data["input_references"] = [{"type": "image_url", "image_url": {"url": ref}}]

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()


class TogetherShim(OpenAIShim):
    """Together Images API: Kontext uses image_url; other models use reference_images."""

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
        method, path, body, headers = super().build_image_request(
            prompt, model, width, height, steps=steps, source_image=source_image, image_url=image_url
        )
        # What was wrong: the OpenAI-compat default sent top-level image_url.
        # Together's default image model (google/flash-image-2.5) only accepts
        # reference_images[]; image_url is ignored or rejected — same silent
        # create-instead-of-edit as OpenRouter's old image_url field.
        # https://docs.together.ai/docs/inference/images/reference-images
        ref = coerce_image_data_url(image_url, source_image)
        if not ref:
            return method, path, body, headers
        data = json.loads(body.decode("utf-8"))
        data.pop("image_url", None)
        if model and "kontext" in model.lower():
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


_SHIM_REGISTRY: dict[str, Callable[[], type[BaseProviderShim]]] = {
    "anthropic": _load_anthropic,
    "google": _load_google,
    "xai": _load_grok,
    "grok": _load_grok,
    "ollama": lambda: OllamaShim,
    "openrouter": lambda: OpenRouterShim,
    "together": lambda: TogetherShim,
}


def get_provider_shim_class(provider: str, endpoint: str | None = None) -> type[BaseProviderShim]:
    """Return the provider shim class matching the provider name, defaulting to OpenAIShim.

    Standard OpenAI-compatible providers (DeepSeek, Mistral, Cerebras, Groq, NVIDIA NIM, Z.ai)
    route to OpenAIShim by default. Google routes to GoogleShim (which inherits OpenAIShim for chat/tools
    and implements native REST for image generation).
    """
    loader = _SHIM_REGISTRY.get(provider)
    return loader() if loader else OpenAIShim
