# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for Calc conditional formatting helpers (no UNO required)."""

import pytest

from plugin.framework.calc_conditional_constants import condition_operator_code_to_name


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "NONE"),
        (1, "EQUAL"),
        (9, "FORMULA"),
        (10, "DUPLICATE"),
        (11, "NOT_DUPLICATE"),
        (99, "99"),
    ],
)
def test_condition_operator_code_to_name(code: int, expected: str) -> None:
    assert condition_operator_code_to_name(code) == expected
