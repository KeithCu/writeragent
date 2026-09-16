# Steal from PPT Master — LO-first Impress polish for small models

**rev 3 — LO-first Route B (supersedes host DeckTheme-primary plan).**
**Status:** Plan (Route B is the plan; Route A is documented context only).
**Audience:** Keith / Chief.
**Date:** 2026-09-16 (rev 3 — supersedes rev 2’s host-side `DeckTheme` + `compose_slide` / `render_deck_plan` plan).
**Doctrine (Keith, 2026-09-16):** **Use every LibreOffice Impress feature first; invent WriterAgent-only systems only after a headed repro shows a real LO wall.**
**Upstream:** [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master) (MIT), skill v6.x.
**Evidence base:** rendered slides from `hugohe3/ppt-master-examples` (glassmorphism, swiss-grid), `plugin/draw/*`, `plugin/framework/prompts.py`, `scripts/eval_2_draw_oracle.py`, [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md), [impress-specialized-toolsets.md](impress-specialized-toolsets.md).

> **Not a product brief:** do **not** paste upstream `references/*.md` or the forked SKILL into a WriterAgent prompt. This doc steals *patterns*. Route B implementation is WriterAgent-original and **LibreOffice-first**.

---

## 0. Decision

There are two ways to close the polish gap between main-chat Impress and ppt-master output.

| Route | What it is | Verdict |
|-------|------------|---------|
| **A** | Use the existing **PPT-Master sidebar mode** (`plugin/chatbot/ppt_master.py` + `plugin/ppt_master/` venv worker) with a strong model. Authors SVG → `svg_to_pptx` → native PPTX → clone into Impress. | **Quality today.** Already installed. Not the subject of this plan — keep running it alongside as the high-quality path. |
| **B** | **LO-first Impress exposure + discipline.** The model chooses content and *which* LibreOffice master / layout / theme. The host exposes Impress APIs (including the Missing Themes row) and lints LO objects. WriterAgent does **not** own look via a custom theme engine until a headed repro shows a real LO wall. | **This plan.** Makes the base product better for mercury-class models without inventing a parallel design system. |

Both can coexist. Route B improves `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` and the Draw/Impress tool path. It does **not** compete with sidebar PPT-Master.

**Principle:** model chooses content + which LO layout / master / theme; host exposes LO APIs and lints; WA does not own look via a custom theme engine until LO cannot.

---

## 1. What we are actually trying to reproduce

Rendered from the public examples repo, two decks show the same five layers on every page. Rev 2 treated those layers as a reason to *invent* a host renderer. Rev 3 maps each layer onto **Impress features** first.

| Layer | What ppt-master examples show | Impress feature (use first) | WA today |
|-------|-------------------------------|-----------------------------|----------|
| **1. Composition** | Full-bleed background, primary panel or band, deliberate negative space — not a white slide with shapes on it | **Master / slide design** + **master/page background** (solid / gradient / bitmap on the page, not a full-bleed shape per slide) | `list_master_slides` / `get_slide_master` / `set_slide_master` (`plugin/draw/masters.py`). Mercury still lands on blank **Default**. No page-background tool. |
| **2. Locked look** | One palette, one type pairing, one icon style, fixed before page 1 (`spec_lock.md`) | **Themes** (color + font schemes) applied deck-wide; one assigned master | **Missing.** [impress-specialized-toolsets.md](impress-specialized-toolsets.md) row **Themes** is ❌ Missing (color/font schemes). Same file: **Templates** ❌ Missing (`.otp` / apply design). |
| **3. Type hierarchy** | Eyebrow / display title / subhead / body / micro + chrome (series name, folio) | **Layouts + placeholders** inherit master text styles + theme fonts. **Headers/footers / slide numbers** for chrome | `set_slide_layout` / `get_slide_layout` (`plugin/draw/transitions.py`); `list_placeholders` / `set_placeholder_text` (`plugin/draw/placeholders.py`); `get_headers_footers` / `set_headers_footers` (`plugin/draw/headers_footers.py`). Headed run used title+bullets and skipped HF. |
| **4. One carrier per page** | 4×4 card grid, KPI row, principles grid — one device family, repeated deck-wide | **Impress layouts** (`title`, `text`, `two_column_text`, `chart`, `table`, `four_objects`, … in `_LAYOUTS`) | Layouts exist. Recipes as a *parallel* layout engine are **not** the first move. Thin aliases (`title_bullets` → `text`) are OK. Invented host carriers (KPI row / card grid) only after a headed probe proves LO has no adequate layout. |
| **5. Assets** | Icons, illustrations, photos, native charts | Existing **tables / charts / images / align / distribute** | Specialized domains already shipped (`tables`, `charts`, `images`, `shapes`). Barely used on the mercury space-elevator deck. |

