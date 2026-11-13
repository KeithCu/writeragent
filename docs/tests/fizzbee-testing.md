# FizzBee Formal Modeling and MCP Testing (Writer & Calc)

## Overview

This document describes the formal model checking and Model-Based Testing (MBT) infrastructure for WriterAgent's **MCP (Model Context Protocol)** server across **Writer** and **Calc** full toolset layouts, powered by **FizzBee** (formal specification language and model checker) and Python MBT runners.

The bar is **WriterAgent's mapping** (which `XText`, which range, which tool after a mutate), not a LibreOffice harness. Do not add random keystrokes, mouse, layout, fonts, undo, or spelling to this stack. Live UNO regressions such as `tests/writer/test_apply_document_content_table_cell_uno.py` stay in the native runner.

---

## 1. Motivation

WriterAgent exposes a large surface area over MCP:
- **Writer**: 108 tools across core functions and 18 specialized domains (`bookmarks`, `footnotes`, `tables`, `tracking`, `page`, `structural`, `styles`, `shapes`, `charts`, `indexes`, `textframes`, etc.).
- **Calc**: 61 tools across core functions and 12 specialized domains (`sheets`, `ranges`, `analysis`, `shapes`, `charts`, `cell_comments`, `conditional_formatting`, `errors`, `pivot_tables`, `python`, `search`, etc.).

Testing such large surfaces requires:
1. **Full layout discovery and validation**: Ensuring all tools produce valid MCP JSON schemas (`inputSchema`, normalized parameter types, `document_url` targeting support).
2. **Formal state machine modeling**: Verifying protocol transitions (`UNINITIALIZED` $\rightarrow$ `INITIALIZED`, exposure mode switches, session handling).
3. **Safety and invariant verification**: Guaranteeing structured error envelopes on invalid tools or parameters, correct tool visibility under different exposure modes (`direct_flat`, `delegate`, `direct_discovery`), and document/spreadsheet state integrity.
4. **Nested-text mapping**: A range never crosses two `XText` objects (body vs table cell vs footnote vs frame). Independent random tool picks against a MagicMock cannot see that; mutate-then-read sequences can.

---

## 2. FizzBee Installation & Verification

FizzBee is distributed as a standalone binary (Go). We provide an automated installer script and Make targets for easy developer onboarding:

### Installation
```bash
# Automated install into the venv (release tree under .venv/share/fizzbee,
# wrappers at .venv/bin/fizz and .venv/bin/fizzbee). GitHub assets are
# linux_x86 / linux_arm / macos_*; the installer must not grab protobuf stubs.
make install-fizzbee
# Or directly via python:
python scripts/install_fizzbee.py --install

# macOS Homebrew alternative:
brew tap fizzbee-io/fizzbee && brew install fizzbee
```

### Checking Formal Models
Directory config lives in [`tests/mcp/fizzbee/fizz.yaml`](../../tests/mcp/fizzbee/fizz.yaml) (`crash_on_yield: false`, `max_concurrent_actions: 1`). Each spec’s YAML frontmatter can still override `max_actions`.

To run the formal model checker against all `.fizz` specifications:
```bash
make check-fizzbee
```

---

## 3. FizzBee Formal Specifications

Formal specifications live in [`tests/mcp/fizzbee/`](../../tests/mcp/fizzbee/):

Specs use real FizzBee syntax (`action Init` for state, `atomic action`, `oneof`, `always assertion`). A `state:` block and `invariant Name:` are not valid FizzBee.

### A. Protocol Lifecycle (`tests/mcp/fizzbee/writer_mcp_protocol.fizz`)
- MCP server lifecycle states (`UNINITIALIZED`, `INITIALIZED`).
- Exposure modes (`DELEGATE`, `DIRECT_FLAT`, `DIRECT_DISCOVERY`).
- Document context targeting (`NONE`, `WRITER`, `CALC`, `DRAW`).
- Assertions: `Inv_InitializedBeforeCalls`, `Inv_FindToolsGating` (call-time mode stored on `last_call_mode`, not a growing history).

### B. Writer Tools Model (`tests/mcp/fizzbee/writer_tools_model.fizz`)
- Nested containers: body string, each table's representative cell string, footnotes, frames.
- Cursor: `(kind, id, start, end)` on one container.
- Actions include `CreateTable`, `SetCell`, `MoveCursorToCell`, `ApplySelection` (writes the **cursor** container, not the body), `GetSelection`.
- Invariants: `Inv_BookmarksBounded`, `Inv_TablesValidDimensions`, `Inv_PendingChangesOnlyWhenRecorded`, `Inv_CursorSameContainer`. Selection-vs-body mapping is pinned by [`tests/writer/test_apply_document_content_table_cell_uno.py`](../../tests/writer/test_apply_document_content_table_cell_uno.py) (FizzBee `last_op` cannot shrink a multi-step story).
- Alphabet is bounded for model checking. The live UNO test covers the MinerU-style cell-vs-body bug class (e.g. create 3×2 table, put text in a cell, select/edit the cell, verify the body stays untouched) — the same class of bug as `apply_document_content` building a cursor on `model.getText()` instead of `target_range.getText()`.

