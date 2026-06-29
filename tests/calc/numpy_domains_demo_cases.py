# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Demo case definitions for the NumPy domains manual QA spreadsheet.

Shared by ``scripts/generate_numpy_domains_demo_spreadsheet.py`` and pytest smoke tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

DomainName = Literal["analysis", "viz", "math", "quant", "optimize", "units"]
CheckMode = Literal["scalar", "visual", "formatted_cell", "grid_egress", "chat_only"]

DOMAIN_SHEET_ORDER: tuple[DomainName, ...] = (
    "analysis",
    "viz",
    "math",
    "quant",
    "optimize",
    "units",
)

# --- Sample grids (mirrors tests/scripting/test_analysis.py) ---

SALES_GRID: list[list[Any]] = [
    ["Region", "Sales", "Units"],
    ["North", "$1,200.50", 10],
    ["South", "800", 8],
    ["North", "$1,500.00", 12],
    ["East", "", 5],
]

DATE_GRID: list[list[Any]] = [
    ["Date", "Revenue"],
    ["2023-01-15", 100],
    ["2023-06-15", 150],
    ["2024-01-15", 200],
    ["2024-06-15", 250],
]

PIVOT_GRID: list[list[Any]] = [
    ["Region", "Quarter", "Sales"],
    ["North", "Q1", 100],
    ["North", "Q2", 120],
    ["South", "Q1", 80],
    ["South", "Q2", 90],
]

MONTE_CARLO_GRID: list[list[Any]] = [
    ["Return"],
    [0.05],
    [-0.02],
    [0.03],
    [0.01],
    [-0.04],
]

REGRESSION_GRID: list[list[Any]] = [["x", "y"], [1, 2], [2, 4], [3, 6], [4, 8]]

OUTLIER_GRID: list[list[Any]] = [["Value"], [1], [2], [3], [4], [100]]

CORRELATION_GRID: list[list[Any]] = [
    ["a", "b", "c"],
    [1, 2, 3],
    [2, 4, 6],
    [3, 6, 9],
]

CLUSTER_GRID: list[list[Any]] = [
    ["a", "b"],
    [1, 1],
    [1.1, 1.2],
    [5, 5],
    [5.2, 4.8],
]

CURRENCY_GRID: list[list[Any]] = [[1234.5]]
PERCENT_GRID: list[list[Any]] = [[0.125]]

# Optimize grids (tests/scripting/test_optimize.py)
LP_GRID: list[list[Any]] = [
    ["c", "a1", "a2", "b"],
    [3, 1, 1, 4],
    [2, 2, 1, 3],
]

SCHEDULING_GRID: list[list[Any]] = [
    ["Task1", "Task2", "Task3"],
    [4, 2, 8],
    [2, 3, 4],
    [8, 1, 2],
]

PORTFOLIO_RETURNS_GRID: list[list[Any]] = [
    ["AAPL", "MSFT", "GOOG"],
    [0.01, 0.008, 0.012],
    [0.02, -0.01, 0.025],
    [-0.005, 0.015, 0.01],
    [0.012, 0.005, -0.008],
    [0.008, 0.02, 0.015],
    [0.015, -0.005, 0.02],
    [-0.01, 0.012, 0.005],
    [0.02, 0.008, 0.018],
    [0.005, 0.015, -0.002],
    [0.01, 0.01, 0.01],
]

# Synthetic OHLCV for technical_analysis
OHLCV_GRID: list[list[Any]] = [
    ["Date", "Open", "High", "Low", "Close", "Volume"],
]
_base_close = 100.0
for day in range(1, 21):
    o = _base_close + day * 0.1
    h = o + 1.5
    low = o - 1.0
    c = o + 0.5
    vol = 1_000_000 + day * 10_000
    OHLCV_GRID.append([f"2024-01-{day:02d}", o, h, low, c, vol])