Main-chat Impress currently produces “title + bullets + one rectangle” ([impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md)). Layers 1–4 are absent in the *output*; several of the Impress features that *implement* those layers are already in the toolset and unused.

**Key conclusion (kept, remapped):** the gap is a *design-system* gap, not a prompt-wording gap. A model that cannot see the slide cannot be talked into frosted glass and a locked type scale. The design system that should close it **already lives in Impress** (masters, layouts, placeholders, HF, themes, templates, page backgrounds). WriterAgent has not exposed Themes/Templates and does not steer mercury onto the features it already has. The host must own **exposure + lint**; the model must own **content + which LO objects to apply**. Invent a WA look engine only after that path hits a real LO wall.

---

## 2. What changes from previous versions of this doc

Rev 1 was prompt-first. Rev 2 promoted a host-side `DeckTheme` + closed recipe enum + `compose_slide` / `render_deck_plan` as the core. Rev 3 demotes that invention until LO is exhausted.

| Item | Rev 1 | Rev 2 (host design system) | Rev 3 (LO-first) |
|------|-------|----------------------------|------------------|
| Page-job / density / one-carrier rules | “Try first” (≤12 lines) | 6-line pointer at `compose_slide` / recipe enum | **Keep ~8–12 lines**, restated: master + layout + placeholder-first; do not freehand until placeholders are filled |
| Closed recipe enum | “Yes, second” | **Core** — host-rendered page templates | **Demoted.** Thin alias to LO layout names, **or** invented host carriers **only after** a headed probe proves LO has no adequate layout (KPI row / card grid). That gate is explicit. |
| Host-drawn chrome / folio / full-bleed background | — | Default path in `compose_slide` | **Demoted.** Use master/page background + `set_headers_footers`. Per-slide shape chrome is not the default. |
| `DeckTheme` dataclass + `DECK_THEMES` presets | — | Primary look system, persisted in `udprops` | **Deferred until LO wall.** Primary look is Impress theme + master. |
| `compose_slide` / `render_deck_plan` / `plugin/draw/deck.py` | — | Core of M0–M6 | **Deferred until LO wall.** Marked in §5. Do not start there. |
| Hard checklist / polish pass | Prompt + `get_draw_tree` | Deterministic `check_deck_layout` | **Keep**, but as **lint over LO objects** (off-canvas, empty placeholders, missing HF) — not a visual design system |
| Eval geometry fields | Maybe | Strengthened in `eval_2_draw_oracle.py` | **Keep.** Measure polish; do not assert it. |
| Themes | Invented tokens | Invented tokens | **Promote:** expose LO Theme API (`list` / `get` / `apply`) — currently ❌ Missing |
| Templates / `.otp` | — | — | **Promote if M0′ says needed:** new from template / apply design |
| Calc transfer | Light | Deferred | Still deferred |
| Sidebar PPT-Master | Keep | Route A | Unchanged. Route A lives there. |

The reusable insight from upstream is its **decomposition** (plan → per-page job → one device → post-check), not its files. The device menu maps onto **Impress layouts**, not a second layout engine.

