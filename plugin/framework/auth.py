"""
Provider-aware auth helpers for LLM HTTP clients.

This module centralizes how we:
- identify a provider from an endpoint URL / config flags
- turn an API key into the correct auth headers

It is intentionally data-driven so new providers can be added by extending
the registry below without changing the core logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from plugin.framework.config import get_provider_from_endpoint
from plugin.framework.utils import normalize_endpoint_url
from plugin.framework.errors import ConfigError


class AuthError(ConfigError):
    """Structured auth error for provider/endpoint configuration problems."""

    def __init__(self, message: str, *, provider: str = "", code: Optional[str] = None) -> None:
        if code is None:
            code = "AUTH_ERROR"
        super().__init__(message, code=code, context={"provider": provider})
        self.provider = provider


@dataclass(frozen=True)
class ProviderConfig:
    """Describes a simple API-key based provider."""

    id: str
    name: str
    # Header style controls how the API key is attached:
    # - "bearer"   -> Authorization: Bearer <key>
    # - "x-api-key" -> x-api-key: <key>
    # - "none"     -> no auth header (for fully anonymous/local endpoints)
    header_style: str = "bearer"
    # Hostname fragments used for auto-detection (e.g. "openrouter.ai").
    host_matches: Tuple[str, ...] = field(default_factory=tuple)
    # Optional static headers that should always be sent for this provider.
    extra_headers: Dict[str, str] = field(default_factory=dict)


PROVIDERS: Dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(id="openrouter", name="OpenRouter", header_style="bearer", host_matches=("openrouter.ai",)),
    "together": ProviderConfig(id="together", name="Together AI", header_style="bearer", host_matches=("api.together.xyz", "together.xyz")),
    "mistral": ProviderConfig(id="mistral", name="Mistral", header_style="bearer", host_matches=("api.mistral.ai",)),
    "openai": ProviderConfig(id="openai", name="OpenAI", header_style="bearer", host_matches=("api.openai.com",)),
    "deepseek": ProviderConfig(id="deepseek", name="DeepSeek", header_style="bearer", host_matches=("api.deepseek.com",)),
    "groq": ProviderConfig(id="groq", name="Groq", header_style="bearer", host_matches=("api.groq.com",)),
    "cerebras": ProviderConfig(id="cerebras", name="Cerebras", header_style="bearer", host_matches=("api.cerebras.ai",)),
    "perplexity": ProviderConfig(id="perplexity", name="Perplexity", header_style="bearer", host_matches=("api.perplexity.ai",)),
    "xai": ProviderConfig(id="xai", name="X.ai (Grok)", header_style="bearer", host_matches=("api.x.ai",)),
    "anthropic": ProviderConfig(id="anthropic", name="Anthropic Claude", header_style="x-api-key", host_matches=("api.anthropic.com",), extra_headers={"anthropic-version": "2023-06-01"}),
    "google": ProviderConfig(
        id="google",
        name="Google Gemini",
        # Google often uses ?key=KEY in URL, handled in client.py, but we set style=none
        # for headers to avoid Bearer interference.
        header_style="none",
        host_matches=("generativelanguage.googleapis.com",),
    ),
    "ollama": ProviderConfig(id="ollama", name="Ollama", header_style="none", host_matches=("localhost:11434", "127.0.0.1:11434", "ollama")),
    # Fallback for endpoints we don't recognize explicitly.
    "custom": ProviderConfig(id="custom", name="Custom", header_style="bearer", host_matches=()),
}


def _resolve_provider_id(endpoint: str, provider_hint: Optional[str] = None) -> str:
    """
    Map an endpoint URL + optional hint to a provider id from PROVIDERS.
    Falls back to "custom" when nothing matches.
    """
    if provider_hint:
        normalized = provider_hint.strip().lower()
        if normalized in PROVIDERS:
            return normalized

    url = normalize_endpoint_url(endpoint).lower()
    for pid, cfg in PROVIDERS.items():
        if not cfg.host_matches:
            continue
        if any(fragment in url for fragment in cfg.host_matches):
            return pid

    return "custom"


def resolve_auth_for_config(api_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve auth information from an API config dict.

    Design note: this function is intentionally similar in spirit to the
    provider resolution logic in Hermes Agent's auth module:
      https://github.com/NousResearch/hermes-agent/blob/main/hermes-agent/hermes_cli/auth.py
    If Hermes evolves its provider registry or detection heuristics, check
    that file when updating this helper so fixes can be ported across.

    The config is expected to come from plugin.framework.config.get_api_config(ctx)
    and must contain at least:
      - endpoint: str
      - api_key: str (may be empty)

    Returns a dict:
      {
        "provider": "<id>",
        "endpoint": "<normalized endpoint>",
        "api_key": "<api key>",
        "header_style": "<style>",
        "headers": { ... provider-specific static headers ... },
      }
    """
    endpoint_raw = str(api_config.get("endpoint") or "")
    endpoint = normalize_endpoint_url(endpoint_raw)
    api_key = str(api_config.get("api_key") or "").strip()

    if not endpoint:
        raise AuthError("No endpoint configured.", provider="", code="missing_endpoint")

    # Try explicit flags first (e.g. is_openrouter), then fall back to URL heuristics.
    provider_hint = None
    if api_config.get("is_openrouter"):
        provider_hint = "openrouter"
    else:
        provider_hint = get_provider_from_endpoint(endpoint) or None

    provider_id = _resolve_provider_id(endpoint, provider_hint)
    provider_cfg = PROVIDERS.get(provider_id, PROVIDERS["custom"])

    # For well-known hosted providers (OpenRouter, OpenAI, etc.), an API key
    # is required and missing keys are treated as configuration errors.
    # For "custom" endpoints (typically local/self-hosted), an empty key is
    # allowed and we simply omit auth headers.
    if not api_key and provider_id != "custom" and provider_cfg.header_style != "none":
        raise AuthError(f"No API key configured for endpoint '{endpoint}'.", provider=provider_id, code="missing_api_key")

    return {"provider": provider_cfg.id, "endpoint": endpoint, "api_key": api_key, "header_style": provider_cfg.header_style, "headers": dict(provider_cfg.extra_headers)}


def build_auth_headers(auth_info: Dict[str, Any]) -> Dict[str, str]:
    """
    Convert a resolved auth descriptor into concrete HTTP headers.

    Does NOT add WriterAgent-specific identification headers (those remain
    the responsibility of the caller, so they can be shared between API and
    other HTTP clients).
    """
    headers: Dict[str, str] = {}
    style = (auth_info.get("header_style") or "bearer").lower()
    api_key = str(auth_info.get("api_key") or "").strip()

    if style == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif style == "x-api-key" and api_key:
        headers["x-api-key"] = api_key
    # style == "none" -> no auth header

    # Merge any provider-specific static headers (e.g., version pins).
    extra = auth_info.get("headers") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            # Do not overwrite explicitly set auth headers.
            if k in headers:
                continue
            headers[str(k)] = str(v)

    return headers
