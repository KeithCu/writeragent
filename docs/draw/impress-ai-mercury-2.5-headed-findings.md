# Impress AI headed findings: `inception/mercury-2.5` (space elevator)

**Status:** Findings only — no product fix in this PR  
**Date:** 2026-09-16  
**Verdict:** **MIXED**  
**Related:** [impress-specialized-toolsets.md](impress-specialized-toolsets.md)

Exploratory headed play (not a formal eval harness). Goal was to see whether Impress chat on tip does a good job with a fast/cheap model, and where tools vs prompts vs model limits show up.

---

## Setup

| Item | Value |
|------|--------|
| Tip | `a3e0b2a1` (master after #778) |
| Extension | WriterAgent **debug** deploy (`make deploy`) |
| Model | **`inception/mercury-2.5` only** (OpenRouter; confirmed `used_model` in debug log) |
| Doc | New Impress presentation |
| Artifacts | `docs/draw/impress-ai-mercury-explore/` (shots below); full debug log kept on the box under `/workspace/impress-ai-explore/writeragent_debug.log` |

### Ops gotcha: LibreHarper + WriterAgent ChatPanel

With **LibreHarper** still installed alongside WriterAgent, opening the chat sidebar in Impress failed repeatedly:

```text
ChatPanel createContainerWindow returned no window
…/LibreHarper.oxt/Dialogs/ChatPanelDialog.xdl  exists=False
```

LO resolved the ChatPanel factory UI to the LibreHarper package path (no `ChatPanelDialog.xdl` there). Removing LibreHarper and keeping WriterAgent-only fixed the sidebar. Treat co-install ChatPanel ownership as a real footgun for headed Impress/Writer QA.

---

## Lead task

Ask chat to create a **multi-slide presentation about the space elevator**, then light follow-ups on the same deck (edit, structural add, one visual shape).

### Screenshots

![Initial multi-slide deck](impress-ai-mercury-explore/01-space-elevator-deck.png)

*Fig. 1 — After create: title + concept/benefits/challenges style deck (4 slides visible).*

![Slide 2 after edit](impress-ai-mercury-explore/02-edited.png)

*Fig. 2 — Slide 2 retitled “Why It Matters” with an extra bullet (thumbnail lag vs canvas).*

![After structural add](impress-ai-mercury-explore/03-structural.png)

*Fig. 3 — Structural follow-up (fifth slide / path-forward content in the run).*

![Green rectangle on title slide](impress-ai-mercury-explore/04-visual.png)

*Fig. 4 — Visual ask: green rectangle on slide 1 via shapes specialist.*

---

## What worked

| Task | Result |
|------|--------|
| Create multi-slide deck from prompt | **OK** — coherent title + bullet slides on the space elevator |
| Edit existing slide (title / body / bullet) | **OK** after an off-by-one correction |
| Structural `add_slide` | **OK** — ended at 5 slides |
| Visual shape | **OK** — `delegate_to_specialized_draw_toolset(domain="shapes")` → `shape_upsert` create rectangle, green fill, on page 0 |
| Model identity | Confirmed `inception/mercury-2.5` on OpenRouter for chat + nested specialist calls |

Mercury was fast enough for interactive headed play and did call tools (not only chat prose) once the run was unstuck.

---

## What failed / friction

### 1. Placeholder roles with `available: []`

Repeated tool errors of the form:

```json
{"status": "error", "code": "TOOL_EXECUTION_ERROR",
 "message": "Placeholder 'title' not found.",
 "details": {"available": [], "tool_name": "set_placeholder_text", "doc_type": "impress"}}
```

Same pattern for `'body'`. Parallel `list_placeholders` often returned `count: 0` (or empty roles) until layout work / retries. The model then looped `set_placeholder_text` and `slide_layouts` delegate tasks.

**Code:** `plugin/draw/placeholders.py` (`list_placeholders`, `set_placeholder_text` — role lookup calls `_list_placeholders` and errors when role missing).  
**Layouts:** specialized domain `slide_layouts` via `plugin/draw/transitions.py` (`set_slide_layout` / `get_slide_layout`).  
**Add slide:** `plugin/draw/pages.py` (`add_slide`).

### 2. Slide / page index confusion

Off-by-one and “edit the wrong slide” behavior showed up in the headed run (agent report + thumbnail vs canvas mismatch in Fig. 2). Prompt/tool surface mixes conventions:

- Many Draw tools: **0-based** `page`
- `get_image`: **1-based** `page=N` (called out in [impress-specialized-toolsets.md](impress-specialized-toolsets.md) and the Draw system prompt)

Mercury recovered, but burned turns.

### 3. Blank-ish / layout-incomplete slides

At least one slide looked empty in the pane / thumbnail while content existed elsewhere; layout reassignment was attempted when placeholders were missing. Default “add then fill by role” is fragile if the new page is not a title+body layout.

### 4. Model limits

- **No vision** for this model (`ModelCapability.NONE` in log) — `get_image` page renders are not useful for mercury-2.5.
- Initial turn sometimes answered generically before committing to tools (prompt already says “do not explain — do the operation”; still happened once).
- Prompt markets “polished, professional, and colorful” (`DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` in `plugin/framework/prompts.py`) but the successful path was plain title + bullets + one rectangle — expectation mismatch more than a hard failure.

### 5. Stability note

No SEGV in the successful session. Early `UnoObjectError` storm was the LibreHarper ChatPanel path, not an Impress tool crash. No `assert_main_thread` fail observed as a sign-off blocker for the successful deck work.

---

## Diagnosis: tools vs prompts vs model

| Layer | Assessment |
|-------|------------|
| **Tools** | Core set is enough for create / edit / add / shapes. Weak spot is **placeholder discovery + layout coupling**: role-based `set_placeholder_text` fails hard when the slide has no named placeholders yet (`available: []`). |
| **Prompts** | Draw/Impress prompt lists tools and says verify `status='error'`, but does not hard-steer “always `list_placeholders` → use **index** if roles empty; set layout before fill.” “Colorful” steers ambition without a default visual recipe. |
| **Model (mercury-2.5)** | Adequate tool caller for this scoped play; weak at index/layout recovery; no vision. Not the main blocker vs placeholder/layout brittleness. |

---

## Possible solutions (options, not a mandate)

### A. Default layout on `add_slide` (tool)

**Idea:** New Impress slides get a text/title-content layout so title/body placeholders exist before the first `set_placeholder_text`.

| Pros | Cons |
|------|------|
| Removes the most common `available: []` failure | May surprise users who want blank/draw-only pages |
| Small, local change in `pages.py` / layout helper | Need a stable layout name across LO versions |

### B. Prompt / tool-description steer (prompt + schema text)

**Idea:** In `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE` and tool descriptions: before `set_placeholder_text` by role, call `list_placeholders`; if empty, `delegate_to_specialized_draw_toolset(domain="slide_layouts")` then retry; prefer **index** when roles are absent. Explicitly remind 0-based vs `get_image` 1-based.

| Pros | Cons |
|------|------|
| No behavior change for blank-slide power users | Models still ignore prompts sometimes |
| Cheap to try | Does not fix empty layouts by itself |

### C. Friendlier `set_placeholder_text` errors + auto-fallback (tool)

**Idea:** When role missing, return structured hint (`try_indices`, `suggest_layout`) or optionally create/fill via `OutlinerShape`/`TitleTextShape` from `get_draw_tree` when placeholders API is empty but shapes exist.

| Pros | Cons |
|------|------|
| Matches how content sometimes already appears (TitleTextShape / OutlinerShape in doc snapshot) | More branching; risk of writing the wrong shape |
| Cuts retry loops | Needs careful tests |

### D. Index hygiene (docs + schemas)

**Idea:** One convention in model-facing strings (or dual params with explicit names `page_0based` / `page_1based` only where needed). Cross-link in tool descriptions.

| Pros | Cons |
|------|------|
| Reduces off-by-one edits | Rename/dual API is noisy if done bluntly |

### E. ChatPanel package ownership (framework)

**Idea:** Ensure ChatPanel XDL resolves to WriterAgent when both OXTs are installed (or LibreHarper must not register ChatPanel factory).

| Pros | Cons |
|------|------|
| Unblocks co-install headed QA | Separate from Impress quality; still worth tracking |

### F. Model choice for visual QA

**Idea:** For headed Impress polish / `get_image` loops, prefer a vision-capable model; keep mercury for cheap structural smoke.

| Pros | Cons |
|------|------|
| Matches tool surface (`get_image`) | Cost; not a substitute for A–C |

**Suggested cut order if doing a small follow-up later:** **A or C** (stop empty-placeholder loops) → **B** (steer) → **D** (index clarity) → **E** (co-install) as ops/framework. Skip mega-PR; Keith will pick.

---

## Code map (quick)

| Concern | Path |
|---------|------|
| Placeholder list/get/set | `plugin/draw/placeholders.py` |
| add/delete/list slides | `plugin/draw/pages.py` |
| Draw/Impress system prompt | `plugin/framework/prompts.py` (`DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE`) |
| Specialized gateway | `plugin/draw/specialized.py` |
| Shapes | `plugin/draw/shapes.py` |
| Layouts / transitions | `plugin/draw/transitions.py` |
| Tool catalog (human) | `docs/draw/impress-specialized-toolsets.md` |

---

## Out of scope for this PR

- No product code changes
- No issue close keywords
- No formal GDPval / string-harness Impress suite