---

## 3. Route B: LO-first Impress exposure + discipline

### 3.1 Separation of concerns

```
model  →  content + which LO master / layout / theme (and HF / assets)
host   →  expose those Impress APIs, apply them, lint the resulting LO objects
```

The model does not emit a WA color token set or a host recipe that the host then paints as shapes. It fills placeholders and picks stock Impress design objects. Coordinates, fills, and type scale stay with the master / layout / theme until a headed repro shows those cannot carry the page job.

This is still the reliability win for mercury-class models: **structured choice over a small LO enum**, not free-form layout reasoning — but the enum is Impress’s, not WriterAgent’s.

### 3.2 Promote (in this order)

Use what Impress already is, then expose the Missing rows, then lint. Do not skip ahead to a custom renderer.

| # | Feature | Why first | WA status / cite |
|---|---------|-----------|------------------|
| **1** | **Master / slide design** | Stops the blank Default look. One assigned master is the cheapest locked composition. | `list_master_slides`, `get_slide_master`, `set_slide_master` in `plugin/draw/masters.py` (`slide_masters`). `get_presentation_info` already lists master names (`plugin/draw/pages.py`). |
| **2** | **Layouts + placeholders** | Prefer `set_slide_layout` + `set_placeholder_text` over freehand text boxes. Placeholders inherit master styles. | `set_slide_layout` / `get_slide_layout` in `plugin/draw/transitions.py` (`_LAYOUTS`: `title`, `text`, `two_column_text`, `title_only`, `chart`, `table`, `four_objects`, …). Core placeholders: `list_placeholders`, `get_placeholder_text`, `set_placeholder_text` in `plugin/draw/placeholders.py`. `add_slide` already defaults Impress to Title + Content (`text`) in `plugin/draw/pages.py`. Headed failure was fill-before-layout (`available: []`) — routing, not a missing renderer. |
| **3** | **Headers / footers / slide numbers** | Chrome and folio without host-drawn text boxes. | `get_headers_footers`, `set_headers_footers` in `plugin/draw/headers_footers.py` (`headers_footers`). Already specialized; **use them**. |
| **4** | **Themes (color + font schemes)** | The locked-look layer. This is the first *product* gap after using shipped tools. | ❌ **Missing** in [impress-specialized-toolsets.md](impress-specialized-toolsets.md) (§2 domain table, Priority 5 “Themes: Color schemes, font schemes”). **Expose `list` / `get` / `apply`.** Inspect the live UNO surface with `plugin/testing_runner.py` — do not guess API names. |
| **5** | **Templates** (`.otp` / new from template / apply design) | If stock masters + themes still look generic, apply a real design instead of drawing one. | ❌ **Missing** (same toolsets doc). Only if the M0′ probe says themes + shipped masters are not enough. |
| **6** | **Master / page background fills** | Solid / gradient / bitmap on the **page or master**, via LO — not a full-bleed `shape_upsert` rectangle per slide as the default. | Not exposed. Distinct from shape `FillStyle` in `plugin/draw/shapes.py` (solid / transparent / none only). Probe page/master fill UNO on a live instance. |
| **7** | **Existing tables / charts / images / align / distribute** | Layer 5. Do not wrap these in a deck renderer. | Specialized domains already complete: `tables`, `charts`, `images`, `align_shapes` / `distribute_shapes` / `create_diagram` (`plugin/draw/shapes.py`, `plugin/draw/layout.py`). |

### 3.3 Demote / delay (until a headed LO wall)

These were the *core* of rev 2. They are now an explicit later phase, not the starting architecture.

