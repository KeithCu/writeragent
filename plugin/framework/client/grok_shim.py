# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""xAI Grok provider shim."""

from __future__ import annotations

from typing import Any

from .base_provider_shim import BaseProviderShim, canonical_aspect_ratio, canonical_resolution, coerce_image_data_url


class GrokShim(BaseProviderShim):
    """Shim for xAI Grok API (OpenAI-compatible chat, Imagine image generation)."""

    _effort_provider: str | None = "xai"

    def build_image_request(self, prompt: str, model: str | None, width: int, height: int, steps: int | None = None, source_image: str | None = None, image_url: str | None = None) -> tuple[str, str, bytes, dict[str, str]]:
        ref = coerce_image_data_url(image_url, source_image)
        # Create stays on /images/generations. xAI edit is a different JSON
        # route (not OpenAI multipart): POST /v1/images/edits with image.url.
        # https://docs.x.ai/developers/model-capabilities/images/editing
        url = self._url("/images/edits" if ref else "/images/generations")

        # An omitted image model uses the current xAI model. A supplied id is
        # sent unchanged. https://docs.x.ai/docs/models/grok-imagine-image
        data: dict[str, Any] = {"prompt": prompt, "n": 1, "response_format": "b64_json", "model": str(model or "").strip() or "grok-imagine-image-2.0"}
        # xAI documents aspect_ratio (default auto) and resolution (1k/2k), not
        # OpenAI size. Write width and height as those fields, or Square / 2048
        # in the sidebar never reach the model.
        # response_format stays b64_json: the documented default is a URL, and
        # this client reads b64_json. steps is not an xAI Images field, so a
        # Settings steps value is not sent.
        # https://docs.x.ai/developers/rest-api-reference/inference/images
        # https://docs.x.ai/developers/model-capabilities/images/generation
        ratio = canonical_aspect_ratio(width, height)
        if ratio:
            data["aspect_ratio"] = ratio
        res = canonical_resolution(width, height, family="grok")
        if res:
            data["resolution"] = res
        if ref:
            data["image"] = {"url": ref, "type": "image_url"}

        return self._post(url, data)
