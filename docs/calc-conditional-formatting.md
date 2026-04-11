# Calc conditional formatting — developer guide

This document is for maintainers who need to understand **how WriterAgent exposes LibreOffice Calc conditional formatting**, how that maps to **UNO**, what was implemented in the codebase, and **what work remains** for parity with rich spreadsheet UIs (including OnlyOffice-style “umbrella” features).

---

## 1. User-facing behavior (extension tools)

Conditional formatting tools live in the **specialized** tier (`conditional_formatting` domain). The main chat agent does **not** see them in the default tool list; callers use **`delegate_to_specialized_calc_toolset`** with `domain: "conditional_formatting"`. See [`docs/calc-specialized-toolsets.md`](calc-specialized-toolsets.md) and [`AGENTS.md`](../AGENTS.md) (Calc section).

| Tool | Role |
|------|------|
| `add_conditional_format` | Append a **classic** rule: operator + optional formulas + **cell style name** when the rule matches. |
| `list_conditional_formats` | List rules on a range (or the used area if `range_name` is omitted). |
| `remove_conditional_formats` | Remove one rule by **0-based** index, or **clear all** rules on the range if `rule_index` is omitted. |

Implementation: [`plugin/modules/calc/conditional.py`](../plugin/modules/calc/conditional.py). Base class: `ToolCalcConditionalBase` in [`plugin/modules/calc/base.py`](../plugin/modules/calc/base.py).

---

## 2. LibreOffice UNO — two different models (critical)

LibreOffice exposes **two** ways to work with conditional formatting. WriterAgent’s tools intentionally use the **first** path today; the second path is the correct place for **data bars, color scales, icon sets**, and related “modern” UI features.

### 2.1 Legacy / “table” conditional format (what we implement)

