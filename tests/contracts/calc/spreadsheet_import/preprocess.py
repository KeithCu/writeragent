"""Contracts sidecar for plugin.calc.spreadsheet_import.preprocess."""

from __future__ import annotations

import deal

from plugin.calc.spreadsheet_import import preprocess as _prod
from tests.contracts.bounds import DEAL_MAX_SOURCE, UNDER_CROSSHAIR, inverse_ensure, str_bounded

_LO_FORMULA_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789=();,\"'<>:+-*/^&%$.!_[]{}# \t\n")


def _deal_lo_formula_ok_pytest(formula: object) -> bool:
    return str_bounded(formula, DEAL_MAX_SOURCE)


def _deal_lo_formula_ok_crosshair(formula: object) -> bool:
    return isinstance(formula, str) and len(formula) <= DEAL_MAX_SOURCE and all(c in _LO_FORMULA_CHARS for c in formula)


_deal_lo_formula_ok = _deal_lo_formula_ok_crosshair if UNDER_CROSSHAIR else _deal_lo_formula_ok_pytest

normalize_lo_formula_for_parse = deal.pre(lambda formula: _deal_lo_formula_ok(formula))(
    deal.post(lambda result: isinstance(result, str))(
        inverse_ensure(lambda formula, result: "\u201c" not in result and "\u201d" not in result)(
            inverse_ensure(lambda formula, result: _prod._scan_quotes(result) == result)(
                _prod.normalize_lo_formula_for_parse
            )
        )
    )
)

PYTEST = [
    "normalize_lo_formula_for_parse",
]

TARGETS = [
    f"{__name__}.normalize_lo_formula_for_parse",
]
