# Calc specialized toolsets (nested delegation)

This document describes how Calc implements **nested delegation** for specialized toolsets, similar to Writer's approach. For detailed background on the delegation model, API design philosophies (Fine-grained vs. Fat APIs), and architecture overview, see [Writer specialized toolsets](writer-specialized-toolsets.md), and [Calc specialized toolsets](calc-specialized-toolsets.md), and 

This document focuses on **Calc-specific** domains, implementation status, and feature coverage.

---

## 1. Calc-specific domains and implementation

LibreOffice Calc supports a large surface area through UNO: cells, ranges, formulas, sheets, charts, pivot tables, named ranges, data validation, conditional formatting, and more. WriterAgent implements **nested delegation** for Calc using the same architecture as Writer:

- **Tier filtering** in `ToolRegistry.get_tools` / `get_schemas`
- **Domain bases** (`ToolCalc*Base`) with `tier = "specialized"` and `specialized_domain`
- **Gateway tool**: `delegate_to_specialized_calc_toolset` (`tier = "core"`, `is_async()`)
- **System prompt**: `CALC_SPECIALIZED_DELEGATION` block in `constants.py`

For implementation details, see the [Writer documentation](writer-specialized-toolsets.md#3-implementation-reference).

---

## 2. Calc domains and feature coverage

WriterAgent organizes Calc tools into specialized domains to keep the main chat toolset focused. Below is the current implementation status and roadmap.

---

## 3. Implementation status and roadmap

### 3.1 Current implementation

| Domain / area | WriterAgent status | Module & tools | Notes |
|---------------|--------------------|----------------|-------|
| **Cells** | ✅ Implemented | `cells.py`: Get/SetCellValues, SetCellFormula, GetCellFormula | Basic cell operations on main list |
| **Ranges** | ✅ Implemented | `cells.py`: Get/SetRangeValues, Get/SetRangeFormulas | — |
| **Sheets** | ✅ Implemented | `sheets.py`: ListSheets, Create/Delete/RenameSheet, GetSheetProperties, SwitchSheet, GetSheetSummary | Basic sheet ops on main list |
| **Formulas** | ✅ Implemented | `formulas.py`: Get/SetFormula, EvaluateFormula, ListFormulaDependencies | — |
| **Charts** | ✅ Implemented | `charts.py`: ListCharts, Create/Edit/DeleteChart (shared with Writer) | Medium-fat API (`create_chart`) |
| **Named Ranges** | ✅ Implemented | `named_ranges.py`: ListNamedRanges, Create/Edit/DeleteNamedRange | — |
| **Data Validation** | ✅ Implemented | `validation.py`: SetDataValidation, GetDataValidationRules | Specialized tier |
| **Conditional Formatting** | ✅ Implemented | [`conditional.py`](../plugin/modules/calc/conditional.py): `add_conditional_format`, `list_conditional_formats`, `remove_conditional_formats` — [UNO / roadmap](calc-conditional-formatting.md) | Specialized tier |
| **Sheet filter (AutoFilter-style)** | ✅ Implemented | [`sheet_filter.py`](../plugin/modules/calc/sheet_filter.py): `apply_sheet_filter`, `clear_sheet_filter`, `get_sheet_filter` — [guide](calc-sheet-filter.md) | Specialized tier (`sheet_filter`) |
| **Pivot Tables** | ✅ Implemented | `pivot.py`: CreatePivotTable, RefreshPivotTable, GetPivotTableData, ListPivotTables | Specialized tier |
| **Tables** | ✅ Implemented | `tables.py`: CreateTable, GetTableInfo, SetTableStyle | — |
| **Shapes** | ✅ Implemented | `shapes.py`: Create/Edit/DeleteShape (shared with Writer/Draw) | — |
| **Comments** | ✅ Implemented | `comments.py`: ListCellComments, AddCellComment, DeleteCellComment | Specialized tier |
| **Forms** | ✅ Implemented | `forms.py`: CreateForm, GenerateForm, ListFormControls, CreateFormControl, EditFormControl, DeleteFormControl (shared with Writer) | Specialized tier |

### 3.2 Future enhancements (roadmap)

| Feature | Status | Notes |
|---------|--------|-------|
| **Macros** | ❌ Not implemented | Macro recording/execution, VBA compatibility |
| **Solver** | ❌ Not implemented | Optimization scenarios, constraint solving |
| **Goal Seek** | ❌ Not implemented | Target value analysis |
| **Scenarios** | ❌ Not implemented | Scenario manager, what-if analysis |
| **Data Tables** | ❌ Not implemented | One-way and two-way data tables |
| **External Data** | ❌ Not implemented | Database connections, SQL queries, web queries |
| **Advanced Forms** | ❌ Not implemented | Advanced form features, database integration, complex validation |
| **Advanced Chart Features** | ✅ Partial | Trend lines, error bars, secondary axes |
| **Pivot Chart Creation** | ❌ Not implemented | Direct pivot chart creation from data |
| **Dynamic Named Ranges** | ❌ Not implemented | Formula-based range definitions |
| **Array Formulas** | ✅ Partial | Basic support, matrix operations TBD |
| **Structured References** | ❌ Not implemented | Table-based formula references |
| **Table Slicers** | ❌ Not implemented | Interactive filtering controls |
| **Sheet Protection** | ❌ Not implemented | Cell/range locking, password protection |
| **Change Tracking** | ❌ Not implemented | Collaborative editing, comment history |

### 3.3 Cross-cutting improvements

- **MCP / API opt-in:** Config or query parameter to list `specialized` tools on `tools/list`
- **Performance tuning:** Timeouts and step limits for sub-agent execution
- **Telemetry:** Track domain usage to prioritize development
- **Documentation:** Keep [`AGENTS.md`](../../AGENTS.md) synchronized

For testing and operations details, see the [Writer documentation](writer-specialized-toolsets.md#4-testing-and-operations).

---

## 4. Summary

| Concern | Mechanism |
|---------|-----------|
| Smaller default tool list | `exclude_tiers` default in `ToolRegistry.get_tools` / `get_schemas` |
| Domain grouping | `ToolCalc*Base.specialized_domain` + `tier = "specialized"` |
| User/model entry point | `delegate_to_specialized_calc_toolset` (`tier = "core"`, async) |
| Sub-agent completion | `final_answer` (`tier = "specialized_control"`) |
| Prompt teaching | `CALC_SPECIALIZED_DELEGATION` in `constants.py` |
| Execution by name | Unchanged `execute()` — tier only affects **listing**, not **dispatch** |

This design trades a second LLM hop (delegation) for a **cleaner main conversation** and **safer tool choice**, while preserving a path to **full** Calc automation per domain.

---

## 5. References

For complete LibreOffice Calc UNO API documentation:
- [Official LibreOffice API Reference](https://api.libreoffice.org/)
- [LibreOffice Developer's Guide](https://wiki.documentfoundation.org/Documentation/DevGuide)
- [LibreOffice Development Tools](https://help.libreoffice.org/latest/en-US/text/shared/guide/dev_tools.html)
- [PyOOCalc - Python Libre/Open Office Calc interface API (UNO)](https://github.com/panpuchkov/pyoocalc)

For recent feature additions:
- [LibreOffice 26.2 Release Notes](https://www.howtogeek.com/libreoffices-first-big-update-for-2026-has-arrived/)
- [LibreOffice 26.2 New Features](https://9to5linux.com/libreoffice-26-2-open-source-office-suite-officially-released-this-is-whats-new)

---

## 7. Summary

| Concern | Mechanism |
|---------|-----------|
| Smaller default tool list | `exclude_tiers` default in `ToolRegistry.get_tools` / `get_schemas` |
| Domain grouping | `ToolCalc*Base.specialized_domain` + `tier = "specialized"` |
| User/model entry point | `delegate_to_specialized_calc_toolset` (`tier = "core"`, async) |
| Sub-agent completion | `final_answer` (`tier = "specialized_control"`) |
| Prompt teaching | `CALC_SPECIALIZED_DELEGATION` in `constants.py` |
| Execution by name | Unchanged `execute()` — tier only affects **listing**, not **dispatch** |

This design trades a second LLM hop (delegation) for a **cleaner main conversation** and **safer tool choice**, while preserving a path to **full** Calc automation per domain. Implementation status, infrastructure, priorities, phased roadmap, and the Calc API coverage map are consolidated in [§5 Implementation status and feature coverage](#5-implementation-status-and-feature-coverage).

---

## 8. References

For complete LibreOffice Calc UNO API documentation:
- [Official LibreOffice API Reference](https://api.libreoffice.org/)
- [LibreOffice Developer's Guide](https://wiki.documentfoundation.org/Documentation/DevGuide)
- [LibreOffice Development Tools](https://help.libreoffice.org/latest/en-US/text/shared/guide/dev_tools.html)
- [PyOOCalc - Python Libre/Open Office Calc interface API (UNO)](https://github.com/panpuchkov/pyoocalc)

For recent feature additions:
- [LibreOffice 26.2 Release Notes](https://www.howtogeek.com/libreoffices-first-big-update-for-2026-has-arrived/)
- [LibreOffice 26.2 New Features](https://9to5linux.com/libreoffice-26-2-open-source-office-suite-officially-released-this-is-whats-new)