@dataclass(frozen=True)
class DomainDemoCase:
    id: str
    domain: DomainName
    helper: str
    description: str
    input_grid: list[list[Any]] | None
    params: dict[str, Any]
    python_expr: str | None
    expected_scalar: str
    script_hint: str
    chat_prompt: str | None = None
    notes: str = ""
    requires_package: str | None = None
    requires_network: bool = False
    check_mode: CheckMode = "scalar"


@dataclass(frozen=True)
class GoalSeekSolverBlock:
    id: str
    description: str
    cells: list[tuple[str, int, int, Any]]  # (sheet-relative A1 label, col, row, value or formula)
    chat_prompt: str
    expected: str
    notes: str = ""


@dataclass(frozen=True)
class MatplotlibDemoBlock:
    """Extra viz-sheet block for raw =PYTHON() matplotlib (no DomainDemoCase)."""

    id: str
    description: str
    input_grid: list[list[Any]] | None
    python_expr: str
    expected_scalar: str
    notes: str = ""


def _spec(helper: str, params: dict[str, Any]) -> str:
    return json.dumps({"helper": helper, "params": params}, separators=(",", ":"))


def _analysis_expr(helper: str, params: dict[str, Any], access: str) -> str:
    return f"from writeragent.scripting.analysis import run_analysis; run_analysis({_spec(helper, params)}, data, {{}}){access}"


def _viz_expr(helper: str, params: dict[str, Any], access: str = '["status"]') -> str:
    return f"from writeragent.scripting.viz import run_viz; run_viz({_spec(helper, params)}, data, {{}}){access}"


def _symbolic_expr(helper: str, params: dict[str, Any], access: str) -> str:
    return f"from writeragent.scripting.symbolic import run_symbolic; run_symbolic({_spec(helper, params)}, None, {{}}){access}"


def _quant_expr(helper: str, params: dict[str, Any], access: str, *, use_data: bool) -> str:
    data_arg = "data" if use_data else "None"
    return f"from writeragent.scripting.quant import run_quant; run_quant({_spec(helper, params)}, {data_arg}, {{}}){access}"


def _optimize_expr(helper: str, params: dict[str, Any], access: str) -> str:
    return f"from writeragent.scripting.optimize import run_optimize; run_optimize({_spec(helper, params)}, data, {{}}){access}"


def _units_expr(helper: str, params: dict[str, Any], access: str) -> str:
    return f"from writeragent.scripting.units import run_units; run_units({_spec(helper, params)}, None, {{}}){access}"


