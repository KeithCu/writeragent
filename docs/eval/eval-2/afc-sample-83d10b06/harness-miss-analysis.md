# AFC Population→Sample: why Ready runs still miss (harness briefing)

**Audience:** Keith — things to think about for harness improvement. Not a PR, not code.  
**Scope:** Six CLEAN Ready runs on `afc-sample-83d10b06` (deal bites absent / fixed). Eval asks for in-workbook sheets `Sample` + `Sample Size Calculation`, QoQ variance in col J, sample flags in col K, multi-criteria selection.  
**Honest frame:** **Ready ≠ solved.** Chat finished; deliverables mostly did not.

---

## 1. TLDR

Across six models with clean `@deal`, every run reached Ready, yet **zero** produced a rubric-credible Sample: either deliverable sheets are missing, empty shells, or PY/error husks; variance/flags are wrong, one-shot, or absent; and agents routinely stop after exploration (`read_cell_range` → “range too large” → `=PY` probe) or after `create_sheet` without copying/filtering rows. The gap is harness + tool-surface + stop-criteria, not residual deal crashes. Fixing deal unblocked the runs; it did not make the task solvable under current chrome.

---

## 2. Scoreboard

| Model / run | Sheets present | Sample quality | Deal | One-line miss |
|---|---|---|---|---|
| gpt-oss-120b `2047` | Sheet1, Sample, Sample Size Calculation (+ anon DB) | Sample ≈ PY `DEAL_MAX_CELL_REF` error + `#DIV/0!` K formulas; SSC = error cell; Sheet1 J formulas filled but wrong/constant | clean* | Sheets created; contents are error husks, not a sample |
| gpt-oss-120b `2054` | Sheet1, Sample, Sample Size Calculation (+ anon) | SSC ≈ n=68 (no FPC); **Sample empty** (1 blank row); Sheet1 J OK-ish, K=`Err:507` PY | clean | Created sheet names; never populated Sample / failed K via PY |
| muse-glimmer-30b `2101` | Sheet1, Sample Size Calculation (no Sample) (+ anon) | SSC params OK but **Recommended R = 0** (FPC refs empty B7); J header only; J2 PY interpreter error | clean | Tiny log / worker only; no Sample; R=0 |
| deepseek-v4-flash `2112` | Sheet1 only (+ anon) | No Sample / SSC; J leftover `_deal_grid_ok` PY errors | clean | Range-too-large thrash + Hung/`getCellAddress` storm; Ready with no deliverables |
| grok-4.6 `2122` | Sheet1 only (+ anon) | No Sample / SSC; J1 = truncated/failed `=PY` probe | clean | Explored + one J1 write; never created deliverable sheets |
| gemini-3.8-flash `0232` | Sheet1 only (+ anon) | No Sample / SSC; leftover `=PY` search residue | clean | Delegated **document_research** (prompt/files inventory); never built sheets |

\*Notes mark these Ready/clean; logs may still show isolated PY PreContract strings inside cells from earlier tool attempts — not the mid-run deal crashes that blocked older runs.

---

## 3. Recurring failure modes (ranked) → harness implications

### 1) Ready without a deliverable gate (all six)
Agents mark finished when the chat loop ends, not when `Sample` exists with rows and K flags ≥ R.  
**Evidence:** deepseek/grok/gemini final ODS = Sheet1 only; gpt-oss-2054 `Sample` has 1 empty row yet run notes “Ready”; muse notes Ready with no Sample.  
**Implication:** Stop condition is social (“I explained the plan”), not structural.

### 2) `range-too-large` → forced `=PY` detour (5/6; gemini avoided count but still left PY)
Reading A1:H1517 returns truncated peek + “pass this A1 address to =PY”. Models comply, then burn turns on PYTHONFUNCTION / spill / deal-shaped returns instead of row-wise Calc formulas or chunked reads.  
**Evidence:** gpt-oss-2047/2054 “Range is too large…” then `write_formula_range`/`=PY`; deepseek **195** range-too-large hits; grok J1 `=PY("df = data.to_pandas()…")` then “formula got truncated”; muse J2 InterpreterError on `data[:,0]`.  
**Implication:** The “safe” large-range affordance teaches the wrong workflow for this eval.

### 3) Sheets domain is create-ish, not “build the sample” (gpt-oss both; specialized path)
`delegate_to_specialized_calc_toolset(domain=sheets, create Sample…)` creates tab names; copying filtered population rows / writing K does not reliably follow. Specialized agent even narrates “user may need to manually copy rows.”  
**Evidence:** 2054 log: `sheet named 'Sample' created` + empty Sample sheet; create_sheet appears inside specialized turns, outer tool counts show **0** direct `create_sheet` from parent on several runs.  
**Implication:** #630 create-only (or create-without-populate) looks like success to the parent agent.