### C. Calc Tools Model (`tests/mcp/fizzbee/calc_tools_model.fizz`)
- Spreadsheet grid cells, formula ranges, sheet management, named ranges, and filters.
- Invariants: `Inv_SheetCountPositive` (`len(sheets) >= 1`), `Inv_ActiveSheetMustExist`, `Inv_NamedRangesIntegrity`.

---

## 4. Full Layout Extraction & Validation

The layout extraction helpers inspect, categorize, and validate all tools for Writer and Calc:

- **Writer Layout Helper**: [`tests/mcp/writer_full_layout.py`](../../tests/mcp/writer_full_layout.py)
- **Calc Layout Helper**: [`tests/mcp/calc_full_layout.py`](../../tests/mcp/calc_full_layout.py)

```python
from tests.mcp.writer_full_layout import extract_full_writer_layout, validate_mcp_schema
from tests.mcp.calc_full_layout import extract_full_calc_layout

writer_layout = extract_full_writer_layout()
calc_layout = extract_full_calc_layout()

print(f"Writer tools: {writer_layout['total_count']}") # 108
print(f"Calc tools:   {calc_layout['total_count']}")   # 61
```

---

## 5. Exposure Modes

WriterAgent MCP supports three tool exposure modes across both Writer and Calc:

| Mode | `tools/list` Content | Specialized Tools Access |
|------|----------------------|--------------------------|
| **`delegate`** (default) | Core tools only (~12-14 tools) | Via delegate gateway or direct call |
| **`direct_flat`** | Full layout (all core & specialized tools) | Listed directly in `tools/list` |
| **`direct_discovery`** | Core tools + `find_tools` | Via dynamic domain lookup with `find_tools` |

---

## 6. Running the Tests & Fuzzer

### A. Automated Pytest Suite
Run the Writer and Calc Model-Based Test suites:

```bash
# Run Writer MCP tests
pytest tests/mcp/test_fizzbee_writer_mcp.py -v

# Run Calc MCP tests
pytest tests/mcp/test_fizzbee_calc_mcp.py -v

# Run with custom steps or duration via environment variables
FIZZBEE_MCP_STEPS=2000 pytest tests/mcp/test_fizzbee_calc_mcp.py -v
FIZZBEE_MCP_DURATION_SEC=10 pytest tests/mcp/test_fizzbee_calc_mcp.py -v
```

### B. Dedicated CLI Randomized Fuzzer
A standalone runner in [`scripts/fizzbee_mcp_fuzzer.py`](../../scripts/fizzbee_mcp_fuzzer.py) runs randomized fuzzing over either Writer or Calc:

```bash
# Fuzz Writer (default)
python scripts/fizzbee_mcp_fuzzer.py --app writer --duration 5

# Fuzz Calc full toolset
python scripts/fizzbee_mcp_fuzzer.py --app calc --duration 5

# Run for a specific step count with malformed parameter mutations
python scripts/fizzbee_mcp_fuzzer.py --app calc --steps 2000 --mutate-rate 0.15 --verbose

# Writer: bias toward mutate-nested-then-read (default --pair-bias 0.4)
python scripts/fizzbee_mcp_fuzzer.py --app writer --steps 2000 --pair-bias 0.4

# Independent tool picks only (schema/wire fuzz, no pair follow-ups)
python scripts/fizzbee_mcp_fuzzer.py --app writer --steps 2000 --pair-bias 0
```

The CLI fuzzer still mocks `execute` (schema and JSON-RPC envelopes). That is why thousands of steps in 30 seconds is cheap. Pair bias does **not** run UNO; it only stops picking tools independently so sequences like `table_insert` → `get_document_content` or `apply_document_content` with `target=selection` after a nested mutate appear often. That order is what found the cell-content bug; MagicMock still cannot *execute* it. The nested-text oracle is the fast checker for container identity.

### Fuzzer Performance & Metrics
- **Throughput**: ~750–1,000 JSON-RPC requests/second.
- **Coverage**: Exercises **100% of all tools** in ~2 seconds.
- **Invariants Checked**: Validates JSON-RPC 2.0 response format, correct request/response ID matching, error envelope schemas, and absence of server crashes on every request.
- **Pair follow-ups**: Counted as `paired_followups` in the CLI summary.
