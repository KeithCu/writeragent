# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval-harness system prompts: production chat templates + one eval footnote.

Does not query the tool registry. Schemas live in ``eval_catalog``.
Does not call ``is_calc`` / ``get_document_type`` (UNO thread guard).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from plugin.framework.prompts import (
    CALC_CORE_DIRECTIVES,
    CHAT_RESPONSE_FORMAT,
    DRAW_CORE_DIRECTIVES,
    WRITER_CORE_DIRECTIVES,
    get_chat_response_format_instructions,
    get_specialized_delegation_tool_hint,
)

# The only eval-specific addendum. Production already appends additional_instructions.
EVAL_HARNESS_NOTE = (
    "[Eval harness] Core tools match chat. Tools the string harness does not "
    "implement return status=error code=unsupported_in_eval — recover or finish "
    "without them. Do not call domain=python."
)


def _stub_doc(service: str) -> Any:
    """UNO-shaped stub for tests that compare against ``get_chat_system_prompt_for_document``."""
    doc = MagicMock()
    doc.supportsService = lambda svc, want=service: svc == want
    return doc


def _stub_writer() -> Any:
    return _stub_doc("com.sun.star.text.TextDocument")


def _stub_calc() -> Any:
    return _stub_doc("com.sun.star.sheet.SpreadsheetDocument")


def _stub_draw() -> Any:
    return _stub_doc("com.sun.star.drawing.DrawingDocument")


def _prompt_for_kind(kind: str) -> str:
    """Same assembly as ``get_chat_system_prompt_for_document`` with ``ctx=None``."""
    from plugin.framework import prompts as pr

    pr._ensure_venv_import_policy_strings()
    if kind == "calc":
        from plugin.calc.base import ToolCalcSpecialBase

        template = pr.DEFAULT_CALC_CHAT_SYSTEM_PROMPT_TEMPLATE
        directives = CALC_CORE_DIRECTIVES
        delegation = get_specialized_delegation_tool_hint(ToolCalcSpecialBase, "Calc", ctx=None)
    elif kind == "draw":
        from plugin.draw.base import ToolDrawSpecialBase

        template = pr.DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE
        directives = DRAW_CORE_DIRECTIVES
        delegation = get_specialized_delegation_tool_hint(ToolDrawSpecialBase, "Draw", ctx=None)
    else:
        from plugin.writer.specialized_base import ToolWriterSpecialBase

        template = pr.DEFAULT_CHAT_SYSTEM_PROMPT_TEMPLATE
        directives = WRITER_CORE_DIRECTIVES
        delegation = get_specialized_delegation_tool_hint(ToolWriterSpecialBase, "Writer", ctx=None)
    base = template.replace("{specialized_delegation}", delegation)
    base = base.replace("{core_directives}", directives)
    base = base.replace(CHAT_RESPONSE_FORMAT, get_chat_response_format_instructions(None))
    return base + "\n\n" + EVAL_HARNESS_NOTE


def get_writer_eval_chat_system_prompt() -> str:
    return _prompt_for_kind("writer")


def get_calc_eval_chat_system_prompt() -> str:
    return _prompt_for_kind("calc")


def get_draw_eval_chat_system_prompt() -> str:
    return _prompt_for_kind("draw")


def get_eval_system_prompt(task_id: str = "") -> str:
    from dataset import task_kind

    return _prompt_for_kind(task_kind(task_id))


def replace_prompt_slice(prompt: str, original: str, replacement: str) -> str:
    """Swap one exact fragment in an assembled eval prompt.

    MIPROv2 proposes replacements for a named slice (e.g. CALC_CORE), not
    the whole ambient prompt. Missing baseline means this task kind does
    not carry that fragment — leave the prompt unchanged so Writer rows
    stay stable during a Calc-slice run.
    """
    if not original or original == replacement:
        return prompt
    if original not in prompt:
        return prompt
    return prompt.replace(original, replacement, 1)
