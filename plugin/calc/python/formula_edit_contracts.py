# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formal verification domains and contract predicates for formula_edit."""

from __future__ import annotations

from typing import Any

from plugin.framework.deal_shim import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    str_bounded,
)

# Sheet/A1 range tokens. Space and ``"`` are product (``My Sheet.A1``, quoted sheets).
_RANGE_ADDR_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!:'$_ \"")

_DEAL_UNQUOTED_CODE = ascii_bounded if UNDER_CROSSHAIR else str_bounded
_DEAL_SHEET_TOKEN = ascii_bounded if UNDER_CROSSHAIR else str_bounded


def _deal_range_addr_ok(s: object) -> bool:
    """Closed A1 / sheet-range domain for formatters and ``_deal_data_args_ok``."""
    return isinstance(s, str) and len(s) <= DEAL_MAX_TOKEN and all(c in _RANGE_ADDR_CHARS for c in s)


def _deal_data_args_ok(data_args: object) -> bool:
    """CrossHair domain for =PY() data-arg lists."""
    return (
        isinstance(data_args, list)
        and len(data_args) <= DEAL_MAX_SHAPE_DIM
        and all(_deal_range_addr_ok(x) for x in data_args)
    )


def _quoted_parse_result_ok(s: str, start: int, result: tuple[str, int] | None) -> bool:
    """Postcondition check for double-quoted string parser."""
    if result is None:
        return True
    code, end = result
    return isinstance(code, str) and isinstance(end, int) and 0 <= start < end <= len(s)


def _parts_result_ok(result: Any) -> bool:
    """Postcondition check for PythonFormulaParts parser."""
    if result is None:
        return True
    prefix = getattr(result, "prefix", None)
    code = getattr(result, "code", None)
    data_suffix = getattr(result, "data_suffix", None)
    return (
        isinstance(prefix, str)
        and bool(prefix)
        and isinstance(code, str)
        and isinstance(data_suffix, str)
        and data_suffix.endswith(")")
    )


__all__ = [
    "_DEAL_SHEET_TOKEN",
    "_DEAL_UNQUOTED_CODE",
    "_RANGE_ADDR_CHARS",
    "_deal_data_args_ok",
    "_deal_range_addr_ok",
    "_parts_result_ok",
    "_quoted_parse_result_ok",
]
