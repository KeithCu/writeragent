# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PROMPT() execution handler (LLM); isolated from =PYTHON() / venv stack."""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

from plugin.framework.blocking_wait import run_blocking_in_thread
from plugin.framework.errors import WriterAgentException, format_error_for_display
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config import get_api_config, get_config_int, get_config_str
from plugin.framework.thread_guard import background_if_dummy_thread, sync_host_dispatch

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
    details = usage_dict.get("completion_tokens_details")
    reasoning_tokens = usage_dict.get("reasoning_tokens")
    if reasoning_tokens is None and isinstance(details, dict):
        reasoning_tokens = details.get("reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = "?"
    used_model = str(result.get("model") or model)
    parts = [
        f"finish_reason={result.get('finish_reason')!r}",
        f"completion_tokens={usage_dict.get('completion_tokens', '?')}",
        f"reasoning_tokens={reasoning_tokens}",
        f"model={used_model}",
    ]

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


def _optional_text(value: Any) -> str:
    """Blank/unset cell values (None, "" and numeric 0) mean "use the default"."""
    if value is None or isinstance(value, bool) or isinstance(value, (list, tuple, dict)):
        return ""
    if isinstance(value, (int, float)):
        return "" if not math.isfinite(value) or value == 0 else str(value).strip()
    return str(value).strip()


def _optional_max_tokens(value: Any) -> int:
    """Only a finite positive int >= 100 wins; everything else uses the configured default."""
    default = get_config_int("calc_prompt_max_tokens")
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if n >= 100 else default


def _call_prompt_llm(ctx: Any, client: LlmClient, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
    # =PY() does not use run_blocking_in_thread: processEventsToIdle on the
    # recalc stack re-enters the formula engine (#VALUE!). Same constraint here.
    result = run_blocking_in_thread(
        ctx,
        client.request_with_tools,
        messages,
        max_tokens=max_tokens,
        tools=None,
        stream=False,
        pump_idle=False,
        prepend_dev_build_system_prefix=False,
    )
    return result if isinstance(result, dict) else {}


def execute_prompt_addin(ctx: Any, message: str, system_prompt: Any, model: Any, max_tokens: Any, *, client_holder: list[LlmClient | None]) -> str:
    """Call the chat API for =PROMPT(); *client_holder* is a one-element list for reuse across recalcs."""
    with background_if_dummy_thread("prompt_addin"):
        with sync_host_dispatch():
            return _execute_prompt_addin_impl(ctx, message, system_prompt, model, max_tokens, client_holder=client_holder)


def _execute_prompt_addin_impl(ctx: Any, message: str, system_prompt: Any, model: Any, max_tokens: Any, *, client_holder: list[LlmClient | None]) -> str:
    # NOTE: We do not recommend HTML formatting in the system prompt for cell calculations
    # (unlike the sidebar chat window which supports rich HTML). Thus, we do not strip HTML
    # tags here. If users see raw tags in cells, they can prompt for plain text output.
    log.debug("=== PROMPT(%s) ===", message)
    try:
        if not str(message or "").strip():
            return "Error: prompt is empty."

        # An explicit empty system_prompt string means "no system message" (as before K032);
        # None / numeric 0 (empty-cell value) fall back to the configured default.
        if isinstance(system_prompt, str) and not system_prompt.strip():
            resolved_system = ""
        else:
            resolved_system = _optional_text(system_prompt) or get_config_str("extend_selection_system_prompt").strip() or CALC_PROMPT_CELL_SYSTEM_PROMPT

        config = get_api_config()
        model_name = _optional_text(model)
        if model_name:
            config = dict(config, model=model_name)
        else:
            model_name = str(config.get("model") or "")

        resolved_max = _optional_max_tokens(max_tokens)

        messages: list[dict[str, str]] = []
        if resolved_system:
            messages.append({"role": "system", "content": resolved_system})
        messages.append({"role": "user", "content": message})

        client = client_holder[0]
        if client is None:
            client = LlmClient(config, ctx, register_with_send=False)
            client_holder[0] = client
        else:
            client.config = config
            # Bugfix: A reused client may have been stopped by sidebar Stop (if previously registered
            # with send scope) or external cancellation; clear the stop latch so subsequent
            # cell calculations do not falsely report 'model returned no text'.
            client.clear_stop()

        result = _call_prompt_llm(ctx, client, messages, resolved_max)
        text = _assistant_text(result)
        if text.strip():
            return text

        # Reasoning models can burn max_tokens on reasoning and return content:null;
        # surface diagnostics so the cell is never a silent blank.
        diagnostic = _format_empty_prompt_diagnostic(result, model=model_name)
        log.warning("PROMPT empty response: finish_reason=%r model=%s", result.get("finish_reason"), result.get("model") or model_name)
        return diagnostic
    except Exception as e:
        if isinstance(e, WriterAgentException):
            log.warning("PROMPT function failed: %s", e)
        else:
            log.exception("PROMPT function execution failed")
        text = format_error_for_display(e)
        if len(text) > _EMPTY_DIAGNOSTIC_MAX_LEN:
            text = text[: _EMPTY_DIAGNOSTIC_MAX_LEN - 1] + "…"
        return text
