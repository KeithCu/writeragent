# Collabora Online Calc — single-cell `=PY()` spill

> **Status:** Second draft, 2026-09-24. Design for **F7** / checklist row **G11** in [`docs/scripting/numpy-jailsafe.md`](../scripting/numpy-jailsafe.md).
> **Code:** Collabora engine tree on this machine, `collabofficefull/engine` (Calc) and `collabofficefull/kit` (Online). This note lives in WriterAgent; the change does not.
> **Decision:** Teach Calc’s existing dynamic-array promotion that `PY` / `PYTHON` intends an array, and collapse a previous spill when a later result is a scalar or an error. Do not add a spill registry, an IDL flag, or a LOKit invalidation path.

---

## 1. What “done” means

A single cell entered as `=PY(...)` (Online: `.uno:EnterString`) whose Python result is a grid fills the neighboring cells through Calc’s dynamic-array engine, and shows `#SPILL!` when that rectangle is blocked.

The grid is the matrix the AddIn already builds. It is not re-shaped here.

| Python result (service JSON) | Matrix today | Spill |
| --- | --- | --- |
| `42`, `"ok"`, `null` | scalar (no matrix) | the formula cell only |
| `[1, 2, 3]` | **1×3 row** (`promoteFlatNumeric` in [`anyjson.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/anyjson.cxx)) | across columns: A1:C1 |
| `[[1, 2], [3, 4]]` | 2×2 | the block |
| one-element list | 1×1 matrix | the formula cell only |

LibrePy Classic spills a 1D list **down** a column (`_result_as_spill_grid` in `plugin/calc/python/function.py`). That is a different shape from the matrix Online already ships, and CSE matrix entry of `=PY` already uses the row. Turning the row into a column is an `anyjson` contract change with its own tests. It is not part of making the matrix spill.

`=@PY(...)` stays a scalar (top-left). Ctrl+Shift+Enter stays a static array of the size the user selected.

---

## 2. Why Classic’s write-back stays out of Core

Classic (`plugin/calc/python/function.py`, `_prepare_auto_spill` / `_queue_off_main_auto_spill`) posts a UI-thread task, writes literal values into neighbors, and remembers them in `WriterAgentSpillRegistry`.

`scaddins/source/pythoncompute/` is an `XVolatileResult` AddIn. It has no `ScDocument` and no `ScDocFunc`. Literal neighbor writes would also skip the dependency graph, undo, and the paint hint that LOKit turns into tiles. Calc already creates those neighbor cells as matrix **reference formulas** inside `ScDocument::ResizeMatrixFormula`. Plug into that.

---

## 3. How Calc spills today

Verified in the engine tree. Line numbers are this tree, not a stock LibreOffice checkout.

### 3.1 A typed formula may promote once, before the interpreter runs

`.uno:EnterString` / the input line call `EnterData(..., bAutoDynamicArray = true)` ([`cellsh3.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/cellsh3.cxx) `SID_ENTER_STRING` and `FID_INPUTLINE_ENTER`). That does two things:

1. Compiles with `bComputeII = false`, so the RPN keeps array-shaped tokens ([`viewfunc.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/viewfunc.cxx) `applyFormulaToCell`).
2. After the cell is cloned onto the document, sets `mbAutoDynamicArrayEligible`. The copy constructor clears that bit, so the setter has to run on the document cell.

`SID_SET_CELL_FORMULA` uses `SetCellText` and does **not** set the bit. Loaded cells and macro-created cells do not get it either. That is existing policy: only a UI entry auto-promotes.

On the first `InterpretTail`, if the bit is set, the cell is not already a matrix, not grouped, not a hyperlink, the RPN is not topped by `@`, and `rpnIntendsArrayResult` is true, the cell becomes a dynamic-array master **before** `ScInterpreter::Interpret()` ([`formulacell.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx) around 2220–2239):

- `cMatrixFlag = ScMatrixMode::Formula`
- declared size 1×1
- `mbDynamicArrayMaster = true`
- the eligible bit is cleared either way

The comment there is the constraint: matrix mode has to be set before the interpreter runs, or the array context is already gone. `=PY(...)+1` only adds 1 to every spilled cell when the interpret itself is in matrix mode. Promoting after the interpreter returns keeps the raw AddIn matrix and still loses elementwise array context.

`rpnIntendsArrayResult` is a stack walk ([same file](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx) `intendsArrayResultInRange`). A function result is an array only for `IsMatrixFunction`, a forced-array parameter, `ocSpill`, or a range operator. `ocExternal` is not in `FormulaCompiler::IsMatrixFunction`. `=SUM(PY(...))` must stay a scalar; an array-wide “this formula contains PY” flag would get that wrong, so the check belongs in the walk, on that token.

