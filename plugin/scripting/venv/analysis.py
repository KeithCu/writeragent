# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv analysis compute — runs in user venv worker."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from plugin.scripting.venv.coerce import (
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_str_list,
    parse_date_column,
    ok_result as _ok_result,
    error_result as _error_result,
    parse_trusted_spec as _parse_trusted_spec,
    missing_package_error as _missing_package_error,
    numeric_columns as _numeric_columns,
    resolve_df as _resolve_df,
    table_from_df as _table_from_df,
    _dedupe_column_names,
    _is_numeric_dtype,
    _NUMERIC_PROFILE_KEYS,
)
from plugin.scripting.venv.map_range import map_over_range

from plugin.scripting.calc_functions_common import (
    ANALYSIS_HELPER_NAMES as HELPER_NAMES,
    ANALYSIS_MAX_TABLE_ROWS as MAX_TABLE_ROWS,
    ANALYSIS_DATA_TABLE_ROWS as DATA_TABLE_ROWS,
    ANALYSIS_MAX_COLS as MAX_COLS,
)

log = logging.getLogger(__name__)

_AGG_FUNCS = frozenset(
    (
        "count",
        "first",
        "last",
        "max",
        "mean",
        "median",
        "min",
        "nunique",
        "prod",
        "quantile",
        "sem",
        "skew",
        "std",
        "sum",
        "var",
    )
)
_ZERO_FILL_AGGS = frozenset({"sum", "count", "prod", "nunique"})

# --- Core Helper Implementations (Venv Execution Path) ---

def _markdown_table(columns: list[str], rows: list[list[Any]]) -> str:
    header = "| " + " | ".join(str(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _unused in columns) + " |"
    body = ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    return "\n".join([header, sep, *body])


def _coerce_columns(value: Any, name: str, helper: str) -> list[str] | None | dict[str, Any]:
    # A string column list was previously iterated as characters (e.g.
    # metrics="Sales" -> S, a, l, e, s; agg="mean" -> tuple of letters).
    if value is None:
        return None
    try:
        return coerce_str_list(value, name=name)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)


def _reject_non_numeric(df: Any, names: list[str], *, helper: str) -> dict[str, Any] | None:
    # _numeric_columns returns requested names without checking dtype, so
    # .astype(float) raised and run_analysis reported ANALYSIS_FAILED.
    numeric_set = set(df.select_dtypes(include="number").columns)
    bad = [n for n in names if n not in numeric_set]
    if bad:
        return _error_result("NON_NUMERIC_COLUMN", f"Non-numeric columns: {', '.join(bad)}", helper=helper)
    return None


def _resolve_numeric(df: Any, columns: list[str] | None, *, helper: str) -> list[str] | dict[str, Any]:
    try:
        numeric_cols = _numeric_columns(df, columns)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper=helper)
    if columns is not None:
        rejected = _reject_non_numeric(df, numeric_cols, helper=helper)
        if rejected is not None:
            return rejected
    return numeric_cols


def _format_single_currency(value: Any, symbol: str = "$", decimals: int = 2) -> str:
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{symbol}{num:,.{decimals}f}"


def format_currency(values: Any, *, symbol: str = "$", decimals: int = 2) -> Any:
    """Format numeric values as currency strings (Excel init-script helper)."""
    return map_over_range(
        _format_single_currency,
        values,
        symbol=symbol,
        decimals=decimals,
        handle_blanks=True,
    )


def _format_single_percent(value: Any, decimals: int = 1) -> str:
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{num * 100:.{decimals}f}%"


def format_percent(values: Any, *, decimals: int = 1) -> Any:
    """Format numeric values as percentage strings."""
    return map_over_range(
        _format_single_percent,
        values,
        decimals=decimals,
        handle_blanks=True,
    )


def _formatted_cells(formatted: Any) -> list[str]:
    # The spec path previously iterated a scalar string (counting characters)
    # and wrapped each grid row as a single cell, which egress stored as a tuple.
    if isinstance(formatted, (list, tuple)):
        cells: list[str] = []
        for item in formatted:
            if isinstance(item, (list, tuple)):
                cells.extend(str(cell) for cell in item)
            else:
                cells.append(str(item))
        return cells
    return [str(formatted)]


