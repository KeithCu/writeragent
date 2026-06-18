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

import json
import logging
import tempfile
import re
import base64
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.client.requests import sync_request
from plugin.framework.config import get_config_int

log = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

TRIM_IMAGES_IN_LOG = True


class ImageProvider:
    def generate(self, prompt, **kwargs):
        raise NotImplementedError()


class EndpointImageProvider(ImageProvider):
    """Uses the endpoint URL and API key from Settings (same as chat). Model from image_model or text model."""

    def __init__(self, api_config, ctx):
        self.client = LlmClient(api_config, ctx)
        self.model = api_config.get("model", "google/gemini-3.1-flash-lite-preview")

    def _save_b64(self, b64_data):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(base64.b64decode(b64_data))
            return [tmp.name]

    def _save_url(self, url, suffix=".webp"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(sync_request(url, parse_json=False))
            return [tmp.name]

    def generate(self, prompt, width=512, height=512, model=None, steps=None, **kwargs):
        """Request image via the configured endpoint (modalities=['image'] where supported)."""
        override = kwargs.pop("image_model", None)
        if isinstance(override, str) and override.strip():
            model = override.strip()
        model = model or self.model
        # For OpenRouter edit (img2img): send multimodal message with text + source image
        source_image = kwargs.get("source_image")
        if self.client.config.get("is_openrouter"):
            if source_image:
                content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + source_image}}]
            else:
                content = prompt
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": prompt}]
        logger.info("Requesting image via endpoint: %s", model)

        fallback_content = ""

        if self.client.config.get("is_openrouter"):
            method, path, body, headers = self.client.make_chat_request(messages, max_tokens=1000, model=model)
            body_dict = json.loads(body)
            body_dict["modalities"] = ["image"]
            if steps is not None and steps > 0:
                body_dict["steps"] = steps
            if "max_tokens" in kwargs:
                body_dict["max_tokens"] = kwargs["max_tokens"]

            chat_resp = self.client.request_with_tools(messages, body_override=json.dumps(body_dict), model=model)
            fallback_content = chat_resp.get("content") or ""

            # Parse response: OpenRouter etc. may put image in message.images[].image_url.url
            paths = []
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
                    paths.extend(self._save_url(url))

            if paths:
                return paths, ""
        else:
            # Use the unified image_completion method (handles Google, Ollama, OpenAI, etc. via shims)
            valid_steps = steps if (steps is not None and steps > 0) else None
            try:
                b64_list = self.client.image_completion(prompt, model=model, width=width, height=height, steps=valid_steps, source_image=kwargs.get("source_image"))
                paths = []
                for b64 in b64_list:
                    paths.extend(self._save_b64(b64))
                if paths:
                    return paths, ""
                return [], "No image data returned from provider"
            except Exception as e:
                logger.error("Image generation error: %s", e)
                return [], str(e)

        # Fallback: image in content string (some endpoints)
        if "data:image" in fallback_content:
            match = re.search(r"base64,([A-Za-z0-9+/=]+)", fallback_content)
            if match:
                return self._save_b64(match.group(1)), ""
        if fallback_content.strip().startswith("http"):
            return self._save_url(fallback_content.strip()), ""

        return [], ""


class ImageService:
    def __init__(self, ctx, config):
        self.ctx = ctx
        self.config = config
        self.providers = {}

    def get_provider(self, name=None):
        if name and name not in ("endpoint", "openrouter"):
            return None
        from plugin.framework.config import get_api_config
        from plugin.framework.client.model_fetcher import get_image_model

        api_config = get_api_config(self.ctx).copy()
        cfg = self.config or {}
        api_config["model"] = (cfg.get("image_model") or "").strip() or get_image_model(self.ctx)
        return EndpointImageProvider(api_config, self.ctx)

    def generate_image(self, prompt, provider_name=None, status_callback=None, **kwargs):
        provider = self.get_provider(provider_name or "endpoint")
        if not provider:
            raise ValueError(f"Unknown provider: {provider_name}")

        # Merge configuration defaults with kwargs
        base_size = get_config_int(self.ctx, "image_base_size")
        steps = get_config_int(self.ctx, "image_steps")

        from typing import Any

        defaults: dict[str, Any] = {
            "width": base_size,
            "height": base_size,
            "steps": steps,
        }

        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v

        return provider.generate(prompt, status_callback=status_callback, **kwargs)