| Rev 2 invention | Why demoted |
|-----------------|-------------|
| Custom `DeckTheme` dataclass as the primary look system | Duplicates Impress Themes (color + font schemes) + master assignment. Persisting WA tokens in `udprops` while LO already has a theme is a parallel design system. |
| Host-drawn chrome / folio / background as the default path (`compose_slide` steps 1–3) | Impress already has master backgrounds and `headers_footers`. Per-slide full-bleed shapes fight the master and break editability in the LO UI. |
| Closed recipe enum as a parallel layout engine (`RECIPES` → host geometry → shapes) | `_LAYOUTS` already *is* the carrier menu. A second enum that expands to `shape_upsert` boxes invents layout Impress already ships. |

Rev 2 file-level core — `plugin/draw/deck.py`, `DeckTheme`, `DECK_THEMES`, `compose_slide`, `render_deck_plan`, `set_deck_theme` — stays **deferred until LO wall**. Do not treat old M0–M6 as the current sequence (see §5).

### 3.4 Still keep (LO-compatible)

Three pieces from rev 2 remain, because they do not invent a look engine.

**Short DECK MODE prompt (~8–12 lines)** in `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` — master + layout + placeholder-first; do not freehand until placeholders are filled:

```
DECK MODE (when building more than one slide):
- Assign a real master (list_master_slides / set_slide_master). Do not leave Default blank.
- Pick an Impress layout per page (set_slide_layout), then fill placeholders
  (list_placeholders → set_placeholder_text). Do not freehand text boxes until
  those placeholders are filled.
- Turn on headers/footers / slide numbers (set_headers_footers) instead of drawing folio.
- Apply one Impress theme for the whole deck when the Theme tools exist.
- One layout family per page. Raw shapes are for extras (icons, callouts), not the page.
- Before finishing, call check_deck_layout and fix reported issues once.
```

Everything else is tool schema and host code — still inside the 2× prompt-size rule. Do **not** paste upstream SKILL / `references/*.md`.

**`check_deck_layout` (or rename)** as **lint over LO objects**, not a visual design system. Report-only by default; optional `fix=true` capped at one obvious pass (e.g. empty-placeholder hint → `set_slide_layout`).

Rules (each returns `{kind, shapes, hint}`):

- `off_canvas` — any shape box outside the page.
- `empty_placeholder` — title/body/subtitle placeholder with no content (the headed `available: []` / empty-fill failure).
- `missing_hf` — slide numbers / footer off when DECK MODE asked for them.
- `blank_master` — still on Default / unnamed blank master when other masters exist.
- `overlap` — text boxes overlapping text boxes beyond a small tolerance (recorded Draw failure: `docs/eval/eval-2/headed-failure-autopsy.md`).
- `one_char_line` — text box too narrow for its character count (recorded vertical-text bug).

Drop or keep-last theme-relative rules such as `unbalanced_margins` vs a WA gutter — those assume a `DeckTheme`. Geometry helpers stay pure in `plugin/draw/layout.py` (`layout_issues` wrapping `get_draw_tree` / `_shape_box`) so they stay unit-testable without UNO.

**Eval polish geometry fields** in `scripts/eval_2_draw_oracle.py` — see §6. Unchanged intent from rev 2: polish must be falsifiable.

### 3.5 Recipes: alias, or invent only after an LO wall

Recipes are **not** a parallel layout engine.

**Allowed now:** a thin alias from a short name to an existing Impress layout in `_LAYOUTS` (`plugin/draw/transitions.py`):

| Alias (optional) | Impress layout |
|------------------|----------------|
| `title_bullets` | `text` (Title + Content; already `add_slide` default) |
| `cover` | `title` (title + subtitle) |
| `section_divider` | `title_only` |
| `two_column_compare` | `two_column_text` |
| `chart` | `chart` / `text_and_chart` |
| `table` | `table` |
| `closing` | `title` or `title_only` |

The alias, if shipped, is a prompt/schema convenience. The host still calls `set_slide_layout` and fills placeholders. It does **not** expand to host-drawn cards.

**Invented host carriers** (`kpi_row`, `card_grid`, quote chrome, process-flow boxes, `compose_slide` geometry) are allowed **only after** this gate:

