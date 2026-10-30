# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Base provider shim interface with default OpenAI-compatible API implementation."""

from __future__ import annotations

import copy
import json
from typing import Any

from plugin.framework.errors import NetworkError
from plugin.framework.i18n import _
from plugin.framework.url_utils import get_url_path_and_query

# Keys WriterAgent builds; openrouter_chat_extra must not replace these.
# Lives here so chat payload assembly does not import llm_client (that
# module imports this package's registry at load time).
OPENROUTER_CHAT_EXTRA_BLOCKLIST: frozenset[str] = frozenset({"messages", "tools", "tool_choice", "stream"})

OUR_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
# Grammar chat_extra only. Not a sidebar value. Means "lowest documented value".
LOWEST_SENTINEL = "minimal"


def _bare_model_id(model: str | None) -> str:
    name = str(model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name


def _sendable_efforts(provider: str | None, model: str | None) -> tuple[str, ...] | None:
    """Ascending values this provider/model may be sent. None = send no field."""
    prov = str(provider or "").strip().lower()
    if prov == "openrouter":
        return ("minimal", "low", "medium", "high", "xhigh", "max")

    if prov == "openai":
        from .openai_shim import _openai_model_base, _openai_reasoning_model

        if not _openai_reasoning_model(model):
            return None
        base = _openai_model_base(model)
        if base.startswith(("gpt-6", "gpt-5.6")):
            return ("minimal", "low", "medium", "high", "xhigh", "max")
        if base.startswith("gpt-5.5"):
            return ("minimal", "low", "medium", "high", "xhigh")
        return ("minimal", "low", "medium", "high")

    if prov == "anthropic":
        bare = _bare_model_id(model)
        if bare.startswith(("claude-opus-4-6", "claude-sonnet-4-6")):
            return ("low", "medium", "high", "max")
        if "mythos" in bare and "preview" in bare:
            return ("low", "medium", "high", "max")
        if bare.startswith("claude-opus-4-5"):
            return ("low", "medium", "high")
        if bare.startswith(
            (
                "claude-opus-4-7",
                "claude-opus-4-8",
                "claude-opus-5",
                "claude-sonnet-5",
                "claude-haiku-5",
                "claude-fable",
                "claude-mythos",
            )
        ):
            return OUR_EFFORTS
        return None

    if prov == "google":
        return ("low", "medium", "high")

    if prov == "xai":
        return ("low", "medium", "high", "xhigh")

    if prov == "deepseek":
        return OUR_EFFORTS

    if prov == "mistral":
        bare = _bare_model_id(model)
        if bare.startswith("mistral-large-4"):
            return ("low", "medium", "high")
        if bare.startswith(("mistral-small-2603", "mistral-medium-3-5")):
            return ("none", "high")
        if bare.startswith("zai-glm"):
            return ("low", "high", "max")
        return None

    if prov == "groq":
        bare = _bare_model_id(model)
        if "muse-spark" in bare:
            return ("low", "medium", "high", "xhigh")
        if "gpt-oss" in bare or "qwen3" in bare:
            return ("low", "medium", "high")
        return None

    if prov == "ollama":
        return OUR_EFFORTS

    # lmstudio, together, cerebras, perplexity, zai, nvidia, custom, and any unrecognized key
    return ("low", "medium", "high")


def _place_effort(provider: str | None, value: str) -> dict[str, Any]:
    prov = str(provider or "").strip().lower()
    if prov == "openrouter":
        return {"reasoning": {"effort": value}}
    if prov == "anthropic":
        return {"output_config": {"effort": value}}
    return {"reasoning_effort": value}


def effort_payload(provider: str | None, model: str | None, level: str | None) -> dict[str, Any]:
    """Fragment to merge into the chat JSON body. {} sends no effort field."""
    if not isinstance(level, str):
        return {}
    req = level.strip().lower()
    if not req or req == "default":
        return {}
    if req != LOWEST_SENTINEL and req not in OUR_EFFORTS:
        return {}

    sendable = _sendable_efforts(provider, model)
    if not sendable:
        return {}

    if req == LOWEST_SENTINEL:
        return _place_effort(provider, sendable[0])

    if req in sendable:
        return _place_effort(provider, req)

    req_idx = OUR_EFFORTS.index(req)
    chosen: str | None = None
    for s in reversed(sendable):
        if s in OUR_EFFORTS:
            if OUR_EFFORTS.index(s) < req_idx:
                chosen = s
                break
        elif s in ("none", "minimal"):
            chosen = s
            break

    if chosen is None:
        return {}
    return _place_effort(provider, chosen)


def next_lower_effort_body(provider: str | None, body: bytes | str) -> bytes | None:
    """Return new JSON request body with stepped-down effort on HTTP 400, or None."""
    try:
        raw_str = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        data = json.loads(raw_str)
        if not isinstance(data, dict):
            return None
    except Exception:
        return None

    model = data.get("model")
    sendable = _sendable_efforts(provider, model)

    sent_val: str | None = None
    field_kind: str | None = None

    out_cfg = data.get("output_config")
    if isinstance(out_cfg, dict) and isinstance(out_cfg.get("effort"), str):
        sent_val = out_cfg["effort"]
        field_kind = "output_config"
    else:
        rsn = data.get("reasoning")
        if isinstance(rsn, dict) and isinstance(rsn.get("effort"), str):
            sent_val = rsn["effort"]
            field_kind = "reasoning"
        elif isinstance(data.get("reasoning_effort"), str):
            sent_val = data["reasoning_effort"]
            field_kind = "reasoning_effort"

    if sent_val is None or field_kind is None:
        return None

    next_val: str | None = None
    if sendable and sent_val in sendable:
        idx = sendable.index(sent_val)
        if idx > 0:
            next_val = sendable[idx - 1]

    if next_val is not None:
        if field_kind == "output_config":
            data["output_config"]["effort"] = next_val
        elif field_kind == "reasoning":
            data["reasoning"]["effort"] = next_val
        elif field_kind == "reasoning_effort":
            data["reasoning_effort"] = next_val
    else:
        if field_kind == "output_config":
            data["output_config"].pop("effort", None)
            if not data["output_config"]:
                data.pop("output_config", None)
        elif field_kind == "reasoning":
            data["reasoning"].pop("effort", None)
            if not data["reasoning"]:
                data.pop("reasoning", None)
        elif field_kind == "reasoning_effort":
            data.pop("reasoning_effort", None)

    new_bytes = json.dumps(data).encode("utf-8")
    old_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
    if new_bytes == old_bytes:
        return None
    return new_bytes


def merge_openrouter_chat_extra(base: dict[str, Any], extra: dict[str, Any] | None) -> None:
    """Merge *extra* into *base* in place. Skips blocklisted keys; recurses into dict values."""
    if not extra:
        return
    for key, val in extra.items():
        if key in OPENROUTER_CHAT_EXTRA_BLOCKLIST:
            continue
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            merge_openrouter_chat_extra(base[key], val)
        elif isinstance(val, dict):
            base[key] = copy.deepcopy(val)
        else:
            base[key] = val


# Named UI / tool values → OpenRouter / Gemini aspect_ratio strings.
_NAMED_ASPECT_RATIOS: dict[str, str] = {
    "square": "1:1",
    "1:1": "1:1",
    "landscape_16_9": "16:9",
    "landscape_16:9": "16:9",
    "16:9": "16:9",
    "portrait_9_16": "9:16",
    "portrait_9:16": "9:16",
    "9:16": "9:16",
    "landscape_3_2": "3:2",
    "landscape_3:2": "3:2",
    "3:2": "3:2",
    "portrait_2_3": "2:3",
    "portrait_2:3": "2:3",
    "2:3": "2:3",
    "4:3": "4:3",
    "3:4": "3:4",
    "21:9": "21:9",
    "4:5": "4:5",
    "5:4": "5:4",
}

# Closest-match table for WxH. min() picks the nearest ratio, so order
# matters only when two ratios are exactly the same distance.
_PIXEL_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (("1:1", 1.0), ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3), ("3:4", 3 / 4), ("3:2", 1.5), ("2:3", 2 / 3), ("21:9", 21 / 9), ("4:5", 4 / 5), ("5:4", 5 / 4))


