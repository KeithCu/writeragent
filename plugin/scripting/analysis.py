# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Trusted venv analysis helpers — Excel/Python-in-Excel inspired standard functions.

Invoked from the LO host through a fixed RPC stub (see analysis_client.py), not
from LLM-submitted code. See docs/analysis-sub-agent.md.
"""
from __future__ import annotations

import logging
from typing import Any, cast

from plugin.scripting.analysis_coerce import CoerceResult, coerce_to_dataframe

log = logging.getLogger(__name__)

MAX_TABLE_ROWS = 50
MAX_COLS = 40

HELPER_NAMES = frozenset(
    {
        "describe_data",
        "kpi_summary",
        "detect_outliers",
        "quick_stats",
        "format_currency",
        "format_percent",
        "clean_and_prepare",
        "pivot_aggregate",
        "group_summary",
        "compare_periods",
        "correlation_matrix",
        "run_regression",
        "cluster_numeric",
        "monte_carlo",
    }
)


def _import_pandas() -> Any:
    import pandas as pd  # type: ignore[import-untyped]

    return pd


def _import_numpy() -> Any:
    import numpy as np

    return np


def _table_from_df(df: Any, *, name: str, max_rows: int = MAX_TABLE_ROWS) -> dict[str, Any]:
    limited = df.head(max_rows)
    return {
        "name": name,
        "columns": [str(c) for c in limited.columns],
        "rows": limited.where(limited.notna(), None).values.tolist(),
        "truncated": len(df) > max_rows,
        "total_rows": int(len(df)),
    }


def _records_from_df(df: Any, *, max_rows: int = MAX_TABLE_ROWS) -> list[dict[str, Any]]:
    limited = df.head(max_rows)
    return limited.where(limited.notna(), None).to_dict(orient="records")


def _markdown_table(columns: list[str], rows: list[list[Any]]) -> str:
    header = "| " + " | ".join(str(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    return "\n".join([header, sep, *body])


def _ok_result(helper: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "helper": helper, **payload}


def _error_result(code: str, message: str, *, helper: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    if details:
        out["details"] = details
    return out


def _resolve_df(data: Any, *, headers: bool = True, header_row: int = 0, sheet_hint: str | None = None) -> CoerceResult:
    if isinstance(data, CoerceResult):
        return data
    pd = _import_pandas()
    if hasattr(data, "columns") and hasattr(data, "index"):
        df = data.copy()
        meta: dict[str, Any] = {
            "n_rows": int(len(df)),
            "n_cols": int(len(df.columns)),
            "numeric_cols": [str(c) for c in df.select_dtypes(include="number").columns],
            "categorical_cols": [str(c) for c in df.select_dtypes(exclude="number").columns if not str(df[c].dtype).startswith("datetime")],
            "datetime_cols": [str(c) for c in df.select_dtypes(include="datetime").columns],
            "dropped_rows": 0,
        }
        if sheet_hint:
            meta["sheet_hint"] = sheet_hint
        return CoerceResult(df=df, metadata=meta)
    return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)


def _numeric_columns(df: Any, columns: list[str] | None = None) -> list[str]:
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError(f"Unknown columns: {', '.join(missing)}")
        return list(columns)
    return [str(c) for c in df.select_dtypes(include="number").columns]


def format_currency(values: Any, *, symbol: str = "$", decimals: int = 2) -> list[str]:
    """Format numeric values as currency strings (Excel init-script helper)."""
    if not isinstance(values, (list, tuple)):
        values = [values]
    out: list[str] = []
    for value in values:
        if value is None:
            out.append("")
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            out.append(str(value))
            continue
        out.append(f"{symbol}{num:,.{decimals}f}")
    return out


def format_percent(values: Any, *, decimals: int = 1) -> list[str]:
    """Format numeric values as percentage strings."""
    if not isinstance(values, (list, tuple)):
        values = [values]
    out: list[str] = []
    for value in values:
        if value is None:
            out.append("")
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            out.append(str(value))
            continue
        out.append(f"{num * 100:.{decimals}f}%")
    return out


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
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if df.empty:
        return _ok_result(
            "describe_data",
            metrics={"row_count": 0, "col_count": 0},
            columns=[],
            tables=[],
            flags=["Empty dataset"],
            metadata=coerced.metadata,
        )

    cols = list(df.columns)[:max_cols]
    limited = df[cols]
    column_summaries: list[dict[str, Any]] = []
    flags: list[str] = []

    for col in cols:
        series = limited[col]
        missing_pct = float(series.isna().mean()) if len(series) else 0.0
        summary: dict[str, Any] = {
            "name": str(col),
            "dtype": str(series.dtype),
            "missing_pct": round(missing_pct, 4),
            "unique_count": int(series.nunique(dropna=True)),
        }
        if str(series.dtype).startswith(("float", "int", "Int", "uint")):
            desc = series.describe()
            summary.update(
                {
                    "mean": _safe_float(desc.get("mean")),
                    "std": _safe_float(desc.get("std")),
                    "min": _safe_float(desc.get("min")),
                    "max": _safe_float(desc.get("max")),
                    "median": _safe_float(series.median()),
                }
            )
            if include_outliers:
                outlier_result = detect_outliers(limited, columns=[str(col)], method="iqr")
                outlier_count = outlier_result.get("metrics", {}).get("outlier_count", 0)
                if outlier_count:
                    flags.append(f"{outlier_count} outliers in {col} (IQR)")
                    summary["outlier_count"] = outlier_count
        column_summaries.append(summary)

    stats_table = _table_from_df(limited.describe(include="all").transpose().reset_index().rename(columns={"index": "column"}), name="describe")

    return _ok_result(
        "describe_data",
        metrics={"row_count": int(len(df)), "col_count": int(len(df.columns))},
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
    metrics: list[str],
    *,
    agg: tuple[str, ...] = ("mean", "min", "max", "sum"),
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Aggregate KPI table for selected numeric columns (Python-in-Excel init helper)."""
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    missing = [m for m in metrics if m not in df.columns]
    if missing:
        return _error_result("UNKNOWN_COLUMN", f"Unknown metrics: {', '.join(missing)}", helper="kpi_summary")
    numeric = _numeric_columns(df, metrics)
    if len(numeric) != len(metrics):
        bad = [m for m in metrics if m not in numeric]
        return _error_result("NON_NUMERIC_COLUMN", f"Non-numeric metrics: {', '.join(bad)}", helper="kpi_summary")

    summary = df[numeric].agg(list(agg)).round(6)
    table = _table_from_df(summary.reset_index().rename(columns={"index": "metric"}), name="kpi_summary")
    return _ok_result(
        "kpi_summary",
        metrics={"metrics": numeric, "aggregations": list(agg)},
        tables=[table],
        metadata=coerced.metadata,
        writer_cleanup_hints={"markdown_table": _markdown_table(cast(list[str], table["columns"]), cast(list[list[Any]], table["rows"]))},
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
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if df.empty:
        return _ok_result("detect_outliers", metrics={"outlier_count": 0}, tables=[], flags=[], metadata=coerced.metadata)

    try:
        numeric_cols = _numeric_columns(df, columns)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper="detect_outliers")

    if not numeric_cols:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to analyze.", helper="detect_outliers")

    np = _import_numpy()
    pd = _import_pandas()
    mask = pd.Series(False, index=df.index)
    per_column: dict[str, int] = {}

    if method == "zscore":
        for col in numeric_cols:
            series = df[col].astype(float)
            std = series.std()
            if std == 0 or np.isnan(std):
                per_column[col] = 0
                continue
            z = (series - series.mean()).abs() / std
            col_mask = z > threshold
            per_column[col] = int(col_mask.sum())
            mask = mask | col_mask
    elif method == "isolation_forest":
        from sklearn.ensemble import IsolationForest  # type: ignore[import-untyped]

        sample = df[numeric_cols].astype(float)
        if sample.empty:
            per_column = {col: 0 for col in numeric_cols}
        else:
            model = IsolationForest(random_state=42, contamination="auto")
            preds = model.fit_predict(sample.fillna(sample.median()))
            mask = pd.Series(preds == -1, index=df.index)
            total = int(mask.sum())
            for col in numeric_cols:
                per_column[col] = total
    else:
        for col in numeric_cols:
            series = df[col].astype(float)
            q1 = series.quantile(0.25)
            q3 = series.quantile(0.75)
            iqr = q3 - q1
            if iqr == 0 or np.isnan(iqr):
                per_column[col] = 0
                continue
            lower = q1 - threshold * iqr
            upper = q3 + threshold * iqr
            col_mask = (series < lower) | (series > upper)
            per_column[col] = int(col_mask.sum())
            mask = mask | col_mask

    outlier_rows = df.loc[mask].copy()
    outlier_rows["_outlier"] = True
    table = _table_from_df(outlier_rows, name="outliers") if not outlier_rows.empty else {"name": "outliers", "columns": [], "rows": [], "truncated": False, "total_rows": 0}
    flags = [f"{count} outliers in {col} ({method})" for col, count in per_column.items() if count]
    return _ok_result(
        "detect_outliers",
        metrics={"outlier_count": int(mask.sum()), "method": method, "per_column": per_column},
        tables=[table],
        flags=flags,
        metadata=coerced.metadata,
    )


