# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Translate Excel-dialect formula text to LibreOffice Calc's API grammar.

Calc's UNO write entry points (``ScCellObj::setFormula``, ``setArrayFormula``,
``setFormulaArray`` in ``sc/source/ui/unoobj/cellsuno.cxx``, named-range
content in ``nameuno.cxx``, and conditional-format entries in ``fmtuno.cxx``)
all compile with ``GRAM_API`` (English function names, ``;`` argument
separator, ``Sheet.A1`` dot sheet references, ``{1;2|3;4}`` inline arrays).

``FormulaDialects.to_api`` parses incoming formula text with Calc's own
``com.sun.star.sheet.FormulaParser`` across three grammars in order and keeps
the first that parses cleanly:

1. Calc API grammar (``FormulaLanguage.API``, ``CONV_OOO``) as written —
   returned verbatim so anything already valid in Calc is untouched.
2. Excel English (``FormulaLanguage.XL_ENGLISH``, ``CONV_XL_A1``) after
   stripping ``_xlfn.`` / ``_xlws.`` prefixes — handles commas, ``Sheet!A1``,
   and ``{1,2;3,4}`` arrays.
3. English names with ``CONV_XL_A1`` and no custom ``OpCodeMap`` — handles
   mixed Calc punctuation with Excel sheet references such as
   ``=SUM(Orders!A1:A5;B1)``.

Note on Calc's ``FormulaParser`` quirks (see ``sc/source/ui/unoobj/tokenuno.cxx``
``ScFormulaParserObj::SetCompilerFlags`` and ``sc/source/core/tool/compiler.cxx``
``ScCompiler::ParseOpCode``):
- A custom ``OpCodeMap`` compiles under ``GRAM_EXTERNAL``, where ``;`` outside
  ``{...}`` is tokenized as ``ocArrayRowSep`` instead of ``ocBad``; ``accepted``
  rejects array separators when no ``{`` token is present.
- ``printFormula`` drops the leading ``=``, so ``to_api`` re-prepends ``=``
  when the input started with one.
- Both ``parseFormula`` and ``printFormula`` must share the same reference
  ``CellAddress`` so relative references stay position-independent.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import uno

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)

XL_ENGLISH = 4  # com.sun.star.sheet.FormulaLanguage.XL_ENGLISH
API_LANGUAGE = 6  # com.sun.star.sheet.FormulaLanguage.API (LibreOffice 5.3+)
CONV_OOO = 0  # com.sun.star.sheet.AddressConvention.OOO
CONV_XL_A1 = 1  # com.sun.star.sheet.AddressConvention.XL_A1
GROUP_SPECIAL = 0  # com.sun.star.sheet.FormulaMapGroup.SPECIAL
GROUP_ARRAY_SEPARATORS = 2  # com.sun.star.sheet.FormulaMapGroup.ARRAY_SEPARATORS
GROUP_ALL = 0x7FFFFFFF  # com.sun.star.sheet.FormulaMapGroup.ALL_EXCEPT_SPECIAL
SPECIAL_BAD = 7  # com.sun.star.sheet.FormulaMapGroupSpecialOffset.BAD

_XLFN_RE = re.compile(r"_xlfn\.(?:_xlws\.)?|_xlws\.", re.IGNORECASE)


def needs_dialect_check(text: str) -> bool:
    """Return True when *text* may contain Excel-only syntax.

    Every Excel-only construct (comma argument/array separators, ``Sheet!A1``
    sheet prefixes, or ``_xlfn.``/``_xlws.`` prefixes) contains ``,``, ``!``,
    or ``_xl`` (case-insensitive). Anything else is already Calc grammar (or a
    plain literal) and skips UNO parser calls.
    """
    return "," in text or "!" in text or "_xl" in text.lower()


def strip_xlfn(text: str) -> str:
    """Strip ``_xlfn.``, ``_xlfn._xlws.``, and ``_xlws.`` prefixes."""
    return _XLFN_RE.sub("", text)


