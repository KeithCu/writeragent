# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Construct ``ToolCallingAgent`` + ``WriterAgentSmolModel`` for librarian and specialized paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.framework.config import get_api_config, get_config_int
from plugin.framework.smol_model import WriterAgentSmolModel
from plugin.modules.http.client import LlmClient

if TYPE_CHECKING:
    from collections.abc import Sequence

    from plugin.contrib.smolagents.tools import Tool as SmolTool
    from plugin.framework.tool_context import ToolContext


def build_toolcalling_agent(ctx: ToolContext, tools: Sequence[SmolTool], *, instructions: str, final_answer_tool_name: str, examples_block: str, status_callback: object | None = None) -> ToolCallingAgent:
    """Shared construction for smolagents runs (same config as main chat: model, max_tokens, max_steps)."""
    uno_ctx = ctx.ctx
    config = get_api_config(uno_ctx)
    max_tokens = get_config_int(uno_ctx, "chat_max_tokens")
    max_steps = get_config_int(uno_ctx, "chat_max_tool_rounds")

    smol_model = WriterAgentSmolModel(LlmClient(config, uno_ctx), max_tokens=max_tokens, status_callback=status_callback)
    return ToolCallingAgent(tools=list(tools), model=smol_model, max_steps=max_steps, instructions=instructions, final_answer_tool_name=final_answer_tool_name, system_prompt_examples=examples_block)
