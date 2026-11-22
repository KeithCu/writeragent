# WriterAgent - eval CLI credential resolution (no writeragent.json).
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Resolve API endpoint, key, and judge model from CLI flags and environment.

Used by benchmark CLI and run_eval_multi so eval runs are pure command-line
parameters fed into LlmClient via llm_chat_eval._build_api_config.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import sys
from typing import Any, Callable, Sequence

from model_configs import DEFAULT_EVAL_STUDENT_MODEL

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
# Same OpenRouter :nitro routing as the default student (retired grok-4.1-fast 404s).
OPENROUTER_DEFAULT_JUDGE = DEFAULT_EVAL_STUDENT_MODEL


def is_openrouter_endpoint(endpoint: str) -> bool:
    return "openrouter.ai" in (endpoint or "").lower()


def resolve_api_key(*, cli_key: str | None = None) -> str:
    """CLI -k > WRITERAGENT_API_KEY > OPENAI_API_KEY > OPENROUTER_API_KEY."""
    if cli_key and str(cli_key).strip():
        return str(cli_key).strip()
    for env_name in ("WRITERAGENT_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return ""


def resolve_api_base(*, cli_base: str | None = None) -> str:
    """CLI --api-base > WRITERAGENT_API_BASE > OPENAI_API_BASE > OpenRouter default."""
    if cli_base and str(cli_base).strip():
        return str(cli_base).strip()
    for env_name in ("WRITERAGENT_API_BASE", "OPENAI_API_BASE"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return DEFAULT_API_BASE


def _unwrap_openrouter_id(model: str) -> str:
    """Strip leading openrouter/ prefix and dynamic OpenRouter routing suffixes."""
    from plugin.framework.openrouter_model_id import resolve_openrouter_catalog_id

    mid = str(model or "").strip()
    if mid.startswith("openrouter/"):
        mid = mid[len("openrouter/") :]
    return resolve_openrouter_catalog_id(mid)


def is_anthropic_model_id(model: str) -> bool:
    """Check whether a model identifier represents an Anthropic Claude model."""
    mid = _unwrap_openrouter_id(model)
    if mid.startswith("anthropic/"):
        return len(mid) > len("anthropic/")
    if mid.startswith("claude-"):
        return len(mid) > len("claude-")
    return False


def native_anthropic_model_id(model: str) -> str:
    """Convert an OpenRouter or bare Claude model id to its native Anthropic name."""
    mid = _unwrap_openrouter_id(model)
    if mid.startswith("anthropic/"):
        mid = mid[len("anthropic/") :]
    # Drop static suffixes like :thinking or :extended not supported by native Anthropic.
    mid = mid.split(":", 1)[0]
    # OpenRouter slugs use 4.5; Anthropic ids use 4-5.
    return re.sub(r"(?<=\d)\.(?=\d)", "-", mid)


def is_gemini_model_id(model: str) -> bool:
    """Check whether a model identifier represents a Google Gemini model."""
    mid = _unwrap_openrouter_id(model)
    # Exclude google/gemma-* — Gemma uses a different tool-calling/pricing path and stays on OpenRouter.
    if mid.startswith("google/gemini-"):
        return len(mid) > len("google/gemini-")
    if mid.startswith("gemini-"):
        return len(mid) > len("gemini-")
    return False


def native_gemini_model_id(model: str) -> str:
    """Convert an OpenRouter or bare Gemini model id to its native Google name."""
    mid = _unwrap_openrouter_id(model)
    if mid.startswith("google/"):
        mid = mid[len("google/") :]
    # Drop static suffixes like :free or :thinking not supported by native Google.
    return mid.split(":", 1)[0]


@dataclass(frozen=True)
class NativeRoute:
    """Routing metadata for a provider whose models can bypass OpenRouter."""

    provider: str
    label: str
    base: str
    matches: Callable[[str], bool]
    native_id: Callable[[str], str]
    env_vars: tuple[str, ...]


_NATIVE_ROUTES: tuple[NativeRoute, ...] = (
    NativeRoute(
        provider="anthropic",
        label="Anthropic",
        base=ANTHROPIC_API_BASE,
        matches=is_anthropic_model_id,
        native_id=native_anthropic_model_id,
        env_vars=("EVAL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    ),
    NativeRoute(
        provider="google",
        label="Gemini",
        base=GEMINI_API_BASE,
        matches=is_gemini_model_id,
        native_id=native_gemini_model_id,
        env_vars=("EVAL_GEMINI_API_KEY", "GEMINI_API_KEY"),
    ),
)


def native_route_for(model: str) -> NativeRoute | None:
    """Return the native provider route for *model*, or None if not natively routed."""
    if not model:
        return None
    for route in _NATIVE_ROUTES:
        if route.matches(model):
            return route
    return None


def route_eval_llm(endpoint: str, api_key: str, model: str) -> tuple[str, str, str]:
    """Route an eval LLM call to native Anthropic or Google Gemini when appropriate.

    Each call's model identifier determines whether to dispatch to a native provider rather than
    the shared base. An existing non-OpenRouter endpoint base is never rewritten.
    """
    from plugin.framework.client.provider_detection import get_provider_from_endpoint

    route = native_route_for(model)
    if route is None:
        return endpoint, api_key, model

    env_key = next(
        (val for name in route.env_vars if (val := os.environ.get(name, "").strip())),
        "",
    )
    provider = get_provider_from_endpoint(endpoint)

    if provider == "openrouter":
        if env_key:
            return route.base, env_key, route.native_id(model)
        return endpoint, api_key, model

    if provider == route.provider:
        effective_key = api_key if (api_key and api_key.strip()) else env_key
        return endpoint, effective_key, route.native_id(model)

    return endpoint, api_key, model


def require_eval_keys(*, endpoint: str, api_key: str, models: Sequence[str]) -> None:
    """Validate that necessary API keys are present for the models being evaluated."""
    if not models:
        require_api_key(api_key, endpoint)
        return

    seen_pairs: set[tuple[str, str]] = set()
    for mid in models:
        base, key, _ = route_eval_llm(endpoint, api_key, mid)
        pair = (base, key)
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            require_api_key(key, base)


def require_api_key(api_key: str, endpoint: str) -> None:
    """Exit with a clear message if the endpoint requires auth and no key was given."""
    from plugin.framework.client.auth import AuthError, resolve_auth_for_config
    from plugin.framework.client.provider_detection import get_provider_from_endpoint

    cfg: dict[str, Any] = {
        "endpoint": endpoint,
        "api_key": api_key,
        "is_openrouter": is_openrouter_endpoint(endpoint),
    }
    try:
        resolve_auth_for_config(cfg)
    except AuthError as exc:
        provider = get_provider_from_endpoint(endpoint)
        route = next((r for r in _NATIVE_ROUTES if r.provider == provider), None)
        base_vars = ("WRITERAGENT_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
        env_names = (*route.env_vars, *base_vars) if route else base_vars
        hint = f"Set --api-key / -k or export {', '.join(env_names[:-1])}, or {env_names[-1]}."
        print(
            f"Error: {exc}\n{hint}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def resolve_judge_model(
    *,
    cli_judge: str | None,
    endpoint: str,
    model_ids: Sequence[str],
) -> str | None:
    """
    CLI --judge > WRITERAGENT_JUDGE_MODEL > OpenRouter default > first --models id.

    Returns None only when no judge should run (caller passes --no-judge).
    """
    if cli_judge and str(cli_judge).strip():
        return str(cli_judge).strip()
    env_judge = os.environ.get("WRITERAGENT_JUDGE_MODEL", "").strip()
    if env_judge:
        return env_judge
    if is_openrouter_endpoint(endpoint):
        return OPENROUTER_DEFAULT_JUDGE
    if model_ids:
        return model_ids[0]
    print(
        "Error: non-OpenRouter endpoint requires --judge or --models for the judge model.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def build_eval_api_config(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    max_tool_rounds: int = 25,
    request_timeout: int = 120,
) -> dict[str, Any]:
    """CLI params -> LlmClient config (same shape as llm_chat_eval)."""
    from llm_chat_eval import _build_api_config

    return _build_api_config(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        max_tool_rounds=max_tool_rounds,
        request_timeout=request_timeout,
    )
