# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""=PROMPT() execution handler (LLM); isolated from =PYTHON() / venv stack."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.async_stream import run_blocking_in_thread
from plugin.framework.client.errors import format_error_for_display
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config import get_api_config, get_config, get_config_int, get_config_str

log = logging.getLogger(__name__)


def execute_prompt_addin(
    ctx: Any,
    message: str,
    system_prompt: Any,
    model: Any,
    max_tokens: Any,
    *,
    client_holder: list[LlmClient | None],
) -> str:
    """Call the chat API for =PROMPT(); *client_holder* is a one-element list for reuse across recalcs."""
    log.debug("=== PROMPT(%s) ===", message)
    try:
        if system_prompt is not None:
            resolved_system = str(system_prompt)
        else:
            resolved_system = get_config_str(ctx, "extend_selection_system_prompt")
            if not str(resolved_system).strip():
                from plugin.framework.constants import CALC_PYTHON_FORMULA_LLM_HINT

                resolved_system = CALC_PYTHON_FORMULA_LLM_HINT
        model_name = model if model is not None else (get_config(ctx, "text_model") or get_config(ctx, "model") or "")
        if max_tokens is not None:
            try:
                resolved_max = int(max_tokens)
            except (TypeError, ValueError):
                resolved_max = 70
        else:
            resolved_max = get_config_int(ctx, "calc_prompt_max_tokens")

        messages: list[dict[str, str]] = []
        if resolved_system:
            messages.append({"role": "system", "content": resolved_system})
        messages.append({"role": "user", "content": message})

        config = get_api_config(ctx)
        if model is not None:
            config = dict(config, model=str(model_name))

        client = client_holder[0]
        if client is None:
            client = LlmClient(config, ctx)
            client_holder[0] = client
        else:
            client.config = config

        return run_blocking_in_thread(ctx, client.chat_completion_sync, messages, max_tokens=resolved_max)
    except Exception as e:
        log.error("PROMPT error: %s", e)
        return format_error_for_display(e)
