# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Provider-aware auth helpers for LLM HTTP clients.

This module centralizes how we:
- identify a provider from an endpoint URL / config flags
- turn an API key into the correct auth headers
- declare model ID conventions (slug vs bare) for combobox filtering

Model ID styles:
- ``slug``: OpenRouter and Together AI (``org/model`` ids)
- ``bare``: all other registered providers (``gpt-4o``, ``glm-5.2``, ``deepseek-chat``, …)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from plugin.framework.constants import APP_REFERER, APP_TITLE
from plugin.framework.url_utils import normalize_endpoint_url
from plugin.framework.client.provider_detection import (
    get_provider_from_endpoint,
    is_openrouter_endpoint,
)
from plugin.framework.errors import ConfigError
from plugin.framework.deal_shim import DEAL_MAX_TOKEN, DEAL_MAX_URL, UNDER_CROSSHAIR, ascii_bounded, str_bounded, deal


def reject_control_chars_in_api_key(api_key: str) -> str:
    """Raise if *api_key* would be unsafe in an HTTP header (CR/LF/other controls)."""
    if any(ord(c) < 32 for c in api_key):
        raise AuthError("API key contains invalid control characters", code="invalid_api_key")
    return api_key


class AuthError(ConfigError):
    """Structured auth error for provider/endpoint configuration problems."""

    def __init__(self, message: str, *, provider: str = "", code: Optional[str] = None) -> None:
        if code is None:
            code = "AUTH_ERROR"
        super().__init__(message, code=code, details={"provider": provider})
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
    # Model list / request ``model`` field style: ``bare`` (vendor id) or ``slug`` (org/model).
    model_id_style: str = "bare"


PROVIDERS: Dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        header_style="bearer",
        host_matches=("openrouter.ai",),
        model_id_style="slug",
        extra_headers={"HTTP-Referer": APP_REFERER, "X-Title": APP_TITLE},
    ),
    "together": ProviderConfig(id="together", name="Together AI", header_style="bearer", host_matches=("api.together.xyz", "together.xyz"), model_id_style="slug"),
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
        # Google's official OpenAI-compatible endpoint (/v1beta/openai) requires
        # standard "Authorization: Bearer <API_KEY>" headers.
        header_style="bearer",
        host_matches=("generativelanguage.googleapis.com",),
    ),
    "ollama": ProviderConfig(id="ollama", name="Ollama", header_style="none", host_matches=("localhost:11434", "127.0.0.1:11434", "ollama")),
    "zai": ProviderConfig(id="zai", name="Z.ai", header_style="bearer", host_matches=("api.z.ai", "z.ai")),
    "nvidia": ProviderConfig(id="nvidia", name="NVIDIA NIM", header_style="bearer", host_matches=("integrate.api.nvidia.com", "api.nvidia.com")),
    # Fallback for endpoints we don't recognize explicitly.
    "custom": ProviderConfig(id="custom", name="Custom", header_style="bearer", host_matches=()),
}


# Substring scan over host_matches: len=3 still ~41m (33211730747). CrossHair
# uses a finite endpoint enum (hit / miss / empty); pytest keeps charset+len.
_URL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._:/")
_DEAL_RESOLVE_ENDPOINTS = frozenset(("", "api.openai.com", "localhost:11434", "openrouter.ai"))
_PROVIDER_HINTS = frozenset(PROVIDERS)


def _deal_resolve_hint_ok_pytest(provider_hint: object) -> bool:
    return provider_hint is None or (isinstance(provider_hint, str) and ascii_bounded(provider_hint, DEAL_MAX_TOKEN))


def _deal_resolve_hint_ok_crosshair(provider_hint: object) -> bool:
    return provider_hint is None or provider_hint in _PROVIDER_HINTS


_deal_resolve_hint_ok = _deal_resolve_hint_ok_crosshair if UNDER_CROSSHAIR else _deal_resolve_hint_ok_pytest


def _deal_resolve_endpoint_ok_pytest(endpoint: object) -> bool:
    return isinstance(endpoint, str) and ascii_bounded(endpoint, DEAL_MAX_URL) and all(c in _URL_CHARS for c in endpoint)


def _deal_resolve_endpoint_ok_crosshair(endpoint: object) -> bool:
    return endpoint in _DEAL_RESOLVE_ENDPOINTS


_deal_resolve_endpoint_ok = (
    _deal_resolve_endpoint_ok_crosshair if UNDER_CROSSHAIR else _deal_resolve_endpoint_ok_pytest
)


def _deal_provider_id_ok_pytest(provider_id: object) -> bool:
    return provider_id is None or str_bounded(provider_id, DEAL_MAX_TOKEN)


def _deal_provider_id_ok_crosshair(provider_id: object) -> bool:
    return provider_id is None or provider_id in _PROVIDER_HINTS


_deal_provider_id_ok = _deal_provider_id_ok_crosshair if UNDER_CROSSHAIR else _deal_provider_id_ok_pytest


