# NumPy domains demo workbook (manual)

Open **`tests/fixtures/numpy_domains_demo.ods`** in LibreOffice Calc.

Native ODS uses fully qualified `ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON()` formulas and semicolon argument separators (LibreOffice does not lowercase custom add-ins on ODS open the way it does for imported XLSX).

## Setup

1. **Settings → Python** — configure `scripting.python_venv_path`.
2. Install venv packages (see **readme** sheet or Settings → Python **Test**).
3. Deploy / restart WriterAgent.

## Sheets

| Sheet | Helpers |
|-------|---------|
| `analysis` | 14 trusted analysis helpers + `analyze_data` chat |
| `forecast` | `forecast_time_series`, `decompose_time_series` + `forecast_data` chat |
| `viz` | `quick_plot`, `correlation_heatmap`, `time_series_plot` + raw matplotlib block |
| `math` | SymPy: solve, simplify, integrate, differentiate |
| `quant` | yfinance / pandas-ta / quantstats / pyportfolioopt (RPS only) |
| `optimize` | scipy LP, portfolio, scheduling + `optimize_data` chat |
| `units` | pint converters (RPS only) |
| `goal_seek_solver` | Native `calc_goal_seek` / `calc_solver` via chat |

## Quick start

1. Open the workbook; read the **readme** tab.
2. On **analysis** (or optimize/math): **Ctrl+Shift+F9** — eyeball `python_formula` vs `expected_scalar`.
3. On **viz**: select data range → **Tools → Run Python Script… → Viz Helpers**.
4. On **math/units/quant/optimize/forecast**: use matching **Run Python Script** helper section.
5. Paste **chat_prompt** cells into WriterAgent chat where provided.

Replace `<DATA_RANGE>` in chat prompts with the actual `Sheet.col` range from each block header row.

## Regenerate

```bash
python scripts/generate_numpy_domains_demo_spreadsheet.py
```

Cases: `tests/calc/numpy_domains_demo_cases.py`.