class QuickStats:
    """Compact numeric summary card (adapted from Python-in-Excel community patterns)."""

    def __init__(self, data: Any, *, numeric_columns: list[str] | None = None, headers: bool = True, header_row: int = 0, sheet_hint: str | None = None):
        coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
        self.df = coerced.df
        self.metadata = coerced.metadata
        cols = numeric_columns or coerced.metadata.get("numeric_cols") or _numeric_columns(self.df)
        self.numeric_columns = [c for c in cols if c in self.df.columns]
        self.record_count = int(len(self.df))

    def tooltip(self) -> dict[str, Any]:
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
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df.copy()
    actions: list[str] = []

    if drop_duplicates:
        before = len(df)
        df = df.drop_duplicates()
        removed = before - len(df)
        if removed:
            actions.append(f"Dropped {removed} duplicate rows")

    for col in df.columns:
        series = df[col]
        if str(series.dtype).startswith(("float", "int", "Int", "uint")):
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

    table = _table_from_df(df, name="cleaned_data")
    result = _ok_result(
        "clean_and_prepare",
        metrics={"row_count": int(len(df)), "col_count": int(len(df.columns))},
        tables=[table],
        flags=actions,
        metadata={**coerced.metadata, "n_rows": int(len(df))},
    )
    return result


def pivot_aggregate(
    data: Any,
    *,
    index: str | list[str],
    columns: str | list[str] | None = None,
    values: str | list[str],
    aggfunc: str = "sum",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Excel PivotTable wrapper around pandas pivot_table."""
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    try:
        pivoted = df.pivot_table(index=index, columns=columns, values=values, aggfunc=aggfunc, fill_value=0)
    except Exception as exc:
        return _error_result("PIVOT_FAILED", str(exc), helper="pivot_aggregate")
    flat = pivoted.reset_index()
    table = _table_from_df(flat, name="pivot")
    return _ok_result(
        "pivot_aggregate",
        metrics={"row_count": int(len(flat)), "col_count": int(len(flat.columns))},
        tables=[table],
        metadata=coerced.metadata,
        writer_cleanup_hints={"markdown_table": _markdown_table(cast(list[str], table["columns"]), cast(list[list[Any]], table["rows"][:10]))},
    )


def group_summary(
    data: Any,
    *,
    by: str | list[str],
    metrics: list[str],
    aggfunc: str | list[str] | dict[str, str] = "sum",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Group-by aggregate summary (Excel SUBTOTAL / pivot rows)."""
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    missing = [m for m in ([by] if isinstance(by, str) else list(by)) + metrics if m not in df.columns]
    if missing:
        return _error_result("UNKNOWN_COLUMN", f"Unknown columns: {', '.join(missing)}", helper="group_summary")
    grouped = df.groupby(by)[metrics].agg(aggfunc).reset_index()
    table = _table_from_df(grouped, name="group_summary")
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
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """YoY / QoQ style period-over-period change."""
    pd = _import_pandas()
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df.copy()
    if date_col not in df.columns or value_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Need columns {date_col!r} and {value_col!r}", helper="compare_periods")
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col, value_col])
    freq = {"Y": "YE", "Q": "QE", "M": "ME"}.get(period.upper(), period)
    grouped = df.set_index(date_col).sort_index()[value_col].resample(freq).sum().reset_index()
    if calc == "pct_change":
        grouped["change"] = grouped[value_col].pct_change()
    else:
        grouped["change"] = grouped[value_col].diff()
    table = _table_from_df(grouped, name="period_comparison")
    return _ok_result(
        "compare_periods",
        metrics={"periods": int(len(grouped)), "period": period, "calc": calc},
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
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        return _error_result("NOT_ENOUGH_NUMERIC", "Need at least two numeric columns.", helper="correlation_matrix")
    corr = numeric.corr(method=method)
    pairs: list[dict[str, Any]] = []
    cols = list(corr.columns)
    for i, col_a in enumerate(cols):
        for col_b in cols[i + 1 :]:
            value = corr.loc[col_a, col_b]
            if value != value:
                continue
            if abs(float(value)) >= min_abs:
                pairs.append({"column_a": str(col_a), "column_b": str(col_b), "correlation": round(float(value), 6)})
    pairs.sort(key=lambda item: abs(item["correlation"]), reverse=True)
    pairs = pairs[:MAX_TABLE_ROWS]
    table = {
        "name": "correlations",
        "columns": ["column_a", "column_b", "correlation"],
        "rows": [[p["column_a"], p["column_b"], p["correlation"]] for p in pairs],
        "truncated": len(pairs) >= MAX_TABLE_ROWS,
        "total_rows": len(pairs),
    }
    return _ok_result(
        "correlation_matrix",
        metrics={"pair_count": len(pairs), "method": method},
        tables=[table],
        metadata=coerced.metadata,
    )


def run_regression(
    data: Any,
    *,
    target: str,
    features: list[str] | None = None,
    add_constant: bool = True,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """OLS / linear regression — Excel LINEST / trendline equivalent."""
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if target not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown target {target!r}", helper="run_regression")
    feature_cols = features or [c for c in _numeric_columns(df) if c != target]
    if not feature_cols:
        return _error_result("NO_FEATURES", "No feature columns available.", helper="run_regression")
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        return _error_result("UNKNOWN_COLUMN", f"Unknown features: {', '.join(missing)}", helper="run_regression")

    sample = df[[target, *feature_cols]].dropna()
    if len(sample) < len(feature_cols) + 1:
        return _error_result("INSUFFICIENT_DATA", "Not enough rows after dropping missing values.", helper="run_regression")

    y = sample[target].astype(float)
    x = sample[feature_cols].astype(float)
    metrics: dict[str, Any]
    coef_rows: list[list[Any]]

    try:
        import statsmodels.api as sm  # type: ignore[import-untyped]

        design = sm.add_constant(x) if add_constant else x
        model = sm.OLS(y, design).fit()
        names = (["const"] if add_constant else []) + feature_cols
        coef_rows = [[name, round(float(coef), 6)] for name, coef in zip(names, model.params)]
        metrics = {
            "r_squared": round(float(model.rsquared), 6),
            "adj_r_squared": round(float(model.rsquared_adj), 6),
            "n_obs": int(model.nobs),
            "method": "statsmodels_ols",
        }
    except Exception:
        from sklearn.linear_model import LinearRegression  # type: ignore[import-untyped]

        model = LinearRegression()
        model.fit(x, y)
        coef_rows = [[name, round(float(coef), 6)] for name, coef in zip(feature_cols, model.coef_)]
        metrics = {
            "r_squared": round(float(model.score(x, y)), 6),
            "intercept": round(float(model.intercept_), 6),
            "n_obs": int(len(sample)),
            "method": "sklearn_linear_regression",
        }

    coef_table = {
        "name": "coefficients",
        "columns": ["term", "coefficient"],
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
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Cluster numeric columns with sklearn KMeans."""
    from sklearn.cluster import KMeans  # type: ignore[import-untyped]

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    try:
        numeric_cols = _numeric_columns(df, columns)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper="cluster_numeric")
    if not numeric_cols:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to cluster.", helper="cluster_numeric")

    sample = df[numeric_cols].astype(float).fillna(df[numeric_cols].median())
    if sample.empty:
        return _error_result("INSUFFICIENT_DATA", "No rows available for clustering.", helper="cluster_numeric")

    n_clusters = max(1, min(int(n_clusters), len(sample)))
    if method != "kmeans":
        return _error_result("UNSUPPORTED_METHOD", f"Unsupported method {method!r}", helper="cluster_numeric")

    model = KMeans(n_clusters=n_clusters, random_state=42, n_init="auto")
    labels = model.fit_predict(sample)
    counts: dict[int, int] = {}
    for label in labels:
        counts[int(label)] = counts.get(int(label), 0) + 1

    centroids = model.cluster_centers_
    centroid_rows = [[int(i), *[round(float(v), 6) for v in row]] for i, row in enumerate(centroids)]
    centroid_table = {
        "name": "centroids",
        "columns": ["cluster", *numeric_cols],
        "rows": centroid_rows,
        "truncated": False,
        "total_rows": len(centroid_rows),
    }
    return _ok_result(
        "cluster_numeric",
        metrics={"n_clusters": n_clusters, "cluster_sizes": counts, "method": method},
        tables=[centroid_table],
        metadata=coerced.metadata,
    )


def monte_carlo(
    base_value: float,
    uncertainty_pct: float,
    n: int = 10000,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Simple normal perturbation simulation — Excel Data Table / @RISK lite."""
    np = _import_numpy()
    n = max(100, min(int(n), 1_000_000))
    rng = np.random.default_rng(seed)
    sigma = abs(float(base_value) * float(uncertainty_pct))
    draws = rng.normal(loc=float(base_value), scale=sigma, size=n)
    percentiles = {
        "p5": round(float(np.percentile(draws, 5)), 6),
        "p50": round(float(np.percentile(draws, 50)), 6),
        "p95": round(float(np.percentile(draws, 95)), 6),
    }
    hist, edges = np.histogram(draws, bins=10)
    hist_table = {
        "name": "histogram",
        "columns": ["bin_start", "bin_end", "count"],
        "rows": [[round(float(edges[i]), 6), round(float(edges[i + 1]), 6), int(hist[i])] for i in range(len(hist))],
        "truncated": False,
        "total_rows": len(hist),
    }
    return _ok_result(
        "monte_carlo",
        metrics={
            "base_value": float(base_value),
            "uncertainty_pct": float(uncertainty_pct),
            "simulations": n,
            **percentiles,
        },
        tables=[hist_table],
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
        return kpi_summary(data, metrics, agg=tuple(params.get("agg", ("mean", "min", "max", "sum"))), **common)
    if name == "detect_outliers":
        return detect_outliers(data, columns=params.get("columns"), method=params.get("method", "iqr"), threshold=params.get("threshold", 1.5), **common)
    if name == "quick_stats":
        qs = QuickStats(data, numeric_columns=params.get("numeric_columns"), **common)
        return qs.tooltip()
    if name == "format_currency":
        values = params.get("values", data)
        formatted = format_currency(values, symbol=params.get("symbol", "$"), decimals=params.get("decimals", 2))
        return _ok_result(name, metrics={"count": len(formatted)}, tables=[{"name": "formatted", "columns": ["value"], "rows": [[v] for v in formatted], "truncated": False, "total_rows": len(formatted)}])
    if name == "format_percent":
        values = params.get("values", data)
        formatted = format_percent(values, decimals=params.get("decimals", 1))
        return _ok_result(name, metrics={"count": len(formatted)}, tables=[{"name": "formatted", "columns": ["value"], "rows": [[v] for v in formatted], "truncated": False, "total_rows": len(formatted)}])
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
        return compare_periods(data, date_col=params["date_col"], value_col=params["value_col"], period=params.get("period", "Y"), calc=params.get("calc", "pct_change"), **common)
    if name == "correlation_matrix":
        return correlation_matrix(data, method=params.get("method", "pearson"), min_abs=params.get("min_abs", 0.0), **common)
    if name == "run_regression":
        if not params.get("target"):
            return _error_result("MISSING_PARAM", "run_regression requires params.target", helper=name)
        return run_regression(data, target=params["target"], features=params.get("features"), add_constant=params.get("add_constant", True), **common)
    if name == "cluster_numeric":
        return cluster_numeric(data, columns=params.get("columns"), n_clusters=params.get("n_clusters", 3), method=params.get("method", "kmeans"), **common)
    if name == "monte_carlo":
        if params.get("base_value") is None or params.get("uncertainty_pct") is None:
            return _error_result("MISSING_PARAM", "monte_carlo requires params.base_value and params.uncertainty_pct", helper=name)
        return monte_carlo(params["base_value"], params["uncertainty_pct"], n=params.get("n", 10000), seed=params.get("seed"))
    return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)


def run_analysis(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC and future tools."""
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return _error_result("INVALID_SPEC", "spec must be a dict or helper name string")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return _error_result("MISSING_HELPER", "spec.helper is required")
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}
    headers = bool(spec_dict.get("headers", True))
    header_row = int(spec_dict.get("header_row", 0))
    ctx = context if isinstance(context, dict) else {}

    try:
        result = _dispatch_helper(helper, data, params, headers=headers, header_row=header_row, context=ctx)
    except Exception as exc:
        log.exception("Analysis helper %s failed", helper)
        return _error_result("ANALYSIS_FAILED", str(exc), helper=helper)

    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}

    if isinstance(result, dict) and result.get("status") == "ok" and spec_dict.get("return_data") and helper in {"clean_and_prepare", "pivot_aggregate", "group_summary"}:
        coerced = _resolve_df(data, headers=headers, header_row=header_row)
        result["data_records"] = _records_from_df(coerced.df)

    return result
