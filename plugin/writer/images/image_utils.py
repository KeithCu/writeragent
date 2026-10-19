# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Unified Image Generation Service for WriterAgent."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
from typing import Any

from plugin.framework.client.base_provider_shim import canonical_aspect_ratio, canonical_resolution
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.client.requests import sync_request
from plugin.framework.config import get_config_int
from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE

log = logging.getLogger(__name__)

TRIM_IMAGES_IN_LOG = True
IMAGE_DOWNLOAD_MAX_BYTES: int = 32 * 1024 * 1024


def suffix_for_image_bytes(data: bytes) -> str:
    """Sniff image extension from magic bytes."""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    first_kb = data[:1024].lower()
    if b"<svg" in first_kb and b"<html" not in first_kb and b"<!doctype html" not in first_kb:
        return ".svg"
    return ".img"


def write_temp_image(data: bytes, suffix: str | None = None) -> str:
    """Write image bytes to a temporary file prefixed with 'wa-img-'. Unlink on failure."""
    if suffix is None:
        suffix = suffix_for_image_bytes(data)
    fd, path = tempfile.mkstemp(prefix="wa-img-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _unlink_temp_paths(paths: list[str]) -> None:
    temp_dir = os.path.realpath(tempfile.gettempdir())
    for p in paths:
        try:
            real = os.path.realpath(p)
            if os.path.basename(real).startswith("wa-img-") and (real.startswith(temp_dir + os.sep) or os.path.dirname(real) == temp_dir):
                os.unlink(real)
        except OSError:
            pass


def resolve_generation_size(
    base_size: int,
    aspect: str | None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int, str | None]:
    """Resolve pixel dimensions and wire aspect ratio for image generation."""
    normalized = canonical_aspect_ratio(named=aspect) if aspect else "1:1"
    if not normalized:
        normalized = "1:1"

    num, den = map(int, normalized.split(":"))
    if num > den:
        calc_w = int(base_size * num / den)
        calc_h = base_size
    elif num < den:
        calc_w = base_size
        calc_h = int(base_size * den / num)
    else:
        calc_w = base_size
        calc_h = base_size

    calc_w = (calc_w // 64) * 64
    calc_h = (calc_h // 64) * 64

    final_w = width if width is not None else calc_w
    final_h = height if height is not None else calc_h

    wire_ratio = canonical_aspect_ratio(final_w, final_h)
    return final_w, final_h, wire_ratio


class ImageProvider:
    def generate(self, prompt: str, **kwargs: Any) -> tuple[list[str], str]:
        raise NotImplementedError()


class EndpointImageProvider(ImageProvider):
    """Uses the endpoint URL and API key from Settings (same as chat). Model from image_model or text model."""

    client: LlmClient
    model: str
    ctx: Any

    def __init__(self, api_config: Any, ctx: Any) -> None:
        self.client = LlmClient(api_config, ctx)
        self.model = api_config.get("model", "google/gemini-3.1-flash-lite-preview")
        self.ctx = ctx

    def _save_b64(self, b64_data: str) -> list[str]:
        raw_bytes = base64.b64decode(b64_data)
        sniffed = suffix_for_image_bytes(raw_bytes)
        suffix = sniffed if sniffed != ".img" else ".png"
        return [write_temp_image(raw_bytes, suffix=suffix)]

    def _save_url(self, url: str, *, stop_checker: Any = None, status_callback: Any = None) -> list[str]:
        extra: dict[str, Any] = {}
        if stop_checker is not None:
            extra["stop_checker"] = stop_checker
        if status_callback is not None:
            extra["status_callback"] = status_callback
        data = sync_request(
            url,
            parse_json=False,
            timeout=get_config_int("request_timeout"),
            max_bytes=IMAGE_DOWNLOAD_MAX_BYTES,
            **extra,
        )
        if len(data) > IMAGE_DOWNLOAD_MAX_BYTES:
            raise RuntimeError("image download exceeded %s bytes" % IMAGE_DOWNLOAD_MAX_BYTES)
        return [write_temp_image(data)]

    def generate(self, prompt: str, width: int = DEFAULT_IMAGE_BASE_SIZE, height: int = DEFAULT_IMAGE_BASE_SIZE, model: str | None = None, steps: int | None = None, **kwargs: Any) -> tuple[list[str], str]:
        """Request image via the configured endpoint (modalities=['image'] where supported)."""
        status_callback = kwargs.pop("status_callback", None)
        stop_checker = kwargs.pop("stop_checker", None)
        wire: dict[str, Any] = {}
        if status_callback is not None:
            wire["status_callback"] = status_callback
        if stop_checker is not None:
            wire["stop_checker"] = stop_checker
        override = kwargs.pop("image_model", None)
        if isinstance(override, str) and override.strip():
            model = override.strip()
        model = model or self.model
        # For OpenRouter edit (img2img): send multimodal message with text + source image
        source_image = kwargs.get("source_image")
        if self.client.config.get("is_openrouter"):
            content: Any
            if source_image:
                content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + source_image}}]
            else:
                content = prompt
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": prompt}]
        log.info("Requesting image via endpoint: %s", model)

        fallback_content = ""

        if self.client.config.get("is_openrouter"):
            use_dedicated = False
            from plugin.framework.client.model_fetcher import is_image_only_model
            if is_image_only_model(self.client._endpoint(), model):
                use_dedicated = True

            if use_dedicated:
                valid_steps = steps if (steps is not None and steps > 0) else None
                paths: list[str] = []
                try:
                    b64_list = self.client.image_completion(prompt, model=model, width=width, height=height, steps=valid_steps, source_image=source_image, **wire)
                    for b64 in b64_list:
                        paths.extend(self._save_b64(b64))
                    if paths:
                        return paths, ""
                    return [], "No image data returned from provider"
                except Exception as e:
                    _unlink_temp_paths(paths)
                    # A user Stop closes the socket mid-request; that is not a failure.
                    if stop_checker is not None and stop_checker():
                        log.debug("OpenRouter dedicated image generation cancelled by Stop: %s", e)
                    else:
                        log.exception("OpenRouter dedicated image generation failed")
                    return [], str(e)

            _method, _path, body, _headers = self.client.make_chat_request(messages, max_tokens=1000, model=model)
            body_dict = json.loads(body)
            body_dict["modalities"] = ["image"]
            # Edit: source image already defines geometry. Sending sidebar
            # Square / Base Size as image_config overrode the selection and
            # mapped ~512 display px to image_size "512", which OpenRouter
            # chat rejects (enum is 0.5K|1K|2K|4K).
            if not source_image:
                # Create: Gemini multimodal models need image_config hints;
                # pixel size is not a chat-completions field
                # (https://openrouter.ai/google/gemini-3.1-flash-lite-image).
                hint = canonical_aspect_ratio(width, height, named=kwargs.get("aspect_ratio"))
                image_config = {}
                if hint:
                    image_config["aspect_ratio"] = hint
                size_hint = canonical_resolution(width, height, family="openrouter_chat")
                if size_hint:
                    image_config["image_size"] = size_hint
                if image_config:
                    body_dict["image_config"] = image_config
            if steps is not None and steps > 0:
                body_dict["steps"] = steps
            if "max_tokens" in kwargs:
                body_dict["max_tokens"] = kwargs["max_tokens"]

            chat_resp = self.client.request_with_tools(messages, body_override=json.dumps(body_dict), model=model, **wire)
            raw_content = chat_resp.get("content")
            fallback_content = raw_content if isinstance(raw_content, str) else ""

            # Parse response: OpenRouter etc. may put image in message.images[].image_url.url
            paths = []
            try:
                for img in chat_resp.get("images") or []:
                    url = None
                    if isinstance(img, dict):
                        if "image_url" in img and isinstance(img["image_url"], dict):
                            url = img["image_url"].get("url")
                        elif "image_url" in img and isinstance(img["image_url"], str):
                            url = img["image_url"]

                    if not url:
                        continue

                    if "data:image" in url:
                        match = re.search(r"base64,([A-Za-z0-9+/=]+)", url)
                        if match:
                            paths.extend(self._save_b64(match.group(1)))
                    elif url.startswith("http"):
                        paths.extend(self._save_url(url, stop_checker=stop_checker, status_callback=status_callback))
            except Exception as e:
                _unlink_temp_paths(paths)
                if stop_checker is not None and stop_checker():
                    log.debug("OpenRouter image download cancelled by Stop: %s", e)
                else:
                    log.exception("OpenRouter image processing failed")
                return [], str(e)

            if paths:
                return paths, ""
        else:
            # Use the unified image_completion method (handles Google, Ollama, OpenAI, etc. via shims)
            valid_steps = steps if (steps is not None and steps > 0) else None
            paths = []
            try:
                b64_list = self.client.image_completion(prompt, model=model, width=width, height=height, steps=valid_steps, source_image=kwargs.get("source_image"), **wire)
                for b64 in b64_list:
                    paths.extend(self._save_b64(b64))
                if paths:
                    return paths, ""
                return [], "No image data returned from provider"
            except Exception as e:
                _unlink_temp_paths(paths)
                if stop_checker is not None and stop_checker():
                    log.debug("Image generation cancelled by Stop: %s", e)
                else:
                    log.exception("Image generation failed")
                return [], str(e)

        # Fallback: image in content string (some endpoints)
        if isinstance(fallback_content, str) and fallback_content:
            paths = []
            try:
                if "data:image" in fallback_content:
                    match = re.search(r"base64,([A-Za-z0-9+/=]+)", fallback_content)
                    if match:
                        paths.extend(self._save_b64(match.group(1)))
                stripped = fallback_content.strip()
                if not paths and stripped.startswith("http"):
                    paths.extend(self._save_url(stripped, stop_checker=stop_checker, status_callback=status_callback))
                if paths:
                    return paths, ""
            except Exception as e:
                _unlink_temp_paths(paths)
                return [], str(e)

        return [], ""


