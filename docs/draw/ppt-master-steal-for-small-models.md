# Steal from PPT Master — LO-first Impress polish for small models

**rev 4 — LO-first Route B, corrected ordering (supersedes rev 3).**
**Status:** Plan (Route B is the plan; Route A is documented context only).
**Audience:** Keith / Chief.
**Date:** 2026-09-16 (rev 4 — supersedes rev 3’s Theme-API-first / template-conditional ordering).
**Doctrine (Keith, 2026-09-16):** **Use every LibreOffice Impress feature first; invent WriterAgent-only systems only after a headed repro shows a real LO wall.**
**Upstream:** [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master) (MIT), skill v6.x.
**Evidence base:** rendered slides from `hugohe3/ppt-master-examples` (glassmorphism, swiss-grid); `plugin/draw/*`; `plugin/framework/prompts.py`; `scripts/eval_2_draw_oracle.py`; [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md); [impress-specialized-toolsets.md](impress-specialized-toolsets.md); filesystem probe of the installed LO (23 shipped slide designs, `.otp` internals).
**Related:** [M0′ / UNO-probe farm brief](impress-lo-first-m0-probe-brief.md)

**Later product change:** core restyle is `list_designs` → `apply_design` on the **open** deck (clone_master). There is no `set_presentation_design` and no `new_document` on `apply_design` — a new Impress window spawned a second sidebar agent. Create-from-template is an internal/test helper. HF stay on specialized `set_headers_footers`. Historical M1′ rows below that name `set_presentation_design` are the original plan, not the shipped API.

> **Not a product brief:** do **not** paste upstream `references/*.md` or the forked SKILL into a WriterAgent prompt. This doc steals *patterns*. Route B implementation is WriterAgent-original and **LibreOffice-first**.

---

## 0. Decision

There are two ways to close the polish gap between main-chat Impress and ppt-master output.

| Route | What it is | Verdict |
|-------|------------|---------|
| **A** | Use the existing **PPT-Master sidebar mode** (`plugin/chatbot/ppt_master.py` + `plugin/ppt_master/` venv worker) with a strong model. Authors SVG → `svg_to_pptx` → native PPTX → clone into Impress. | **Quality today.** Already installed. Not the subject of this plan — keep running it alongside as the high-quality path. |
| **B** | **LO-first Impress exposure + discipline.** The model chooses content and *which* LibreOffice slide design / master / layout. The host exposes the Impress APIs mercury actually needs **in the main chat (core)**, and lints the resulting LO objects. WriterAgent does **not** own look via a custom theme engine until a headed repro shows a real LO wall. | **This plan.** Makes the base product better for mercury-class models without inventing a parallel design system. |

Both can coexist. Route B improves `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` and the Draw/Impress tool path. It does **not** compete with sidebar PPT-Master.

**Principle:** model chooses content + which LO design / master / layout; the host exposes those APIs (in core where the deck turn always needs them) and lints; WA invents look only where LO cannot.

---

## 1. What we are actually trying to reproduce

Rendered from the public examples repo, two decks show the same five layers on every page. Rev 2 treated those layers as a reason to *invent* a host renderer. Rev 3 mapped them onto Impress but started with a Theme API that likely does not exist. Rev 4 maps each layer onto **Impress features that are on disk today**, and treats “themes” as probe-gated.

