# Calc standard filter (AutoFilter-style) — developer guide

This document describes **sheet data filtering** in LibreOffice Calc: hiding rows that do not match criteria (the same feature family as **Data → AutoFilter** and **Standard Filter** in the UI). It is **not** related to **conditional formatting** (cell styles based on values). For conditional formatting, see [calc-conditional-formatting.md](calc-conditional-formatting.md).

---

## 1. User-facing behavior (extension tools)

Sheet filter tools live in the **specialized** tier with domain **`sheet_filter`**. The main chat agent does not see them in the default tool list; use **`delegate_to_specialized_calc_toolset`** with `domain: "sheet_filter"`. See [calc-specialized-toolsets.md](calc-specialized-toolsets.md).

| Tool | Role |
|------|------|
| `apply_sheet_filter` | Apply one or more conditions (`TableFilterField2` + `FilterOperator2`) to a **data range**. |
| `clear_sheet_filter` | Remove the filter on that range (show all rows again). |
| `get_sheet_filter` | Read back active criteria (round-trip / debugging). |

Implementation: [`plugin/modules/calc/sheet_filter.py`](../plugin/modules/calc/sheet_filter.py). Base class: `ToolCalcSheetFilterBase` in [`plugin/modules/calc/base.py`](../plugin/modules/calc/base.py). Operator name ↔ code helpers: [`plugin/framework/calc_filter_constants.py`](../plugin/framework/calc_filter_constants.py).

---

## 2. LibreOffice UNO model (summary)

- **`XSheetFilterable`** on a **`SheetCellRange`**: call **`createFilterDescriptor`**, configure the descriptor, then **`filter(descriptor)`**.
- **`XSheetFilterDescriptor2`**: **`setFilterFields2` / `getFilterFields2`** with **`com.sun.star.sheet.TableFilterField2`** structs.
- Each **`TableFilterField2`** has:
  - **`Field`**: 0-based **column index within the filtered range** (left column = 0).
  - **`Operator`**: a **`FilterOperator2`** long (see [FilterOperator2](https://api.libreoffice.org/docs/idl/ref/namespacecom_1_1sun_1_1star_1_1sheet_1_1FilterOperator2.html)).
  - **`Connection`**: **`FilterConnection.AND`** or **`OR`** vs the previous condition (the first condition still uses a connection value; use **AND**).
  - **`IsNumeric`**, **`NumericValue`**, **`StringValue`**: value payload as appropriate for the operator.
- Descriptor **`ContainsHeader`**: when `true`, the **first row** of `range_name` is treated as headers (not filtered as a data row).

Wildcards (`*`, `?`) in string conditions follow Calc’s usual rules when **`UseRegularExpressions`** / case options are set on the descriptor; the WriterAgent tools currently expose **`contains_header`** only—extend the implementation if you need those properties explicitly.

---

## 3. Criteria JSON shape (`apply_sheet_filter`)

Each element of **`criteria`** is an object:

| Property | Required | Description |
|----------|----------|-------------|
| `field` | yes | 0-based column index within `range_name`. |
| `operator` | yes | `FilterOperator2` name (e.g. `EQUAL`, `CONTAINS`, `BEGINS_WITH`, `GREATER`, `EMPTY`, `TOP_VALUES`). |
| `value` | usually | Filter value as string. Omit for `EMPTY` / `NOT_EMPTY`. For `TOP_VALUES` / `TOP_PERCENT` / `BOTTOM_*`, pass a numeric string. |
| `is_numeric` | no | If `true`, `value` is interpreted as a number (`NumericValue`). |
| `connection` | no | `AND` or `OR` vs the **previous** row (meaningful from the second criterion onward). |

Examples:

- Text contains `east` in column **B** of range `A1:D10` (header row):  
  `criteria`: `[{"field": 1, "operator": "CONTAINS", "value": "east"}]`, `contains_header`: `true`.
- Numeric greater than 100 in column **C**:  
  `{"field": 2, "operator": "GREATER", "value": "100", "is_numeric": true}`.

---

## 4. Related files

| File | Purpose |
|------|---------|
| [`plugin/framework/calc_filter_constants.py`](../plugin/framework/calc_filter_constants.py) | `FilterOperator2` name/code mapping (no UNO import). |
| [`plugin/modules/calc/sheet_filter.py`](../plugin/modules/calc/sheet_filter.py) | Tools + UNO helpers. |
| [`plugin/modules/calc/bridge.py`](../plugin/modules/calc/bridge.py) | Range resolution. |

---

## 5. References

- [SheetFilterDescriptor](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1sheet_1_1SheetFilterDescriptor.html)
- [TableFilterField2](https://api.libreoffice.org/docs/idl/ref/structcom_1_1sun_1_1star_1_1sheet_1_1TableFilterField2.html)
- [XSheetFilterable](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XSheetFilterable.html)
- DevGuide: [Spreadsheet Documents — Filtering](https://wiki.documentfoundation.org/Documentation/DevGuide/Spreadsheet_Documents#Filtering)