def accepted(
    opcodes: Sequence[int],
    bad: int,
    array_open: int | None,
    array_seps: frozenset[int],
) -> bool:
    """Return True when *opcodes* has no bad token and no stray array separator."""
    if bad in opcodes:
        return False
    if array_open is None or array_open not in opcodes:
        if any(op in array_seps for op in opcodes):
            return False
    return True


class FormulaDialects:
    """Per-document ``FormulaParser`` cascade for accepting Excel-dialect formulas."""

    def __init__(self, doc: Any) -> None:
        self._doc: Any = doc
        self._initialized: bool = False
        self._bad: int = 0
        self._array_open: int | None = None
        self._array_seps: frozenset[int] = frozenset()
        self._api: Any = None
        self._excel: Any = None
        self._excel_refs: Any = None
        self._default_pos: Any = None

    def _ensure_parsers(self) -> None:
        if self._initialized:
            return
        mapper = self._doc.createInstance("com.sun.star.sheet.FormulaOpCodeMapper")
        self._bad = int(mapper.getAvailableMappings(XL_ENGLISH, GROUP_SPECIAL)[SPECIAL_BAD].Token.OpCode)
        seps = {
            str(entry.Name): int(entry.Token.OpCode)
            for entry in mapper.getAvailableMappings(XL_ENGLISH, GROUP_ARRAY_SEPARATORS)
        }
        self._array_open = seps.get("{")
        self._array_seps = frozenset(v for k, v in seps.items() if k in (";", ","))

        api_map = mapper.getAvailableMappings(API_LANGUAGE, GROUP_ALL)
        xl_map = mapper.getAvailableMappings(XL_ENGLISH, GROUP_ALL)
        self._api = self._make_parser(self._doc, CONV_OOO, api_map)
        self._excel = self._make_parser(self._doc, CONV_XL_A1, xl_map)
        self._excel_refs = self._make_parser(self._doc, CONV_XL_A1, None)
        self._default_pos = uno.createUnoStruct("com.sun.star.table.CellAddress")
        self._initialized = True

    @staticmethod
    def _make_parser(doc: Any, conv: int, opmap: Any) -> Any:
        parser = doc.createInstance("com.sun.star.sheet.FormulaParser")
        parser.CompileEnglish = True
        parser.FormulaConvention = conv
        if opmap is not None:
            parser.OpCodeMap = opmap
        return parser

    def _clean(self, parser: Any, text: str, pos: Any) -> bool:
        tokens = parser.parseFormula(text, pos)
        return accepted([int(t.OpCode) for t in tokens], self._bad, self._array_open, self._array_seps)

    def to_api(self, text: str, pos: Any = None, *, sheet: int | None = None) -> str:
        """Return *text* in Calc's API grammar if an Excel dialect parses cleanly.

        Returns *text* unchanged if it already parses in Calc API grammar, does
        not need a dialect check, or fails to parse cleanly in every fallback
        grammar (so Calc reports its normal cell error on write).

        *pos* (a ``CellAddress``) or *sheet* sets the reference scope used for
        sheet-local named ranges.
        """
        if not needs_dialect_check(text):
            return text
        self._ensure_parsers()
        if pos is not None:
            ref_pos = pos
        elif sheet is not None:
            addr: Any = uno.createUnoStruct("com.sun.star.table.CellAddress")
            addr.Sheet = sheet
            ref_pos = addr
        else:
            ref_pos = self._default_pos
        if self._clean(self._api, text, ref_pos):
            return text
        for parser, candidate in ((self._excel, strip_xlfn(text)), (self._excel_refs, text)):
            tokens = parser.parseFormula(candidate, ref_pos)
            if not accepted([int(t.OpCode) for t in tokens], self._bad, self._array_open, self._array_seps):
                continue
            out = ("=" if text.startswith("=") else "") + str(self._api.printFormula(tokens, ref_pos))
            if self._clean(self._api, out, ref_pos):
                log.debug("Translated formula dialect: %r -> %r", text, out)
                return out
        return text