1. Headed probe on a real deck job (e.g. mercury space-elevator, or a KPI/card-grid brief).
2. Model is steered to stock masters + layouts + placeholders + HF + themes (and templates if M1′ landed).
3. The probe shows Impress has **no adequate layout** for that page job — not “the model failed to pick `four_objects`”, but “LO’s layouts cannot express a KPI row / card grid at acceptable polish.”
4. Write the headed repro down (shot + tool log + which layouts were tried). Only then may Route B grow a host carrier for *that* gap.

Until that repro exists, `freeform` via the specialized `shapes` domain remains the escape hatch — same as rev 2’s “not a cage,” without a default renderer.

### 3.6 Assets (unchanged discipline, no new pipeline)

- **v1:** Unicode / existing image tools; no network required.
- **v2 (opt-in):** `image_generate` / `image_insert` (`plugin/writer/images/images.py`) for one hero — already shipped.
- **v3:** illustration-sheet slicing (upstream). Out of scope until an LO wall says otherwise.

Do not invent a deck-owned asset system.

### 3.7 UNO fidelity — inspect, don’t guess

Per `AGENTS.md`, probe with `plugin/testing_runner.py` on a live Impress instance:

1. **Theme API.** What list/get/apply actually are on this LO (color scheme, font scheme, `XTheme` / slide design). This is M1′s first hour, not a guessed wrapper.
2. **Templates / apply design.** `.otp` load, “new from template,” apply master-from-file. Only if M0′ says stock designs + themes are not enough.
3. **Page / master background.** `FillStyle` `SOLID` / `GRADIENT` / `BITMAP` on the draw page or master — not a per-slide shape. Shape-level `fill_gradient` in `_apply_shape_properties` stays a later *shape* enhancement, not the default composition path.
4. **HF + masters already shipped.** Confirm mercury can reach them via `delegate_to_specialized_draw_toolset` (`slide_masters`, `headers_footers`, `slide_layouts`) without new domains.

---

## 4. Architecture and file map

LO-first work touches existing Draw modules and one Missing API. Rev 2’s `deck.py` design system is not in this map as a starting deliverable.

| File | Change | When |
|------|--------|------|
| `plugin/draw/masters.py` | Use as-is (`list_master_slides`, `get_slide_master`, `set_slide_master`) | M0′ / M2′ |
| `plugin/draw/transitions.py` | Use as-is (`get_slide_layout`, `set_slide_layout`, `_LAYOUTS`). Optional thin alias names only if they reduce mercury errors | M0′ / M2′ |
| `plugin/draw/placeholders.py` | Use as-is; keep fill-before-layout hints | M0′ / M2′ |
| `plugin/draw/headers_footers.py` | Use as-is | M0′ / M2′ |
| `plugin/draw/pages.py` | `add_slide` already defaults to `text`; `get_presentation_info` lists masters | M0′ |
| `plugin/draw/themes.py` (new, name TBD after UNO probe) | `list` / `get` / `apply` Impress color+font schemes | **M1′** |
| `plugin/draw/templates.py` (new, only if M0′ says so) | New from `.otp` / apply design | **M1′** (conditional) |
| Page/master background helper (module TBD after UNO probe) | Solid / gradient / bitmap on page or master | After M0′ if stock master fills are insufficient *and* theme apply does not set them |
| `plugin/framework/prompts.py` | ~8–12 line DECK MODE block (master+layout+placeholder-first) | **M2′** |
| `plugin/draw/layout.py` | `layout_issues(...)` over LO boxes / placeholder / HF flags — lint, not carriers | **M2′** |
| Lint tool on an existing specialized base (name: `check_deck_layout` or similar) | Wraps `get_draw_tree` + HF/master/placeholder reads | **M2′** |
| `scripts/eval_2_draw_oracle.py` | Geometry + HF/master/placeholder fields | M2′ / measure |
| `tests/draw/test_draw_layout.py` | Cases for `layout_issues` | M2′ |
| `tests/draw/test_themes.py` + `test_themes_uno.py` (new) | Theme list/get/apply | M1′ |
| `docs/draw/impress-specialized-toolsets.md` | Flip Themes (and Templates if shipped) off the Missing row | When the tools land |
| `plugin/draw/shapes.py` / tables / charts / images | No new deck wrapper; use existing tools | Ongoing |