| Layer | What ppt-master examples show | Impress feature (use first) | WA today |
|-------|-------------------------------|-----------------------------|----------|
| **1. Composition** | Full-bleed background, primary panel or band, deliberate negative space — not a white slide with shapes on it | **A real slide design** (master page + styles) applied once, plus **page/master background fill** (solid / gradient / bitmap) | 23 designs ship with LO at `<install>/share/template/common/presnt/*.otp`. `list_master_slides` / `set_slide_master` (`plugin/draw/masters.py`) can *assign* a master, but a fresh doc has only `Default` and there is **no tool to apply a design or a page background**. Mercury lands on blank Default. |
| **2. Locked look** | One palette, one type pairing, one icon style, fixed before page 1 (`spec_lock.md`) | **The applied slide design** + **restyled master** (background + the master’s Title/Body/… text styles). *Not* an ODF theme color/font scheme. | **Missing.** [impress-specialized-toolsets.md](impress-specialized-toolsets.md) lists **Themes** ❌ Missing (color/font schemes) and **Templates** ❌ Missing. Note: a shipped `.otp` carries its look in `styles.xml` (master page + named styles); there is **no theme/color-scheme element in the ODP**. LO’s “theme” support is a PPTX-import concept. |
| **3. Type hierarchy** | Eyebrow / display title / subhead / body / micro + chrome (series name, folio) | **Layouts + placeholders** inherit master text styles. **Headers/footers / slide numbers** for chrome | `set_slide_layout` / `get_slide_layout` (`plugin/draw/transitions.py`; `_LAYOUTS` has 30 entries, including `four_objects` = 23). Placeholders: `list_placeholders` / `get_placeholder_text` / `set_placeholder_text` (`plugin/draw/placeholders.py`; **core**). HF: `get_headers_footers` / `set_headers_footers` (`plugin/draw/headers_footers.py`; specialized). `add_slide` already defaults to `text` and applies it (`plugin/draw/pages.py`). Headed run skipped HF and failed fill-before-layout — routing, not missing features. |
| **4. One carrier per page** | 4×4 card grid, KPI row, principles grid — one device family, repeated deck-wide | **Impress layouts** (`_LAYOUTS`: `title`, `text`, `two_column_text`, `table`, `chart`, `four_objects`, `two_objects`, …) | Layouts exist and are reachable via `add_slide(layout=…)` (core) or `set_slide_layout` (specialized). Invented host carriers are **not** the first move. |
| **5. Assets** | Icons, illustrations, photos, native charts | Existing **tables / charts / images / align / distribute** | Specialized domains already shipped (`tables`, `charts`, `images`, `shapes`). Barely used on the mercury space-elevator deck. |

Main-chat Impress currently produces “title + bullets + one rectangle” ([impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md)).

**Key conclusion.** The design system is **not already complete in LM/UNO as a Theme API** — but the *designs themselves* ship with LibreOffice. The look lives in **master pages and applied slide designs** (`<install>/share/template/common/presnt/*.otp`, 23 of them on this machine; discover the directory via LO’s `PathSettings`/`Paths` template path rather than hardcoding), and in the master’s background + text styles. WriterAgent has not exposed **apply-a-design**, **page/master background**, or **master restyle**, and does not route mercury onto the features it *does* expose. The host must own **exposure (preferably in core) + lint**; the model must own **content + which LO objects to apply**. Invent a WA look engine only after that path hits a real LO wall.

---

## 2. What changes from previous versions of this doc

| Item | Rev 1 | Rev 2 (host design system) | Rev 3 (LO-first) | Rev 4 (this rev) |
|------|-------|----------------------------|------------------|------------------|
| Page-job / density / one-carrier rules | “Try first” | 6-line pointer at `compose_slide` | Keep ~8–12 lines, restated for LO | **Keep**, now “apply a design, then layout + placeholder-first; HF for chrome” |
| Apply a real slide design (`.otp` / template) | — | — | Conditional row 5, gated on M0′ | **Promoted to the first move.** 23 designs ship with LO; it is the source of masters and the locked look |
| Master / page background | — | Host-drawn full-bleed shape | Buried at row 6 | **Promoted.** Page/master fill (solid/gradient/bitmap) + master restyle deliver layer 1–2 without a WA engine |
| Themes (color + font schemes) | Invented tokens | `DeckTheme` tokens | **Promoted to M1′** on the assumption an API exists | **Probe-gated.** ODP has no theme element; likely OOXML-only. Primary look = applied design + restyled master |
| Master assignment (`set_slide_master`) | — | — | Row 1, but a fresh doc has only `Default` | **Folded into “apply a design”,** which supplies real masters first |
| Closed recipe enum | Yes, second | Core — host templates | Demoted; thin alias | Demoted; thin alias only (see §3.6) |
| `DeckTheme` / `compose_slide` / `render_deck_plan` / `deck.py` | — | Core of M0–M6 | Deferred to M3′ | Deferred to M3′ (unchanged) |
| Host-drawn chrome / folio / background | — | Default path | Demoted | Demoted |
| DECK MODE prompt | — | Points at renderer | ~8–12 lines | **Keep**, but paired with **host defaults in core** (§3.4), because prompt-only steering fades |
| Hard checklist / polish pass | Prompt | `check_deck_layout` | Lint over LO objects | **Keep**, ship independent of the M0′ gate |
| LO APIs reachable without delegation | — | Core deck tools | Noted, deferred | **Load-bearing (§3.4):** core `add_slide` + a small core `set_presentation_design` default |
| Eval geometry fields | Maybe | Strengthened oracle | Keep | **Keep**, with explicit M0′ pass criteria (§6) |
| Calc transfer | Light | Deferred | Deferred | Deferred |
| Sidebar PPT-Master | Keep | Route A | Unchanged | Unchanged |
| Templates | — | — | Conditional on M0′ | **No longer conditional** — it is step 1 |

