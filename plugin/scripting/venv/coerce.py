# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grid-to-DataFrame coercion for trusted venv analysis helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

_NUMERIC_PROFILE_KEYS = (
    ("mean", "mean"),
    ("std", "std"),
    ("min", "min"),
    ("max", "max"),
    ("median", "50%"),
)

_LO_ERROR_TOKENS = frozenset(
    {
        "#N/A",
        "#DIV/0!",
        "#VALUE!",
        "#REF!",
        "#NAME?",
        "#NUM!",
        "#NULL!",
        "#N/A N/A",
    }
)

_CURRENCY_RE = re.compile(r"^[\s$€£¥₹]+\s*([\d,]+(?:\.\d+)?)\s*$")
_PERCENT_RE = re.compile(r"^([\d,]+(?:\.\d+)?)\s*%\s*$")
_NUMERIC_RE = re.compile(r"^[\s$€£¥₹+-]*([\d,]+(?:\.\d+)?)\s*$")

# --- Coercion & CoerceResult ---

@dataclass(frozen=True)
class CoerceResult:
    """DataFrame plus structural metadata for analysis helpers."""
    df: Any
    metadata: dict[str, Any]


def is_missing_value(value: Any) -> bool:
    """Check if value represents a missing cell, blank string, error token, or NaN/None."""
    if value is None:
        return True
    if isinstance(value, float):
        import math
        if math.isnan(value):
            return True
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "" or stripped in _LO_ERROR_TOKENS:
            return True
    try:
        import numpy as np
        if isinstance(value, (np.floating, float)) and np.isnan(value):
            return True
    except ImportError:
        pass
    return False


def _parse_numeric_string(text: str) -> float | None:
    stripped = text.strip()
    if not stripped or stripped in _LO_ERROR_TOKENS:
        return None
    pct = _PERCENT_RE.match(stripped)
    if pct:
        try:
            return float(pct.group(1).replace(",", "")) / 100.0
        except ValueError:
            return None
    cur = _CURRENCY_RE.match(stripped)
    if cur:
        try:
            return float(cur.group(1).replace(",", ""))
        except ValueError:
            return None
    num = _NUMERIC_RE.match(stripped)
    if num:
        try:
            return float(num.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _coerce_cell(value: Any) -> Any:
    if is_missing_value(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        parsed = _parse_numeric_string(value)
        if parsed is not None:
            return parsed
        return value.strip()
    return value


def _dedupe_column_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in names:
        base = (raw or "column").strip() or "column"
        count = seen.get(base, 0)
        if count:
            out.append(f"{base}_{count}")
        else:
            out.append(base)
        seen[base] = count + 1
    return out


def _normalize_input_grid(data: Any) -> list[list[Any]]:
    if data is None:
        return []
    if isinstance(data, dict):
        columns = data.get("columns")
        rows = data.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            return [list(columns)] + [list(row) if isinstance(row, (list, tuple)) else [row] for row in rows]
    if isinstance(data, (list, tuple)):
        if not data:
            return []
        first = data[0]
        if isinstance(first, dict):
            keys: list[str] = []
            for row in data:
                if isinstance(row, dict):
                    for key in row:
                        if key not in keys:
                            keys.append(key)
            return [[key for key in keys]] + [[row.get(key) if isinstance(row, dict) else None for key in keys] for row in data]
        if isinstance(first, (list, tuple)):
            return [list(row) for row in data]
        return [[item] for item in data]
    return [[data]]


def _coerce_column_types(df: Any) -> Any:
    import pandas as pd
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if series.dtype == object or str(series.dtype) == "string":
            coerced = series.map(_coerce_cell)
            numeric: Any = pd.to_numeric(coerced, errors="coerce")
            non_null = coerced.notna().sum()
            numeric_non_null = numeric.notna().sum()
            if non_null > 0 and numeric_non_null >= max(1, int(non_null * 0.8)):
                out[col] = numeric
            else:
                dt: Any = pd.to_datetime(coerced, errors="coerce", utc=False, format="mixed")
                dt_non_null = dt.notna().sum()
                if non_null > 0 and dt_non_null >= max(1, int(non_null * 0.8)):
                    out[col] = dt
                else:
                    out[col] = coerced
    return out


def _build_metadata(df: Any, *, sheet_hint: str | None, dropped_rows: int) -> dict[str, Any]:
    numeric_cols = [str(c) for c in df.columns if str(df[c].dtype).startswith(("float", "int", "Int", "uint"))]
    categorical_cols = [str(c) for c in df.columns if c not in numeric_cols and not str(df[c].dtype).startswith("datetime")]
    datetime_cols = [str(c) for c in df.columns if str(df[c].dtype).startswith("datetime")]
    meta: dict[str, Any] = {
        "n_rows": int(len(df)),
        "n_cols": int(len(df.columns)),
        "numeric_cols": numeric_cols,
        "categorical_cols": categorical_cols,
        "datetime_cols": datetime_cols,
        "dropped_rows": dropped_rows,
    }
    if sheet_hint:
        meta["sheet_hint"] = sheet_hint
    return meta


def coerce_to_dataframe(
    data: Any,
    *,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> CoerceResult:
    """Convert wire-format *data* into a typed DataFrame with metadata."""
    import pandas as pd
    grid = _normalize_input_grid(data)
    dropped_rows = 0

    if not grid:
        empty = pd.DataFrame()
        return CoerceResult(df=empty, metadata=_build_metadata(empty, sheet_hint=sheet_hint, dropped_rows=0))

    if headers:
        header_idx = max(0, min(header_row, len(grid) - 1))
        raw_headers = [_coerce_cell(cell) for cell in grid[header_idx]]
        col_names = _dedupe_column_names([str(h) if h is not None else "" for h in raw_headers])
        body = grid[header_idx + 1 :]
    else:
        width = max((len(row) for row in grid), default=0)
        col_names = [f"col_{i}" for i in range(width)]
        body = grid

    rows: list[list[Any]] = []
    for row in body:
        padded = list(row) + [None] * (len(col_names) - len(row))
        coerced_row = [_coerce_cell(cell) for cell in padded[: len(col_names)]]
        if all(cell is None for cell in coerced_row):
            dropped_rows += 1
            continue
        rows.append(coerced_row)

    df = pd.DataFrame(rows, columns=cast("Any", col_names))
    df = _coerce_column_types(df)
    return CoerceResult(df=df, metadata=_build_metadata(df, sheet_hint=sheet_hint, dropped_rows=dropped_rows))
