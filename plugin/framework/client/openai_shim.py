# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenAI-compatible provider shims and registry lookup."""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import NetworkError
from .anthropic_shim import AnthropicShim
from .base_provider_shim import BaseProviderShim, canonical_aspect_ratio, canonical_resolution, coerce_image_data_url, coerce_raw_b64, effort_payload
from .google_shim import GoogleShim
from .grok_shim import GrokShim
from .responses_shim import (
    is_responses_body,
    parse_responses_chunk,
    parse_responses_sync,
    responses_failed_message,
    responses_payload,
    uses_responses_api,
)

OPENROUTER_USE_RESPONSES = False

# api.openai.com o-series and gpt-5/6 reject max_tokens. Dated snapshots
# (o3-mini-2025-01-31, gpt-5-2025-08-07) and ft:gpt-5-... share the family prefix.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")
# Chat Completions default. These models reject every other temperature.
_OPENAI_DEFAULT_TEMPERATURE = 1


def _openai_reasoning_model(model_name: str | None) -> bool:
    """True for OpenAI model families that reject ``max_tokens``."""
    name = str(model_name or "").strip().lower()
    if not name:
        return False
    # Fine-tune ids are ft:<base>:<org>:<suffix>:<id>. Splitting on '/' first
    # turned ft:gpt-5-mini:org/team:name:id into team:name:id, so the gpt-5
    # base kept max_tokens and api.openai.com returned 400. The base is the
    # field after ft:, including when a provider prefix sits in front
    # (openai/ft:...). A '/' inside that base (ft:openai/gpt-5-mini:...) is
    # stripped afterwards.
    # https://developers.openai.com/api/reference/resources/fine_tuning/subresources/jobs/methods/create
    ft_at = name.find("ft:")
    if ft_at != -1 and (ft_at == 0 or name[ft_at - 1] in "/:"):
        base = name[ft_at + 3 :].split(":", 1)[0]
        if base:
            name = base
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    # ChatGPT snapshots (gpt-5-chat-latest, gpt-5.1-chat-latest,
    # gpt-5.2-chat-latest, gpt-6-chat-latest) accept max_tokens and a non-default
    # temperature. Their model pages do not list reasoning-token support; gpt-5.1 does.
    # startswith("gpt-5-chat") missed gpt-5.1-chat-latest, so that id was
    # rewritten and the user's temperature was dropped.
    # https://developers.openai.com/api/docs/models/gpt-5.1-chat-latest
    if name.startswith(("gpt-5", "gpt-6")) and "-chat" in name:
        return False
    return name.startswith(_OPENAI_REASONING_PREFIXES)


def _pick_size(width: int, height: int, landscape: str, portrait: str, square: str) -> str:
    """Bucket WxH into one of three vendor sizes.

    ``width=0`` used to divide as 0 and pick the portrait size. ``height=0``
    already became square. Both axes must be positive or the ratio is treated
    as square. ``image_generate`` can pass 0 after ``(n // 64) * 64``.
    """
    ratio = (width / height) if width and height else 1.0
    if ratio > 1.2:
        return landscape
    if ratio < 0.8:
        return portrait
    return square


def _without_reasoning_details(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions body must not carry reasoning_details.

    This PR is what writes that key onto openai session messages. api.openai.com
    Chat Completions rejects the OpenRouter field. The Responses branch reads
    the key itself, so only the chat branch strips it. Copy the dict; do not
    mutate the list make_chat_request passed in. Leave reasoning and
    reasoning_content alone. Those string keys predate this PR.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict) and "reasoning_details" in message:
            message = {key: value for key, value in message.items() if key != "reasoning_details"}
        out.append(message)
    return out


def _responses_request(
    shim: BaseProviderShim, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None
) -> tuple[str, str, bytes, dict[str, str]]:
    data = responses_payload(
        messages, max_tokens, temperature, tools, stream, model_name, response_format,
        endpoint_effort=shim._endpoint_reasoning_effort(),
        chat_extra=chat_extra if isinstance(chat_extra, dict) else None,
    )
    # If the effort-mapping function exists on HEAD, map data["reasoning"]["effort"]
    # here and nowhere else. If it does not exist, leave the string.
    if "reasoning" in data and isinstance(data["reasoning"], dict):
        effort = data["reasoning"].get("effort")
        if effort:
            fragment = effort_payload(shim._reasoning_provider(), effort)
            val = fragment.get("reasoning_effort") or (fragment.get("reasoning") or {}).get("effort")
            if val:
                data["reasoning"]["effort"] = val
    return shim._post(shim._url("/responses"), data)


