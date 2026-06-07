# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv visualization helpers — matplotlib/seaborn plots from sheet data."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from plugin.scripting.analysis_coerce import CoerceResult, coerce_to_dataframe
from plugin.scripting.viz_common import HELPER_NAMES, VIZ_VENV_PIP_INSTALL

__all__ = ["HELPER_NAMES", "run_viz"]


def _error_result(code: str, message: str, *, helper: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    return out


def _missing_package_error(helper: str, package: str) -> dict[str, Any]:
    return _error_result(
        "MISSING_PACKAGE",
        f"{package} is required for {helper}. Install: {VIZ_VENV_PIP_INSTALL}",
        helper=helper,
    )


def _resolve_df(data: Any, *, headers: bool = True, header_row: int = 0, sheet_hint: str | None = None) -> CoerceResult:
    if isinstance(data, CoerceResult):
        return data
    if hasattr(data, "columns") and hasattr(data, "index"):
        df = data.copy()
        meta: dict[str, Any] = {
            "n_rows": int(len(df)),
            "n_cols": int(len(df.columns)),
            "numeric_cols": [str(c) for c in df.select_dtypes(include="number").columns],
        }
        if sheet_hint:
            meta["sheet_hint"] = sheet_hint
        return CoerceResult(df=df, metadata=meta)
    return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)


def _numeric_columns(df: pd.DataFrame, columns: list[str] | None = None) -> list[str]:
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError(f"Unknown columns: {', '.join(missing)}")
        return list(columns)
    return [str(c) for c in df.select_dtypes(include="number").columns]


def _figure_payload(fig: Any) -> dict[str, Any]:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        raise ImportError("matplotlib") from None
    from plugin.scripting.venv_sandbox import _figure_to_image_payload

    payload = _figure_to_image_payload(fig)
    plt.close(fig)
    return payload


def _ok_viz(helper: str, fig: Any, *, chart_type: str, title: str = "", legend: bool = False) -> dict[str, Any]:
    return {
        "status": "ok",
        "helper": helper,
        "image": _figure_payload(fig),
        "title": title or helper,
        "chart_type": chart_type,
        "legend": legend,
        "writer_cleanup_hints": [],
    }


def _require_matplotlib(helper: str) -> Any | None:
    try:
        import matplotlib.pyplot as plt  # type: ignore[import-untyped]

        plt.switch_backend("Agg")
        return plt
    except ImportError:
        return None