def _analysis_cases() -> list[DomainDemoCase]:
    return [
        DomainDemoCase(
            id="describe_data",
            domain="analysis",
            helper="describe_data",
            description="Extended EDA + column quality (ydata-profiling)",
            input_grid=SALES_GRID,
            params={},
            python_expr=_analysis_expr("describe_data", {}, '["metrics"]["row_count"]'),
            expected_scalar="4",
            script_hint="Analysis Helpers → [Analysis] describe_data",
            chat_prompt='analyze_data helper=describe_data data_range=<DATA_RANGE>',
            requires_package="ydata-profiling",
        ),
        DomainDemoCase(
            id="kpi_summary",
            domain="analysis",
            helper="kpi_summary",
            description="Aggregate mean/min/max/sum for metrics",
            input_grid=SALES_GRID,
            params={"metrics": ["Sales", "Units"]},
            python_expr=_analysis_expr("kpi_summary", {"metrics": ["Sales", "Units"]}, '["status"]'),
            expected_scalar="ok",
            script_hint="Analysis Helpers → [Analysis] kpi_summary",
            chat_prompt='analyze_data helper=kpi_summary data_range=<DATA_RANGE> params={"metrics":["Sales","Units"]}',
        ),
        DomainDemoCase(
            id="detect_outliers",
            domain="analysis",
            helper="detect_outliers",
            description="IQR outlier detection (also try zscore / isolation_forest in params)",
            input_grid=OUTLIER_GRID,
            params={"method": "iqr"},
            python_expr=_analysis_expr("detect_outliers", {"method": "iqr"}, '["metrics"]["outlier_count"]'),
            expected_scalar=">= 1",
            script_hint="Analysis Helpers → [Analysis] detect_outliers",
            chat_prompt='analyze_data helper=detect_outliers data_range=<DATA_RANGE> params={"method":"iqr"}',
            notes="Param variants: method=zscore, isolation_forest",
        ),
        DomainDemoCase(
            id="quick_stats",
            domain="analysis",
            helper="quick_stats",
            description="Compact metric card",
            input_grid=SALES_GRID,
            params={},
            python_expr=_analysis_expr("quick_stats", {}, '["metrics"]["record_count"]'),
            expected_scalar="4",
            script_hint="Analysis Helpers → [Analysis] quick_stats",
            chat_prompt="analyze_data helper=quick_stats data_range=<DATA_RANGE>",
        ),
        DomainDemoCase(
            id="format_currency",
            domain="analysis",
            helper="format_currency",
            description="Display formatter — currency",
            input_grid=CURRENCY_GRID,
            params={},
            python_expr=(
                "from writeragent.scripting.analysis import format_currency; "
                "format_currency([data[0][0] if isinstance(data[0], list) else data[0]])[0]"
            ),
            expected_scalar="$1,234.50",
            script_hint="Analysis Helpers → [Analysis] format_currency",
            chat_prompt="analyze_data helper=format_currency data_range=<DATA_RANGE>",
        ),
        DomainDemoCase(
            id="format_percent",
            domain="analysis",
            helper="format_percent",
            description="Display formatter — percent",
            input_grid=PERCENT_GRID,
            params={},
            python_expr=(
                "from writeragent.scripting.analysis import format_percent; "
                "format_percent([data[0][0] if isinstance(data[0], list) else data[0]])[0]"
            ),
            expected_scalar="12.5%",
            script_hint="Analysis Helpers → [Analysis] format_percent",
            chat_prompt="analyze_data helper=format_percent data_range=<DATA_RANGE>",
        ),
        DomainDemoCase(
            id="clean_and_prepare",
            domain="analysis",
            helper="clean_and_prepare",
            description="Dedupe + simple imputation",
            input_grid=SALES_GRID,
            params={"fill_numeric": "median"},
            python_expr=_analysis_expr("clean_and_prepare", {"fill_numeric": "median"}, '["metrics"]["row_count"]'),
            expected_scalar="4",
            script_hint="Analysis Helpers → [Analysis] clean_and_prepare",
            chat_prompt='analyze_data helper=clean_and_prepare data_range=<DATA_RANGE> params={"fill_numeric":"median"}',
        ),
        DomainDemoCase(
            id="pivot_aggregate",
            domain="analysis",
            helper="pivot_aggregate",
            description="Pivot table wrapper",
            input_grid=PIVOT_GRID,
            params={"index": "Region", "columns": "Quarter", "values": "Sales", "aggfunc": "sum"},
            python_expr=_analysis_expr(
                "pivot_aggregate",
                {"index": "Region", "columns": "Quarter", "values": "Sales", "aggfunc": "sum"},
                '["tables"][0]["total_rows"]',
            ),
            expected_scalar=">= 2",
            script_hint="Analysis Helpers → [Analysis] pivot_aggregate",
            chat_prompt=(
                'analyze_data helper=pivot_aggregate data_range=<DATA_RANGE> '
                'params={"index":"Region","columns":"Quarter","values":"Sales","aggfunc":"sum"}'
            ),
        ),
        DomainDemoCase(
            id="group_summary",
            domain="analysis",
            helper="group_summary",
            description="Group-by aggregates",
            input_grid=SALES_GRID,
            params={"by": "Region", "metrics": ["Sales"], "aggfunc": "sum"},
            python_expr=_analysis_expr(
                "group_summary",
                {"by": "Region", "metrics": ["Sales"], "aggfunc": "sum"},
                '["metrics"]["group_count"]',
            ),
            expected_scalar=">= 2",
            script_hint="Analysis Helpers → [Analysis] group_summary",
            chat_prompt=(
                'analyze_data helper=group_summary data_range=<DATA_RANGE> '
                'params={"by":"Region","metrics":["Sales"],"aggfunc":"sum"}'
            ),
        ),
        DomainDemoCase(
            id="compare_periods",
            domain="analysis",
            helper="compare_periods",
            description="YoY/QoQ/MoM comparisons",
            input_grid=DATE_GRID,
            params={"date_col": "Date", "value_col": "Revenue", "period": "Y"},
            python_expr=_analysis_expr(
                "compare_periods",
                {"date_col": "Date", "value_col": "Revenue", "period": "Y"},
                '["status"]',
            ),
            expected_scalar="ok",
            script_hint="Analysis Helpers → [Analysis] compare_periods",
            chat_prompt=(
                'analyze_data helper=compare_periods data_range=<DATA_RANGE> '
                'params={"date_col":"Date","value_col":"Revenue","period":"Y"}'
            ),
            notes="Full report should include a change column in tables",
        ),
        DomainDemoCase(
            id="correlation_matrix",
            domain="analysis",
            helper="correlation_matrix",
            description="Top correlated pairs",
            input_grid=CORRELATION_GRID,
            params={},
            python_expr=_analysis_expr("correlation_matrix", {}, '["metrics"]["pair_count"]'),
            expected_scalar=">= 1",
            script_hint="Analysis Helpers → [Analysis] correlation_matrix",
            chat_prompt="analyze_data helper=correlation_matrix data_range=<DATA_RANGE>",
        ),
        DomainDemoCase(
            id="run_regression",
            domain="analysis",
            helper="run_regression",
            description="OLS via statsmodels",
            input_grid=REGRESSION_GRID,
            params={"target": "y", "features": ["x"]},
            python_expr=_analysis_expr(
                "run_regression",
                {"target": "y", "features": ["x"]},
                '["metrics"]["r_squared"]',
            ),
            expected_scalar="~1.0",
            script_hint="Analysis Helpers → [Analysis] run_regression",
            chat_prompt='analyze_data helper=run_regression data_range=<DATA_RANGE> params={"target":"y","features":["x"]}',
            requires_package="statsmodels",
        ),
        DomainDemoCase(
            id="cluster_numeric",
            domain="analysis",
            helper="cluster_numeric",
            description="KMeans centroids",
            input_grid=CLUSTER_GRID,
            params={"n_clusters": 2},
            python_expr=_analysis_expr("cluster_numeric", {"n_clusters": 2}, '["metrics"]["n_clusters"]'),
            expected_scalar="2",
            script_hint="Analysis Helpers → [Analysis] cluster_numeric",
            chat_prompt='analyze_data helper=cluster_numeric data_range=<DATA_RANGE> params={"n_clusters":2}',
        ),
        DomainDemoCase(
            id="monte_carlo",
            domain="analysis",
            helper="monte_carlo",
            description="Monte Carlo resampling",
            input_grid=MONTE_CARLO_GRID,
            params={"sims": 50, "bust": -0.05, "goal": 0.05},
            python_expr=_analysis_expr(
                "monte_carlo",
                {"sims": 50, "bust": -0.05, "goal": 0.05},
                '["metrics"]["simulations"]',
            ),
            expected_scalar="50",
            script_hint="Analysis Helpers → [Analysis] monte_carlo",
            chat_prompt=(
                'analyze_data helper=monte_carlo data_range=<DATA_RANGE> '
                'params={"sims":50,"bust":-0.05,"goal":0.05}'
            ),
            requires_package="pandas-montecarlo",
        ),
    ]