The reusable insight from upstream is its **decomposition** (plan → per-page job → one device → post-check), not its files. The device menu maps onto **Impress layouts**, not a second layout engine.

---

## 3. Route B: LO-first Impress exposure + discipline

### 3.1 Separation of concerns

```
model  →  content + which LO slide design / master / layout (and HF / assets)
host   →  expose those Impress APIs (core where the turn always needs them), apply
          the chosen design once at deck level, lint the resulting LO objects
```

The model does not emit a WA color token set or a host recipe the host paints as shapes. It fills placeholders and picks stock Impress design objects. Coordinates, fills, and type scale stay with the applied design / master / layout until a headed repro shows those cannot carry the page job.

This is still the reliability win for mercury-class models: **structured choice over a small LO enum**, not free-form layout reasoning — but the enum is Impress’s, not WriterAgent’s.

### 3.2 Promote (in this order)

Corrected order: **design → restyle/background → layout+placeholders → chrome → assets → (theme only if it exists).**

| # | Feature | Why this position | WA status / cite |
|---|---------|-------------------|------------------|
| **1** | **Apply a real slide design** (shipped `.otp` / template, or in-doc master) | This is the *source* of masters and the locked look. It must come first — `set_slide_master` on a fresh doc has nothing but `Default` to assign. | 23 designs at `<install>/share/template/common/presnt/*.otp` (Metropolis, Midnightblue, Sunset, Vivid, Focus, Portfolio, Candy, Vintage, …). ❌ **Missing** as a tool. Probe paths: apply a design to the current doc (the UI’s Slide Design / Master Slides behavior) or create the deck from a template and fill it. Inspect the live UNO surface with `plugin/testing_runner.py`; candidates are `XStyleLoader.loadStylesFromURL` (`com.sun.star.style.XStyleLoader`) and master-page supply — do not hardcode guessed names. |
| **2** | **Master restyle + page/master background** | Delivers layers 1–2 (composition + locked palette) using LO-native fills, not per-slide shapes, and not an ODP theme API. Highest visual impact per unit of work. | Not exposed. Page/master fill via the draw page / master page `Background` property (solid / gradient / bitmap); restyle the master’s Title/Body/… text styles. Distinct from *shape* `FillStyle` in `plugin/draw/shapes.py` (solid / transparent / none). Probe on a live instance. |
| **3** | **Layouts + placeholders** | Prefer `add_slide(layout=…)` / `set_slide_layout` + `set_placeholder_text` over freehand text boxes. Placeholders inherit master styles. | `add_slide` is core and already applies a layout (default `text`; `plugin/draw/pages.py`). `set_slide_layout` / `get_slide_layout` are specialized (`plugin/draw/transitions.py`; `_LAYOUTS` incl. `four_objects`=23). Placeholders are **core** (`plugin/draw/placeholders.py`). Fix routing so fill comes after layout (`available: []` failure). |
| **4** | **Headers / footers / slide numbers** | Chrome and folio without host-drawn text boxes. | `get_headers_footers`, `set_headers_footers` (`plugin/draw/headers_footers.py`; specialized). Use them; do not draw folio shapes. |
| **5** | **Existing tables / charts / images / align / distribute** | Layer 5. Do not wrap these in a deck renderer. | Specialized domains already complete: `tables`, `charts`, `images`, `align_shapes` / `distribute_shapes` / `create_diagram` (`plugin/draw/shapes.py`, `plugin/draw/layout.py`). |
| **6** | **Theme API (color + font schemes)** — **only if the probe finds one** | Likely does not exist for ODF Impress. Treat as a probe result, not a deliverable. | No theme/color-scheme element in a shipped `.otp`; look lives in `styles.xml` master + named styles. If a real UNO theme service exists on this LO, expose `list`/`get`/`apply`; otherwise the answer is steps 1–2. |

