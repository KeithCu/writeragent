# =PYTHON() serialization test sheet (manual)

Open **`tests/fixtures/serialization_tests.xlsx`** in LibreOffice Calc (**File → Open**).
Calc may show an import dialog on first open — accept defaults.

## Setup

1. **Settings → Python** — set `scripting.python_venv_path` to a venv with NumPy.
2. Deploy / restart WriterAgent so `=PYTHON()` is registered.

## Layout (each test block)

| Column | Header name | Content |
|--------|-------------|---------|
| A | `test_id` | Case id (data rows use `row_N`) |
| B | `description` | What this test checks |
| C | `tags` | e.g. `split_grid`, `multi_range`, `bool` |
| D–H | `col_1` … `col_5` | **Range 1** (single-range cases use this group only) |
| I–M | `col_6` … `col_10` | **Range 2** (multi-range varargs only) |
| N | `calc_oracle` | Native Calc reference (`=SUM`, `=MAX`, `=INDEX`, …) |
| O | `compare_pass_fail` | `=IF(…;"PASS";"FAIL")` |
| … | `expected` | Expected value (reference) |
| … | `notes` | Extra hints |
| R | `python_formula` | `=PYTHON("…", range)` or `=PYTHON("…", r1, r2)` |

Each group holds up to **5 columns × 5 rows**. Multi-range cases place range 0 in group 1 and range 1 in group 2; Python receives ``data[0]``, ``data[1]``, …

Green band rows label sections: **normal** → **multi** → **mixed** → **grid** → **nan** → **errors**.

Formulas use **comma** argument separators (Excel/XLSX). LibreOffice should convert them to semicolons if your locale requires it on import. If you still see **Err:508**, check **Tools → Options → LibreOffice Calc → Formula → Separators** and edit one formula in the bar (comma ↔ semicolon) to match.

## Quick start

1. Open `tests/fixtures/serialization_tests.xlsx`.
2. Press **Ctrl+Shift+F9** (recalculate).
3. **compare_pass_fail** should show `PASS`.

Input grid (`col_1` …):
- Numeric cells are written as **real numbers** in the XLSX (not text `"1.0"`). If Calc shows them as text after import, `np.sum(data)` will fail with a string dtype error — re-run the generator or format cells as numbers.
- Bools use **`1`** / **`0`** as numbers (not `=TRUE()` formulas).
- Text `"True"` in a cell would stay a string through the wire (pickle-faithful) and break `np.sum`.

## Oracles

- **SUM** — primary; touches every numeric/logical cell. Multi-range: ``=SUM(r1,r2)``.
- **MAX** — one spot-check on 4×4 (answer 16).
- **INDEX** — first cell (text/unicode) or per-cell grid egress.

## Grid returns (grid section)

1. Select a 4×4 output area aligned with the input block.
2. Paste the formula from **python_formula** (column R) as a **matrix formula** (`Ctrl+Shift+Enter`).
3. Each cell should match **calc_oracle** (column N).

## Error cases (errors section)

**compare_pass_fail** should show `PASS` when **python_formula** displays `Error: …`.

## Inline code and ``float()``

Do **not** put ``float(...)`` inside the formula string (e.g. avoid ``=PYTHON("float(np.sum(data))",…)``).
Calc's formula lexer can treat ``float`` as a spreadsheet function and show **#NAME?** before Python runs.
Use ``np.sum(data)`` / ``np.max(data)`` (return values are coerced on the bridge). For longer code, put the script in a cell and use ``=PYTHON(A1, D6:G6)``.

## Regenerate

```bash
python scripts/generate_serialization_spreadsheet.py
```

Cases live in `tests/calc/serialization_cases.py`.