def _parse_responses_event(
    chunk: dict[str, Any], stream_state: dict[str, Any] | None
) -> tuple[str, str | None, str | None, dict[str, Any]] | None:
    event_type = chunk.get("type")
    if not (isinstance(event_type, str) and event_type.startswith("response.")):
        return None
    if event_type == "response.failed":
        raise NetworkError(responses_failed_message(chunk), code="STREAM_ERROR")
    return parse_responses_chunk(chunk, stream_state, model_name=None)


class OpenAIShim(BaseProviderShim):
    """Official ``api.openai.com`` chat and Images API.

    Registered only for provider ``openai``. Other hosts use
    ``BaseProviderShim`` or their own shim.

    Image edit is ``POST /v1/images/edits`` JSON ``images[].image_url``
    (https://developers.openai.com/api/reference/resources/images/methods/edit).
    GPT Image models reject ``response_format``. ``dall-e-*`` create keeps the
    legacy body; ``dall-e-*`` edit raises.
    """

    _effort_provider: str | None = "openai"

    def _chat_payload(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = super()._chat_payload(messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)
        # api.openai.com o1/o3/o4 and gpt-5 reject max_tokens and any
        # temperature other than the default (1), which is HTTP 400. This
        # class is only constructed for provider openai; Groq, OpenRouter,
        # and every other host use a different shim and keep max_tokens.
        # The documented replacement is max_completion_tokens.
        if not _openai_reasoning_model(model_name):
            return data
        if "max_tokens" in data:
            data["max_completion_tokens"] = data.pop("max_tokens")
        temp = data.get("temperature")
        if temp is not None and temp != _OPENAI_DEFAULT_TEMPERATURE:
            data.pop("temperature", None)
        return data

    def build_chat_request(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> tuple[str, str, bytes, dict[str, str]]:
        if uses_responses_api(model_name, tools):
            return _responses_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)
        return super().build_chat_request(
            _without_reasoning_details(messages), max_tokens, temperature, tools, stream,
            model_name, response_format, chat_extra,
        )

    def parse_response_chunk(
        self, chunk: dict[str, Any], stream_state: dict[str, Any] | None = None
    ) -> tuple[str, str | None, str | None, dict[str, Any]]:
        parsed = _parse_responses_event(chunk, stream_state)
        if parsed is not None:
            return parsed
        return super().parse_response_chunk(chunk, stream_state)

    def parse_sync_response(
        self, response_data: dict[str, Any]
    ) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        if isinstance(response_data, dict) and is_responses_body(response_data):
            return parse_responses_sync(response_data)
        return super().parse_sync_response(response_data)

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        # ``steps`` is on the shared signature. The OpenAI Images API has no steps field.
        ref = coerce_image_data_url(image_url, source_image)
        lower_model = str(model).lower() if model else ""
        # dall-e-2 and dall-e-3 were retired on 2026-05-12. dall-e-3 never
        # edited. dall-e-2 edits were multipart image=@file, which this client
        # does not send. The old error told the user to pick dall-e-2, and a
        # missing model kept response_format on /images/edits. GPT Image
        # models (the documented default when model is omitted is
        # gpt-image-2.5-sunburst) reject that field with HTTP 400.
        # https://developers.openai.com/api/reference/resources/images/methods/edit
        # https://developers.openai.com/api/reference/python/resources/images/methods/generate/
        if ref and lower_model.startswith("dall-e"):
            raise ValueError(f"{model} cannot edit an existing image. Pick a GPT Image model (gpt-image-… or chatgpt-image-latest).")

        url = self._url("/images/edits" if ref else "/images/generations")
        data: dict[str, Any] = {"prompt": prompt, "n": 1}
        if model:
            data["model"] = model

        if lower_model.startswith("dall-e-3"):
            # Legacy create body. Proxies that still serve the retired id
            # accept response_format and these three sizes.
            data["response_format"] = "b64_json"
            data["size"] = _pick_size(width, height, "1792x1024", "1024x1792", "1024x1024")
        elif lower_model.startswith("dall-e"):
            data["response_format"] = "b64_json"
            edge = max(width, height)
            if edge <= 256:
                data["size"] = "256x256"
            elif edge <= 512:
                data["size"] = "512x512"
            else:
                data["size"] = "1024x1024"
        else:
            # gpt-image*, chatgpt-image-latest, and an omitted or unknown name.
            # Those models always return b64_json and 400 on response_format.
            # The three sizes below are valid for every GPT Image model,
            # including gpt-image-1, which rejects arbitrary WxH.
            data["size"] = _pick_size(width, height, "1536x1024", "1024x1536", "1024x1024")

        if ref:
            data["images"] = [{"image_url": ref}]
        return self._post(url, data)


class OllamaShim(BaseProviderShim):
    """Shim for Ollama specifically (handles native /api image endpoints if needed)."""

    _effort_provider: str | None = "ollama"

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        # ``steps`` is on the shared signature. Ollama /api/generate has no steps field.
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
        return self._post(url, data)

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        # A non-dict body, or a list entry that is not a string, used to be
        # returned as-is. The caller base64-decodes each item and then raises.
        if not isinstance(response_data, dict):
            return []
        images = response_data.get("images")
        if isinstance(images, list):
            out = [item for item in images if isinstance(item, str) and item]
            if out:
                return out
        img = response_data.get("image")
        if isinstance(img, str) and img:
            return [img]
        if "data" in response_data:
            return super().parse_image_responses(response_data)
        return []


class OpenRouterShim(BaseProviderShim):
    """Shim for OpenRouter specifically (handles dedicated /images endpoint)."""

    _effort_provider: str | None = "openrouter"

    def build_chat_request(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> tuple[str, str, bytes, dict[str, str]]:
        if OPENROUTER_USE_RESPONSES and str(model_name or "").strip().lower().startswith("openai/") and uses_responses_api(model_name, tools):
            return _responses_request(self, messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)
        return super().build_chat_request(messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)

    def parse_response_chunk(
        self, chunk: dict[str, Any], stream_state: dict[str, Any] | None = None
    ) -> tuple[str, str | None, str | None, dict[str, Any]]:
        parsed = _parse_responses_event(chunk, stream_state) if OPENROUTER_USE_RESPONSES else None
        if parsed is not None:
            return parsed
        return super().parse_response_chunk(chunk, stream_state)

    def parse_sync_response(
        self, response_data: dict[str, Any]
    ) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        if OPENROUTER_USE_RESPONSES and isinstance(response_data, dict) and is_responses_body(response_data):
            return parse_responses_sync(response_data)
        return super().parse_sync_response(response_data)

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        # ``steps`` is on the shared signature. OpenRouter /images has no steps field.
        url = self._url("/images")
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

        return self._post(url, data)


class TogetherShim(BaseProviderShim):
    """Together Images API: Kontext uses image_url; other models use reference_images."""

    _effort_provider: str | None = "together"

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        url = self._url("/images/generations")
        # Start from the generic generations dict. Decoding super()'s bytes
        # just to edit two fields encoded the body twice.
        data = self._image_payload(prompt, model, width, height, steps, source_image, image_url)
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
        else:
            if width:
                data["width"] = width
            if height:
                data["height"] = height
        # Together's default image model (black-forest-labs/FLUX.2-dev) and
        # other non-Kontext models only accept reference_images[]. A top-level
        # image_url is ignored or rejected — the same silent create-instead-of-edit
        # as OpenRouter's old image_url field. Kontext keeps the image_url
        # ``_image_payload`` already set.
        # https://docs.together.ai/docs/inference/images/reference-images
        if not is_kontext and data.get("image_url"):
            data["reference_images"] = [data.pop("image_url")]
        return self._post(url, data)


# ``grok`` duplicated ``xai``. Detection returns ``xai``; nothing reads ``grok``.
# ``openai`` is explicit so unknown hosts stay on the generic base body.
_SHIM_REGISTRY: dict[str, type[BaseProviderShim]] = {
    "anthropic": AnthropicShim,
    "google": GoogleShim,
    "openai": OpenAIShim,
    "xai": GrokShim,
    "ollama": OllamaShim,
    "openrouter": OpenRouterShim,
    "together": TogetherShim,
}


def get_provider_shim_class(provider: str) -> type[BaseProviderShim]:
    """Return the shim class for ``provider``.

    Unknown OpenAI-compatible hosts (DeepSeek, Mistral, Cerebras, Groq,
    NVIDIA NIM, Z.ai, custom) use ``BaseProviderShim``. ``openai`` is
    ``OpenAIShim``. Google, xAI, and Together subclass the base directly.
    """
    return _SHIM_REGISTRY.get(provider, BaseProviderShim)