### 3.3 Demote / delay (until a headed LO wall)

These were the core of rev 2. They remain an explicit later phase.

| Rev 2 invention | Why demoted |
|-----------------|-------------|
| Custom `DeckTheme` dataclass as the primary look system | Duplicates an applied slide design + restyled master. Persisting WA tokens in `udprops` while LO already has designs is a parallel design system. |
| Host-drawn chrome / folio / full-bleed background as the default (`compose_slide` steps 1–3) | Impress has master backgrounds and `headers_footers`. Per-slide full-bleed shapes fight the master and break editability in the LO UI. |
| Closed recipe enum as a parallel layout engine (`RECIPES` → host geometry → shapes) | `_LAYOUTS` already *is* the carrier menu. A second enum that expands to `shape_upsert` boxes invents layout Impress ships. |

Rev 2 file-level core — `plugin/draw/deck.py`, `DeckTheme`, `DECK_THEMES`, `compose_slide`, `render_deck_plan`, `set_deck_theme` — stays **deferred until LO wall** (see §5).

### 3.4 Host defaults in core (new, and load-bearing)

The reason rev 3 would still under-deliver: masters, layouts, and HF are **specialized**, so a weak model must make several long-running `delegate_to_specialized_draw_toolset` round-trips per deck (`slide_masters`, `slide_layouts`, `headers_footers`) just to use LO features. The mercury findings show prompt-only steering fades; the fix is to make the **defaults** do the LO-right thing, in tools the main chat already has.

Two concrete pieces, both small:

1. **A core `set_presentation_design` (name TBD).** One call applies the chosen slide design for the whole deck, assigns the matching master to existing slides, and enables headers/footers + slide numbers. This is the deck-level setup a deck turn always needs, so it belongs in core, not behind a delegation. On Draw docs it can report “not an Impress document” cleanly.
2. **Use `add_slide` as the layout seam.** It is already core and already applies a layout from `_LAYOUTS`. Extend its `layout` handling with the thin aliases in §3.6 and have it inherit the deck’s assigned master by default. No new engine; one core tool plus one core parameter.

Keep raw `shapes` specialized as the escape hatch. Nothing here draws a page.

### 3.5 Still keep (LO-compatible)

**Short DECK MODE prompt (~8–12 lines)** in `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` — paired with, not a substitute for, §3.4:

```
DECK MODE (when building more than one slide):
- Apply one real slide design / master at deck level first (set_presentation_design).
  Do not leave Default blank.
- Pick an Impress layout per page (add_slide layout= / set_slide_layout), then fill
  placeholders (list_placeholders → set_placeholder_text). Do not freehand text boxes
  until those placeholders are filled.
- Turn on headers/footers / slide numbers (part of set_presentation_design) instead
  of drawing folio.
- One layout family per page. Raw shapes are for extras (icons, callouts), not the page.
- Before finishing, call check_deck_layout and fix reported issues once.
```

Everything else is tool schema and host code — inside the 2× prompt-size rule. Do **not** paste upstream SKILL / `references/*.md`.

**`check_deck_layout` (or rename)** as **lint over LO objects**, not a visual design system. Ship it **regardless of the M0′ outcome** — it is cheap and directly targets recorded failures. Report-only by default; optional `fix=true` capped at one obvious pass (e.g. empty-placeholder hint → `set_slide_layout`).

Rules (each returns `{kind, shapes, hint}`):

- `off_canvas` — any shape box outside the page.
- `empty_placeholder` — title/body/subtitle placeholder with no content (the headed `available: []` / empty-fill failure).
- `missing_hf` — slide numbers / footer off when DECK MODE asked for them.
- `blank_master` — still on `Default` / unnamed blank master when other masters exist.
- `overlap` — text boxes overlapping text boxes beyond a small tolerance (recorded Draw failure: `docs/eval/eval-2/headed-failure-autopsy.md`).
- `one_char_line` — text box too narrow for its character count (recorded vertical-text bug).

Geometry helpers stay pure in `plugin/draw/layout.py` (`layout_issues` wrapping `get_draw_tree` / `_shape_box`) so they stay unit-testable without UNO. Do **not** add theme-gutter / folio-shape rules — those assume a WA `DeckTheme`.

### 3.6 Recipes: alias, or invent only after an LO wall

Recipes are **not** a parallel layout engine.

