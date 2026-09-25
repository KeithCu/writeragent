# Collabora Online Calc — Single-Cell `=PY()` Dynamic Array Spill

> **Status:** Revised draft, 2026-09-25. Design for **F7** / checklist row **G11** in [`docs/scripting/numpy-jailsafe.md`](../scripting/numpy-jailsafe.md).  
> **Codebase:** Collabora engine tree on this machine: `collabofficefull/engine` (Calc core) and `collabofficefull/kit` (Online). This design note lives in WriterAgent; the engine implementation is in the C++ engine tree.  

---

## 1. Background: What Exists Today vs. The MVP Patches

To understand where this work fits, it is essential to distinguish between what is already in production today, what is introduced by the pending Collabora Online MVP patches, and how native Calc handles dynamic arrays.

### 1.1 What WriterAgent / LibrePy Does Today (Production)
WriterAgent / LibrePy is already shipped and running in production as a desktop extension for LibreOffice:
1. **Local Python Execution:** LibrePy runs Python scripts directly in a local virtual environment (venv) on the user's desktop, communicating with Calc over UNO.
2. **1D List Normalization:** When a Python formula returns a 1D list (e.g. `[1, 2, 3]`), LibrePy’s `_result_as_spill_grid` ([`plugin/calc/python/function.py`](file:///home/keithcu/Desktop/Python/writeragent/plugin/calc/python/function.py)) normalizes it to a column vector `[[1], [2], [3]]`, matching Microsoft Excel's `=PY()` behavior.
3. **Extension-Level Workaround for Spilling:** Because stock LibreOffice Calc did not natively spill external AddIn formulas, LibrePy historically implemented an out-of-engine workaround:
   * When `=PY(...)` returns a multi-cell grid, LibrePy queues a deferred callback on LibreOffice's main UI thread.
   * That callback writes literal values directly into adjacent cells via UNO (`setDataArray`) and records the spilled cell addresses in a custom document property dictionary (`WriterAgentSpillRegistry`).
4. **Why LibrePy's Workaround Cannot Be Used in Core or Online:**
   * **No Document Access:** The Collabora Online C++ AddIn is a thin `XVolatileResult` component that has no access to `ScDocument` or `ScDocFunc`.
   * **Dependency Graph:** Writing literal values bypasses Calc's formula dependency graph (DAG). Formulas referencing spilled cells do not recalculate properly when the source formula changes.
   * **Undo/Redo:** External UNO writes bypass Calc's native formula undo stack (`ScUndoEnterData`).
   * **Tile Invalidation (LOKit):** Online rendering depends on LibreOfficeKit tile invalidations emitted by Calc's paint hints (`ScPaintHint`). Bypassing the core matrix resizer causes neighboring tiles to not repaint.

### 1.2 The Collabora Online MVP Patches (Gerrit 8122 & 8123)
The initial Collabora Online Python integration is introduced by two foundational patchsets currently in Gerrit (and present in the local `collabofficefull/` tree):
* **Gerrit 8122 (`collabofficefull/engine`):** Adds the core C++ AddIn (`scaddins/source/pythoncompute/`) registering `=PY()` and `=PYTHON()`. It implements UNO’s `XVolatileResult` interface, displays an interim `"#BUSY!"` string, and parses the returned JSON into an `ScMatrix` via `anyjson.cxx`.
* **Gerrit 8123 (`collabofficefull/kit`):** Implements the broker in `kit` and `coolwsd` that extracts cell arguments and forwards the code to an isolated Python compute service over HTTP.

### 1.3 Native Dynamic Arrays in Calc Core: What Exists and What UNO Exposes
A critical finding from engine inspection is that **Calc core already has a full, native dynamic-array engine**:
* Functions like `FILTER`, `SORT`, `UNIQUE`, and `=SEQUENCE(rows, cols)` natively spill across rows and columns.
* The engine tracks dynamic array masters (`mbDynamicArrayMaster` in [`formulacell.hxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/inc/formulacell.hxx)), manages matrix reference cells (`ScMatrixMode::Reference`), performs collision detection with `#SPILL!` (`IsMatrixSpillBlocked` in [`documen4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen4.cxx)), executes dynamic resizing (`ResizeMatrixFormula` / `ProcessPendingMatrixResizes`), invalidates LOKit tiles via `ScPaintHint`, and round-trips through ODS (`coext:spill="true"`) and XLSX (`cm="1"` / `_xlfn.ANCHORARRAY`).

#### Why Native Dynamic Arrays Are Not Exposed to Standard UNO
Why can't extensions simply trigger dynamic arrays via standard UNO?

As documented by Calc engine developers in [`formulacell.cxx:2220-2226`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx#L2220-L2226):
```cpp
// Loaded and macro-created cells leave the flag false so legacy formulas keep their old result.
```
When an external script or UNO macro calls `cell.setFormula("=...")`, it invokes `ScDocFunc::SetCellText(..., GRAM_API)` in `cellsuno.cxx`. This path deliberately does **not** set `bAutoDynamicArray = true`. 

**The Rationale:** If standard UNO `setFormula()` auto-promoted formulas to dynamic arrays, decades of existing VBA macros, Basic scripts, and UNO extensions that expect formulas to return single scalars would silently break by having cells unexpectedly spill over and overwrite adjacent data.

#### The Crucial Distinction: Collabora Online UI Entry
While macro-created UNO calls do not auto-promote, **browser user entry in Collabora Online does!**
* When a user types a formula in the Collabora Online browser interface, Online dispatches the UNO command **`.uno:EnterString`**.
* `.uno:EnterString` routes through `cellsh3.cxx` (`SID_ENTER_STRING`), which calls `EnterData(..., bAutoDynamicArray = true)`.
* This sets `pCell->SetAutoDynamicArrayEligible(true)` ([`viewfunc.cxx:574`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/viewfunc.cxx#L574)).

---

## 2. The Problem: What Happens Under the MVP Patches Today

With the MVP patchset (Gerrit 8122 & 8123) in place, when a user enters `=PY(...)` in Collabora Online and the Python code produces a multi-cell result (a 2D matrix or a 1D list), **Calc displays only the top-left scalar element and silently discards the rest of the matrix**.

### 2.1 Why the Matrix Gets Discarded
Even though `.uno:EnterString` sets `mbAutoDynamicArrayEligible = true`, the cell must pass an array-intent check during its very first calculation pass ([`formulacell.cxx:2232-2238`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx#L2232-L2238)):
```cpp
if (!rpnTopIsImplicitIntersection(*pCode)
    && rpnIntendsArrayResult(*pCode))
{
    cMatrixFlag = ScMatrixMode::Formula;
    SetMatColsRows(1, 1);
    mbDynamicArrayMaster = true;
}
mbAutoDynamicArrayEligible = false;
```
1. `rpnIntendsArrayResult` walks the compiled RPN token stack (`intendsArrayResultInRange`).
2. External AddIn functions are represented by opcode `ocExternal`.
3. `ocExternal` is not recognized as an array-returning function by this walk.
4. Because array intent is not detected, the cell is not promoted to a dynamic-array master (`cMatrixFlag` remains `ScMatrixMode::None`).
5. When the asynchronous Python computation completes and the AddIn delivers the real `ScMatrix` via `SetResult()`, Calc executes lines 2577–2582 of `formulacell.cxx`: **because `cMatrixFlag` is not `Formula`, Calc replaces the matrix with its top-left scalar element**.

### 2.2 Why Legacy Ctrl+Shift+Enter (CSE) Array Entry is Unacceptable
In legacy spreadsheets, multi-cell formula output required the user to pre-select a fixed target rectangle (e.g. `A1:C20`) and press **Ctrl+Shift+Enter (CSE)**. While CSE entry does set `cMatrixFlag = Formula` and retains matrices, it is unusable for real-world Python workflows because usually the output size cannot be known in advance.

* **Example:** Suppose a Python formula filters a company dataset:
  ```python
  # How many salespeople from the Midwest sold revenue > $500K?
  df[(df['Region'] == 'Midwest') & (df['Revenue'] > 500_000)]
  ```
  How many rows will this return? It could be 3 salespeople this month, 14 next quarter, or 0 during a slow period.
* **No Automatic Resizing:** Static CSE array ranges are fixed at entry time and cannot expand or shrink automatically when upstream data updates or the query result set changes size.

Dynamic array spilling eliminates this guesswork entirely: the user enters `=PY(...)` into a single cell, and Calc automatically grows or shrinks the spilled rectangle to match the exact dimensions returned by Python.

---

## 3. The Goal and Why This is a Remarkably Small Code Change

### 3.1 The Goal
The goal of this design (F7) is to enable native, single-cell dynamic-array spill for `=PY(...)` in Calc core:
* **Single-Cell Entry:** A user types `=PY(...)` into a single cell using regular Enter (Online: `.uno:EnterString`), without requiring legacy Ctrl+Shift+Enter (CSE) array syntax.
* **Native Spilling:** When Python returns a grid, Calc’s engine automatically promotes the cell to a dynamic-array master and allocates neighboring cells as matrix reference cells (`ScMatrixMode::Reference`).
* **Collision Detection:** If the target rectangle contains existing non-empty cells, the formula cell displays `#SPILL!` and does not overwrite existing data. When the collision is resolved by the user, the spill automatically expands.
* **Dynamic Lifecycle:** If a subsequent recalculation returns a smaller grid, a scalar, or an error, the old spill rectangle collapses automatically, clearing the previous reference cells.
* **Standard Persistence:** Spilled dynamic arrays round-trip transparently in ODS (`coext:spill="true"`) and XLSX (`cm="1"` / `_xlfn.ANCHORARRAY`).

### 3.2 The Key Architectural Insight: A Tiny Core Change
Because native dynamic array support, collision detection, reference cell management, tile invalidations, and file filters **already exist in Calc core**, we do **not** need to write a new spill engine or invent complex cross-layer protocols.

The required core change is approximately **15–20 lines of C++**:
1. **Teach the Array-Intent Walk (~10 lines in `formulacell.cxx`):** In `intendsArrayResultInRange`, when encountering `ocExternal`, check if the programmatic token name is `getPy` or `getPython`. If so, return `true`. This allows `.uno:EnterString` to promote `=PY(...)` to a dynamic master on Pass 1.
2. **Handle Scalar/Error Collapse (~10 lines in `documen4.cxx`):** In `ProcessPendingMatrixResizes`, ensure that if a dynamic-array master recalculates into a scalar or error (having no matrix), it calls `ResizeMatrixFormula(pos, 1, 1)` to clear old reference cells (while preserving `#BUSY!` during in-flight recalculations).

---

## 4. The Data Pipeline and 1D List Shape

### 4.1 The Data Pipeline: Python → JSON → `ScMatrix` → Sheet
To understand where this work fits, it is helpful to follow how multi-cell data flows from the Python runtime into Calc:

1. **Python Output:** The Python script runs in the compute worker and produces an array, list, DataFrame, or Series.
2. **JSON Wire Format:** The compute service serializes the result into a standard JSON envelope:
   * 2D matrix: `[[1, 2], [3, 4]]`
   * 1D list: `[1, 2, 3]`
   * Scalar: `42`, `"hello"`, `null`
3. **C++ AddIn Deserialization (`anyjson.cxx`):** Collabora Online's `scaddins/source/pythoncompute/anyjson.cxx` parses the JSON result into a UNO nested sequence (`Sequence<Sequence<double>>` or `Sequence<Sequence<Any>>`) and delivers it through `SetResult()`.
4. **Interpreter Matrix Construction:** Calc's `ScInterpreter` turns that UNO nested sequence into an internal `ScMatrix`.
5. **Dynamic Spill Allocation:** Calc’s dynamic-array engine reads the dimensions of that `ScMatrix` and resizes the formula rectangle on the sheet via `ScDocument::ResizeMatrixFormula`.

The core spill engine changes described in this document operate at **Step 5**. The engine dynamically allocates cells to match the dimensions of whatever `ScMatrix` is provided by Step 4.

### 4.2 Result Shapes and 1D List Orientation (Row vs. Column)

| Python Result (Service JSON) | Matrix Built by AddIn Today | Dynamic Spill Behavior with F7 |
| --- | --- | --- |
| `42`, `"ok"`, `null` | Scalar (no matrix) | Origin cell only (1×1) |
| `[[1, 2], [3, 4]]` | 2×2 matrix | 2×2 rectangle |
| `[1, 2, 3]` (1D list) | 1×3 row (`promoteFlatNumeric` in `anyjson.cxx`) | Spills across columns: `A1:C1` |
| `[42]` (1-element list) | 1×1 matrix | Origin cell only (1×1) |

#### Analysis: Why LibrePy and Excel Spill 1D Lists as Columns
There is a notable difference in how 1D lists are shaped today between LibrePy, Microsoft Excel, and Collabora Online:

* **Microsoft Excel Python (`=PY()`):** When Python code in Excel returns a 1D Python `list` `[1, 2, 3]`, a 1D NumPy array `np.array([1, 2, 3])`, or a Pandas `Series`, **Excel spills it vertically down a column** (rows A1:A3). To spill across a row, Excel requires an explicit 2D structure: `[[1, 2, 3]]` or `arr.reshape(1, -1)`.
* **WriterAgent / LibrePy:** LibrePy’s desktop extension (`_result_as_spill_grid` in [`plugin/calc/python/function.py`](file:///home/keithcu/Desktop/Python/writeragent/plugin/calc/python/function.py)) also converts a 1D list `[1, 2, 3]` into a column `[[1], [2], [3]]`, matching Excel.
* **Collabora Online's `anyjson.cxx`:** Today, Online’s C++ AddIn (`promoteFlatNumeric` and `elemsToAny`) converts a flat JSON array `[1, 2, 3]` into a 1×N row matrix (`Sequence<Sequence<double>>` of length 1, containing $N$ elements).

**Conclusion:** LibrePy’s column-oriented design is **not wrong**—it adheres to Microsoft Excel’s `=PY()` behavior and matches standard spreadsheet conventions (data sequences and records populate rows down a column; `=SEQUENCE(5)` defaults to a column).

Collabora Online’s 1×N row orientation in `anyjson.cxx` was an arbitrary initial prototype decision. Because Calc’s dynamic-array engine is completely dimension-agnostic, F7 works with whatever matrix shape the AddIn provides. Aligning `anyjson.cxx` to emit $N \times 1$ column vectors for 1D lists is an `anyjson` contract change that should be addressed as a companion task (see [§10](#10-out-of-scope--future-work)).

---

## 5. How Calc’s Native Dynamic Array Spill Engine Works Today

Understanding the engine's promotion and resize pipeline is essential for integrating external AddIns cleanly.

### 5.1 Pre-Calculation Promotion Gate
When a user types a formula and presses Enter (in Collabora Online: `.uno:EnterString` / `FID_INPUTLINE_ENTER`), the view calls `EnterData(..., bAutoDynamicArray = true)` in `cellsh3.cxx`. This triggers two critical actions:
1. **RPN Compilation:** The formula compiles with `bComputeII = false` (`applyFormulaToCell` in `viewfunc.cxx`), ensuring the Reverse Polish Notation (RPN) token array retains array-shaped tokens.
2. **Eligibility Flag:** After the new formula cell is instantiated on the document, `mbAutoDynamicArrayEligible` is set to `true`. (The copy constructor clears this flag, so it must be set directly on the document cell).

During the cell's very first calculation pass (`InterpretTail` in `formulacell.cxx` around lines 2220–2239), Calc checks if the cell qualifies to become a dynamic-array master:
* The eligibility bit `mbAutoDynamicArrayEligible` is `true`.
* The cell is not already a matrix formula (`cMatrixFlag == ScMatrixMode::None`).
* The cell is not part of a formula group (`mxGroup` is null).
* The formula RPN does not begin with the intersection operator `@`.
* **Array Intent Walk:** `intendsArrayResultInRange` returns `true`.

If all conditions are met, the cell is promoted to a dynamic-array master **before the interpreter executes**:
* `cMatrixFlag = ScMatrixMode::Formula`
* Declared size is initialized to 1×1.
* `mbDynamicArrayMaster = true`.
* The eligibility bit `mbAutoDynamicArrayEligible` is cleared (it is strictly one-shot).

> **Why Pre-Interpretation Promotion is Mandatory:**  
> Matrix mode must be enabled on the cell *before* `ScInterpreter::Interpret()` runs. If the interpreter runs in scalar mode, array context is lost. For example, in `=PY(...)+1`, adding 1 to every element of the Python grid only works if the outer interpreter evaluates the addition in matrix mode.

`intendsArrayResultInRange` inspects the RPN token stack. Today, it only returns `true` for functions listed in `FormulaCompiler::IsMatrixFunction`, forced-array parameters, range operators, or `ocSpill`. External AddIn functions (`ocExternal`) are not in `IsMatrixFunction`, so this walk returns `false`.

### 5.2 Matrix Retention vs. Truncation
During calculation, if a function returns an `ScMatrix`:
* If `cMatrixFlag` is `ScMatrixMode::Formula`, Calc retains the matrix and evaluates spill dimensions.
* If `cMatrixFlag` is `ScMatrixMode::None` (a standard scalar cell), Calc executes lines 2577–2582 of `formulacell.cxx`: **the matrix is discarded and replaced with its top-left scalar element**.

### 5.3 Collision Detection (`#SPILL!`) and Queued Resizing
When a dynamic-array master returns a matrix:
1. **Collision Check (`IsMatrixSpillBlocked` in `documen4.cxx`):** Calc inspects the proposed rectangle. Empty cells and existing matrix reference cells belonging to this master are valid. Any non-empty cell (or extending past the sheet boundary) causes a collision.
2. **Collision Response:** Calc sets the cell result to `FormulaError::Spill` (displaying `#SPILL!`). If the master previously had an expanded grid, `MarkPendingMatrixResize` is called to queue a collapse back to 1×1.
3. **Expansion:** If unblocked and larger than the declared size, Calc calls `MarkPendingMatrixResize`. (Resizing cannot occur synchronously inside `Interpret()` because creating matrix reference cells would trigger recursive document mutations).
4. **Execution of Resizes (`ProcessPendingMatrixResizes`):** Queued resizes are processed at safe points:
   * At the end of `CalcFormulaTree` (`documen7.cxx`).
   * At the end of `TrackFormulas`.
   * In the epilogue of the outermost `ScFormulaCell::Interpret`.

`ResizeMatrixFormula` updates the matrix reference cells, broadcasts `SfxHintId::ScDataChanged` over the union of the old and new rectangles, and calls `ScDocShell::PostPaint`.

### 5.4 Tile Invalidation and Undo
* **Tile Rendering:** `ScDocShell::PostPaint` broadcasts an `ScPaintHint`. Collabora Online’s LibreOfficeKit listens to these paint hints to invalidate and re-render tiles for the entire affected rectangle. No custom LOKit invalidation code is needed.
* **Undo:** `ResizeMatrixFormula` is not an isolated undo action. Resolving or triggering a spill integrates directly with Calc's existing `ScUndoEnterData` and `ResolveSpilledOutputs`.

---

## 6. Baseline Analysis: Why `=PY()` Fails to Spill Under the MVP Patches Today

The core dynamic-array spill code for `=PY` is not yet implemented. Below is the baseline execution flow in the engine with the MVP patches applied, illustrating exactly where and why the matrix is lost.

Collabora Online’s `pythoncompute` AddIn is an asynchronous component utilizing UNO’s `XVolatileResult` interface:

```mermaid
sequenceDiagram
    participant User
    participant Cell as ScFormulaCell
    participant Interp as ScInterpreter
    participant Vol as PythonComputeVolatileResult
    participant Lis as ScAddInListener
    participant View as ScTabView

    User->>Cell: EnterString =PY("result = [[1, 2], [3, 4]]")
    Note over Cell: eligible bit set by .uno:EnterString; rpnIntendsArrayResult is false for ocExternal
    Cell->>Interp: Interpret (Pass 1)
    Interp->>Vol: getPy returns XVolatileResult
    Vol-->>Lis: modified("#BUSY!") while still inside Pass 1
    Note over Lis: TrackFormulas sees IsInInterpreter; skips immediate calc
    Interp-->>Cell: store literal "#BUSY!"; eligible bit cleared; cMatrixFlag remains NONE
    Vol->>Lis: finish() called later under SolarMutexGuard
    Lis->>Cell: Notify → marks cell dirty + TrackFormulas
    Note over Lis: Cell is marked ONLOAD_LENIENT (not FORCED), so it waits for next view calc
    Lis->>View: doc shell ScDataChanged
    View->>Cell: paint → InterpretVisible → Interpret (Pass 2)
    Note over Cell: AddIn returns 2×2 ScMatrix, but cMatrixFlag is still NONE!
    Note over Cell: formulacell.cxx lines 2577–2582 discard matrix; origin displays top-left "1"
```

### The Two Points of Failure:
1. **Pass 1 (Missed Intent):** During initial entry via `.uno:EnterString`, `intendsArrayResultInRange` encounters `ocExternal` for `getPy`. Because it does not recognize `getPy` as intending an array, the cell is not promoted to a dynamic master. `mbAutoDynamicArrayEligible` is cleared. Pass 1 finishes by storing the temporary string `"#BUSY!"`.
2. **Pass 2 (Dropped Matrix):** When the background Python process delivers the real result, `finish()` notifies the listener and dirty-flags the cell. When the view repaints, Pass 2 interprets the formula and `SetResult` delivers the full $2 \times 2$ `ScMatrix`. However, because `cMatrixFlag` is still `ScMatrixMode::None`, Calc discards all elements except the top-left value (`1`).

---

## 7. Proposed Engine Modifications

The implementation requires minimal, targeted modifications to Calc core (`sc/`).

### 7.1 Recognizing Array Intent for `PY` and `PYTHON`
In `formulacell.cxx`, update `intendsArrayResultInRange` to recognize the external programmatic function names of the Python compute AddIn.

When the token opcode is `ocExternal` and the token type is `svExternal`, inspect the programmatic name via `FormulaExternalToken::GetExternal()`. Return `true` if it matches (case-insensitively):
* `org.collaboraoffice.sheet.addin.PythonComputeFunctions.getPy`
* `org.collaboraoffice.sheet.addin.PythonComputeFunctions.getPython`

*(This follows the exact pattern already used in `FormulaCompiler` for external functions such as `Analysis.getRandbetween`).*

With this change:
1. **Pass 1:** `intendsArrayResultInRange` returns `true`. The cell is promoted to a 1×1 dynamic-array master before the first interpreter run. Pass 1 completes and stores `"#BUSY!"`.
2. **Pass 2:** The cell evaluates in matrix mode (`cMatrixFlag == ScMatrixMode::Formula`). When the AddIn pushes the `ScMatrix`, Calc retains the matrix and calls `MarkPendingMatrixResize`. The interpret epilogue calls `ProcessPendingMatrixResizes`, creating matrix reference cells and spilling the array cleanly into the grid.

### 7.2 Dynamic Resizing, Shrinking, and Scalar Collapse
A Python formula may return a multi-cell grid on one run and a single scalar or error on a subsequent run:
* **Current Limitation:** In `ProcessPendingMatrixResizes` (`documen4.cxx`), if a pending resize is processed for a master whose current result has **no matrix**, the resize is skipped. If a previously spilled formula becomes a scalar, the old reference cells would linger.
* **The Fix:** Extend `ProcessPendingMatrixResizes`: when a dynamic-array master cell has no matrix (or a 1×1 result), its declared size is greater than 1×1, and threaded group calculations are not active:
  * Call `ResizeMatrixFormula(pos, 1, 1)` to clear the old reference cells and restore the master to a 1×1 footprint.
  * Ensure the deferred retry path in `ProcessPendingMatrixResizes` also handles collapsing empty/scalar masters.

### 7.3 Interim State Handling (`#BUSY!`)
While an asynchronous calculation is in flight, the cell temporarily holds the string `"#BUSY!"`.
* The engine must **not** collapse an existing expanded spill when recalculating into `"#BUSY!"`.
* If a cell is already spilled from a previous run, entering `"#BUSY!"` during a subsequent recalc must preserve the existing spill boundaries until the final result arrives, preventing visual flashing and unnecessary cell recreation.

### 7.4 Boundaries: What is Intentionally Left Untouched
To keep the engine change focused and robust:
* **No IDL Changes:** Do not alter `XPythonComputeFunctions.idl`. The return type remains `any` to support `XVolatileResult`.
* **No Dynamic Array Function Flag:** Do not add `PY` to `FormulaTokenArray::HasDynamicArrayFunction`. That flag is reserved for built-in Excel-style functions (`UNIQUE`, `FILTER`) and is not checked by the stack walk.
* **No Changes to CSE Handling:** Legacy Ctrl+Shift+Enter (`FID_INPUTLINE_MATRIX`) already sets `cMatrixFlag = ScMatrixMode::Formula` directly; its behavior is preserved.
* **No Formula Group Spill:** Formula groups (`mxGroup`) explicitly refuse dynamic-array promotion. Filled-down `=PY(...)` cells in a column remain individual scalar formulas.
* **No Custom LOKit Invalidation Layer:** `ResizeMatrixFormula` broadcasts standard `ScPaintHint` objects, which LibreOfficeKit already converts to tile updates.

---

## 8. Document Persistence (ODS and XLSX)

Calc’s native dynamic-array persistence handles dynamic masters automatically:

* **ODS Format (`xmlexprt.cxx` / `xmlcelli.cxx`):**
  * An expanded dynamic master is saved as a matrix cell with attributes `table:number-matrix-columns-spanned` and `rows-spanned`, alongside the extended attribute `coext:spill="true"`.
  * A master currently in `#SPILL!` status is saved as a non-array formula with `coext:spill="true"`. On reload, `xmlcelli.cxx` calls `SetDynamicArrayMaster(true)` and `MarkFormulaSpilled()`.
* **XLSX Format (`formulabuffer.cxx` / `xetable.cxx`):**
  * Import recognizes dynamic array cells and calls `SetDynamicArrayMaster(true)`.
  * Export checks `IsDynamicArrayMaster()` and writes `cm="1"` in the cell record, generating the corresponding dynamic-array entry in `xl/metadata.xml`.

Because our change sets `mbDynamicArrayMaster = true` directly on the formula cell, all existing ODS and XLSX save/load logic functions without modification.

---

## 9. Verification & Test Plan

All tests will be implemented as CppUnit tests in the Collabora engine tree (`sc/qa/unit/` and `scaddins/qa/`).

### Test Harness Operation
Tests interact with the AddIn without requiring an external HTTP service:
1. In the test fixture, call `pythoncompute_set_emitter(nullptr, nullptr)`.
2. Enter the formula via `SetAutoDynamicArrayEligible(true)`.
3. Complete the volatile result synchronously via `pythoncompute_complete_json`.
4. Trigger interpretation via `cell->Interpret()` or `doc->CalcFormulaTree(false)`.

### Test Matrix

| Test Case | Inputs / Condition | Expected Behavior |
| --- | --- | --- |
| **Basic 2×2 Spill** | `=PY(...)` returning `[[1, 2], [3, 4]]` | Cell becomes dynamic master; allocates 2×2 rectangle; neighbor cells have `ScMatrixMode::Reference`. |
| **Spill Collision** | Same 2×2 result, but cell `B2` contains a number | Formula cell displays `FormulaError::Spill` (`#SPILL!`); declared size collapses to 1×1; blocker cell unchanged. |
| **Collision Resolution** | Clear cell `B2` and re-evaluate | Spill expands to full 2×2 rectangle. |
| **1D List Spill** | `=PY(...)` returning `[1, 2, 3]` | Spills across 1×3 row `A1:C1` (matching current `anyjson.cxx` shape). |
| **Scalar Collapse** | Re-run formula returning scalar `42` after a 2×2 spill | Reference cells removed; declared size returns to 1×1; cell displays `42`. |
| **`#BUSY!` Stability** | Background recalc in flight while 2×2 spill active | Spill rectangle remains intact while holding `"#BUSY!"`. |
| **Explicit Intersection** | `=@PY(...)` returning a 2×2 matrix | Evaluates to top-left scalar only; not promoted to dynamic master. |
| **CSE Array Entry** | Ctrl+Shift+Enter over pre-selected `A1:B2` | Static matrix formula created; no dynamic resize. |
| **Enclosing Reduction** | `=SUM(PY(...))` returning a 2×2 matrix | Formula evaluates to a scalar sum; master cell is not dynamic. |
| **ODS Roundtrip** | Save and reopen document with spilled `=PY` | Reloads with `coext:spill="true"`; dynamic master state intact. |
| **XLSX Roundtrip** | Save and reopen document with spilled `=PY` | Writes `cm="1"`; reloads with dynamic array master flag set. |

---

## 10. Out of Scope & Future Work

The following items are deliberately separated from this engine spill implementation:
1. **1D List Column vs. Row Alignment (`anyjson.cxx`):**
   * *Status:* Follow-up task in `scaddins/source/pythoncompute/anyjson.cxx`.
   * *Scope:* Update `promoteFlatNumeric` and `elemsToAny` so that 1D JSON lists serialize into an $N \times 1$ sequence of sequences (column vector) instead of a $1 \times N$ row vector. This will achieve parity with Microsoft Excel `=PY()` and LibrePy.
2. **DataFrame Headers:** LibrePy handles DataFrame header row formatting in `result_to_calc_grid`. Collabora Online passes the raw JSON grid provided by the compute container.
3. **In-Cell Short Python Error Messages (F6):** Formatting exceptions as human-readable cell strings is tracked under checklist item F6.
4. **Cell Images (F3):** Image insertion from Python objects is handled in a separate rendering path.
5. **Immediate Recalc of `ONLOAD_LENIENT`:** Off-screen cells evaluate on paint or during explicit full-document recalculation; this volatile behavior is preserved.

---

## 11. Code Reference Index

| Component | Responsibility | Source Location |
| --- | --- | --- |
| **Array Intent Walk** | Token stack walk inspecting array intent | [`sc/source/core/data/formulacell.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx) `intendsArrayResultInRange` |
| **Dynamic Promotion Gate** | Promotes eligible cell to dynamic master | [`sc/source/core/data/formulacell.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx) `InterpretTail` |
| **Scalar Clipping** | Discards matrix for non-matrix formulas | [`sc/source/core/data/formulacell.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/formulacell.cxx) ~2577–2582 |
| **Spill & Resize Engine** | Collision checks, resizing, and queue drain | [`sc/source/core/data/documen4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen4.cxx) `IsMatrixSpillBlocked`, `ResizeMatrixFormula`, `ProcessPendingMatrixResizes` |
| **UI Entry Eligibility** | Sets `bAutoDynamicArray` on cell entry | [`sc/source/ui/view/viewfunc.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/viewfunc.cxx) `applyFormulaToCell`, [`cellsh3.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/ui/view/cellsh3.cxx) `SID_ENTER_STRING` |
| **Volatile AddIn Bridge** | Asynchronous result listener and notify | [`sc/source/core/tool/addinlis.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/addinlis.cxx) `ScAddInListener::modified` |
| **AddIn Matrix Push** | Converts UNO sequences to `ScMatrix` | [`sc/source/core/tool/addincol.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/tool/addincol.cxx) `SetResult` |
| **JSON Deserialization** | Converts JSON envelope to UNO Any / Sequence | [`scaddins/source/pythoncompute/anyjson.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/anyjson.cxx) `promoteFlatNumeric`, `elemsToAny` |
| **LibrePy Column Spill** | Desktop extension spill grid normalization | [`plugin/calc/python/function.py`](file:///home/keithcu/Desktop/Python/writeragent/plugin/calc/python/function.py) `_result_as_spill_grid` |
