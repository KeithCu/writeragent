# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grid-to-DataFrame coercion for trusted venv analysis helpers.

The single conversion core is :func:`grid_to_dataframe` (also used by
:meth:`plugin.scripting.calc_range.CalcRange.to_pandas`). Header policy is
explicit via ``header_row``; string→number/date guessing is opt-in via
``parse_strings``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, cast

from plugin.framework.deal_shim import deal
from plugin.scripting.calc_range import ensure_rectangular_2d

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

# The sign used to sit in the ignored prefix, so "-1,234.50" and "$-123" became
# positive floats. Capture one sign before or after the currency symbol. Two
# signs ("+-5") is not a single number.
_SIGN = r"([+-])?"
_NUMBER = r"([\d,]+(?:\.\d+)?)"
_CURRENCY_CHARS = r"[$€£¥₹]"
_PERCENT_RE = re.compile(rf"^\s*{_SIGN}\s*{_NUMBER}\s*%\s*$")
_CURRENCY_RE = re.compile(rf"^\s*{_SIGN}\s*{_CURRENCY_CHARS}+\s*{_SIGN}\s*{_NUMBER}\s*$")
_NUMERIC_RE = re.compile(rf"^\s*{_SIGN}\s*{_CURRENCY_CHARS}*\s*{_SIGN}\s*{_NUMBER}\s*$")

# --- Coercion & CoerceResult ---

@dataclass(frozen=True)
class CoerceResult:
    """DataFrame plus structural metadata for analysis helpers."""
    df: Any
    metadata: dict[str, Any]


@deal.post(lambda result: isinstance(result, list) and len(set(result)) == len(result))
def _dedupe_column_names(names: list[str]) -> list[str]:
    """Unique labels, one per input, on the default ``CalcRange.to_pandas`` path.

    The counter used to be stored only under the raw base. ``['a', 'a', 'a_1']``
    therefore emitted ``a_1`` twice (the generated suffix and the later header),
    and the uniqueness postcondition raised ``deal.PostContractError``. A suffix
    is skipped when that label was already emitted.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        base = (raw or "column").strip() or "column"
        candidate = base
        suffix = 1
        while candidate in seen:
            candidate = f"{base}_{suffix}"
            suffix += 1
        seen.add(candidate)
        out.append(candidate)
    return out


def is_missing_value(value: Any) -> bool:
    """Check if value represents a missing cell, blank string, error token, or NaN/None."""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == "" or stripped in _LO_ERROR_TOKENS
    if isinstance(value, float):
        import math

        return math.isnan(value)
    try:
        import numpy as np

        if isinstance(value, np.floating) and np.isnan(value):
            return True
    except ImportError:
        pass
    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return True
    except ImportError:
        pass
    return False


# ISBLANK used to call is_missing_value, so ISBLANK("#VALUE!") was True.
# Blank is only None or a stripped empty string; error tokens and NaN are not blank.
# NA() is float nan. None and "" are not NA.
def is_blank_value(value: Any) -> bool:
    """True for None or a stripped empty string. Errors and NaN are not blank."""
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == ""


def is_na_value(value: Any) -> bool:
    """True for float or numpy NaN, or a stripped #N/A token."""
    if isinstance(value, float):
        import math

        if math.isnan(value):
            return True
    if isinstance(value, str) and value.strip().upper() == "#N/A":
        return True
    try:
        import numpy as np

        if isinstance(value, np.floating) and np.isnan(value):
            return True
    except ImportError:
        pass
    return False


def _signed_magnitude(sign_a: str | None, sign_b: str | None, digits: str) -> float | None:
    """Apply one leading sign. Two signs means the text is not a single number."""
    if sign_a and sign_b:
        return None
    try:
        return float((sign_a or sign_b or "") + digits.replace(",", ""))
    except ValueError:
        return None


def _parse_numeric_string(text: str) -> float | None:
    stripped = text.strip()
    if not stripped or stripped in _LO_ERROR_TOKENS:
        return None
    pct = _PERCENT_RE.match(stripped)
    if pct:
        value = _signed_magnitude(pct.group(1), None, pct.group(2))
        return None if value is None else value / 100.0
    cur = _CURRENCY_RE.match(stripped)
    if cur:
        return _signed_magnitude(cur.group(1), cur.group(2), cur.group(3))
    num = _NUMERIC_RE.match(stripped)
    if num:
        return _signed_magnitude(num.group(1), num.group(2), num.group(3))
    return None


