# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify converted cells against ingest snapshot oracles."""

from __future__ import annotations

import math
from typing import Any

from plugin.calc.error_detector import is_calc_error_display
from plugin.calc.python.cell_discovery import is_py_formula_text
from plugin.calc.spreadsheet_import.models import ConversionReport, OutputSheetModel, SheetModel, VerifyMismatch, VerifyResult


def _values_equal(expected: Any, actual: Any) -> tuple[bool, str]:
    if expected is None and actual is None:
        return True, ""
    if is_calc_error_display(expected) or is_calc_error_display(actual):
        return str(expected) == str(actual), f"error display {expected!r} vs {actual!r}"
    if isinstance(expected, str) or isinstance(actual, str):
        return str(expected) == str(actual), f"string {expected!r} vs {actual!r}"
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual), f"bool {expected!r} vs {actual!r}"
    if expected is None or actual is None:
        return expected == actual, f"none {expected!r} vs {actual!r}"
    try:
        exp_f = float(expected)
        act_f = float(actual)
        if math.isnan(exp_f) and math.isnan(act_f):
            return True, ""
        if abs(exp_f - act_f) <= 1e-9:
            return True, ""
        return False, f"float {exp_f} vs {act_f}"
    except (TypeError, ValueError):
        return expected == actual, f"other {expected!r} vs {actual!r}"


def verify_converted_cells(
    source: SheetModel,
    output: OutputSheetModel,
    report: ConversionReport,
    *,
    actual_values: dict[str, Any],
) -> VerifyResult:
    """Compare converted cell values against the source ingest oracle."""
    result = VerifyResult()
    for addr in report.converted:
        if addr not in source.cells:
            result.skipped.append(addr)
            continue
        oc = output.cells.get(addr)
        if oc is None or not oc.formula or not is_py_formula_text(oc.formula):
            result.failed.append(
                VerifyMismatch(
                    address=addr,
                    expected="=PY(...)",
                    actual=oc.formula if oc else None,
                    message="missing PY formula",
                )
            )
            continue
        if addr not in actual_values:
            result.skipped.append(addr)
            continue
        expected = source.cells[addr].value
        actual = actual_values[addr]
        ok, msg = _values_equal(expected, actual)
        if ok:
            result.passed.append(addr)
        else:
            result.failed.append(VerifyMismatch(address=addr, expected=expected, actual=actual, message=msg))
    return result