**Allowed now:** a thin alias from a short name to an existing Impress layout in `_LAYOUTS` (`plugin/draw/transitions.py`):

| Alias (optional) | Impress layout |
|------------------|----------------|
| `title_bullets` | `text` (already `add_slide` default) |
| `cover` | `title` (title + subtitle) |
| `section_divider` | `title_only` |
| `two_column_compare` | `two_column_text` |
| `chart` | `chart` / `text_and_chart` |
| `table` | `table` |
| `closing` | `title` or `title_only` |

The alias, if shipped, is a prompt/schema convenience. The host still calls `add_slide(layout=…)` / `set_slide_layout` and fills placeholders. It does **not** expand to host-drawn cards.

**Invented host carriers** (`kpi_row`, `card_grid`, quote chrome, process-flow boxes, `compose_slide` geometry) are allowed **only after** this gate:

1. Headed probe on a real deck job (e.g. mercury space-elevator, or a KPI/card-grid brief).
2. Model is steered to stock designs + masters + layouts + placeholders + HF (and `check_deck_layout`).
3. The probe shows Impress has **no adequate layout** for that page job — not “the model failed to pick `four_objects`”, but “LO’s layouts cannot express a KPI row / card grid at acceptable polish.”
4. Write the headed repro down (shot + tool log + which layouts/designs were tried). Only then may Route B grow a host carrier for *that* gap.

Until that repro exists, `freeform` via the specialized `shapes` domain remains the escape hatch — no default renderer.

### 3.7 Assets (unchanged discipline, no new pipeline)

- **v1:** Unicode / existing image tools; no network required.
- **v2 (opt-in):** `image_generate` / `image_insert` (`plugin/writer/images/images.py`) for one hero — already shipped.
- **v3:** illustration-sheet slicing (upstream). Out of scope until an LO wall says otherwise.

Do not invent a deck-owned asset system.

### 3.8 UNO fidelity — inspect, don’t guess

Per `AGENTS.md`, probe with `plugin/testing_runner.py` on a live Impress instance. Deliverable is a **written probe result**, not a guessed wrapper:

1. **Apply-design path.** How do we apply a shipped `.otp` design (or its master pages) to the current document? Candidates: `XStyleLoader.loadStylesFromURL`, master-page supply/copy, or create-from-template. Also: how to enumerate designs (LO `PathSettings`/`Paths` template dir; `<install>/share/template/common/presnt/`).
2. **Master restyle + page background.** Set background fill (solid / gradient / bitmap) on the master page or draw page, and set the master’s Title/Body text styles. Confirm property names on the live instance.
3. **Theme API (probe, not assume).** Does this LO expose any color/font scheme service for ODF Impress? Expect no. Record the answer; it decides whether row 6 exists.
4. **HF + masters reachability.** Confirm the main chat can reach `slide_masters` / `slide_layouts` / `headers_footers` via `delegate_to_specialized_draw_toolset`, and that §3.4’s core defaults remove the need for that in the deck setup path.

---

## 4. Architecture and file map

LO-first work touches existing Draw modules and adds a small “design/background” surface plus core defaults. Rev 2’s `deck.py` design system is not a starting deliverable.

