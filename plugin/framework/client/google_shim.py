# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Google Gemini Provider Shim.

Combines Google's OpenAI-compatible interface for chat/streaming/tools
with Google's native REST interface for image generation (Imagen & Gemini Multimodal).
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from .base_provider_shim import BaseProviderShim, _dict_parts, canonical_aspect_ratio, canonical_resolution, coerce_raw_b64, inline_image_mime

log = logging.getLogger(__name__)


def _google_model_id(model: str | None) -> str:
    """Resource id for ``/v1beta/models/{id}:…``.

    Native ``models.list`` returns ``models/gemini-…``. Pasting that name used
    to build ``/v1beta/models/models/…`` and 404. Official ids are lowercase;
    ``Imagen-4`` missed ``startswith("imagen")`` and went to generateContent,
    which also skipped the Imagen edit guard.
    https://ai.google.dev/api/models
    """
    name = str(model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/") :]
    if not name:
        name = "imagen-4.0-generate-001"
    return urllib.parse.quote(name, safe="-._")


def _gemini_image_size(model_id: str, width: int, height: int) -> str | None:
    """``imageSize`` for Gemini 3 image models, or None.

    ``gemini-2.5-flash-image`` documents ``aspectRatio`` only. ``imageSize``
    on that model (including the ``512`` tier) is not in the generateContent
    samples, and a Google 400 is not retried. Gemini 3.1 Flash Lite Image is
    1K only. ``512`` (no ``K``) is only Gemini 3.1 Flash Image.
    https://ai.google.dev/gemini-api/docs/generate-content/image-generation
    """
    lower = model_id.lower()
    accepts = "nano-banana" in lower or ("gemini-3" in lower and "image" in lower)
    if not accepts:
        return None
    if "lite" in lower:
        return "1K"
    res = canonical_resolution(width, height)
    if not res:
        return None
    if res == "512" and "3.1-flash-image" not in lower:
        return "1K"
    return res


class GoogleShim(BaseProviderShim):
    """Shim for Google Gemini: OpenAI-compatible for chat/tools, native REST for images."""

    _effort_provider: str | None = "google"

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        key = self.client._resolve_auth().get("api_key", "")
        model_name = _google_model_id(model)

        # Key in x-goog-api-key, not ?key=, so it does not land in access logs
        # or ``log.debug("URL: ...")``.
        has_source = bool(image_url or source_image)
        if model_name.startswith("imagen"):
            # Imagen :predict is text-to-image only. Posting a prompt-only
            # predict while a graphic is selected would replace it with a
            # new image (same silent miss as OpenRouter's old image_url).
            if has_source:
                raise ValueError("Imagen models cannot edit an existing image. Pick a Gemini image model (for example gemini-2.5-flash-image).")
            url = f"{endpoint}/v1beta/models/{model_name}:predict"
            aspect = canonical_aspect_ratio(width, height) or "1:1"
            params: dict[str, Any] = {"sampleCount": 1, "aspectRatio": aspect}
            # Imagen ignores pixel size; imageSize is 1K/2K only.
            res = canonical_resolution(width, height, family="imagen")
            if res:
                params["imageSize"] = res
            data: dict[str, Any] = {"instances": [{"prompt": prompt}], "parameters": params}
        else:
            url = f"{endpoint}/v1beta/models/{model_name}:generateContent"
            parts: list[dict[str, Any]] = [{"text": prompt}]
            raw = coerce_raw_b64(image_url, source_image)
            if raw:
                parts.append({"inlineData": {"mimeType": inline_image_mime(image_url, source_image), "data": raw}})
            # Gemini image models ignore pixel size. aspectRatio is documented
            # for every Nano Banana model. imageSize is only on Gemini 3 image
            # models; gemini-2.5-flash-image samples send aspectRatio alone.
            aspect = canonical_aspect_ratio(width, height) or "1:1"
            image_config: dict[str, Any] = {"aspectRatio": aspect}
            res = _gemini_image_size(model_name, width, height)
            if res:
                image_config["imageSize"] = res
            data = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"responseModalities": ["IMAGE", "TEXT"], "imageConfig": image_config}}

        headers = dict(self.client._headers())
        if key:
            headers["x-goog-api-key"] = key
        return self._post(url, data, headers)

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        # ``image_completion`` already rejects a non-dict HTTP body. A direct
        # caller passing a list used to raise AttributeError on ``.get``.
        if not isinstance(response_data, dict):
            return []
        out: list[str] = []
        # A success body can include "error": null. Checking the key treated
        # that as a failure and raised NetworkError("None") via str(None).
        # Only a truthy error is a Google API error. Chat parse already
        # ignores a null error the same way.
        err = response_data.get("error")
        if err:
            from plugin.framework.errors import NetworkError

            msg = err.get("message", "Unknown Google API error") if isinstance(err, dict) else str(err)
            log.error("Google image generation error: %s", msg)
            raise NetworkError(str(msg), code="HTTP_ERROR")

        for pr in _dict_parts(response_data.get("predictions")):
            if b64 := pr.get("bytesBase64Encoded"):
                out.append(b64)

        candidates = response_data.get("candidates", [])
        if candidates and isinstance(candidates, list):
            cand = candidates[0]
            if isinstance(cand, dict):
                # A present null ``content`` (Gemini safety blocks) is not the
                # default {}. ``None.get`` used to raise AttributeError out of
                # image_completion instead of returning no images.
                content = cand.get("content")
                for p in _dict_parts(content.get("parts") if isinstance(content, dict) else None):
                    inline = p.get("inlineData", {})
                    if isinstance(inline, dict) and inline.get("data"):
                        out.append(inline["data"])
        return out
