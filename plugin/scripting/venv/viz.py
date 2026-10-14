# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv viz compute — runs in user venv worker."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from typing import Any

from plugin.scripting.venv.coerce import (
    error_result as _error_result,
    missing_package_error as _missing_package_error,
    numeric_columns as _numeric_columns,
    parse_date_column,
    resolve_df as _resolve_df,
    _sheet_layout_flags,
)

from plugin.scripting.calc_functions_common import VIZ_HELPER_NAMES as HELPER_NAMES

log = logging.getLogger(__name__)

_CORR_METHODS = frozenset({"pearson", "kendall", "spearman"})


# --- Core Helper Implementations (Venv Execution Path) ---


def _figure_payload(fig: Any) -> dict[str, Any]:
    # Render only. _open_figure closes the figure, including when savefig raises.
    # Closing here used to run only after a successful render, so a failure left
    # the figure registered on the warm worker. The next sandbox cell returns
    # whatever pyplot still has open.
    from plugin.scripting.venv.venv_sandbox import _figure_to_image_payload

    return _figure_to_image_payload(fig)


def _ok_viz(helper: str, fig: Any, *, chart_type: str, title: str = "", legend: bool = False) -> dict[str, Any]:
    # Call only inside _open_figure. This function does not close the figure.
    return {
        "status": "ok",
        "helper": helper,
        "image": _figure_payload(fig),
        "title": title or helper,
        "chart_type": chart_type,
        "legend": legend,
        "writer_cleanup_hints": [],
    }


# Trusted actions never call _close_open_figures (sandbox execute does).
# time_series_plot returned INSUFFICIENT_DATA after plt.subplots, and
# _figure_payload closed only after savefig succeeded, so the figure stayed
# in pyplot's global state for the life of the worker.
@contextmanager
def _open_figure(plt: Any, figsize: tuple[int, int]) -> Iterator[tuple[Any, Any]]:
    fig, ax = plt.subplots(figsize=figsize)
    try:
        yield fig, ax
    finally:
        try:
            plt.close(fig)
        except Exception:
            log.debug("failed to close viz figure", exc_info=True)


def _non_numeric_column(df: Any, name: str, *, helper: str) -> dict[str, Any] | None:
    # numeric_columns returns a requested name that exists and does not check
    # dtype. quick_plot and plot_data then called Series.astype(float), and a
    # text column raised ValueError. The worker reported that as a generic
    # VIZ_ERROR. select_dtypes(include="number") is the same test as
    # analysis._reject_non_numeric, so bool is not numeric.
    if name not in set(df.select_dtypes(include="number").columns):
        return _error_result("NON_NUMERIC_COLUMN", f"Non-numeric columns: {name}", helper=helper)
    return None


def _require_matplotlib(helper: str) -> Any | None:
    try:
        import matplotlib.pyplot as plt

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
    """Default line or bar chart from numeric columns.

    Bar when the frame has 12 or fewer rows, otherwise line. ``x_col`` labels
    the bar ticks and is the line chart's x values when that column exists.
    """
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
    rejected = _non_numeric_column(df, y_name, helper="quick_plot")
    if rejected is not None:
        return rejected
    y_values = df[y_name]

    with _open_figure(plt, (8, 4)) as (fig, ax):
        chart_type = "bar" if len(df) <= 12 else "line"
        if chart_type == "bar":
            ax.bar(range(len(y_values)), y_values.astype(float))
            ax.set_xticks(range(len(y_values)))
            if x_col and x_col in df.columns:
                ax.set_xticklabels([str(v) for v in df[x_col]], rotation=45, ha="right")
        else:
            # The line branch plotted y against the row index. x_col was only
            # applied as bar tick labels, so the same argument changed meaning
            # at 12 rows. Plot that column when it exists.
            if x_col and x_col in df.columns:
                ax.plot(df[x_col], y_values.astype(float))
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
    # hue used to set a legend title and result["legend"] without splitting or
    # coloring series. Nothing in the tool schema passes it. Ignore the key.
    title = str(spec.get("title") or "Plot")

    numeric = _numeric_columns(df)
    if not numeric:
        return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to plot.", helper="plot_data")

    y_name = str(y_col) if y_col in df.columns else numeric[0]
    rejected = _non_numeric_column(df, y_name, helper="plot_data")
    if rejected is not None:
        return rejected

    scatter_x: str | None = None
    if chart_type == "scatter":
        scatter_x = str(x_col) if x_col in df.columns else (numeric[1] if len(numeric) > 1 else numeric[0])
        rejected_x = _non_numeric_column(df, scatter_x, helper="plot_data")
        if rejected_x is not None:
            return rejected_x

    with _open_figure(plt, (8, 4)) as (fig, ax):
        if chart_type == "scatter" and scatter_x is not None:
            sample = df[[scatter_x, y_name]].dropna()
            ax.scatter(sample[scatter_x].astype(float), sample[y_name].astype(float))
            ax.set_xlabel(scatter_x)
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

        ax.set_title(title)
        fig.tight_layout()
        return _ok_viz("plot_data", fig, chart_type=chart_type, title=title)


