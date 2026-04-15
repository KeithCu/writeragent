# Calc sheet filter — future development (roadmap)

Audience: senior engineers maintaining WriterAgent’s Calc **standard filter** tools (`apply_sheet_filter`, `get_sheet_filter`, `clear_sheet_filter`). This is **not** a product roadmap for end users; it records **intentional scope**, **UNO facts**, and **candidate extensions** with risk notes.

---

## 1. Design constraint (current)

**We mirror UNO’s Standard Filter model only.** The implementation maps JSON to:

- `com.sun.star.sheet.XSheetFilterable` + `createFilterDescriptor` / `filter`
- `com.sun.star.sheet.XSheetFilterDescriptor2` (`setFilterFields2` / `getFilterFields2`)
- `com.sun.star.sheet.TableFilterField2` (field index, `FilterOperator2`, `FilterConnection`, numeric/string payload)
- Descriptor property `ContainsHeader` via `XPropertySet` on the filter descriptor

We **do not** add a second semantic layer (e.g. arbitrary boolean AST, SQL-like `WHERE`, or automatic helper-column synthesis) inside the extension. That keeps behavior aligned with **Data → Standard Filter** in Calc and reduces divergence bugs between what the UI would do and what the tool does.

**Boolean logic:** UNO only supports a **linear chain** of conditions: each row’s `Connection` is AND or OR versus the **previous** row (left-associative). Anything that cannot be expressed that way is out of scope unless we add **non-UNO** workflows (see §4).

---

## 2. UNO surface already covered

Implementation split: pure criteria parsing in [`plugin/framework/calc_sheet_filter_criteria.py`](../plugin/framework/calc_sheet_filter_criteria.py); UNO wiring in [`plugin/modules/calc/sheet_filter.py`](../plugin/modules/calc/sheet_filter.py).

| Area | Status |
|------|--------|
| `TableFilterField2.Field` | Mapped as JSON `field` (0-based within range). |
| `FilterOperator2` | Mapped by name via [`plugin/framework/calc_filter_constants.py`](../plugin/framework/calc_filter_constants.py); unknown names fall back to `com.sun.star.sheet.FilterOperator2` enum if present. |
| `FilterConnection` (AND/OR) | Mapped as JSON `connection` from the **second** criterion onward; first row always AND (ignored in JSON, matches LO convention). |
| `IsNumeric` / `NumericValue` / `StringValue` | Via `is_numeric` + `value` heuristics and operator classes (e.g. TOP_*, EMPTY). |
| `ContainsHeader` | `contains_header` on apply/clear. |
| Round-trip for debugging | `get_sheet_filter` reads `getFilterFields2` and maps back to JSON-shaped dicts. |

---

## 3. UNO descriptor features **not** exposed (pass-through candidates)

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

---

## 4. Higher-level features (explicitly **not** UNO — higher risk)

These would be **new product behavior**, not UNO mapping. Treat as separate proposals; each needs its own design and test plan.

### 4.1 Helper-column or “effective filter” workflows

**Problem:** Linear AND/OR chains cannot express every boolean formula (e.g. `(A ∨ B) ∧ (C ∨ D)` may not equal any single chain of four links).

**Possible approach:** A **separate** tool (or orchestration in the agent) that writes a temporary column with `=IF(…)` or similar, filters on that column, then optionally deletes the column. **Not** a small extension to `criteria[]`—it’s a different feature with formula locale, sheet churn, and undo expectations.

### 4.2 Multi-pass filtering

Apply filter, user/agent edits, apply another filter. Scripting that reliably is **stateful** (active filter on range, sheet cursor). Prefer documenting patterns for the LLM over adding opaque “pipelines” in-process unless there is a clear UX need.

### 4.3 Query / advanced filter from named ranges / database range

LibreOffice also supports **Advanced Filter** / database-oriented APIs (`com.sun.star.sheet.DatabaseRange`, query descriptors). That is a **different UNO surface** than `XSheetFilterable` on a cell range. If we ever need it, add **new tools** rather than overloading `apply_sheet_filter`.

---

## 5. Engineering checklist for any change

1. **IDL first:** Confirm property names and types on the LO version you ship against (`SheetFilterDescriptor`, `TableFilterField2`).
2. **JSON schema:** Extend `parameters` on the tool class so models get accurate enums/descriptions.
3. **Docs:** Update [calc-sheet-filter.md](calc-sheet-filter.md) (user-facing / integrator contract).
4. **Tests:** Unit tests for pure parsing (`_parse_criterion`, operator resolution); UNO tests for apply/get/clear and any new descriptor field.
5. **Failure modes:** Missing `XSheetFilterDescriptor2` on old builds is already a hard error—keep messages actionable.

---

## 6. References

- [calc-sheet-filter.md](calc-sheet-filter.md) — current JSON contract and AND/OR semantics.
- [SheetFilterDescriptor](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1sheet_1_1SheetFilterDescriptor.html)
- [TableFilterField2](https://api.libreoffice.org/docs/idl/ref/structcom_1_1sun_1_1star_1_1sheet_1_1TableFilterField2.html)
- DevGuide: [Spreadsheet Documents — Filtering](https://wiki.documentfoundation.org/Documentation/DevGuide/Spreadsheet_Documents#Filtering)
