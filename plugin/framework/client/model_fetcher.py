"""
Logic for fetching available models from LLM endpoints.
"""
import urllib.parse
import ipaddress
import logging
from typing import Any

from plugin.framework.url_utils import normalize_endpoint_url, get_api_version_suffix
from plugin.framework.errors import NetworkError
from plugin.framework.config import get_api_key_for_endpoint, get_config_bool_safe, as_bool, get_config

log = logging.getLogger(__name__)

# GET {base}/v1/models — memoized for the lifetime of this Python process (LibreOffice
# session). Key is normalized URL, or ``url + "\\x1f" + api_key`` when ``ctx`` is passed
# (same host, different keys must not share cache). Value is model id list or None after failure.
_model_fetch_cache: dict[str, list[str] | None] = {}


def _model_fetch_cache_key(url: str, ctx: Any, base: str, api_key_override: str | None = None) -> str:
    if ctx is None:
        return url
    if api_key_override is not None:
        key = str(api_key_override).strip()
    else:
        key = str(get_api_key_for_endpoint(ctx, base) or "")
    return f"{url}\x1f{key}"


def endpoint_url_suitable_for_v1_models_fetch(endpoint: str) -> bool:
    """True if endpoint looks like a complete http(s) URL with a real host (skip mid-typing e.g. 'http:/')."""
    if not endpoint or not isinstance(endpoint, str):
        return False
    try:
        p = urllib.parse.urlparse(endpoint.strip())
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.hostname
    if not host:
        return False
    h = host.lower()
    if h == "localhost":
        return True
    if "." in h:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def fetch_available_models(endpoint, ctx=None, api_key_override: str | None = None):
    """Fetch available models from endpoint/v1/models. Returns list of IDs or None on error.

    When ``ctx`` is set, sends the same auth headers as chat (Bearer / x-api-key per provider)
    using ``get_api_key_for_endpoint(ctx, base)``. Pass ``api_key_override`` (including ``""``)
    to use a key not yet saved to config (e.g. Settings dialog typing order: URL then API key).

    When ``ctx`` is omitted, behavior matches legacy unauthenticated GET (tests / callers
    without context). ``api_key_override`` is ignored if ``ctx`` is ``None``.

    Responses (including failed lookups, stored as None) are cached in `_model_fetch_cache`
    for the process lifetime so repeated Settings/sidebar use does not re-hit the network.
    """
    if not endpoint:
        return None
    base = normalize_endpoint_url(endpoint)
    if not base:
        return None
    if not endpoint_url_suitable_for_v1_models_fetch(base):
        return None
    is_owu = get_config_bool_safe(ctx, "is_openwebui") if ctx else False
    suffix = get_api_version_suffix(base, is_openwebui=is_owu)
    url = f"{base}{suffix}/models"
    cache_key = _model_fetch_cache_key(url, ctx, base, api_key_override)
    if cache_key in _model_fetch_cache:
        return _model_fetch_cache[cache_key]

    req_headers: dict[str, str] = {}
    if ctx is not None:
        from plugin.framework.client.auth import AuthError, build_auth_headers, resolve_auth_for_config

        if api_key_override is not None:
            api_key = str(api_key_override).strip()
        else:
            api_key = str(get_api_key_for_endpoint(ctx, base) or "").strip()
        is_openwebui = as_bool(get_config(ctx, "is_openwebui")) or "open-webui" in base.lower() or "openwebui" in base.lower()
        is_openrouter = "openrouter.ai" in base.lower() or as_bool(get_config(ctx, "is_openrouter"))
        mini = {"endpoint": base, "api_key": api_key, "is_openwebui": is_openwebui, "is_openrouter": is_openrouter}
        try:
            req_headers = build_auth_headers(resolve_auth_for_config(mini))
        except AuthError as e:
            log.debug("fetch_available_models skipping %s: %s", url, e)
            _model_fetch_cache[cache_key] = None
            return None

    try:
        from plugin.framework.client.requests import sync_request
        data = sync_request(url, parse_json=True, headers=req_headers if req_headers else None)
        if data and isinstance(data, dict) and "data" in data:
            models = []
            for m in data["data"]:
                mid = m.get("id")
                if mid:
                    models.append(mid)
            _model_fetch_cache[cache_key] = models
            return models
    except (ValueError, TypeError, IOError) as e:
        log.warning("fetch_available_models network/parse error for %s: %s", url, e)
    except Exception as e:
        if isinstance(e, NetworkError):
            log.warning("fetch_available_models NetworkError for %s: %s", url, e)
        else:
            log.warning("fetch_available_models unexpected error for %s: %s", url, type(e).__name__)
    _model_fetch_cache[cache_key] = None
    return None

def _filter_fetched_models(models: list[str], req_cap: str) -> list[str]:
    """Filter raw model IDs from /v1/models based on the requested capability (text/image/audio)."""
    if not models:
        return []

    out = []
    if req_cap == "text":
        # Exclude known non-chat models (mirrors LibreAI C++ logic)
        exclude = {
            "embedding", "embed", "aqa", "attribution", "retrieval", "vision",
            "rerank", "classifier", "moderation", "whisper", "speech", "audio",
            "llava", "stable-diffusion", "sdxl", "dall", "aurora", "imagen",
            "codellama", "codegemma", "starcoder", "deepseek-coder", "coder"
        }
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in exclude):
                continue
            out.append(m)
    elif req_cap == "image":
        # Positive filter for image models (especially useful for Ollama/Custom endpoints)
        include = {"flux", "stable-diffusion", "sdxl", "dall-e", "aurora", "imagen", "dreamshaper", "playground", "juggernaut"}
        for m in models:
            m_lower = m.lower()
            if any(kw in m_lower for kw in include):
                out.append(m)
    else:
        # Audio/STT or other: no filtering yet
        out = list(models)
    return out
