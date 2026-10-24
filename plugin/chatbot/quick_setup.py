"""Endpoint connection check for Settings (Test Connection)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT
from plugin.framework.url_utils import get_api_version_suffix, normalize_endpoint_url


log = logging.getLogger(__name__)


def check_endpoint_connection(endpoint: str, api_key: str = "") -> tuple[bool, str, list[str]]:
    """Test connection to endpoint and return (success, message, models_list)."""
    if not endpoint:
        return False, "Endpoint URL cannot be empty.", []

    endpoint_clean = endpoint.rstrip("/")
    # Build models probe URL
    if ":11434" in endpoint_clean:
        models_url = f"{endpoint_clean}/api/tags"
    else:
        norm_endpoint = normalize_endpoint_url(endpoint_clean)
        suffix = get_api_version_suffix(norm_endpoint)
        models_url = f"{norm_endpoint}{suffix}/models"

    headers = {"User-Agent": USER_AGENT}
    if "openrouter.ai" in endpoint_clean:
        headers["HTTP-Referer"] = APP_REFERER
        headers["X-Title"] = APP_TITLE

    if api_key:
        if "anthropic.com" in endpoint_clean:
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    start_time = time.monotonic()
    try:
        req = urllib.request.Request(models_url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models: list[str] = []
                if ":11434" in endpoint_clean and "models" in data:
                    models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                elif isinstance(data, dict) and "data" in data:
                    models = [m.get("id", "") for m in data["data"] if isinstance(m, dict) and m.get("id")]
                elif isinstance(data, list):
                    models = [m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")]

                msg = f"✓ Connected successfully! ({elapsed_ms}ms, {len(models)} models available)"
                return True, msg, models
            return False, f"Server returned HTTP {resp.status}.", []
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Authentication failed (401 Unauthorized). Please check your API key.", []
        if e.code == 404:
            return False, "Models endpoint not found (404). Check the endpoint URL.", []
        return False, f"HTTP Error {e.code}: {e.reason}", []
    except urllib.error.URLError as e:
        return False, f"Connection failed: {e.reason}", []
    except (OSError, ValueError) as e:
        return False, f"Error: {e}", []
