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
# Teachings stay; shape is split so flash models cannot merge three adjacent
# identical Do-because lines. Because-clause text is PR 616's (no write tools).
_CALC_SORT_ROUTING = (
    'Do delegate_to_specialized_calc_toolset(domain="ranges") then sort_range '
    "to reorder rows (multi-key sorts are two stable one-column passes) "
    "because rewriting values by hand loses the header row."
)
_CALC_HAS_HEADER = (
    "When row 1 is labels, pass has_header=true — otherwise labels sort as values."
)
_CALC_RELATIVE_FORMULA = (
    "Do write each row's formula with that row's cells because copying one "
    "prototype pins cell refs to the first row (e.g. Banana row uses B3, not a "
    "stamped B2)."
)
_SORT_RANGE_HAS_HEADER = (
    "Do pass has_header=true when row 1 is labels because otherwise labels "
    "sort as values."
)


def test_calc_eval_prompt_pins_sort_and_relative_formula_rules() -> None:
    from plugin.framework.prompts import CALC_CORE_DIRECTIVES, CALC_WORKFLOW

    calc = get_calc_eval_chat_system_prompt()
    assert _CALC_SORT_ROUTING in calc
    assert _CALC_HAS_HEADER in calc
    assert _CALC_RELATIVE_FORMULA in calc
    # Naming write_formula_range / =PY in the because-clause primed weak
    # models to call the tool we want them to avoid (glm-5.3-flash).
    sort_line = next(line for line in calc.splitlines() if "then sort_range" in line)
    assert "write_formula_range" not in sort_line
    assert "=PY" not in sort_line
    # Headers + different shapes: not three adjacent identical Do-because lines.
    assert "FORMULAS:" in CALC_CORE_DIRECTIVES
    assert "SORT:" in CALC_CORE_DIRECTIVES
    assert "Do pass has_header=true" not in CALC_CORE_DIRECTIVES
    formulas_at = CALC_CORE_DIRECTIVES.index("FORMULAS:")
    sort_at = CALC_CORE_DIRECTIVES.index("SORT:")
    assert formulas_at < sort_at
    # Slim surface: sort routing lives in CALC_CORE_DIRECTIVES only.
    assert _CALC_SORT_ROUTING not in CALC_WORKFLOW
    assert _CALC_HAS_HEADER not in CALC_WORKFLOW
    # Writer/Draw needle lines stay out of this shared Calc prompt.
    assert "NEMA 4" not in calc
    assert "flowchart" not in calc.lower()


def test_calc_tool_descriptions_pin_sort_has_header_not_tax() -> None:
    from plugin.calc.cells import SortRange, WriteCellRange

    write_desc = WriteCellRange.description
    assert "sort_range" not in write_desc
    assert "Banana" not in write_desc
    assert "stamped B2" not in write_desc
    sort_desc = SortRange.description
    assert "Stable one-column sort" in sort_desc
    assert "Multi-key sorts are multiple calls" in sort_desc
    assert "two stable one-column passes" in sort_desc
    assert _SORT_RANGE_HAS_HEADER in sort_desc
    assert SortRange.parameters["required"] == ["range", "has_header"]
    assert "true when row 1 is labels" in SortRange.parameters["properties"]["has_header"]["description"]
    assert "false only for a headerless block" in SortRange.parameters["properties"]["has_header"]["description"]