**Deferred until LO wall (old M0–M6 / rev 2 core) — do not start here:**

| File / symbol | Why deferred |
|---------------|--------------|
| `plugin/draw/deck.py` | Host design system |
| `DeckTheme`, `DECK_THEMES`, `set_deck_theme` / `get_deck_theme` via `udprops` | Parallel look tokens |
| `RECIPES` as host-rendered templates, `compose_slide`, `render_deck_plan` | Parallel layout engine |
| `carrier_boxes` as the primary composition API | Invented geometry vs `_LAYOUTS` |
| `tests/draw/test_deck.py`, `test_deck_uno.py` | Only if M3′ opens |
| Host-drawn folio / eyebrow / full-bleed background as default | Conflicts with master + HF |
| `ToolDrawDeckBase` domain `deck` as core orchestrators | Premature product surface |

**Tier decision (LO-first):** Theme list/get/apply and `check_deck_layout` should be reachable without a long delegation chain once they exist (same small-model reason rev 2 wanted core tools). Raw `shapes` stays specialized as the escape hatch. Masters / layouts / HF are already specialized — M2′ prompt must name the `delegate_to_specialized_draw_toolset` domains (`slide_masters`, `slide_layouts`, `headers_footers`) until/unless a later change promotes the few calls DECK MODE always needs.

**Reuse, not reinvention:** `CreateDiagram` / `diagram_node_boxes` (`plugin/draw/layout.py`) stay the flowchart path. Do not add a second layout engine for `process_flow` unless M3′’s headed gate fires.

---

## 5. Milestones

Each milestone is independently shippable. **M0′ is a headed probe, not a renderer.** Old M0–M6 (rev 2) assumed `plugin/draw/deck.py` + `DECK_THEMES` as the core — they are **deferred until LO wall** (M3′), not the current sequence.

| # | Deliverable | Tests / evidence | Notes |
|---|----------------|------------------|-------|
| **M0′** | **Headed probe:** stock Impress designs / themes only (no new renderer). Assign a real master, `set_slide_layout` + `set_placeholder_text`, HF / slide numbers. Can mercury get non-generic polish via master + layout + placeholders + HF? | Headed space-elevator (or peer) with shots + debug log. Native UNO inspection of what themes / slide designs the installed LO already exposes, even if WA has no Theme tool yet (apply them by hand in the UI for the A/B). | **Gate for everything else.** If yes → M1′/M2′ are exposure + discipline. If no, record *which* LO feature failed (no useful master, theme not reachable from UNO, no KPI layout, …) — that write-up *is* the LO wall. |
| **M1′** | **Expose Theme API** (`list` / `get` / `apply` color + font schemes). Add template apply (`.otp` / apply design) **only if** M0′ says stock masters + in-doc themes are not enough. | `test_themes.py`, `test_themes_uno.py`; flip the Missing **Themes** row when landed | Inspect UNO first. Do not invent `DeckTheme` here. |
| **M2′** | **Prompt / tool routing:** layout + placeholder-first DECK MODE (~8–12 lines); optional `check_deck_layout` lint over LO objects; optional thin aliases → `_LAYOUTS` | Prompt/unit tests; `test_draw_layout.py` for lint; re-run headed mercury | No `compose_slide`. Routing must prevent fill-before-layout (`available: []`). |
| **M3′** | **Invent recipes / `DeckTheme` overlay only where a headed repro shows an LO wall** | The headed repro from M0′ (or a later KPI/card-grid brief) plus new tests *for that gap only* | This is where old M0–M6 / `plugin/draw/deck.py` / `DECK_THEMES` / host-drawn carriers may re-enter — **narrowly**, for the failed page job (e.g. KPI row / card grid), not as a wholesale design-system replacement. |

