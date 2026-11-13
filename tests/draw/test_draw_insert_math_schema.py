# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON schema validation tests for InsertMathDraw."""

from __future__ import annotations

import jsonschema
import pytest

from plugin.draw.math_insert import InsertMathDraw


def test_insert_math_schema_is_valid_json_schema() -> None:
    schema = InsertMathDraw().parameters
    assert isinstance(schema, dict)
    jsonschema.Draft7Validator.check_schema(schema)


def test_insert_math_schema_accepts_latex_payload() -> None:
    schema = InsertMathDraw().parameters
    assert schema is not None
    jsonschema.validate(
        {
            "formula_type": "latex",
            "formula": "E = mc^2",
            "page": 0,
            "x": 2000,
            "y": 2000,
        },
        schema,
    )


def test_insert_math_schema_accepts_mathml_payload() -> None:
    schema = InsertMathDraw().parameters
    assert schema is not None
    mml = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>'
    jsonschema.validate(
        {
            "formula_type": "mathml",
            "formula": mml,
            "page": 0,
            "x": 100,
            "y": 100,
        },
        schema,
    )


def test_insert_math_schema_rejects_missing_page() -> None:
    schema = InsertMathDraw().parameters
    assert schema is not None
    with pytest.raises(jsonschema.ValidationError, match="page"):
        jsonschema.validate(
            {
                "formula_type": "latex",
                "formula": "a",
                "x": 0,
                "y": 0,
            },
            schema,
        )
