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
  - **`Operator`**: a **`FilterOperator2` long (see [FilterOperator2](https://api.libreoffice.org/docs/idl/ref/namespacecom_1_1sun_1_1star_1_1sheet_1_1FilterOperator2.html)).
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

### AND and OR (`connection`)

**Does UNO support AND and OR?** Yes. Each `TableFilterField2` after the first carries a **`Connection`** (`FilterConnection.AND` or `FilterConnection.OR`) that specifies how **this** row combines with the **immediately previous** row. That is the full Standard Filter / AutoFilter boolean model exposed to UNO—there is no separate “expression tree” API for sheet filters.

**How WriterAgent maps JSON:** On the **first** criterion, any `connection` value is **ignored**; the implementation always sets AND (LibreOffice still expects a connection on every struct). From the **second** criterion onward, omitting `connection` defaults to **AND**. Values are case-insensitive (`"and"` / `"or"` are fine).

**How conditions combine:** Criteria form a **single linear chain**, combined **left to right** (same as **Data → Standard Filter** in Calc). So three rows with connections `c2`, `c3` mean:

`((condition1) c2 condition2) c3 condition3`

Examples:

- **Two tests, both required (AND):** column 0 equals `X` **and** column 1 greater than 100 — second object has no `connection` or `"connection": "AND"`:
  `[{"field": 0, "operator": "EQUAL", "value": "X"}, {"field": 1, "operator": "GREATER", "value": "100", "is_numeric": true}]`
- **Either condition (OR):** column **B** (`field` 1) contains `east` **or** column **D** (`field` 3) contains `west` — second object uses `"connection": "OR"`:
  `[{"field": 1, "operator": "CONTAINS", "value": "east"}, {"field": 3, "operator": "CONTAINS", "value": "west", "connection": "OR"}]`
- **Mixed:** `A AND B OR C` is `((A AND B) OR C)` — list `A`, then `B` with default or explicit AND, then `C` with `"connection": "OR"`.

**Expressivity:** The filter is **not** a general boolean formula with arbitrary parentheses (e.g. `(A OR B) AND (C OR D)` is not necessarily the same as any single left-associative chain of four OR/AND links). If the needed logic does not match a linear chain, use **helper columns**, **multiple filter steps**, or **different tools**—not something missing from the `connection` field itself.

### Warning — `get_sheet_filter` / `getFilterFields2` and `Connection` (future work)

In practice (including in-LO UNO tests), **`apply_sheet_filter` with `"connection": "OR"` can behave correctly** (rows match OR semantics), but **`get_sheet_filter` may still report `connection`: `"AND"`** on later criteria when reading the active descriptor via `createFilterDescriptor(false)` / `getFilterFields2()`. Do not rely on `connection` round-trip alone to prove OR; validate behavior (e.g. which rows stay visible, or the Standard Filter dialog in Calc) or extend tooling to surface ground truth.

**Future work:** Investigate whether this is a LibreOffice bug or an alternate internal encoding of OR in `ScQueryParam` vs what `convertQueryEntryToUno` emits in `sc/source/ui/unoobj/datauno.cxx` (`getFilterFields2`), file upstream if confirmed; until then, treat **`get_sheet_filter` as best-effort for operators/fields/values**, not as a faithful boolean connector audit for every build.

---

Further single-condition examples:

- Text contains `east` in column **B** of range `A1:D10` (header row):  
  `criteria`: `[{"field": 1, "operator": "CONTAINS", "value": "east"}]`, `contains_header`: `true`.
- Numeric greater than 100 in column **C**:  
  `{"field": 2, "operator": "GREATER", "value": "100", "is_numeric": true}`.

---

## 4. Related files

Future scope, pass-through ideas, and out-of-scope workflows are in **§5** below.

| File | Purpose |
|------|---------|
| [`plugin/framework/calc_filter_constants.py`](../plugin/framework/calc_filter_constants.py) | `FilterOperator2` name/code mapping (no UNO import). |
| [`plugin/framework/calc_sheet_filter_criteria.py`](../plugin/framework/calc_sheet_filter_criteria.py) | Pure JSON → `TableFilterField2` field tuple parsing (unit-tested; no Calc package import). |
| [`plugin/modules/calc/sheet_filter.py`](../plugin/modules/calc/sheet_filter.py) | Tools + UNO helpers. |
| [`plugin/modules/calc/bridge.py`](../plugin/modules/calc/bridge.py) | Range resolution. |

---

## 5. Future development and roadmap

Audience: maintainers of WriterAgent’s Calc **standard filter** tools (`apply_sheet_filter`, `get_sheet_filter`, `clear_sheet_filter`). This is **not** a product roadmap for end users; it records **intentional scope**, **UNO facts**, and **candidate extensions** with risk notes.

### 5.1 Design constraint (current)

**We mirror UNO’s Standard Filter model only.** The implementation maps JSON to:

- `com.sun.star.sheet.XSheetFilterable` + `createFilterDescriptor` / `filter`
- `com.sun.star.sheet.XSheetFilterDescriptor2` (`setFilterFields2` / `getFilterFields2`)
- `com.sun.star.sheet.TableFilterField2` (field index, `FilterOperator2`, `FilterConnection`, numeric/string payload)
- Descriptor property `ContainsHeader` via `XPropertySet` on the filter descriptor

We **do not** add a second semantic layer (e.g. arbitrary boolean AST, SQL-like `WHERE`, or automatic helper-column synthesis) inside the extension. That keeps behavior aligned with **Data → Standard Filter** in Calc and reduces divergence bugs between what the UI would do and what the tool does.

For **linear AND/OR** semantics and expressivity limits, see **§3** above. Anything that cannot be expressed as a linear chain is out of scope unless we add **non-UNO** workflows (see **§5.4**).

### 5.2 UNO surface already covered

Implementation split: pure criteria parsing in [`plugin/framework/calc_sheet_filter_criteria.py`](../plugin/framework/calc_sheet_filter_criteria.py); UNO wiring in [`plugin/modules/calc/sheet_filter.py`](../plugin/modules/calc/sheet_filter.py).

| Area | Status |
|------|--------|
| `TableFilterField2.Field` | Mapped as JSON `field` (0-based within range). |
| `FilterOperator2` | Mapped by name via [`plugin/framework/calc_filter_constants.py`](../plugin/framework/calc_filter_constants.py); unknown names fall back to `com.sun.star.sheet.FilterOperator2` enum if present. |
| `FilterConnection` (AND/OR) | Mapped as JSON `connection` from the **second** criterion onward; first row always AND (ignored in JSON, matches LO convention). |
| `IsNumeric` / `NumericValue` / `StringValue` | Via `is_numeric` + `value` heuristics and operator classes (e.g. TOP_*, EMPTY). |
| `ContainsHeader` | `contains_header` on apply/clear. |
| Round-trip for debugging | `get_sheet_filter` reads `getFilterFields2` and maps back to JSON-shaped dicts. |

### 5.3 UNO descriptor features not exposed (pass-through candidates)

The filter descriptor implements additional properties (exact set varies slightly by LO version). These are **candidates for future pass-through** parameters on `apply_sheet_filter` / `get_sheet_filter` because they do not change the boolean model—only string matching and output behavior.

**Typical examples** (verify against your target LO IDL / `SheetFilterDescriptor` service):

- **`UseRegularExpressions`** — treat `*` / `?` (and regex if LO interprets as regex) in string comparisons.
- **`IsCaseSensitive`** — case-sensitive string matches.
- **`CopyOutputData`**, **`OutputPosition`**, **`SaveOutputPosition`** — copy filtered rows to another range instead of hiding rows (different UX; test carefully with `filter()` semantics).

**Implementation sketch when needed:**

1. After `createFilterDescriptor`, keep existing `XPropertySet` query on `fd`.
2. Add optional tool kwargs with defaults matching current behavior (e.g. regex off if unset).
3. Extend `get_sheet_filter` to return these when readable.
4. Add UNO tests that set each flag and assert via `getPropertyValue` or visible behavior.

**Risk:** Some properties interact with operators (e.g. regex + `CONTAINS`). Prefer **one property at a time** behind kwargs with integration tests.

### 5.4 Higher-level features (explicitly not UNO — higher risk)

These would be **new product behavior**, not UNO mapping. Treat as separate proposals; each needs its own design and test plan.

#### Helper-column or “effective filter” workflows

**Problem:** Linear AND/OR chains cannot express every boolean formula (e.g. `(A ∨ B) ∧ (C ∨ D)` may not equal any single chain of four links).

**Possible approach:** A **separate** tool (or orchestration in the agent) that writes a temporary column with `=IF(…)` or similar, filters on that column, then optionally deletes the column. **Not** a small extension to `criteria[]`—it’s a different feature with formula locale, sheet churn, and undo expectations.

#### Multi-pass filtering

Apply filter, user/agent edits, apply another filter. Scripting that reliably is **stateful** (active filter on range, sheet cursor). Prefer documenting patterns for the LLM over adding opaque “pipelines” in-process unless there is a clear UX need.

#### Query / advanced filter from named ranges / database range

LibreOffice also supports **Advanced Filter** / database-oriented APIs (`com.sun.star.sheet.DatabaseRange`, query descriptors). That is a **different UNO surface** than `XSheetFilterable` on a cell range. If we ever need it, add **new tools** rather than overloading `apply_sheet_filter`.

### 5.5 Engineering checklist for any change

1. **IDL first:** Confirm property names and types on the LO version you ship against (`SheetFilterDescriptor`, `TableFilterField2`).
2. **JSON schema:** Extend `parameters` on the tool class so models get accurate enums/descriptions.
3. **Docs:** Update the contract sections in this document (**§§1–4**).
4. **Tests:** Unit tests for pure parsing (`_parse_criterion`, operator resolution); UNO tests for apply/get/clear and any new descriptor field.
5. **Failure modes:** Missing `XSheetFilterDescriptor2` on old builds is already a hard error—keep messages actionable.

---

## 6. References

- [SheetFilterDescriptor](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1sheet_1_1SheetFilterDescriptor.html)
- [TableFilterField2](https://api.libreoffice.org/docs/idl/ref/structcom_1_1sun_1_1star_1_1sheet_1_1TableFilterField2.html)
- [XSheetFilterable](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XSheetFilterable.html)
- DevGuide: [Spreadsheet Documents — Filtering](https://wiki.documentfoundation.org/Documentation/DevGuide/Spreadsheet_Documents#Filtering)
