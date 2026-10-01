# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PROMPT() execution handler (LLM); isolated from =PYTHON() / venv stack."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from plugin.framework.async_stream import run_blocking_in_thread
from plugin.framework.client.errors import format_error_for_display
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config import get_api_config, get_config_int, get_config_str
from plugin.framework.client.model_fetcher import get_text_model
from plugin.framework.thread_guard import sync_host_dispatch

log = logging.getLogger(__name__)

# Default system prompt for Calc =PROMPT() when systemPrompt arg and extend_selection_system_prompt are empty.
CALC_PROMPT_CELL_SYSTEM_PROMPT = "Answer the user's request directly in plain text suitable for a spreadsheet cell. Do not use HTML or markdown fences unless the user asks for them."

# Cap diagnostic cell text so Calc stays readable when reasoning excerpts are long.
_EMPTY_DIAGNOSTIC_MAX_LEN = 500
_REASONING_EXCERPT_MAX = 200


def _assistant_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if content is None:
        return ""
    return str(content)


def _format_empty_prompt_diagnostic(result: Mapping[str, Any], *, model: str) -> str:
    """Visible cell message when the provider returned no assistant text (never a silent blank)."""
    usage = result.get("usage")
    usage_dict = usage if isinstance(usage, dict) else {}
    parts = [f"finish_reason={result.get('finish_reason')!r}", f"completion_tokens={usage_dict.get('completion_tokens', '?')}", f"reasoning_tokens={usage_dict.get('reasoning_tokens', '?')}", f"model={model}"]

    msg = "Error: model returned no text. " + "; ".join(parts) + "."
    for key in ("reasoning", "reasoning_content"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            excerpt = " ".join(val.split())
            if len(excerpt) > _REASONING_EXCERPT_MAX:
                excerpt = excerpt[:_REASONING_EXCERPT_MAX] + "…"
            msg += f" Reasoning excerpt: {excerpt}"
            break
    if len(msg) > _EMPTY_DIAGNOSTIC_MAX_LEN:
        msg = msg[: _EMPTY_DIAGNOSTIC_MAX_LEN - 1] + "…"
    return msg


def _call_prompt_llm(ctx: Any, client: LlmClient, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
    # =PY() does not use run_blocking_in_thread: processEventsToIdle on the
    # recalc stack re-enters the formula engine (#VALUE!). Same constraint here.
    result = run_blocking_in_thread(ctx, client.request_with_tools, messages, max_tokens=max_tokens, tools=None, stream=False, pump_idle=False)
    return result if isinstance(result, dict) else {}


def execute_prompt_addin(ctx: Any, message: str, system_prompt: Any, model: Any, max_tokens: Any, *, client_holder: list[LlmClient | None]) -> str:
    """Call the chat API for =PROMPT(); *client_holder* is a one-element list for reuse across recalcs."""
    with sync_host_dispatch():
        return _execute_prompt_addin_impl(ctx, message, system_prompt, model, max_tokens, client_holder=client_holder)


def _execute_prompt_addin_impl(ctx: Any, message: str, system_prompt: Any, model: Any, max_tokens: Any, *, client_holder: list[LlmClient | None]) -> str:
    # NOTE: We do not recommend HTML formatting in the system prompt for cell calculations
    # (unlike the sidebar chat window which supports rich HTML). Thus, we do not strip HTML
    # tags here. If users see raw tags in cells, they can prompt for plain text output.
    log.debug("=== PROMPT(%s) ===", message)
    try:
        if system_prompt is not None:
            resolved_system = str(system_prompt)
        else:
            resolved_system = get_config_str("extend_selection_system_prompt")
            if not str(resolved_system).strip():
                resolved_system = CALC_PROMPT_CELL_SYSTEM_PROMPT
        model_name = str(model) if model is not None else get_text_model()
        if max_tokens is not None:
            try:
                resolved_max = int(max_tokens)
            except (TypeError, ValueError):
                resolved_max = get_config_int("calc_prompt_max_tokens")
        else:
            resolved_max = get_config_int("calc_prompt_max_tokens")

        messages: list[dict[str, str]] = []
        if resolved_system:
            messages.append({"role": "system", "content": resolved_system})
        messages.append({"role": "user", "content": message})

        config = get_api_config()
        if model is not None:
            config = dict(config, model=str(model_name))

        client = client_holder[0]
        if client is None:
            client = LlmClient(config, ctx)
            client_holder[0] = client
        else:
            client.config = config

        result = _call_prompt_llm(ctx, client, messages, resolved_max)
        text = _assistant_text(result)
        if text.strip():
            return text

        # Reasoning models can burn max_tokens on reasoning and return content:null;
        # surface diagnostics so the cell is never a silent blank.
        diagnostic = _format_empty_prompt_diagnostic(result, model=model_name)
        log.warning("PROMPT empty response: %s", diagnostic)
        return diagnostic
    except Exception as e:
        log.exception("PROMPT function execution failed")
        return format_error_for_display(e)
