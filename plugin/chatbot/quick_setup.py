"""Endpoint connection check for Settings (Test Connection)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from plugin.framework.client.model_fetcher import (
    _model_fetch_auth_headers,
    _parse_v1_models_response,
    _v1_models_request,
)
from plugin.framework.client.provider_detection import is_openrouter_endpoint, is_openwebui_endpoint
from plugin.framework.config import get_config_bool_safe
from plugin.framework.constants import USER_AGENT
from plugin.framework.i18n import _


log = logging.getLogger(__name__)


def check_endpoint_connection(endpoint: str, api_key: str = "") -> tuple[bool, str, list[str]]:
    """Test connection to endpoint and return (success, message, models_list)."""
    if not endpoint:
        return False, _("Endpoint URL cannot be empty."), []

    # Same model-list URL and auth headers as fetch_available_models.
    built = _v1_models_request(endpoint, api_key)
    if built is None:
        return False, _("Invalid endpoint URL."), []
    base, models_url, _cache_key = built

    auth_headers = _model_fetch_auth_headers(
        base,
        api_key,
        models_url,
        is_openwebui=is_openwebui_endpoint(endpoint, explicit_is_openwebui=get_config_bool_safe("is_openwebui")),
        is_openrouter=is_openrouter_endpoint(base, explicit_is_openrouter=get_config_bool_safe("is_openrouter")),
        log_label="check_endpoint_connection",
    )
    if auth_headers is None:
        return False, _("Authentication setup failed. Check the API key."), []
    headers = {"User-Agent": USER_AGENT, **auth_headers}

    start_time = time.monotonic()
    try:
        req = urllib.request.Request(models_url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                parsed = _parse_v1_models_response(data)
                models = parsed[0] if parsed is not None else []

                msg = _("✓ Connected successfully! ({0}ms, {1} models available)").format(elapsed_ms, len(models))
                return True, msg, models
            return False, _("Server returned HTTP {0}.").format(resp.status), []
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, _("Authentication failed (401 Unauthorized). Please check your API key."), []
        if e.code == 404:
            return False, _("Models endpoint not found (404). Check the endpoint URL."), []
        return False, _("HTTP Error {0}: {1}").format(e.code, e.reason), []
    except urllib.error.URLError as e:
        return False, _("Connection failed: {0}").format(e.reason), []
    except (OSError, ValueError) as e:
        return False, _("Error: {0}").format(e), []
