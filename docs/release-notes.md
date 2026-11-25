# Release Notes

## Unreleased (post-0.9.6)

### Breaking Changes

- **Removed `=PY(code; ROW()-1)` result-index fill-down form:** In 0.9.6, passing `ROW()-1` (or `ROW()-n`) as a trailing argument to `=PY()` selected a per-row element from a list result. Every argument after `code` is now an input (`data` / `ranges`), so legacy `=PY(code; ROW()-1)` formulas either trigger auto-spill (`#SPILL!`) or return only the first value.
  - **Use auto-spill (recommended):** Enter `=PY("result = ...")` in a single cell and let `scripting.python_auto_spill` populate neighboring cells.
  - **Use a matrix array block (LibreOffice 26.8+):** Select the output range, enter `=PY("result = ...")` without `ROW()-1`, and confirm with **Ctrl+Shift+Enter** (`{=PY(...)}`).
  - **Index explicitly in Python if needed:** Reference the trailing argument in code (e.g. `=PY("result = f(data[0])[int(data[1])]"; A1:A10; ROW()-1)`).
  - **Row-wise sheet transforms:** Use ordinary Calc formulas with fill-down (e.g. `=B2*0.1` across `C2:C10`).
