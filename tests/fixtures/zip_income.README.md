# `zip_income.csv` — ACS ZCTA median household income

Sibling reference file for the pretty Calc demo (`python_showcase_demo.ods` / `.xlsx`).
This is boring census data you would **not** keep in the company sales workbook.

| Column | Meaning |
|--------|---------|
| `zip` | ZIP Code Tabulation Area (5-digit ZCTA) |
| `median_household_income` | Median household income (USD). Blank when ACS suppresses the estimate. |
| `city` | Place name for ZCTAs used on the Sales sheet (optional elsewhere) |
| `state` | State/territory of the Census Reporter geography query |

**Source:** U.S. Census Bureau, American Community Survey **2024 5-year** estimates, detailed table **B19013** (Median Household Income in the Past 12 Months). That is the same household-median statistic published on subject table **S1903**. Downloaded via [Census Reporter](https://api.censusreporter.org/) (`acs2024_5yr`, all ZCTAs by state/territory). Public data; cite the Census Bureau.

**Regenerate the demo + copy this CSV beside it:**

```bash
python scripts/generate_pretty_demo_spreadsheet.py --format all
```

The generator copies this file next to the ODS/XLSX. The `SQL_DuckDB` sheet live-joins `{sheet: "Sales_Analytics"}` / named range `SalesData` to this extract through `=PY()` + `run_sql` (same catalog as `query_folder_sql`).
