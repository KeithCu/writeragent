# AFC / eval-2 harness misses — first-principles analysis (v4)

**Audience:** Keith / Chief / harness + Calc product owners  
**Inputs:** PR 632 `harness-miss-analysis.md`, eval-2 `prompt.writeragent.txt` / gold `rubric_pretty.txt`, product under `/workspace/writeragent` (verified 2026-09-06), DeepSeek review of prior cut, Keith feedback  
**Scope:** Docs/research only — no product PR.  
**Status:** v4 — independent calls. Where DeepSeek (or earlier drafts) proposed something we are **not** doing, see **§6 Not adopting**. Where several product paths are viable, see **§5 Forks & model experiments** — do not pretend there is one true answer until a keyed trial says so.

**Constraint:** default `chatbot.max_tool_rounds` = **15** (`plugin/chatbot/module.yaml`). Eval-2 wants ~**50**. Round budget amplifies whatever the funnel teaches. Pair any bump with a repeated-error brake (**§2.4**); do not implement either in this research track.

---

## 1. Problem framing

### Causal chain (one story)

Six CLEAN Ready runs finished the chat loop; **zero** produced a rubric-credible Sample:

1. **Can’t see the table** — `read_cell_range` caps at 80 cells; truncated message + `WriteCellRange` / `CALC_WORKFLOW` **teach `=PY`**.  
2. **Wrong actuation** — `=PY` tourism, or native `write_formula_range` that **pins** one formula across 1516 rows (`manipulator.py`: `values = [formula] * total_cells`, then identical `setFormula` per cell).  
3. **False progress** — `create_sheet` returns ok with an empty tab; create looks like the deliverable.  
4. **Ready** — `STREAM_DONE` with no `tool_calls` → status Ready (`tool_loop_state.py`); round exhaustion still ends Ready via `SpawnFinalStreamEffect`. No document gate.

Deal-unblocking made runs finish. It did not make the task solvable under current chrome.

### Ready ≠ solved

Scoring that trusts Ready green-washes empty workbooks. Pass/fail belongs on the ODS.

### Four layers (don’t mix)

| Layer | Job |
|-------|-----|
| **Product affordance** | What tools do; what descriptions/results teach; fill-down; copy |
| **Stop / status** | When the loop may say Ready; repeated-error brake |
| **Eval chrome** | Prompt / fixture / rubric; clean trial dir; eval-2 round budget |
| **Harness scoring** | Pass/fail on the ODS, independent of chat status |

Keep these separate so a later PR does not reintroduce specialized postconditions or AFC-shaped Ready gates into the product.

---

## 2. Per miss (verified + recommendations)

### 2.1 Range-too-large → `=PY` funnel

**Verified**

- Cap 80; truncated msg: “pass this A1 address to =PY instead of re-reading” (`cells.py`).  
- `WriteCellRange.description` **opens** with `=PY(...; DataRange)` and includes deliberate anti-husk guidance (`to_pandas()` vs `pd.DataFrame`, `np.unique` mixed types) — **preserve that paragraph when reordering**.  
- Descriptions teach cross-sheet as `Sheet1.A1` (dot), never Excel `!`.

**Reject:** raising the cap; prompt-only “don’t use =PY” while tools still funnel there.

**Recommend**

1. Keep the 80-cell cap.  
2. Rewrite truncated message + peek: rows/cols/cells + header peek; **cardinality-capped distinct counts** (skip / summarize columns with hundreds of uniques — no phone book).  
3. Steer: row-wise **ordinary Calc** formulas → `write_formula_range` / fill-down; reductions that spill small results → `=PY` into **one** empty cell outside the data.  
4. Reorder `WriteCellRange` / `CALC_WORKFLOW`: values & native formulas & fill-down first; =PY spill second — **keep anti-husk text verbatim**.

**Task-local principle (not a universal Calc law):** for AFC-style full-column transforms (variance into J), the model should **write formula + fill-down**, not read 1516 rows. Distincts / small peeks are the legitimate read use for selection criteria. Other Calc tasks may still need column peeks — don’t bake “never read” into global product dogma.

