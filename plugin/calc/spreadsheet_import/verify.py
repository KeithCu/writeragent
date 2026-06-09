# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify converted cells against ingest snapshot oracles."""

from __future__ import annotations

import math
from typing import Any

from plugin.calc.spreadsheet_import.graph import is_calc_error_display
from plugin.calc.spreadsheet_import.models import (
    ConversionReport,
    OutputSheetModel,
    SheetModel,
    VerifyMismatch,
    VerifyResult,
)


def _values_equal(expected: Any, actual: Any, *, rtol: float) -> tuple[bool, str]:
    if expected is None and actual is None:
        return True, ""
    if is_calc_error_display(expected) or is_calc_error_display(actual):
        return str(expected) == str(actual), f"error display {expected!r} vs {actual!r}"
    if isinstance(expected, str) or isinstance(actual, str):
        return str(expected) == str(actual), f"string {expected!r} vs {actual!r}"
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual), f"bool {expected!r} vs {actual!r}"
    try:
        exp_f = float(expected)
        act_f = float(actual)
        if math.isnan(exp_f) and math.isnan(act_f):
            return True, ""
        if abs(exp_f - act_f) <= rtol:
            return True, ""
        return False, f"float {exp_f} vs {act_f}"
    except (TypeError, ValueError):
        return expected == actual, f"other {expected!r} vs {actual!r}"


def verify_converted_cells(
    source: SheetModel,
    output: OutputSheetModel,
    report: ConversionReport,
    *,
    rtol: float = 1e-9,
    actual_values: dict[str, Any] | None = None,
) -> VerifyResult:
    """Compare cell values to *source* ingest oracle.

    When *actual_values* is omitted (no LibreOffice recalc), only checks that
    converted cells have ``=PY()`` formulas and records oracles for later UNO
    verification.
    """
    result = VerifyResult()
    for addr in report.converted:
        if addr not in source.cells:
            result.skipped.append(addr)
            continue
        oc = output.cells.get(addr)
        if oc is None or not oc.formula or not oc.formula.lstrip().upper().startswith("=PY"):
            result.failed.append(
                VerifyMismatch(
                    address=addr,
                    expected="=PY(...)",
                    actual=oc.formula if oc else None,
                    message="missing PY formula",
                ),
            )
            continue
        if actual_values is not None:
            expected = source.cells[addr].value
            actual = actual_values.get(addr)
            ok, msg = _values_equal(expected, actual, rtol=rtol)
            if ok:
                result.passed.append(addr)
            else:
                result.failed.append(
                    VerifyMismatch(address=addr, expected=expected, actual=actual, message=msg),
                )
        else:
            result.passed.append(addr)
    return result


def verify_output_formulas_present(output: OutputSheetModel, report: ConversionReport) -> VerifyResult:
    """Smoke-check converted cells have ``=PY()`` formulas in *output*."""
    result = VerifyResult()
    for addr in report.converted:
        oc = output.cells.get(addr)
        if oc is None or not oc.formula or not oc.formula.upper().startswith("=PY"):
            result.failed.append(
                VerifyMismatch(
                    address=addr,
                    expected="=PY(...)",
                    actual=oc.formula if oc else None,
                    message="missing PY formula",
                ),
            )
        else:
            result.passed.append(addr)
    return result
