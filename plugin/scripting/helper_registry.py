# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared catalog of trusted scripting helpers for the LibreOffice host and user venv."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class HelperSpec:
    domain: str
    name: str
    description: str
    defaults: tuple[tuple[str, Any], ...]   # template literals; () if none
    bind: str                                 # present|format|spec|time_series|params|symbolic|units|text
    allow: tuple[str, ...] = ()
    reject_unknown: bool = False             # True only for the three optimize helpers
    required_truthy: tuple[str, ...] = ()    # falsy params.get → MISSING_PARAM
    required_present: tuple[str, ...] = ()   # "name" not in params → MISSING_PARAM
    required_message: str = ""
    forbid: tuple[tuple[str, str, str], ...] = ()  # (key, code, message)
    ship: bool = True
    param_help: str = ""


_SPECS: tuple[HelperSpec, ...] = (
    # --- Analysis ---
    HelperSpec(
        domain="analysis",
        name="describe_data",
        description="Extended EDA and column quality summary",
        defaults=(),
        bind="present",
        allow=("include_outliers", "max_cols"),
    ),
    HelperSpec(
        domain="analysis",
        name="kpi_summary",
        description="Mean/min/max/sum for selected numeric columns",
        defaults=(("metrics", ["Column1"]),),
        bind="present",
        allow=("metrics", "agg"),
        required_truthy=("metrics",),
        required_message="kpi_summary requires params.metrics",
    ),
    HelperSpec(
        domain="analysis",
        name="detect_outliers",
        description="Flag outlier rows (IQR, z-score, or isolation forest)",
        defaults=(),
        bind="present",
        allow=("columns", "method", "threshold"),
    ),
    HelperSpec(
        domain="analysis",
        name="quick_stats",
        description="Compact metric card for numeric columns",
        defaults=(),
        bind="present",
        allow=("numeric_columns",),
    ),
    HelperSpec(
        domain="analysis",
        name="format_currency",
        description="Format values as currency strings",
        defaults=(("symbol", "$"), ("decimals", 2)),
        bind="format",
    ),
    HelperSpec(
        domain="analysis",
        name="format_percent",
        description="Format values as percentage strings",
        defaults=(("decimals", 1),),
        bind="format",
    ),
    HelperSpec(
        domain="analysis",
        name="clean_and_prepare",
        description="Light dedupe and imputation",
        defaults=(),
        bind="present",
        allow=("drop_duplicates", "fill_numeric", "fill_categorical"),
    ),
    HelperSpec(
        domain="analysis",
        name="pivot_aggregate",
        description="Pivot table aggregate",
        defaults=(("index", "Category"), ("values", "Amount")),
        bind="present",
        allow=("index", "columns", "values", "aggfunc"),
        required_truthy=("index", "values"),
        required_message="pivot_aggregate requires params.index and params.values",
    ),
    HelperSpec(
        domain="analysis",
        name="group_summary",
        description="Group-by aggregates",
        defaults=(("by", "Region"), ("metrics", ["Sales"])),
        bind="present",
        allow=("by", "metrics", "aggfunc"),
        required_truthy=("by", "metrics"),
        required_message="group_summary requires params.by and params.metrics",
    ),
    HelperSpec(
        domain="analysis",
        name="compare_periods",
        description="Period-over-period change (YoY/QoQ/MoM); agg is sum, mean, or last",
        defaults=(("date_col", "Date"), ("value_col", "Amount"), ("agg", "sum")),
        bind="present",
        allow=("date_col", "value_col", "period", "calc", "agg"),
        required_truthy=("date_col", "value_col"),
        required_message="compare_periods requires params.date_col and params.value_col",
    ),
    HelperSpec(
        domain="analysis",
        name="correlation_matrix",
        description="Pairwise correlations and top pairs",
        defaults=(),
        bind="present",
        allow=("method", "min_abs"),
    ),
    HelperSpec(
        domain="analysis",
        name="run_regression",
        description="OLS linear regression (coefficients, standard errors, p-values, intervals)",
        defaults=(("target", "Y"), ("features", ["X1", "X2"])),
        bind="present",
        allow=("target", "features", "add_constant"),
        required_truthy=("target",),
        required_message="run_regression requires params.target",
    ),
    HelperSpec(
        domain="analysis",
        name="cluster_numeric",
        description="KMeans on standardized numeric columns, with a row-to-cluster table",
        defaults=(),
        bind="present",
        allow=("columns", "n_clusters", "method", "scale"),
    ),
    HelperSpec(
        domain="analysis",
        name="monte_carlo",
        description="Order-shuffle simulation: bust and goal from the cumulative path",
        defaults=(),
        bind="present",
        allow=("sims", "bust", "goal"),
    ),

    # --- Forecast ---
    HelperSpec(
        domain="forecast",
        name="forecast_time_series",
        description="Forward time-series predictions with optional confidence intervals",
        defaults=(("periods", 12), ("model", "auto"), ("date_col", "Date"), ("value_col", "Value")),
        bind="present",
        allow=("periods", "model", "date_col", "value_col", "seasonal_periods"),
        param_help=(
            "periods (int, default 12, minimum 1, capped at 10000), model (auto, holt_winters, "
            "arima, or moving_average; default auto), date_col (default Date) and value_col "
            "(default Value) must be different columns, seasonal_periods (optional int). Date cells "
            "may be ISO strings, Calc serial numbers, or calendar years 1900-2200. model=auto uses "
            "Holt-Winters when at least two seasonal cycles are present, otherwise ARIMA(1,1,1), "
            "otherwise a flat moving average. seasonal_periods applies to auto and holt_winters. "
            "Explicit arima and moving_average ignore it and add a flag."
        ),
    ),
    HelperSpec(
        domain="forecast",
        name="decompose_time_series",
        description="Trend / seasonal / residual decomposition",
        defaults=(("date_col", "Date"), ("value_col", "Value"), ("model", "additive"), ("period", None)),
        bind="present",
        allow=("date_col", "value_col", "model", "period"),
        param_help=(
            "date_col (default Date) and value_col (default Value) must be different columns, "
            "model (additive or multiplicative; default additive), period (optional int greater than 1; "
            "inferred from the dates when omitted)."
        ),
    ),
    HelperSpec(
        domain="forecast",
        name="anomaly_detection_time_series",
        description="Flag temporal outliers via STL residuals and robust z-scores",
        defaults=(
            ("date_col", "Date"),
            ("value_col", "Value"),
            ("period", None),
            ("method", "stl_residual"),
            ("threshold", 3.0),
            ("include_all", False),
        ),
        bind="present",
        allow=("date_col", "value_col", "period", "method", "threshold", "include_all"),
        param_help=(
            "date_col (default Date) and value_col (default Value) must be different columns, "
            "period (optional int greater than 1), method (stl_residual only), threshold (number greater "
            "than 0, default 3), include_all (bool, default false)."
        ),
    ),

    # --- Optimize ---
    HelperSpec(
        domain="optimize",
        name="optimize_portfolio",
        description="Long-only minimum-variance portfolio weights",
        defaults=(("returns_col", None), ("target_return", None), ("risk_free_rate", 0.0)),
        bind="present",
        allow=("returns_col", "target_return", "risk_free_rate"),
        reject_unknown=True,
        param_help=(
            "Long-only minimum-variance weights. Each weight is between 0 and 1 and the weights sum to 1. "
            "returns_col is a column name or a list; when omitted, every numeric column is used except "
            "date-named columns and monotonic unique integer columns such as ids, and each skipped column "
            "is listed in flags. target_return, when set, is a per-period mean constraint. risk_free_rate "
            "(default 0) is used only in the reported Sharpe and does not change the weights. Return and "
            "volatility are per period, not annualized. There is no short selling and no max-Sharpe mode."
        ),
    ),
    HelperSpec(
        domain="optimize",
        name="linear_programming",
        description="Linear programming solver",
        defaults=(("c_col", "c"), ("a_cols", ["a1"]), ("b_col", "b"), ("maximize", False)),
        bind="present",
        allow=("c_col", "a_cols", "b_col", "bounds", "maximize"),
        reject_unknown=True,
        required_present=("c_col", "a_cols", "b_col"),
        required_message="linear_programming requires c_col, a_cols, and b_col",
        param_help=(
            "c_col, a_cols (one name or a list), b_col, bounds (one [low, high] pair applied to every "
            "variable, default [0, null], or one pair per variable), maximize (bool, default false). Each row "
            "is a variable and c_col is its objective coefficient. Each name in a_cols is one <= constraint. "
            "b_col supplies one bound per constraint column; a single constraint column may repeat that same "
            "bound on every row. Only <= constraints are supported. A blank or non-numeric cell drops that "
            "variable row and does not fail the solve. c_col, b_col, and every name in a_cols must be "
            "distinct; a repeat is INVALID_PARAM."
        ),
    ),
    HelperSpec(
        domain="optimize",
        name="solve_scheduling_problem",
        description="Assignment problem solver (e.g., workers to tasks)",
        defaults=(("cost_cols", ["cost1"]),),
        bind="present",
        allow=("cost_cols",),
        reject_unknown=True,
        param_help=(
            "cost_cols is a column name or a list. When omitted, every numeric column is used except "
            "date-named columns and monotonic unique integer columns such as ids, and each skipped column "
            "is listed in flags. The helper minimizes total cost. row_index is the dataframe index of the "
            "assigned row after blank rows are removed, not a sheet row number. metrics.unassigned_rows "
            "counts rows left over when there are more rows than columns. metrics.unassigned_columns "
            "counts columns left over when there are more columns than rows. There is no maximize mode."
        ),
    ),

    # --- Quant ---
    HelperSpec(
        domain="quant",
        name="fetch_historical_data",
        description="Fetch historical prices via yfinance",
        defaults=(
            ("tickers", "AAPL"),
            ("start_date", "2023-01-01"),
            ("end_date", "2024-01-01"),
            ("interval", "1d"),
        ),
        bind="params",
    ),
    HelperSpec(
        domain="quant",
        name="technical_analysis",
        description="Calculate MACD, RSI, and Bollinger Bands",
        defaults=(("indicators", ["macd", "rsi", "bbands"]),),
        bind="params",
    ),
    HelperSpec(
        domain="quant",
        name="portfolio_tearsheet",
        description="Generate performance metrics from a returns sheet via quantstats",
        defaults=(),
        bind="params",
    ),
    HelperSpec(
        domain="quant",
        name="efficient_frontier",
        description="Optimize weights from a returns sheet via PyPortfolioOpt",
        defaults=(),
        bind="params",
    ),

    # --- Viz ---
    HelperSpec(
        domain="viz",
        name="quick_plot",
        description="Auto line/bar chart of one numeric column in the data range.",
        defaults=(),
        bind="present",
        allow=("x_col", "y_col"),
        forbid=(("y_cols", "INVALID_PARAM", "quick_plot takes y_col, not y_cols"),),
    ),
    HelperSpec(
        domain="viz",
        name="plot_data",
        description="Plot Data",
        defaults=(),
        bind="spec",
        ship=False,
    ),
    HelperSpec(
        domain="viz",
        name="correlation_heatmap",
        description="Heatmap of pairwise correlations (matplotlib/seaborn).",
        defaults=(("method", "pearson"),),
        bind="present",
        allow=("method",),
    ),
    HelperSpec(
        domain="viz",
        name="time_series_plot",
        description="Line plot for date_col vs value_col.",
        defaults=(("date_col", "Date"), ("value_col", "Amount")),
        bind="time_series",
        required_truthy=("date_col", "value_col"),
        required_message="time_series_plot requires params.date_col and params.value_col",
    ),

    # --- Symbolic ---
    HelperSpec(
        domain="symbolic",
        name="solve_equation",
        description="Solve an equation for a variable (use = or expression equal to zero).",
        defaults=(("equation", "x**2 - 4"), ("variable", "x")),
        bind="symbolic",
    ),
    HelperSpec(
        domain="symbolic",
        name="symbolic_simplify",
        description="Simplify a symbolic expression.",
        defaults=(("expression", "(x + 1)**2 - x**2 - 2*x"),),
        bind="symbolic",
    ),
    HelperSpec(
        domain="symbolic",
        name="integrate",
        description="Integrate an expression (add lower/upper for definite integrals).",
        defaults=(("expression", "sin(x)"), ("variable", "x")),
        bind="symbolic",
    ),
    HelperSpec(
        domain="symbolic",
        name="differentiate",
        description="Differentiate an expression with respect to a variable (default x).",
        defaults=(("expression", "x**2"), ("variable", "x")),
        bind="symbolic",
    ),
    HelperSpec(
        domain="symbolic",
        name="latex_to_math_object",
        description="Latex To Math Object",
        defaults=(),
        bind="symbolic",
        ship=False,
    ),

    # --- Units ---
    HelperSpec(
        domain="units",
        name="convert_quantity",
        description="Convert a numeric value between units.",
        defaults=(("value", "data"), ("from", "m/s"), ("to", "km/h")),
        bind="units",
    ),
    HelperSpec(
        domain="units",
        name="parse_quantity",
        description="Parse a quantity string (e.g. '10 m/s') into magnitude and units.",
        defaults=(("quantity", "data"),),
        bind="units",
    ),
    HelperSpec(
        domain="units",
        name="format_quantity",
        description="Format a magnitude and unit string for display.",
        defaults=(),
        bind="units",
        ship=False,
    ),
    HelperSpec(
        domain="units",
        name="check_dimensionality",
        description="Check whether two quantities or units are dimensionally compatible.",
        defaults=(("quantity_a", "10 m/s"), ("quantity_b", "5 km/h")),
        bind="units",
    ),

    # --- Text ---
    HelperSpec(
        domain="text",
        name="full",
        description="Full spaCy + textdescriptives analysis (readability, entities, key phrases)",
        defaults=(),
        bind="text",
    ),
    HelperSpec(
        domain="text",
        name="readability",
        description="Readability scores and descriptive stats via textdescriptives",
        defaults=(),
        bind="text",
    ),
    HelperSpec(
        domain="text",
        name="entities",
        description="Named entity recognition (multilingual NER)",
        defaults=(),
        bind="text",
    ),
    HelperSpec(
        domain="text",
        name="key_phrases",
        description="Key phrases via noun chunks (lemmatized)",
        defaults=(),
        bind="text",
    ),
    HelperSpec(
        domain="text",
        name="topics",
        description="Topic modeling (NMF + TF-IDF). Best results when whole document yields section list.",
        defaults=(("n_topics", 4),),
        bind="text",
    ),
    HelperSpec(
        domain="text",
        name="sentiment",
        description="transformers + multilingual model (XLM-RoBERTa default) sentiment (score + label). Best with whole document for per-section results. Override model via JSON config.",
        defaults=(),
        bind="text",
    ),
    HelperSpec(
        domain="text",
        name="diagnostics",
        description="Diagnostics",
        defaults=(),
        bind="text",
        ship=False,
    ),
    HelperSpec(
        domain="text",
        name="check",
        description="Check",
        defaults=(),
        bind="text",
        ship=False,
    ),
)