| File | Change | When |
|------|--------|------|
| `plugin/draw/masters.py` | Use as-is (`list_master_slides`, `get_slide_master`, `set_slide_master`) | M1′ |
| `plugin/draw/transitions.py` | Use as-is (`get_slide_layout`, `set_slide_layout`, `_LAYOUTS`, `four_objects`=23). Optional thin alias names in §3.6 | M1′ / M2′ |
| `plugin/draw/placeholders.py` | Use as-is (core); keep fill-after-layout hints | M1′ / M2′ |
| `plugin/draw/headers_footers.py` | Use as-is; apply once via `set_presentation_design` | M1′ |
| `plugin/draw/pages.py` | `add_slide` already defaults to `text` and applies it; inherit assigned master; accept alias names | **M1′** |
| `plugin/draw/designs.py` (new, name TBD after UNO probe) | Enumerate + apply shipped `.otp` slide designs to the current doc; expose `list_designs` / `apply_design` | **M1′** |
| Page/master background + master restyle helper (module TBD after UNO probe) | Solid / gradient / bitmap on page or master; master text styles | **M1′** |
| Core `set_presentation_design` (where the Impress deck setup lives) | One core call: apply design + assign master + enable HF/slide numbers | **M1′** |
| `plugin/draw/themes.py` (new, **only if §3.8 probe finds a real API**) | `list` / `get` / `apply` color + font schemes | Conditional (M1′ probe) |
| `plugin/framework/prompts.py` | ~8–12 line DECK MODE block (§3.5) | **M2′** |
| `plugin/draw/layout.py` | `layout_issues(...)` over LO boxes / placeholder / HF flags — lint, not carriers | **M2′** |
| Lint tool on an existing specialized base (`check_deck_layout` or similar) | Wraps `get_draw_tree` + HF/master/placeholder reads | **M2′** |
| `scripts/eval_2_draw_oracle.py` | Geometry + HF/master/placeholder fields; M0′ pass criteria fields | M0′ / M2′ |
| `tests/draw/test_*` | `test_draw_layout.py` for lint; `test_designs_uno.py` / `test_master_background_uno.py` for M1′; `test_themes.py` only if themes exist | M1′ / M2′ |
| `docs/draw/impress-specialized-toolsets.md` | Replace the **Themes** ❌ row with the probe result; flip **Templates** when apply-design lands | When tools land |
| `plugin/draw/shapes.py` / tables / charts / images | No new deck wrapper; use existing tools | Ongoing |

**Deferred until LO wall (old rev 2 core) — do not start here:**

| File / symbol | Why deferred |
|---------------|--------------|
| `plugin/draw/deck.py` | Host design system |
| `DeckTheme`, `DECK_THEMES`, `set_deck_theme` / `get_deck_theme` via `udprops` | Parallel look tokens |
| `RECIPES` as host-rendered templates, `compose_slide`, `render_deck_plan` | Parallel layout engine |
| `carrier_boxes` as the primary composition API | Invented geometry vs `_LAYOUTS` |
| `tests/draw/test_deck.py`, `test_deck_uno.py` | Only if M3′ opens |
| Host-drawn folio / eyebrow / full-bleed background as default | Conflicts with master + HF |
| `ToolDrawDeckBase` domain `deck` as core orchestrators | Premature product surface |

**Tier decision (LO-first, corrected):** the deck setup the turn always needs goes in **core** (`set_presentation_design`, plus `add_slide` inherited master) so mercury does not need a delegation chain. `check_deck_layout` and the design/background tools should also be reachable without a long delegation chain once they exist. Raw `shapes` stays specialized as the escape hatch. Masters / layouts / HF remain specialized for one-off edits, but the M1′ default must not require them.

**Reuse, not reinvention:** `CreateDiagram` / `diagram_node_boxes` (`plugin/draw/layout.py`) stay the flowchart path. Do not add a second layout engine for `process_flow` unless M3′’s headed gate fires.

---

## 5. Milestones

Each milestone is independently shippable. **M0′ is a headed probe with pass criteria; the cheap lint and M1′ design/background work ship regardless of its outcome.** Old rev 2 M0–M6 stay deferred to M3′.

| # | Deliverable | Tests / evidence | Notes |
|---|-------------|------------------|-------|
| **M0′** | **Headed probe, 3 arms, with pass criteria.** Arms: (A) baseline title+bullets; (B) + master + layout + placeholder + HF; (C) + applied slide design + background. Question: can mercury get non-generic polish via LO designs? | Headed space-elevator (or peer) with shots + debug log; the §6 oracle run on each arm. **Pass criteria:** master != `Default`; placeholder coverage ≥ 80%; slide numbers on; `layout_issues` count below arm A; and arm B/C preferred over A in a blind A/B. Native UNO inspection of designs/background/theme on this LO. | **Gate for invention only.** If B/C pass → M1′/M2′ are exposure + discipline. If not, record *which* LO feature failed — that write-up *is* the LO wall. |
| **M1′** | **Apply-design + background + core defaults.** `list_designs` / `apply_design` (shipped `.otp`); page/master background + master restyle; core `set_presentation_design` (design + master + HF); `add_slide` inherits the assigned master and accepts aliases. Themes only if §3.8 finds an API. | `test_designs_uno.py`, `test_master_background_uno.py`; `test_draw_layout.py`; run M0′ arms again through the tools | Inspect UNO first. Do not invent `DeckTheme` here. |
| **M2′** | **Prompt / tool routing + lint.** DECK MODE (~8–12 lines); `check_deck_layout` over LO objects; extend the oracle. | Prompt/unit tests; `test_draw_layout.py`; headed re-run | No `compose_slide`. Routing must prevent fill-before-layout (`available: []`). |
| **M3′** | **Invent recipes / `DeckTheme` overlay only where a headed repro shows an LO wall.** | The headed repro from M0′ (or a later KPI/card-grid brief) plus tests *for that gap only* | Old M0–M6 / `plugin/draw/deck.py` / `DECK_THEMES` / host carriers may re-enter **narrowly**, for the failed page job, not as a wholesale design-system replacement. |