def _describe_columns_from_profile(
    limited: Any,
    variables: dict[str, Any],
    *,
    include_outliers: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map YData Profiling variables JSON into our column summary contract."""
    column_summaries: list[dict[str, Any]] = []
    flags: list[str] = []
    for col in limited.columns:
        col_name = str(col)
        var_info = variables.get(col_name, {})
        summary: dict[str, Any] = {
            "name": col_name,
            "dtype": str(limited[col].dtype),
            "missing_pct": round(float(var_info.get("p_missing", 0.0)), 4),
            "unique_count": var_info.get("n_unique", 0),
        }
        if var_info.get("type") == "Numeric":
            for out_key, in_key in _NUMERIC_PROFILE_KEYS:
                summary[out_key] = _safe_float(var_info.get(in_key))
            if include_outliers:
                outlier_result = detect_outliers(limited, columns=[col_name], method="iqr")
                outlier_count = outlier_result.get("metrics", {}).get("outlier_count", 0)
                if outlier_count:
                    flags.append(f"{outlier_count} outliers in {col_name} (IQR)")
                    summary["outlier_count"] = outlier_count
        column_summaries.append(summary)
    return column_summaries, flags


def describe_data(
    data: Any,
    *,
    include_outliers: bool = True,
    max_cols: int = MAX_COLS,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Extended EDA summary — Excel Data Analysis / describe() plus column quality."""
    try:
        max_cols_val = coerce_int(max_cols, name="max_cols")
        include_outliers_val = coerce_bool(include_outliers, name="include_outliers", default=True)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="describe_data")
    if max_cols_val < 1:
        return _error_result("INVALID_PARAM", f"max_cols must be an integer >= 1, got {max_cols_val!r}", helper="describe_data")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if df.empty:
        return _ok_result(
            "describe_data",
            metrics={"row_count": 0, "col_count": 0, "cols_analyzed": 0},
            columns=[],
            tables=[],
            flags=["Empty dataset"],
            metadata=coerced.metadata,
        )

    cols = list(df.columns)[:max_cols_val]
    limited = df[cols]
    column_summaries: list[dict[str, Any]] = []
    flags: list[str] = []
    profile_messages: list[Any] = []

    try:
        from data_profiling import ProfileReport  # type: ignore[import-not-found, ty:unresolved-import]  # pyright: ignore[reportMissingImports]
    except ImportError:
        return _missing_package_error("describe_data", "fg-data-profiling")

    profile = ProfileReport(limited, minimal=True, progress_bar=False)
    report_json = json.loads(profile.to_json())
    variables = report_json.get("variables", {})
    profile_messages = report_json.get("messages", [])
    column_summaries, flags = _describe_columns_from_profile(
        limited,
        variables,
        include_outliers=include_outliers_val,
    )

    for alert in profile_messages:
        if isinstance(alert, str):
            flags.append(alert)

    if len(df.columns) > len(cols):
        flags.append(f"Analyzed the first {len(cols)} of {len(df.columns)} columns")

    stats_table = _table_from_df(limited.describe(include="all").transpose().reset_index().rename(columns={"index": "column"}), name="describe")

    return _ok_result(
        "describe_data",
        metrics={"row_count": int(len(df)), "col_count": int(len(df.columns)), "cols_analyzed": int(len(cols))},
        columns=column_summaries,
        tables=[stats_table],
        flags=flags,
        metadata=coerced.metadata,
        writer_cleanup_hints={
            "bullets": [
                f"{coerced.metadata.get('n_rows', len(df))} rows × {len(cols)} columns analyzed",
                *(flags[:5]),
            ],
        },
    )


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        pass
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def kpi_summary(
    data: Any,
    metrics: Any,
    *,
    agg: Any = ("mean", "min", "max", "sum"),
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Aggregate KPI table for selected numeric columns (Python-in-Excel init helper)."""
    coerced_metrics = _coerce_columns(metrics, "metrics", "kpi_summary")
    if isinstance(coerced_metrics, dict):
        return coerced_metrics
    if not coerced_metrics:
        return _error_result("MISSING_PARAM", "kpi_summary requires metrics", helper="kpi_summary")

    if agg is None:
        return _error_result("INVALID_PARAM", "agg must list at least one aggregation", helper="kpi_summary")
    coerced_agg = _coerce_columns(agg, "agg", "kpi_summary")
    if isinstance(coerced_agg, dict):
        return coerced_agg
    if not coerced_agg:
        return _error_result("INVALID_PARAM", "agg must list at least one aggregation", helper="kpi_summary")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    missing = [m for m in coerced_metrics if m not in df.columns]
    if missing:
        return _error_result("UNKNOWN_COLUMN", f"Unknown metrics: {', '.join(missing)}", helper="kpi_summary")

    rejected = _reject_non_numeric(df, coerced_metrics, helper="kpi_summary")
    if rejected is not None:
        return rejected

    summary = df[coerced_metrics].agg(list(coerced_agg)).round(6)
    table = _table_from_df(summary.reset_index().rename(columns={"index": "metric"}), name="kpi_summary")
    return _ok_result(
        "kpi_summary",
        metrics={"metrics": coerced_metrics, "aggregations": list(coerced_agg)},
        tables=[table],
        metadata=coerced.metadata,
        writer_cleanup_hints={"markdown_table": _markdown_table(cast("list[str]", table["columns"]), cast("list[list[Any]]", table["rows"]))},
    )


def detect_outliers(
    data: Any,
    *,
    columns: list[str] | None = None,
    method: str = "iqr",
    threshold: float = 1.5,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Flag outliers using IQR, z-score, or sklearn IsolationForest."""
    if not isinstance(method, str):
        return _error_result("INVALID_PARAM", f"method must be iqr, zscore, or isolation_forest, got {method!r}", helper="detect_outliers")
    method_name = method.strip().lower()
    if method_name not in ("iqr", "zscore", "isolation_forest"):
        return _error_result("INVALID_PARAM", f"method must be iqr, zscore, or isolation_forest, got {method!r}", helper="detect_outliers")

    try:
        threshold_val = coerce_float(threshold, name="threshold")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="detect_outliers")

    coerced_cols = _coerce_columns(columns, "columns", "detect_outliers")
    if isinstance(coerced_cols, dict):
        return coerced_cols

    import numpy as np
    import pandas as pd

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if df.empty:
        return _ok_result("detect_outliers", metrics={"outlier_count": 0}, tables=[], flags=[], metadata=coerced.metadata)

    resolved_cols = _resolve_numeric(df, coerced_cols, helper="detect_outliers")
    if isinstance(resolved_cols, dict):
        return resolved_cols
    numeric_cols = resolved_cols

    if not numeric_cols:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to analyze.", helper="detect_outliers")

    sample = df[numeric_cols].astype(float)
    mask = pd.Series(False, index=df.index)
    per_column: dict[str, int] = {col: 0 for col in numeric_cols}

    if method_name == "zscore":
        try:
            from scipy import stats as scipy_stats
        except ImportError:
            # The import sat above every method, so IQR required scipy even though
            # only zscore calls scipy.stats.zscore.
            return _missing_package_error("detect_outliers", "scipy")
        z = np.abs(cast("Any", scipy_stats.zscore(sample.values, axis=0, nan_policy="omit")))
        col_mask = pd.DataFrame(z > threshold_val, index=sample.index, columns=sample.columns).fillna(False)
        mask = col_mask.any(axis=1)
        per_column = {str(col): int(cast("Any", col_mask[col].sum())) for col in numeric_cols}
    elif method_name == "isolation_forest":
        # Missing scikit-learn previously raised an unhandled ImportError when
        # isolation_forest was requested, failing the run_analysis contract.
        # Catching ImportError and returning MISSING_PACKAGE with package name
        # "scikit-learn" matches the shared helper contract.
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError:
            return _missing_package_error("detect_outliers", "scikit-learn")

        # per_column counted non-null cells after fillna(median), so every column
        # got the row count and the flags blamed each column; IsolationForest
        # scores rows; empty per_column plus one row flag says that.
        per_column = {}
        if not sample.empty:
            filled = sample.fillna(sample.median())
            model = IsolationForest(random_state=42, contamination="auto")
            preds = model.fit_predict(filled)
            mask = pd.Series(preds == -1, index=df.index)
    else:
        q1 = sample.quantile(0.25)
        q3 = sample.quantile(0.75)
        iqr = q3 - q1
        zero_iqr = iqr == 0
        lower = q1 - threshold_val * iqr
        upper = q3 + threshold_val * iqr
        col_mask = sample.lt(lower) | sample.gt(upper)
        col_mask = col_mask.mul(~zero_iqr, axis=1)
        mask = col_mask.any(axis=1)
        per_column = {str(col): int(col_mask[col].sum()) for col in numeric_cols}

    outlier_rows = df.loc[mask].copy()
    outlier_rows["_outlier"] = True
    table = _table_from_df(outlier_rows, name="outliers") if not outlier_rows.empty else {"name": "outliers", "columns": [], "rows": [], "truncated": False, "total_rows": 0}
    outlier_count = int(np.asarray(mask, dtype=bool).sum())
    if method_name == "isolation_forest":
        flags = [f"{outlier_count} outlier rows (isolation_forest)"] if outlier_count > 0 else []
    else:
        flags = [f"{count} outliers in {col} ({method_name})" for col, count in per_column.items() if count]
    return _ok_result(
        "detect_outliers",
        metrics={"outlier_count": outlier_count, "method": method_name, "per_column": per_column},
        tables=[table],
        flags=flags,
        metadata=coerced.metadata,
    )