### 4) Sample-size / variance logic bugs even when sheets exist (muse, gpt-oss)
Muse FPC: `B8 = B7/(1+(B7-1)/B4)` while infinite-n lives in a formula cell that doesn’t feed B7 → **R=0**, then Ready. gpt-oss-2047 Sample/SSC cells are PY address errors; Sheet1 variance fill shows identical values / `H2`-anchored formulas. gpt-oss-2054 SSC reports 68 without FPC (gold/rubric want FPC).  
**Implication:** No mechanical check that R≥1, S≥R, or J is per-row.

### 5) Tool-loop pathology / false progress (deepseek, grok)
Deepseek: `Hung` / `tool_execute round=11 delegate…`, **~450** `getCellAddress` `TOOL_EXECUTION_ERROR`s, still Ready. Grok: repeated truncated PY, `_deal_grid_ok` / `_deal_dict_ok` PreContract strings **in cell values**, still Ready.  
**Implication:** Errors are treated as observations, not hard failures; loops don’t escalate to a simpler Calc plan.

### 6) DuckDB / `__Anonymous_Sheet_DB__0` always present, never decisive
Every final ODS includes the anonymous DuckDB sheet; `run_sql` outer tool-call counts were **0** in these six. Surface noise without payoff.  
**Implication:** Extra SQL/DB chrome competes with sheets tools without helping selection criteria.

### 7) Off-task specialized domains (gemini)
Gemini’s delegates: `document_research` for workspace file inventory + reading `prompt.writeragent.txt`, then `specialized_workflow_finished` with an engagement-file answer — while Sheet1 untouched for Sample.  
**Implication:** Research toolset is an escape hatch from the hard Calc work.

### 8) Prompt/rubric column chrome (eval hygiene, all models notice)
Writer prompt: variance on “columns H and I”, result in J, flags in K. Fixture headers are G=Q3, H=Q2 (no I). Rubric_pretty still talks basename `Sample` workbook and I/J in places. gpt-oss explicitly reasons “no I column… meant G and H.”  
**Implication:** Column-letter traps burn context; soft rubric mismatch muddies “did we pass?”

---

## 4. Concrete harness ideas (DO + why)

1. **DO gate Ready on structural checks** — require sheets named `Sample` and `Sample Size Calculation`, Sample data rows > 0, count of K=`1` ≥ R (or ≥1 if R missing), and no `#DIV/0!`/`Error:`/`Err:507` in J/K for sampled rows. **Why:** these six prove conversational Ready is meaningless.

2. **DO prefer chunked native Calc for variance** — scaffold or steer `J2=IF(H2=0,…)` fill down (or `write_formula_range` over J2:J1517) before offering `=PY` for 1500-row grids. **Why:** PY path is where truncation, deal wrappers, and Err:507 cluster.

3. **DO change the large-range tool message** — when range-too-large, return schema + N + distinct Division/Sub-Division/Country counts + criterion hit counts, not “use =PY on A1”. **Why:** peek-only currently funnels models into PYTHONFUNCTION tourism.

4. **DO make sheets specialized populate, not just create** — one atomic “copy Sheet1 → Sample with columns J/K” or reject create-only as incomplete. **Why:** empty `Sample` tab is the gpt-oss-2054 false win.

5. **DO add mid-run deliverable checklist in prompt chrome** — explicit ordered milestones: (1) SSC with N,z,p,e,FPC,R (2) J filled (3) K criteria (4) Sample sheet = flagged rows. **Why:** single-shot agents stop after step 0–1.

6. **DO consider multi-turn scaffolding / forced subgoals** for this eval size (~1516 rows × multi-criteria). **Why:** single chat budget dies in read/PY loops before selection.

7. **DO hide or demote DuckDB/SQL for this task** unless criteria queries are first-class and return row ids into K. **Why:** anon DB sheet on all six finals, zero useful `run_sql` usage.

8. **DO tighten specialized routing** — Population open + “create Sample sheet” should not route to `document_research`. **Why:** gemini burned the run inventorying the prompt file.

9. **DO align column letters in writer prompt (and eval-2 rubric) with the fixture** — G/H → J variance, K flags; drop basename-`Sample` soft criteria or mark clearly soft. **Why:** apples-to-apples with gold intent without burning tokens on H/I vs G/H.

10. **DO fail closed on R=0 / empty Sample in harness scoring** even if UI says Ready. **Why:** muse’s FPC bug is a perfect false green.

---

## 5. What NOT to blame

- **Not deal.** These six were selected as clean Ready (notes: no mid-run coaching / deal fix applied / 0 PreContract for gemini after `is_numeric_grid` fix). Residual PY error *strings inside cells* are model/tool-choice failures, not the old blocking deal crashes.
- **Not “models refuse the task.”** Several correctly restate sample-size math (90%, z=1.645, n≈68) and the selection criteria — then fail to operationalize them in-sheet.
- **Not missing Population data.** Sheet1 is present (~1516 data rows) in every workbook; the miss is transformation + stop discipline.

**Bottom line for harness work:** treat this eval as a **deliverable-completeness** problem under Calc tools, not a deal-debug remnant. Ready is currently a participation trophy.
