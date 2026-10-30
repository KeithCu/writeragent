# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Grid-to-DataFrame coercion for trusted venv analysis helpers.

The single conversion core is :func:`grid_to_dataframe` (also used by
:meth:`plugin.scripting.calc_range.CalcRange.to_pandas`). Header policy is
explicit via ``header_row``; string→number/date guessing is opt-in via
``parse_strings``. Also holds numeric-column selection helpers.
"""

from __future__ import annotations

import math
import warnings
import re
from dataclasses import dataclass
from typing import Any, cast

from plugin.framework.deal_shim import deal
from plugin.scripting.calc_range import CalcRange, ensure_rectangular_2d

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
        # Case-fold so lower/mixed case error tokens like "#n/a" and "#div/0!" match.
        return stripped == "" or stripped.upper() in _LO_ERROR_TOKENS
    if isinstance(value, float):
        return math.isnan(value)
    try:
        import numpy as np

        if isinstance(value, np.floating) and np.isnan(value):
            return True
        # numpy.datetime64 and timedelta64 NaT are not np.floating, so the NaN
        # check misses them. Test them explicitly with np.isnat.
        if isinstance(value, (np.datetime64, np.timedelta64)) and np.isnat(value):
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
    # bool and int labels are str(value); only a whole-number float needs a different spelling
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _coerce_cell(value: Any, *, parse: bool) -> Any:
    """Normalize blanks and errors. Parse currency/percent/numeric text only when parse is true."""
    if is_missing_value(value):
        return None
    if isinstance(value, str):
        if parse:
            parsed = _parse_numeric_string(value)
            if parsed is not None:
                return parsed
        return value.strip()
    return value


def _normalize_input_grid(data: Any) -> list[list[Any]]:
    if data is None:
        return []
    # CalcRange / objects with .values
    if isinstance(data, CalcRange):
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
    # A raw ndarray used to be stored as the single cell of a 1x1 frame.
    try:
        import numpy as np

        if isinstance(data, np.ndarray):
            if data.ndim == 0:
                return [[data.item()]]
            if data.ndim in (1, 2):
                return ensure_rectangular_2d(data.tolist())
            return [[data]]
    except ImportError:
        pass
    try:
        import pandas as pd

        if isinstance(data, pd.Series):
            return ensure_rectangular_2d(cast("Any", data).tolist())
    except ImportError:
        pass
    if isinstance(data, (int, float, str, bool)):
        return [[data]]
    raise ValueError(f"Cannot normalize input of type {type(data).__name__} to 2D grid")


_DATE_NAME_TOKENS = frozenset({
    "date",
    "time",
    "timestamp",
    "datetime",
    "day",
    "month",
    "year",
    "created",
    "updated",
    "expires",
    "dob",
    "start",
    "end",
})
_CAMEL_CASE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _is_date_like_column_name(name: str) -> bool:
    """Check if column name contains exact temporal indicator tokens.

    The substring 'or's matched 'candidate' / 'runtime'; a boundary regex
    misses 'OrderDate' and 'datetime'.
    """
    # Substring checks matched candidate, runtime, etc. A boundary regex
    # missed OrderDate and datetime. Splitting on camelCase humps and extracting
    # alphanumeric tokens matches exact temporal components.
    spaced = _CAMEL_CASE_RE.sub(" ", str(name))
    tokens = _TOKEN_RE.findall(spaced.lower())
    return any(tok in _DATE_NAME_TOKENS for tok in tokens)


def _is_numeric_dtype(dtype: Any) -> bool:
    import pandas as pd

    return bool(pd.api.types.is_numeric_dtype(dtype) and not pd.api.types.is_bool_dtype(dtype))


def _is_datetime_dtype(dtype: Any) -> bool:
    import pandas as pd

    return bool(pd.api.types.is_datetime64_any_dtype(dtype))


def _date_cols_items(date_cols: Any) -> list[Any] | None:
    if date_cols is True:
        return None
    if isinstance(date_cols, (int, str)) and not isinstance(date_cols, bool):
        return [date_cols]
    if isinstance(date_cols, (list, tuple, set)):
        return list(date_cols)
    return []


def _coerce_column_types(
    df: Any,
    *,
    parse_strings: bool,
    date_cols: list[str | int] | bool = False,
    date_origin: str = "1899-12-30",
) -> Any:
    """Optional string→numeric/datetime inference and explicit date_cols conversion."""
    items = _date_cols_items(date_cols)
    if not parse_strings and items == []:
        return df
    import numpy as np
    import pandas as pd
    out = df.copy()

    # 1. Explicit date_cols handling
    target_date_cols: set[Any] = set()
    if items is None:
        for col in out.columns:
            if _is_date_like_column_name(str(col)):
                target_date_cols.add(col)
    else:
        col_str_map: dict[str, list[Any]] = {}
        for c in out.columns:
            col_str_map.setdefault(str(c), []).append(c)
        for item in items:
            if isinstance(item, int) and 0 <= item < len(out.columns):
                target_date_cols.add(out.columns[item])
            elif item in out.columns:
                target_date_cols.add(item)
            else:
                target_date_cols.update(col_str_map.get(str(item), []))

    for col in target_date_cols:
        series = out[col]
        # A numeric dtype, including nullable Float64, must go through
        # parse_date_column: format="mixed" on that dtype used to store
        # 45292 as nanoseconds since 1970, and integer years 2019/2020 used to
        # become 1905-07-11 / 1905-07-12 because this loop always used unit="D".
        out[col] = parse_date_column(series, date_origin=date_origin)

    # 2. General string inference if requested
    if parse_strings:
        for col in out.columns:
            if col in target_date_cols:
                continue
            series = out[col]
            if series.dtype == object or str(series.dtype) == "string":
                coerced = series.map(lambda value: _coerce_cell(value, parse=True))
                non_null = 0
                all_numeric = True
                for v in coerced:
                    if v is not None and not (isinstance(v, float) and math.isnan(v)):
                        non_null += 1
                        if not isinstance(v, (int, float, np.integer, np.floating)):
                            all_numeric = False
                if non_null > 0 and all_numeric:
                    out[col] = pd.to_numeric(coerced, errors="coerce")
                elif non_null > 0:
                    dt: Any = pd.to_datetime(coerced, errors="coerce", utc=False, format="mixed")
                    if dt.notna().sum() == non_null:
                        out[col] = dt
                    else:
                        out[col] = coerced
                else:
                    out[col] = coerced
    return out


def _column_labels(df: Any) -> list[str]:
    """Return DataFrame columns as a list of string labels."""
    return [str(c) for c in df.columns]


def _build_metadata(df: Any, *, sheet_hint: str | None, dropped_rows: int) -> dict[str, Any]:
    # What was wrong: resolve_df on a DataFrame with duplicate labels raised
    # AttributeError: 'DataFrame' object has no attribute 'dtype'.
    # How: df[col] is a DataFrame when that label appears twice.
    # Why this fixes it: df.dtypes has one dtype per column position, so duplicate
    # labels are classified instead of looked up by name.
    numeric_cols: list[str] = []
    categorical_cols: list[str] = []
    datetime_cols: list[str] = []
    for label, dtype in zip(_column_labels(df), df.dtypes):
        if _is_datetime_dtype(dtype):
            datetime_cols.append(label)
        elif _is_numeric_dtype(dtype):
            numeric_cols.append(label)
        else:
            categorical_cols.append(label)

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
    if hasattr(data, "columns") and hasattr(data, "index"):
        df = data.copy()
        return CoerceResult(df=df, metadata=_build_metadata(df, sheet_hint=sheet_hint, dropped_rows=0))
    hint = (sheet_hint or data.address) if isinstance(data, CalcRange) else sheet_hint
    return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=hint)


def parse_date_column(series: Any, *, date_origin: str = "1899-12-30") -> Any:
    """Parse date series supporting datetime64, years, serials, strings, and mixed.

    pd.to_datetime on a Calc serial is nanoseconds since 1970 (45292.0 ->
    1970-01-01 00:00:00.000045292). read_range without format info leaves serial
    floats on purpose. The year window is separate because 1900-2200 would
    otherwise become 1905.
    """
    import numpy as np
    import pandas as pd

    if not isinstance(series, pd.Series):
        series = pd.Series(series)

    if pd.api.types.is_datetime64_any_dtype(series):
        if getattr(series.dt, "tz", None) is not None:
            series = series.dt.tz_localize(None)
        return series.astype("datetime64[ns]")

    if pd.api.types.is_bool_dtype(series):
        return pd.Series(pd.NaT, index=series.index, name=series.name, dtype="datetime64[ns]")

    if pd.api.types.is_numeric_dtype(series):
        return _parse_numeric_date_series(series, date_origin=date_origin)

    # Object or string dtype
    is_num_cell: Any = series.map(
        lambda x: isinstance(x, (int, float, np.integer, np.floating))
        and not isinstance(x, bool)
        and not bool(pd.isna(x))
    )

    result = pd.Series(pd.NaT, index=series.index, name=series.name, dtype="datetime64[ns]")

    if is_num_cell.any():
        num_slice = series[is_num_cell]
        parsed_num = _parse_numeric_date_series(num_slice, date_origin=date_origin)
        result.loc[is_num_cell] = parsed_num

    not_num = ~is_num_cell
    if not_num.any():
        str_slice = series[not_num]
        # What was wrong: without format="mixed", pandas locks onto the first cell,
        # so "2024-01-03" plus "01/01/2024" plus "2024-01-02 15:30:00" parsed as one
        # timestamp and three NaTs (verified on this series through resolve_df, which leaves
        # the column as object because "notes" blocks promotion). _order_by_date then sorted
        # those rows last and portfolio_tearsheet dropped them.
        # How it happened: pd.to_datetime guesses a single format across the series by default.
        # Why this change fixes it: format="mixed" parses each cell individually, matching
        # _coerce_column_types. Mixed offsets return object and .dt raises; when the result
        # is not datetime64, the fallback calls pd.to_datetime without format to keep one
        # offset and NaT for the rest without raising AttributeError.
        # Mixed offsets make pandas 2 emit a FutureWarning; the fallback result is intended
        # (one offset, NaT for the rest), so silence just that warning. pandas 3 raises
        # ValueError ("Mixed timezones detected") instead; take the same fallback.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*mixed time zones.*", category=FutureWarning)
            try:
                parsed_str: Any = pd.to_datetime(str_slice, errors="coerce", format="mixed")
            except ValueError:
                parsed_str = None
            if parsed_str is None or not pd.api.types.is_datetime64_any_dtype(parsed_str):
                parsed_str = pd.to_datetime(str_slice, errors="coerce")
        if getattr(parsed_str.dt, "tz", None) is not None:
            parsed_str = parsed_str.dt.tz_localize(None)
        result.loc[not_num] = parsed_str.astype("datetime64[ns]")

    return result


def _parse_numeric_date_series(series: Any, *, date_origin: str = "1899-12-30") -> Any:
    import numpy as np
    import pandas as pd

    non_null = series.dropna()
    is_year = False
    if len(non_null) > 0:
        try:
            arr = non_null.to_numpy(dtype=float)
            if np.isfinite(arr).all():
                is_int = (arr % 1 == 0)
                in_range = (arr >= 1900) & (arr <= 2200)
                if is_int.all() and in_range.all():
                    is_year = True
        except Exception:  # noqa: S110
            pass

    if is_year:
        year_strs = series.map(
            lambda v: f"{int(v):04d}-01-01"
            if pd.notna(v) and np.isfinite(float(v))
            else pd.NaT
        )
        return pd.to_datetime(year_strs, errors="coerce").astype("datetime64[ns]")
    else:
        def to_serial(v: Any) -> float:
            if pd.isna(v):
                return float("nan")
            try:
                fv = float(v)
                if np.isfinite(fv) and 0 < fv <= 2958465:
                    return fv
            except Exception:  # noqa: S110
                pass
            return float("nan")

        serials = series.map(to_serial)
        return pd.to_datetime(serials, unit="D", origin=date_origin, errors="coerce").astype("datetime64[ns]")


def _optional_numpy() -> Any:
    try:
        import numpy as np
        return np
    except ImportError:
        return None


def _whole_number(value: Any) -> int | None:
    if math.isfinite(value) and float(value).is_integer():
        return int(value)
    return None


def coerce_int(value: Any, name: str = "value") -> int:
    """Coerce *value* to int or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    np = _optional_numpy()
    int_types = (int, np.integer) if np is not None else (int,)
    float_types = (float, np.floating) if np is not None else (float,)

    if isinstance(value, int_types):
        return int(value)
    if isinstance(value, float_types):
        whole = _whole_number(value)
        if whole is not None:
            return whole
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                whole = _whole_number(float(stripped))
                if whole is not None:
                    return whole
            except (ValueError, TypeError):
                pass
        raise ValueError(f"{name} must be an integer, got {value!r}")
    raise ValueError(f"{name} must be an integer, got {value!r}")