def correlation_heatmap(
    data: Any,
    *,
    method: str = "pearson",
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Heatmap of pairwise numeric correlations."""
    # DataFrame.corr(method=...) raises ValueError for an unknown method, and
    # the worker turned that into a generic VIZ_ERROR. The allowed set is the
    # three string methods pandas documents.
    method_name = method.strip().lower() if isinstance(method, str) else ""
    if method_name not in _CORR_METHODS:
        return _error_result(
            "INVALID_PARAMS",
            "method must be pearson, kendall, or spearman",
            helper="correlation_heatmap",
        )

    plt = _require_matplotlib("correlation_heatmap")
    if plt is None:
        return _missing_package_error("correlation_heatmap", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        return _error_result("NOT_ENOUGH_NUMERIC", "Need at least two numeric columns.", helper="correlation_heatmap")

    corr = numeric.corr(method=method_name)
    n_cols = int(numeric.shape[1])
    with _open_figure(plt, (max(6, n_cols), max(5, n_cols - 1))) as (fig, ax):
        drew = False
        try:
            import seaborn as sns

            sns.heatmap(corr, annot=n_cols <= 8, fmt=".2f", cmap="coolwarm", ax=ax)
            drew = True
        except ImportError:
            pass
        except Exception:
            # Heatmap used to be guarded only for ImportError. Any other seaborn
            # error skipped the imshow fallback and skipped plt.close.
            log.debug("seaborn heatmap failed; using imshow", exc_info=True)
        if not drew:
            im = ax.imshow(corr.values, cmap="coolwarm", aspect="auto")
            ax.set_xticks(range(len(corr.columns)))
            ax.set_yticks(range(len(corr.index)))
            ax.set_xticklabels([str(c) for c in corr.columns], rotation=45, ha="right")
            ax.set_yticklabels([str(c) for c in corr.index])
            fig.colorbar(im, ax=ax)

        ax.set_title(f"Correlation ({method_name})")
        fig.tight_layout()
        return _ok_viz("correlation_heatmap", fig, chart_type="heatmap", title=ax.get_title())


def time_series_plot(
    data: Any,
    *,
    date_col: str,
    value_col: str,
    forecast_col: str | None = None,
    lower_col: str | None = None,
    upper_col: str | None = None,
    historical_value_col: str | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Line plot for a date-indexed series with optional forecast overlay and bands.

    historical_value_col, when set, must name a column.
    """
    plt = _require_matplotlib("time_series_plot")
    if plt is None:
        return _missing_package_error("time_series_plot", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if date_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown date column {date_col!r}", helper="time_series_plot")

    # historical_value_col used to fall back to value_col when absent from df,
    # silently plotting the wrong data or obscuring the missing column name.
    # When provided, it must name an existing column.
    if historical_value_col:
        if historical_value_col not in df.columns:
            return _error_result(
                "UNKNOWN_COLUMN",
                f"Unknown historical value column {historical_value_col!r}",
                helper="time_series_plot",
            )
        hist_col = historical_value_col
    elif value_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown value column {value_col!r}", helper="time_series_plot")
    else:
        hist_col = value_col

    import pandas as pd
    from typing import cast

    plot_cols = [date_col, hist_col]
    if forecast_col and forecast_col in df.columns:
        plot_cols.append(forecast_col)
    if lower_col and lower_col in df.columns:
        plot_cols.append(lower_col)
    if upper_col and upper_col in df.columns:
        plot_cols.append(upper_col)

    series = df[plot_cols].copy()
    dates_all = cast(pd.Series, parse_date_column(series[date_col]))
    if dates_all.isna().all():
        return _error_result("INVALID_DATES", "Could not parse date column.", helper="time_series_plot")

    hist_values = cast(pd.Series, pd.to_numeric(series[hist_col], errors="coerce"))
    hist_mask = dates_all.notna() & hist_values.notna()

    forecast_values: pd.Series | None = None
    forecast_mask: Any = None
    if forecast_col and forecast_col in series.columns:
        f_vals = cast(pd.Series, pd.to_numeric(series[forecast_col], errors="coerce"))
        forecast_values = f_vals
        forecast_mask = dates_all.notna() & f_vals.notna()

    lower_values: pd.Series | None = None
    upper_values: pd.Series | None = None
    band_mask: Any = None
    if (
        lower_col
        and upper_col
        and lower_col in series.columns
        and upper_col in series.columns
    ):
        l_vals = cast(pd.Series, pd.to_numeric(series[lower_col], errors="coerce"))
        u_vals = cast(pd.Series, pd.to_numeric(series[upper_col], errors="coerce"))
        lower_values = l_vals
        upper_values = u_vals
        band_mask = dates_all.notna() & l_vals.notna() & u_vals.notna()

    if not hist_mask.any() and not (forecast_col and forecast_col in series.columns):
        return _error_result("INSUFFICIENT_DATA", "No rows to plot.", helper="time_series_plot")

    with _open_figure(plt, (9, 4)) as (fig, ax):
        if hist_mask.any():
            ax.plot(dates_all[hist_mask], hist_values[hist_mask], label=hist_col)

        if forecast_mask is not None and forecast_mask.any() and forecast_values is not None:
            ax.plot(
                dates_all[forecast_mask],
                forecast_values[forecast_mask],
                linestyle="--",
                label=forecast_col,
            )

        if (
            band_mask is not None
            and band_mask.any()
            and lower_values is not None
            and upper_values is not None
        ):
            ax.fill_between(
                dates_all[band_mask],
                lower_values[band_mask],
                upper_values[band_mask],
                alpha=0.2,
                label="confidence band",
            )

        ax.set_xlabel(date_col)
        ax.set_ylabel(hist_col)
        ax.set_title(f"{hist_col} over time")
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc="best")
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
            forecast_col=str(params["forecast_col"]) if params.get("forecast_col") else None,
            lower_col=str(params["lower_col"]) if params.get("lower_col") else None,
            upper_col=str(params["upper_col"]) if params.get("upper_col") else None,
            historical_value_col=str(params["historical_value_col"]) if params.get("historical_value_col") else None,
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

    layout = _sheet_layout_flags(spec_dict)
    if isinstance(layout, dict):
        return layout
    headers, header_row = layout
    ctx = context if isinstance(context, dict) else {}
    return _dispatch_helper(helper, data, params, headers=headers, header_row=header_row, context=ctx)
