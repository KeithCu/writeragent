# Impress LO-first M0′ probe results

**Status:** Headed 3-arm probe + UNO inspection complete (docs only). No product code.  
**Brief:** [impress-lo-first-m0-probe-brief.md](impress-lo-first-m0-probe-brief.md) (PR [#787](https://github.com/KeithCu/writeragent/pull/787)).  
**Source plan:** [ppt-master-steal-for-small-models.md](ppt-master-steal-for-small-models.md) **rev 4**.  
**Related:** [impress-specialized-toolsets.md](impress-specialized-toolsets.md) **Themes** / **Templates** Missing rows; [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md).  
**Date:** 2026-09-16  
**Verdict:** **Proceed M1′** for **new decks** (create-from-template). Declare an **LO wall** for **current-doc** apply-design. Theme wrappers: **no**.

Later follow-up (load-master probe + headed #791): DiaMode clipboard import is **not** product-safe headed (system clipboard). Current-doc `apply_design` clones the Hidden `.otp` master into the open deck, then `MasterPage` assign-all. The `loadStylesFromURL` / PresentationLayout walls still stand; do not revert to those APIs or to DiaMode Copy/Paste.

Shots, ODPs, and debug logs stay **box-local** under `/workspace/impress-m0-probe/` (not committed — same lean-history practice as the headed findings run).

---

## Setup

| Item | Value |
|------|--------|
| Model | **`inception/mercury-2.5`** only |
| Extension | WriterAgent **debug** deploy; **WriterAgent-only** (no LibreHarper co-install) |
| Prompt | Space elevator create + edit + add structural + visual (same text all arms; sidebar often split create vs follow-up) |
| Prompt text | `Create a multi-slide presentation about the space elevator. Then, on the same deck: edit an existing slide, add one structural slide, and add one visual shape.` |
| Scoring | Reviewer + UNO / report inspection (not `eval_2_draw_oracle.py` Clearbend Draw task) |
| Artifacts root | `/workspace/impress-m0-probe/` |

---

## Arm A — baseline (chat only)

**Dir:** `/workspace/impress-m0-probe/arm-a/`

| Field | Result |
|-------|--------|
| Slides final | 6 |
| Flow | Create / edit / add structural / visual **completed** |
| Master | White **Default** only |
| `blank_master` | **true** (Default) |
| HF / slide numbers | **Off** (not enabled in report) |
| HF footer text | — |
| Polish vs baseline | n/a (this *is* the baseline) |

### Pass criteria (A)

| # | Criterion | Score |
|---|-----------|-------|
| 1 | `blank_master == false` | **fail** |
| 2 | Placeholder coverage ≥ 80% (title+body on content slides) | **pass\*** — reviewer estimate ≥80% on filled content slides |
| 3 | HF / slide numbers on | **fail** |
| 4 | Polish improves vs A; overlap / one-char not worse | n/a |
| 5 | Blind prefer B or C over A | n/a |

\*Reviewer estimate from reports/shots; not a measured oracle %. Content title+body on filled slides looked mostly placeholder-backed.

### Box-local shots

- `/workspace/impress-m0-probe/arm-a/arm-a-01-create.png`
- `/workspace/impress-m0-probe/arm-a/arm-a-02-edit.png`
- `/workspace/impress-m0-probe/arm-a/arm-a-03-add-slide.png`
- `/workspace/impress-m0-probe/arm-a/arm-a-04-visual.png`
- ODP: `/workspace/impress-m0-probe/arm-a/arm-a-space-elevator.odp`
- Log: `/workspace/impress-m0-probe/arm-a/writeragent_debug.log`
- Report: `/workspace/impress-m0-probe/arm-a/REPORT.md`

---

## Arm B — master + layout + placeholders + HF (no shipped design)

**Dir:** `/workspace/impress-m0-probe/arm-b/`

| Field | Result |
|-------|--------|
| Slides final | 6 |
| Flow | Chat create/edit/add/visual; **some manual** placeholder fill, shape, and HF |
| Master | Still **Default-only** (no alternate master available without a design) |
| `blank_master` | **true** (still Default) |
| HF / slide numbers | **On** — footer `Space Elevator • WriterAgent` + slide numbers |
| Polish vs A | **Slight** (chrome only — HF/numbers) |

### Pass criteria (B)

| # | Criterion | Score |
|---|-----------|-------|
| 1 | `blank_master == false` | **fail** |
| 2 | Placeholder coverage ≥ 80% | **pass\*** — estimate ≥80% on filled content title+body; structural slide needed some **manual** fill |
| 3 | HF / slide numbers on | **pass** |
| 4 | Polish vs A; overlap / one-char not worse | **pass** — slight polish up (chrome); no worse one-char/overlap observed in report |
| 5 | Blind prefer B over A | **mild yes** (chrome) |

### Box-local shots

- `/workspace/impress-m0-probe/arm-b/arm-b-01-create.png`
- `/workspace/impress-m0-probe/arm-b/arm-b-02-edit.png`
- `/workspace/impress-m0-probe/arm-b/arm-b-03-add-slide.png`
- `/workspace/impress-m0-probe/arm-b/arm-b-04-visual.png`
- `/workspace/impress-m0-probe/arm-b/arm-b-05-master-hf.png`
- ODP: `/workspace/impress-m0-probe/arm-b/arm-b-space-elevator.odp`
- Log: `/workspace/impress-m0-probe/arm-b/writeragent_debug.log`
- Report: `/workspace/impress-m0-probe/arm-b/REPORT.md`

**Takeaway:** B alone **cannot** clear `blank_master` without a shipped design. HF works; polish gain is chrome-only.

---

## Arm C — shipped design (Metropolis) + background + HF

**Dir:** `/workspace/impress-m0-probe/arm-c/`

| Field | Result |
|-------|--------|
| Design | **Metropolis** via **File → New → Templates** (new doc before chat) |
| Slides final | 7 |
| Flow | Create / edit / add / visual **OK** |
| Master / bg | Blue designed Metropolis background; not white Default |
| `blank_master` | **false** |
| HF / slide numbers | **On** (manual Insert → Header and Footer; same footer text pattern) |
| Polish vs A | **Clear** polish up (Metropolis look) |

### Pass criteria (C)

| # | Criterion | Score |
|---|-----------|-------|
| 1 | `blank_master == false` | **pass** |
| 2 | Placeholder coverage ≥ 80% | **pass\*** — estimate ≥80% on filled content title+body; some manual fill as in B |
| 3 | HF / slide numbers on | **pass** |
| 4 | Polish vs A; overlap / one-char not worse | **pass** — clear polish up; no worse one-char/overlap observed |
| 5 | Blind prefer C over A | **yes** |

### Box-local shots

- `/workspace/impress-m0-probe/arm-c/arm-c-01-create.png`
- `/workspace/impress-m0-probe/arm-c/arm-c-02-edit.png`
- `/workspace/impress-m0-probe/arm-c/arm-c-03-add-slide.png`
- `/workspace/impress-m0-probe/arm-c/arm-c-04-visual.png`
- `/workspace/impress-m0-probe/arm-c/arm-c-05-design-bg.png`
- ODP: `/workspace/impress-m0-probe/arm-c/arm-c-space-elevator.odp`
- Log: `/workspace/impress-m0-probe/arm-c/writeragent_debug.log`
- Report: `/workspace/impress-m0-probe/arm-c/REPORT.md`

**Takeaway:** Arm C + UNO show the LO-first path works for **new decks**.

---

## UNO probe summary

**Notes:** `/workspace/impress-m0-probe/uno/UNO_PROBE_NOTES.md`  
**Live probe:** `plugin/testing_runner.py` + throwaway `@native_test` under `/workspace/impress-m0-probe/uno/`.

### Paths that work

| Path | Result |
|------|--------|
| **Enumerate designs** | `thePathSettings` → template dirs; **24** `.otp` under `presnt/` (incl. Metropolis, Midnightblue, Sunset, Vivid, …) |
| **Create-from-template** (`loadComponentFromURL` + `AsTemplate`) | **Works — new doc only.** Metropolis master: **6 shapes** |
| **Background props** | Page/master `Background`; nested `FillStyle` / `FillColor` / `FillGradient*` / `FillBitmap*` (solid / gradient / bitmap) |

### Paths that do not / incomplete

| Path | Result |
|------|--------|
| **`loadStylesFromURL`** on Impress | **ABSENT** on current doc **and** new factory Impress doc (`AttributeError`) |
| **Master insert + Background prop copy** onto current doc | Inserts an **empty** master (**0 shapes**) — not a full `.otp` look |
| **Theme apply API** | **NO** usable list/apply theme / color-scheme service. Master `XTheme` exposes `getColorSet` only (palette hook, not apply-design) |

Full JSON dumps live under `/workspace/impress-m0-probe/uno/` (`enumerate_designs.json`, `apply_*.json`, `background_*.json`, `theme_api.json`, `xtheme_introspect.json`, …).

---

## Pass criteria rollup

| Criterion | A | B | C |
|-----------|---|---|---|
| 1. `blank_master == false` | fail | fail | **pass** |
| 2. Placeholder coverage ≥ 80% (reviewer estimate) | pass\* | pass\* | pass\* |
| 3. HF / slide numbers on | fail | **pass** | **pass** |
| 4. Polish ↑ vs A; overlap / one-char not worse | — | slight (chrome) | clear (Metropolis) |
| 5. Blind prefer over A | — | mild yes | **yes** |

\*Caveat: estimate from reports/shots for content title+body on filled slides; B/C needed some manual placeholder fill. Do not treat as a measured oracle percentage.

**Overall:** Arm **B alone cannot clear `blank_master`** without a design. Arm **C + UNO** show LO-first works for **new decks**.

---

## Recommendation

**Proceed M1′:**

1. `list_designs` — enumerate shipped `.otp` via PathSettings (do not hardcode install path).
2. `apply_design` via **create-from-template** (**new doc** only).
3. Core `set_presentation_design` — design + master + HF / slide numbers.

**Declare LO wall** for **current-doc** apply-design:

- `loadStylesFromURL` is **absent** on Impress.
- Master insert + Background copy ≠ full `.otp` import (empty master / 0 shapes).

That wall feeds a **later follow-up** (e.g. Slide Design dispatch or deeper master import) — **not** DeckTheme / M3′ invention yet.

**Theme wrappers: no.** Master `XTheme.getColorSet` is not an apply-theme API. Look = applied `.otp` + master restyle / background.

---

## Keep

- Existing masters / layouts / placeholders / HF tools (`list_master_slides`, `set_slide_master`, layouts, `set_placeholder_text`, `set_headers_footers`, …).
- Steal-doc non-goals: no SVG pipeline in main chat; no SKILL / `references/*.md` in prompts; no competing with sidebar PPT-Master (Route A); no Calc polish transfer.
- Cheap lint (`check_deck_layout`) remains independently shippable of this gate.

## Don’t

- Land `plugin/draw/deck.py`, `DeckTheme` / `DECK_THEMES`, `compose_slide`, Theme API wrappers, or invent host chrome from this probe.
- Land `designs.py` / product apply-design **in this results PR** (docs only here; M1′ is the next ticket).
- Paste ppt-master SKILL into prompts.
- Merge this PR or close issues from the commit/PR text.
- Commit large PNGs / ODPs / debug logs into the repo — keep them box-local.

---

## Artifact index (box-local)

| Arm / probe | Path |
|-------------|------|
| Arm A | `/workspace/impress-m0-probe/arm-a/` (`arm-a-01..04.png`, `.odp`, `writeragent_debug.log`, `REPORT.md`) |
| Arm B | `/workspace/impress-m0-probe/arm-b/` (`arm-b-01..05.png`, `.odp`, log, `REPORT.md`) |
| Arm C | `/workspace/impress-m0-probe/arm-c/` (`arm-c-01..05.png` incl. `arm-c-05-design-bg.png`, `.odp`, log, `REPORT.md`) |
| UNO | `/workspace/impress-m0-probe/uno/UNO_PROBE_NOTES.md` + JSON / live probe scripts |

---

## Done checklist (brief § Done when)

1. Headed A/B/C shots + scores vs five pass criteria — **yes** (paths above).
2. UNO note: enumerate; apply candidates current vs new; background props; Theme API yes/no — **yes**.
3. Recommendation: **M1′** (new-doc create-from-template) + **LO wall** (current-doc apply) — **yes**. Theme wrappers deferred.