def coerce_optional_int(value: Any, name: str = "value") -> int | None:
    if value is None:
        return None
    return coerce_int(value, name=name)


def _coerce_number(value: Any) -> float | None:
    """Coerce value to float or return None."""
    if isinstance(value, bool):
        return None
    np = _optional_numpy()
    num_types = (int, float, np.integer, np.floating) if np is not None else (int, float)
    if isinstance(value, num_types):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        parsed = _parse_numeric_string(value)
        if parsed is not None:
            return parsed
        stripped = value.strip()
        if stripped:
            try:
                f = float(stripped)
                return f if math.isfinite(f) else None
            except (ValueError, TypeError):
                pass
    return None


def coerce_float(value: Any, name: str = "value") -> float:
    """Coerce *value* to float or raise ValueError."""
    f = _coerce_number(value)
    if f is not None:
        return f
    raise ValueError(f"{name} must be a float, got {value!r}")


def coerce_optional_float(value: Any, name: str = "value") -> float | None:
    if value is None:
        return None
    return coerce_float(value, name=name)


def coerce_bool(value: Any, *, name: str = "value", default: bool = False) -> bool:
    """Coerce *value* to bool or raise ValueError."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    np = _optional_numpy()
    if np is not None:
        # What was wrong: coerce_bool(np.bool_(True)) raised ValueError.
        # How: np.bool_ is not a subclass of bool or of np.integer. islogical
        # already treats it as logical.
        # Why this fixes it: a NumPy bool is returned with bool(value) before
        # the 0/1 integer checks.
        if isinstance(value, np.bool_):
            return bool(value)

    int_types = (int, np.integer) if np is not None else (int,)
    float_types = (float, np.floating) if np is not None else (float,)

    if isinstance(value, int_types):
        if value == 1:
            return True
        if value == 0:
            return False
        raise ValueError(f"{name} must be a boolean, got {value!r}")
    if isinstance(value, float_types):
        whole = _whole_number(value)
        if whole == 1:
            return True
        if whole == 0:
            return False
        raise ValueError(f"{name} must be a boolean, got {value!r}")
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in ("1", "true", "yes", "y"):
            return True
        if stripped in ("0", "false", "no", "n"):
            return False
        raise ValueError(f"{name} must be a boolean, got {value!r}")
    raise ValueError(f"{name} must be a boolean, got {value!r}")


def coerce_str_list(value: Any, name: str = "value") -> list[str] | None:
    """Coerce *value* to a list of column names or raise ValueError."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{name} must be a column list, got {value!r}")
        if "," in value:
            parts = [p.strip() for p in value.split(",") if p.strip()]
            if not parts:
                raise ValueError(f"{name} must be a column list, got {value!r}")
            return parts
        return [stripped]
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            # str(None) is the column name "None", which then raised UNKNOWN_COLUMN.
            if item is None:
                continue
            s = item.strip() if isinstance(item, str) else str(item).strip()
            if s:
                result.append(s)
        return result
    raise ValueError(f"{name} must be a column list, got {value!r}")