class QuickStats:
    """Compact numeric summary card (adapted from Python-in-Excel community patterns)."""

    df: Any
    metadata: dict[str, Any]
    numeric_columns: list[str]
    record_count: int
    _requested_columns: Any

    def __init__(
        self,
        data: Any,
        *,
        numeric_columns: Any = None,
        headers: bool = True,
        header_row: int = 0,
        sheet_hint: str | None = None,
    ) -> None:
        coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
        self.df = coerced.df
        self.metadata = coerced.metadata
        self.numeric_columns = []
        self._requested_columns = numeric_columns
        self.record_count = int(len(self.df))

    def tooltip(self) -> dict[str, Any]:
        if self._requested_columns is not None:
            coerced_cols = _coerce_columns(self._requested_columns, "numeric_columns", "quick_stats")
            if isinstance(coerced_cols, dict):
                return coerced_cols
            cols_list = coerced_cols or []
            missing = [c for c in cols_list if c not in self.df.columns]
            if missing:
                return _error_result("UNKNOWN_COLUMN", f"Unknown columns: {', '.join(missing)}", helper="quick_stats")
            rejected = _reject_non_numeric(self.df, cols_list, helper="quick_stats")
            if rejected is not None:
                return rejected
            self.numeric_columns = list(cols_list)
        else:
            cols = self.metadata.get("numeric_cols") or _numeric_columns(self.df)
            self.numeric_columns = [c for c in cols if c in self.df.columns]

        columns: list[str] = ["Metric", "Value"]
        rows: list[list[Any]] = [["Records", self.record_count]]
        for col in self.numeric_columns[:8]:
            series = self.df[col].dropna()
            if series.empty:
                continue
            rows.append([f"Avg {col}", round(float(series.mean()), 4)])
            rows.append([f"Min {col}", round(float(series.min()), 4)])
            rows.append([f"Max {col}", round(float(series.max()), 4)])
        table = {"name": "quick_stats", "columns": columns, "rows": rows, "truncated": False, "total_rows": len(rows)}
        return _ok_result(
            "quick_stats",
            metrics={"record_count": self.record_count, "numeric_columns": self.numeric_columns},
            tables=[table],
            metadata=self.metadata,
            writer_cleanup_hints={"markdown_table": _markdown_table(columns, rows)},
        )


def quick_stats(
    data: Any,
    *,
    numeric_columns: Any = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Template entry. ``QuickStats`` is the class; Run Python Script imports this name."""
    return QuickStats(
        data,
        numeric_columns=numeric_columns,
        headers=headers,
        header_row=header_row,
        sheet_hint=sheet_hint,
    ).tooltip()


def clean_and_prepare(
    data: Any,
    *,
    drop_duplicates: bool = False,
    fill_numeric: str = "median",
    fill_categorical: str = "mode",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Lightweight cleaning — type coercion, optional dedupe, simple imputation."""
    try:
        drop_duplicates_val = coerce_bool(drop_duplicates, name="drop_duplicates", default=False)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="clean_and_prepare")

    if not isinstance(fill_numeric, str):
        return _error_result("INVALID_PARAM", f"fill_numeric must be a string, got {fill_numeric!r}", helper="clean_and_prepare")
    if not isinstance(fill_categorical, str):
        return _error_result("INVALID_PARAM", f"fill_categorical must be a string, got {fill_categorical!r}", helper="clean_and_prepare")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df.copy()
    actions: list[str] = []

    if drop_duplicates_val:
        before = len(df)
        df = df.drop_duplicates()
        removed = before - len(df)
        if removed:
            actions.append(f"Dropped {removed} duplicate rows")

    import pandas as pd

    for col in df.columns:
        series = df[col]
        # Prefix check missed Float64 and UInt64 (capital F/U), so a DataFrame
        # passed straight through resolve_df (grids usually infer plain float64)
        # was mode-filled and labeled categorical; _is_numeric_dtype matches
        # those extension dtypes, and complex stays categorical so median cannot
        # drop the imaginary part.
        is_num = _is_numeric_dtype(series.dtype) and not pd.api.types.is_complex_dtype(series.dtype)
        if is_num:
            if fill_numeric == "median":
                fill_value = series.median()
            elif fill_numeric == "mean":
                fill_value = series.mean()
            else:
                fill_value = 0
            if series.isna().any():
                df[col] = series.fillna(fill_value)
                actions.append(f"Filled numeric column {col} with {fill_numeric}")
        else:
            if fill_categorical == "mode":
                mode = series.mode(dropna=True)
                fill_value = mode.iloc[0] if not mode.empty else ""
            else:
                fill_value = ""
            if series.isna().any():
                df[col] = series.fillna(fill_value)
                actions.append(f"Filled categorical column {col} with {fill_categorical}")

    table = _table_from_df(df, name="cleaned_data", max_rows=DATA_TABLE_ROWS)
    result = _ok_result(
        "clean_and_prepare",
        metrics={"row_count": int(len(df)), "col_count": int(len(df.columns))},
        tables=[table],
        flags=actions,
        metadata={**coerced.metadata, "n_rows": int(len(df))},
    )
    return result


