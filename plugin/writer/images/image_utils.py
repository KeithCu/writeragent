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
from plugin.framework.errors import NetworkError, ToolExecutionError, resolve_exception_message

log = logging.getLogger(__name__)

IMAGE_DOWNLOAD_MAX_BYTES: int = 32 * 1024 * 1024
IMAGE_CONTENT_TYPE_DENYLIST: tuple[str, ...] = ("text/html", "text/plain", "application/xhtml+xml", "application/json")


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


def validated_image_suffix(data: bytes) -> str:
    """Validate image payload size and sniff extension, rejecting non-images and oversized data."""
    if not data:
        raise RuntimeError("empty image download")
    if len(data) > IMAGE_DOWNLOAD_MAX_BYTES:
        raise RuntimeError("image download exceeded %s bytes" % IMAGE_DOWNLOAD_MAX_BYTES)
    suffix = suffix_for_image_bytes(data)
    if suffix == ".img":
        raise RuntimeError("Downloaded data is not a recognized image format")
    return suffix


def write_temp_image(data: bytes, suffix: str) -> str:
    """Write image bytes to a temporary file prefixed with 'wa-img-'. Unlink on failure."""
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


def _abandon_image_paths(paths: list[str], stop_checker: Any, exc: Exception, log_message: str) -> tuple[list[str], str]:
    _unlink_temp_paths(paths)
    is_stopped = False
    if isinstance(exc, NetworkError) and getattr(exc, "code", None) == "STOPPED":
        is_stopped = True
    elif stop_checker is not None and stop_checker():
        is_stopped = True
    if is_stopped:
        log.debug("Image generation cancelled by Stop: %s", exc)
        raise ToolExecutionError("Image generation stopped by user.", code="USER_STOPPED") from exc
    message = resolve_exception_message(exc)
    if isinstance(exc, (NetworkError, ToolExecutionError)):
        log.warning("%s: %s", log_message, message)
    else:
        log.exception(log_message)
    return [], message


def _extract_b64(text: str) -> str | None:
    match = re.search(r"base64,([A-Za-z0-9+/=]+)", text)
    return match.group(1) if match else None