**Distinct counts:** no existing cardinality helper in `plugin/calc/` (spot-check). Computing them on the truncated path (via existing read/`getDataArray`) is new code; scope it there first rather than inventing a public tool.

---

### 2.2 Formula pin → fill-down (**=PY-exempt**)

**Verified:** single-string fill pins identical formula text on every cell.

**Reject as product answer:** refuse-pin, or “pass an array of 1516 formulas” (recreates the context blowup the 80-cell cap exists for).

**Recommend — fill-down for ordinary Calc formulas only**

| Input | Multi-cell range |
|-------|------------------|
| Ordinary Calc (`=IF(H2=0;…)`, `=H2+1`, …) | Fill-down with relative-ref adjust; `$` stays absolute |
| `=PY` / PYTHON / PYTHONFUNCTION | **Verbatim**, single dest cell; **never** adjust the DataRange arg across rows |

**Absolute `$` is necessary but not sufficient** — a mistaken multi-cell `=PY("…"; A1:H1517)` would still wreck husks if fill-down always adjusted A1 refs.

**Implementation preference (this draft):** **Python A1-adjust** in the existing `formula_cells` loop (`manipulator.py` ~807–811), not LO fill/series APIs first. Reasons: pure unit tests in `tests/calc/` with no soffice; LO fill behavior varies by build; least new surface. (If a later spike shows LO fill is trivially reliable and cheaper, revisit — see §5.)

**Range shape:** well-defined for **1×N or N×1**. For a 2-D range + one formula, refuse or require an explicit array — don’t invent fill-down-and-right semantics in v1.

**Refs:** same-sheet relative + `$` are the AFC pin bug. Tool text also teaches `Sheet.A1` / quoted sheets — support those in the adjuster **enough that common cases don’t silently corrupt**, but don’t block the PR on every LO ref dialect (see §6).

**Tests:** `J2:J5`+`=H2` → H2…H5; `$H$2` stays; `=PY(…; A1:H10)` multi-cell does not shift DataRange; after ship, harness “J3 refs H3” (don’t score before fill-down exists).

**Build vs release:** spike fill-down + the 5-line unit test **first** in the change set; rewrite messages against behavior that exists. **Release** still ships message rewrite + fill-down (+ create/copy teach) together so we don’t un-teach =PY into a pin-only world.

---

### 2.3 Empty Sample / create-without-populate

**Verified:** `CreateSheet` (`sheets.py`) — specialized via `ToolCalcSheetBase` → `specialized_domain = "sheets"`. Returns `{status: ok, message: "…created."}` with **no populate**. No `copy_range` tool in `plugin/`.

**Keith call (adopted):** specialized vs core is **not** the biggest problem. The model needs to know create is **CRUD only** — tab exists, document content unchanged. **Prefer teaching that first** (description + result message + workflow line), before any “promote to core” move.

**Reject:** specialized completion schemas / `rows_written` postconditions; `copy/filter-to-sheet` mega-tool (filter grows unbounded; Sample can be full population + K flags).

**Recommend (sequenced)**

1. **First:** make create’s contract unmistakable — description + ok payload (“sheet created; no cells copied”) and a CALC_WORKFLOW one-liner: create ≠ Sample deliverable.  
2. **Then:** generic **`copy_range`** (source → target sheet/start, optional header, `{rows_copied}`) — copies **whatever range the caller passes**. Do **not** bake “must include J/K” into the tool.  
3. **Optional later:** put `create_sheet` on the core list if delegation tax still shows up in trials after (1)–(2). That is an **experiment**, not a prerequisite (§5).

**Eval strategy note (not a tool law):** a strong AFC pattern is write J/K formulas on Sheet1 (fill-down), then `copy_range` of `A1:K…` (or A:H + later fill on Sample) onto Sample. Oracle should check flags **where the deliverable lives** (Sample sheet for eval-2). Alternate patterns exist — see §5.

---

### 2.4 Ready / oracle / brake / rounds

**Verified:** Ready = loop idle; exhaustion still Ready; AGENTS.md: keep chat FSM pure — `next_state` no I/O.

**Harness oracle (eval-2, fail-closed on ODS)**