@deal.pre(
    lambda endpoint, provider_hint=None: _deal_resolve_endpoint_ok(endpoint)
    and _deal_resolve_hint_ok(provider_hint)
)
def _resolve_provider_id(endpoint: str, provider_hint: Optional[str] = None) -> str:
    """
    Map an endpoint URL + optional hint to a provider id from PROVIDERS.
    Falls back to "custom" when nothing matches.
    """
    # crosshair: off  # substring fragment-in-url; finite endpoint enum still 61k examples (pre filters, does not construct). Doable later with a constructor domain (cover-all 33258921875).
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


@deal.pre(lambda provider_id: _deal_provider_id_ok(provider_id))
@deal.post(lambda result: isinstance(result, bool))
def provider_requires_api_key(provider_id: str | None) -> bool:
    """True when a known provider expects an API key (Bearer / x-api-key), not local/anonymous."""
    if not provider_id or provider_id == "custom":
        return False
    provider_cfg = PROVIDERS.get(provider_id)
    if not provider_cfg:
        return False
    return provider_cfg.header_style != "none"


@deal.pre(lambda provider_id: _deal_provider_id_ok(provider_id))
@deal.post(lambda result: isinstance(result, bool))
def provider_requires_slug_model_id(provider_id: str | None) -> bool:
    """True when combobox / LRU entries must use org/model slugs (OpenRouter, Together)."""
    if not provider_id:
        return False
    provider_cfg = PROVIDERS.get(provider_id)
    if not provider_cfg:
        return False
    return provider_cfg.model_id_style == "slug"


def resolve_auth_for_config(api_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve auth information from an API config dict.

    Design note: this function is intentionally similar in spirit to the
    provider resolution logic in Hermes Agent's auth module:
      https://github.com/NousResearch/hermes-agent/blob/main/hermes-agent/hermes_cli/auth.py
    If Hermes evolves its provider registry or detection heuristics, check
    that file when updating this helper so fixes can be ported across.

    The config is expected to come from plugin.framework.config.get_api_config()
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
    # crosshair: off
    endpoint_raw = str(api_config.get("endpoint") or "")
    is_owu = api_config.get("is_openwebui", False)
    endpoint = normalize_endpoint_url(endpoint_raw, is_openwebui=is_owu)
    api_key = str(api_config.get("api_key") or "").strip()

    if not endpoint:
        raise AuthError("No endpoint configured.", provider="", code="missing_endpoint")

    # Use the consolidated detection helpers (2026 provider heuristic cleanup).
    # This guarantees the same OpenRouter + provider logic used everywhere else
    # (model fetching, error messages, local SSL fallback, etc.).
    provider_hint: str | None
    if is_openrouter_endpoint(endpoint, explicit_is_openrouter=api_config.get("is_openrouter")):
        provider_hint = "openrouter"
    else:
        provider_hint = get_provider_from_endpoint(endpoint)

    provider_id = _resolve_provider_id(endpoint, provider_hint)
    provider_cfg = PROVIDERS.get(provider_id, PROVIDERS["custom"])

    # For well-known hosted providers (OpenRouter, OpenAI, etc.), an API key
    # is required and missing keys are treated as configuration errors.
    # For "custom" endpoints (typically local/self-hosted), an empty key is
    # allowed and we simply omit auth headers.
    if not api_key and provider_id != "custom" and provider_cfg.header_style != "none":
        raise AuthError(f"No API key configured for endpoint '{endpoint}'.", provider=provider_id, code="missing_api_key")

    return {"provider": provider_cfg.id, "endpoint": endpoint, "api_key": api_key, "header_style": provider_cfg.header_style, "headers": dict(provider_cfg.extra_headers)}


@deal.pre(lambda auth_info: isinstance(auth_info, dict))
@deal.post(lambda result: isinstance(result, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in result.items()))
def build_auth_headers(auth_info: Dict[str, Any]) -> Dict[str, str]:
    """
    Convert a resolved auth descriptor into concrete HTTP headers.

    Does NOT add WriterAgent-specific identification headers (those remain
    the responsibility of the caller, so they can be shared between API and
    other HTTP clients).
    """
    # crosshair: off
    headers: Dict[str, str] = {}
    # Coerce: callers/CrossHair may pass non-str header_style (e.g. int 2).
    style = str(auth_info.get("header_style") or "bearer").lower().strip()
    api_key = str(auth_info.get("api_key") or "").strip()
    if api_key:
        api_key = reject_control_chars_in_api_key(api_key)

    if style == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif style == "x-api-key" and api_key:
        headers["x-api-key"] = api_key
    # style == "none" -> no auth header

    # Merge any provider-specific static headers (e.g., version pins).
    # Plain dict only — isinstance(dict) is true for CrossHair AttrDict and .items() can crash.
    extra = auth_info.get("headers") or {}
    if type(extra) is dict:
        for k, v in extra.items():
            # Do not overwrite explicitly set auth headers.
            if k in headers:
                continue
            headers[str(k)] = str(v)

    return headers
