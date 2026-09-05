# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval system prompt is the production chat builder plus the eval note."""

from plugin.framework.prompts import (
    TOOL_USAGE_PATTERNS,
    WRITER_REVIEW_MODES_RULES,
    get_chat_system_prompt_for_document,
)
from scripts.prompt_optimization.eval_prompts import (
    EVAL_HARNESS_NOTE,
    _stub_calc,
    _stub_draw,
    _stub_writer,
    get_calc_eval_chat_system_prompt,
    get_draw_eval_chat_system_prompt,
    get_writer_eval_chat_system_prompt,
)


def test_writer_eval_prompt_equals_production_plus_note() -> None:
    stub = _stub_writer()
    production = get_chat_system_prompt_for_document(stub, "", ctx=None)
    eval_p = get_writer_eval_chat_system_prompt()
    assert eval_p == get_chat_system_prompt_for_document(stub, EVAL_HARNESS_NOTE, ctx=None)
    assert eval_p.startswith(production)
    assert EVAL_HARNESS_NOTE in eval_p
    assert TOOL_USAGE_PATTERNS in eval_p
    assert WRITER_REVIEW_MODES_RULES in eval_p
    assert "delegate_to_specialized_writer_toolset" in eval_p
    assert "APPLY_DOCUMENT_CONTENT" in eval_p or "HTML" in eval_p
    assert "Only get_document_content" not in eval_p


def test_calc_draw_eval_prompts_use_production_builder() -> None:
    assert get_calc_eval_chat_system_prompt() == get_chat_system_prompt_for_document(
        _stub_calc(), EVAL_HARNESS_NOTE, ctx=None
    )
    assert get_draw_eval_chat_system_prompt() == get_chat_system_prompt_for_document(
        _stub_draw(), EVAL_HARNESS_NOTE, ctx=None
    )
    calc = get_calc_eval_chat_system_prompt()
    assert "write_formula_range" in calc
    assert EVAL_HARNESS_NOTE in calc
    draw = get_draw_eval_chat_system_prompt()
    assert "get_draw_tree" in draw
    assert EVAL_HARNESS_NOTE in draw


# gpt-oss-20b data_sorting / tax_column routing (docs/eval/oss-20b-eval.md).
# Rule shape: Don't X because Y; do Z instead. Shared prompt — no 20b fork.
_CALC_SORT_ROUTING = (
    "Don't sort or reorder rows with write_formula_range or =PY because that "
    "overwrites the range (including headers) and fights Calc's own sort; do "
    'delegate_to_specialized_calc_toolset(domain="ranges") then sort_range instead '
    "(multi-key sorts are two stable one-column passes)."
)
_CALC_RELATIVE_FORMULA = (
    "Don't copy one prototype formula onto every fill row because cell refs stay "
    "pinned to the first row; do write each row's formula with that row's cells "
    "instead (e.g. Banana row uses B3, not a stamped B2)."
)
_WRITE_FORMULA_RANGE_SORT_ONLY = (
    "Don't use this tool to reorder rows because it overwrites the range "
    "(including headers); do delegate_to_specialized_calc_toolset"
    '(domain="ranges") then sort_range instead.'
)


def test_calc_eval_prompt_pins_sort_and_relative_formula_rules() -> None:
    calc = get_calc_eval_chat_system_prompt()
    assert _CALC_SORT_ROUTING in calc
    assert _CALC_RELATIVE_FORMULA in calc
    # Writer/Draw needle lines stay out of this shared Calc prompt.
    assert "NEMA 4" not in calc
    assert "flowchart" not in calc.lower()


def test_calc_tool_descriptions_pin_sort_routing_not_tax() -> None:
    from plugin.calc.cells import SortRange, WriteCellRange

    write_desc = WriteCellRange.description
    assert _WRITE_FORMULA_RANGE_SORT_ONLY in write_desc
    assert "Banana" not in write_desc
    assert "stamped B2" not in write_desc
    sort_desc = SortRange.description
    assert "Stable one-column sort" in sort_desc
    assert "Multi-key sorts are multiple calls" in sort_desc
    assert "two stable one-column passes" in sort_desc