def _viz_cases() -> list[DomainDemoCase]:
    return [
        DomainDemoCase(
            id="quick_plot",
            domain="viz",
            helper="quick_plot",
            description="Auto line/bar chart from numeric columns",
            input_grid=SALES_GRID,
            params={},
            python_expr=_viz_expr("quick_plot", {}),
            expected_scalar="ok (chart image on sheet after RPS)",
            script_hint="Viz Helpers → [Viz] quick_plot",
            chat_prompt="plot_data helper=quick_plot data_range=<DATA_RANGE>",
            requires_package="matplotlib",
            check_mode="visual",
            notes="Run Python Script inserts chart graphic; =PYTHON() checks status only",
        ),
        DomainDemoCase(
            id="correlation_heatmap",
            domain="viz",
            helper="correlation_heatmap",
            description="Pairwise correlation heatmap (seaborn)",
            input_grid=CORRELATION_GRID,
            params={"method": "pearson"},
            python_expr=_viz_expr("correlation_heatmap", {"method": "pearson"}),
            expected_scalar="ok (heatmap image on sheet after RPS)",
            script_hint="Viz Helpers → [Viz] correlation_heatmap",
            chat_prompt='plot_data helper=correlation_heatmap data_range=<DATA_RANGE> params={"method":"pearson"}',
            requires_package="seaborn",
            check_mode="visual",
        ),
        DomainDemoCase(
            id="time_series_plot",
            domain="viz",
            helper="time_series_plot",
            description="Date-indexed line plot",
            input_grid=DATE_GRID,
            params={"date_col": "Date", "value_col": "Revenue"},
            python_expr=_viz_expr("time_series_plot", {"date_col": "Date", "value_col": "Revenue"}),
            expected_scalar="ok (line chart on sheet after RPS)",
            script_hint="Viz Helpers → [Viz] time_series_plot",
            chat_prompt=(
                'plot_data helper=time_series_plot data_range=<DATA_RANGE> '
                'params={"date_col":"Date","value_col":"Revenue"}'
            ),
            requires_package="matplotlib",
            check_mode="visual",
        ),
    ]