def _normalize_aggfunc(
    aggfunc: Any,
    *,
    helper: str,
    key_columns: list[str] | None,
) -> tuple[str | list[str] | dict[str, str], None] | tuple[None, dict[str, Any]]:
    allowed_str = ", ".join(sorted(_AGG_FUNCS))
    if aggfunc is None:
        return None, _error_result(
            "INVALID_PARAM",
            f"aggfunc must be a string, list of strings, or dict of column to string, got {aggfunc!r}",
            helper=helper,
        )

    if isinstance(aggfunc, str):
        val = aggfunc.strip().lower()
        if val not in _AGG_FUNCS:
            return None, _error_result(
                "INVALID_PARAM",
                f"aggfunc must be one of {allowed_str}, got {aggfunc!r}",
                helper=helper,
            )
        return val, None

    if isinstance(aggfunc, list):
        if not aggfunc:
            return None, _error_result(
                "INVALID_PARAM",
                f"aggfunc must name at least one aggregation, got {aggfunc!r}",
                helper=helper,
            )
        normalized_list: list[str] = []
        for item in aggfunc:
            if not isinstance(item, str):
                return None, _error_result(
                    "INVALID_PARAM",
                    f"aggfunc must be a string, list of strings, or dict of column to string, got {item!r}",
                    helper=helper,
                )
            val = item.strip().lower()
            if val not in _AGG_FUNCS:
                return None, _error_result(
                    "INVALID_PARAM",
                    f"aggfunc must be one of {allowed_str}, got {item!r}",
                    helper=helper,
                )
            normalized_list.append(val)
        return normalized_list, None

    if isinstance(aggfunc, dict):
        if not aggfunc:
            return None, _error_result(
                "INVALID_PARAM",
                f"aggfunc must name at least one aggregation, got {aggfunc!r}",
                helper=helper,
            )
        if key_columns is None:
            return None, _error_result(
                "INVALID_PARAM",
                "aggfunc dict requires aggregated columns",
                helper=helper,
            )
        normalized_dict: dict[str, str] = {}
        for k, v in aggfunc.items():
            if not isinstance(k, str):
                return None, _error_result(
                    "INVALID_PARAM",
                    f"aggfunc keys must be column names, got {k!r}",
                    helper=helper,
                )
            key = k.strip()
            if key not in key_columns:
                return None, _error_result(
                    "INVALID_PARAM",
                    f"aggfunc key {key!r} is not in {', '.join(key_columns)}",
                    helper=helper,
                )
            if not isinstance(v, str):
                return None, _error_result(
                    "INVALID_PARAM",
                    f"aggfunc must be a string, list of strings, or dict of column to string, got {v!r}",
                    helper=helper,
                )
            val = v.strip().lower()
            if val not in _AGG_FUNCS:
                return None, _error_result(
                    "INVALID_PARAM",
                    f"aggfunc must be one of {allowed_str}, got {v!r}",
                    helper=helper,
                )
            normalized_dict[key] = val
        return normalized_dict, None

    return None, _error_result(
        "INVALID_PARAM",
        f"aggfunc must be a string, list of strings, or dict of column to string, got {aggfunc!r}",
        helper=helper,
    )


def _flatten_result_columns(df: Any) -> Any:
    # str of a MultiIndex tuple became "('Sales', 'Q1')" and "('Region', '')",
    # which is what egress writes as a header.
    import pandas as pd

    if not isinstance(df.columns, pd.MultiIndex):
        return df

    out_cols: list[str] = []
    for col_tuple in df.columns:
        parts = [
            str(part).strip()
            for part in col_tuple
            if part is not None and str(part).strip() != ""
        ]
        joined = "_".join(parts) if parts else "value"
        out_cols.append(joined)

    deduped = _dedupe_column_names(out_cols)
    res = df.copy()
    res.columns = deduped
    return res