def header_label(value: Any) -> str:
    """Column-name text for a header cell.

    Calc numeric cells arrive as floats. ``str(2024.0)`` is ``\"2024.0\"``, so
    ``df[\"2024\"]`` and a DSUM field ``\"2024\"`` missed the column. Whole-number
    floats use the integer spelling. Callers must not run ``parse_strings`` on
    headers first: ``\"00123\"`` is a label, not the number 123.
    """
    if value is None:
        return ""
    # bool is an int subclass; ``True`` must stay the label "True", not "1".
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _coerce_cell_basic(value: Any) -> Any:
    """Normalize blanks/errors; leave numbers and text unchanged."""
    if is_missing_value(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value.strip()
    return value


def _coerce_cell_parse_strings(value: Any) -> Any:
    """Like basic coerce, but parse currency/percent/plain numeric strings."""
    if is_missing_value(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        parsed = _parse_numeric_string(value)
        if parsed is not None:
            return parsed
        return value.strip()
    return value


def _normalize_input_grid(data: Any) -> list[list[Any]]:
    if data is None:
        return []
    # CalcRange / objects with .values
    if hasattr(data, "values") and hasattr(data, "shape") and type(data).__name__ == "CalcRange":
        return ensure_rectangular_2d(data.values)
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
        return ensure_rectangular_2d(data)
    return [[data]]


def _is_date_like_column_name(name: str) -> bool:
    """Check if column name contains common temporal indicator words."""
    cleaned = str(name).strip().lower()
    return bool(re.search(r"(?:^|[_\s-]|\b)(?:date|time|timestamp|day|month|year|created|updated|expires|dob|start|end)(?:[_\s-]|\b|$)", cleaned) or "date" in cleaned or "time" in cleaned or "timestamp" in cleaned)


def _coerce_column_types(
    df: Any,
    *,
    parse_strings: bool,
    date_cols: list[str | int] | bool = False,
    date_origin: str = "1899-12-30",
) -> Any:
    """Optional string→numeric/datetime inference and explicit date_cols conversion."""
    if not parse_strings and not date_cols:
        return df
    import pandas as pd
    out = df.copy()

    # 1. Explicit date_cols handling
    target_date_cols: set[Any] = set()
    if date_cols is True:
        for col in out.columns:
            if _is_date_like_column_name(str(col)):
                target_date_cols.add(col)
            elif str(out[col].dtype).startswith(("float", "int", "uint")):
                # Check if non-null values look like plausible Calc serials (e.g. between 1 and 100,000)
                non_null = out[col].dropna()
                if len(non_null) > 0 and (non_null > 0).all() and (non_null < 150000).all():
                    # If column name matches or parse_strings is on, target it
                    if _is_date_like_column_name(str(col)):
                        target_date_cols.add(col)
    else:
        raw_items = [date_cols] if isinstance(date_cols, (int, str)) and not isinstance(date_cols, bool) else (list(date_cols) if isinstance(date_cols, (list, tuple, set)) else [])
        for item in raw_items:
            if isinstance(item, int) and 0 <= item < len(out.columns):
                target_date_cols.add(out.columns[item])
            elif item in out.columns:
                target_date_cols.add(item)
            elif str(item) in [str(c) for c in out.columns]:
                for c in out.columns:
                    if str(c) == str(item):
                        target_date_cols.add(c)

    for col in target_date_cols:
        series = out[col]
        if str(series.dtype).startswith(("float", "int", "uint", "Int")):
            try:
                out[col] = pd.to_datetime(series, unit="D", origin=date_origin, errors="coerce")
            except Exception:
                pass
        else:
            try:
                out[col] = pd.to_datetime(series, errors="coerce", format="mixed")
            except Exception:
                pass

    # 2. General string inference if requested
    if parse_strings:
        for col in out.columns:
            if col in target_date_cols:
                continue
            series = out[col]
            if series.dtype == object or str(series.dtype) == "string":
                coerced = series.map(_coerce_cell_parse_strings)
                numeric: Any = pd.to_numeric(coerced, errors="coerce")
                non_null = coerced.notna().sum()
                numeric_non_null = numeric.notna().sum()
                # Promote only when every non-null cell parsed. An 80% cutoff
                # replaced the column with to_numeric and turned the remaining
                # text into NaN (a mixed [10, "x", 30, ...] column became float).
                if non_null > 0 and numeric_non_null == non_null:
                    out[col] = numeric
                else:
                    dt: Any = pd.to_datetime(coerced, errors="coerce", utc=False, format="mixed")
                    dt_non_null = dt.notna().sum()
                    if non_null > 0 and dt_non_null == non_null:
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


def resolve_df(
    data: Any,
    *,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> CoerceResult:
    """Coerce *data* to a DataFrame without treating an existing frame's columns as a header row."""
    if isinstance(data, CoerceResult):
        return data
    # DataFrame duck-type. Do not send it through grid_to_dataframe: that would
    # treat column names as a header row. CalcRange is unwrapped by
    # _normalize_input_grid inside coerce_to_dataframe.
    if type(data).__name__ == "CalcRange":
        hint = sheet_hint or getattr(data, "address", None)
        return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=hint)
    if hasattr(data, "columns") and hasattr(data, "index"):
        df = data.copy()
        return CoerceResult(df=df, metadata=_build_metadata(df, sheet_hint=sheet_hint, dropped_rows=0))
    return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)


def numeric_columns(df: Any, columns: list[str] | None = None) -> list[str]:
    """Return *columns* when given, else the numeric column names. Unknown names raise."""
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError(f"Unknown columns: {', '.join(missing)}")
        return list(columns)
    return [str(c) for c in df.select_dtypes(include="number").columns]


def grid_to_dataframe(
    data: Any,
    *,
    header_row: int | None = 0,
    index_col: int | None = None,
    parse_strings: bool = False,
    date_cols: list[str | int] | bool = False,
    date_origin: str = "1899-12-30",
    sheet_hint: str | None = None,
) -> CoerceResult:
    """Convert a rectangular grid / CalcRange into a typed DataFrame.

    Args:
        header_row: Row used as column names, or ``None`` for ``col_0..``.
        index_col: Optional body column to become the index.
        parse_strings: Opt-in currency/percent/numeric/datetime string parsing.
        date_cols: Specific column names/indices or True to coerce numeric
            serials/date strings to datetime64.
        date_origin: Base epoch for serial numbers (default '1899-12-30').
        sheet_hint: Optional metadata tag.
    """
    import pandas as pd
    grid = _normalize_input_grid(data)
    dropped_rows = 0
    cell_fn = _coerce_cell_parse_strings if parse_strings else _coerce_cell_basic

    if not grid:
        empty = pd.DataFrame()
        return CoerceResult(df=empty, metadata=_build_metadata(empty, sheet_hint=sheet_hint, dropped_rows=0))

    if header_row is not None:
        header_idx = max(0, min(int(header_row), len(grid) - 1))
        # Labels, not values. parse_strings on this row turned "00123" into the
        # column name "123.0" and str(2024.0) into "2024.0".
        raw_headers = [_coerce_cell_basic(cell) for cell in grid[header_idx]]
        col_names = _dedupe_column_names([header_label(h) for h in raw_headers])
        body = grid[header_idx + 1 :]
    else:
        width = max((len(row) for row in grid), default=0)
        col_names = [f"col_{i}" for i in range(width)]
        body = grid

    rows: list[list[Any]] = []
    for row in body:
        padded = list(row) + [None] * (len(col_names) - len(row))
        coerced_row = [cell_fn(cell) for cell in padded[: len(col_names)]]
        if all(cell is None for cell in coerced_row):
            dropped_rows += 1
            continue
        rows.append(coerced_row)

    df = pd.DataFrame(rows, columns=cast("Any", col_names))
    df = _coerce_column_types(df, parse_strings=parse_strings, date_cols=date_cols, date_origin=date_origin)

    if index_col is not None and 0 <= int(index_col) < len(df.columns):
        col_name = df.columns[int(index_col)]
        df = df.set_index(col_name)

    return CoerceResult(df=df, metadata=_build_metadata(df, sheet_hint=sheet_hint, dropped_rows=dropped_rows))


def coerce_to_dataframe(
    data: Any,
    *,
    headers: bool = True,
    header_row: int = 0,
    parse_strings: bool = True,
    date_cols: list[str | int] | bool = False,
    date_origin: str = "1899-12-30",
    sheet_hint: str | None = None,
    index_col: int | None = None,
) -> CoerceResult:
    """Compatibility wrapper: ``headers=False`` maps to ``header_row=None``.

    Analysis tools historically defaulted to parsing currency/percent strings; keep
    that default here so trusted helpers stay useful on pasted text sheets. User
    ``CalcRange.to_pandas()`` defaults ``parse_strings=False``.
    """
    resolved_header: int | None = header_row if headers else None
    return grid_to_dataframe(
        data,
        header_row=resolved_header,
        index_col=index_col,
        parse_strings=parse_strings,
        date_cols=date_cols,
        date_origin=date_origin,
        sheet_hint=sheet_hint,
    )


# --- Shared Result Shapes ---

def ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def parse_trusted_spec(
    spec: dict[str, Any] | str,
    *,
    helper_names: frozenset[str] | set[str],
    context: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], bool, int, dict[str, Any], dict[str, Any]] | dict[str, Any]:
    """Shared preamble for run_analysis / run_forecast / run_optimize.

    Returns ``(helper, params, headers, header_row, context, spec)`` or an error dict.
    """
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return error_result("INVALID_SPEC", "spec must be a dict or helper name string")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return error_result("MISSING_HELPER", "spec.helper is required")
    if helper not in helper_names:
        return error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}
    headers = bool(spec_dict.get("headers", True))
    header_row = int(spec_dict.get("header_row", 0))
    ctx = context if isinstance(context, dict) else {}
    return helper, params, headers, header_row, ctx, spec_dict


def error_result(
    code: str,
    message: str,
    *,
    helper: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    return out


def missing_package_error(helper: str, package: str) -> dict[str, Any]:
    return error_result(
        "MISSING_PACKAGE",
        f"{package} is required for {helper}.",
        helper=helper,
    )


def table_from_df(df: Any, *, name: str, max_rows: int = 50) -> dict[str, Any]:
    limited = df.head(max_rows)
    return {
        "name": name,
        "columns": [str(c) for c in limited.columns],
        "rows": limited.where(limited.notna(), None).values.tolist(),
        "truncated": len(df) > max_rows,
        "total_rows": int(len(df)),
    }


def records_from_df(df: Any, *, max_rows: int = 50) -> list[dict[str, Any]]:
    limited = df.head(max_rows)
    return limited.where(limited.notna(), None).to_dict(orient="records")