def canonical_aspect_ratio(width: int | None = None, height: int | None = None, named: str | None = None) -> str | None:
    """Return a provider aspect-ratio hint (``1:1``, ``16:9``, …) or None.

    Named UI/tool values win. Pixel WxH maps to the closest standard ratio
    within about 7% so 64-pixel rounding (``896x512``) still yields ``16:9``.
    An absolute 0.12 gap mapped ``512x1024`` (1:2) onto ``9:16`` while
    ``1024x512`` stayed unmapped, and OpenRouter then sent that aspect
    instead of the pixel size.
    """
    if named:
        key = str(named).strip().lower().replace(" ", "_").replace("(", "").replace(")", "")
        mapped = _NAMED_ASPECT_RATIOS.get(key)
        if mapped:
            return mapped
    if not width or not height or width < 1 or height < 1:
        return None
    ratio = width / height
    label, expected = min(_PIXEL_ASPECT_RATIOS, key=lambda item: abs(ratio - item[1]))
    if expected and abs(ratio / expected - 1) <= 0.07:
        return label
    return None


def canonical_resolution(width: int | None = None, height: int | None = None, *, family: str = "standard") -> str | None:
    """Map max(width, height) to a vendor resolution tier.

    The tier follows the longest edge, so 1536×1024 is ``1K`` (1536, and the
    ``1K`` bucket runs through 1536). Standard (OpenRouter ``/images``
    resolution, Google native): ``512``, ``1K``, ``2K``, ``4K``. OpenRouter
    chat ``image_config.image_size`` uses ``0.5K`` for the low tier
    (``family="openrouter_chat"``). Grok only documents ``1k`` / ``2k``.
    Imagen only documents ``1K`` / ``2K``.
    """
    if not width or not height or width < 1 or height < 1:
        return None
    edge = max(width, height)
    if edge <= 768:
        tier = "512"
    elif edge <= 1536:
        tier = "1K"
    elif edge <= 3072:
        tier = "2K"
    else:
        tier = "4K"
    if family == "grok":
        return "2k" if tier in ("2K", "4K") else "1k"
    if family == "imagen":
        if tier == "512":
            return "1K"
        if tier == "4K":
            return "2K"
        return tier
    # OpenRouter chat modalities path rejects "512"; enum is 0.5K|1K|2K|4K.
    if family == "openrouter_chat":
        return "0.5K" if tier == "512" else tier
    return tier