def pivot_aggregate(
    data: Any,
    *,
    index: str | list[str],
    columns: str | list[str] | None = None,
    values: str | list[str],
    aggfunc: str | list[str] | dict[str, str] = "sum",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Excel PivotTable wrapper around pandas pivot_table."""
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df

    key_cols: list[str] | None = None
    if isinstance(aggfunc, dict):
        if isinstance(values, str):
            key_cols = [values]
        elif isinstance(values, list) and all(isinstance(v, str) for v in values):
            key_cols = list(values)
        else:
            return _error_result(
                "INVALID_PARAM",
                "aggfunc dict requires values to be a column name or list of column names",
                helper="pivot_aggregate",
            )

    norm_agg, err = _normalize_aggfunc(aggfunc, helper="pivot_aggregate", key_columns=key_cols)
    if err is not None:
        return err
    assert norm_agg is not None

    # fill_value=0 wrote 0 into missing mean/median groups (South with no Q2 became 0).
    # Zero is an honest empty sum/count/prod/nunique and a lie for the others.
    if isinstance(norm_agg, str):
        all_zero_fill = norm_agg in _ZERO_FILL_AGGS
    elif isinstance(norm_agg, list):
        all_zero_fill = all(a in _ZERO_FILL_AGGS for a in norm_agg)
    elif isinstance(norm_agg, dict):
        all_zero_fill = all(a in _ZERO_FILL_AGGS for a in norm_agg.values())
    else:
        all_zero_fill = False
    fill_value: int | None = 0 if all_zero_fill else None

    try:
        pivoted = df.pivot_table(index=index, columns=columns, values=values, aggfunc=norm_agg, fill_value=fill_value)
    except Exception as exc:
        return _error_result("PIVOT_FAILED", str(exc), helper="pivot_aggregate")
    flat = pivoted.reset_index()
    flat = _flatten_result_columns(flat)
    table = _table_from_df(flat, name="pivot", max_rows=DATA_TABLE_ROWS)
    return _ok_result(
        "pivot_aggregate",
        metrics={"row_count": int(len(flat)), "col_count": int(len(flat.columns))},
        tables=[table],
        metadata=coerced.metadata,
        writer_cleanup_hints={"markdown_table": _markdown_table(cast("list[str]", table["columns"]), cast("list[list[Any]]", table["rows"][:10]))},
    )


def group_summary(
    data: Any,
    *,
    by: Any,
    metrics: Any,
    aggfunc: str | list[str] | dict[str, str] = "sum",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Group-by aggregate summary (Excel SUBTOTAL / pivot rows)."""
    coerced_by = _coerce_columns(by, "by", "group_summary")
    if isinstance(coerced_by, dict):
        return coerced_by
    if not coerced_by:
        return _error_result("MISSING_PARAM", "group_summary requires by and metrics", helper="group_summary")

    coerced_metrics = _coerce_columns(metrics, "metrics", "group_summary")
    if isinstance(coerced_metrics, dict):
        return coerced_metrics
    if not coerced_metrics:
        return _error_result("MISSING_PARAM", "group_summary requires by and metrics", helper="group_summary")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    missing = [m for m in coerced_by + coerced_metrics if m not in df.columns]
    if missing:
        return _error_result("UNKNOWN_COLUMN", f"Unknown columns: {', '.join(missing)}", helper="group_summary")

    rejected = _reject_non_numeric(df, coerced_metrics, helper="group_summary")
    if rejected is not None:
        return rejected

    norm_agg, err = _normalize_aggfunc(aggfunc, helper="group_summary", key_columns=coerced_metrics)
    if err is not None:
        return err
    assert norm_agg is not None

    grouped = df.groupby(coerced_by)[coerced_metrics].agg(norm_agg).reset_index()
    grouped = _flatten_result_columns(grouped)
    table = _table_from_df(grouped, name="group_summary", max_rows=DATA_TABLE_ROWS)
    return _ok_result(
        "group_summary",
        metrics={"group_count": int(len(grouped))},
        tables=[table],
        metadata=coerced.metadata,
    )


def compare_periods(
    data: Any,
    *,
    date_col: str,
    value_col: str,
    period: str = "Y",
    calc: str = "pct_change",
    agg: str = "sum",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """YoY / QoQ style period-over-period change.

    ``agg`` defaults to ``sum`` (revenue). A price or a rate needs ``mean`` or
    ``last`` — summing those was a silent wrong answer.
    """
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df.copy()
    if date_col not in df.columns or value_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Need columns {date_col!r} and {value_col!r}", helper="compare_periods")

    rejected = _reject_non_numeric(df, [value_col], helper="compare_periods")
    if rejected is not None:
        return rejected

    agg_name = str(agg or "sum").strip().lower()
    if agg_name not in ("sum", "mean", "last"):
        return _error_result("INVALID_PARAM", f"agg must be sum, mean, or last, got {agg_name!r}", helper="compare_periods")

    if not isinstance(period, str):
        return _error_result("INVALID_PARAM", f"period must be Y, Q, M, YE, QE, or ME, got {period!r}", helper="compare_periods")
    period_upper = period.strip().upper()
    freq_map = {"Y": "YE", "Q": "QE", "M": "ME", "YE": "YE", "QE": "QE", "ME": "ME"}
    if period_upper not in freq_map:
        return _error_result("INVALID_PARAM", f"period must be Y, Q, M, YE, QE, or ME, got {period!r}", helper="compare_periods")
    freq = freq_map[period_upper]

    if not isinstance(calc, str):
        return _error_result("INVALID_PARAM", f"calc must be pct_change or diff, got {calc!r}", helper="compare_periods")
    calc_lower = calc.strip().lower()
    if calc_lower not in ("pct_change", "diff"):
        return _error_result("INVALID_PARAM", f"calc must be pct_change or diff, got {calc!r}", helper="compare_periods")

    # pd.to_datetime on a Calc serial or a year integer is nanoseconds since 1970,
    # not NaT, so three years became one 1970 period (confirmed: serials 44927,
    # 45292, 45658 and years 2023–2025 each return periods == 1 and revenue 450).
    # read_range leaves serials as floats and resolve_df does not set date_cols.
    # parse_date_column is the same epoch and year window forecast and quant
    # already use. #1486 tightened period / calc / agg and left this line in place.
    df[date_col] = parse_date_column(df[date_col])
    df = df.dropna(subset=[date_col, value_col])
    grouped = df.set_index(date_col).sort_index()[value_col].resample(freq).agg(agg_name).reset_index()
    if calc_lower == "pct_change":
        grouped["change"] = grouped[value_col].pct_change()
    else:
        grouped["change"] = grouped[value_col].diff()
    table = _table_from_df(grouped, name="period_comparison", max_rows=DATA_TABLE_ROWS)
    return _ok_result(
        "compare_periods",
        metrics={"periods": int(len(grouped)), "period": period_upper, "calc": calc_lower, "agg": agg_name},
        tables=[table],
        metadata=coerced.metadata,
    )


def correlation_matrix(
    data: Any,
    *,
    method: str = "pearson",
    min_abs: float = 0.0,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Pairwise correlations — Excel CORREL matrix, top pairs above threshold."""
    if not isinstance(method, str):
        return _error_result("INVALID_PARAM", f"method must be pearson, spearman, or kendall, got {method!r}", helper="correlation_matrix")
    method_name = method.strip().lower()
    if method_name not in ("pearson", "spearman", "kendall"):
        return _error_result("INVALID_PARAM", f"method must be pearson, spearman, or kendall, got {method!r}", helper="correlation_matrix")

    try:
        min_abs_val = coerce_float(min_abs, name="min_abs")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="correlation_matrix")

    # A negative threshold is below every absolute correlation, so the
    # filter kept every finite pair.
    if min_abs_val < 0:
        return _error_result("INVALID_PARAM", f"min_abs must be >= 0, got {min_abs_val!r}", helper="correlation_matrix")

    import pandas as pd

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        return _error_result("NOT_ENOUGH_NUMERIC", "Need at least two numeric columns.", helper="correlation_matrix")
    corr = numeric.corr(method=method_name)
    pairs: list[dict[str, Any]] = []
    cols = list(corr.columns)
    for i, col_a in enumerate(cols):
        for col_b in cols[i + 1 :]:
            value = corr.loc[col_a, col_b]
            if value != value:
                continue
            if abs(float(value)) >= min_abs_val:
                pairs.append({"column_a": str(col_a), "column_b": str(col_b), "correlation": round(float(value), 6)})
    pairs.sort(key=lambda item: abs(item["correlation"]), reverse=True)

    # Slicing happened before pair_count and total_rows were recorded, and
    # truncated used >=, so the true pair count was replaced by 50.
    pairs_df = pd.DataFrame(pairs, columns=["column_a", "column_b", "correlation"])
    table = _table_from_df(pairs_df, name="correlations", max_rows=MAX_TABLE_ROWS)

    return _ok_result(
        "correlation_matrix",
        metrics={"pair_count": table["total_rows"], "method": method_name},
        tables=[table],
        metadata=coerced.metadata,
    )


def run_regression(
    data: Any,
    *,
    target: str,
    features: Any = None,
    add_constant: bool = True,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """OLS / linear regression — Excel LINEST / trendline equivalent."""
    if not isinstance(target, str) or not target.strip():
        return _error_result("INVALID_PARAM", "target must be a column name", helper="run_regression")

    try:
        add_constant_val = coerce_bool(add_constant, name="add_constant", default=True)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="run_regression")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if target not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown target {target!r}", helper="run_regression")

    rejected_target = _reject_non_numeric(df, [target], helper="run_regression")
    if rejected_target is not None:
        return rejected_target

    if features is None:
        feature_cols = [c for c in _numeric_columns(df) if c != target]
    else:
        coerced_features = _coerce_columns(features, "features", "run_regression")
        if isinstance(coerced_features, dict):
            return coerced_features
        feature_cols = list(coerced_features or [])

    if not feature_cols:
        return _error_result("NO_FEATURES", "No feature columns available.", helper="run_regression")

    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        return _error_result("UNKNOWN_COLUMN", f"Unknown features: {', '.join(missing)}", helper="run_regression")

    rejected_features = _reject_non_numeric(df, feature_cols, helper="run_regression")
    if rejected_features is not None:
        return rejected_features

    # dropna keeps ±inf; statsmodels then returned status: ok with r_squared nan
    # and coefficients ±inf (reproduced with y = inf on the third row of
    # 1,2 / 2,4 / 3,inf / 4,8). Replacing inf with NaN and dropping those rows
    # fits the finite sample, or returns INSUFFICIENT_DATA when too few rows remain.
    sample = (
        df[[target, *feature_cols]]
        .replace([float("inf"), float("-inf")], float("nan"))
        .dropna()
    )
    if len(sample) < len(feature_cols) + 1:
        return _error_result("INSUFFICIENT_DATA", "Not enough rows after dropping missing values.", helper="run_regression")

    y = sample[target].astype(float)
    x = sample[feature_cols].astype(float)

    try:
        import statsmodels.api as sm
    except ImportError:
        return _missing_package_error("run_regression", "statsmodels")

    design = sm.add_constant(x) if add_constant_val else x
    model = sm.OLS(y, design).fit()
    # The fit already has uncertainty. A coefficient alone reads as exact.
    ci = model.conf_int()

    def _round_stat(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:  # NaN when the residual variance is zero
            return None
        return round(number, 6)

    coef_rows = []
    for name in model.params.index:
        coef_rows.append(
            [
                str(name),
                _round_stat(model.params[name]),
                _round_stat(model.bse[name]),
                _round_stat(model.pvalues[name]),
                _round_stat(ci.loc[name].iloc[0]),
                _round_stat(ci.loc[name].iloc[1]),
            ]
        )
    metrics = {
        "r_squared": round(float(model.rsquared), 6),
        "adj_r_squared": round(float(model.rsquared_adj), 6),
        "n_obs": int(model.nobs),
        "method": "statsmodels_ols",
    }

    coef_table = {
        "name": "coefficients",
        "columns": ["term", "coefficient", "std_err", "p_value", "ci_low", "ci_high"],
        "rows": coef_rows,
        "truncated": False,
        "total_rows": len(coef_rows),
    }
    return _ok_result(
        "run_regression",
        metrics=metrics,
        tables=[coef_table],
        metadata=coerced.metadata,
    )


def cluster_numeric(
    data: Any,
    *,
    columns: list[str] | None = None,
    n_clusters: int = 3,
    method: str = "kmeans",
    scale: bool = True,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Cluster numeric columns with sklearn KMeans.

    Unscaled KMeans treats a column in the thousands as more important than a
    column in the tens, so the clusters follow magnitude instead of shape.
    ``scale=True`` standardizes first and reports centroids in the original units.
    """
    try:
        n_clusters_val = coerce_int(n_clusters, name="n_clusters")
        scale_val = coerce_bool(scale, name="scale", default=True)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="cluster_numeric")

    coerced_cols = _coerce_columns(columns, "columns", "cluster_numeric")
    if isinstance(coerced_cols, dict):
        return coerced_cols

    import pandas as pd
    # See missing-package comment above IsolationForest import in detect_outliers.
    try:
        from sklearn.cluster import KMeans
    except ImportError:
        return _missing_package_error("cluster_numeric", "scikit-learn")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df

    # _numeric_columns returns requested names without checking dtype, so
    # .astype(float) raised and run_analysis reported ANALYSIS_FAILED.
    resolved_cols = _resolve_numeric(df, coerced_cols, helper="cluster_numeric")
    if isinstance(resolved_cols, dict):
        return resolved_cols
    numeric_cols = resolved_cols

    if not numeric_cols:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to cluster.", helper="cluster_numeric")

    sample = df[numeric_cols].astype(float).fillna(df[numeric_cols].median())
    if sample.empty:
        return _error_result("INSUFFICIENT_DATA", "No rows available for clustering.", helper="cluster_numeric")

    n_clusters_val = max(1, min(n_clusters_val, len(sample)))
    if method != "kmeans":
        return _error_result("UNSUPPORTED_METHOD", f"Unsupported method {method!r}", helper="cluster_numeric")

    values = sample.to_numpy(dtype=float)
    scaler = None
    fitted = values
    if scale_val and values.shape[0] >= 2:
        # See missing-package comment above IsolationForest import in detect_outliers.
        try:
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            return _missing_package_error("cluster_numeric", "scikit-learn")

        scaler = StandardScaler()
        fitted = scaler.fit_transform(values)

    # n_init="auto" is a single k-means++ start on current sklearn, which can
    # lock onto a high-range column even after scaling. A few restarts find
    # the standardized groups.
    model = KMeans(n_clusters=n_clusters_val, random_state=42, n_init=10)  # pyright: ignore[reportArgumentType]
    labels = model.fit_predict(fitted)
    counts: dict[int, int] = {}
    for label in labels:
        counts[int(label)] = counts.get(int(label), 0) + 1

    centers = model.cluster_centers_
    if scaler is not None:
        centers = scaler.inverse_transform(centers)
    centroid_rows = [[int(i), *[round(float(v), 6) for v in row]] for i, row in enumerate(centers)]
    centroid_table = {
        "name": "centroids",
        "columns": ["cluster", *numeric_cols],
        "rows": centroid_rows,
        "truncated": False,
        "total_rows": len(centroid_rows),
    }
    labels_df = pd.DataFrame({"row": [str(idx) for idx in sample.index], "cluster": [int(label) for label in labels]})
    labels_table = _table_from_df(labels_df, name="labels", max_rows=DATA_TABLE_ROWS)

    metrics: dict[str, Any] = {
        "n_clusters": n_clusters_val,
        "cluster_sizes": counts,
        "method": method,
        "scale": bool(scale_val),
    }
    n_labels = len(counts)
    if n_labels >= 2 and len(labels) > n_labels:
        try:
            from sklearn.metrics import silhouette_score

            metrics["silhouette"] = round(float(silhouette_score(fitted, labels)), 6)
        except Exception:
            log.exception("silhouette_score failed")
    return _ok_result(
        "cluster_numeric",
        metrics=metrics,
        tables=[centroid_table, labels_table],
        metadata=coerced.metadata,
    )


def monte_carlo(
    data: Any,
    *,
    sims: int = 100,
    bust: float = -1.0,
    goal: float = 0.0,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Monte Carlo simulation on a numeric series (pandas-montecarlo)."""
    try:
        sims_val = coerce_int(sims, name="sims")
        bust_val = coerce_float(bust, name="bust")
        goal_val = coerce_float(goal, name="goal")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper="monte_carlo")

    try:
        from pandas_montecarlo import montecarlo as pmc_montecarlo
    except ImportError:
        return _missing_package_error("monte_carlo", "pandas-montecarlo")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df

    if df.empty:
        return _error_result("INSUFFICIENT_DATA", "No data for monte_carlo", helper="monte_carlo")

    numeric_cols = _numeric_columns(df)
    if not numeric_cols:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns for monte_carlo", helper="monte_carlo")

    series = df[numeric_cols[0]].dropna()
    if len(series) < 2:
        return _error_result("INSUFFICIENT_DATA", "Need at least 2 data points", helper="monte_carlo")

    sims_val = max(2, min(sims_val, 1_000_000))
    result = pmc_montecarlo(series, sims=sims_val, bust=bust_val, goal=goal_val)
    total = getattr(result, "data", None)
    if total is None or not hasattr(total, "cumsum"):
        return _error_result("MONTE_CARLO_FAILED", "pandas-montecarlo returned unexpected result", helper="monte_carlo")

    cumsum = total.cumsum()
    if "original" not in cumsum.columns:
        return _error_result("MONTE_CARLO_FAILED", "pandas-montecarlo returned unexpected result", helper="monte_carlo")

    import pandas as pd

    # The rename dict previously collapsed two "index" keys, and the summary
    # stats were taken over the whole cumulative grid of an order shuffle whose
    # terminal value does not vary.
    rows_data = []
    for col in cumsum.columns:
        path = cumsum[col]
        rows_data.append({
            "simulation": str(col),
            "final_value": round(float(path.iloc[-1]), 6),
            "trough": round(float(path.min()), 6),
        })
    totals_df = pd.DataFrame(rows_data, columns=["simulation", "final_value", "trough"])
    table = _table_from_df(totals_df, name="monte_carlo_totals", max_rows=MAX_TABLE_ROWS)

    metrics = {
        "simulations": sims_val,
        "final_value": round(float(cumsum["original"].iloc[-1]), 6),
        "bust_prob": float(result.stats["bust"]),
        "goal_prob": float(result.stats["goal"]),
        "worst_trough": round(float(cumsum.min(axis=0).min()), 6),
    }

    return _ok_result(
        "monte_carlo",
        metrics=metrics,
        tables=[table],
        metadata=coerced.metadata,
    )


def _dispatch_helper(name: str, data: Any, params: dict[str, Any], *, headers: bool, header_row: int, context: dict[str, Any]) -> dict[str, Any]:
    sheet_hint = context.get("sheet_name") if isinstance(context.get("sheet_name"), str) else None
    common: dict[str, Any] = {"headers": headers, "header_row": header_row, "sheet_hint": sheet_hint}

    if name == "describe_data":
        return describe_data(data, include_outliers=params.get("include_outliers", True), max_cols=params.get("max_cols", MAX_COLS), **common)
    if name == "kpi_summary":
        metrics = params.get("metrics")
        if not metrics:
            return _error_result("MISSING_PARAM", "kpi_summary requires params.metrics", helper=name)
        return kpi_summary(data, metrics, agg=params.get("agg", ("mean", "min", "max", "sum")), **common)
    if name == "detect_outliers":
        return detect_outliers(data, columns=params.get("columns"), method=params.get("method", "iqr"), threshold=params.get("threshold", 1.5), **common)
    if name == "quick_stats":
        qs = QuickStats(data, numeric_columns=params.get("numeric_columns"), **common)
        return qs.tooltip()
    if name == "format_currency":
        symbol = params.get("symbol", "$")
        if not isinstance(symbol, str):
            return _error_result("INVALID_PARAM", f"symbol must be a string, got {symbol!r}", helper=name)
        try:
            decimals = coerce_int(params.get("decimals", 2), name="decimals")
        except ValueError as exc:
            return _error_result("INVALID_PARAM", str(exc), helper=name)
        if decimals < 0:
            return _error_result("INVALID_PARAM", f"decimals must be an integer >= 0, got {decimals!r}", helper=name)
        values = params.get("values", data)
        formatted = format_currency(values, symbol=symbol, decimals=decimals)
        cells = _formatted_cells(formatted)
        return _ok_result(
            name,
            metrics={"count": len(cells)},
            tables=[{
                "name": "formatted",
                "columns": ["value"],
                "rows": [[c] for c in cells],
                "truncated": False,
                "total_rows": len(cells),
            }],
        )
    if name == "format_percent":
        try:
            decimals = coerce_int(params.get("decimals", 1), name="decimals")
        except ValueError as exc:
            return _error_result("INVALID_PARAM", str(exc), helper=name)
        if decimals < 0:
            return _error_result("INVALID_PARAM", f"decimals must be an integer >= 0, got {decimals!r}", helper=name)
        values = params.get("values", data)
        formatted = format_percent(values, decimals=decimals)
        cells = _formatted_cells(formatted)
        return _ok_result(
            name,
            metrics={"count": len(cells)},
            tables=[{
                "name": "formatted",
                "columns": ["value"],
                "rows": [[c] for c in cells],
                "truncated": False,
                "total_rows": len(cells),
            }],
        )
    if name == "clean_and_prepare":
        return clean_and_prepare(data, drop_duplicates=params.get("drop_duplicates", False), fill_numeric=params.get("fill_numeric", "median"), fill_categorical=params.get("fill_categorical", "mode"), **common)
    if name == "pivot_aggregate":
        if not params.get("index") or not params.get("values"):
            return _error_result("MISSING_PARAM", "pivot_aggregate requires params.index and params.values", helper=name)
        return pivot_aggregate(data, index=params["index"], columns=params.get("columns"), values=params["values"], aggfunc=params.get("aggfunc", "sum"), **common)
    if name == "group_summary":
        if not params.get("by") or not params.get("metrics"):
            return _error_result("MISSING_PARAM", "group_summary requires params.by and params.metrics", helper=name)
        return group_summary(data, by=params["by"], metrics=params["metrics"], aggfunc=params.get("aggfunc", "sum"), **common)
    if name == "compare_periods":
        if not params.get("date_col") or not params.get("value_col"):
            return _error_result("MISSING_PARAM", "compare_periods requires params.date_col and params.value_col", helper=name)
        return compare_periods(
            data,
            date_col=params["date_col"],
            value_col=params["value_col"],
            period=params.get("period", "Y"),
            calc=params.get("calc", "pct_change"),
            agg=params.get("agg", "sum"),
            **common,
        )
    if name == "correlation_matrix":
        return correlation_matrix(data, method=params.get("method", "pearson"), min_abs=params.get("min_abs", 0.0), **common)
    if name == "run_regression":
        if not params.get("target"):
            return _error_result("MISSING_PARAM", "run_regression requires params.target", helper=name)
        return run_regression(data, target=params["target"], features=params.get("features"), add_constant=params.get("add_constant", True), **common)
    if name == "cluster_numeric":
        return cluster_numeric(
            data,
            columns=params.get("columns"),
            n_clusters=params.get("n_clusters", 3),
            method=params.get("method", "kmeans"),
            scale=params.get("scale", True),
            **common,
        )
    if name == "monte_carlo":
        return monte_carlo(data, sims=params.get("sims", 100), bust=params.get("bust", -1.0), goal=params.get("goal", 0.0), **common)
    return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)


def run_analysis(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC and future tools."""
    parsed = _parse_trusted_spec(spec, helper_names=HELPER_NAMES, context=context)
    if isinstance(parsed, dict):
        return parsed
    helper, params, headers, header_row, ctx, _spec = parsed

    try:
        result = _dispatch_helper(helper, data, params, headers=headers, header_row=header_row, context=ctx)
    except Exception as exc:
        log.exception("Analysis helper %s failed", helper)
        return _error_result("ANALYSIS_FAILED", str(exc), helper=helper)

    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}

    return result
