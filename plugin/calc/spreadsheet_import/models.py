# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Records the import's stages pass along: ingest's ``CellRecord``/``SheetModel``, translate's ``TranslationResult``, emit's ``ConversionReport``/``TodoCell``, preserve's ``OutputCell``/``OutputSheetModel``, verify's ``VerifyMismatch``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CellType = Literal["empty", "constant", "formula", "py_formula", "prompt", "array_formula", "error"]

FORMULA_LIKE_TYPES: frozenset[CellType] = frozenset({"formula", "py_formula", "array_formula", "error"})

# Report codes for unconverted formula cells:
# - translate: UNSUPPORTED_FUNCTION (function or reference with no emitter),
#   UNSUPPORTED_ARITY (wrong argument count), PARSE_ERROR (parser or codegen cannot read it)
# - emit: PROMPT and ARRAY_FORMULA (cell kinds never converted), CIRCULAR_REF,
#   EMIT_ERROR (unexpected exception for that one cell)
ReportCode = Literal[
    "UNSUPPORTED_FUNCTION",
    "UNSUPPORTED_ARITY",
    "PARSE_ERROR",
    "PROMPT",
    "ARRAY_FORMULA",
    "CIRCULAR_REF",
    "EMIT_ERROR",
]


@dataclass
class CellRecord:
    """One cell in an ingested sheet snapshot."""

    address: str
    type: CellType
    value: Any
    formula: str | None
    number_format: int | None  # UNO NumberFormat key; None in bulk ingest path
    precedents: list[str] = field(default_factory=list)


@dataclass
class SheetModel:
    """Ingested sheet: classified cells plus circular groups."""

    sheet_name: str
    used_range: str
    cells: dict[str, CellRecord]
    circular_groups: list[list[str]] = field(default_factory=list)


@dataclass
class OutputCell:
    """One cell in a preserve-phase output grid."""

    address: str
    value: Any
    formula: str | None
    number_format: int | None


@dataclass
class TranslationResult:
    """Outcome of translating one Calc formula to Python.

    ``has_function`` is True when the parsed formula calls at least one function,
    False when it is only references, literals and operators (``=A1*2``), and
    None when it did not parse. The import keeps function-free formulas as
    native Calc and does not list them in the report.
    """

    ok: bool
    code: str | None = None
    data_ranges: list[str] | None = None
    reason: ReportCode | None = None
    has_function: bool | None = None


@dataclass
class TodoCell:
    """Formula cell left unconverted, with its report code (see ``ReportCode``)."""

    address: str
    reason: ReportCode


@dataclass
class ConversionReport:
    """Aggregate stats from a translate+emit pass."""

    converted: list[str] = field(default_factory=list)
    skipped: list[TodoCell] = field(default_factory=list)
    pass_through: list[str] = field(default_factory=list)

    def formula_total(self) -> int:
        """len(converted) + len(pass_through); skipped addresses are already in pass_through."""
        return len(self.converted) + len(self.pass_through)

    def conversion_rate(self) -> float:
        total = self.formula_total()
        if total <= 0:
            return 0.0
        return len(self.converted) / total

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["conversion_rate"] = self.conversion_rate()
        return payload

    def summary(self) -> str:
        total = self.formula_total()
        pct = 100.0 * self.conversion_rate()
        return f"Converted {len(self.converted)} / {total} formula cells ({pct:.1f}%)"


@dataclass
class VerifyMismatch:
    address: str
    expected: Any
    actual: Any
    message: str


@dataclass
class OutputSheetModel:
    """Output grid after preserve pass (constants + normalized PY + pass-through formulas)."""

    sheet_name: str
    used_range: str
    cells: dict[str, OutputCell]
