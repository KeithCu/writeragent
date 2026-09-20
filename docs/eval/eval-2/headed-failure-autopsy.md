# Eval-2 headed failure autopsy

**Audience:** Keith / Chief / Scrolly / harness owners  
**Status:** Living harness-debug note (not a KPI scoreboard). **Overnight queue (slots 6/8/9/10) folded 2026-09-09 — morning-ready.**  
**Bar:** **Product/task success** first. Oracles are for later benchmarking; a headed “HAPPY” means the deliverable did the job even if a soft oracle is red. Prefer **DO + why**.

Box run artifacts cited below may be untracked locally; paths are under `docs/eval/eval-2/*/runs/` or `docs/eval/eval-2/runs/` on the shared machine.

**Skip:** slot **#7** `writer-headed-template/` (PARKED).

---

## 1. Snapshot (headed product bar)

| # | Sibling | Headed stamp(s) | Product bar | Oracle | Dominant failure class |
|---|---------|-----------------|-------------|--------|------------------------|
| 3 | AFC Population | `runs/20260908-0030-…-retry2` (Gemini) | **HAPPY** (Sample+SSC, S=65 R=2) | PASS | Earlier thrash = fixture/prompt key mismatch (smoothed) |
| 1 | Tenant Retention | `tenant-…/runs/20260908-0121-…` (Gemini) | **HAPPY** (real memo) | FAIL → softened | Oracle false-red (titles in `text:h`, table cells, length) |
| 2 | Cadaver Proposal | `cadaver-…/runs/20260908-2246-…` (Gemini) | **HAPPY** (real proposal + charts) | FAIL → soften PR | Oracle false-red (aliases, Figure/draw:frame, length) |
| 4 | GMP Change Control | `gmp-…/20260909-0103` then `…-0225` (gpt-oss-120b) | 0103 **NOT HAPPY** → 0225 **HAPPY** | 0225 FAIL cite only | **Peer polarity** dump→fill; then product OK |
| 5 | Floorstand Writer→Calc | `0323` gpt-oss; private `1733` gpt-oss; **`1748` Gemini private-patch** | **NOT HAPPY** (all) | FAIL (empty) | `1748`: polarity **HIT** but empty+stall; gpt-oss still extract MISS |
| 6 | Calc-primary | `calc-primary-model/…/20260909-0400-…` (gpt-oss-120b) | **NOT HAPPY** | FAIL (Regions A–G) | **CSV-row dump** into col A + wrong factor + `#NAME?` invented sheet |
| 8 | Draw-primary | `draw-primary-deliverable/…/20260909-0411-…` (gpt-oss-120b) | **NOT HAPPY** (garbled layout) | FAIL (Clearbend / failure / triage) | Non-empty map; **identity labels + layout overlap** |
| 9 | Reverse Tenant | `reverse-tenant/…/20260909-0414-…` (gpt-oss-120b) | **NOT HAPPY** (blank Sheet1) | FAIL (0 cells) | **Talk-not-write** + PreContractError; Sheet2/3 only in chat |
| 10 | Long Writer pack | `long-writer-pack/…/20260909-0419-…` (gpt-oss-120b) | **NOT HAPPY** (wrong facts) | FAIL (money/dates/purpose/comment) | TOC+headings OK; **invented budget/dates**; **0 comments** |

---

## 2. Cross-cutting finding: peer ask polarity

Two Writer→peer mutation siblings exist. Same machinery (`send_peer_work/send_peer_result` / live peer sidebar). Opposite outcomes.

### 2.1 GMP — dump then fill (product recovered)

