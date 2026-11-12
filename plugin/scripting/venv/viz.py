# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv viz compute — runs in user venv worker."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from typing import Any, cast

from plugin.scripting.venv.coerce import (
    column_by_label,
    parse_date_column,
    resolve_columns,
    resolve_df as _resolve_df,
)
from plugin.scripting.helper_registry import helper
from plugin.scripting.venv.results import error_result as _error_result
from plugin.scripting.venv.trusted_dispatch import (
    missing_package_error as _missing_package_error,
    run_domain,
)

log = logging.getLogger(__name__)

_CORR_METHODS = frozenset({"pearson", "kendall", "spearman"})

# The IPC frame cap is 16 MiB (DEFAULT_MAX_PAYLOAD_BYTES). Leave headroom for
# the rest of the result and re-render a dense SVG as PNG above this size.
_MAX_SVG_BYTES = 8 * 1024 * 1024


# --- Core Helper Implementations (Venv Execution Path) ---


def _figure_payload(fig: Any) -> dict[str, Any]:
    # Render only. _open_figure closes the figure, including when savefig raises,
    # so the warm worker does not leak pyplot state into the next cell.
    from plugin.scripting.venv.venv_sandbox import _figure_to_image_payload

    payload = _figure_to_image_payload(fig)
    if payload["format"] == "svg" and len(payload["data"]) > _MAX_SVG_BYTES:
        # A dense SVG can exceed the IPC frame cap.
        payload = _figure_to_image_payload(fig, fmt="png")
    return payload


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


def _require_matplotlib() -> Any | None:
    try:
        import matplotlib.pyplot as plt

        plt.switch_backend("Agg")
        return plt
    except ImportError:
        return None