def _id_like(series: Any) -> bool:
    import numpy as np
    import pandas as pd

    if len(series) < 2:
        return False
    if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
        return False
    try:
        arr = series.to_numpy(dtype=float)
        if not np.isfinite(arr).all():
            return False
        if not (arr % 1 == 0).all():
            return False
        if len(np.unique(arr)) != len(arr):
            return False
        diffs = np.diff(arr)
        if (diffs > 0).all() or (diffs < 0).all():
            return True
    except Exception:  # noqa: S110
        pass
    return False


def auto_numeric(
    df: Any,
    requested: list[str] | None,
    flags: list[str] | None = None,
) -> list[Any]:
    if requested is not None:
        return numeric_columns(df, requested)
    # What was wrong: numeric_columns stringifies labels, so column 0 became "0"
    # and df["0"] raised KeyError on a DataFrame passed into the helper. Skipped
    # date and id columns were also silent, so a scheduling solve could drop a
    # real cost column and still return status: ok.
    # How it happened: resolve_df keeps a DataFrame's columns, and the auto path
    # indexed with the stringified name. dropped_rows already appends to flags;
    # this path did not.
    # Why this change: select select_dtypes(include="number") labels as they are,
    # and append one flag per skipped column when the caller passes a list.
    cols = list(df.select_dtypes(include="number").columns)
    kept: list[Any] = []
    for col in cols:
        name = str(col)
        if _is_date_like_column_name(name):
            if flags is not None:
                flags.append(f"Skipped column {name} (looks like a date); pass the column list explicitly to include it.")
        elif _id_like(df[col]):
            if flags is not None:
                flags.append(f"Skipped column {name} (looks like an id); pass the column list explicitly to include it.")
        else:
            kept.append(col)
    return kept


