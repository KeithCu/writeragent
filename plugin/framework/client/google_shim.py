# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Google Gemini Provider Shim.

Combines Google's OpenAI-compatible interface for chat/streaming/tools
with Google's native REST interface for image generation (Imagen & Gemini Multimodal).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from plugin.framework.url_utils import get_url_path_and_query
from .base_provider_shim import canonical_aspect_ratio, coerce_raw_b64, inline_image_mime
from .openai_shim import OpenAIShim

log = logging.getLogger(__name__)


class GoogleShim(OpenAIShim):
    """Shim for Google Gemini: OpenAI-compatible for chat/tools, native REST for images."""

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
        key = self.client._resolve_auth().get("api_key", "")
        model_name = model or "imagen-4.0-generate-001"

        # Key in x-goog-api-key, not ?key=, so it does not land in access logs
        # or ``log.debug("URL: ...")``.
        has_source = bool(image_url or source_image)
        if model_name.startswith("imagen"):
            # Imagen :predict is text-to-image only. Posting a prompt-only
            # predict while a graphic is selected would replace it with a
            # new image (same silent miss as OpenRouter's old image_url).
            if has_source:
                raise ValueError(
                    "Imagen models cannot edit an existing image. Pick a Gemini image model (for example gemini-2.5-flash-image)."
                )
            url = f"{endpoint}/v1beta/models/{model_name}:predict"
            aspect = canonical_aspect_ratio(width, height) or "1:1"
            data: dict[str, Any] = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": aspect}}
        else:
            url = f"{endpoint}/v1beta/models/{model_name}:generateContent"
            parts: list[dict[str, Any]] = [{"text": prompt}]
            raw = coerce_raw_b64(image_url, source_image)
            if raw:
                parts.append({
                    "inlineData": {
                        "mimeType": inline_image_mime(image_url, source_image),
                        "data": raw,
                    }
                })
            # Gemini image models ignore pixel size; imageConfig.aspectRatio is
            # the documented hint (1:1, 16:9, 4:3, …). Native generateContent
            # previously sent no aspect at all.
            aspect = canonical_aspect_ratio(width, height) or "1:1"
            data = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseModalities": ["IMAGE", "TEXT"],
                    "imageConfig": {"aspectRatio": aspect},
                },
            }

        path = get_url_path_and_query(url)
        headers = dict(self.client._headers())
        if key:
            headers["x-goog-api-key"] = key
        return "POST", path, json.dumps(data).encode("utf-8"), headers

    def parse_image_responses(self, response_data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        if "error" in response_data:
            msg = response_data["error"].get("message", "Unknown Google API error")
            log.error("Google image generation error: %s", msg)
            return []

        if "predictions" in response_data:
            preds = response_data.get("predictions", [])
            if isinstance(preds, list):
                for pr in preds:
                    if isinstance(pr, dict):
                        if b64 := pr.get("bytesBase64Encoded"):
                            out.append(b64)

        candidates = response_data.get("candidates", [])
        if candidates and isinstance(candidates, list):
            cand = candidates[0]
            if isinstance(cand, dict):
                parts = cand.get("content", {}).get("parts", [])
                if isinstance(parts, list):
                    for p in parts:
                        if isinstance(p, dict):
                            inline = p.get("inlineData", {})
                            if isinstance(inline, dict) and inline.get("data"):
                                out.append(inline["data"])
        return out
