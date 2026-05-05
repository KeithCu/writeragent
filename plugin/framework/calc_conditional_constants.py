# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure constants/helpers for Calc conditional formatting UNO operator codes.
# Kept outside ``plugin.modules.calc`` so unit tests avoid importing that
# package's ``__init__`` (which loads UNO-dependent re-exports).

"""LibreOffice ``ConditionOperator`` / ``ConditionOperator2`` code labels (no UNO import)."""

_CONDITION_OPERATOR_CODE_NAMES: tuple[str, ...] = ("NONE", "EQUAL", "NOT_EQUAL", "GREATER", "GREATER_EQUAL", "LESS", "LESS_EQUAL", "BETWEEN", "NOT_BETWEEN", "FORMULA", "DUPLICATE", "NOT_DUPLICATE")


def condition_operator_code_to_name(code: int) -> str:
    """Map UNO condition operator *code* (long) to a stable string label."""
    if 0 <= code < len(_CONDITION_OPERATOR_CODE_NAMES):
        return _CONDITION_OPERATOR_CODE_NAMES[code]
    return str(int(code))
