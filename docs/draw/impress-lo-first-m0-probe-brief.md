# Impress LO-first M0′ / UNO-probe farm brief

**Status:** Farmable probe brief (docs only). Do **not** land product code in the probe PR.  
**Source plan:** [ppt-master-steal-for-small-models.md](ppt-master-steal-for-small-models.md) **rev 4** (master `01b4841c`).  
**Related:** [impress-specialized-toolsets.md](impress-specialized-toolsets.md) **Themes** ❌ Missing (color/font schemes) and **Templates** ❌ Missing (document templates); [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md).  
**Doctrine:** LO-first — use shipped Impress features before inventing. Themes stay **probe-gated**. This brief’s scope is measurement + UNO inspection, not a design engine.

> Another agent should be able to run this without reading the whole steal doc. Steal-doc §5 **M0′** + §3.8 + §6 are the source of the numbers and probe questions.

---

## Goal

Two parallel deliverables, same LO-first question:

1. **M0′ headed 3-arm probe** — can `inception/mercury-2.5` get **non-generic polish** using stock LibreOffice slide designs + master / layout / placeholder / headers-footers (no WriterAgent design engine)?
2. **Parallel UNO probe** — how to **enumerate and apply** shipped `.otp` slide designs, how to set **page/master background**, and whether any **ODF Theme API** exists on this LO. Written result, not guessed wrappers.

If arms B/C pass the criteria below, next work is **M1′** (expose list/apply design + core `set_presentation_design`). If they fail, the write-up of *which* LO feature failed **is** the LO wall for **M3′** — do not invent `deck.py` / `DeckTheme` in this brief’s scope.

---

## Prompt (same text, all three arms)

There is **no** formal eval-2 Impress space-elevator task under `docs/eval/`. [`docs/eval/eval-2/draw-primary-deliverable/`](../eval/eval-2/draw-primary-deliverable/) is a **Draw** process-flow map (Clearbend) — do **not** substitute it.

Use the headed space-elevator lead task from [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md):

```text
Create a multi-slide presentation about the space elevator.
Then, on the same deck: edit an existing slide, add one structural slide,
and add one visual shape.
```

**Setup (from the findings run):** WriterAgent debug deploy; **`inception/mercury-2.5`** only; **new Impress** presentation; WriterAgent-only (LibreHarper + WriterAgent ChatPanel co-install is a known sidebar footgun). Record shots + tool/debug logs per arm.

---

## Arms

| Arm | What to do | How if tools are missing |
|-----|------------|--------------------------|
| **A** | Baseline **title + bullets** (current main-chat behavior). | Chat tools only. |
| **B** | Same prompt **+** real **master** + **layout** + **placeholders** + **headers/footers / slide numbers**. | Use existing tools if they fire; **manual or scripted LO steps are OK**. |
| **C** | Same prompt **+** an **applied shipped slide design** + **master/page background**. | **Manual or scripted apply is OK** — M1′ `list_designs` / `apply_design` may not exist yet. |

Existing tools to prefer (do not replace them):

| Need | Tools | Where |
|------|-------|--------|
| Masters | `list_master_slides`, `get_slide_master`, `set_slide_master` | specialized `slide_masters` — `plugin/draw/masters.py` |
| Layouts | `add_slide` (core, default `text`); `get_slide_layout` / `set_slide_layout` | `plugin/draw/pages.py`, specialized `slide_layouts` — `plugin/draw/transitions.py` |
| Placeholders | `list_placeholders`, `get_placeholder_text`, `set_placeholder_text` | **core** — `plugin/draw/placeholders.py` (fill **after** layout; headed `available: []` was fill-before-layout) |
| HF / slide numbers | `get_headers_footers`, `set_headers_footers` | specialized `headers_footers` — `plugin/draw/headers_footers.py` |

A fresh doc’s only master is often `Default`. Arm B can assign a master **if one exists**; arm C is the arm that **brings in** a shipped design so a non-blank master exists.

---

## Pass criteria (numeric — steal doc §6)

Score **each** arm. Polish is falsifiable — not a vibe.

