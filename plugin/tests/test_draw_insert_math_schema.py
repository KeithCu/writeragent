# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""insert_math JSON schema matches smolagents WrappedSmolTool validation."""

from __future__ import annotations

import pytest

from plugin.contrib.smolagents.tools import Tool, validate_tool_arguments
from plugin.modules.draw.math_insert import InsertMathDraw


def _smol_inputs_from_tool_parameters(writer_tool: object) -> dict:
    """Mirror plugin.framework.specialized_base.WrappedSmolTool.__init__."""
    inputs: dict = {}
    params = getattr(writer_tool, "parameters", {}) or {}
    props = params.get("properties", {})
    for param_name, spec in props.items():
        inputs[param_name] = {**spec}
        inputs[param_name]["type"] = spec.get("type", "any")
        inputs[param_name]["description"] = spec.get("description", "")
    return inputs


class _InsertMathSmolStubTool(Tool):
    """Minimal smolagents Tool for validate_tool_arguments only."""

    skip_forward_signature_validation = True

    def __init__(self, inputs: dict[str, dict]) -> None:
        self.name = "insert_math"
        self.description = "stub for schema validation"
        self.inputs = inputs
        self.output_type = "object"
        super().__init__()

    def forward(self, **kwargs):  # noqa: ARG002
        return {}


def _stub() -> _InsertMathSmolStubTool:
    return _InsertMathSmolStubTool(_smol_inputs_from_tool_parameters(InsertMathDraw()))


def test_insert_math_smolagents_validate_latex_payload() -> None:
    validate_tool_arguments(
        _stub(),
        {
            "formula_type": "latex",
            "formula": "E = mc^2",
            "page_index": 0,
            "x": 2000,
            "y": 2000,
        },
    )


def test_insert_math_smolagents_validate_mathml_payload() -> None:
    mml = "<math xmlns=\"http://www.w3.org/1998/Math/MathML\"><mi>x</mi></math>"
    validate_tool_arguments(
        _stub(),
        {
            "formula_type": "mathml",
            "formula": mml,
            "page_index": 0,
            "x": 100,
            "y": 100,
        },
    )


def test_insert_math_smolagents_missing_page_index_raises() -> None:
    with pytest.raises(ValueError, match="page_index"):
        validate_tool_arguments(
            _stub(),
            {
                "formula_type": "latex",
                "formula": "a",
                "x": 0,
                "y": 0,
            },
        )
