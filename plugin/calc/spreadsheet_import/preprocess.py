# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize LibreOffice formula strings for Excel-oriented parsers."""

from __future__ import annotations

from plugin.calc.python.formula_edit import normalize_formula_string


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


def normalize_lo_formula_for_parse(formula: str) -> str:
    """Map LO ``;`` argument separators to ``,`` for parse-only backends.

    Only replaces ``;`` outside double-quoted strings, single-quoted sheet
    names, and ``{...}`` array constants (the tokenizer uses ``;`` as the
    array column separator).
    """
    raw = normalize_formula_string(formula)
    return _scan_quotes(raw)