@helper("viz")
def quick_plot(
    data: Any,
    *,
    x_col: Any = None,
    y_col: Any = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Default line or bar chart of one numeric column.

    Bar when the frame has 12 or fewer rows, otherwise line. ``x_col`` labels
    the bar ticks and is the line chart's x values when that column exists.
    """
    if isinstance(y_col, (list, tuple)):
        return _error_result("INVALID_PARAM", "y_col must be one column name", helper="quick_plot")

    plt = _require_matplotlib()
    if plt is None:
        return _missing_package_error("quick_plot", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if y_col is not None:
        resolved_y = resolve_columns(df, [y_col], numeric=True, helper="quick_plot")
        if isinstance(resolved_y, dict):
            return resolved_y
        y_name = resolved_y[0]
    else:
        numeric = resolve_columns(df, None, numeric=True)
        if isinstance(numeric, dict) or not numeric:
            return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to plot.", helper="quick_plot")
        y_name = numeric[0]

    resolved_x = column_by_label(df, x_col)

    with _open_figure(plt, (8, 4)) as (fig, ax):
        y_float = df[y_name].astype(float)
        chart_type = "bar" if len(df) <= 12 else "line"
        if chart_type == "bar":
            ax.bar(range(len(y_float)), y_float)
            ax.set_xticks(range(len(y_float)))
            if resolved_x is not None:
                ax.set_xticklabels([str(v) for v in df[resolved_x]], rotation=45, ha="right")
        else:
            # The line branch plotted y against the row index. x_col was only
            # applied as bar tick labels, so the same argument changed meaning
            # at 12 rows. Plot that column when it exists.
            if resolved_x is not None:
                ax.plot(df[resolved_x], y_float)
            else:
                ax.plot(y_float)
        ax.set_ylabel(str(y_name))
        ax.set_title(f"Quick plot: {str(y_name)}")
        fig.tight_layout()
        return _ok_viz("quick_plot", fig, chart_type=chart_type, title=ax.get_title())


_CHART_TYPES = frozenset({"line", "scatter", "histogram", "bar"})


def _parse_chart_type(raw: Any) -> str | dict[str, Any]:
    if raw is None or raw == "":
        return "line"
    if isinstance(raw, str):
        stripped = raw.strip().lower()
        if not stripped:
            return "line"
        if stripped in _CHART_TYPES:
            return stripped
    return _error_result(
        "INVALID_PARAM",
        "chart_type must be line, scatter, histogram, or bar",
        helper="plot_data",
    )


@helper("viz")
def plot_data(
    data: Any,
    *,
    spec: dict[str, Any] | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Plot from numeric grid using a small chart spec dict."""
    spec = dict(spec or {})
    chart_type = _parse_chart_type(spec.get("chart_type"))
    if isinstance(chart_type, dict):
        return chart_type

    plt = _require_matplotlib()
    if plt is None:
        return _missing_package_error("plot_data", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    x_col = spec.get("x")
    y_col = spec.get("y")
    # hue is ignored: the tool schema does not expose it and honoring it would mis-label series without real grouping.
    title = str(spec.get("title") or "Plot")

    if y_col is not None:
        resolved_y = resolve_columns(df, [y_col], numeric=True, helper="plot_data")
        if isinstance(resolved_y, dict):
            return resolved_y
        y_name = resolved_y[0]
    else:
        numeric = resolve_columns(df, None, numeric=True)
        if isinstance(numeric, dict) or not numeric:
            return _error_result("NO_NUMERIC_COLUMNS", "No numeric columns to plot.", helper="plot_data")
        y_name = numeric[0]

    resolved_x = column_by_label(df, x_col)

    scatter_x: Any = None
    if chart_type == "scatter":
        if resolved_x is not None:
            rejected_x = resolve_columns(df, [resolved_x], numeric=True, helper="plot_data")
            if isinstance(rejected_x, dict):
                return rejected_x
            scatter_x = resolved_x
        else:
            others = [c for c in df.select_dtypes(include="number").columns if c != y_name]
            if not others:
                return _error_result("NOT_ENOUGH_NUMERIC", "Need at least two numeric columns.", helper="plot_data")
            scatter_x = others[0]

    with _open_figure(plt, (8, 4)) as (fig, ax):
        if chart_type == "scatter" and scatter_x is not None:
            sample = df[[scatter_x, y_name]].dropna()
            ax.scatter(sample[scatter_x].astype(float), sample[y_name].astype(float))
            ax.set_xlabel(str(scatter_x))
            ax.set_ylabel(str(y_name))
        elif chart_type == "histogram":
            ax.hist(df[y_name].dropna().astype(float), bins=min(30, max(5, len(df) // 5)))
            ax.set_xlabel(str(y_name))
        elif chart_type == "bar":
            if resolved_x is not None:
                ax.bar(df[resolved_x].astype(str), df[y_name].astype(float))
                ax.set_xlabel(str(resolved_x))
            else:
                ax.bar(range(len(df)), df[y_name].astype(float))
            ax.set_ylabel(str(y_name))
        else:
            if resolved_x is not None:
                ax.plot(df[resolved_x], df[y_name].astype(float), label=str(y_name))
                ax.set_xlabel(str(resolved_x))
            else:
                ax.plot(df[y_name].astype(float), label=str(y_name))
            ax.set_ylabel(str(y_name))

        ax.set_title(title)
        fig.tight_layout()
        return _ok_viz("plot_data", fig, chart_type=chart_type, title=title)


@helper("viz")
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
            "INVALID_PARAM",
            "method must be pearson, kendall, or spearman",
            helper="correlation_heatmap",
        )

    plt = _require_matplotlib()
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
        try:
            import seaborn as sns

            sns.heatmap(corr, annot=n_cols <= 8, fmt=".2f", cmap="coolwarm", ax=ax)
        except Exception as exc:
            # Non-ImportError seaborn failures fall back to imshow; ImportError stays quiet when seaborn is simply absent.
            if not isinstance(exc, ImportError):
                log.debug("seaborn heatmap failed; using imshow", exc_info=True)
            im = ax.imshow(corr.values, cmap="coolwarm", aspect="auto")
            ax.set_xticks(range(len(corr.columns)))
            ax.set_yticks(range(len(corr.index)))
            ax.set_xticklabels([str(c) for c in corr.columns], rotation=45, ha="right")
            ax.set_yticklabels([str(c) for c in corr.index])
            fig.colorbar(im, ax=ax)

        ax.set_title(f"Correlation ({method_name})")
        fig.tight_layout()
        return _ok_viz("correlation_heatmap", fig, chart_type="heatmap", title=ax.get_title())


@helper("viz")
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
    plt = _require_matplotlib()
    if plt is None:
        return _missing_package_error("time_series_plot", "matplotlib")

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if date_col not in df.columns:
        return _error_result("UNKNOWN_COLUMN", f"Unknown date column {date_col!r}", helper="time_series_plot")

    # When historical_value_col is set, it must exist in the frame so missing columns surface as UNKNOWN_COLUMN instead of silently reusing value_col.
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

    # Rows were plotted in sheet order, so an unsorted date column drew a zig-zag.
    if not dates_all.is_monotonic_increasing:
        order = dates_all.to_numpy().argsort(kind="stable")
        series = series.iloc[order]
        dates_all = dates_all.iloc[order]

    hist_values = cast(pd.Series, pd.to_numeric(series[hist_col], errors="coerce"))
    # Only an all-text value column is an error; a stray text cell is skipped like a blank.
    if series[hist_col].notna().any() and hist_values.isna().all():
        return _error_result("NON_NUMERIC_COLUMN", f"Non-numeric columns: {hist_col}", helper="time_series_plot")
    dated = dates_all.notna()
    hist_mask = dated & hist_values.notna()

    forecast_values: pd.Series | None = None
    forecast_mask: Any = None
    if forecast_col and forecast_col in series.columns:
        forecast_values = cast(pd.Series, pd.to_numeric(series[forecast_col], errors="coerce"))
        assert forecast_values is not None
        forecast_mask = dated & forecast_values.notna()

    lower_values: pd.Series | None = None
    upper_values: pd.Series | None = None
    band_mask: Any = None
    if lower_col and upper_col and lower_col in series.columns and upper_col in series.columns:
        lower_values = cast(pd.Series, pd.to_numeric(series[lower_col], errors="coerce"))
        upper_values = cast(pd.Series, pd.to_numeric(series[upper_col], errors="coerce"))
        assert lower_values is not None and upper_values is not None
        band_mask = dated & lower_values.notna() & upper_values.notna()

    # Require at least one plottable history, forecast, or confidence-band row so all-null forecast columns cannot open an empty figure.
    has_hist = bool(hist_mask.any())
    has_forecast = bool(forecast_mask is not None and forecast_mask.any())
    has_band = bool(band_mask is not None and band_mask.any())
    if not (has_hist or has_forecast or has_band):
        return _error_result("INSUFFICIENT_DATA", "No rows to plot.", helper="time_series_plot")

    with _open_figure(plt, (9, 4)) as (fig, ax):
        if has_hist:
            ax.plot(dates_all[hist_mask], hist_values[hist_mask], label=hist_col)

        if forecast_mask is not None and forecast_mask.any() and forecast_values is not None:
            ax.plot(
                dates_all[forecast_mask],
                forecast_values[forecast_mask],
                linestyle="--",
                label=forecast_col,
            )

        if band_mask is not None and band_mask.any() and lower_values is not None and upper_values is not None:
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


def run_viz(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher for trusted viz helpers."""
    return run_domain("viz", spec, data, context=context)