def matplotlib_demo_blocks() -> list[MatplotlibDemoBlock]:
    return [
        MatplotlibDemoBlock(
            id="matplotlib_multi_figure",
            description="Raw matplotlib via =PYTHON() — multiple open figures merge vertically",
            input_grid=None,
            python_expr="import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.plot([3, 2, 1])",
            expected_scalar="Chart image in cell (merged figures)",
            notes="Phase A viz pipeline; lists/dicts of figures use __wa_payload__: image envelope",
        ),
    ]


def _math_cases() -> list[DomainDemoCase]:
    return [
        DomainDemoCase(
            id="solve_equation",
            domain="math",
            helper="solve_equation",
            description="Solve equation for variable (SymPy)",
            input_grid=None,
            params={"equation": "x**2 - 4", "variable": "x"},
            python_expr=_symbolic_expr("solve_equation", {"equation": "x**2 - 4", "variable": "x"}, '["status"]'),
            expected_scalar="ok (2 solutions in full report)",
            script_hint="Math Helpers → [Math] solve_equation",
            chat_prompt='symbolic_math helper=solve_equation params={"equation":"x**2 - 4","variable":"x"}',
            requires_package="sympy",
            notes="RPS inserts Calc grid or Writer LO Math object",
        ),
        DomainDemoCase(
            id="symbolic_simplify",
            domain="math",
            helper="symbolic_simplify",
            description="Simplify symbolic expression",
            input_grid=None,
            params={"expression": "(x + 1)**2 - x**2 - 2*x"},
            python_expr=_symbolic_expr(
                "symbolic_simplify",
                {"expression": "(x + 1)**2 - x**2 - 2*x"},
                '["text"]',
            ),
            expected_scalar="1",
            script_hint="Math Helpers → [Math] symbolic_simplify",
            chat_prompt='symbolic_math helper=symbolic_simplify params={"expression":"(x + 1)**2 - x**2 - 2*x"}',
            requires_package="sympy",
        ),
        DomainDemoCase(
            id="integrate",
            domain="math",
            helper="integrate",
            description="Symbolic integration",
            input_grid=None,
            params={"expression": "sin(x)", "variable": "x"},
            python_expr=_symbolic_expr("integrate", {"expression": "sin(x)", "variable": "x"}, '["status"]'),
            expected_scalar="ok (latex contains cos term)",
            script_hint="Math Helpers → [Math] integrate",
            chat_prompt='symbolic_math helper=integrate params={"expression":"sin(x)","variable":"x"}',
            requires_package="sympy",
        ),
        DomainDemoCase(
            id="differentiate",
            domain="math",
            helper="differentiate",
            description="Symbolic derivative (no shipped RPS template — use header in notes)",
            input_grid=None,
            params={"expression": "x**2", "variable": "x"},
            python_expr=_symbolic_expr("differentiate", {"expression": "x**2", "variable": "x"}, '["text"]'),
            expected_scalar="2*x",
            script_hint="Math Helpers (custom) — # writeragent:math helper=differentiate",
            chat_prompt='symbolic_math helper=differentiate params={"expression":"x**2","variable":"x"}',
            requires_package="sympy",
            notes="Copy template header: # writeragent:math helper=differentiate params={\"expression\":\"x**2\",\"variable\":\"x\"}",
        ),
    ]