- Sheets `Sample` + `Sample Size Calculation`  
- Sample data rows > 0  
- S = count of flag=`1` on **Sample** ≥ R (and R ≥ 1)  
- Ban dominant husks: `#DIV/0!` / `Err:507` / `Error:` **and** observed residue: `_deal_*` / `DEAL_*` / `PYTHONFUNCTION`-shaped cell text  

Leave **interactive** Ready alone — no AFC predicates in the general product Ready path.

**Repeated-error brake**

Deepseek ~450 `getCellAddress` errors then Ready. ~50 rounds **without** a brake lengthens storms.

- Lives in **host / interpreter** (`tool_loop` layer) where tool results (including **delegated** specialized errors) are visible — **not** inside pure `next_state`.  
- Signature must see through `delegate_to_specialized_*` results.  
- Pair with ~50 as **one eval-2 rollout**.

**Not adopting (DeepSeek):** inventing a full product **“Stuck”** terminal status as the required design (host + FSM + UI). See §6. Prefer a host circuit-breaker that stops further tools and ends the turn with a clear **reason string** (logs / harness), reusing existing stop/exhaust paths where possible. If trials show operators confuse that with success, *then* consider distinct UI chrome.

---

### 2.5 `document_research` escape

**Verified:** Calc MUST-delegate `document_research` for other files; prose mentions other personal/business files in the same folder (`prompts.py`).

**Reject (product):** gateway keyword firewalls / code “identity” rules about the open workbook.

**Recommend**

1. **Eval hygiene (what actually stops Gemini):** clean trial dir — Population ODS only.  
2. **Prompt tweak only:** research is for **other** documents, not re-reading the open workbook / sibling prompt files.

---

### 2.6 Prompt / rubric / fixture columns

**Verified**

- Fixture headers: A=No … **G=Q3 2024 KRI, H=Q2 2024 KRI** (8 columns A–H).  
- Writer prompt: “columns **H and I**” for variance → **J** / flags **K** — **I does not exist**; Q2/Q3 axes wrong.  
- Gold `rubric_pretty.txt` is **structurally** a different deliverable (separate Sample workbook; variance in **I**; flags in **J**; S-total wording references **K** while other lines treat **J** as variance). Not a letter-shift of the in-workbook variant.

**One-line prompt fix (eval-2):**  
*“Q2 is in H, Q3 is in G; variance = (G−H)/H into J; flags in K.”*

Derive `rubric.eval2` from **fixture + in-workbook oracle**, not by substituting letters in gold. Leave `docs/eval/gdpval/` untouched.

---

### 2.7 DuckDB / `__Anonymous_Sheet_DB__0`

Anon name is a LibreOffice **database-range** from xlsx→ods conversion — **runtime artifact**, not something you “strip from the xlsx fixture.” Scrapers must not treat it as agent SQL.

**Levers:** (a) demote DuckDB advertising from default =PY policy blurb; (b) harness/scrapers ignore anon sheet; (c) optional delete-at-trial-open. Prefer (a)+(b) first; (c) only if scrapers still false-positive (§5 / §6).

---

### 2.8 DESIGN.md / milestones / DSPy

- Staged DESIGN minimal-repros may not be in-tree — don’t block on them.  
- SSC→J→K→Sample checklist: optional **one-line** eval hint, not a workstream.  
- DSPy / multi-turn: premature until fill-down + teach-create + copy + messages + oracle exist.

---

## 3. Cross-cutting principles

1. Tool messages are product policy.  
2. Actuation over ceremony — fill-down and copy beat specialized postconditions.  
3. Ready is loop-local; pass/fail is document-local.  
4. Primitives stay dumb — create creates; copy copies the range you pass.  
5. Teach create≠populate **before** rearranging tier/core.  
6. Prompt clarity over gateway heuristics; clean trial dir stops Gemini.  
7. Eval chrome may differ from gold; don’t edit gold trees.  
8. Fail closed on emptiness; brake with any round bump; brake sees delegation.  
9. Un-teach =PY and ship fill-down together **at release**; spike fill-down first **in build**.  
10. Fill-down never adjusts =PY.  
11. Where forks exist, **experiment on real models** (§5) instead of freezing taste.

---

## 4. Recommended follow-up order

