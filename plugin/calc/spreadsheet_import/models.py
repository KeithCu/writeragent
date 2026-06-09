# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data models for Calc spreadsheet → Python import (ingest + preserve phases)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CellType = Literal[
    "empty",
    "constant",
    "formula",
    "py_formula",
    "prompt",
    "array_formula",
    "error",
]

FORMULA_LIKE_TYPES: frozenset[CellType] = frozenset(
    {"formula", "py_formula", "array_formula", "error"},
)


@dataclass
class CellRecord:
    """One cell in an ingested sheet snapshot."""

    address: str
    type: CellType
    value: Any
    formula: str | None
    number_format: int | None  # UNO NumberFormat key; None in bulk ingest path
    precedents: list[str] = field(default_factory=list)
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SheetModel:
    """Ingested sheet: classified cells plus dependency ordering."""

    sheet_name: str
    used_range: str
    cells: dict[str, CellRecord]
    formula_order: list[str] = field(default_factory=list)
    circular_groups: list[list[str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "used_range": self.used_range,
            "cells": {addr: cell.to_dict() for addr, cell in self.cells.items()},
            "formula_order": list(self.formula_order),
            "circular_groups": [list(group) for group in self.circular_groups],
        }


@dataclass
class PyCellExtract:
    """Parsed and normalized ``=PY()`` / ``=PYTHON()`` cell."""

    address: str
    original_formula: str
    normalized_formula: str
    code: str
    data_args: list[str]
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OutputCell:
    """One cell in a preserve-phase output grid."""

    address: str
    value: Any
    formula: str | None
    number_format: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TranslationResult:
    """Outcome of translating one Calc formula to Python."""

    ok: bool
    code: str | None = None
    data_ranges: list[str] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TodoCell:
    """Formula cell left unconverted with a reason code."""

    address: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConversionReport:
    """Aggregate stats from a translate+emit pass."""

    converted: list[str] = field(default_factory=list)
    skipped: list[TodoCell] = field(default_factory=list)
    normalized_py: list[str] = field(default_factory=list)
    pass_through: list[str] = field(default_factory=list)

    def conversion_rate(self, *, formula_denominator: int | None = None) -> float:
        denom = formula_denominator if formula_denominator is not None else (
            len(self.converted) + len(self.skipped) + len(self.pass_through)
        )
        if denom <= 0:
            return 0.0
        return len(self.converted) / denom

    def to_dict(self) -> dict[str, Any]:
        return {
            "converted": list(self.converted),
            "skipped": [item.to_dict() for item in self.skipped],
            "normalized_py": list(self.normalized_py),
            "pass_through": list(self.pass_through),
            "conversion_rate": self.conversion_rate(),
        }

    def summary(self) -> str:
        total = len(self.converted) + len(self.skipped) + len(self.pass_through)
        pct = 100.0 * self.conversion_rate(formula_denominator=total) if total else 0.0
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
class VerifyResult:
    passed: list[str] = field(default_factory=list)
    failed: list[VerifyMismatch] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": list(self.passed),
            "failed": [item.to_dict() for item in self.failed],
            "skipped": list(self.skipped),
        }


@dataclass
class OutputSheetModel:
    """Output grid after preserve pass (constants + normalized PY + pass-through formulas)."""

    sheet_name: str
    used_range: str
    cells: dict[str, OutputCell]
    py_extracts: list[PyCellExtract] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "used_range": self.used_range,
            "cells": {addr: cell.to_dict() for addr, cell in self.cells.items()},
            "py_extracts": [item.to_dict() for item in self.py_extracts],
        }