def _quant_cases() -> list[DomainDemoCase]:
    fetch_params = {
        "tickers": ["AAPL", "MSFT"],
        "start_date": "2023-01-01",
        "end_date": "2024-01-01",
        "interval": "1d",
    }
    return [
        DomainDemoCase(
            id="fetch_historical_data",
            domain="quant",
            helper="fetch_historical_data",
            description="Fetch OHLCV via yfinance (requires internet)",
            input_grid=None,
            params=fetch_params,
            python_expr=_quant_expr("fetch_historical_data", fetch_params, '["status"]', use_data=False),
            expected_scalar="ok (table rows > 0)",
            script_hint="Quant Helpers → [Quant] fetch_historical_data",
            chat_prompt=None,
            requires_package="yfinance",
            requires_network=True,
            notes="Skip when offline; uv pip install yfinance pandas-ta quantstats pyportfolioopt",
        ),
        DomainDemoCase(
            id="technical_analysis",
            domain="quant",
            helper="technical_analysis",
            description="MACD, RSI, Bollinger Bands on OHLCV grid",
            input_grid=OHLCV_GRID,
            params={"indicators": ["macd", "rsi", "bbands"]},
            python_expr=_quant_expr(
                "technical_analysis",
                {"indicators": ["macd", "rsi", "bbands"]},
                '["status"]',
                use_data=True,
            ),
            expected_scalar="ok",
            script_hint="Quant Helpers → [Quant] technical_analysis",
            chat_prompt=None,
            requires_package="pandas_ta",
        ),
        DomainDemoCase(
            id="portfolio_tearsheet",
            domain="quant",
            helper="portfolio_tearsheet",
            description="Portfolio performance metrics via quantstats",
            input_grid=PORTFOLIO_RETURNS_GRID,
            params={},
            python_expr=_quant_expr("portfolio_tearsheet", {}, '["status"]', use_data=True),
            expected_scalar="ok (metrics + tables)",
            script_hint="Quant Helpers → [Quant] portfolio_tearsheet",
            chat_prompt=None,
            requires_package="quantstats",
        ),
        DomainDemoCase(
            id="efficient_frontier",
            domain="quant",
            helper="efficient_frontier",
            description="Mean-variance frontier via PyPortfolioOpt",
            input_grid=PORTFOLIO_RETURNS_GRID,
            params={},
            python_expr=_quant_expr("efficient_frontier", {}, '["status"]', use_data=True),
            expected_scalar="ok (weights table)",
            script_hint="Quant Helpers → [Quant] efficient_frontier",
            chat_prompt=None,
            requires_package="pypfopt",
        ),
    ]


