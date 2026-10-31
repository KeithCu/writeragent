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
# CrossHair uses this closed set.
_LO_FORMULA_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789=();,\"'<>:+-*/^&%$.!_[]{}# \t\n")
_PREPROCESS_CROSSHAIR = os.environ.get(CROSSHAIR_ENV) == "1"


def _deal_lo_formula_ok_pytest(formula: object) -> bool:
    return str_bounded(formula, DEAL_MAX_SOURCE)


def _deal_lo_formula_ok_crosshair(formula: object) -> bool:
    return isinstance(formula, str) and len(formula) <= DEAL_MAX_SOURCE and all(c in _LO_FORMULA_CHARS for c in formula)


_deal_lo_formula_ok = _deal_lo_formula_ok_crosshair if _PREPROCESS_CROSSHAIR else _deal_lo_formula_ok_pytest


# What was wrong: only "..." was tracked, so a ';' in a quoted sheet name
# ('My;Sheet'.A1) or an array constant ({1;2|3;4}) was rewritten to ','.
# Why this fixes it: single-quoted names and {...} are copied verbatim.
def _scan_quotes(s: str) -> str:
    out: list[str] = []
    i = 0
    quote = ""  # open text literal or quoted sheet name
    depth = 0  # inside {...}: ';' is the array column separator
    while i < len(s):
        ch = s[i]
        if quote:
            doubled = ch == quote and i + 1 < len(s) and s[i + 1] == quote
            if ch == quote and not doubled:
                quote = ""
            step = 2 if doubled else 1
        elif ch in ('"', "'"):
            quote = ch
            step = 1
        elif ch == "{":
            depth += 1
            step = 1
        elif ch == "}" and depth:
            depth -= 1
            step = 1
        elif ch == ";" and depth == 0:
            out.append(",")
            i += 1
            continue
        else:
            step = 1
        out.append(s[i : i + step])
        i += step
    return "".join(out)


@deal.pre(lambda s: _deal_lo_formula_ok(s))
@deal.post(lambda result: isinstance(result, bool))
def _no_unquoted_semicolon(s: str) -> bool:
    """True when every ``;`` in *s* is inside a text literal, a quoted sheet name, or an array constant (or none exist)."""
    return _scan_quotes(s) == s


# Two nested ensures (curly-quote scan + _no_unquoted_semicolon) skip
# under CrossHair; cheap str post and the implementation loop stay.
@deal.pre(lambda formula: _deal_lo_formula_ok(formula))
@deal.post(lambda result: isinstance(result, str))
@inverse_ensure(lambda formula, result: "\u201c" not in result and "\u201d" not in result)
@inverse_ensure(lambda formula, result: _no_unquoted_semicolon(result))
def normalize_lo_formula_for_parse(formula: str) -> str:
    """Map LO ``;`` argument separators to ``,`` for parse-only backends.

    Only replaces ``;`` outside double-quoted strings, single-quoted sheet
    names, and ``{...}`` array constants (the tokenizer uses ``;`` as the
    array column separator).
    """
    raw = normalize_formula_string(formula)
    if not raw:
        return raw
    return _scan_quotes(raw)
