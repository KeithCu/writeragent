# Steal from PPT Master — small-model Impress polish (advice)

**Audience:** Keith / Chief — incremental Impress (and light Calc) polish ideas that fit mercury-class models.  
**Not a product brief:** do **not** paste hugohe3/ppt-master’s full skill into WriterAgent prompts.  
**Date:** 2026-09-16  
**Upstream:** [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master) (MIT), skill v6.x  
**WA already:** adapter + sidebar PPT-Master mode + forked `plugin/contrib/ppt_master/skill/SKILL.md` (~63 KB orchestration). Main Draw/Impress chat stays on UNO tools (`prompts.py` `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE`).

---

## 1. What “PPT master” is

| Candidate | Match |
|-----------|--------|
| **[hugohe3/ppt-master](https://github.com/hugohe3/ppt-master)** | **Strong / intended.** Agent skill: Plan → SVG pages → native PPTX; Strategist / Image_Generator / Executor roles; huge `references/` (executor-base ~52 KB, strategist ~38 KB, image-generator ~45 KB, shared-standards-core ~32 KB, visual-review rubric, animations, topology…). Officially aimed at strong coding agents + frontier models (README recommends Kimi/Claude + image models). |
| WA `plugin/ppt_master` + contrib | **Already integrated** as optional sidebar workflow (venv smol + upstream scripts on disk). Export/import path, not main-chat Impress AI. See [`docs/archive/ppt-master-integration-plan.md`](../archive/ppt-master-integration-plan.md). |
| Generic “PowerPoint master” templates | Weak — Keith’s “huge Claude prompt” + abandoned wholesale port points at the skill pack, not a slide template pack. |

**Why wholesale port failed (already diagnosed in-repo):** upstream expects bash + filesystem + long references + multi-role gates. LO main-chat Impress uses UNO shape tools. The forked SKILL alone is ~63 KB — far past a 2× prompt-size steal budget for mercury.

**Gap Keith named:** tip Impress tools are HAPPY (e.g. mercury-2.5 space-elevator), **visual/polish** still trails full PPT Master. Spreadsheet AI “looks okay” similarly — structure OK, polish soft.

---

## 2. Stealable techniques (decompose, don’t paste)

Group by *what to steal*, not by upstream file dumps.

### A. Planning vocabulary (tiny, high leverage)

Upstream forces every page to answer:

- **Page job** — what this slide must do for the reader  
- **Relationships** — order / link / parent / membership / contrast / overlap / none  
- **Topology yes/no** — must geometry *carry* those relationships?  
- **Density tag** — `anchor` | `dense` | `breathing`  
- **Page role** — cover / toc / section / content / ending (or similar)

**Steal as:** 5–8 bullet Do+why rules in Draw prompt (or a one-shot “deck outline” JSON before shapes), not the full `design_spec.md` / Strategist.

### B. Device / carrier menu (defaults that do the right thing)

Upstream “devices”: KPI tile, icon-and-label, card band, quote block, divider, hero number — pick **one carrier family per page**, repeat deck-wide.

**Steal as:** a **closed enum** of 6–8 Impress recipes mapped to existing tools (`shape_upsert`, `align_shapes`, `distribute_shapes`, placeholders, charts). Mercury chooses `recipe=kpi_row` instead of inventing layout.

### C. Visual hard/soft rubric (from `visual-review.md`, compressed)

Hard (fix when clear):

1. Text/shape outside canvas  
2. Text overflow / collision  
3. Contrast / text on busy image without scrim  
4. Missing title / declared chrome  

Soft (one pass, don’t thrash):

1. Vertical rhythm too tight/hollow  
2. Same-column x drift  
3. Card grid spacing uneven  
4. Emphasis ≠ page job  

**Steal as:** post-edit checklist + optional `get_image` + `get_draw_tree` self-check (1 iteration). Upstream’s multi-subagent visual-review is **frontier tax** — skip for mercury.

### D. Tool-loop patterns

| Upstream | Small-model WA analogue |
|----------|-------------------------|
| Plan artifact then Do·Check·Act | One outline turn → build → one polish pass |
| Early gate after ~5 pages | After slide 5 (or mid-deck), `get_image` on cover + one content slide only |
| `svg_quality_checker` | Deterministic host checks (bounds, empty text, overlap) > more prompt |
| Quick Generate (no Strategist / no design_spec) | **Closest upstream profile to mercury** — keep capability, drop multi-role + durable plan files |

### E. Eval rubrics

Upstream visual-review JSON schema + Hard/Soft split is gold for **headed eval oracles**, not for system prompt bulk.

**Steal as:** rubric items in eval-2 Draw/Impress tasks (like Clearbend-style criteria), scored offline.

### F. Explicitly skip (for small models / main chat)

- Full Strategist → Image_Generator → Executor role switching  
- Loading entire `executor-base` / `strategist` / `image-generator` into context  
- Confirm UI / live SVG preview servers  
- 187-preset shape vocabulary as prompt text  
- Animations / Morph / narrated video pipeline  
- Dual-path: main chat **and** sidebar PPT-Master both trying to “be” upstream  

Sidebar PPT-Master remains the place for the **full** skill when the user has a clone + strong model.

---

## 3. Small-model filter (2× prompt-size rule)

Assume current Draw system prompt is already large (tools + delegation). A steal that **doubles** that block fails the bar.

| Idea | Approx add | Mercury fit |
|------|------------|-------------|
| Page-job + density + one-carrier Do+why (≤12 lines) | ~0.3–0.5× | **Yes — try first** |
| Closed recipe enum (6 recipes) + tool mapping | ~0.5× | **Yes** |
| Post-build polish checklist (Hard only, 5 lines) | ~0.2× | **Yes** |
| Mid-deck single `get_image` vision pass | tools already exist | **Yes** if vision available; else host checker |
| Deterministic layout lint in tools | code, not prompt | **Yes** (best ROI long-term) |
| Compressed visual-review Soft rules | ~0.5× | Maybe later |
| Quick Generate whole profile / forked SKILL | ~10–20× | **No** |
| Multi-agent Strategist stack | huge | **No** (frontier / sidebar only) |

---

## 4. Ranked incremental next steps

### Try first with mercury (Impress main chat)

1. **Page-job + carrier defaults** in `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` (or a slim Impress-only addendum): one job / one carrier / density tag; prefer `align_shapes`/`distribute_shapes` over free coordinates.  
2. **Recipe presets** (host-side preferred): e.g. `title_bullets`, `kpi_row_3`, `two_column_compare`, `process_flow`, `quote`, `section_divider` — each expands to known shape ops.  
3. **One polish pass**: after build, Hard checklist; call `get_draw_tree` (+ `get_image` if vision). Cap at one repair loop.  
4. **Retest** headed space-elevator (or similar) for polish delta, not tool HAPPY.

### Needs frontier / keep in sidebar PPT-Master

- SVG pipeline, design_spec, image sheet slicing, topology grammar, native chart verification, animations/notes/video.  
- Wholesale skill load (already abandoned for main chat — correct).

### Skip

- Pasting `references/*.md` into prompts.  
- Competing with sidebar mode by re-implementing svg_to_pptx in-prompt.  
- Soft-rule thrash (multiple visual-review iterations).

### Light Calc / sheets transfer

Same *polish grammar*, different tools:

| Steal | Calc analogue |
|-------|----------------|
| Page job | Sheet job (“dashboard” vs “data entry”) |
| Density | Header band + breathing empty rows vs packed grid |
| Carrier | KPI strip / comparison table / chart+callout |
| Hard checklist | Number formats, contrast on fills, merged-header alignment |
| Soft | Column rhythm, accent color competition |

Still: short Do+why, not ppt-master files.

---

## 5. Findings table (cut-paste for tickets)

| # | Problem | Where in WA | Proposed piece | Why small-model OK | Risk |
|---|---------|-------------|----------------|--------------------|------|
| 1 | Tools HAPPY, slides look generic | `prompts.py` Draw template | ≤12-line page-job / density / one-carrier rules | Tiny prompt; no new tools | Over-constrains creative decks |
| 2 | Free-layout coordinate soup | `plugin/draw/` + specialized shapes | Closed recipe enum → existing align/distribute/placeholder tools | Enum ≪ open layout reasoning | Wrong recipe → awkward slide |
| 3 | No post-build visual QA | Draw loop | Hard checklist + one `get_draw_tree`/`get_image` pass | One iteration; Soft rules deferred | Vision cost / mercury ignores checklist |
| 4 | Polish not measured | eval-2 Draw rubrics | Compress visual-review H1–H4/H9 into headed criteria | Eval-only; zero chat tokens | Rubric gaming |
| 5 | Calc “okay not great” | Calc prompt / style tools | Sheet-job + KPI-band defaults | Same pattern as #1 | Theme fights user brand |
| 6 | Full PPT Master quality | Sidebar `ppt-master` mode | Keep full skill on strong models + data root | Already isolated | Users expect main chat = sidebar quality |

---

## 6. Recommendation

**Do not** resume wholesale prompt port. **Do** treat ppt-master as a **pattern library**:

1. Steal **vocabulary + recipes + Hard checklist** into main Impress (and lightly Calc).  
2. Keep **pipeline depth** in sidebar PPT-Master for users who clone upstream and run strong models.  
3. Prefer **host-side recipes/lints** over prompt bulk when choosing between equals (2× rule).

First experiment: #1 + #3 on mercury space-elevator; measure polish subjectively + optional rubric #4. Only if that moves the needle, add recipe enum (#2).

---

## References (upstream, read selectively)

- Skill entry: `skills/ppt-master/SKILL.md`  
- Small-model-adjacent profile: `workflows/profiles/quick-generate.md`  
- Rubric to compress: `references/visual-review.md`  
- Heavy (do not load into WA prompts): `references/executor-base.md`, `strategist.md`, `image-generator.md`, `shared-standards-core.md`  
- WA integration: [`docs/archive/ppt-master-integration-plan.md`](../archive/ppt-master-integration-plan.md), [`plugin/contrib/ppt_master/README.md`](../../plugin/contrib/ppt_master/README.md)
