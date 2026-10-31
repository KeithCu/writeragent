# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data models for Calc spreadsheet → Python import (ingest + preserve phases)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from plugin.calc.spreadsheet_import.extract import normalize_py_formula

CellType = Literal["empty", "constant", "formula", "py_formula", "prompt", "array_formula", "error"]

FORMULA_LIKE_TYPES: frozenset[CellType] = frozenset({"formula", "py_formula", "array_formula", "error"})


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
    """Ingested sheet: classified cells plus dependency ordering."""

    sheet_name: str
    used_range: str
    cells: dict[str, CellRecord]
    formula_order: list[str] = field(default_factory=list)
    circular_groups: list[list[str]] = field(default_factory=list)


@dataclass
class OutputCell:
    """One cell in a preserve-phase output grid."""

    address: str
    value: Any
    formula: str | None
    number_format: int | None


def output_cell_from_record(cell: CellRecord) -> OutputCell:
    if cell.type == "empty":
        return OutputCell(address=cell.address, value=None, formula=None, number_format=None)
    if cell.type == "constant":
        return OutputCell(address=cell.address, value=cell.value, formula=None, number_format=cell.number_format)
    if cell.type == "py_formula":
        formula = cell.formula
        if formula:
            normalized = normalize_py_formula(formula)
            if normalized is not None:
                formula = normalized
        return OutputCell(address=cell.address, value=None, formula=formula, number_format=None)
    if cell.formula:
        return OutputCell(address=cell.address, value=None, formula=cell.formula, number_format=None)
    return OutputCell(address=cell.address, value=cell.value, formula=None, number_format=None)


@dataclass
class TranslationResult:
    """Outcome of translating one Calc formula to Python."""

    ok: bool
    code: str | None = None
    data_ranges: list[str] | None = None
    reason: str | None = None


@dataclass
class TodoCell:
    """Formula cell left unconverted with a reason code."""

    address: str
    reason: str


@dataclass
class ConversionReport:
    """Aggregate stats from a translate+emit pass."""

    converted: list[str] = field(default_factory=list)
    skipped: list[TodoCell] = field(default_factory=list)
    pass_through: list[str] = field(default_factory=list)

    def formula_total(self) -> int:
        """Distinct formula-like cells considered; a cell with a reason is counted once."""
        return len(set(self.converted) | set(self.pass_through) | {todo.address for todo in self.skipped})

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OutputSheetModel:
    """Output grid after preserve pass (constants + normalized PY + pass-through formulas)."""

    sheet_name: str
    used_range: str
    cells: dict[str, OutputCell]