def _optimize_cases() -> list[DomainDemoCase]:
    return [
        DomainDemoCase(
            id="linear_programming",
            domain="optimize",
            helper="linear_programming",
            description="LP via scipy.optimize.linprog",
            input_grid=LP_GRID,
            params={"c_col": "c", "a_cols": ["a1", "a2"], "b_col": "b", "maximize": True},
            python_expr=_optimize_expr(
                "linear_programming",
                {"c_col": "c", "a_cols": ["a1", "a2"], "b_col": "b", "maximize": True},
                '["status"]',
            ),
            expected_scalar="ok",
            script_hint="Optimize Helpers → [Optimize] linear_programming",
            chat_prompt=(
                'optimize_data helper=linear_programming data_range=<DATA_RANGE> '
                'params={"c_col":"c","a_cols":["a1","a2"],"b_col":"b","maximize":true}'
            ),
        ),
        DomainDemoCase(
            id="optimize_portfolio",
            domain="optimize",
            helper="optimize_portfolio",
            description="Mean-variance portfolio weights",
            input_grid=PORTFOLIO_RETURNS_GRID,
            params={"returns_col": ["AAPL", "MSFT", "GOOG"]},
            python_expr=_optimize_expr(
                "optimize_portfolio",
                {"returns_col": ["AAPL", "MSFT", "GOOG"]},
                '["status"]',
            ),
            expected_scalar="ok",
            script_hint="Optimize Helpers → [Optimize] optimize_portfolio",
            chat_prompt=(
                'optimize_data helper=optimize_portfolio data_range=<DATA_RANGE> '
                'params={"returns_col":["AAPL","MSFT","GOOG"]}'
            ),
        ),
        DomainDemoCase(
            id="solve_scheduling_problem",
            domain="optimize",
            helper="solve_scheduling_problem",
            description="Assignment problem (Hungarian algorithm)",
            input_grid=SCHEDULING_GRID,
            params={"cost_cols": ["Task1", "Task2", "Task3"]},
            python_expr=_optimize_expr(
                "solve_scheduling_problem",
                {"cost_cols": ["Task1", "Task2", "Task3"]},
                '["metrics"]["total_cost"]',
            ),
            expected_scalar="6.0",
            script_hint="Optimize Helpers → [Optimize] solve_scheduling_problem",
            chat_prompt=(
                'optimize_data helper=solve_scheduling_problem data_range=<DATA_RANGE> '
                'params={"cost_cols":["Task1","Task2","Task3"]}'
            ),
        ),
    ]


def _units_cases() -> list[DomainDemoCase]:
    return [
        DomainDemoCase(
            id="convert_quantity",
            domain="units",
            helper="convert_quantity",
            description="Convert 10 m/s → km/h (single formatted cell)",
            input_grid=None,
            params={"value": "10", "from_unit": "m/s", "to_unit": "km/h"},
            python_expr=_units_expr(
                "convert_quantity",
                {"value": "10", "from_unit": "m/s", "to_unit": "km/h"},
                '["formatted"]',
            ),
            expected_scalar="36 km/h",
            script_hint="Units Helpers → [Units] convert_quantity",
            chat_prompt=None,
            requires_package="pint",
            check_mode="formatted_cell",
        ),
        DomainDemoCase(
            id="parse_quantity",
            domain="units",
            helper="parse_quantity",
            description="Parse quantity string",
            input_grid=None,
            params={"quantity": "5 km/h"},
            python_expr=_units_expr("parse_quantity", {"quantity": "5 km/h"}, '["magnitude"]'),
            expected_scalar="5",
            script_hint="Units Helpers → [Units] parse_quantity",
            chat_prompt=None,
            requires_package="pint",
            check_mode="formatted_cell",
        ),
        DomainDemoCase(
            id="format_quantity",
            domain="units",
            helper="format_quantity",
            description="Format magnitude + units (no shipped RPS template)",
            input_grid=None,
            params={"magnitude": "3.5", "units": "m"},
            python_expr=_units_expr("format_quantity", {"magnitude": "3.5", "units": "m"}, '["formatted"]'),
            expected_scalar="contains 3.5",
            script_hint="Units Helpers (custom) — # writeragent:units helper=format_quantity",
            chat_prompt=None,
            requires_package="pint",
            check_mode="formatted_cell",
            notes='Header: # writeragent:units helper=format_quantity params={"magnitude":"3.5","units":"m"}',
        ),
        DomainDemoCase(
            id="check_dimensionality",
            domain="units",
            helper="check_dimensionality",
            description="Dimensional compatibility check",
            input_grid=None,
            params={
                "quantity_a": "10 m/s",
                "quantity_b": "5 km/h",
                "output_style": "detailed",
            },
            python_expr=_units_expr(
                "check_dimensionality",
                {"quantity_a": "10 m/s", "quantity_b": "5 km/h"},
                '["compatible"]',
            ),
            expected_scalar="True",
            script_hint="Units Helpers → [Units] check_dimensionality",
            chat_prompt=None,
            requires_package="pint",
            check_mode="grid_egress",
            notes='Use output_style:"detailed" in params for full key-value grid',
        ),
    ]