### Deferred (rev 2 M0–M6) — only after M3′ opens

| Old # | Old deliverable | Status in rev 4 |
|-------|-----------------|------------------|
| M0 | `carrier_boxes` + `layout_issues` as the start of a host design system | `layout_issues` **lint** ships in M2′. `carrier_boxes` as composition API waits for M3′. |
| M1 | `DeckTheme` + presets + `compose_slide` | **Deferred.** Look comes from applied design + restyled master. |
| M2 | `render_deck_plan` | **Deferred.** DECK MODE points at LO tools. |
| M3 | `check_deck_layout` as renderer QA | **Kept earlier** (M2′) as LO-object lint. |
| M4 | `set_deck_theme` via `udprops` + host recipes | **Deferred.** Theme persist = applied design, not `WriterAgent.DeckTheme`. |
| M5 | Hero image on host-rendered cover | Use existing `image_*` tools without a deck renderer. |
| M6 | Eval geometry + headed before/after | **Keep** as measurement around M0′ and M2′ (§6). |

---

## 6. Measurement

Polish must be falsifiable, and the M0′ gate must be numeric — not a vibe. Extend `scripts/eval_2_draw_oracle.py` (it already parses saved `.odg` XML) with fields that score **LO usage**, not host-chrome presence:

- Geometry: `off_canvas_count`, `text_overlap_count`, `empty_text_count`, `one_char_line_count`.
- LO objects: `empty_placeholder_count`, `has_slide_number` / HF on, `blank_master` (still `Default`), later `has_applied_design` / `has_named_theme` once M1′ exists.
- Do **not** require `has_folio` drawn as a shape — that was a rev 2 host-chrome tell.
- Keep existing semantic anchors (Clearbend, lanes, decision). Add geometry / LO-object fields; do not replace content scoring.
- Report a `polish` sub-score separately from the content score so a deck can be content-HAPPY and polish-low (today’s failure mode).

**M0′ pass criteria (use these numbers to decide whether M3′ is ever needed):**

1. `blank_master == False` (a real design/master is applied).
2. Placeholder coverage ≥ 80% of title+body placeholders on content slides.
3. Slides numbers / HF on when DECK MODE asked for them.
4. `polish` sub-score improves over arm A, with `text_overlap_count` / `one_char_line_count` not worse.
5. Blind A/B: arm B or C preferred over arm A by the reviewer.

Run `scripts/eval_2_headed.py --task draw-primary --score …` on all three arms on the same prompt. If arm B/C already shows the polish jump, that is the evidence to *not* build `deck.py`. Add the same criteria to `docs/eval/eval-2/draw-primary-deliverable/rubric.eval2.md`.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Applying a shipped design to an existing doc has no clean UNO path | Probe first (§3.8); fall back to create-from-template for new decks; record the limitation. Do not invent `DeckTheme` as a workaround. |
| Stock Impress still looks generic even with design + master + background + layout + HF | That is the M0′ question, now with pass criteria. If it fails, M3′ is justified with a written LO wall. |
| Theme API does not exist (likely) | M1′ is design/master/background first; themes are conditional. Replace the **Themes** ❌ row with the probe result so the toolsets doc stops implying a gap that may be OOXML-only. |
| Mercury still freehands boxes and skips designs | M1′ core defaults (`set_presentation_design`, `add_slide` inherit) + M2′ DECK MODE + lint (`empty_placeholder`, `blank_master`, `missing_hf`). Prompt alone is known to fade. |
| `.otp` / apply-design scope creep | Enumerate + apply only; no template manager. No user-authored template editing. |
| Lint grows into a visual design system | Keep rules about LO objects and geometry. No theme-gutter / folio-shape / card-rhythm checks until M3′ exists. |
| Inventing `DeckTheme` too early | §4 marks `deck.py` / `DECK_THEMES` deferred. Review should reject PRs that start rev 2 M0–M6 without an M0′/M3′ repro. |
| Card-grid / KPI page jobs LO layouts cannot express | Allowed *after* the §3.6 gate. Until then, `four_objects` / table / chart layouts + `shapes` escape hatch. |
| Fonts missing / design fonts flatten | Impress design fallback is LO’s problem first; document what apply actually set. Liberation/DejaVu notes apply only if M3′ draws text boxes. |
| Scope creep into an SVG engine | Non-goal. Route A owns that pipeline. |

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
- Inventing a color/font-scheme Theme API before the §3.8 probe says one exists.
- Calc polish transfer (still deferred; Calc has its own failures).