### Deferred (rev 2 M0–M6) — only after M3′ opens

| Old # | Old deliverable | Status in rev 3 |
|-------|-----------------|-----------------|
| M0 | `carrier_boxes` + `layout_issues` as the start of a host design system | `layout_issues` **lint** may ship in M2′. `carrier_boxes` as composition API waits for M3′. |
| M1 | `DeckTheme` + presets + `compose_slide` for cover / KPI / card grid / … | **Deferred.** Visual payoff is supposed to come from master + theme + layout first. |
| M2 | `render_deck_plan` + DECK MODE pointing at the renderer | **Deferred.** DECK MODE in M2′ points at LO tools. |
| M3 | `check_deck_layout` as renderer QA | **Kept earlier** (M2′) as LO-object lint. |
| M4 | `set_deck_theme` via `udprops` + remaining host recipes | **Deferred.** Theme persist = Impress theme, not `WriterAgent.DeckTheme`. |
| M5 | Hero image on host-rendered cover | Use existing `image_*` tools without a deck renderer. |
| M6 | Eval geometry + headed before/after | **Keep** as measurement around M0′ and M2′ (see §6). |

---

## 6. Measurement

Polish must be falsifiable. Extend `scripts/eval_2_draw_oracle.py` (it already parses saved `.odg` XML) with fields that score **LO usage**, not host-chrome presence:

- Geometry: `off_canvas_count`, `text_overlap_count`, `empty_text_count`, `one_char_line_count`.
- LO objects: `empty_placeholder_count`, `has_slide_number` / HF on, `blank_master` (still Default), later `has_named_theme` once M1′ exists.
- Do **not** require `has_folio` drawn as a shape — that was a rev 2 host-chrome tell.
- Keep existing semantic anchors (Clearbend, lanes, decision). Add geometry / LO-object fields; do not replace content scoring.
- Report a `polish` sub-score separately from the content score so a deck can be content-HAPPY and polish-low (today’s failure mode).

Run `scripts/eval_2_headed.py --task draw-primary --score …` **before M0′ work lands in product** (baseline: current mercury title+bullets) and **after M2′** on the same prompt. If M0′ already shows a polish jump from hand-applied masters/themes, that is the evidence to *not* build `deck.py`. Add the same criteria to `docs/eval/eval-2/draw-primary-deliverable/rubric.eval2.md`.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Stock Impress still looks generic even with master + layout + HF + theme | That is the M0′ question. If the headed probe says yes (still generic), M3′ is justified — with a written LO wall, not a vibe. |
| Theme / template UNO is version-skewed or thin | Inspect on the live instance (`testing_runner`); ship the subset that works; do not invent `DeckTheme` as a workaround until that probe is written down. |
| Mercury still freehands boxes and skips masters | M2′ DECK MODE + lint (`empty_placeholder`, `blank_master`, `missing_hf`) + existing placeholder hints. Specialized-domain discoverability is a routing bug, not a reason to host-draw the page. |
| `.otp` / apply-design scope creep | Templates are conditional on M0′. Do not build a template manager in M1′ unless the probe asked for it. |
| Lint grows into a visual design system | Keep rules about LO objects and geometry. No theme-gutter / folio-shape / card-rhythm checks until M3′ exists. |
| Inventing `DeckTheme` too early | File map in §4 marks `deck.py` / `DECK_THEMES` deferred. Review should reject PRs that start M0–M6 without an M0′/M3′ repro. |
| Card-grid / KPI page jobs that LO layouts cannot express | Allowed *after* the §3.5 gate. Until then, `four_objects` / table / chart layouts + `shapes` escape hatch. |
| Fonts missing / theme fonts flatten | Impress theme fallback is LO’s problem first; document what apply actually set. Liberation/DejaVu notes from rev 2 apply only if M3′ draws text boxes. |
| Scope creep into an SVG engine | Still a non-goal. Route A owns that pipeline. |

