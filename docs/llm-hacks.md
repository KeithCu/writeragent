# LLM Hacks and Workarounds

This document tracks technical workarounds and "hacks" implemented to handle the quirks, inconsistencies, and limitations of LLMs when interacting with LibreOffice tools.

## 1. CSV Delimiter Handling
**Problem**: LLMs are inconsistent with CSV delimiters. Since Calc formula syntax favors semicolons (`;`), models often use them for CSV data even when asked for commas (`,`). This leads to data being imported into a single column.

### [Workaround] Auto-Detection
Instead of requiring a `delimiter` parameter, the tool `import_csv_from_string` now handles it automatically in `core/calc_manipulator.py`.
- **Implementation**: The tool peeks at the first few lines. If semicolons are significantly more prevalent than commas, it switches the CSV reader to `;`. Otherwise, it defaults to `,`.
- **Reasoning**: Reduces the cognitive load on the LLM and makes the tool "just work" regardless of the model's preferred separator.

## 2. Range Writing Semicolon Splitting
**Problem**: When using `write_formula_range`, the LLM often sends a raw string like `"Name;Age;Salary"` instead of a JSON-encoded array `["Name", "Age", "Salary"]`. Without a workaround, this writes the entire string into a single cell.

### [Workaround] Raw String Splitting
The internal `_parse_formula_or_values_string` helper in `core/calc_manipulator.py` detects raw semicolon-separated strings.
- **Formula-Safe Detection**: It skips strings starting with `=` (standard formulas) but splits other strings containing `;` using `csv.reader`.
- **CSV Reader**: Using `csv.reader` (with `skipinitialspace=True`) ensures that if the LLM puts a semicolon inside a quoted string, it won't be split incorrectly.

## 3. Formula-Safe JSON Normalization
**Problem**: LLMs often use semicolons inside JSON arrays (e.g., `["A"; "B"]`). Standard `json.loads` fails here. A blind `replace(";", ",")` fix breaks real formulas that *need* semicolons (e.g., `=SUM(A1;B1)` becomes `=SUM(A1,B1)` which is an error in Calc).

### [Workaround] Regex-Bound Replacement
In `core/calc_manipulator.py`, we use a regular expression to normalize JSON arrays before parsing.
- **Regex**: `re.sub(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', ',', s_strip)`
- **Behavior**: This only replaces semicolons that are **not** inside double quotes. This preserves semicolons inside formula strings while fixing the JSON structure.

## 4. Prompt Steering for Syntax
**gotcha**: LibreOffice is very strict about formula syntax. Using a comma instead of a semicolon as an argument separator results in "Error 508".

### [Workaround] Explicit Prompt Rules
In `core/constants.py`, the system prompt includes high-pressure instructions on formula syntax.
- **Constraint**: "FORMULA SYNTAX: LibreOffice uses semicolon (;) as the formula argument separator. Wrong: =SUM(A1,A10) (no commas)."
- **Duality**: We also explicitly warn about CSV: "CSV DATA: Use comma (,) for import_csv_from_string." to counteract the formula semicolon rule.

## 5. Defensive Parameter Handling
**Problem**: Models sometimes pass range names as lists or wrap them in extra quotes.

### [Workaround] Multi-Type Dispatcher
In `core/calc_tools.py`, the `execute_calc_tool` dispatcher often checks if `range_name` is a list or a single string.
- **Looping**: If the model passes a list of ranges (hallucination or efficiency attempt), the code loops over them automatically rather than crashing.

---

*This document should be updated as new hacks are discovered or as improvements in models allow us to remove them.*