_SPECS_BY_DOMAIN: dict[str, tuple[HelperSpec, ...]] = {}
_NAMES_BY_DOMAIN: dict[str, frozenset[str]] = {}
_SPEC_BY_DOMAIN_NAME: dict[tuple[str, str], HelperSpec] = {}

for _spec in _SPECS:
    _SPECS_BY_DOMAIN.setdefault(_spec.domain, ())
    _SPECS_BY_DOMAIN[_spec.domain] = (*_SPECS_BY_DOMAIN[_spec.domain], _spec)
    _SPEC_BY_DOMAIN_NAME[(_spec.domain, _spec.name)] = _spec

for _domain, _domain_specs in _SPECS_BY_DOMAIN.items():
    _NAMES_BY_DOMAIN[_domain] = frozenset(s.name for s in _domain_specs)

_BOUND: dict[tuple[str, str], Callable[..., Any]] = {}


def names(domain: str) -> frozenset[str]:
    """Return all helper names declared for *domain*."""
    return _NAMES_BY_DOMAIN.get(domain, frozenset())


def specs(domain: str) -> tuple[HelperSpec, ...]:
    """Return all HelperSpec instances for *domain*."""
    return _SPECS_BY_DOMAIN.get(domain, ())


def get_spec(domain: str, name: str) -> HelperSpec | None:
    """Return the HelperSpec for *(domain, name)*, or None."""
    return _SPEC_BY_DOMAIN_NAME.get((domain, name))


def helper(domain: str, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register a venv implementation callable for *(domain, name)*."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        target_name = name if name is not None else getattr(fn, "__name__", "")
        _BOUND[(domain, target_name)] = fn
        return fn

    return decorator


def bound(domain: str, name: str) -> Callable[..., Any]:
    """Return the registered callable for *(domain, name)*."""
    return _BOUND[(domain, name)]


_TEXT_DIAGNOSTICS_NAMES: frozenset[str] = frozenset({"diagnostics", "check"})


def is_text_diagnostics(helper: object) -> bool:
    """True when *helper* names the text domain's venv check (``diagnostics`` or ``check``).

    Diagnostics take no document text and return an install report instead of an
    analysis, so the dialog, the result check and the child dispatcher branch on it.
    A non-string helper (for example a list from a malformed spec) is False rather
    than a TypeError from hashing it.
    """
    return isinstance(helper, str) and helper in _TEXT_DIAGNOSTICS_NAMES

