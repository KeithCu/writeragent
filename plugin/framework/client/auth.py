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
from typing import Any

from plugin.framework.constants import APP_REFERER, APP_TITLE
from plugin.framework.url_utils import normalize_endpoint_url
from plugin.framework.client.provider_detection import get_provider_from_endpoint, is_openrouter_endpoint
from plugin.framework.errors import ConfigError
from plugin.framework.deal_shim import DEAL_MAX_TOKEN, UNDER_CROSSHAIR, str_bounded, deal


class AuthError(ConfigError):
    """Structured auth error for provider/endpoint configuration problems."""

    provider: str

    def __init__(self, message: str, *, provider: str = "", code: str = "AUTH_ERROR") -> None:
        super().__init__(message, code=code, details={"provider": provider})
        self.provider = provider


@dataclass(frozen=True)
class ProviderConfig:
    """Describes a simple API-key based provider."""

    id: str
    # Header style controls how the API key is attached:
    # - "bearer"   -> Authorization: Bearer <key> (key required)
    # - "x-api-key" -> x-api-key: <key> (key required)
    # - "none"     -> key not required; empty key sends no auth header; a non-empty key becomes Authorization: Bearer in build_auth_headers (Ollama, LM Studio)
    header_style: str = "bearer"
    # Optional static headers that should always be sent for this provider.
    # Host matching lives in provider_detection. This table is auth behavior only.
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Model list / request ``model`` field style: ``bare`` (vendor id) or ``slug`` (org/model).
    model_id_style: str = "bare"


PROVIDERS: dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(id="openrouter", model_id_style="slug", extra_headers={"HTTP-Referer": APP_REFERER, "X-Title": APP_TITLE}),
    "together": ProviderConfig(id="together", model_id_style="slug"),
    "mistral": ProviderConfig(id="mistral"),
    "openai": ProviderConfig(id="openai"),
    "deepseek": ProviderConfig(id="deepseek"),
    "groq": ProviderConfig(id="groq"),
    "cerebras": ProviderConfig(id="cerebras"),
    "perplexity": ProviderConfig(id="perplexity"),
    "xai": ProviderConfig(id="xai"),
    "anthropic": ProviderConfig(id="anthropic", header_style="x-api-key", extra_headers={"anthropic-version": "2023-06-01"}),
    # Google's official OpenAI-compatible endpoint (/v1beta/openai) requires
    # standard "Authorization: Bearer ***" headers.
    "google": ProviderConfig(id="google"),
    "ollama": ProviderConfig(id="ollama", header_style="none"),
    # Local LM Studio accepts an empty key the way Ollama does. A key, when
    # set, still becomes Authorization: Bearer via build_auth_headers.
    "lmstudio": ProviderConfig(id="lmstudio", header_style="none"),
    "zai": ProviderConfig(id="zai"),
    "nvidia": ProviderConfig(id="nvidia"),
    # Fallback for endpoints we don't recognize explicitly.
    "custom": ProviderConfig(id="custom"),
}


_PROVIDER_HINTS = frozenset(PROVIDERS)


def _deal_provider_id_ok_pytest(provider_id: object) -> bool:
    return provider_id is None or str_bounded(provider_id, DEAL_MAX_TOKEN)


def _deal_provider_id_ok_crosshair(provider_id: object) -> bool:
    return provider_id is None or provider_id in _PROVIDER_HINTS


_deal_provider_id_ok = _deal_provider_id_ok_crosshair if UNDER_CROSSHAIR else _deal_provider_id_ok_pytest


@deal.pre(lambda provider_id: _deal_provider_id_ok(provider_id))
@deal.post(lambda result: isinstance(result, bool))
def provider_requires_api_key(provider_id: str | None) -> bool:
    """True when a known provider expects an API key (Bearer / x-api-key), not local/anonymous."""
    cfg = PROVIDERS.get(provider_id or "")
    return bool(cfg and provider_id != "custom" and cfg.header_style != "none")


@deal.pre(lambda provider_id: _deal_provider_id_ok(provider_id))
@deal.post(lambda result: isinstance(result, bool))
def provider_requires_slug_model_id(provider_id: str | None) -> bool:
    """True when combobox / LRU entries must use org/model slugs (OpenRouter, Together)."""
    cfg = PROVIDERS.get(provider_id or "")
    return bool(cfg and cfg.model_id_style == "slug")


def resolve_auth_for_config(api_config: dict[str, Any]) -> dict[str, Any]:
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

    if is_openrouter_endpoint(endpoint, explicit_is_openrouter=api_config.get("is_openrouter")):
        provider_id = "openrouter"
    else:
        # Hostname equality, not a substring. "ollama" inside notollama.example used to match and skip a required API key.
        provider_id = get_provider_from_endpoint(endpoint) or "custom"
    provider_cfg = PROVIDERS.get(provider_id, PROVIDERS["custom"])

    # For well-known hosted providers (OpenRouter, OpenAI, etc.), an API key
    # is required and missing keys are treated as configuration errors.
    # Check uses provider_requires_api_key with provider_cfg.id so fallback to
    # custom correctly permits an empty key.
    if not api_key and provider_requires_api_key(provider_cfg.id):
        raise AuthError(f"No API key configured for endpoint '{endpoint}'.", provider=provider_id, code="missing_api_key")

    return {"provider": provider_cfg.id, "endpoint": endpoint, "api_key": api_key, "header_style": provider_cfg.header_style, "headers": dict(provider_cfg.extra_headers)}


def build_auth_headers(auth_info: dict[str, Any]) -> dict[str, str]:
    """
    Convert a resolved auth descriptor into concrete HTTP headers.

    Does NOT add WriterAgent-specific identification headers (those remain
    the responsibility of the caller, so they can be shared between API and
    other HTTP clients).
    """
    # crosshair: off
    headers: dict[str, str] = {}
    # Coerce: callers/CrossHair may pass non-str header_style (e.g. int 2).
    style = str(auth_info.get("header_style") or "bearer").lower().strip()
    api_key = str(auth_info.get("api_key") or "").strip()
    if api_key:
        # What was wrong: Checking only ord(c) < 32 let DEL (\x7f), NBSP (\xa0),
        # and unicode chars like smart quotes through. Smart quotes caused
        # http.client putheader to fail with a confusing latin-1 UnicodeEncodeError.
        # Why this fixes it: Enforce printable ASCII (33 to 126), disallowing interior
        # spaces, DEL, and non-ASCII chars while code stays invalid_api_key.
        if any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise AuthError("API key must be printable ASCII", code="invalid_api_key")
        if style == "x-api-key":
            headers["x-api-key"] = api_key
        else:
            # What was wrong: Style "none" providers (Ollama / LM Studio) dropped the API
            # key on model fetching and /api/show because the fallback was previously only
            # in LlmClient._headers.
            # Why this fixes it: Consolidate here so all HTTP paths (chat, catalog
            # fetch, and /api/show probes) send the key. Null/empty keys remain header-less.
            headers["Authorization"] = f"Bearer {api_key}"

    # Merge any provider-specific static headers (e.g., version pins).
    extra = auth_info.get("headers") or {}
    existing = {h.lower() for h in headers}
    for k, v in extra.items():
        # Do not overwrite explicitly set auth headers.
        if str(k).lower() in existing:
            continue
        headers[str(k)] = str(v)
        existing.add(str(k).lower())

    return headers