- **Property:** `ConditionalFormat` on a **`SheetCellRange`** (or a cursor spanning a used area).
- **UNO type:** [`TableConditionalFormat`](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1sheet_1_1TableConditionalFormat.html) implementing [`XSheetConditionalEntries`](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XSheetConditionalEntries.html).
- **Add a rule:** [`addNew(sequence<PropertyValue>)`](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XSheetConditionalEntries.html#ae6003e13d9f4a9723070b73f4b62c818). Supported properties include **`Operator`**, **`Formula1`**, **`Formula2`**, **`SourcePosition`**, **`StyleName`** (see IDL docs for the exact set).
- **Semantics:** The **first** matching rule in index order wins (documented on `TableConditionalFormat`).

This matches the classic “apply **named cell style** when condition X holds” workflow. It is **not** the same object model as Excel’s Data Bar / Icon Set dialogs, but it is stable, well-documented, and sufficient for value comparisons, formulas, and (on LibreOffice) duplicate detection via extended operator codes.

### 2.2 Sheet-level conditional formats container (“modern” API)

- **Property:** `ConditionalFormats` on the **sheet**, implementing [`XConditionalFormats`](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XConditionalFormats.html).
- **Create:** `createByRange(XSheetCellRanges)` returns an **ID**; **`removeByID`** removes a block.
- **Each block:** [`ConditionalFormat`](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1sheet_1_1ConditionalFormat.html) service with **`XConditionalFormat.createEntry(Type, Position)`** where **`Type`** is [`ConditionEntryType`](https://api.libreoffice.org/docs/idl/ref/namespacecom_1_1sun_1_1star_1_1sheet_1_1ConditionEntryType.html): **CONDITION**, **COLORSCALE**, **DATABAR**, **ICONSET**, **DATE**, etc.
- **Polymorphic entries:** [`XConditionEntry`](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XConditionEntry.html) — concrete services include [`DataBar`](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1sheet_1_1DataBar.html), **ColorScale**, **IconSet**, etc.

**Implication:** Rules created **only** through this newer pipeline may **not** appear in `getPropertyValue("ConditionalFormat")` on a cell range, and **vice versa**. Our **`list_conditional_formats`** tool currently reads **only** the legacy range property. A future “full fidelity” listing would need to **merge** legacy range rules with sheet-level **`ConditionalFormats`** filtered by range overlap.

---

## 3. Operators: `ConditionOperator` vs `ConditionOperator2`

The historical [`ConditionOperator`](https://www.openoffice.org/api/docs/common/ref/com/sun/star/sheet/ConditionOperator.html) enum (Apache OpenOffice baseline) ends at **`FORMULA`**.

LibreOffice adds **[`ConditionOperator2`](https://api.libreoffice.org/docs/idl/ref/namespacecom_1_1sun_1_1star_1_1sheet_1_1ConditionOperator2.html)** with at least:

| Code | Name |
|------|------|
| 10 | `DUPLICATE` |
| 11 | `NOT_DUPLICATE` |

These are still passed as the **`Operator`** property in `addNew` — the value is a **numeric** operator code (PyUNO may surface it as `int`-like constants from `ConditionOperator2`).

### Reading entries back

[`XSheetCondition`](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XSheetCondition.html) `getOperator()` predates extended codes. For reliable round-tripping of **10** and **11**, use [`XSheetCondition2`](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1sheet_1_1XSheetCondition2.html) **`getConditionOperator()`** / **`setConditionOperator(long)`**.

WriterAgent follows the same pattern as [`plugin/modules/calc/pivot.py`](../plugin/modules/calc/pivot.py): **`queryInterface(uno.getTypeByName("com.sun.star.sheet.XSheetCondition2"))`** — imported IDL classes are **not** passed directly to `queryInterface` in PyUNO.

Listing output includes:

- **`operator`**: stable string (e.g. `DUPLICATE`) via [`condition_operator_code_to_name()`](../plugin/framework/calc_conditional_constants.py).
- **`operator_code`**: present when `XSheetCondition2` succeeds (integer, e.g. `10`).

---

## 4. What we implemented in code (this iteration)

File: [`plugin/modules/calc/conditional.py`](../plugin/modules/calc/conditional.py).

1. **`DUPLICATE` / `NOT_DUPLICATE`** on `add_conditional_format`  
   - Operator values use **`ConditionOperator2`** when importable, with fallback to **10** / **11**.  
   - **`formula1`** is **not** required for these operators (empty string is fine).

2. **Validation**  
   - **`BETWEEN` / `NOT_BETWEEN`** require **`formula2`**.  
   - Other operators (except duplicates) require non-empty **`formula1`**.

3. **`list_conditional_formats`**  
   - Prefer **`XSheetCondition2.getConditionOperator()`** so extended codes map to **`DUPLICATE`** / **`NOT_DUPLICATE`** instead of opaque enum strings.  
   - Expose optional **`operator_code`** for debugging and agent reasoning.

4. **Missing `ConditionalFormat` container**  
   - If `getPropertyValue("ConditionalFormat")` returns **`None`**, we **`createInstanceWithContext("com.sun.star.sheet.TableConditionalFormat", ctx)`** via the document’s component context (see **`_ensure_table_conditional_format`**). This avoids a rare first-rule failure on some paths.

5. **`remove_conditional_formats`**  
   - Safer behavior when the container is missing or empty (clear is a no-op if there is nothing to clear).

6. **Tests**  
   - Unit: [`plugin/tests/test_calc_conditional.py`](../plugin/tests/test_calc_conditional.py) — imports [`condition_operator_code_to_name`](../plugin/framework/calc_conditional_constants.py) without loading the Calc package (avoids UNO side effects during `pytest`).  
   - UNO: [`plugin/tests/uno/test_calc.py`](../plugin/tests/uno/test_calc.py) — `test_calc_conditional_formatting` extended with **BETWEEN** and **DUPLICATE** cases.

---

## 5. What to do next (recommended roadmap)

### 5.1 Short term (same legacy API)

- Add UNO coverage for **`NOT_DUPLICATE`**, **`FORMULA`**, and **`NOT_BETWEEN`** if any regressions show up on specific LibreOffice versions.  
- Optional: expose **`SourcePosition`** in `add_conditional_format` for relative formulas (PropertyValue **`SourcePosition`** — see `XSheetConditionalEntries` docs).  
- Document in tool descriptions that **`list_conditional_formats`** is **legacy-container-only** until merged listing exists.

### 5.2 Medium term (full listing)

- Implement **merged** listing: legacy range **`ConditionalFormat`** **plus** sheet **`ConditionalFormats`** entries whose **`Range`** intersects the requested range.  
- Define a clear JSON shape (e.g. `source: "legacy_range" | "sheet_conditional_formats"`) so LLMs do not confuse the two.

### 5.3 Larger feature (OnlyOffice “umbrella” parity for presets)

- New tools (or a structured sub-API) using **`XConditionalFormats.createByRange`**, **`ConditionalFormat.createEntry`**, and configuration of **`DataBar`**, **`ColorScale`**, **`IconSet`** services.  
- Expect **large** JSON schemas (many optional visual parameters) and substantial UNO testing.  
- Cross-reference: [`onlyoffice_calc_impressplan.md`](../onlyoffice_calc_impressplan.md) section A.3 (preset helpers vs UNO).

---

## 6. Debugging tips

- **PyUNO `queryInterface`:** Use **`uno.getTypeByName("com.sun.star....")`** — see `_query_interface` in [`conditional.py`](../plugin/modules/calc/conditional.py) and [`pivot.py`](../plugin/modules/calc/pivot.py).  
- **First rule fails with no container:** Check **`_ensure_table_conditional_format`** and LO version; verify **`TableConditionalFormat`** service name.  
- **Operator shows wrong in UI but list looks right:** Compare **`operator_code`** from **`list_conditional_formats`** against [`calc_conditional_constants`](../plugin/framework/calc_conditional_constants.py).  
- **Rules from UI missing in list:** User may have created **modern** CF — see §2.2; implement merged listing (§5.2).

---

## 7. Related files

| File | Purpose |
|------|---------|
| [`plugin/framework/calc_conditional_constants.py`](../plugin/framework/calc_conditional_constants.py) | Pure operator code → name mapping (importable without loading `calc` package / UNO) |
| [`plugin/modules/calc/conditional.py`](../plugin/modules/calc/conditional.py) | Tools + UNO helpers |
| [`plugin/modules/calc/base.py`](../plugin/modules/calc/base.py) | `ToolCalcConditionalBase`, `specialized_domain` |
| [`plugin/modules/calc/bridge.py`](../plugin/modules/calc/bridge.py) | Range resolution |
| [`plugin/modules/calc/specialized.py`](../plugin/modules/calc/specialized.py) | `delegate_to_specialized_calc_toolset` |
| [`docs/calc-specialized-toolsets.md`](calc-specialized-toolsets.md) | Delegation overview |

---

*Last updated to match the DUPLICATE / BETWEEN / XSheetCondition2 listing work and the dual-UNO documentation above.*
