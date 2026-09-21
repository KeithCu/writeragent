# Writer table cells — locked design

**Status:** Locked 2026-09-21 (Keith + Chief). Implements [discussion #821](https://github.com/KeithCu/writeragent/discussions/821).

**Code:** [`plugin/writer/specialized/tables.py`](../../plugin/writer/specialized/tables.py) (`TableList`, `TableGetCells`, `_hosted_in_band`). Draw/Impress uses the same tool names via [`plugin/draw/tables.py`](../../plugin/draw/tables.py). HTML copy: [`plugin/writer/html_export.py`](../../plugin/writer/html_export.py) `_copy_table`.

This is a small read fix plus the same first-row bound on delete and HTML copy. No new tool, no rebuilt `matrix`, no visual spans / merge-split UX / `include_content`.

---

## The failure

A Writer table whose first row is one merged cell still has real cells in later rows. `search_in_document` reports `Taula136` / `D2`. `table_get_cells` used to report `rows: 5`, `cols: 1`, and a 5×1 `matrix`. `D2` was not in that result. `table_set_cell` could write `D2` anyway, because it checks `getCellNames()` before it writes. The read path never asked for those names.

`table_get_cells` built `matrix` with `range(rows) × range(cols)` from `getRows()` / `getColumns()`. That was the bug.

---

## What LibreOffice returns

Citations are `sw/source/core/unocore/unotbl.cxx` on LibreOffice master.

| Call | What it actually is |
|------|---------------------|
| `getColumns().getCount()` | `SwXTableColumns::getCount`. Box count of the **first** row (`GetTabLines().front()->GetTabBoxes().size()`). The "table too complex" guard above that return is commented out, so a merged banner still reports `1`. |
| `getRows().getCount()` | `SwXTableRows::getCount`. Number of top-level lines. The 5-row fixture stays `5`. |
| `getCellNames()` | `lcl_InspectLines`. Every box with a name and `rowSpan > 0`, including lines nested inside a split cell. Covered cells are absent. Merging `A1:D1` on a 5×4 table drops `B1`/`C1`/`D1` and leaves `D2`: 20 − 3 = 17. |
| `getCellByPosition(col, row)` | Not "the col-th box in that row". `lcl_CreateXCell` builds a name with `sw_GetCellName` and looks that box up. |
| `GetCellPosition` | Writer names are base 52: `A–Z`, then `a–z`, then `AA…`. Row is `o3tl::toInt32` from the first digit (stops at the first non-digit), so split-cell `A1.1.1` is the same band as `A1`. The comment on that function says the coordinate math is for tables where `IsTableComplex()` is false. |

`plugin/draw/tables.py` `parse_a1` and `_col_letters` in the Writer module are spreadsheet letters (and `parse_a1` uppercases). They match Writer through column `Z` only. Past `Z`, Writer `a1` and spreadsheet `AA` are different cells. Do **not** parse Writer names with `parse_a1`. Do **not** use the Writer parser to rebuild a rectangle for the read path.

An existing empty cell is a name in `getCellNames()` whose text is `""`. A covered or invented coordinate is a name that is not in that list.

Host cells that contain a nested table keep using `_cell_matrix_text` (the cell's own paragraphs). `getString()` concatenates the inner table. `nesting` and `nested_in_cells` stay as they are.

---

## Locked choices (ship in one PR)

1. **1a** — No new tool. Extend `table_get_cells`.
2. **2c** — Writer payload: always `cells` + `cell_names` from `getCellNames()` + `getCellByName` + `_cell_matrix_text`. **Stop returning `matrix` on Writer.** Keep `rows`, `cols`, `nesting`, `nested_in_cells`. Empty string = empty cell; missing key = not a cell.
3. **3b** — Optional `cell=` on `table_get_cells` (one address). Reuse `_resolve_cell_name`. Shared helper for the "Its cells are: …" sample used by set/insert/get. Draw honors `cell=` via `parse_a1` (shared schema).
4. **4a** — `table_list`: add `cell_count` (Writer: `len(getCellNames())` via the nesting pass; Draw: `rows*cols`). Description: if `cell_count != rows*cols`, not a rectangle — use get_cells/`cells`.
5. **Same PR** — Fix `_hosted_in_band` to scan `getCellNames()` (not `range(cols)`). Writer base-52 name parser matching `SwXTextTable::GetCellPosition` (`A`, `Z`, `a`, `AA`; row via `toInt32` so `A1.1.1` is the `A1` band) lives in LibrePy-shipped [`html_export.py`](../../plugin/writer/html_export.py). Do **not** use spreadsheet `parse_a1` for Writer names; do **not** use the parser to rebuild `matrix`. Fix `_copy_table` the same way (merged tables must not drop D2).
6. **Reject** — Do not rebuild a wide fake `matrix`. Covered cells would become `""` and look like empty cells.

Draw/Impress path: keep rectangular `matrix` / `rows` / `cols` (plus `cell=` if shared).

Descriptions: first sentence names **`cells` for Writer**, **`matrix` for Draw**. Same on `_TableProxy.get_cells` in `writeragent_api.py` (regenerated).

---

## Hypothesis (verified in comments and native tests)

`getColumns()` is `SwXTableColumns::getCount`: the first row's box count. After merge `A1:D1` on a 5×4 table, `cols == 1` while `rows == 5` and `len(getCellNames()) == 17`. That is why `range(cols)` skipped `D2`.
