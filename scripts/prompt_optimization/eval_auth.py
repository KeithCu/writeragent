# WriterAgent - eval CLI credential resolution (no writeragent.json).
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Resolve API endpoint, key, and judge model from CLI flags and environment.

Used by benchmark CLI and run_eval_multi so eval runs are pure command-line
parameters fed into LlmClient via llm_chat_eval._build_api_config.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Sequence

DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_JUDGE = "x-ai/grok-4.1-fast"


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


def require_api_key(api_key: str, endpoint: str) -> None:
    """Exit with a clear message if the endpoint requires auth and no key was given."""
    from plugin.framework.client.auth import AuthError, resolve_auth

    cfg: dict[str, Any] = {
        "endpoint": endpoint,
        "api_key": api_key,
        "is_openrouter": is_openrouter_endpoint(endpoint),
    }
    try:
        resolve_auth(cfg)
    except AuthError as exc:
        print(
            f"Error: {exc}\n"
            "Set --api-key / -k or export WRITERAGENT_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY.",
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
