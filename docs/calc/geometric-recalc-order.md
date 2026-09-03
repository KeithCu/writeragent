# Geometric Recalc Order — implementation plan

**Status:** Decided — ready to implement. **Not implemented.** Closed calls in [§9](#9-decisions) stand (no IDL, no 1×1 value-shape strip, no `locate_formula_cell_in_doc` for eval identity, precedent-only, cap skip-sheet, no strip-on-disable, Isolated checkbox visible / no-op). **Eval identity was not closed** in the previous draft — this revision specifies it as **unanimous-ours** plus an off-main `workbook_key` ([§9.5](#95-marker-is-the-udprop--in-memory-map)).

**Related:** [Enabling NumPy & Python](../enabling_numpy_in_libreoffice.md) (session modes, auto-spill), [Microsoft `=PY` design stance](../scripting/ms-py-compatibility.md) (why we refuse Excel co-volatility), [Calc `=PY()` data shapes](py-data-shapes.md) (`data` / `ranges` arity).

---

## Executive summary

Shared-kernel `=PY()` already persists one Python namespace per workbook, but Calc may evaluate those cells in **any order**. Authors today must pass the upstream cell as a `data` argument so the DAG runs precedents first. That is correct and cheap — and easy to forget.

**Geometric Recalc Order** is an opt-in Settings → Python flag. When on, LibrePy treats the sheet’s `=PY()` cells as a **list in sheet order** (row then column — the same order the Python sidebar already uses) and **auto-attaches only the previous list entry** as an extra formula field. Calc then runs A before B because B’s formula literally names A. Partial recalc stays intact: edit A, only A and the chain after it dirty.

This is **not** Excel co-volatility (re-run every Python cell when any one is dirty). It is the existing `data`-as-dependency-edge idea, applied automatically to one predecessor.

**Hard part:** inserting a new `=PY()` cell in the middle of the list. The successor’s predecessor field must be rewritten to the new cell. Those writes **must happen outside recalc**, using the same deferred, undo-isolated pattern as auto-spill (`perform_deferred_spill` + 0.1s timer). Writing other cells from inside the add-in re-enters the formula engine.

**Marker (required):** a workbook UDProp plus an in-memory map, same pattern as `WriterAgentSpillRegistry` / `SPILL_REGISTRY` / `load_spill_registry_for_doc` in [`function.py`](../../plugin/calc/python/function.py). Eval-time strip consults that map. A 1×1 / “last arg is a PY cell” heuristic is **unimplementable** — `execute_python_addin` / `split_python_addin_data_args` / `calc_addin_args_from_split` see only values, never addresses.

**Difficulty:** medium for someone who already knows the spill / formula-edit path — on the order of **one careful week plus about a day** for the UDProp / in-memory map (the original happy-path week did not budget a marker). The risk is semantic (`data` arity, insert/delete, undo), not “can we write cells after recalc.”

---

## 1. Why this exists

### The gap users hit

| Mode | Persistence | Order |
|------|-------------|--------|
| Isolated (default) | Fresh namespace per cell | Irrelevant for Python globals |
| Shared kernel today | One workbook namespace | **Only** via explicit `data` refs (or luck) |
| Excel `=PY` | One workbook namespace | Row-major + **re-run all PY cells** (co-volatility) |

A typical Shared-kernel pipeline is a **vertical list**:

```text
A1  =PY("df = load()")
A2  =PY("df = clean(df)")
A3  =PY("result = df.describe()")
```

Without `data` edges, Calc may run A3 before A1. The current docs tell authors to write `=PY("…"; A1)` on A2. Geometric Recalc Order does that attach automatically.

### What we will not do

Do **not** implement Excel co-volatility. That needs a workbook-global PY barrier in `sc/`, flip-flop with non-PY formulas, and N Python executions per keystroke. [ms-py-compatibility §5.2](../scripting/ms-py-compatibility.md#52-co-volatility-a-second-calculation-mode) already rejected it. Geometric order reuses Calc’s DAG: one extra precedent per cell, dirty subgraph only.

Do **not** add a dedicated IDL ordering argument. That rebuilds `.rdb`s for both OXTs and adds a Collabora/Excel arity case. Collabora Gerrit is in review with Tomaž; do not pile another IDL change on that. Precedent-only strip of a trailing A1 field is enough ([§9.1](#91-precedent-only-not-value-in-data-not-idl)).

---

## 2. Product definition

**Flag name (UI):** Geometric Recalc Order  
**Config key (proposed):** `scripting.python_geometric_recalc_order`  
**Type:** bool, default **false**  
**Surface:** Settings → Python, next to session mode / auto-spill (`plugin/scripting/module.yaml`). Same checkbox path as `python_auto_spill`. LibrePy **and** WriterAgent.

**When on:**

1. Discover `=PY()` / `=PYTHON()` cells (reuse [`cell_discovery.py`](../../plugin/calc/python/cell_discovery.py) — already sorted **row then column**).
2. For each cell after the first in that list, ensure the formula’s trailing fields include **exactly one geometric predecessor**: the previous list entry’s address. Record the attach in the UDProp / in-memory map ([§4](#4-data-binding--do-not-shadow-data)).
3. Leave user-authored ranges alone (see [§4](#4-data-binding--do-not-shadow-data)).
4. On insert / delete / move that changes who “previous” is, **rewrite** the affected successor formulas — **deferred**, not during add-in evaluation.

**When off:** no attach, no rewrite. Existing user-written `data` args stay. Geometric refs already attached **stay** (they are valid DAG edges). Do **not** implement strip-on-disable in this feature — the marker exists, but leaving refs is the cheaper correct default ([§9.4](#94-flag-turned-off-leave-refs)).

**Most valuable with Shared kernel.** Isolated cells do not share names, so order-only precedents do nothing useful for Python globals. Isolated + this flag is a **no-op** for Python semantics (the checkbox stays visible; helper text says it is used with Shared kernel). Do not hide the checkbox when Isolated is selected.

---

## 3. Mechanism (senior-dev view)

### 3.1 The list

`list_python_cells_on_sheet` already returns `PythonCellInfo` sorted by `(row, column)`. That **is** the geometric list.

**List (decided):** all PY cells on **each sheet**, row-major, each sheet chained **independently**. Flag-on / document-open reconcile every sheet (`list_python_cells_in_doc(..., active_sheet_only=False)`). Insert/delete repair only the **modified** sheet. Cross-sheet predecessors are out of scope (sheet-qualified refs + sheet insert/rename). Workbook-global order (Sheet1 then Sheet2) is a later option, not required to prove the idea.

**Cross-cluster chaining (decided):** two independent PY clusters on one sheet (A1:A5 and D1:D5) become one chain — D1 waits on A5. That slightly over-dirties the D column when A3 changes. Correctness is fine; users who care can turn the flag off and write explicit `data` refs. Do not add spatial clustering.

**Cap (decided):** `list_python_cells_on_sheet` stops at `_MAX_PYTHON_CELLS_FOUND = 100` (also `_MAX_CELLS_TO_SCAN = 50000`) and returns a list with **no truncated flag** (`cell_discovery.py`). **If a cap is hit, skip geometric chaining for that entire sheet and log it.** Do not chain the first 100 and leave #101 with no predecessor. Do not raise the cap. Do not mark any eval-index triple strip-safe for that sheet — you cannot prove unanimous-ours on a truncated list ([§9.5](#95-marker-is-the-udprop--in-memory-map)). Phase 1 treats `len(found) >= _MAX_PYTHON_CELLS_FOUND` as cap-hit (over-skips an exact 100). A real `truncated` flag is **Phase 3**, not Phase 1. If the 50k scan cap fires with fewer than 100 PY cells, that list is also incomplete — same skip.

A 100-cell chain is serial (venv IPC per dirty cell); that is the price of order, not a new cliff.

### 3.2 Auto-attach is a formula field, not a Python parse

Calc only orders cells that **name** each other in the formula. We do **not** parse Python for `df = …`. We rewrite:

```text
A2:  =PY("df = clean(df)")          →  =PY("df = clean(df)"; A1)
A3:  =PY("result = df.describe()")   →  =PY("result = df.describe()"; A2)
```

Reuse [`parse_python_formula`](../../plugin/calc/python/formula_edit.py) / `parse_data_binding_text` / `build_data_suffix`. Quoted-code cells: `rebuild_python_formula_with_data`. Code-in-cell (`=PY($A$1; B1:B10)`): detect with `py_formula_has_unquoted_code_ref` / `py_code_arg_is_cell_ref` and rebuild with `rebuild_python_formula_with_code_ref` — **not** `rebuild_python_formula_with_data` (that quotes the code-ref as a string). `PythonFormulaParts` has no quoted flag (`prefix` / `code` / `data_suffix` only) — splice code-in-cell from the **raw formula**, not `parts.code` alone. Eval-index `code` is the **resolved source** (contents of `$A$1`), not the token `$A$1` ([§9.5](#95-marker-is-the-udprop--in-memory-map)). Do not invent a second formula serializer.

The first cell in the list gets **no** predecessor. Cycles cannot appear if we only ever attach the previous entry in a total order. If a first cell still has a trailing geometric field (successor became first after delete), run the **remove-field** primitive ([§9.5](#95-marker-is-the-udprop--in-memory-map)).

### 3.3 Why “just the previous” is enough

A chain A1→A2→A3→A4 is enough for Calc: dirty A2 recalculates A2, then A3, then A4. We do **not** attach A1 onto every later cell. One field, one rewrite on insert.

### 3.4 Insert / delete / move — the only reason this is not a one-liner

Calc will shift A1-style refs when rows move, but it will **not** retarget “previous PY cell” when a **new PY formula** appears between two existing ones.

Example: list is A1, A3. A3 has `;A1`. User inserts a PY cell at A2.

| Cell | Before | After repair |
|------|--------|----------------|
| A1 | `=PY("…")` | unchanged |
| A2 | `=PY("…")` (new) | `=PY("…"; A1)` + map record |
| A3 | `=PY("…"; A1)` | `=PY("…"; A2)` + map record updated |

Delete A2: A3’s predecessor must become A1 again, or **remove-field** if A3 is now first.

Row insert that only **moves** existing PY cells: Calc’s own reference adjust may already be correct. The deferred pass should be **idempotent**: recompute desired predecessor per cell, rewrite only when the geometric field differs.

### 3.5 Writes must be outside recalc (same as auto-spill)

`=PY()` evaluation is a **synchronous add-in** in Calc’s recalc. Invariants already in the tree:

- Do not mutate other cells from `execute_python_addin` / `finalize_python_return`.
- Do not `processEventsToIdle` during recalc (re-enters the engine → `#VALUE!`).
- Auto-spill already defers neighbor writes: collision check sync, then `threading.Timer(0.1)` → `perform_deferred_spill` on the **UI thread**, inside `_undo_lock`.

Geometric rewrites use that same shape:

1. **Detect** (shared modify/save trigger, Monaco/formula save, flag toggle) that the geometric list changed.
2. **Compute** a small patch: cells whose predecessor field is wrong.
3. **Schedule** a deferred UI-thread job (reuse the 0.1s timer / drain pattern; do not start a raw thread — `run_in_background` + main-thread apply, or the existing Timer-on-main pattern in `function.py`).
4. **Apply** `setFormula` under `_undo_lock`. `_undo_lock` calls `enterHiddenUndoContext` only when `um.isUndoPossible()`; otherwise `um.lock()`. User edits hide under the existing undo action. Flag-on / document-open reconcile with no prior edit is **one locked unit**, not a hidden-under-nothing no-op.
5. **Guard** like spill: same doc URL / lifecycle key; skip if the origin formula is no longer what we expected. **Re-entrancy:** an explicit flag so `setFormula` → `modified` cannot run repair inside repair. A rewrite pass that finds nothing to do is a no-op.

Yellow recalc / off-main formula groups: same contract as spill and session lookup — **no UNO desktop/document queries from a recalc worker**. Discovery + rewrite only on the UI thread after the pass. Eval-time strip reads the already-loaded in-memory map only.

### 3.6 When to run the repair pass

| Trigger | Why |
|---------|-----|
| Flag turned **on** | One-shot attach for **all sheets**, each chained independently |
| Flag turned **off** | Stop maintaining refs; **leave** existing geometric fields; leave the map (do not strip-on-disable) |
| Monaco / native **Save** of a PY cell | Primary attach path — save is already outside recalc (`editor.py` `_apply_formula_save` / native Save). New or edited formula may need a predecessor; neighbors may need retarget. Do **not** collapse everything onto modify-only. |
| Sheet `XModifyListener` (shared trigger) | Insert/delete/clear. **Do not** add a sibling `CalcGeometricModifyListener`. `CalcSpillModifyListener.modified` walks `SPILL_REGISTRY` for that sheet only — it does **not** scan formula cells, so geometric repair cannot piggyback on that walk. Share the **trigger / debounce** (0.1s timer, drain, UI thread, `_undo_lock`) and the re-entrancy flag. Geometric repair then runs its own `list_python_cells_on_sheet`. Factor a one-sheet dispatcher if a second `addModifyListener` is undesirable; do not merge the two jobs into one class. |
| Document open | If flag on, load the UDProp into the in-memory map (like `load_spill_registry_for_doc`) and reconcile once so files authored with the flag stay consistent |

Do **not** rewrite from inside the add-in just because this cell is evaluating.

---

## 4. Data binding — do not shadow `data`

**Highest-risk implementation detail.** Decided: **precedent-only**, strip via the UDProp / in-memory map ([§9.1](#91-precedent-only-not-value-in-data-not-idl), [§9.5](#95-marker-is-the-udprop--in-memory-map)).

Today ([data shapes](py-data-shapes.md)):

- One trailing arg → Python `data` is that `CalcRange`.
- Two or more → `data` is the **list** (same as `ranges`).

`calc_addin_args_from_split` in [`calc_addin_data.py`](../../plugin/calc/calc_addin_data.py) is the flip: `len(args) == 1` returns one 2D grid; `len(args) >= 2` returns a **list** of grids. If A2 is `=PY("np.mean(data)"; B1:B10)` and we append `;A1`, then `data` suddenly becomes a list and `np.mean(data)` breaks. That is the common case, not a corner.

**Contract:** the geometric predecessor is a **Calc-only ordering token**. The add-in **strips it** before packing worker `data` / `ranges`. User-authored args keep today’s arity. Isolated and Shared both see the same `data` they wrote.

### 4.1 Why a value-shape strip cannot work

`PythonFunction.python` / `execute_python_addin` receive `(code, data)` values only (`addin_impl.py`). `split_python_addin_data_args` and `calc_addin_args_from_split` never see addresses or “this arg is a PY cell.”

A rule like “if the last arg is a 1×1 PY cell, drop it” is therefore unimplementable. `locate_formula_cell_in_doc` (`function.py` `session_key`) is **not** a fix: it returns `None` on 0 or 2+ matches, and cannot disambiguate `=PY("f(data)"; A1)` when A1 is both predecessor and user data.

The last-row idea “user already passed the previous PY cell as real data → no-op, satisfied either way” is **wrong** under a shape-strip: it would drop real user data. Under the map, that row is: **do not record as ours, do not strip** ([§9.5](#95-marker-is-the-udprop--in-memory-map) table).

### 4.2 Where to strip (must be before the index heuristic)

In `_execute_python_addin_impl` ([`function.py`](../../plugin/calc/python/function.py)):

1. `args = split_python_addin_data_args(data)`
2. **Strip here** if the eval index marks this `(workbook_key, resolved_code, n_args)` **strip-safe** (unanimous-ours — [§9.5](#95-marker-is-the-udprop--in-memory-map)). Unconditional across **both** branches — including `_code_uses_indexed_multi_data` (`"data["` / `"ranges["` in the source). If the geometric field stays, it becomes `data[-1]` / `ranges[-1]`.
3. Then `py_data = calc_addin_args_from_split(...)` and the existing trailing-single-cell **matrix-index** heuristic (the `is_multi and not _code_uses_indexed_multi_data(code)` block that peels a last 1-cell arg as `index_arg`, after which `finalize_python_return` slices `flat[value]`).

If strip is skipped on a fill-down of identical `=PY("np.mean(data)"; B1:B10; pred)`, `calc_addin_args_from_split` flips `data` to a list, then the index heuristic peels the predecessor **value** as `index_arg` — silent wrong numbers. Strip must run first, and fill-down must be strip-safe when every cell with that triple is ours. Phase 4 must test both `=PY("np.mean(data)"; B1:B10)` and `=PY("ranges[-1].shape"; B1:B10)`, plus fill-down and mixed neighbors.

Do not invent a reserved formula suffix. Do not add a third IDL argument.

---

## 5. User-visible behavior

**What the user sees:** formulas gain a trailing cell ref they did not type. That is the feature (Calc must see it). Document it in Settings helper text and the hub session-modes page when this ships.

**What they should not see:** extra undo steps **when an undo action already exists** (hidden under the user edit); `#REF!` storms after insert; `data` breaking on cells that already pass ranges; a full-sheet PY re-run after one edit. Flag-on / open reconcile with an empty undo stack may appear as **one locked unit** — that is accepted, not a second “rewrite A3” step on top of a user edit.

**LibrePy sidebar:** the existing cell list is already geometric. A later UX nicety (not MVP) is a small “depends on A1” hint. Do not block the flag on sidebar chrome.

**Excel import:** the OOXML rewriter must **not** invent geometric edges ([ms-py already says this](../scripting/ms-py-compatibility.md)). If the user turns the flag on after import, the deferred pass attaches them. Export **leaves** geometric-only args as extra `_xlws.PY` deps (they are valid precedents). Do not special-case strip on export for MVP.

---

## 6. Difficulty and reuse

| Piece | New? | Reuse |
|-------|------|--------|
| Settings checkbox | Small | `module.yaml` + existing Settings dialog |
| Discover PY cells in order | Phase 1: `len >= 100` | `list_python_cells_on_sheet` / `list_python_cells_in_doc` — no truncated flag today; a real flag is Phase 3, do not raise the cap |
| Parse / rebuild `=PY(code; args)` | Small splice + remove-field | `formula_edit.py` — `rebuild_python_formula_with_data` **or** `rebuild_python_formula_with_code_ref` |
| Marker | ~1 extra day | Copy `WriterAgentSpillRegistry` / `load_spill_registry_for_doc` / `SPILL_REGISTRY` (`udprops`). Record `workbook_key` even in Isolated. Unanimous-ours eval index — not uniqueness, not ≥1-hit. |
| Deferred UI-thread writes + undo | Small | `perform_deferred_spill`, `_undo_lock`, Timer 0.1s — share debounce; explicit re-entrancy flag |
| Sheet modify | Small | **Shared trigger**, not a sibling listener; geometric job does its own PY discovery |
| Strip geometric arg from worker `data` | Medium | `_execute_python_addin_impl` — map lookup **before** the index heuristic and **before** `calc_addin_args_from_split` |
| Insert-in-middle repair | Medium | Pure list-diff + `setFormula` |
| Tests | Required | pytest on list-diff + formula splice + map/strip; UNO for insert-row + deferred rewrite |

**Not required:** LibreOffice core patches, co-volatility, IDL change, venv protocol change, chat tools, strip-on-disable, raising the 100-cell cap.

**Rough effort:** 3–5 days for the happy path (flag + attach + deferred repair on one sheet) **plus about a day** for the UDProp / in-memory map; another 2–3 days for insert/delete/undo/flag-toggle edges and tests.

Compare to **full Excel co-volatility:** multiple engineer-months in `sc/`, high regression risk. This flag is the cheap 80%.

---

## 7. Risks

| Risk | Mitigation |
|------|------------|
| Shadowing `data` (arity flip) | Precedent-only strip via the map, **before** the index heuristic ([§4](#4-data-binding--do-not-shadow-data)) |
| 1×1 / “is a PY cell” strip | Forbidden — add-in sees values only ([§4.1](#41-why-a-value-shape-strip-cannot-work)) |
| Index heuristic eats `;A1` | Strip first; Phase 4 tests `np.mean(data)`, `ranges[-1].shape`, fill-down |
| Uniqueness / “fail-safe = no strip” | **Rejected** — kills fill-down of identical `=PY("np.mean(data)"; B1:B10)` |
| ≥1-hit strip | **Rejected** — would strip a mixed matrix-index neighbor |
| Mixed ours + user same triple | Do not mark strip-safe; residual is “chain loses strip,” not “user cell loses last arg” |
| Two open workbooks | `off_main_calc_session_is_unambiguous()` false → no strip |
| Isolated still needs strip | Isolated never enters `workbook_session_id`; UI load/repair must `record_active_calc_session("calc:" + _workbook_session_key)` so the unambiguous check can pass |
| Keying the token `$A$1` | Eval `code` is resolved source (`execute_python_addin`); repair must read the code cell |
| Naive `;` arity | Repair `n_args` must match `split_python_addin_data_args`, not a semicolon count |
| Rewrite during recalc | Same ban as spill; deferred only |
| Undo fragmentation | `_undo_lock`: hidden when `isUndoPossible()`, else `lock()`; flag-on reconcile is one locked unit |
| Infinite rewrite loop | Idempotent desired-vs-actual; re-entrancy flag; skip if already correct |
| Calc already adjusted refs on row insert | Repair pass compares desired predecessor, does not blindly rewrite |
| User already passed the previous cell as **data** | Do not record as ours; do not strip ([§9.5](#95-marker-is-the-udprop--in-memory-map)) |
| User passed a **different** single-cell last arg (real data) | Append, do not replace, unless the map says that last arg is **our** stale predecessor |
| Two independent PY clusters on one sheet | One row-major chain; slightly over-dirties the later cluster |
| Circular refs from user forward-refs | Calc reports circular; we never attach a later cell |
| 100-cell discovery cap | Skip the **whole** sheet; never chain a partial list |
| Shared + Isolated confusion | Checkbox always visible; helper: “Used with Shared kernel”; Isolated is a no-op |
| Collabora Online | Desktop LibrePy first. Online has no deferred UNO spill-style writes in the same way; do not promise this flag in jail-safe compute until desktop is boring |

---

## 8. Suggested phases

**Phase 0 — Review (this doc).** Closed product calls stand. Eval identity is specified in [§9.5](#95-marker-is-the-udprop--in-memory-map) (unanimous-ours + `workbook_key`) — do not treat the previous uniqueness draft as closed. Do not reopen [§9.1](#91-precedent-only-not-value-in-data-not-idl) C (IDL) or a value-shape strip.

**Phase 1 — Pure list + formula splice.** Unit tests only: given a list of addresses + current formulas + the in-memory record, compute the patch and the eval-index bools. No UNO. Encode the [§9.5](#95-marker-is-the-udprop--in-memory-map) table, including **remove-field**, code-in-cell splice from the raw formula (`rebuild_python_formula_with_code_ref`), fill-down unanimous-ours, mixed poison, and “`len >= 100` → skip sheet, do not mark strip-safe.” No truncated-flag API in this phase.

**Phase 2 — Flag + attach on save / flag-on.** Monaco and native cell save call the splicer; apply on the UI thread after save (save is already outside recalc). Settings default off. Flag-on walks **all sheets**. Persist / load the UDProp like spill. Isolated UI load/repair must `record_active_calc_session` with `calc:` + `_workbook_session_key` (same string eval reads; never `""`).

**Phase 3 — Deferred repair on insert/delete.** Shared trigger + spill-like timer + re-entrancy flag. UNO tests: three-cell column, insert PY in the middle, successor’s field updates; delete (including successor-becomes-first → remove-field); undo. Cap-hit sheet is left unchained. A real discovery `truncated` flag belongs here if needed — not Phase 1.

**Phase 4 — Strip geometric arg from worker ingress.** After `split_python_addin_data_args`, if the triple is strip-safe, drop `args[-1]` **before** the index heuristic and `calc_addin_args_from_split`. Tests in [§10](#10-test-plan-when-implemented).

**Non-goals until someone asks:** cross-sheet chains, workbook-global order, Isolated value-piping, sidebar annotations, Excel export special-case, raising the 100-cell cap, strip-on-disable, spatial clustering of independent PY groups, a dedicated IDL arg.

---

## 9. Decisions

Closed product calls stand. Eval identity is specified in [§9.5](#95-marker-is-the-udprop--in-memory-map) (was not closed by the uniqueness draft). Rejected alternatives are listed so they are not reopened.

### 9.1 Precedent-only (not value-in-`data`, not IDL)

**Decision: A — Precedent-only.** The geometric arg is a Calc DAG token. Strip it before packing worker `data` / `ranges`. Do not inject the previous cell’s value.

**Rejected:**

- **B — Value-in-`data`.** Inject the previous cell’s value into `data` / `data[-1]`. Breaks `np.mean(data)` on every cell that already passes one range.
- **C — Third IDL parameter.** Rebuilds `.rdb`s for both OXTs; Collabora/Excel import get another arity case. Collabora Gerrit is in review; do not pile another IDL change. Do not reopen C in this feature.

A trailing A1 field is enough **if** we strip it via the map, not a 1×1 heuristic.

### 9.2 The list is all PY cells on the sheet, row-major

**Decision: A.** One chain per sheet. Independent clusters become one chain. Matches `list_python_cells_on_sheet` and the sidebar.

**Rejected:** contiguous-column-only (surprises authors who put the next step in C1); spatial clustering; workbook-global order. Those can wait.

### 9.3 Isolated mode — checkbox visible, no-op

**Decision: A.** Always visible; Isolated is a no-op. Helper: “Ensures PY cells evaluate in sheet order. Most useful with Shared kernel.”

**Rejected:** hide the checkbox when Isolated is selected (couples two settings; looks like a bug when the box disappears).

Precedent-only strip means Isolated `data` is unchanged. Isolated is a no-op for **Python globals**, not for the strip: `workbook_session_id` returns `None` when mode ≠ `shared`, but Isolated still needs strip (else arity breaks). Isolated UI load/repair must `record_active_calc_session` with `calc:` + `_workbook_session_key` so yellow eval can see one recorded id ([§9.5](#95-marker-is-the-udprop--in-memory-map)).

### 9.4 Flag turned off — leave refs

**Decision: A.** Stop attaching and stop repairing. The refs stay as valid DAG edges. After a precedent-only strip they do not change Python behavior.

**Rejected for this feature: B — strip-on-disable.** The marker now exists ([§9.5](#95-marker-is-the-udprop--in-memory-map)), so B is implementable later with the same remove-field primitive. Do not build it here.

### 9.5 Marker is the UDProp / in-memory map

**Decision: C (required), used to implement A’s rewrite table.** Not a reserved suffix. Not IDL. Not a 1×1 / “last arg is a PY cell” heuristic. **Eval identity was not closed** by the uniqueness draft — it is **unanimous-ours** plus `workbook_key`, below.

Copy the spill pattern — do not invent a second subsystem:

| Spill (exists) | Geometric (this feature) |
|----------------|--------------------------|
| UDProp `WriterAgentSpillRegistry` | A sibling document property (e.g. `WriterAgentGeometricRegistry`) |
| `SPILL_REGISTRY` in memory | In-memory map, loaded on the UI thread |
| `load_spill_registry_for_doc` / `save_spill_registry_for_doc` | Same load/save shape via `udprops` |
| Keyed by `(doc_url, sheet, row, col)` | Per-cell attach record (addresses on the UI thread) **plus** `workbook_key` even in Isolated |

**Rewrite** (UI thread) always has addresses. The map is the ours-vs-user marker: append / replace / remove using the table below.

**Eval-time strip** cannot look up `(sheet, row, col)`. `PythonFunction.python` / `execute_python_addin` never get a calling address (`addin_impl.py`). Do **not** call `locate_formula_cell_in_doc` (None on 0 or 2+ matches; cannot tell user-data-is-previous-PY from our field). Do **not** query the desktop / document from a recalc worker. Off-main, `doc` stays `None` (`_execute_python_addin_impl` fills `doc` only on main).

#### Eval index — unanimous-ours (not uniqueness, not ≥1-hit)

At **repair time** (UI thread, we have addresses), compute an eval-index bool per `(workbook_key, resolved_code, n_args)`:

- **strip-safe** iff **every** discovered PY cell with that triple is in the map (ours-only / unanimous).
- **Eval:** if that triple is marked strip-safe, drop `args[-1]` before the index heuristic and before `calc_addin_args_from_split`. Unconditional on both branches including `data[]` / `ranges[]`.
- **Mixed** same-code/arity (a non-mapped user cell, e.g. matrix-index `=PY("f"; range; i)` next to a chain of `=PY("f"; range; pred)`) → do **not** mark strip-safe → **no-strip for the whole triple** (chain included) until the user cell is gone or attached. Residual to name: **mixed poisons the chain**, not “user cell also loses last arg.” Do **not** use a ≥1-hit rule; that would strip the matrix-index neighbor.
- **Cap-hit** → skip the entire sheet, do **not** mark any triple strip-safe, do not write a partial chain. You cannot prove unanimous on a truncated list.
- **Rejected:** uniqueness / “fail-safe = no strip” / “typical pipelines have distinct code strings.” That kills fill-down of identical `=PY("np.mean(data)"; B1:B10)`: after attach every successor has the same `resolved_code` and `n_args=2`, non-unique → no-strip → `calc_addin_args_from_split` flips `data` to a list, then the index heuristic peels the predecessor **value** as `index_arg` — silent wrong numbers.

Optional (not instead of unanimous, not the primary key): a fingerprint of `args[:-1]` so two chains with the same snippet and **different ranges** do not share a triple. Does not save mixed same-range. Do **not** fingerprint the last-arg value (first recalc is empty/0).

#### Three must-gets (easy to get wrong)

**1. Key `code` is what `execute_python_addin` receives, not the formula token.** `PythonFunction.python` passes Calc’s first argument through as `code` (`addin_impl.py`). For `=PY($A$1; B1:B10; pred)` that is the **cell contents of `$A$1`** (resolved source), not the token `$A$1`. Repair must **read that cell** when building the eval index (`formula_edit.py` unquoted branch vs `addin_impl.py`). Keying the token `$A$1` misses every script-bank cell. Detect / splice with `py_formula_has_unquoted_code_ref` / `py_code_arg_is_cell_ref` / `rebuild_python_formula_with_code_ref` (exist on master). `PythonFormulaParts` has no quoted flag (`prefix` / `code` / `data_suffix` only) — splice code-in-cell from the **raw formula**, not `parts.code` alone, or `$A$1` gets quoted by `rebuild_python_formula_with_data`. Cells that share resolved source collide on this triple; same unanimous rule.

**2. `n_args` at eval is `len(split_python_addin_data_args(data))`** (`calc_addin_data.py`). Repair arity **must** match that splitter, not a naive semicolon count. A pair `(range, 1×1 pred)` does **not** collapse under `_is_legacy_single_column_range`: the inner of the 1×1 is a sequence, so two varargs stay two args (`n_args=2` after attach).

**3. Cap-hit:** `list_python_cells_on_sheet` returns 100 with **no** truncated flag (`cell_discovery.py`). You cannot prove unanimous. Already-decided skip-sheet is the fail-safe; do **not** mark those triples strip-safe. Phase 1 treats `len >= _MAX_PYTHON_CELLS_FOUND` as cap-hit (over-skips exact 100). A real truncated flag is Phase 3.

#### `workbook_key` (blocking — do not cite `get_python_init_kwargs`)

`get_python_init_kwargs` does **not** carry `doc_url`. `build_python_eval_init_kwargs` is init-script / hash only. `session_key` leaves `doc_url=""` off-main (fills `doc` only on main). Isolated: `workbook_session_id` returns `None` when mode ≠ `shared` (`session_manager.py`) and **never enters** that function’s `record_active_calc_session` path, so `_RECORDED_CALC_SESSION_IDS` can stay empty and `off_main_calc_session_is_unambiguous()` stays false. Isolated still needs strip off-main (else arity breaks).

**Eval `workbook_key` = `get_cached_calc_session_id()` only when `off_main_calc_session_is_unambiguous()`** (`session_manager.py`: `len(_RECORDED_CALC_SESSION_IDS) == 1`). Else do not strip (two open workbooks, or Isolated that never recorded).

On the **UI-thread** load / repair path, write that key into the geometric map **and** call `record_active_calc_session` with the **same string eval will read**: `calc:` + `_workbook_session_key` (never `""`). Do this **even in Isolated**. Do **not** invent a second Isolated key. Then one open file makes `len(_RECORDED_CALC_SESSION_IDS) == 1` and yellow Isolated can strip. Sibling of `load_spill_registry_for_doc`. Unsaved files must not use empty URL — same #402 hole as `session_key` / `_workbook_session_key` (URL, else a persisted unsaved id, never `""`).

#### Remove-field

Parse → drop the last geometric data arg → rebuild → drop the map record. Needed when a successor becomes first after delete. Same primitive strip-on-disable ([§9.4](#94-flag-turned-off-leave-refs) B) would use later. **Idempotent:** first cell with a trailing geometric field → strip that field. After remove-field, recompute unanimous-ours for the affected triples.

**Rewrite table** (desired predecessor A1 unless noted). “Ours” means the map has a record for this cell.

| Scenario | Last arg | Map | Action |
|----------|----------|-----|--------|
| No args | — | none | Append `;A1`; record |
| User range `B1:B10` | range | none | Append (`;B1:B10;A1`); record |
| Fill-down of identical `=PY("np.mean(data)"; B1:B10)` | range + pred | **all** successors ours | Append each; triple is strip-safe (unanimous) |
| Already correct | single cell = desired | ours, pred A1 | No-op |
| Stale predecessor after insert | single cell = old pred | ours, old pred A1, desired A2 | Replace `;A1` → `;A2`; update record |
| User single-cell data `C5` | single cell ≠ desired | none | Append (`;C5;A1`); do not overwrite `C5` |
| User already passed the previous PY cell as **real data** | single cell = desired | **none** (we did not attach) | **No-op. Do not record.** If this cell shares a triple with mapped cells, the triple is **not** strip-safe (mixed poisons the chain). |
| Mixed matrix-index neighbor `=PY("f"; range; i)` next to `=PY("f"; range; pred)` | 1×1 | one ours, one not | Do **not** mark strip-safe; **neither** strips |
| Successor became first (delete) | geometric field | ours | **Remove-field**; drop record; recompute index |
| First cell still has a leftover field | geometric field | ours | Remove-field (idempotent) |
| Cap hit on this sheet | — | — | Skip the sheet; log; no partial chain; **do not** mark triples strip-safe |

### 9.6 Flag-on / document-open — all sheets

**Decision: A.** All sheets, each chained independently. `list_python_cells_in_doc(..., active_sheet_only=False)` already walks `doc.getSheets()`. Modify-listener repair stays per-sheet.

**Rejected:** active sheet only (other tabs stay inconsistent until visited).

---

## 10. Test plan (when implemented)

**Unit (`tests/calc/python/`, match the new module name; splice cases can extend `test_formula_edit.py`):**

Phase 1 — list-diff + splice + eval-index bools (encode [§9.5](#95-marker-is-the-udprop--in-memory-map)):

- Empty, one cell, two cells, insert in middle, delete middle, delete first (remove-field), reorder.
- Formula splice: no args; existing range args preserved; already-correct predecessor; stale predecessor replaced; user extra cell-ref appended not overwritten when it is not ours.
- **Code-in-cell:** splice `=PY($A$1; B1:B10)` from the **raw formula** via `rebuild_python_formula_with_code_ref`; result stays an unquoted `$A$1`, not `=PY("$A$1"; …)`. Eval-index `code` is the **resolved source** (cell contents of `$A$1`), not the token.
- Repair `n_args` matches `len(split_python_addin_data_args(...))`, not a semicolon count. `(range, 1×1 pred)` stays `n_args=2`.
- Remove-field: first cell with a trailing geometric field → field gone; second call is a no-op.
- Cap: `len >= _MAX_PYTHON_CELLS_FOUND` → skip sheet, no patch, **no strip-safe marks**. No truncated-flag helper in Phase 1.

Phase 4 — `data` strip (inject the in-memory map; no UNO):

- `=PY("np.mean(data)"; B1:B10)` after attach still packs a single `CalcRange` (not a list).
- `=PY("ranges[-1].shape"; B1:B10)` after attach: `ranges[-1]` is `B1:B10`, not the predecessor (indexed multi-data branch).
- Strip runs before the matrix-index peel: last geometric 1-cell must **not** become `index_arg`.
- **Fill-down:** two identical `=PY("np.mean(data)"; B1:B10)` after attach → **both** strip (unanimous-ours, same resolved code, `n_args=2`).
- **Mixed:** matrix-index neighbor `=PY("f"; range; i)` next to a chain of `=PY("f"; range; pred)` → **neither** strips (mixed poisons the triple).
- **Two open workbooks / `off_main_calc_session_is_unambiguous()` false** → no strip.
- **Isolated** UI load/repair calls `record_active_calc_session("calc:" + _workbook_session_key)` (same string eval reads) and strips when unambiguous.
- User 1×1 last arg **not** in the map, and no mixed poison of a chain: no strip of that user cell.
- Never fall back to 1×1, uniqueness, or ≥1-hit.

**UNO (`test_*_uno.py`):**

- Shared kernel, flag on: A3 reads a name assigned in A1 without a user-typed `data` ref; result is stable across F9.
- Insert a PY row between two chained cells; after the deferred pass, successor formula names the new cell; values update on next recalc.
- Delete middle cell: successor retargets or remove-field if it is now first.
- Flag off: no new attaches; existing refs stay.
- Isolated + flag on: no-op for Python **globals**; strip still runs when `workbook_key` is unambiguous (no `data` breakage).
- Undo: user types a new PY cell, geometric rewrite does not add a second undo step when `isUndoPossible()` (hidden context). Flag-on reconcile with no prior edit is one locked unit (`test_calc_spill_undo_lock` is the spill analogue).
- `#SPILL!` / auto-spill still works on a chained origin cell.
- Re-entrancy: repair `setFormula` does not nest a second repair.
- Cap-hit sheet: no chain, log emitted, no strip-safe marks.

Do not run the full suite until this is implemented. Phase 1 is mockable without soffice.

---

## 11. Docs to update when this ships (not now)

- Hub [session modes](../enabling_numpy_in_libreoffice.md#session-modes-and-recalc-semantics): one short subsection + Settings table row.
- [ms-py-compatibility](../scripting/ms-py-compatibility.md): pointer — “opt-in geometric *chain*, still not co-volatility.”
- Settings helper in `module.yaml`.
- This file: flip Status to shipped.

Do not touch `AGENTS.md` unless the rewrite-outside-recalc rule needs to become a global invariant (it is already implied by the spill / `=PY()` contract).