---

## 8. Non-goals

- Re-implementing `svg_to_pptx` or an SVG pipeline in main chat.
- Loading `executor-base.md` / `strategist.md` / `image-generator.md` (or the forked SKILL) into any prompt.
- Multi-agent Strategist/Executor role switching in main chat.
- Animations / Morph / narrated video.
- AI illustration-sheet slicing in v1.
- Removing or deprecating sidebar PPT-Master (Route A).
- **Building `plugin/draw/deck.py` + `DECK_THEMES` + host-rendered carriers as the first cut** (rev 2 M0–M6). That path is M3′ only, after a headed LO wall.
- Host-drawn chrome / folio / full-bleed background as the default composition path.
- A closed recipe enum that expands to shape geometry while `_LAYOUTS` still fits the page job.
- Calc polish transfer (still deferred; Calc has its own failures).

---

## 9. Findings (cut-paste) and debt this avoids

| # | Problem | Where in WA | LO-first piece | Why small-model OK | Risk |
|---|---------|-------------|----------------|--------------------|------|
| 1 | Tools HAPPY, slides look generic | Main-chat Impress; headed space-elevator | Use master + layout + placeholders + HF **before** any renderer | Features already exist; prompt + routing, not a new engine | Stock designs still generic → M0′ must say so |
| 2 | Freehand coordinate soup | `plugin/draw/` shapes domain | Prefer `set_slide_layout` + `set_placeholder_text`; thin aliases only | Enum is LO’s `_LAYOUTS`, not a WA recipe table | Model ignores and freehands |
| 3 | Locked look absent | Themes ❌ Missing | Expose theme list/get/apply (M1′) | Host API, tiny prompt | UNO thinner than hoped |
| 4 | No post-build QA | Draw loop | `check_deck_layout` over LO objects | One tool result; no vision | Lint scope creep |
| 5 | Polish not measured | eval-2 Draw rubrics | Geometry + master/HF/placeholder fields | Eval-only | Rubric gaming |
| 6 | Full PPT Master quality | Sidebar `ppt-master` mode | Keep Route A on strong models | Already isolated | Users expect main chat = sidebar |
| 7 | Parallel design system | Rev 2 `DeckTheme` / `deck.py` | Do not build until M3′ LO wall | Avoids a second look engine | Premature `deck.py` PR |

Debt this **avoids** (rev 3 vs rev 2):

- **No second look engine** until Impress themes/masters/layouts are proven insufficient.
- **No host chrome vs LO HF fight** (two folios, backgrounds that hide the master).
- **Lint instead of prose** still stands — and stays cheaper because it reads LO objects mercury already creates.
- **Measured polish** still stands — now scoring “used a master / filled placeholders / HF on,” not “host drew a folio shape.”

Debt that **remains available** after an LO wall (do not pre-build):

- One shape-styling path (`fill_gradient` in `_apply_shape_properties`) if M3′ or page-background work needs it.
- `carrier_boxes` next to `align_boxes` / `distribute_boxes` / `diagram_node_boxes` if a headed KPI/card-grid gap is real.

---

## References (upstream, read selectively)

- Skill entry: `skills/ppt-master/SKILL.md`
- Rubric to compress: `references/visual-review.md`
- Do not load into WA prompts: `references/executor-base.md`, `strategist.md`, `image-generator.md`, `shared-standards-core.md`
- WA integration: `plugin/contrib/ppt_master/README.md`, `plugin/ppt_master/`, `plugin/chatbot/ppt_master.py`
- WA tools / gaps: [impress-specialized-toolsets.md](impress-specialized-toolsets.md) (Themes ❌ Missing; Templates ❌ Missing; shipped `slide_masters`, placeholders, `headers_footers`), [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md)
