# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval-harness system prompts for ``scripts/prompt_optimization``.

Reuse production HTML / Calc dest rules. Core tools are advertised as in
chat; unimplemented names return ``unsupported_in_eval``.
"""
from __future__ import annotations

from plugin.framework.prompts import (
    CALC_CORE_DIRECTIVES,
    SIDEBAR_VS_DOCUMENT,
    TRANSLATION_RULES,
    WRITER_APPLY_DOCUMENT_HTML_RULES,
    WRITER_CHAT_TOOLS_SECTION,
)


EVAL_HARNESS_NOTE = (
    "[Eval harness] Core tools match chat. Tools the string harness does not "
    "implement return status=error code=unsupported_in_eval — recover or finish "
    "without them. Do not call domain=python."
)


def get_writer_eval_chat_system_prompt() -> str:
    """Writer chat-style system prompt for string eval."""
    return "\n\n".join(
        [
            SIDEBAR_VS_DOCUMENT,
            EVAL_HARNESS_NOTE,
            WRITER_CHAT_TOOLS_SECTION,
            TRANSLATION_RULES,
            WRITER_APPLY_DOCUMENT_HTML_RULES,
        ]
    )


def get_calc_eval_chat_system_prompt() -> str:
    """Calc prompt: dest / =PY rules from production plus the eval note."""
    return "\n\n".join(
        [
            EVAL_HARNESS_NOTE,
            CALC_CORE_DIRECTIVES,
            (
                "Use write_formula_range for values and for "
                '=PY("result = …"; DataRange) in an empty cell outside DataRange '
                "(e.g. J1 for A1:H500). Do not read the whole block into chat."
            ),
        ]
    )


def get_draw_eval_chat_system_prompt() -> str:
    return "\n\n".join(
        [
            EVAL_HARNESS_NOTE,
            "Use shape_upsert to create flowchart shapes and shape_connect for edges. "
            "Verify with get_draw_tree (connections, text, types) — no screenshots.",
        ]
    )


def get_eval_system_prompt(task_id: str = "") -> str:
    from dataset import task_kind

    kind = task_kind(task_id)
    if kind == "calc":
        return get_calc_eval_chat_system_prompt()
    if kind == "draw":
        return get_draw_eval_chat_system_prompt()
    return get_writer_eval_chat_system_prompt()
