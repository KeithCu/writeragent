# Release Notes

## Unreleased (post-0.9.6)

### Breaking Changes

- **Removed `=PY(code; ROW()-1)` result-index form:** In 0.9.6, passing `ROW()-1` (or `ROW()-n`) as the only argument after `code` selected a per-row element from a list result. Every argument after `code` is now passed to Python as an input (`data` / `ranges`); two-argument forms such as `=PY(code; A1:A10; ROW()-1)` were already two inputs in 0.9.6 and are unchanged.
  - **Ordinary filled-down cells (`=PY(code; ROW()-1)`):** Triggers auto-spill (`#SPILL!`) or returns only the first value, with no error marker.
  - **`{=PY(code; ROW()-1)}` array block before LibreOffice 26.8:** Shows the first value in every cell of the block, with no error marker.
  - **`{=PY(code; ROW()-1)}` array block on LibreOffice 26.8+:** Still fills the block via the native matrix return, and `ROW()-1` is now just passed as `data`.
  - **Use auto-spill (recommended):** Enter `=PY("result = ...")` in a single cell and let `scripting.python_auto_spill` populate neighboring cells.
  - **Use a matrix array block (LibreOffice 26.8+):** Select the output range, enter `=PY("result = ...")` without `ROW()-1`, and confirm with **Ctrl+Shift+Enter** (`{=PY(...)}`).
