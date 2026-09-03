# Geometric Recalc Order — implementation plan

**Status:** Implementation in progress. **Phase 1 landed.** **Phase 2 + Phase 4 are in this PR** (Settings flag default off, attach on save / flag-on, UDProp load/save, Isolated `record_active_calc_session`, eval strip before the index heuristic, recommended `args[:-1]` fingerprint). **Phase 3 is still open** (sheet modify-listener / insert-delete deferred repair / truncated-flag API). Closed calls in [§9](#9-decisions) stand (no IDL, no 1×1 value-shape strip, no `locate_formula_cell_in_doc` for eval identity, precedent-only, cap skip-sheet, no strip-on-disable, Isolated checkbox visible / no-op). **Eval identity** is **unanimous-ours** plus an off-main `workbook_key` and a recommended fingerprint of `args[:-1]` ([§9.5](#95-marker-is-the-udprop--in-memory-map)). **Cap-hit UI:** skip the sheet, log, **and** show one message box per skipped sheet (`notify_geometric_cap_hit`) — do not only `log.error`.

**Related:** [Enabling NumPy & Python](../enabling_numpy_in_libreoffice.md) (session modes, auto-spill), [Microsoft `=PY` design stance](../scripting/ms-py-compatibility.md) (why we refuse Excel co-volatility), [Calc `=PY()` data shapes](py-data-shapes.md) (`data` / `ranges` arity).

---

## Executive summary

Shared-kernel `=PY()` already persists one Python namespace per workbook, but Calc may evaluate those cells in **any order**. Authors today must pass the upstream cell as a `data` argument so the DAG runs precedents first. That is correct and cheap — and easy to forget.

**Geometric Recalc Order** is an opt-in Settings → Python flag. When on, LibrePy treats the sheet’s `=PY()` cells as a **list in sheet order** (row then column — the same order the Python sidebar already uses) and **auto-attaches only the previous list entry** as an extra formula field. Calc then runs A before B because B’s formula literally names A. Partial recalc stays intact: edit A, only A and the chain after it dirty.

This is **not** Excel co-volatility (re-run every Python cell when any one is dirty). It is the existing `data`-as-dependency-edge idea, applied automatically to one predecessor.

**Hard part:** inserting a new `=PY()` cell in the middle of the list. The successor’s predecessor field must be rewritten to the new cell. Those writes **must happen outside recalc**, using the same deferred, undo-isolated pattern as auto-spill (`perform_deferred_spill` + 0.1s timer). Writing other cells from inside the add-in re-enters the formula engine.

**Marker (required):** a workbook UDProp plus an in-memory map, same pattern as `WriterAgentSpillRegistry` / `SPILL_REGISTRY` / `load_spill_registry_for_doc` in [`function.py`](../../plugin/calc/python/function.py). Eval-time strip consults that map. A 1×1 / “last arg is a PY cell” heuristic is **unimplementable** — `execute_python_addin` / `split_python_addin_data_args` / `calc_addin_args_from_split` see only values, never addresses.

**Difficulty:** medium for someone who already knows the spill / formula-edit path — on the order of **one careful week plus about a day** for the UDProp / in-memory map (the original happy-path week did not budget a marker). The risk is semantic (`data` arity, insert/delete, undo), not “can we write cells after recalc.”

Collabora / LibreOffice core is a living sketch in [§12](#12-collabora--libreoffice-core-living-sketch-not-final) (pass 2: extra `StartListeningCell`, **not** trailing `;A1`); do not start until Tomaž’s `=PY()` lands.

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

**Cap (decided):** `list_python_cells_on_sheet` stops at `_MAX_PYTHON_CELLS_FOUND = 100` (also `_MAX_CELLS_TO_SCAN = 50000`) and returns a list with **no truncated flag** (`cell_discovery.py`). **If a cap is hit, skip geometric chaining for that entire sheet, log it, and show one user-visible error** (`notify_geometric_cap_hit` → existing `msgbox`, UI thread only, one box per skipped sheet; Online infobar — [§12](#12-collabora--libreoffice-core-living-sketch-not-final)). Do not only `log.error`. Do not chain the first 100 and leave #101 with no predecessor. Do not raise the cap. Do not mark any eval-index triple strip-safe for that sheet — you cannot prove unanimous-ours on a truncated list ([§9.5](#95-marker-is-the-udprop--in-memory-map)). Phase 1 treats `len(found) >= _MAX_PYTHON_CELLS_FOUND` as cap-hit (over-skips an exact 100). A real `truncated` flag is **Phase 3**, not Phase 1. If the 50k scan cap fires with fewer than 100 PY cells, that list is also incomplete — same skip.

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

Yellow recalc / off-main formula groups: same contract as spill and session lookup — **no UNO desktop/document queries from a recalc worker**. Discovery + rewrite only on the UI thread after the pass. Eval-time strip reads the already-loaded in-memory map only. The strip-safe index is written on the UI thread and read from the recalc worker. Do not mutate it in place like `SPILL_REGISTRY` (unlocked). Swap in an immutable snapshot (frozenset). GIL-atomic bind is enough; no UNO, no per-cell lookup. A dedicated lock is optional, not required if you only rebind the name.

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
2. **Strip here** if the eval index marks this `(workbook_key, resolved_code, n_args, fingerprint(args[:-1]))` **strip-safe** (unanimous-ours — [§9.5](#95-marker-is-the-udprop--in-memory-map)). Unconditional across **both** branches — including `_code_uses_indexed_multi_data` (`"data["` / `"ranges["` in the source). If the geometric field stays, it becomes `data[-1]` / `ranges[-1]`.
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
| Isolated still needs strip | Isolated never enters `workbook_session_id`. Init-non-empty already records via `build_python_eval_init_kwargs` → `calc_init_session_id`. Isolated + no init still never records. UI load/repair must `record_active_calc_session("calc:" + _workbook_session_key)` (same string, idempotent) so the no-init case can pass the unambiguous check |
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
| Collabora Online | Living sketch in [§12](#12-collabora--libreoffice-core-living-sketch-not-final) (pass 2: extra listen, **not** trailing `;A1`). Do not start until Tomaž’s `=PY()` lands. Desktop LibrePy first. |

---

## 8. Suggested phases

**Phase 0 — Review (this doc).** Closed product calls stand. Eval identity is specified in [§9.5](#95-marker-is-the-udprop--in-memory-map) (unanimous-ours + `workbook_key`) — do not treat the previous uniqueness draft as closed. Do not reopen [§9.1](#91-precedent-only-not-value-in-data-not-idl) C (IDL) or a value-shape strip.

**Phase 1 — Pure list + formula splice.** **Landed** in `plugin/calc/python/geometric_recalc.py` (`tests/calc/python/test_geometric_recalc.py`). Unit tests only: given a list of addresses + current formulas + the in-memory record, compute the patch and the eval-index bools. No UNO. Encodes the [§9.5](#95-marker-is-the-udprop--in-memory-map) table, including **remove-field**, code-in-cell splice from the raw formula (`rebuild_python_formula_with_code_ref`), fill-down unanimous-ours, mixed poison, and “`len >= 100` → skip sheet, do not mark strip-safe.” Cap-hit also returns a user-visible message; `notify_geometric_cap_hit` shows one `msgbox` per skipped sheet on the UI thread. No truncated-flag API in this phase.

**Phase 2 — Flag + attach on save / flag-on.** **Landed in this PR** (with Phase 4). Monaco and native cell save call the splicer; apply on the UI thread after save (save is already outside recalc). Settings default off. Flag-on walks **all sheets**. Persist / load the UDProp like spill. Isolated UI load/repair must `record_active_calc_session` with `calc:` + `_workbook_session_key` (same string eval reads; never `""`).

**Phase 3 — Deferred repair on insert/delete.** **Still open.** Shared trigger + spill-like timer + re-entrancy flag. UNO tests: three-cell column, insert PY in the middle, successor’s field updates; delete (including successor-becomes-first → remove-field); undo. Cap-hit sheet is left unchained. A real discovery `truncated` flag belongs here if needed — not Phase 1.

**Phase 4 — Strip geometric arg from worker ingress.** **Landed in this PR** (with Phase 2 — attach without strip is the arity footgun). After `split_python_addin_data_args`, if the triple is strip-safe, drop `args[-1]` **before** the index heuristic and `calc_addin_args_from_split`. Key includes the recommended `args[:-1]` fingerprint so two chains that reuse a snippet on different ranges do not share a triple. Tests in [§10](#10-test-plan-when-implemented).

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

Precedent-only strip means Isolated `data` is unchanged. Isolated is a no-op for **Python globals**, not for the strip: `workbook_session_id` returns `None` when mode ≠ `shared`, but Isolated still needs strip (else arity breaks). Isolated does **not** “never enter `record_active_calc_session`”: a non-empty init script records via `build_python_eval_init_kwargs` → `calc_init_session_id` → `calc_workbook_base_session_id`. Isolated + no init still never records. Geometric UI load/repair must `record_active_calc_session("calc:" + _workbook_session_key)` (same string, idempotent) so the no-init case can pass the unambiguous check ([§9.5](#95-marker-is-the-udprop--in-memory-map)).

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

At **repair time** (UI thread, we have addresses), compute an eval-index bool per `(workbook_key, resolved_code, n_args, fingerprint(args[:-1]))`:

- **strip-safe** iff **every** discovered PY cell with that triple is in the map (ours-only / unanimous).
- **Eval:** if that triple is marked strip-safe, drop `args[-1]` before the index heuristic and before `calc_addin_args_from_split`. Unconditional on both branches including `data[]` / `ranges[]`.
- **Mixed** same-code/arity/**user-args** (a non-mapped user cell, e.g. matrix-index `=PY("f"; range; i)` next to a chain of `=PY("f"; range; pred)`) → do **not** mark strip-safe → **no-strip for the whole triple** (chain included) until the user cell is gone or attached. Residual to name: **mixed poisons the chain**, not “user cell also loses last arg.” Do **not** use a ≥1-hit rule; that would strip the matrix-index neighbor. A stray on range A must **not** poison a distinct chain on range B.
- **Cap-hit** → skip the entire sheet, do **not** mark any triple strip-safe, do not write a partial chain. You cannot prove unanimous on a truncated list.
- **Rejected:** uniqueness / “fail-safe = no strip” / “typical pipelines have distinct code strings.” That kills fill-down of identical `=PY("np.mean(data)"; B1:B10)`: after attach every successor has the same `resolved_code` and `n_args=2`, non-unique → no-strip → `calc_addin_args_from_split` flips `data` to a list, then the index heuristic peels the predecessor **value** as `index_arg` — silent wrong numbers.

**Recommended** (not instead of unanimous, not a replacement for `workbook_key` / `resolved_code` / `n_args`): fingerprint `args[:-1]` so two chains with the same snippet and **different ranges** do not share a triple. Without it, one non-mapped user cell poisons every chain that reuses `np.mean(data)` on any range. Residual of a coarse key is safe (no strip → no wrong numbers), just degraded. Does not save mixed same-range. Do **not** fingerprint the last-arg value (first recalc is empty/0).

#### Three must-gets (easy to get wrong)

**1. Key `code` is what `execute_python_addin` receives, not the formula token.** `PythonFunction.python` passes Calc’s first argument through as `code` (`addin_impl.py`). For `=PY($A$1; B1:B10; pred)` that is the **cell contents of `$A$1`** (resolved source), not the token `$A$1`. Repair must **read that cell** when building the eval index (`formula_edit.py` unquoted branch vs `addin_impl.py`). Keying the token `$A$1` misses every script-bank cell. Detect / splice with `py_formula_has_unquoted_code_ref` / `py_code_arg_is_cell_ref` / `rebuild_python_formula_with_code_ref` (exist on master). `PythonFormulaParts` has no quoted flag (`prefix` / `code` / `data_suffix` only) — splice code-in-cell from the **raw formula**, not `parts.code` alone, or `$A$1` gets quoted by `rebuild_python_formula_with_data`. Cells that share resolved source collide on `(code, n_args)` unless the recommended `args[:-1]` fingerprint splits them; same unanimous rule per finer key.

**2. `n_args` at eval is `len(split_python_addin_data_args(data))`** (`calc_addin_data.py`). Repair arity **must** match that splitter, not a naive semicolon count. A pair `(range, 1×1 pred)` does **not** collapse under `_is_legacy_single_column_range`: the inner of the 1×1 is a sequence, so two varargs stay two args (`n_args=2` after attach).

**3. Cap-hit:** `list_python_cells_on_sheet` returns 100 with **no** truncated flag (`cell_discovery.py`). You cannot prove unanimous. Already-decided skip-sheet is the fail-safe; do **not** mark those triples strip-safe. Phase 1 treats `len >= _MAX_PYTHON_CELLS_FOUND` as cap-hit (over-skips exact 100). A real truncated flag is Phase 3.

#### `workbook_key` (blocking — do not cite `get_python_init_kwargs`)

`get_python_init_kwargs` does **not** carry `doc_url`. `build_python_eval_init_kwargs` (`document_scripts.py`) returns `{}` with **no** session record when the init script is empty. When init is non-empty it calls `calc_init_session_id(doc)` → `calc_workbook_base_session_id` → `record_active_calc_session("calc:" + _workbook_session_key)` (`session_manager.py`). `set_calc_init_script` and on-main `get_python_init_kwargs` both go through that builder. `record_active_calc_session(None, kwargs)` itself does **not** add to `_RECORDED_CALC_SESSION_IDS` (`None` is ignored); the add is the side effect of building kwargs. Isolated does **not** “never enter `record_active_calc_session`.” Isolated + no init still never records. `workbook_session_id` still returns `None` when mode ≠ `shared`. Isolated still needs strip off-main (else arity breaks). Do **not** write a unit test that asserts Isolated always leaves `_RECORDED_CALC_SESSION_IDS` empty.

**Eval `workbook_key` = `get_cached_calc_session_id()` only when `off_main_calc_session_is_unambiguous()`** (`session_manager.py`: `len(_RECORDED_CALC_SESSION_IDS) == 1`). Else do not strip (two open workbooks, or Isolated that never recorded).

On the **UI-thread** load / repair path, write that key into the geometric map **and** call `record_active_calc_session` with the **same string eval will read**: `calc:` + `_workbook_session_key` (never `""`). Do this **even in Isolated**. Same string as the init-kwargs path; idempotent; **required for the no-init case**. Do **not** invent a second Isolated key. Then one open file makes `len(_RECORDED_CALC_SESSION_IDS) == 1` and yellow Isolated can strip. Sibling of `load_spill_registry_for_doc`. Unsaved files must not use empty URL — same #402 hole as `session_key` / `_workbook_session_key` (URL, else a persisted unsaved id, never `""`).

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
| Cap hit on this sheet | — | — | Skip the sheet; log **and** one message box; no partial chain; **do not** mark triples strip-safe |

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
- **Fingerprint:** same snippet, two ranges; one stray matrix-index neighbor on range A must **not** poison range B.
- **Two open workbooks / `off_main_calc_session_is_unambiguous()` false** → no strip.
- **Isolated** UI load/repair calls `record_active_calc_session("calc:" + _workbook_session_key)` (same string eval reads) and strips when unambiguous. Do not assert Isolated always leaves `_RECORDED_CALC_SESSION_IDS` empty.
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

---

## 12. Collabora / LibreOffice core (living sketch, not final)

**Status:** Research pass 2 against Keith’s Arch tree `~/Desktop/collabofficefull` (2026-09-02, flip 2026-09-03). Not implemented. **Pass 2 Collabora-native call is extra listen, not a trailing `;A1`.** Closed WriterAgent calls in [§9](#9-decisions) still stand (precedent-only trailing field, unanimous-ours if we strip by values, **no IDL pile-on**, cap-hit skip-sheet + a *user-visible* notice). WriterAgent [§9.1](#91-precedent-only-not-value-in-data-not-idl) stays trailing-field — LibrePy cannot add `sc/` listeners. This section is how the same *product* would attach if it had to live in Collabora Core / Online instead of LibrePy.

**Do not start this** until Tomaž’s jail-safe `=PY()` Gerrit is reviewed/landed ([online#16010](https://github.com/CollaboraOnline/online/issues/16010), [Gerrit online/+/8122](https://gerrit.collaboraoffice.com/c/online/+/8122), forum [4844](https://forum.collaboraonline.com/t/py-numpy-inside-collabora/4844)). Geometric order is a follow-up in existing files, not a third IDL argument.

### 12.1 What is different from WriterAgent (pass-2 flip)

| | WriterAgent / LibrePy | Collabora Online / this tree |
|--|----------------------|------------------------------|
| Attach (the flip) | Trailing `;A1` formula field ([§9.1](#91-precedent-only-not-value-in-data-not-idl)) | **Path A:** extra `StartListeningCell(prevPY)` after the RPN walk. **Do not** land per-cell `;A1` as the default |
| `=PY()` | Sync UNO add-in; `python()` blocks until the venv returns | Thin C++ AddIn returns `XVolatileResult` (`#BUSY!`), HTTP via kit→wsd→compute service, `complete_json` later |
| Worker | `compute_service` / venv — **forbidden in kit jail** | Already out-of-kit; attach/strip must be **in-process Core** |
| Calling address | `addin_impl.py` never sees it | `getPy(code, data)` never sees it either. Interpreter **does** have `pMyFormulaCell` during `ExecuteCall` |
| Deferred rewrite | `threading.Timer(0.1)` → UNO `setFormula` on the UI thread | **Wrong default.** Pending `vcl::Timer` only finishes volatiles. Path A attach is Core listen / import (see [§12.4](#124-can-online-defer-formula-rewrites-like-the-spill-timer)) |
| Session | Shared kernel optional | `buildExecuteRequestJson` **hard-codes** `"mode":"isolated"` today (jailsafe F5 is the shared-kernel follow-up) |
| Discovery | `list_python_cells_on_sheet` | Walk `ScColumn::GetCellStore()` / `GetFormulaCell` (no Python helper) |
| Shared formula groups | UNO `setFormula` on one cell | `CompileXML` groups consecutive identical formulas. A different `;A1` on each cell **breaks `mxGroup`** |

**Headline:** a trailing `;A1` still makes Calc’s listener DAG dirty B when A changes (`StartListeningTo` walks `GetNextReferenceRPN`). That is how WriterAgent does it, and it is still true here — and it is the **wrong default in this tree**. `CompileXML` (`formulacell.cxx` ~L1345–1385) groups consecutive identical formulas into `mxGroup`. Fill-down of `=PY("df = clean(df)")` stays one group. A unique `;A1` / `;A2` / `;A3` on each successor **splits the group**. VARARGS then packs that ref as a value into `data` (`interpr4.cxx` ~L3197–3221) unless we strip — the same arity footgun as [§4](#4-data-binding--do-not-shadow-data), plus a JSON identity split in `buildExecuteRequestJson`. Extra listen needs **no strip, no JSON identity split, no IDL**.

What WriterAgent gets “for free” from a **sync** add-in — Python in A **finishes** before B starts — Collabora does **not**. Both cells `getPy` → both emit HTTP → both `#BUSY!` in one pass. `ResultEvent` retriggers later. Extra listen (or a hand-typed `;A1`) without an **emit gate** (or a service-side ordered queue) is dirty-tracking, not A-before-B Python. Async race + emit-gate still stand.

Isolated mode (today’s Online default) makes geometric order a **no-op for Python globals**, same as [§9.3](#93-isolated-mode--checkbox-visible-no-op). Path A has no arity/strip work even so. Do not promise this checkbox in Online until (1) desktop LibrePy geometric is boring and (2) F5 shared kernel exists, unless the only goal is dirty-subgraph.

### 12.2 Where things live (this tree)

Root: `~/Desktop/collabofficefull` (Online at repo root; LibreOffice Core under `engine/`).

#### PY AddIn (Collabora scaddins)

| Path | What |
|------|------|
| `engine/scaddins/idl/org/collaboraoffice/sheet/addin/XPythonComputeFunctions.idl` **L20–24** | `any getPy(string code, sequence<any> data)` and `getPython` alias. **Do not add a third argument.** |
| `engine/scaddins/source/pythoncompute/addin.cxx` **L49–66** | `ScaPythonComputeAddIn::getPy` → `startCompute`; display names `PY` / `PYTHON` ~L93–167 |
| `engine/scaddins/source/pythoncompute/anyjson.cxx` **L878–895** | `buildExecuteRequestJson` — Path B strip site only (drop last Any before JSON). **L885 hard-codes `mode=isolated`.** Path A does not touch this. |
| `engine/scaddins/source/pythoncompute/bridge.cxx` **L68–75, L134–179, L344–417** | SolarMutex-only pending + param cache; `PendingTimeoutTimer`; `startCompute` emit |
| `engine/scaddins/source/pythoncompute/volatile.*` | `XVolatileResult` / `ResultEvent` |
| `engine/scaddins/source/pythoncompute/README.md` | Wire + identity + Solar 1+ε |
| `engine/scaddins/qa/pythoncompute.cxx` | CppUnit (identity, timeout, JSON) |

#### Formula compiler / DAG / precedents (stock Calc)

| Path | What |
|------|------|
| `engine/sc/source/core/tool/compiler.cxx` | `CompileString`; AddIn name maps ~L292–545; a trailing cell ref in `=PY("…"; A1)` is a normal `svSingleRef` token (Path B only) |
| `engine/sc/source/core/data/formulacell.cxx` **L1203** `Compile`; **L1251** `IsInsertingFromOtherDoc` **skips listen** — do not rely on first compile; **L1276** `CompileTokenArray`; **L1345–1385** `CompileXML` shared-formula groups (`mxGroup`); **L1443** `CalcAfterLoad`; **L2501–2507** matrix top-left only (F7); **L5855–5894** `StartListeningTo` — RPN `svSingleRef` → `StartListeningCell`; **this is Path A’s hook** (extra `StartListeningCell(prevPY)` **after** the RPN walk); **L5970–6012** `EndListeningTo` — **only walks RPN**. Extra listen **must** pair `End` or it leaks |
| `engine/sc/source/core/data/documen2.cxx` **L1203–1220** | `ScDocument::SetFormula` / `SetFormulaCell` — Path B rewrite API (no UNO). Not the Path A attach |
| `engine/sc/source/core/data/documen7.cxx` **~L591** | `StartAllListeners` — re-establish extra listen after import / listen-skip |
| `engine/sc/source/core/data/document.cxx` **~L4257–4294** | Import-time listen restore. Pair with `CalcAfterLoad` / `StartAllListeners`, not first `Compile` |
| `engine/sc/source/core/data/table2.cxx` **L1790** | `ScTable::SetFormulaCell` |
| `engine/sc/inc/column.hxx` **L455–456**; `column3.cxx` **L3248** | `GetFormulaCell` — discovery walk |
| `engine/sc/source/core/tool/interpr4.cxx` **L2991–3314** | `ScUnoAddInCall`; `NeedsCaller` is the **document** shell, not the cell (`addincol.cxx` **L1416–1432**). After `ExecuteCall`, `HasVarRes()` installs `ScAddInListener`. **`pMyFormulaCell` is the calling cell** (L3308) — Core already has the address WriterAgent lacks. **L3197–3221** VARARGS packs a trailing ref into `data` (Path B tax) |
| `engine/sc/source/core/inc/interpre.hxx` **L241** | `ScAddress aPos` on the interpreter |
| `engine/sc/source/core/tool/addinlis.cxx` **L104–118** | `ScAddInListener::modified` → `Broadcast` + `TrackFormulas` (SolarMutex) |
| `engine/sc/source/core/tool/formuladepchain.cxx` | Online JSON inspector (caps at 10/50 cells) — **not** the recalc engine. Do not hook geometric order here |

#### Online kit / wsd (jail)

| Path | What |
|------|------|
| `kit/PythonComputeEmitter.cpp` **L55–108, L187–287** | `dlsym` `pythoncompute_set_emitter` / `complete_json` / `clear_caches`; `session->sendTextFrame("pythoncompute: " + payload)` |
| `kit/ChildSession.cpp` **L1135–1137** | Spreadsheet-only `installEmitter` **on document load** (before status notify so on-load `=PY()` can emit). **L143** `clearEmitter`. **L3802** `completeFromJson` |
| `kit/Kit.cpp` **L3246** | `kitPoll` — SalTimer / `vcl::Timer` Invoke land here |
| `wsd/ClientSession.cpp` **L3021–3029, L3852+** | `handlePythonComputeFromKit` — jail cannot network; coolwsd POSTs. **No formula rewrite.** MOBILEAPP: ignored |
| `coolwsd.xml.in` **L273–278**; `common/ConfigUtil.cpp` **L276–279** | `security.python_compute.enable` default **false**, url, api_key, timeout_secs |

### 12.3 How hard is extra listen (path A) vs a trailing field (path B)?

**Path A (Collabora default):** extra `StartListeningCell(prevPY)` after the RPN walk in `StartListeningTo`. Not a compiler rewrite. Formula string stays `=PY("df = clean(df)")`. `GetFormula()` will not show the edge. Dirty-tracking only — same DAG `Broadcast` / `TrackFormulas` as a real ref token.

**Path B (WriterAgent; not the Collabora default):** if the formula string is `=PY("df = clean(df)"; A1)`, `CompileString` already emits a ref token and `StartListeningTo` already orders dirtying. Inserting the field is `ScDocument::SetFormula` (or splice then SetFormula), same product as [§3.2](#32-auto-attach-is-a-formula-field-not-a-python-parse). Do **not** land this as the Online default. Per-cell `;A1` is worse here than in LibrePy:

1. **`mxGroup`.** `CompileXML` groups consecutive identical formulas. A different `;A1` on each cell breaks the group. Fill-down rewrites are scarier than LibrePy UNO `setFormula` on one cell (`StartListeningTo` L5857 already special-cases `mxGroup`).
2. **VARARGS pack.** The ref becomes a `data` value (`interpr4.cxx` ~L3197–3221) unless we strip in `buildExecuteRequestJson` / `startCompute` before 1-vs-N packing (L886–892). Extra listen needs no strip, no param-cache identity split (`makeParamCacheKey` uses the JSON), no IDL.
3. **Visible field + timer.** Path B needs a deferred `SetFormula` walk. That is the wrong default ([§12.4](#124-can-online-defer-formula-rewrites-like-the-spill-timer)).

**Hard parts that remain on path A:**

1. **Discover PY cells row-major per sheet** — walk formula cells, match AddIn original name `ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY` / display `PY` / `PYTHON` (LibrePy uses a *different* UNO token; Online files won’t see WriterAgent names unless rewritten). Cap: pick a Core cap (100 is fine) and **skip the whole sheet**; Online has **no VCL message box** — need an infobar / view notification / cell-adjacent note, not `MessageBox`.
2. **Pair End / re-establish.** `EndListeningTo` (~L5970–6012) only walks RPN. Extra listen **must** pair `End` or it leaks. `IsInsertingFromOtherDoc` skips listen (~L1251). Re-establish at `StartAllListeners` (`documen7.cxx` ~L591) / import (`document.cxx` ~L4257–4294) / `CalcAfterLoad` (formulacell L1443), **not** first compile. Flag-on reconcile all sheets at those hooks. Kit `installEmitter` (ChildSession L1135) is already the load-time emitter point — listen restore is Core, not kit.
3. **Insert-in-middle** — same list-diff as [§3.4](#34-insert--delete--move--the-only-reason-this-is-not-a-one-liner), but the patch is “drop extra listen on old pred, add extra listen on new pred,” not a formula splice. Shared formula groups stay intact (`mxGroup` sees identical strings).
4. **Calling address** — `getPy` still has only `(code, data)` values. Path A does not need unanimous-ours for strip. Emit-gate (if we want Python order) should use `pMyFormulaCell` in `interpr4.cxx` (see [§12.4](#124-can-online-defer-formula-rewrites-like-the-spill-timer)). Prefer that over copying the WriterAgent map hack into C++.
5. **Flag** — not `security.python_compute.*` (admin jail switch). A Calc doc option / UDProp, default off. Isolated remains visible no-op for globals ([§9.3](#93-isolated-mode--checkbox-visible-no-op)).

**Hidden opcode so it doesn’t round-trip: don’t.** Path A is an extra listen, not a synthetic token. Reviewers will read a hidden opcode as a recalc-engine side channel. Extra listen is already a side channel — own that, pair End, re-walk on load. Do not invent a third IDL argument to make it “visible.”

### 12.4 Can Online defer formula rewrites like the spill timer?

**Timer `SetFormula` is unproven and the wrong default.** Path A attach is Core listen / import. Do not schedule a LibrePy-shaped rewrite.

| Path | Viable? |
|------|---------|
| WriterAgent `Timer` + UNO `setFormula` from a Python worker | **No** — jail, no LibrePy, no compute_service in kit |
| `vcl::Timer` in `pythoncompute` / a small `sc/` helper, `Invoke` under `SolarMutexGuard`, `ScDocument::SetFormula` | **Wrong default.** Pending `vcl::Timer` (`bridge.cxx` L134–179, README “Unipoll feeds SalTimer into kitPoll”) only **finishes volatiles**. Kit `postUnoCommand` is **not** a `SetFormula` walk. Timer `SetFormula` would race Unipoll/Solar (sticky `#BUSY!` class). Do not copy the spill timer here |
| Extra `StartListeningCell` after the RPN walk + re-establish at `StartAllListeners` / import / `CalcAfterLoad` | **Yes — Path A** |
| Compile-time inject in `ScCompiler` | Unnecessary; more dangerous |
| Import filter | Not needed for MVP; Excel rewriter already refuses synthetic prior-PY edges |
| Document-open | **Yes** — `ChildSession` load already installs the emitter (L1135); `CalcAfterLoad` (formulacell L1443) + `StartAllListeners` are the Core hooks. Flag-on reconcile all sheets here. `IsInsertingFromOtherDoc` skipped first compile — this is the restore |
| coolwsd rewriting formulas from HTTP | **No** — broker is document-blind by design |

Re-entrancy flag + idempotent desired-vs-actual still required for any Path B experiment. Undo in Online is the kit undo stack, not `_undo_lock` / `enterHiddenUndoContext` — Path A avoids a formula rewrite, so undo is “the user’s edit,” not a hidden splice. Budget extra time if anyone later insists on Path B.

**Emit gate (Collabora-native, if we want Python order, not just dirtying):** around `interpr4.cxx` `ExecuteCall` / `startCompute`, if `pMyFormulaCell`’s geometric predecessor is still in `g_aPending`, do not emit B’s HTTP yet (return the same `#BUSY!` volatile or a waiter). Resume emit from `complete_json` of the predecessor. That is **in-process**, Solar-only, no IDL, no worker. It *is* extra state machine on the pending map. Do not put this on Tomaž’s current patch. Extra listen alone does **not** serialize HTTP.

### 12.5 Jail / Online constraints — what still works in-process

**Cannot:** WriterAgent `compute_service`, venv worker, UNO-from-worker, pywebview, `processEventsToIdle`, Classic msgbox, per-user `~/.writeragent_venv`.

**Can:** everything already in `pythoncompute` — SolarMutex maps, `vcl::Timer` (volatiles only), `XVolatileResult`, listener DAG, extra `StartListeningCell` / paired `End`, kit emitter, wsd HTTP. Discovery + listen restore is Core C++ on the kit side of the jail (same process as LOKit).

**Must not:** `std::mutex` on AddIn state (Meeks 1+ε). Filename prefix `pythoncompute_` on sources. Runtime flag default on. Merge anyjson into jsuno. Pile IDL on `XPythonComputeFunctions`. Land per-cell `;A1` as the Online default. Emit from `complete_json` without re-acquiring Solar. Rewrite formulas from the wsd thread.

MOBILEAPP: pythoncompute is compiled out — geometric Online is desktop/server Online only.

### 12.6 Overlap with Tomaž’s in-review `=PY()`

The in-review work **is** the AddIn + kit + wsd tip. Geometric **attaches after it**:

- **IDL stays `getPy(code, data)`.** Path A never touches arity. Path B’s trailing A1 is already a legal varargs `data` element — still no `.rdb` bump, still not the default.
- Extra listen + optional emit-gate land in `formulacell.cxx` `StartListeningTo` / `EndListeningTo` + `StartAllListeners` / a small sc helper — **not** a pile-on to `anyjson.cxx` / `bridge.cxx` unless emit-gate needs the pending map. Keep the follow-up as a **new Gerrit** once the tip is merged, not a pile-on patchset.
- Do not block F6 (visible errors) or F7 (auto-spill) on this. F7 is the sibling “deferred write neighbors” problem. Path A does **not** share a SetFormula timer with F7 (there is no such timer for geometric). If someone later does Path B, **share one timer / re-entrancy flag**, same as [§3.5](#35-writes-must-be-outside-recalc-same-as-auto-spill) vs spill.
- F5 (shared kernel) is the product this flag is for. WSD should own `mode`/`session_id` (jailsafe F5: overwrite client ids). Geometric emit-gate still lives in Core so order is Calc-list, not HTTP arrival.
- Interop: Collabora stores `…PYTHONCOMPUTEFUNCTIONS.GETPY(...)`; LibrePy rewrites that on Classic load (`collabora_formula.py`). Path A extra listen **does not persist in the formula string** — Classic will not see the edge; LibrePy flag-on reconcile (trailing field) is a separate attach if the same ODS is opened in WriterAgent. Path B would need the LibrePy strip map to understand Collabora-attached preds. Out of scope for the first Collabora follow-up; note it. Prefer Path A so the file does not grow synthetic args that Classic must strip.

### 12.7 Effort vs WriterAgent’s ~1 week

WriterAgent’s week assumes Python, `formula_edit.py`, spill timer, UDProp, and a **sync** add-in.

| Slice | Guess | Why |
|-------|-------|-----|
| **Path A** extra `StartListeningCell` + paired `End` + re-establish at `StartAllListeners` / import / `CalcAfterLoad`, Isolated-only, dirty-subgraph only | **~1 week** Core C++ for someone who already owns this AddIn | Discovery walk + listen pair + cap-skip + load restore are new C++; no strip, no JSON identity, no `SetFormula` timer. Algorithm (list-diff) is already specified in §§3–9 |
| **Path B** trailing-field attach + `anyjson` strip + timer repair | **1–2 weeks** and the **wrong default** | `mxGroup` split + VARARGS pack + Unipoll/Solar `SetFormula` race. Do not schedule this |
| Unanimous-ours map copied into C++ because we refuse to touch `interpr4` | **+several days** (Path B only) and still the same identity footguns | AddIn has **no `ScDocument` pointer** today (process-wide volatile cache). Path A does not need it for attach |
| `pMyFormulaCell` stash + address-keyed map (no unanimous-ours) | **Days**, but it is an `sc/` interpreter touch | Cleaner than WriterAgent if we need emit-gate identity; more reviewer surface than scaddins-only |
| Emit-gate so shared-kernel Python actually runs A then B | **+1–3 weeks** state machine on `g_aPending` + tests | This is the Collabora-specific tax. Extra listen is dirty-tracking, not A-before-B Python. Service FIFO lock (F5) is **not** geometric order |
| Reviewer / Gerrit / Online undo / no-msgbox / extra-listen lifetime / multi-view | **Open-ended; think months if we fight the tip review** | Wait for Tomaž. Don’t combine with F3 plots or F7 spill in one series |

**Honest range:** Path A dirty-tracking in Core is **about a week** after `=PY()` has landed. Real A-before-B Python on Online is **weeks**, not a WriterAgent week, because of `XVolatileResult`. Full Excel co-volatility remains months in `sc/` and is still rejected.

Do not schedule this as “LibrePy geometric (trailing field), then copy-paste to Collabora in a day.”

### 12.8 Scariest unknowns (2–3, plus a fourth)

1. **Async volatile vs sync `python()`.** Extra listen + DAG ≠ serialized HTTP. Confirm with a two-cell shared-kernel (F5) experiment: A assigns `x=1`, B reads `x`. If both POST before either `complete_json`, B loses. Emit-gate or service-side geometric queue is then in-scope; listen attach alone is not.
2. **Calling address / process-wide AddIn.** `getPy` is values-only; cache is process-wide, not per-`ScDocument`. Two views of one doc share kit (OK). Two docs in one kit (unusual) would share strip identity **if** anyone later does Path B. `pMyFormulaCell` is the way out for emit-gate; using it means touching `interpr4.cxx` which Tomaž did not ask to re-review.
3. **Online UX for cap-hit skip-sheet.** Closed call wants a user-visible message box. Kit has no desktop dialog. Infobar vs log-only vs `#DISABLED`-style marker is unresolved.
4. **Extra-listen lifetime.** `EndListeningTo` only walks RPN — unpaired extra listen leaks. `IsInsertingFromOtherDoc` skips listen; copy/import/undo/fill must re-establish at `StartAllListeners` / import / `CalcAfterLoad`, not first compile. Shared-formula `mxGroup` is *why* we prefer Path A, but a group of identical PY cells still share one listener walk — confirm extra listen is per-cell, not per-group. Also: kit undo vs hidden LibrePy undo; `GetFormula()` will not show the edge (reviewers will read it as a recalc-engine side channel — own that).

**Not unknown, just easy to regress:** SolarMutex 1+ε; don’t emit from `complete_json` without re-acquiring Solar; don’t rewrite formulas from the wsd thread; don’t land Path B `;A1` “because WriterAgent did.”

### 12.9 Suggested Collabora phases (after the tip lands)

0. Keep this sketch in the WriterAgent doc. No product C++ until Keith + Tomaž want a follow-up.
1. Prove dirty-tracking only: two Isolated `=PY` cells, extra `StartListeningCell` on B (or a throwaway hand-typed `;A1` as a *control*), confirm `StartListeningTo` already dirties B. Prove the async race with logging of emit order. **Do not** ship the `;A1` control.
2. If race is real (expected): design emit-gate using `pMyFormulaCell` + pending map. **Still no IDL.**
3. Then Path A auto-attach + End-pair + load restore + cap skip, flag default off, Isolated no-op for globals. No SetFormula timer. No trailing field.
4. LibrePy interop note when the same ODS moves Classic ↔ Online (Path A edge does not survive in the formula string; Classic flag-on reconcile is a separate attach).

---

*Living sketch, pass 2. Tree paths are from `collabofficefull` as of 2026-09-02; line numbers will drift. Re-read `formulacell.cxx` `StartListeningTo` / `EndListeningTo` / `CompileXML`, `documen7.cxx` `StartAllListeners`, `interpr4.cxx` VARARGS + `pMyFormulaCell`, and `anyjson.cxx` before any brief. Do not treat pass 1’s trailing-field default as current.*