def analysis_demo_cases() -> list[DomainDemoCase]:
    return _analysis_cases()


def viz_demo_cases() -> list[DomainDemoCase]:
    return _viz_cases()


def math_demo_cases() -> list[DomainDemoCase]:
    return _math_cases()


def quant_demo_cases() -> list[DomainDemoCase]:
    return _quant_cases()


def optimize_demo_cases() -> list[DomainDemoCase]:
    return _optimize_cases()


def units_demo_cases() -> list[DomainDemoCase]:
    return _units_cases()


def cases_for_domain(domain: DomainName) -> list[DomainDemoCase]:
    builders = {
        "analysis": _analysis_cases,
        "viz": _viz_cases,
        "math": _math_cases,
        "quant": _quant_cases,
        "optimize": _optimize_cases,
        "units": _units_cases,
    }
    return builders[domain]()


def all_domain_demo_cases() -> list[DomainDemoCase]:
    out: list[DomainDemoCase] = []
    for domain in DOMAIN_SHEET_ORDER:
        out.extend(cases_for_domain(domain))
    return out


def goal_seek_solver_layout() -> list[GoalSeekSolverBlock]:
    return [
        GoalSeekSolverBlock(
            id="goal_seek_square",
            description="Find x such that x^2 = 100",
            cells=[
                ("A1 (variable x)", 0, 0, 1.0),
                ("B1 (=A1^2)", 1, 0, "=A1^2"),
            ],
            chat_prompt=(
                "calc_goal_seek formula_cell=goal_seek_solver.B1 variable_cell=goal_seek_solver.A1 "
                "target_value=100 apply_result=true"
            ),
            expected="|x| ≈ 10",
            notes="Use WriterAgent chat with domain=analysis or MCP calc_goal_seek",
        ),
        GoalSeekSolverBlock(
            id="solver_lp",
            description="Maximize 3x+5y subject to x+y<=10, x,y>=0",
            cells=[
                ("A3 (x)", 0, 2, 1.0),
                ("B3 (y)", 1, 2, 1.0),
                ("C3 objective", 2, 2, "=3*A3+5*B3"),
                ("D3 constraint", 3, 2, "=A3+B3"),
            ],
            chat_prompt=(
                "calc_solver objective_cell=goal_seek_solver.C3 "
                "variables=[goal_seek_solver.A3,goal_seek_solver.B3] maximize=true "
                'engine=com.sun.star.sheet.SolverLinear constraints='
                '[{"left":"goal_seek_solver.D3","operator":"LESS_EQUAL","right":"10.0"},'
                '{"left":"goal_seek_solver.A3","operator":"GREATER_EQUAL","right":"0.0"},'
                '{"left":"goal_seek_solver.B3","operator":"GREATER_EQUAL","right":"0.0"}]'
            ),
            expected="objective=50, y=10, x=0",
            notes="Requires Calc Solver engine; skip if unavailable",
        ),
    ]
