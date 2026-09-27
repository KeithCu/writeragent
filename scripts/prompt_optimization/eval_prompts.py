# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval-harness system prompts: production chat builder + one eval footnote.

Does not query the tool registry. Schemas live in ``eval_catalog``.
Kind-keyed assembly via ``get_chat_system_prompt_for_kind`` — no live document / ``get_document_type``. MagicMock stubs remain for tests that compare against the document-model builder.
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


def _prompt_for_kind(kind: str, note: str | None = None) -> str:
    """Same assembly as production chat, keyed by kind — no MagicMock / get_document_type.

    LoLane workers call this off ``_lo_thread``. The old MagicMock stub still
    entered ``@main_thread_only`` ``get_document_type`` (GUARD on → raise;
    GUARD off → warn while real UNO may be in flight on the shared bridge).
    """
    from plugin.framework.prompts import get_chat_system_prompt_for_kind

    return get_chat_system_prompt_for_kind(
        kind, note if note is not None else EVAL_HARNESS_NOTE, ctx=None
    )


# The string-harness note forbids domain=python (=PY rows). The flag is the
# one task that must take that path, on headless LO.
FLAG_HARNESS_NOTE = (
    "[Eval harness] This row is headless LibreOffice, not the string simulator. "
    'Call delegate_to_specialized_writer_toolset with domain="python". '
    "That agent must call delegate_tool_domains with domains including shapes, "
    "then one run_venv_python_script that places shapes with wa.shape.upsert "
    "at page scale (about 10000–20000 HMM wide). "
    "Do not use domain=images or LLM-only shape_upsert."
)


def get_writer_eval_chat_system_prompt() -> str:
    return _prompt_for_kind("writer")


def get_calc_eval_chat_system_prompt() -> str:
    return _prompt_for_kind("calc")


def get_draw_eval_chat_system_prompt() -> str:
    return _prompt_for_kind("draw")


def get_eval_system_prompt(task_id: str = "") -> str:
    from dataset import task_kind

    if task_id == "python_shapes_flag":
        return _prompt_for_kind("writer", FLAG_HARNESS_NOTE)
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
