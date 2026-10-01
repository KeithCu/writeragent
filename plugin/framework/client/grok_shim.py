# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""xAI Grok provider shim."""

from __future__ import annotations

import json
from typing import Any

from plugin.framework.url_utils import get_url_path_and_query
from .base_provider_shim import canonical_aspect_ratio, canonical_resolution, coerce_image_data_url
from .openai_shim import OpenAIShim


class GrokShim(OpenAIShim):
    """Shim for xAI Grok API (OpenAI-compatible with Aurora image generation)."""

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        ref = coerce_image_data_url(image_url, source_image)
        # Create stays on /images/generations. xAI edit is a different JSON
        # route (not OpenAI multipart): POST /v1/images/edits with image.url.
        # https://docs.x.ai/developers/model-capabilities/images/editing
        url = endpoint + api_path + ("/images/edits" if ref else "/images/generations")

        data: dict[str, Any] = {"prompt": prompt, "n": 1, "response_format": "b64_json", "model": model or "aurora"}
        # What was wrong: width/height were accepted and never written. xAI
        # documents aspect_ratio (default auto) and resolution (1k/2k), not
        # OpenAI size, so Square / 2048 in the sidebar never reached the model.
        # https://docs.x.ai/developers/model-capabilities/images/generation
        ratio = canonical_aspect_ratio(width, height)
        if ratio:
            data["aspect_ratio"] = ratio
        res = canonical_resolution(width, height, family="grok")
        if res:
            data["resolution"] = res
        if steps:
            data["steps"] = steps
        if ref:
            data["image"] = {"url": ref, "type": "image_url"}

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()