`FormulaTokenArray::HasDynamicArrayFunction` is set for `UNIQUE` / `FILTER` / `SORT` / … at compile time and is not read by this walk. Do not add `PY` to that flag as a second list.

### 3.2 The matrix is kept only for a matrix formula

After interpret, a non-null matrix on a cell whose `cMatrixFlag` is not `Formula` is replaced by its top-left token (around 2577–2582). That is the whole of today’s `=PY` bug once the AddIn has pushed an `ScMatrix`.

When the flag is `Formula` and the cell is a dynamic-array master (or already in spill state):

- Result larger than the declared size: `IsMatrixSpillBlocked`. Cells already inside the declared rectangle are skipped. Matrix reference cells are skipped. Any other non-empty cell, or a spill past the sheet edge, is `#SPILL!` (`FormulaError::Spill` on the **result**, not a sticky code error).
- Blocked, and the declared size is already bigger than 1×1: `MarkPendingMatrixResize` so the drain can collapse to 1×1.
- Unblocked and **smaller**: `ResizeMatrixFormula` inline.
- Unblocked and **larger**: `MarkPendingMatrixResize` (creating reference cells during interpret would recurse).

`ProcessPendingMatrixResizes` ([`documen4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen4.cxx) around 642) performs the queued resize. A `#SPILL!` master collapses to 1×1. A master with **no matrix is skipped**. That hole matters for `PY` and is called out in §5.

Drains today:

- end of `CalcFormulaTree` ([`documen7.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen7.cxx) around 440)
- end of `TrackFormulas`, when not inside an interpreter (around 584–588)
- end of the outermost `ScFormulaCell::Interpret` (formulacell around 2179–2186)

`ResizeMatrixFormula` broadcasts `SfxHintId::ScDataChanged` on the union of the old and new rectangles and `PostPaint`s that same range. `ScDocShell::PostPaint` broadcasts an `ScPaintHint`. LOKit tile invalidation listens to those paint hints. There is no separate “invalidate the origin cell only” path to fix.

`ResizeMatrixFormula` is not an undo step of its own. Editing a blocker already replays spill through `ScDocShell::ResolveSpilledOutputs` from `ScUndoEnterData`. A spilled `PY` uses that same undo.

The formula bar shows a dynamic-array master without `{...}` braces (formulacell around 1134).

---

## 4. What `=PY` does instead

```mermaid
sequenceDiagram
    participant User
    participant Cell as ScFormulaCell
    participant Interp as ScInterpreter
    participant Vol as PythonComputeVolatileResult
    participant Lis as ScAddInListener
    participant View as ScTabView

    User->>Cell: EnterString =PY("result = [[1, 2], [3, 4]]")
    Note over Cell: eligible bit set; rpnIntendsArrayResult is false for ocExternal
    Cell->>Interp: Interpret (pass 1)
    Interp->>Vol: getPy returns XVolatileResult
    Vol-->>Lis: modified("#BUSY!") while still inside pass 1
    Note over Lis: TrackFormulas sees IsInInterpreter and does not calc
    Interp-->>Cell: store literal #BUSY!; eligible bit cleared; matrix flag still NONE
    Vol->>Lis: finish() later, under SolarMutexGuard
    Lis->>Cell: Notify → dirty + formula track
    Note over Lis: TrackFormulas moves the cell onto the formula tree.<br/>ONLOAD_LENIENT is not FORCED, so it does not Interpret.
    Lis->>View: doc shell ScDataChanged
    View->>Cell: paint → InterpretVisible → Interpret (pass 2)
    Note over Cell: matrix is 2×2 but cMatrixFlag is still NONE
    Note over Cell: top-left token replaces the matrix; cell shows 1
```

Two separate misses:

1. **Intent.** The RPN walk never treats this AddIn as an array, so pass 1 never sets `mbDynamicArrayMaster`. The eligible bit is one-shot. Pass 2 cannot promote through that gate.
2. **When pass 2 runs.** `ScExternal` marks the formula `ScRecalcMode::ONLOAD_LENIENT` ([`interpr4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/interpr4.cxx) around 3436), same as the compiler’s `ocExternal` case. `TrackFormulas` only calls `CalcFormulaTree(true)` for `IsRecalcModeForced()`. The second interpret is whoever next interprets that dirty cell. On screen that is `ScTabView::InterpretVisible` during paint, after the doc-shell `ScDataChanged` from `ScAddInListener::modified`. A headless test that only calls `pythoncompute_complete_json` leaves the cell dirty until something calls `Interpret` or `CalcFormulaTree(false)`.

`g_aPending` is one process-wide map from request id to volatile result ([`bridge.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/bridge.cxx)). Every view shares the document. `finish()` re-acquires the solar mutex because kit poll delivers the result with solar released. Do not add another mutex.

The interim value is the string `"#BUSY!"`, not a formula error ([`volatile.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/volatile.cxx)). There is no `FormulaError::Busy`. Keep it that way.

---

## 5. The change

One helper, used from `intendsArrayResultInRange` when the opcode is `ocExternal` and the token is `svExternal`. True when the programmatic name equals, ASCII-case-insensitive:

- `org.collaboraoffice.sheet.addin.PythonComputeFunctions.getPy`
- `org.collaboraoffice.sheet.addin.PythonComputeFunctions.getPython`

That is the same `FormulaExternalToken::GetExternal()` string the compiler already special-cases for `Analysis.getRandbetween`. The display names `PY` and `PYTHON` are not what the token stores.

Then the existing gate does the rest for a UI-entered cell:

1. Pass 1 promotes to a 1×1 dynamic-array master, then stores `"#BUSY!"`. Declared size is already 1×1, and the result is not a matrix, so nothing resizes.
2. Pass 2 interprets in matrix mode. `SetResult` has already built the `ScMatrix` ([`addincol.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/addincol.cxx) `sequence<sequence<double>>` / `sequence<sequence<Any>>`). The matrix is kept. A larger result queues `MarkPendingMatrixResize`; the interpret epilogue drains it.
3. A 1×1 matrix or a scalar leaves the cell as a 1×1 master. The displayed value is that scalar.

### Scalar and error after a spill

`PY` is allowed to return a scalar after it has returned a grid. `GetMatrix()` is then null, and today’s drain **ignores** a pending resize with no matrix. The old reference cells would stay.

When the master is dynamic, the new result is not a matrix, the declared size is larger than 1×1, and a threaded group calc is not running: call `ResizeMatrixFormula(pos, 1, 1)`, same as a shrinking matrix. `ResizeMatrixFormula` already re-queues itself while a cell-store iterator is live. Extend `ProcessPendingMatrixResizes` so that deferred retry still collapses a dynamic master with no matrix and a declared size above 1×1. Without that, the guarded retry is a no-op.

Do not collapse when the result string is `"#BUSY!"`. Pass 1, and any later recalc that observes the interim, would otherwise wipe a live spill and expand it again when the real result arrives. A finished Python value that is exactly the string `#BUSY!` also skips collapse. That one string is the interim marker; leave it.

`#SPILL!` already collapses through the error branch of the drain. Blocked spills do not need a new path.

### What this deliberately does not touch

- IDL and `XPythonComputeFunctions`. The return is `Any` because of the volatile. An extra “returns matrix” annotation would be a second copy of the name check.
- `anyjson` shape, including the 1×N row for a flat list.
- `IsMatrixFunction`’s opcode list. Other AddIns stay scalars.
- CSE entry (`FID_INPUTLINE_MATRIX` → `EnterMatrix`). `cMatrixFlag` is already `Formula`, so the eligible-bit gate does not run, and `bShouldCheckSpill` stays false for a static master.
- Formula groups. `mxGroup` refuses promotion. Identical filled-down `PY` cells stay single-cell, same as any other grouped formula.
- LOKit / `ChildSession`. The paint rect is already the resized bounding box. Confirm with a test; do not add an invalidation API up front.
- A Cypress test. CppUnit is the gate. A browser check is optional after the engine test is green.
- WriterAgent’s Excel converter. `ANCHORARRAY` / `cm="1"` import there is still an A1 snapshot ([`ms-py-compatibility.md`](../scripting/ms-py-compatibility.md)). This work does not make those snapshots live.

---

## 6. Files on disk

Dynamic vs static is **not** “ODF matrix span, therefore dynamic.”

**ODS** ([`xmlexprt.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/filter/xml/xmlexprt.cxx) `WriteCell`, [`xmlcelli.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/filter/xml/xmlcelli.cxx)):

- An expanded dynamic master is a matrix cell (`table:number-matrix-columns-spanned` / `rows-spanned`) plus extended-ODF `coext:spill="true"`.
- A master currently in `#SPILL!` is saved as a **non-array** formula with `coext:spill="true"`. Import calls `SetDynamicArrayMaster(true)` and `MarkFormulaSpilled`.
- A static CSE matrix has the span attributes and no `coext:spill`.
- Plain ODF (not extended) has nowhere to store dynamic-vs-static. That limitation is Calc’s, shared with `UNIQUE`.

**XLSX** is the other filter, not the ODS one. `sc/source/filter/oox/formulabuffer.cxx` is OOXML import: `SetDynamicArrayMaster(true)` when the cell was a dynamic-array master. Export writes `cm="1"` from `IsDynamicArrayMaster()` ([`xetable.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/filter/excel/xetable.cxx) `XclExpFormulaCell::SaveXml`) and the `xl/metadata.xml` dynamic-array record. `_xlfn.ANCHORARRAY` is the spilled-range operator (`A1#`), not the master’s `cm` bit.

Once pass 1 has set `mbDynamicArrayMaster`, save/load already round-trips it. No new file-format attribute.

The eligible bit is not saved and must not be. It exists only until the first interpret.

---

## 7. Tests

Engine CppUnit, not WriterAgent pytest. `sc/ucalc_setup.mk` already optionally links `pythoncompute`.

Confirm while writing the test that this harness compiles `=PY` to an `ocExternal` token. If it does not, the formula-level test belongs in a subsequent test that has the office service manager, and the intent helper still needs a direct token-array test.

Drive the volatile the way the AddIn does: install no HTTP emitter, call `pythoncompute_complete_json` with a finished envelope, then `Interpret()` the cell (or `CalcFormulaTree(false)`). Do not expect `TrackFormulas` alone to replace `#BUSY!`.

| Case | Expect |
| --- | --- |
| UI-style entry (`SetAutoDynamicArrayEligible(true)`) of `=PY` returning `[[1, 2], [3, 4]]` | master, 2×2, values in the block, reference cells are `ScMatrixMode::Reference` |
| same, with a non-empty neighbor in the block | `FormulaError::Spill`, declared size back to 1×1, blocker cell unchanged |
| clear the blocker and interpret again | spill expands |
| `[1, 2, 3]` | 1×3, values to the **right** |
| scalar `42` after a 2×2 spill | reference cells removed, origin shows 42 |
| `"#BUSY!"` while a 2×2 spill is already up | spill unchanged |
| `=@PY` returning a 2×2 | top-left only, not a dynamic master |
| CSE over a pre-sized range | static matrix, no auto-resize |
| `=SUM(PY(...))` with a grid result | scalar, not a master |

One ODS round-trip and one XLSX round-trip of a spilled master: `coext:spill="true"` / `cm="1"`, and the block is still a dynamic master after reload. Copy the shape of `testArrayFormulaSpillRoundtripODS` / `testDynamicArraySpilledExportXLSX` in `sc/qa/unit/subsequent_export_test2.cxx`.

If `ResizeMatrixFormula`’s `PostPaint` range in that test is the single origin cell, stop and fix the paint rect. Do not start that work before the assert exists.

---

## 8. Out of scope

- DataFrame header rows. Classic adds those in `result_to_calc_grid` before spilling. Online spills the JSON grid the service sent.
- Short Python errors in the cell (F6).
- Images (F3).
- Changing TrackFormulas so `ONLOAD_LENIENT` interprets immediately. Visible cells already leave `#BUSY!` on the next paint. Off-screen cells wait for paint or a real recalc, which is today’s volatile behavior.
- Making formula groups spill.

---

## 9. Pointers

| What | Where |
| --- | --- |
| Promotion gate | [`formulacell.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx) ~2220–2239 |
| Array-intent walk | same file, `intendsArrayResultInRange` ~1668–1754 |
| Top-left drop and spill check | same file, ~2575–2668 |
| Interpret drains a queued resize | same file, ~2179–2186 |
| Eligible bit set only for UI entry | [`viewfunc.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/viewfunc.cxx) ~570–575, [`cellsh3.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/cellsh3.cxx) `SID_ENTER_STRING` |
| Collision, resize, drain | [`documen4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen4.cxx) `IsMatrixSpillBlocked`, `ResizeMatrixFormula`, `ProcessPendingMatrixResizes` |
| TrackFormulas does not interpret this AddIn | [`documen7.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen7.cxx) `TrackFormulas` |
| Async listener | [`addinlis.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/addinlis.cxx) `ScAddInListener::modified` |
| AddIn call and matrix push | [`interpr4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/interpr4.cxx) `ScExternal`, [`addincol.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/addincol.cxx) `SetResult` |
| `#BUSY!`, solar, pending map | [`volatile.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/volatile.cxx), [`bridge.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/bridge.cxx) |
| Flat list is a row | [`anyjson.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/anyjson.cxx) `promoteFlatNumeric`; asserted in `scaddins/qa/pythoncompute.cxx` |
| Classic column spill | `plugin/calc/python/function.py` `_result_as_spill_grid` |