**`20260909-0103-gpt-oss-120b` (NOT HAPPY)** — tip `172a36b6` (#680-era master)

- Writer peer-asked Draw: *“Please provide the blank Change Control Tracking Form content (all pages) so I can fill it.”*
- Polarity = **dump/research**, not **fill**.
- Form: `filled_fields=0`. Memo ~987 words with risk content; assistant **claimed** form filled (false).
- No UNO thread-violation dialog. ~26+ min to Ready; Save As needed (Ctrl+S missed trial-dir paths).

**`20260909-0225-gpt-oss-120b` (HAPPY)** — tip base `c91f6378` + local `str_bounded` peer-task truncate (not yet the cloud PR)

- Peer ask: *“Please update the Change Control Tracking Form with the following information: … Please enter these details into the appropriate fields of the Change Control Tracking Form. Leave any fields that require unavailable data blank.”*
- Polarity = **fill-not-dump**.
- Form: `filled_fields=9`, `filled_chars=1497`. Memo ~601 words with required sections.
- Oracle only: “memo does not cite the filled change-control form” — **secondary** vs headed happy bar.
- Also needed a private `str_bounded` (not `ascii_bounded`) truncate fix so long peer tasks did not crash Deal — product/plumbing, not oracle.

**Why 0225 worked:** explicit fill instruction + payload of field values in the peer task; Draw treated as write surface.

### 2.2 Floorstand — extract-not-fill (still broken) — DEEPEST

**`writer-calc-peer-write/runs/20260909-0323-gpt-oss-120b` (NOT HAPPY)**

Evidence (`notes.txt`, `thinking_and_tools.md`, `score.txt`, `/workspace/fs-*.png`):

- Tip `891cd670` (#688). Model gpt-oss-120b:nitro, `max_tool_rounds=150`.
- Writer fired `send_peer_work/send_peer_result` **3×**. Asks were to **extract** component-by-component cost **JSON** from the **empty** budget scaffold / email trail — e.g. extract breakdown, return JSON array.
- Calc: `get_sheet_summary` / `read_cell_range` on empty Cost Comparison; more extract asks.
- Workbook stayed scaffold: **`nonempty_cells=2`** (titles only).
- Writer email **blank** (`words: 0`).
- LO/OXT **crashed** after peer round (soffice gone); **0** UNO thread violations / PreContractError.
- Oracle FAIL is honest here — empty deliverables — not a false-red.

**Contrast with GMP:** Draw form has obvious blank fields to fill. Calc scaffold looks like a **sheet to read**. Models default to research polarity unless the peer task says **write cells / fill Cost Comparison**.

**Exact peer asks (observer notes):** Writer asked Calc to *“Please extract the component-by-component cost breakdown … Return a JSON array…”* (×3). Calc then `get_sheet_summary` / `read_cell_range` on empty Cost Comparison. ComputerUse saw assistant text about an empty JSON `[]`. Tip `891cd670` (#688); model `openai/gpt-oss-120b:nitro`; `max_tool_rounds=150`; UNO thread violations **0**; soffice died after peer.

**Prompt already says fill** (`prompt_used.txt` / notes): *“The holiday floorstand budget workbook is already open… Fill that workbook.”* and *“Write the draft email in this open Writer document.”* Product still failed — **eval prompt fill language alone is not enough** when `send_peer_work/send_peer_result` tasks default to extract/JSON. GMP recovered only when the **peer task** said update/enter-into-fields (0225), not when the Writer prompt alone said fill.

### 2.3 Solutions (DO + why) — peer polarity

Ordered by leverage for Floorstand / future Writer→Calc:

1. **DO — Peer-task polarity examples in product prompts (Calc + Draw)**  
   Teach: when the sibling is an **empty write target**, `send_peer_*` tasks must say **update/fill/write cells** (or Draw fields), not extract/return JSON/dump blank content.  
   **Why:** 0103 vs 0225 is the controlled experiment; Floorstand is the same miss on Calc.

2. **DO — Harness/eval one-liner in Floorstand `prompt.writeragent.txt` only if (1) is not enough**  
   Gold-shaped: “The budget workbook is empty until you fill it; do not ask Calc only to extract costs.”  
   **Why:** eval-2 already remaps deliverable location; a polarity sentence is harness debug, not KPI gaming — but prefer product prompt (1) so all Calc peers benefit.

3. **DO — Peer-tool description / schema hint**  
   `send_peer_work/send_peer_result` description: for writable peers, prefer imperative fill tasks; research of siblings stays `document_research`.  
   **Why:** tool messages train harder than eval prompts (AFC lesson).

4. **DO — Soft telemetry in headed notes**  
   Tag peer asks `fill` vs `dump/extract` from keywords; Scrolly notes “polarity MISS” without waiting on oracle.  
   **Why:** Floorstand oracle FAIL is late; polarity is visible mid-run.

5. **Investigate LO crash after peer (Floorstand)**  
   Separate from polarity; may be long peer / nested drain / OXT. Reproduce with a tiny fill peer ask.  
   **Why:** crash ends the run even if polarity is fixed.

6. **Don’t** gate interactive Ready on peer polarity. **Don’t** add gold dollar totals to the product prompt.

### 2.4 Floorstand private-patch retest — polarity HIT, product still empty (Gemini)

**Keith gate:** eval-2 / GDPval headed = **Gemini 3.8 Flash** until HAPPY; gpt-oss is not the product gate.

**Private local patches** (tip `a58ab1ad`, debug OXT on `writeragent-master`, **not PR’d** — `.bak-scrolly-*`):

1. `PEER_INNER_CHOICE_RULES` (+1 sentence): when the peer doc is meant to be written (empty sheet/form), ask it to **write/fill and include the values** — don’t only extract/return JSON.
2. `write_formula_range` description (+1 sentence): a table row is **one value per cell**; don’t put a whole comma-joined row in one cell (Calc-primary CSV-dump lesson).

| Stamp | Model | Polarity | Product | Notes |
|-------|-------|----------|---------|-------|
| `20260909-0323-gpt-oss-120b` | gpt-oss | MISS (extract/JSON ×3) | empty + **LO crash** | Overnight baseline |
| `20260909-1733-gpt-oss-120b-private-patch` | gpt-oss + patches | **MISS** (still extract/JSON) | empty email + titles-only tabs | Patches did **not** flip gpt-oss polarity |
| `20260909-1748-gemini-3.8-flash-private-patch` | **Gemini 3.8 Flash** + patches | **HIT** (fill/write asks) | **still empty** + **hard stall >3 min** | No LO crash; UNO=0; PreContract=0; no `*_SAVED` |

Paths (box): `docs/eval/eval-2/writer-calc-peer-write/runs/…` under Scrolly’s `writeragent-master`; shots `/workspace/fs3-*.png` (Gemini), `/workspace/fs2-*.png` (gpt-oss private), `/workspace/fs-*.png` (overnight).

#### Gemini `1748` product evidence

- Oracle / ODS: `nonempty_cells=2` (Cost Comparison + Final Store List **titles only**); `words: 0` email; `formulas: 0`.
- Observer: peer asks were **fill/write**, not extract/JSON — polarity telemetry **HIT**.
- UI shots (`fs3-sent` / `fs3-calc`): sidebar **Thinking…** with **blank chat transcript** and blank email (0 words); later `fs3-final`: **Ready**, still blank email, chat UI empty. Calc still in taskbar.
- computerUse: hard-stall **>3 min** after send → stopped; did not create `*_SAVED` copies (scored live trial files).
- Debug log for this run was **rotated away** at `17:48:34` when LO restarted into Calc-primary Gemini (`Debug log active` resets the file). Floorstand tool-call trace is **not** in the live `writeragent_debug.log` — reconstruct from notes + shots only. (Do not confuse post-17:48 Calc-primary `Raw Data` / `peer_count=0` lines with Floorstand.) **Standing rule:** save the full debug log for every headed run — `--launch` exit snapshots it; mid-stall use `scripts/save_eval2_debug_log.py DEST_DIR` before restart. See [§ Debug log snapshot](#debug-log-snapshot-standing-rule).

#### Ranked causes — why fill polarity still left empty sheets + blank email + stall

1. **Writer hard-stall / incomplete outer loop (highest confidence for “empty + Thinking”)**  
   Shots show prolonged **Thinking…** with empty transcript and **0-word** email; computerUse aborted after >3 min. Outer Writer never reached a finished “peer filled → draft email here” end state.  
   **Why empty both sides:** if Writer stalls before/during peer wait, Calc may never get a completed drain cycle *or* Writer never drafts the email after peer returns. Matches blank Ready at the end (UI recovered / stopped) with scaffold unchanged.

2. **Fill ask without value payload (high — product teaching gap)**  
   Private `PEER_INNER` already says include values. GMP-0225 HAPPY peer asks carried **concrete field values**. A polarity-HIT ask that only says “please fill Cost Comparison” (no shelf-strip delta / store counts / cost lines) leaves Calc with nothing to write → titles-only scaffold.  
   **Why:** polarity telemetry keys on fill/write verbs; product needs **verbs + numbers**.

3. **Calc peer did not execute durable writes (medium — consistent with titles-only ODS)**  
   Even with a fill ask, peer may have researched (`document_research` / read trail) or hung mid-tool-loop without `write_formula_range` on Cost Comparison. Cannot confirm tool names without the rotated log. Outcome matches research-only / no-write peer: `nonempty_cells=2`.

4. **Nested peer drain / wait-forever (medium — explains stall, not polarity)**  
   Writer `send_peer_work/send_peer_result` → Calc sidebar drain → Writer waits. Nested drain / stuck LLM stream can leave Writer on Thinking with no transcript paint (UI grey box). Related to overnight LO crash class but here **no crash** — soft hang instead. Later Calc-primary log shows at least one Gemini stream `finish_reason=None` / `used_model='unknown'` (different run; signal that Gemini streams can die oddly).

5. **Wrong peer / peer not on wire (lower for this stamp)**  
   Observer recorded fill asks to the budget peer; workbook is the open Cost Comparison scaffold. Post-restart logs show `peer_count=0` on Calc-primary alone — not evidence for Floorstand. Keep as a check if a future run logs `on_wire=False` while two docs are open.

6. **Ruled out / secondary**  
   - **Polarity MISS:** ruled out for Gemini `1748` (HIT). Still true for gpt-oss `1733`/`0323`.  
   - **CSV-in-one-cell:** N/A — nothing written.  
   - **UNO / PreContract:** 0 / 0.  
   - **LO crash:** none on `1748` (progress vs overnight).  
   - **Eval oracle false-red:** no — empty is honest.

#### Light product next steps (few / small; minimal eval cheats)

1. **DO — Keep the private `PEER_INNER` polarity sentence** for Gemini (it flipped polarity vs gpt-oss). Land as a tiny product PR when Scrolly’s tree is free — don’t block on gpt-oss still missing.  
2. **DO — One more general peer sentence: fill asks must carry the values to write** (counts, shelf-strip +$0.25, line labels) — mirror GMP-0225 payload style. Prefer `PEER_INNER` / `send_peer_work/send_peer_result` description over Floorstand eval gold cheats.  
3. **DO — Outer Writer: after peer returns, write the email in this doc; if peer workbook still titles-only, one retry peer ask with explicit numbers — then draft what you can.** Small prompt; fights Ready-empty + stall-without-email.  
4. **DO — Stall plumbing (separate from polarity)** — fail-loud / timeout when peer drain or LLM stream hangs >N minutes; preserve debug log across LO restarts for headed trials. Not an eval cheat.  
5. **Don’t** add gold dollar totals to `prompt.writeragent.txt`. **Don’t** wait on gpt-oss polarity for Gemini gating. **Don’t** fight Scrolly’s `writeragent-master` deploy while Calc-primary Gemini runs.

**Next Gemini headed:** same private patches + value-payload peer teaching; expect nonempty Cost Comparison **and** nonempty email; capture `thinking_and_tools` + a full `writeragent_debug.log` in the stamp dir (do not rely on the live profile file after the next LO restart).

---

## Debug log snapshot (standing rule)

Keith: **save off the full `writeragent_debug.log` for every headed / eval-2 run.** The live file lives next to `writeragent.json` in the LibreOffice user profile. A later LO restart re-inits logging (`Debug log active`) and can reset that file — that is how Floorstand Gemini `1748` lost its tool-call trace.

- **`--launch` exit:** Enter / Ctrl-C after the trial copies the live log via `shutil.copy2` into `--run-dir/writeragent_debug.log`, or (if `--run-dir` is omitted) `docs/eval/eval-2/<task>/runs/<YYYYMMDD-HHMM>/writeragent_debug.log`. A missing live log warns; config restore still runs.
- **Mid-stall (before restart):** `.venv/bin/python scripts/save_eval2_debug_log.py DEST_DIR` — prints the source path and dest size; exits non-zero if the live log is missing.
- Discovery is shared with `scripts/analyze_tool_call_timing.py` (`eval_2_debug_log.py`; same profile dirs as `writeragent.json` / `DEBUG_LOG_FILENAME`). Do not change live-log format or truncate behavior.

---

## 3. Single-doc Writer: Tenant & Cadaver (product OK)

Both Gemini headed drafts were **real product solves**. Oracles failed on extraction/alias brittleness; softens documented elsewhere (#665 / Cadaver soften).

| Issue | Tenant | Cadaver | Product? |
|-------|--------|---------|----------|
| Section titles in `text:h` only | yes | yes | False-red if oracle reads only `text:p` |
| Counts as table cells / `45.0%` | yes | — | False-red |
| Word band slightly over | 1476>1400 | 2505>2500 | Soft |
| Fee / duration aliases | — | Facility Fee; 60–90 min | False-red |
| Graph as `draw:frame` + “Figure” | — | yes | False-red if text-only |

**DO:** keep oracle softens for benchmarking. **Don’t** treat those reds as product misses (Keith: product over oracle).

---

## 4. AFC Population (product OK after smoother)

- Early Gemini thrash: searched GDPVal entity/KRI strings **not in the fixture** (`SMOOTHER_CHANGES.md`).
- After sheet rename `Population` + Legal Entity / KRI remaps: **`20260908-0030-…-retry2` oracle PASS** (S=65 R=2).
- Earlier `20260908-0009` exhausted mid-stream with weak J/K evidence — process/hung-final, not the same as peer polarity.

**DO:** keep smoother changelog so GDPVal-hard criteria can be restored later. Round budget ~150 mattered for polish; pair with error brake if storms return.

---

## 5. Predicted miss modes (no headed yet) — slots 6 / 8 / 9 / 10

From each sibling `notes.md` + soft oracles after #693. Fold real stamps into this section when Scrolly lands runs.

### 6 — Calc-primary (branch profitability) — LANDED NOT HAPPY

**`calc-primary-model/runs/20260909-0400-gpt-oss-120b`** — tip `7a30dcb4`; model `openai/gpt-oss-120b:nitro`; max 150; ~8–10 min; UNO=0 / PreContract=0; WriterAgent **Error** twice then recovered. Shots: `/workspace/calcprim-*.png`.

**What worked (rules out empty-tab / pin):**
- Sheets created: Raw Data + **5** schedules (Income Statement, Monthly Trend, Branch Ranking, Regional Comparison, Efficiency Volume Profitability).
- Oracle: `schedule_sheets: 5`, `formulas: 208`, **`pinned_columns: 0`**, husks 1/243.
- Not the predicted empty-`create_sheet` miss. Not the AFC-style pin miss.

**Product NOT HAPPY — three real miss classes:**

1. **CSV-row dump (dominant / new)** — whole header and even formula rows written as **one comma-joined string in column A** instead of cell-per-column. Visible on Efficiency Volume Profitability (`calcprim-final.png`: `Implementation Headcount Hours…,0,0,0,…` in A4+; B–J empty bordered husks). ODS also has CSV dumps on Income Statement / Monthly Trend / Branch Ranking / Regional Comparison (header lines and `Revenue,=SUMIFS(...),=SUMIFS(...)` as literal text).  
   **Why:** model treated Calc write like pasting a CSV line; schedules look “filled” in chat but are unusable grids.

2. **Wrong factor → `#DIV/0!` (×20 on Branch Ranking)** — ARPU-style formulas divide Revenue by SUMIFS criteria **`"Units"`** instead of fixture account **`Revenue (Units)`** (denom zero / no match). Predicted miss mode confirmed.

3. **`#NAME?` (×20)** — `VLOOKUP(...;'headcount'.a:b;2;0)` references an **invented sheet** `headcount` that does not exist. Related product bug: Sales $/Headcount without a real headcount source tab.

4. **Sparse Regions A–G** — oracle: *missing Regions A–G (found A only)*. Regional Comparison under-filled vs prompt §4.

**Solutions (DO + why):**

1. **DO — Cell/range write teaching (product prompt + tool description)**  
   One cell or rectangular range per write; never dump a CSV/comma row into a single cell. Mid-run: if `get_sheet_summary` sees long comma-joined A-column strings with empty B+, treat as actuation fail and rewrite.  
   **Why:** CSV-dump is the headed happy-bar killer even when sheet count and formula count look OK.

2. **DO — Factor tokens from fixture in teaching / oracle already soft-fails**  
   Denominators must use exact Raw Data account labels (`Revenue (Units)`, Implementation Hours, …) — not shortened `"Units"`.  
   **Why:** `#DIV/0!` ×20 is wrong-factor, not Ready noise.

3. **DO — Ban invented helper sheets** unless `create_sheet` + populate first  
   Headcount for Sales $/Headcount must come from Raw Data headcount accounts (or an explicitly created sheet), never a phantom `headcount` tab.  
   **Why:** `#NAME?` VLOOKUP to missing sheet.

4. **DO — Regional Comparison completeness check in headed notes**  
   Observer tags Region A–G presence before Ready (oracle already fail-closes).  
   **Why:** found A only is a sparse-schedule miss.

5. **Don’t** call this an empty-tab or pin miss. **Don’t** soften Regions A–G away — product package is incomplete. Soft oracle can still keep husk/#DIV thresholds as secondary once grids are real cells.

**Next headed (after product write fix):** re-run same model; expect real multi-column grids, Regions A–G labels, no CSV-in-A, ARPU denom = `Revenue (Units)`.

### 8 — Draw-primary (process map) — LANDED NOT HAPPY

**`draw-primary-deliverable/runs/20260909-0411-gpt-oss-120b`** — tip `c9ad7a08`; model `openai/gpt-oss-120b:nitro`; **max 50**; observer ~**10s**; UNO=0 / PreContract=0; no LO crash. Shots: `/workspace/drawprim-*.png`.

**What worked (rules out title-only / no-connectors):**
- Oracle: `labeled_shapes: 20`, `connectors: 13`, `labeled_chars: 354`.
- Automation + Manual lanes, start/end, decision diamonds, scan/sort/packaging, connectors.
- Not the predicted empty / title-only husk.

**Product NOT HAPPY:**

1. **Layout overlap / garbled** — overlapping titles; decision text **one character per line** vertically (`drawprim-final.png`); dense stacking. Leadership-usable map fails despite healthy shape counts.
2. **Identity labels missing:** **Clearbend Logistics Hub** (0 hits); **automation failure/exception/jam** path absent; **manual triage/rework** absent (manual lane = handling/inspection/packaging only).

**Solutions (DO + why):**
1. **DO — Clearbend Logistics Hub in map title/header** — gold facility claim, not soft alias.
2. **DO — Explicit failure→manual edge** (jam/no-read/reject) — prompt’s overflow story was dropped in the 10s one-shot.
3. **DO — Manual triage/rework step** — oracle check 11 / ad hoc manual pain point.
4. **DO — Draw layout teaching** — wrap text in-box (not vertical char-per-line); one title; gap lanes; `get_draw_tree` before Ready.
5. **Investigate early Ready** — max 50 / ~10s suggests one-shot; retest at 80–150 only after (1)–(4); don’t raise everyday default.
6. **Don’t** soften Clearbend / failure / triage. **Don’t** score a Writer memo as the map.

**Next headed:** identity + layout teaching; readable non-overlapping Clearbend map with failure→manual + triage/rework.

### 9 — Reverse Tenant (Theatre CBA) — LANDED NOT HAPPY

**`reverse-tenant/runs/20260909-0414-gpt-oss-120b`** — tip `c9ad7a08`; model `openai/gpt-oss-120b:nitro`; **max 150**; **under 1 minute**; UNO=0; **PreContractError=1**; no LO crash. Shots: `/workspace/revten-*.png`.

**Outcome:** Sheet1 **blank only** (`scored_cells: 0`, `sheets: Sheet1`, no formulas). Chat essay describes **Sheet 2** contract rates + **Sheet 3** totals formatting — **no sheets created, no cells written** (`revten-final.png`). Oracle: missing CBA/theatre/roster anchors; `pay_categories` / `instruments` empty; `brief_present: False` (score-time brief path — secondary to empty workbook).

**Miss class:** **Talk-not-write / Ready-empty** (predicted blank workbook Ready) + tool **PreContractError** (one Deal reject) — opposite of invent-rates-with-content. Model narrated a multi-sheet payroll template instead of actuating Calc.

**Solutions (DO + why):**
1. **DO — Actuation-first / refuse Ready on empty Sheet1** — write ≥12 cells (rates + roster labels) before Ready; chat formatting tips are not a deliverable.  
   **Why:** under-1-min Ready with blank grid is the happy-bar fail.
2. **DO — Dig the PreContractError** — which tool/args Deal rejected; fix schema teaching or caller so writes aren’t blocked mid-plan.  
   **Why:** PreContractError=1 with 0 cells suggests a failed write path, not a finished model.
3. **DO — `create_sheet` + cell writes for rates/roster tabs** — if the design needs Sheet2/Sheet3, create and populate them; never leave them as chat fiction.
4. **DO — Read CBA Writer sibling before inventing** (`document_research` / open brief) — rates must come from the excerpt; blank grid also means brief was unused.
5. **Don’t** soften 0-cell oracle. **Don’t** treat chat Sheet2/3 prose as product success.

**Next headed:** after PreContract dig + actuation teaching; expect populated payroll model (≥12 cells, CBA/theatre/roster anchors, real multi-sheet or single-sheet tables).

**Watcher (overnight):** **queue complete** — calc `0400`, draw `0411`, reverse-tenant `0414`, long-writer `0419` all folded (all NOT HAPPY on gpt-oss-120b overnight).

### 10 — Long Writer pack — LANDED NOT HAPPY

**`long-writer-pack/runs/20260909-0419-gpt-oss-120b`** — tip `6670b802`; model `openai/gpt-oss-120b:nitro`; **max 50** (START ×2 — prompt auto-submitted during sidebar transition); ~3 min; UNO=0 / PreContract=0. Shots: `/workspace/longpack-*.png`.

**What worked (rules out empty / no-TOC / no-headings):**
- Oracle: `words: 400`, `paras: 63`, `headings: 13`, **`toc: True`**, substantial multi-section brief (~3 pages).
- Named sections exist (Executive Summary, Scope, Budget, Timeline, Open Decisions, …). Northhaven / CL-2026 identity present.
- Not the predicted “no TOC / bold-as-heading husk” miss.

**Product NOT HAPPY:**

1. **Invented program facts** (dominant) — body uses **$12.5M total / Renovation $5.0M** and **Q4 2026 → Q4 2027 → Q1 2028** instead of fixture **$4.2M** base, annex **$1.8M / $2.4M**, **April 2027–October 2028**. Oracle fail-closes on those anchors (correct — wrong capital brief).
2. **Missing purpose / scope section theme** — has “Scope” / Project Description but not the prompt’s Purpose + Scope of Work pairing the oracle accepts.
3. **Zero review comments** (`comments: 0`) — prompt required comments on annex siting / funding split / weekend staffing; Decision Log content was prose, not `office:annotation` on a real span.

**Solutions (DO + why):**
1. **DO — Research-before-write** — `document_research` (or open) **Northhaven Library Program Facts** + **Decision Log** before drafting numbers/dates; refuse Ready if $4.2M / annex options / Apr2027–Oct2028 absent.  
   **Why:** structural TOC success hid a fact-fabricated brief.
2. **DO — Exact figure/date copy from fixtures** — don’t round to alternate capital stories ($12.5M / quarterly labels).  
   **Why:** oracle = product identity for native pack.
3. **DO — Real review comments on spans** — `add_comment` (or equivalent) on open-decision passages, not a Decision Log section alone.  
   **Why:** comments=0 fails the Decision Log intent.
4. **DO — Purpose / Scope of Work heading pair** — match prompt section list (or aliases oracle already accepts).  
5. **Don’t** soften money/date fails. **Don’t** treat toc=True alone as HAPPY.

**Next headed:** after research-before-write teaching; expect fixture dollars/dates, purpose/scope theme, ≥1 real review comment.


---

## 6. Ordered next experiments

1. **Floorstand polarity retest** (highest product pain) — product peer-prompt/tool description from §2.3 (1)+(3); same model; expect fill peer asks + nonempty Cost Comparison + non-empty email.  
2. **Calc-primary CSV-dump / factor retest** — after cell-write teaching (§5.6); same model; expect multi-column grids, Regions A–G, no comma-joined A cells, ARPU denom `Revenue (Units)`, no phantom `headcount` sheet.  
3. **Tiny peer-fill crash repro** — one `send_peer` that writes 3 cells; see if LO still dies.  
4. **GMP oracle cite soften** (optional) — secondary; product already HAPPY.  
5. **Draw-primary identity + layout retest** — after §5.8; Clearbend title, failure→manual, triage/rework, readable layout (consider max 80–150 only if still one-shot).  
6. **Reverse Tenant actuation retest** — after PreContract dig + refuse-empty-Ready; ≥12 cells, CBA/theatre/roster anchors, real sheets not chat fiction.  
7. **Long Writer fixture-faithful retest** — after research-before-write; $4.2M + annex $1.8M/$2.4M + Apr2027–Oct2028 + purpose/scope + ≥1 review comment on a real span.  
8. Keep folding Scrolly stamps into §1 / §5; amend this file or open follow-up PRs.

---


## 6b. Overnight queue wrap (2026-09-09 morning)

Scrolly gpt-oss-120b headed stamps for Ready slots **6 / 8 / 9 / 10** all landed **NOT HAPPY**. Prior siblings unchanged: AFC / Tenant / Cadaver / GMP-0225 product HAPPY; Floorstand + GMP-0103 peer polarity; then:

| Stamp | Dominant miss | First fix lever |
|-------|---------------|-----------------|
| Calc-primary `0400` | CSV-row dump into col A + wrong ARPU factor + phantom `headcount` | Cell/range write teaching; fixture factor tokens |
| Draw-primary `0411` | Garbled layout + missing Clearbend / failure / triage | Identity labels + layout; maybe more rounds after teaching |
| Reverse Tenant `0414` | Talk-not-write blank Sheet1 + PreContractError | Refuse empty Ready; dig PreContract; real sheets |
| Long Writer `0419` | Invented $12.5M / wrong dates; 0 comments | Research-before-write; fixture dollars/dates; real annotations |

**Morning priority order (product pain):** Floorstand **value-payload peer + stall** (polarity HIT on Gemini `1748` still empty) → Calc CSV-dump → Reverse Tenant empty → Long Writer facts → Draw identity/layout. Oracles stay for benchmarking; do not KPI-game softens on these four.

## 7. Non-goals

- Closing GitHub issues from PR keywords.  
- Editing `docs/eval/gdpval/` gold trees.  
- Treating oracle soft fails as product regressions when headed bar is HAPPY.  
- Un-parking #7 in this note.  
- Inventing peer spawn / PDF AcroForm product claims.

---

## Appendix — artifact index

| Sibling | Path |
|---------|------|
| Floorstand | `0323` + `/workspace/fs-*.png`; private `1733` + `fs2-*.png`; Gemini private `1748` + `/workspace/fs3-*.png` (under `writeragent-master` runs/) |
| GMP | `gmp-change-control-58ac1cc5/runs/20260909-0103-…` and `…-0225-…` + `/workspace/gmp*.png` |
| Tenant | `tenant-retention-ed2bc14c/runs/20260908-0121-…` |
| Cadaver | `cadaver-proposal-61b0946a/runs/20260908-2246-…` |
| AFC | `docs/eval/eval-2/runs/20260908-0030-…` (+ smoother `afc-sample-83d10b06/SMOOTHER_CHANGES.md`) |
| Calc-primary | `calc-primary-model/runs/20260909-0400-gpt-oss-120b/` + `/workspace/calcprim-*.png` |