def coerce_image_data_url(image_url: str | None = None, source_image: str | None = None) -> str | None:
    """Normalize a source image to a data URL or http(s) URL for JSON image APIs."""
    ref = image_url or source_image
    if not ref:
        return None
    # Any data: URL is already labeled. Wrapping data:application/… again
    # produced data:image/png;base64,data:application/… and the model could
    # not decode it. Raw selection bytes stay PNG: get_selected_image_base64
    # always exports PNG, and raw base64 has no MIME to sniff.
    if ref.startswith(("data:", "http://", "https://")):
        return ref
    return "data:image/png;base64," + ref


def coerce_raw_b64(image_url: str | None = None, source_image: str | None = None) -> str | None:
    """Normalize a source image to raw base64 (Ollama ``images`` / Gemini ``inlineData``)."""
    ref = image_url or source_image
    if not ref:
        return None
    # Ollama's REST ``images[]`` and Gemini ``inlineData.data`` are raw base64.
    # An http(s) URL used to be returned unchanged and sent as if it were
    # base64. Ollama does not fetch URLs on this route, and a Gemini URL
    # belongs in ``fileData.fileUri``, which this client does not send.
    # https://docs.ollama.com/api/generate
    # https://ai.google.dev/gemini-api/docs/generate-content/image-understanding
    if ref.startswith(("http://", "https://")):
        raise ValueError("Ollama and Gemini image edits need raw base64 or a data URL, not an http(s) URL.")
    # A data URL with no comma stays the whole string. Stripping at the first
    # comma is the only split; Anthropic skips that shape before calling here.
    if ref.startswith("data:") and "," in ref:
        return ref.split(",", 1)[1]
    return ref


def inline_image_mime(image_url: str | None = None, source_image: str | None = None) -> str:
    """MIME type for an inline image part; default png when the source is raw base64."""
    ref = image_url or source_image
    if ref and ref.startswith("data:") and ";" in ref:
        mime = ref[5:].split(";", 1)[0]
        if mime:
            return mime
    return "image/png"


