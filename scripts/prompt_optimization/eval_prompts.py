# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval-harness system prompts: production chat builder + one eval footnote.

Does not query the tool registry. Schemas live in ``eval_catalog``.
Stubs answer ``supportsService`` only — no live document / ``get_document_type``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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
    from plugin.framework.prompts import get_chat_system_prompt_for_document

    if kind == "calc":
        model = _stub_calc()
    elif kind == "draw":
        model = _stub_draw()
    else:
        model = _stub_writer()
    return get_chat_system_prompt_for_document(model, EVAL_HARNESS_NOTE, ctx=None)


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