---

## 9. Findings (cut-paste) and debt this avoids

| # | Problem | Where in WA | LO-first piece | Why small-model OK | Risk |
|---|---------|-------------|----------------|--------------------|------|
| 1 | Tools HAPPY, slides look generic | Main-chat Impress; headed space-elevator | Apply a real design + master + background **before** any renderer | 23 designs ship with LO; host defaults, not a new engine | Apply-design UNO path may be awkward → probe |
| 2 | Freehand coordinate soup | `plugin/draw/` shapes domain | Prefer `add_slide(layout=)` / `set_slide_layout` + placeholders | Enum is LO’s `_LAYOUTS`, not a WA recipe table | Model ignores and freehands |
| 3 | Locked look absent | Themes ❌ / Templates ❌ in toolsets doc | Apply-design + master restyle/background (M1′) | Host API, tiny prompt; theme API likely unnecessary | Stock designs still generic → M0′ must say so |
| 4 | LO features exist but are behind delegations | `slide_masters`, `slide_layouts`, `headers_footers` specialized | Core `set_presentation_design` + `add_slide` inherit master | Removes delegation round-trips for a weak model | Adds core tool surface (keep it to one) |
| 5 | No post-build QA | Draw loop | `check_deck_layout` over LO objects | One tool result; no vision | Lint scope creep |
| 6 | Polish not measured | eval-2 Draw rubrics | Geometry + master/HF/placeholder fields, M0′ criteria | Eval-only | Rubric gaming |
| 7 | Full PPT Master quality | Sidebar `ppt-master` mode | Keep Route A on strong models | Already isolated | Users expect main chat = sidebar |
| 8 | Parallel design system | Rev 2 `DeckTheme` / `deck.py` | Do not build until M3′ LO wall | Avoids a second look engine | Premature `deck.py` PR |

Debt this **avoids** (rev 4 vs rev 2/3):

- **No second look engine** until Impress designs/masters/backgrounds/layouts are proven insufficient.
- **No invented Theme API** on the assumption it exists; the probe decides.
- **No host chrome vs LO HF fight** (two folios, backgrounds that hide the master).
- **Lint instead of prose** still stands — and stays cheaper because it reads LO objects mercury already creates.
- **Measured polish** still stands — scoring “applied a design / filled placeholders / HF on,” not “host drew a folio shape.”

Debt that **remains available** after an LO wall (do not pre-build):

- One shape-styling path (`fill_gradient` in `_apply_shape_properties`) if M3′ or page-background work needs it.
- `carrier_boxes` next to `align_boxes` / `distribute_boxes` / `diagram_node_boxes` if a headed KPI/card-grid gap is real.

---

## References (upstream, read selectively)

- Skill entry: `skills/ppt-master/SKILL.md`
- Rubric to compress: `references/visual-review.md`
- Do not load into WA prompts: `references/executor-base.md`, `strategist.md`, `image-generator.md`, `shared-standards-core.md`
- WA integration: `plugin/contrib/ppt_master/README.md`, `plugin/ppt_master/`, `plugin/chatbot/ppt_master.py`
- WA tools / gaps: [impress-specialized-toolsets.md](impress-specialized-toolsets.md) (Themes ❌ → replace with probe result; Templates ❌ → apply-design), [impress-ai-mercury-2.5-headed-findings.md](impress-ai-mercury-2.5-headed-findings.md)
- Shipped LO designs on this machine: `<install>/share/template/common/presnt/*.otp` (23), e.g. Metropolis, Midnightblue, Sunset, Vivid, Focus, Portfolio, Progress, Candy, Vintage