class BaseProviderShim:
    """Base provider shim implementing standard OpenAI-compatible API format by default."""

    client: Any
    _effort_provider: str | None = None

    def __init__(self, client: Any) -> None:
        self.client = client

    def _reasoning_provider(self) -> str:
        if self._openrouter_chat():
            return "openrouter"
        if self._effort_provider:
            return self._effort_provider
        get_provider = getattr(self.client, "_get_provider", None)
        if callable(get_provider):
            res = get_provider()
            if isinstance(res, str) and res:
                return res
        get_endpoint = getattr(self.client, "_endpoint", None)
        if callable(get_endpoint):
            from .provider_detection import get_provider_from_endpoint

            ep = get_endpoint()
            if isinstance(ep, str):
                return get_provider_from_endpoint(ep) or "custom"
        return "custom"

    def _endpoint_reasoning_effort(self) -> str:
        config = getattr(self.client, "config", None)
        if not isinstance(config, dict):
            return ""
        raw = config.get("reasoning_effort")
        if not isinstance(raw, str):
            return ""
        return raw.strip()

    def _requested_effort_level(self, chat_extra: dict[str, Any] | None) -> str:
        if isinstance(chat_extra, dict):
            reasoning = chat_extra.get("reasoning")
            if isinstance(reasoning, dict):
                raw_effort = reasoning.get("effort")
                if isinstance(raw_effort, str) and raw_effort.strip():
                    return raw_effort.strip()
        return self._endpoint_reasoning_effort()

    def _openrouter_chat(self) -> bool:
        config = getattr(self.client, "config", None)
        return bool(config.get("is_openrouter")) if isinstance(config, dict) else False

    def _url(self, suffix: str) -> str:
        """``endpoint + api_path + suffix``. Ollama and Google build their own URLs."""
        return self.client._endpoint() + self.client._api_path() + suffix

    def _post(self, url: str, data: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), headers if headers is not None else self.client._headers()

    def _chat_payload(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # top_p used to be hardcoded to 0.9 on every OpenAI-compatible chat.
        # Models that reject temperature and top_p together returned HTTP 400,
        # and there is no top_p setting. Omit it, same as a missing temperature.
        data: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens, "stream": stream}
        if temperature is not None:
            data["temperature"] = temperature
        if model_name:
            data["model"] = model_name
        if tools:
            data["tools"] = tools
            data["tool_choice"] = "auto"
            # Always false until the tool loop can apply parallel calls. The
            # config key is stored but not read here; do not send True.
            # Omitting the field is not safe: OpenAI defaults it to true.
            data["parallel_tool_calls"] = False
        if response_format:
            data["response_format"] = response_format

        openrouter = self._openrouter_chat()
        if openrouter:
            extra = self.client.config.get("openrouter_chat_extra")
            if isinstance(extra, dict) and extra:
                merge_openrouter_chat_extra(data, extra)

        # Bugfix: grammar's chat_extra was an OpenRouter reasoning object merged raw
        # (or rewritten to reasoning_effort: "minimal" on non-OpenRouter), sending
        # undocumented keys or values. The sidebar value was also copied verbatim,
        # sending max to xAI/Gemini and xhigh to models whose ceiling is high.
        # Now effort_payload maps or steps down per provider and model.
        level = self._requested_effort_level(chat_extra)
        if level:
            fragment = effort_payload(self._reasoning_provider(), model_name, level)
            if "reasoning" in fragment:
                reasoning = data.get("reasoning")
                if isinstance(reasoning, dict):
                    reasoning["effort"] = fragment["reasoning"]["effort"]
                else:
                    data["reasoning"] = dict(fragment["reasoning"])
            elif "reasoning_effort" in fragment:
                data["reasoning_effort"] = fragment["reasoning_effort"]
                if not openrouter and "reasoning" in data:
                    data.pop("reasoning", None)
        if isinstance(chat_extra, dict) and chat_extra:
            payload_extra = {k: v for k, v in chat_extra.items() if k != "reasoning"}
            if payload_extra:
                merge_openrouter_chat_extra(data, payload_extra)
        return data

    def build_chat_request(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float | None, tools: list[dict[str, Any]] | None, stream: bool, model_name: str | None, response_format: dict[str, Any] | None, chat_extra: dict[str, Any] | None = None
    ) -> tuple[str, str, bytes, dict[str, str]]:
        data = self._chat_payload(messages, max_tokens, temperature, tools, stream, model_name, response_format, chat_extra)
        return self._post(self._url("/chat/completions"), data)

    def parse_response_chunk(self, chunk: dict[str, Any], stream_state: dict[str, Any] | None = None) -> tuple[str, str | None, str | None, dict[str, Any]]:
        from .response_normalizers import normalize_message_content
        from .stream_normalizer import extract_thinking_from_delta

        choices = chunk.get("choices", [])
        # Unexpected schema (string "choices", non-dict first element) used to
        # AttributeError on .get and abort the whole stream. Treat as no choice
        # so later valid chunks can still apply.
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta_raw = choice.get("delta", {})
        delta = delta_raw if isinstance(delta_raw, dict) else {}

        finish_reason = choice.get("finish_reason") if choice else None
        if not finish_reason:
            finish_reason = chunk.get("finish_reason") or chunk.get("done_reason")
        if not finish_reason and choices:
            for c in choices:
                if isinstance(c, dict) and c.get("finish_reason"):
                    finish_reason = c.get("finish_reason")
                    break

        # Some OpenAI-compatible hosts send delta.content as a list of parts.
        # Returning that list made ThinkTagStreamSplitter.feed concatenate
        # str + list, which raised TypeError and aborted the stream. Sync
        # parse already flattens parts with this helper.
        # ``delta`` is always a dict here. ``if delta`` never failed: an empty
        # dict is truthy, so the else branch was dead.
        raw_content = delta.get("content")
        content = normalize_message_content(raw_content) or ""
        thinking = extract_thinking_from_delta(chunk)
        return content, finish_reason, thinking, delta

    def parse_sync_response(self, response_data: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]] | None, dict[str, Any], list[str], dict[str, Any]]:
        from .response_normalizers import normalize_message_content
        from .stream_normalizer import normalize_delta

        # Local models (Ollama) can return {"done_reason": "stop", "message": ...}
        # with no top-level "choices". Prefer choices[0] when present, and fall
        # back to the top-level message / done_reason so content is kept.
        choices = response_data.get("choices")
        choice = choices[0] if (isinstance(choices, list) and choices and isinstance(choices[0], dict)) else {}
        message = choice.get("message") or response_data.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        normalize_delta(message)
        finish_reason = choice.get("finish_reason") or response_data.get("finish_reason") or response_data.get("done_reason")
        err_obj = choice.get("error") or response_data.get("error")
        if finish_reason == "error" or err_obj:
            err_msg = ""
            if isinstance(err_obj, dict):
                err_msg = str(err_obj.get("message") or err_obj.get("type") or "").strip()
            elif isinstance(err_obj, str):
                err_msg = err_obj.strip()
            if not err_msg:
                err_msg = _("Stream ended with finish_reason=error")
            # The streaming loop raises NetworkError on finish_reason == 'error'
            # and choices[0].error. The sync path must do the same, or an empty
            # body looks like a successful reply and the caller burns turns.
            raise NetworkError(err_msg, code="STREAM_ERROR")

        raw_content = message.get("content")
        content = normalize_message_content(raw_content) or ""
        # A string ``images`` used to be returned as-is. ``image_utils`` iterates
        # that value, so each character was treated as an image URL.
        images = message.get("images")
        if not isinstance(images, list):
            images = []
        tool_calls = message.get("tool_calls")
        # A string tool_calls is truthy. request_with_tools then rewrites
        # finish_reason to tool_calls, and coalesce_split_tool_calls drops
        # the value because it is not a list.
        if not isinstance(tool_calls, list):
            tool_calls = None
        # JSON "usage": null makes .get("usage", {}) return None, because the
        # key is present. A list or string used to pass through, and callers
        # call usage.get.
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        return content, finish_reason, tool_calls, usage, images, message

    def _image_payload(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> dict[str, Any]:
        """Generic OpenAI-compatible generations body. Official OpenAI overrides the request."""
        data: dict[str, Any] = {"prompt": prompt, "n": 1, "response_format": "b64_json"}
        # image_generate floors with (n // 64) * 64 and keeps an explicit 0.
        # "0x512" is not a size any host accepts. Omit the field and let the
        # server default apply. OpenAI buckets sizes itself; Together drops size.
        if width > 0 and height > 0:
            data["size"] = f"{width}x{height}"
        if model:
            data["model"] = model
        if steps:
            data["steps"] = steps

        ref = coerce_image_data_url(image_url, source_image)
        if ref:
            data["image_url"] = ref
        return data

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        """Build an image generation request (standard OpenAI format)."""
        data = self._image_payload(prompt, model, width, height, steps, source_image, image_url)
        return self._post(self._url("/images/generations"), data)

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        """Extract list of base64 image data from response (standard OpenAI format)."""
        # ``image_completion`` already rejects a non-dict HTTP body. A direct
        # caller passing a list used to raise AttributeError on ``.get``.
        if not isinstance(response_data, dict):
            return []
        items = response_data.get("data", [])
        # A null or non-object entry used to raise AttributeError on .get.
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            if isinstance(it, dict) and (b64 := it.get("b64_json")):
                out.append(b64)
        return out