**Same week (eval, cheap) — no wait on actuation PR**

1. ODS oracle on the six Ready runs (expanded husk ban; flags on Sample).  
2. Column-align writer prompt (one-liner above) + `rubric.eval2`; clean trial dirs; ignore anon sheet in scrapers.

**First product change set (release together; build fill-down spike first)**

3. Truncated-read + WriteCellRange / CALC_WORKFLOW split; cardinality-capped distincts; **keep =PY anti-husk paragraph**.  
4. Fill-down (Python A1-adjust; 1×N/N×1; =PY-exempt) + tests.  
5. **Teach** create≠populate (description/result/workflow). Add **`copy_range`**. Core promotion of create = **only if** §5 Exp C says so.

**Downstream**

6. Prompt: document_research = other documents.  
7. Demote DuckDB from default =PY blurb.  
8. Repeated-error brake (host layer, delegation-aware) **+** max_tool_rounds ~50 as one eval-2 rollout.  
9. Land this analysis in-tree if useful; DSPy only if still red after the above.

---

## 5. Forks & model experiments

Several places have **more than one defensible solution**. Don’t collapse them in the doc — run cheap keyed trials (same model card Keith cares about: e.g. gpt-oss-120b, plus one weak stormy model like deepseek-shaped) on the eval-2 AFC row or a tiny fixture.

### Exp A — Truncation / =PY steer (message only vs message+fill)

| Arm | Change |
|-----|--------|
| A0 | Baseline (current funnel) |
| A1 | Message rewrite only (no fill-down yet) |
| A2 | Message rewrite + fill-down (release candidate) |

**Watch:** =PY rate; pin rate; any Ready with empty Sample; whether A1 alone just moves failure from =PY to pin.

### Exp B — Fill-down implementation

| Arm | Change |
|-----|--------|
| B1 | Python A1-adjust in manipulator loop (default plan) |
| B2 | LO fill/series API if a one-day spike shows it is stable on our LO builds |

**Watch:** unit-test cost; headed flake; same-sheet pin fix rate. Prefer B1 unless B2 is clearly cheaper and green.

### Exp C — create_sheet discoverability

| Arm | Change |
|-----|--------|
| C1 | **Teach only** — description + ok payload “no cells copied” + workflow line (preferred first) |
| C2 | C1 + create on **core** tool list |
| C3 | C1 + create stays specialized, but core list gets a one-line pointer (“sheets domain: create_sheet”) |

**Watch:** rounds to first `create_sheet`; empty-Sample Ready rate after copy exists. **Hypothesis:** C1 is enough; C2 is optional tax cut, not the main bug.

### Exp D — Sample population strategy (prompt/notes, not tool shape)

| Arm | End-state |
|-----|-----------|
| D1 | Full population on Sample + K flags (and J); `copy_range` of wide range including flags |
| D2 | Write J/K on Sheet1, copy A:K to Sample |
| D3 | Copy A:H only, then fill-down J/K **on Sample** |

**Watch:** oracle pass rate; tool-round count; flag column location mistakes. Tool stays dumb (`copy_range` = caller’s range). Oracle always scores **Sample**.

### Exp E — Brake UX

| Arm | Change |
|-----|--------|
| E1 | Host circuit-breaker → stop tools + reason string; reuse exhaust/final-stream path (no new status) |
| E2 | New distinct terminal status (“Stuck”) in FSM/UI |

**Watch:** false stops on flaky UNO; whether operators/harness still misread Ready. **Default plan: E1**; escalate to E2 only if E1 confuses scoring/ops.

### Exp F — Anon DB sheet

| Arm | Change |
|-----|--------|
| F1 | Ignore in scrapers + demote DuckDB blurb |
| F2 | F1 + delete anon sheet at trial open |

**Watch:** false “used SQL” flags; open-time flake. Prefer F1.

### Exp G — Round budget

| Arm | Change |
|-----|--------|
| G0 | 15 rounds, no brake |
| G1 | ~50 rounds, no brake (expect longer storms — control) |
| G2 | ~50 + brake (candidate rollout) |

**Watch:** deepseek-shaped error storms; successful fill+copy completions on stronger models.

---

## 6. Not adopting (and why)