def numeric_columns(df: Any, columns: list[str] | None = None) -> list[str]:
    """Return *columns* when given (an empty list returns []), else all numeric column names. Unknown names raise."""
    if isinstance(columns, str):
        columns = [columns]
    elif isinstance(columns, tuple):
        columns = list(columns)
    existing_labels = set(_column_labels(df))
    if columns is not None:
        missing = [c for c in columns if c not in existing_labels and c not in df.columns]
        if missing:
            raise ValueError(f"Unknown columns: {', '.join(missing)}")
        return list(columns)
    return _column_labels(df.select_dtypes(include="number"))


def _apply_index_col(df: Any, index_col: int | None) -> Any:
    # A negative index would select the last column. Reject it.
    if index_col is None:
        return df
    idx = int(index_col)
    cols = _column_labels(df)
    n_cols = len(cols)
    if n_cols == 0:
        raise ValueError(f"index_col {idx} is outside the columns (no columns)")
    if idx < 0 or idx >= n_cols:
        raise ValueError(f"index_col {idx} is outside the columns (0..{n_cols - 1})")
    return df.set_index(df.columns[idx])


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

    A DataFrame input is copied, keeps its columns, and ignores header_row.
    Out-of-range header_row and index_col raise ValueError. date_cols converts
    numeric serials/date strings or 1900..2200 integer years to datetime64.
    date_origin applies only to numeric serials (not to integer years).

    Args:
        header_row: Row used as column names, or ``None`` for ``col_0..``.
            Raises ValueError if outside the grid. Ignored for DataFrame input.
        index_col: Optional body column to become the index. Raises ValueError
            if outside the columns.
        parse_strings: Opt-in currency/percent/numeric/datetime string parsing.
        date_cols: Specific column names/indices or True to coerce numeric
            serials/date strings or 1900..2200 integer years to datetime64.
        date_origin: Base epoch for serial numbers (default '1899-12-30').
            Applies only to serials, not integer years.
        sheet_hint: Optional metadata tag.
    """
    import pandas as pd

    if isinstance(data, pd.DataFrame):
        df = data.copy()
        df = _coerce_column_types(
            df,
            parse_strings=parse_strings,
            date_cols=date_cols,
            date_origin=date_origin,
        )
        df = _apply_index_col(df, index_col)
        return CoerceResult(df=df, metadata=_build_metadata(df, sheet_hint=sheet_hint, dropped_rows=0))

    grid = _normalize_input_grid(data)
    dropped_rows = 0

    if not grid:
        empty = pd.DataFrame()
        return CoerceResult(df=empty, metadata=_build_metadata(empty, sheet_hint=sheet_hint, dropped_rows=0))

    if header_row is not None:
        header_idx = int(header_row)
        if header_idx < 0 or header_idx >= len(grid):
            raise ValueError(f"header_row {header_idx} is outside the grid (0..{len(grid) - 1})")
        # Labels, not values. parse_strings on this row turned "00123" into the
        # column name "123.0" and str(2024.0) into "2024.0".
        raw_headers = [_coerce_cell(cell, parse=False) for cell in grid[header_idx]]
        col_names = _dedupe_column_names([header_label(h) for h in raw_headers])
        body = grid[header_idx + 1 :]
    else:
        width = max((len(row) for row in grid), default=0)
        col_names = [f"col_{i}" for i in range(width)]
        body = grid

    rows: list[list[Any]] = []
    for row in body:
        padded = list(row) + [None] * (len(col_names) - len(row))
        coerced_row = [_coerce_cell(cell, parse=parse_strings) for cell in padded[: len(col_names)]]
        if all(cell is None for cell in coerced_row):
            dropped_rows += 1
            continue
        rows.append(coerced_row)

    df = pd.DataFrame(rows, columns=cast("Any", col_names))
    df = _coerce_column_types(df, parse_strings=parse_strings, date_cols=date_cols, date_origin=date_origin)

    df = _apply_index_col(df, index_col)

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


def table_from_df(df: Any, *, name: str, max_rows: int = 50) -> dict[str, Any]:
    limited = df.head(max_rows)
    # where(..., None) on a float column stores NaN again, so decomposition edges
    # became the JSON token NaN and Calc error cells.
    obj_df = limited.astype(object).where(limited.notna(), None)
    return {
        "name": name,
        "columns": [str(c) for c in limited.columns],
        "rows": obj_df.values.tolist(),
        "truncated": len(df) > max_rows,
        "total_rows": int(len(df)),
    }
