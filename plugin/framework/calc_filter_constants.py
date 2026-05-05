# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure helpers for Calc standard filter / AutoFilter UNO operator codes
# (``FilterOperator2``). Kept outside ``plugin.modules.calc`` so unit tests
# avoid importing that package's ``__init__``.

"""LibreOffice ``FilterOperator2`` code labels (no UNO import)."""

_FILTER_OPERATOR2_CODE_NAMES: tuple[str, ...] = (
    "EMPTY",
    "NOT_EMPTY",
    "EQUAL",
    "NOT_EQUAL",
    "GREATER",
    "GREATER_EQUAL",
    "LESS",
    "LESS_EQUAL",
    "TOP_VALUES",
    "TOP_PERCENT",
    "BOTTOM_VALUES",
    "BOTTOM_PERCENT",
    "CONTAINS",
    "DOES_NOT_CONTAIN",
    "BEGINS_WITH",
    "DOES_NOT_BEGIN_WITH",
    "ENDS_WITH",
    "DOES_NOT_END_WITH",
)

_NAME_TO_CODE: dict[str, int] = {name: idx for idx, name in enumerate(_FILTER_OPERATOR2_CODE_NAMES)}

# Stable tuple of all FilterOperator2 names (for tool JSON schemas).
FILTER_OPERATOR2_LABELS: tuple[str, ...] = _FILTER_OPERATOR2_CODE_NAMES


def filter_operator2_code_to_name(code: int) -> str:
    """Map UNO ``FilterOperator2`` *code* (long) to a stable string label."""
    if 0 <= code < len(_FILTER_OPERATOR2_CODE_NAMES):
        return _FILTER_OPERATOR2_CODE_NAMES[code]
    return str(int(code))


def filter_operator2_name_to_code(name: str) -> int | None:
    """Resolve case-insensitive operator name to code, or ``None`` if unknown."""
    key = name.strip().upper().replace("-", "_")
    return _NAME_TO_CODE.get(key)
