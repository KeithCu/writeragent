# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize LibreOffice formula strings for Excel-oriented parsers."""

from __future__ import annotations

import os

from plugin.calc.python.formula_edit import normalize_formula_string
from plugin.framework.deal_shim import CROSSHAIR_ENV, DEAL_MAX_SOURCE, str_bounded, deal, inverse_ensure

# Quote-machine / typical formula alphabet. Pytest keeps Unicode (curly quotes);
# CrossHair uses this closed set (normalize_lo_formula_for_parse 3:59, 32877875221).
_LO_FORMULA_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "=();,\"'<>:+-*/^&%$.!_[]{}# \t\n"
)
_PREPROCESS_CROSSHAIR = os.environ.get(CROSSHAIR_ENV) == "1"


def _deal_lo_formula_ok_pytest(formula: object) -> bool:
    return str_bounded(formula, DEAL_MAX_SOURCE)


def _deal_lo_formula_ok_crosshair(formula: object) -> bool:
    return (
        isinstance(formula, str)
        and len(formula) <= DEAL_MAX_SOURCE
        and all(c in _LO_FORMULA_CHARS for c in formula)
    )


_deal_lo_formula_ok = _deal_lo_formula_ok_crosshair if _PREPROCESS_CROSSHAIR else _deal_lo_formula_ok_pytest


@deal.pre(lambda s: _deal_lo_formula_ok(s))
@deal.post(lambda result: isinstance(result, bool))
def _no_unquoted_semicolon(s: str) -> bool:
    """True when every ``;`` in *s* sits inside a Calc double-quoted string (or none exist)."""
    in_quote = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            if in_quote and i + 1 < len(s) and s[i + 1] == '"':
                i += 2
                continue
            in_quote = not in_quote
            i += 1
            continue
        if ch == ";" and not in_quote:
            return False
        i += 1
    return True


# Deep check-all run 32840960268: two nested ensures (curly-quote scan +
# _no_unquoted_semicolon) cost ~7+7+6 min. Skip under CrossHair; cheap str post
# and the implementation loop stay.
@deal.pre(lambda formula: _deal_lo_formula_ok(formula))
@deal.post(lambda result: isinstance(result, str))
@inverse_ensure(lambda formula, result: "\u201c" not in result and "\u201d" not in result)
@inverse_ensure(lambda formula, result: _no_unquoted_semicolon(result))
def normalize_lo_formula_for_parse(formula: str) -> str:
    """Map LO ``;`` argument separators to ``,`` for parse-only backends.

    Only replaces ``;`` outside double-quoted strings. Array literals ``{=…}``
    braces are not special-cased in v1 (rare in P1 corpus).
    """
    raw = normalize_formula_string(formula)
    if not raw:
        return raw

    out: list[str] = []
    i = 0
    in_quote = False
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            if in_quote and i + 1 < len(raw) and raw[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if ch == ";" and not in_quote:
            out.append(",")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)