class ImageService:
    ctx: Any
    config: Any

    def __init__(self, ctx: Any, config: Any) -> None:
        self.ctx = ctx
        self.config = config
        self.providers: dict[str, Any] = {}

    def get_provider(self, name: str | None = None) -> Any | None:
        if name and name not in ("endpoint", "openrouter"):
            return None
        from plugin.framework.config import get_api_config
        from plugin.framework.client.model_fetcher import get_image_model

        api_config = get_api_config().copy()
        cfg = self.config or {}
        api_config["model"] = (cfg.get("image_model") or "").strip() or get_image_model()
        return EndpointImageProvider(api_config, self.ctx)

    def generate_image(self, prompt: str, provider_name: str | None = None, status_callback: Any = None, stop_checker: Any = None, **kwargs: Any) -> tuple[list[str], str]:
        provider = self.get_provider(provider_name or "endpoint")
        if not provider:
            raise ValueError(f"Unknown provider: {provider_name}")

        # Merge configuration defaults with kwargs
        base_size = get_config_int("image_base_size")
        steps = get_config_int("image_steps")


        defaults: dict[str, Any] = {
            "width": base_size,
            "height": base_size,
            "steps": steps,
        }

        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v

        return provider.generate(prompt, status_callback=status_callback, stop_checker=stop_checker, **kwargs)
