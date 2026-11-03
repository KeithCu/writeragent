# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Public layout vocabulary shared by notebook import, controls, and runner."""

from __future__ import annotations

from plugin.notebook.cell_registry import (
    _IN_PROMPT_RE as IN_PROMPT_RE,
    _coerce_notebook_text as coerce_notebook_text,
    _format_in_prompt as format_in_prompt,
    _prepare_display_text as prepare_display_text,
)
from plugin.notebook.notebook_controls import (
    _CODE_FONT_NAME as CODE_FONT_NAME,
    _cap_from_style as cap_from_style,
    _doc_key as doc_key,
    _height_for_text as height_for_text,
    _insert_code_input_in_flow as insert_code_input_in_flow,
    _insert_run_button_in_flow as insert_run_button_in_flow,
    _mono_ms as mono_ms,
    _page_style as page_style,
    _text_area_width_units as text_area_width_units,
    _text_width_from_style as text_width_from_style,
)

STYLE_MD_H1 = "Heading 1"
STYLE_MD_H2 = "Heading 2"
STYLE_NOTEBOOK_IN = "WriterAgent Notebook In"
STYLE_NOTEBOOK_OUT = "WriterAgent Notebook Output"
PARAGRAPH_BREAK = 0  # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK

__all__ = [
    "CODE_FONT_NAME",
    "IN_PROMPT_RE",
    "PARAGRAPH_BREAK",
    "STYLE_MD_H1",
    "STYLE_MD_H2",
    "STYLE_NOTEBOOK_IN",
    "STYLE_NOTEBOOK_OUT",
    "cap_from_style",
    "coerce_notebook_text",
    "doc_key",
    "format_in_prompt",
    "height_for_text",
    "insert_code_input_in_flow",
    "insert_run_button_in_flow",
    "mono_ms",
    "page_style",
    "prepare_display_text",
    "text_area_width_units",
    "text_width_from_style",
]