1. `blank_master == false` (a real design/master is applied; not still `Default` / unnamed blank).
2. Placeholder coverage ≥ **80%** of title+body placeholders on **content** slides.
3. Slide numbers / HF **on** when the prompt / DECK MODE asked for them.
4. `polish` sub-score **improves vs arm A**; `text_overlap_count` / `one_char_line_count` **not worse**.
5. Blind A/B: reviewer prefers **B or C over A**.

`scripts/eval_2_draw_oracle.py` today scores Draw `.odg` process maps. Impress LO-object fields (`blank_master`, placeholder coverage, HF) are **not shipped yet**. Score this probe by **UNO inspection + reviewer polish**, not by running `--task draw-primary` on Clearbend. Steal-doc §6 still lists the intended oracle fields for a later M0′/M2′ extension — do not invent that extension in this probe unless scoring is otherwise blocked.

If B or C fails, write **which LO feature failed** (apply-design? master assign? background fill? HF? placeholders after a real design?). That paragraph is the **LO wall** for M3′.

---

## UNO probe deliverable

Inspect on a **live Impress** instance. Per `AGENTS.md`, use `plugin/testing_runner.py` (not a guessed `test_runner.py`). Pattern: write a throwaway `@native_test` that takes `ctx`, instantiate the service, `dir()` / call it. Existing Impress probes: `tests/draw/test_placeholders_uno.py`, `tests/draw/test_impress_headers_footers_uno.py`, `tests/draw/test_impress_uno.py`.

```bash
.venv/bin/python plugin/testing_runner.py tests/draw/test_placeholders_uno.py
```

Deliver a **written result** (paths that work / don’t). Do **not** land Theme API wrappers or `plugin/draw/designs.py` in this probe.

1. **Enumerate shipped designs.** Discover via LO `PathSettings` / `Paths` template path. Typical on-disk location is `share/template/common/presnt/*.otp` (23 designs on the steal-doc machine: Metropolis, Midnightblue, Sunset, Vivid, …). **Discover; do not hardcode** the install path.
2. **Apply-design path candidates** — which works on the **current** doc vs **new doc only**:
   - `XStyleLoader.loadStylesFromURL` (`com.sun.star.style.XStyleLoader`)
   - master-page copy / supply
   - create-from-template
3. **Master/page background fill** (solid / gradient / bitmap): record **live property names** on the draw page / master page (steal doc points at a `Background` property — confirm; do not guess).
4. **Theme / color-scheme API for ODF Impress.** Expect **none** (shipped `.otp` look lives in `styles.xml` master + named styles; LO “theme” is likely PPTX-import). Record yes/no. This decides whether [impress-specialized-toolsets.md](impress-specialized-toolsets.md) **Themes** ❌ stays a gap or is an OOXML-only non-gap.

---

## Keep

- Existing masters / layouts / placeholders / HF tools (table above).
- Steal-doc **non-goals** ([§8](ppt-master-steal-for-small-models.md)): no SVG pipeline in main chat; no SKILL / `references/*.md` in prompts; no competing with sidebar PPT-Master (Route A); no Calc polish transfer.

---

## Don’t

- Build `plugin/draw/deck.py`, `DeckTheme` / `DECK_THEMES`, `compose_slide`, `render_deck_plan`, host chrome/folio/full-bleed shapes, or Theme API wrappers **before the probe says an API exists**.
- Paste ppt-master SKILL / `references/` into prompts.
- Merge PRs or close issues.

---

## Done when

1. **Headed report** with A/B/C shots + scores vs the five pass criteria (and tool/debug logs per arm).
2. **UNO probe note**: enumerate path; which apply-design candidate works on current vs new doc; background property names; Theme API **yes/no**.
3. **Recommendation:** proceed **M1′** (list/apply shipped design + core `set_presentation_design`: design + master + HF) **or** declare an **LO wall** for a specific gap (feeds M3′ only).

Prefer landing the headed report + UNO notes at `docs/draw/impress-lo-first-m0-probe-results.md` when the farmed agent finishes. **This ticket only adds the brief**, not the results file.

---

## Constraints

- Docs / headed shots / probe notes only for the farmed run. No product code in *this* brief’s PR; the farmed follow-up is also measurement-first.
- Keep links accurate: steal doc rev 4; toolsets **Themes** / **Templates** Missing rows.
- Do not start rev 2 M0–M6 (`deck.py` design system) from this brief.