| Proposal | Source | Why we are **not** doing it (or not yet) |
|----------|--------|------------------------------------------|
| Refuse-pin / pass N formulas as the product answer | earlier draft | Recreates context blowup at 1516 rows; unsolvable native path. |
| Fill-down of `=PY` / adjusting DataRange across a multi-cell write | risk if fill is naive | New husk class; =PY belongs in one dest cell. |
| Prefer LO fill API over Python A1-adjust as default | DeepSeek inverse of earlier draft | We **prefer Python first** (unit-testable, no soffice). LO fill is Exp B, not the committed default. |
| Require day-one support for every sheet-ref dialect before merge | DeepSeek tone | Common `Sheet.H2` / quoted / `$` yes; don’t block actuation on exotic refs. |
| Promote `create_sheet` to core as the first/primary fix | earlier draft / DeepSeek emphasis | Specialized isn’t the main bug; **teach create≠populate first** (Keith). Core move = Exp C. |
| Bake “copy must include J/K” into `copy_range` | DeepSeek | Tool copies the range you pass. J/K strategy is prompt/oracle (Exp D), not tool schema. |
| `copy/filter-to-sheet` mega-tool | rejected earlier | Filter scope creeps; flags/formulas do selection. |
| Specialized `rows_written` / completion postconditions | rejected earlier | Gameable, AFC-shaped. |
| Product identity/gateway firewall for `document_research` | rejected | Brittle; clean dir + prompt wording instead. |
| Gate interactive Ready on AFC predicates | rejected | Keep pass/fail document-local. |
| New first-class **“Stuck”** UI/FSM status as required brake design | DeepSeek | Real cost across host/FSM/UI for an eval amplifier. Prefer host brake + reason string (Exp E1); Stuck only if needed. |
| Treat brake + 50 as “just a config line” | earlier soft wording | Brake is host logic (delegation-aware); **roll out** paired with 50, but don’t understate the code. |
| “Never read the column” as global Calc law | DeepSeek absolute | True for AFC variance transforms; not universal. |
| Strip `__Anonymous_Sheet_DB__0` from the xlsx fixture | earlier draft | Runtime conversion artifact — strip is a no-op. Use F1/F2. |
| Delete =PY anti-husk warnings when demoting =PY in descriptions | risk | Those warnings prevent real husks; reorder, don’t delete. |
| Replace DESIGN.md purpose with this doc by fiat | DeepSeek suggestion | This can land in-tree as analysis; DESIGN.md was minimal-repro recipes — don’t conflate unless Keith wants one file. |
| Edit `docs/eval/gdpval/` or letter-substitute gold → `rubric.eval2` | — | Gold is structurally different; derive eval-2 rubric from fixture + oracle. |
| Implement max_tool_rounds / brake / fill-down in this research track | scope | Plan only; Chief/coding agents implement after review. |

---

## 7. Non-goals (short)

- Raising `read_cell_range` to full Population size.  
- DuckDB as the AFC selection engine.  
- Sample-size math product tool (oracle `R≥1` is enough for muse-style FPC→0).  
- Blaming Deal for these six Ready misses.  
- DSPy before actuation + teach-create + oracle.

---

## Appendix — symbol map

| Concern | Where |
|---------|--------|
| Read cap / =PY steer | `plugin/calc/cells.py` — `_READ_CELL_RANGE_*`, `ReadCellRange`, `WriteCellRange` |
| Formula pin / fill loop | `plugin/calc/manipulator.py` — `[formula]*total_cells`, `setFormula` loop |
| Create sheet (specialized sheets domain) | `plugin/calc/sheets.py` `CreateSheet`; base tier via `ToolCalcSheetBase` |
| Ready / exhaust | `plugin/chatbot/tool_loop_state.py` STREAM_DONE / SpawnFinalStreamEffect |
| FSM purity | root `AGENTS.md` — `next_state` no I/O |
| Rounds | `plugin/chatbot/module.yaml` `max_tool_rounds` |
| Research MUST | `plugin/framework/prompts.py` Calc document_research directives |
| Eval | `docs/eval/eval-2/afc-sample-83d10b06/`, gold under `docs/eval/gdpval/…` |