class EndpointImageProvider:
    """Uses the endpoint URL and API key from Settings (same as chat). Model from image_model or text model."""

    client: LlmClient
    model: str

    def __init__(self, api_config: Any, ctx: Any) -> None:
        from plugin.framework.client.model_fetcher import get_image_model

        self.client = LlmClient(api_config, ctx)
        self.model = str(api_config.get("model") or get_image_model() or "").strip()

    def _save_b64(self, b64_data: str) -> str:
        if len(b64_data) > (IMAGE_DOWNLOAD_MAX_BYTES * 4) // 3 + 4:
            raise RuntimeError("image download exceeded %s bytes" % IMAGE_DOWNLOAD_MAX_BYTES)
        raw_bytes = base64.b64decode(b64_data)
        suffix = validated_image_suffix(raw_bytes)
        return write_temp_image(raw_bytes, suffix=suffix)

    def _save_url(self, url: str, *, stop_checker: Any = None, status_callback: Any = None) -> str:
        resp = sync_request(
            url,
            parse_json=False,
            include_meta=True,
            timeout=get_config_int("request_timeout"),
            max_bytes=IMAGE_DOWNLOAD_MAX_BYTES,
            stop_checker=stop_checker,
            status_callback=status_callback,
        )
        body = getattr(resp, "body", resp)
        content_type = (getattr(resp, "content_type", "") or "").split(";")[0].strip().lower()
        if content_type in IMAGE_CONTENT_TYPE_DENYLIST:
            raise RuntimeError("Invalid Content-Type '%s' for image download" % content_type)
        suffix = validated_image_suffix(body)
        return write_temp_image(body, suffix=suffix)

    def _save_image_ref(
        self,
        ref: str,
        *,
        stop_checker: Any,
        status_callback: Any,
        http_text: str,
        fetch_http_if_data_failed: bool,
    ) -> str | None:
        if "data:image" in ref:
            b64 = _extract_b64(ref)
            if b64:
                return self._save_b64(b64)
            if not fetch_http_if_data_failed:
                return None
        if http_text.startswith("http"):
            return self._save_url(http_text, stop_checker=stop_checker, status_callback=status_callback)
        return None

    def _run_image_completion(
        self,
        prompt: str,
        model: str,
        width: int,
        height: int,
        steps: int | None,
        source_image: str | None,
        stop_checker: Any,
        status_callback: Any,
        log_message: str,
    ) -> tuple[list[str], str]:
        paths: list[str] = []
        try:
            b64_list = self.client.image_completion(
                prompt,
                model=model,
                width=width,
                height=height,
                steps=steps,
                source_image=source_image,
                stop_checker=stop_checker,
                status_callback=status_callback,
            )
            for b64 in b64_list:
                paths.append(self._save_b64(b64))
            if paths:
                return paths, ""
            return [], "No image data returned from provider"
        except Exception as e:
            return _abandon_image_paths(paths, stop_checker, e, log_message)

    def generate(
        self,
        prompt: str,
        width: int = DEFAULT_IMAGE_BASE_SIZE,
        height: int = DEFAULT_IMAGE_BASE_SIZE,
        model: str | None = None,
        steps: int | None = None,
        status_callback: Any = None,
        stop_checker: Any = None,
        image_model: str | None = None,
        source_image: str | None = None,
        aspect_ratio: str | None = None,
    ) -> tuple[list[str], str]:
        """Request image via the configured endpoint (modalities=['image'] where supported)."""
        try:
            if isinstance(image_model, str) and image_model.strip():
                model = image_model.strip()
            model = model or self.model
            steps = steps if (steps is not None and steps > 0) else None
            log.info("Requesting image via endpoint: %s", model)

            is_openrouter = self.client.config.get("is_openrouter")
            if not is_openrouter:
                return self._run_image_completion(
                    prompt, model, width, height, steps, source_image, stop_checker, status_callback,
                    "Image generation failed",
                )

            from plugin.framework.client.model_fetcher import is_image_only_model
            if is_image_only_model(self.client._endpoint(), model):
                return self._run_image_completion(
                    prompt, model, width, height, steps, source_image, stop_checker, status_callback,
                    "OpenRouter dedicated image generation failed",
                )

            content: Any
            if source_image:
                content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + source_image}}]
            else:
                content = prompt
            messages = [{"role": "user", "content": content}]
            fallback_content = ""

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
                hint = canonical_aspect_ratio(width, height, named=aspect_ratio)
                image_config = {}
                if hint:
                    image_config["aspect_ratio"] = hint
                size_hint = canonical_resolution(width, height, family="openrouter_chat")
                if size_hint:
                    image_config["image_size"] = size_hint
                if image_config:
                    body_dict["image_config"] = image_config
            if steps is not None:
                body_dict["steps"] = steps

            paths = []
            try:
                chat_resp = self.client.request_with_tools(
                    messages,
                    body_override=json.dumps(body_dict),
                    model=model,
                    stop_checker=stop_checker,
                    status_callback=status_callback,
                )
                raw_content = chat_resp.get("content")
                fallback_content = raw_content if isinstance(raw_content, str) else ""

                # Parse response: OpenRouter etc. may put image in message.images[].image_url.url
                for img in chat_resp.get("images") or []:
                    url = None
                    if isinstance(img, dict):
                        if "image_url" in img and isinstance(img["image_url"], dict):
                            url = img["image_url"].get("url")
                        elif "image_url" in img and isinstance(img["image_url"], str):
                            url = img["image_url"]

                    if not url:
                        continue

                    path = self._save_image_ref(
                        url,
                        stop_checker=stop_checker,
                        status_callback=status_callback,
                        http_text=url,
                        fetch_http_if_data_failed=False,
                    )
                    if path:
                        paths.append(path)
            except Exception as e:
                return _abandon_image_paths(paths, stop_checker, e, "OpenRouter image processing failed")

            if paths:
                return paths, ""

            # Fallback: image in content string (some endpoints)
            if fallback_content:
                try:
                    path = self._save_image_ref(
                        fallback_content,
                        stop_checker=stop_checker,
                        status_callback=status_callback,
                        http_text=fallback_content.strip(),
                        fetch_http_if_data_failed=True,
                    )
                    if path:
                        paths.append(path)
                        return paths, ""
                except Exception as e:
                    return _abandon_image_paths(paths, stop_checker, e, "Image fallback content processing failed")

            return [], "No image data returned from provider"
        finally:
            self.client._close_connection()


class ImageService:
    ctx: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def get_provider(self) -> EndpointImageProvider:
        from plugin.framework.config import get_api_config
        from plugin.framework.client.model_fetcher import get_image_model

        api_config = get_api_config().copy()
        api_config["model"] = get_image_model()
        return EndpointImageProvider(api_config, self.ctx)

    def generate_image(self, prompt: str, status_callback: Any = None, stop_checker: Any = None, **kwargs: Any) -> tuple[list[str], str]:
        provider = self.get_provider()
        base_size = get_config_int("image_base_size")
        for key in ("width", "height"):
            if kwargs.get(key) is None:
                kwargs[key] = base_size
        kwargs.setdefault("steps", get_config_int("image_steps"))

        return provider.generate(prompt, status_callback=status_callback, stop_checker=stop_checker, **kwargs)