def quick_plot(
    data: Any,
    *,
    x_col: str | None = None,
    y_cols: list[str] | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Default line or bar chart from numeric columns."""
    plt = _require_matplotlib("quick_plot")
    if plt is None:
        return _missing_package_error("quick_plot", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    try:
        numeric = _numeric_columns(df, y_cols)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper="quick_plot")
    if not numeric:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to plot.", helper="quick_plot")

    y_name = numeric[0]
    x_values = df[x_col] if x_col and x_col in df.columns else range(len(df))
    y_values = df[y_name]

    fig, ax = plt.subplots(figsize=(8, 4))
    chart_type = "bar" if len(df) <= 12 else "line"
    if chart_type == "bar":
        ax.bar(range(len(y_values)), y_values.astype(float))
        ax.set_xticks(range(len(y_values)))
        if x_col and x_col in df.columns:
            ax.set_xticklabels([str(v) for v in x_values], rotation=45, ha="right")
    else:
        ax.plot(y_values.astype(float))
    ax.set_ylabel(y_name)
    ax.set_title(f"Quick plot: {y_name}")
    fig.tight_layout()
    return _ok_viz("quick_plot", fig, chart_type=chart_type, title=ax.get_title())


def plot_data(
    data: Any,
    *,
    spec: dict[str, Any] | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Plot from numeric grid using a small chart spec dict."""
    plt = _require_matplotlib("plot_data")
    if plt is None:
        return _missing_package_error("plot_data", "matplotlib")

    spec = dict(spec or {})
    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    chart_type = str(spec.get("chart_type") or "line").lower()
    x_col = spec.get("x")
    y_col = spec.get("y")
    hue = spec.get("hue")
    title = str(spec.get("title") or "Plot")

    try:
        numeric = _numeric_columns(df)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper="plot_data")
    if not numeric:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to plot.", helper="plot_data")

    y_name = str(y_col) if y_col in df.columns else numeric[0]
    fig, ax = plt.subplots(figsize=(8, 4))

    if chart_type == "scatter":
        x_name = str(x_col) if x_col in df.columns else (numeric[1] if len(numeric) > 1 else numeric[0])
        sample = df[[x_name, y_name]].dropna()
        ax.scatter(sample[x_name].astype(float), sample[y_name].astype(float))
        ax.set_xlabel(x_name)
        ax.set_ylabel(y_name)
    elif chart_type == "histogram":
        ax.hist(df[y_name].dropna().astype(float), bins=min(30, max(5, len(df) // 5)))
        ax.set_xlabel(y_name)
    elif chart_type == "bar":
        if x_col and x_col in df.columns:
            ax.bar(df[x_col].astype(str), df[y_name].astype(float))
            ax.set_xlabel(str(x_col))
        else:
            ax.bar(range(len(df)), df[y_name].astype(float))
        ax.set_ylabel(y_name)
    else:
        if x_col and x_col in df.columns:
            ax.plot(df[x_col], df[y_name].astype(float), label=str(y_name))
            ax.set_xlabel(str(x_col))
        else:
            ax.plot(df[y_name].astype(float), label=str(y_name))
        ax.set_ylabel(y_name)
        chart_type = "line"

    if hue and hue in df.columns:
        ax.legend(title=str(hue))
    ax.set_title(title)
    fig.tight_layout()
    return _ok_viz("plot_data", fig, chart_type=chart_type, title=title, legend=bool(hue))


def correlation_heatmap(
    data: Any,
    *,
    method: str = "pearson",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Heatmap of pairwise numeric correlations."""
    plt = _require_matplotlib("correlation_heatmap")
    if plt is None:
        return _missing_package_error("correlation_heatmap", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        return _error_result("NOT_ENOUGH_NUMERIC", "Need at least two numeric columns.", helper="correlation_heatmap")

    corr = numeric.corr(method=method)
    fig, ax = plt.subplots(figsize=(max(6, numeric.shape[1]), max(5, numeric.shape[1] - 1)))

    try:
        import seaborn as sns  # type: ignore[import-untyped]

        sns.heatmap(corr, annot=numeric.shape[1] <= 8, fmt=".2f", cmap="coolwarm", ax=ax)
    except ImportError:
        im = ax.imshow(corr.values, cmap="coolwarm", aspect="auto")
        ax.set_xticks(range(len(corr.columns)))
        ax.set_yticks(range(len(corr.index)))
        ax.set_xticklabels([str(c) for c in corr.columns], rotation=45, ha="right")
        ax.set_yticklabels([str(c) for c in corr.index])
        fig.colorbar(im, ax=ax)

    ax.set_title(f"Correlation ({method})")
    fig.tight_layout()
    return _ok_viz("correlation_heatmap", fig, chart_type="heatmap", title=ax.get_title())


def time_series_plot(
    data: Any,
    *,
    date_col: str,
    value_col: str,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Line plot for a date-indexed series."""
    plt = _require_matplotlib("time_series_plot")
    if plt is None:
        return _missing_package_error("time_series_plot", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if date_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown date column {date_col!r}", helper="time_series_plot")
    if value_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown value column {value_col!r}", helper="time_series_plot")

    series = df[[date_col, value_col]].dropna()
    if series.empty:
        return _error_result("INSUFFICIENT_DATA", "No rows to plot.", helper="time_series_plot")

    dates = pd.to_datetime(series[date_col], errors="coerce")
    values = series[value_col].astype(float)
    mask = dates.notna()
    dates = dates[mask]
    values = values[mask]
    if dates.empty:
        return _error_result("INVALID_DATES", "Could not parse date column.", helper="time_series_plot")

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(dates, values)
    ax.set_xlabel(date_col)
    ax.set_ylabel(value_col)
    ax.set_title(f"{value_col} over time")
    fig.autofmt_xdate()
    fig.tight_layout()
    return _ok_viz("time_series_plot", fig, chart_type="line", title=ax.get_title())


def _dispatch_helper(name: str, data: Any, params: dict[str, Any], *, headers: bool, header_row: int, context: dict[str, Any]) -> dict[str, Any]:
    sheet_hint = context.get("sheet_name") if isinstance(context.get("sheet_name"), str) else None
    common: dict[str, Any] = {"headers": headers, "header_row": header_row, "sheet_hint": sheet_hint}

    if name == "quick_plot":
        return quick_plot(data, x_col=params.get("x_col"), y_cols=params.get("y_cols"), **common)
    if name == "plot_data":
        return plot_data(data, spec=params.get("spec") if isinstance(params.get("spec"), dict) else params, **common)
    if name == "correlation_heatmap":
        return correlation_heatmap(data, method=params.get("method", "pearson"), **common)
    if name == "time_series_plot":
        if not params.get("date_col") or not params.get("value_col"):
            return _error_result(
                "MISSING_PARAM",
                "time_series_plot requires params.date_col and params.value_col",
                helper=name,
            )
        return time_series_plot(
            data,
            date_col=str(params["date_col"]),
            value_col=str(params["value_col"]),
            **common,
        )
    return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)


def run_viz(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher for trusted viz helpers."""
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return _error_result("INVALID_SPEC", "spec must be a dict or helper name")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return _error_result("MISSING_HELPER", "helper is required")
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params = spec_dict.get("params")
    if params is None:
        params = {k: v for k, v in spec_dict.items() if k not in ("helper", "headers", "header_row")}
    if not isinstance(params, dict):
        params = {}

    headers = bool(spec_dict.get("headers", True))
    header_row = int(spec_dict.get("header_row", 0))
    ctx = context if isinstance(context, dict) else {}
    return _dispatch_helper(helper, data, params, headers=headers, header_row=header_row, context=ctx)
