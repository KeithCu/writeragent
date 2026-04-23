# Dev Plan: HTML Math -> Editable LibreOffice Math

## Purpose

This document turns the research in `docs/libreoffice-html-math-proposal.md` into an execution plan that a coding agent can implement incrementally and that a human reviewer can use to track scope, milestones, and acceptance criteria.

The goal is to add **editable math support** to WriterAgent's HTML import path for LibreOffice Writer.

## Current status (read this first)

| Stage | Status | Notes |
|--------|--------|--------|
| **Phase 0** (spike) | **Done** | MathML → temp `.mml` → LO Math `Formula` → Writer `TextEmbeddedObject`; proven in shipped code + UNO tests. |
| **Phase 1** (MathML MVP) | **Done** (core) | Segmentation, mixed HTML import, inline/display, tests, StarMath `newline` collapse for Writer embeds. Gaps called out in [Phase 1](#phase-1-mathml-mvp) below. |
| **Phase 2** (TeX fallback) | **Not started** | See [Phase 2](#phase-2-tex-aware-fallback). |
| **Phase 3** (robustness) | **Not started** | See [Phase 3](#phase-3-robustness-and-quality). |

**Shipped modules (WriterAgent):** `html_math_segment.py`, `math_mml_convert.py`, `math_formula_insert.py`, orchestration in `format_support.py`; tests under `plugin/tests/` and `plugin/tests/uno/`; agent context in `AGENTS.md`.

**Next priorities (pick from):** Phase 2 TeX; optional `"".join` for `apply_document_content` list `content`; policy for true multi-line / `mtable` vs global `newline` stripping; trim DEBUG logging; optional tool-return warnings; upstream LO Writer OLE + `newline` rendering.

## Problem statement

WriterAgent already has a working HTML import path for normal rich text. That path is good for prose, lists, headings, and tables, but it is not a reliable way to preserve math as editable equation objects.

For math-heavy content, we want this behavior:

- imported HTML with math should produce **real LibreOffice Math objects**
- formulas should remain **editable** in Writer
- non-math HTML should continue using the current import flow
- failures should degrade gracefully and visibly

## Product goal

When WriterAgent receives HTML containing supported math, the user should get a normal Writer document with:

- paragraphs, headings, tables, and formatting preserved as they are today
- inline equations inserted as inline LibreOffice Math objects
- display equations inserted as block-style LibreOffice Math objects
- formulas editable by double-clicking them in Writer

## Non-goals for v1

These are explicitly out of scope for the first implementation:

- arbitrary CSS-drawn visual math with no underlying MathML or TeX source
- full TeX compatibility from day one
- image-first equation rendering
- broad redesign of the current HTML import system
- automatic repair of every malformed or unsupported formula

## User-visible definition of success

The feature is successful when all of the following are true:

1. simple HTML with `<math>` imports as editable LibreOffice formula objects
2. KaTeX-style HTML containing embedded MathML imports as editable formulas
3. normal non-math HTML still imports correctly
4. unsupported math does not silently disappear
5. the implementation is covered by unit tests and UNO tests

**As of 2026-04:** (1)–(4) are **met** for MathML-backed HTML; (5) is **met for core paths** with gaps listed under Phase 1 **Not done yet** (full matrix, structured tool warnings).

## Recommended release strategy

Ship this in three stages (Phase 0 + Phase 1 are **complete in code**; Phases 2–3 remain):

1. **Prototype spike** — **done** (validated as part of Phase 1 delivery).
2. **Phase 1 MVP** — **done** (MathML-aware HTML import; see gaps in the Phase 1 section).
3. **Phase 2 + Phase 3** — **not done** (TeX fallback, then robustness / quality).

This keeps risk low and gives us a natural checkpoint after the hardest technical unknown is resolved.

## Proposed architecture

### High-level flow

The new import pipeline should be:

1. receive HTML input
2. detect whether math is present
3. if no math is present, use the existing HTML import flow unchanged
4. if math is present, parse the content into a sequence of:
   - normal HTML/text segments
   - math segments
5. convert each math segment into a LibreOffice Math command string
6. insert normal HTML segments through the current import helpers
7. insert math segments as real formula objects at the correct position

### Design principle

Do not try to teach the generic HTML filter to understand equations.

Instead, build a thin math-aware layer in front of the existing HTML import system.

### Preferred conversion target

The insertion target is a Writer formula object backed by LibreOffice Math, with the final formula stored as a StarMath command string through the UNO `Formula` property.

## Scope by phase

## Phase 0: Technical spike

**Status: Done** (subsumed into shipped Phase 1; no separate throwaway prototype remains).

### Objective

Validate the hardest technical question first:

Can we reliably turn a MathML string into an editable Writer formula object via UNO?

### Deliverable

Originally a standalone prototype; **delivered** as production helpers + tests:

- [x] One MathML string → Writer formula object via UNO
- [x] LibreOffice MathML import reused (hidden Math doc from temp `.mml`)
- [x] `Formula` read back and post-processed for Writer embed (`newline` collapse)
- [x] Inline vs block insertion path (`math_formula_insert` + `display="block"` / `mode="display"`)
- [x] Failure modes: visible `[Math import failed]…` fallback + debug logging

### Tasks

1. [x] UNO insertion pattern for Writer formula objects (`TextEmbeddedObject` + Math CLSID)
2. [x] Reuse LibreOffice MathML import programmatically
3. [x] Read `Formula` from imported Math document
4. [x] Inline vs block constraints (coded + UNO-tested)
5. [x] Document failure modes (this plan + `AGENTS.md` + in-code comments)

### Exit criteria

**Met:** **preferred path** — LibreOffice converts MathML to StarMath well enough to reuse. **Internal MathML→StarMath converter** not required for MVP.

## Phase 1: MathML MVP

**Status: Done (core pipeline).** Remaining gaps are explicit under **Not done yet** so they are not mistaken for future phases.

### Objective

Support the most structured and realistic first wave of HTML math inputs.

### Supported inputs

| Input | Status |
|--------|--------|
| Explicit `<math>…</math>` | **Done** |
| KaTeX / MathJax HTML that **contains** `<math>` (same extractor) | **Done** (no separate DOM fingerprint beyond `<math>`) |
| Math-heavy HTML **without** `<math>` (CSS-only, TeX-only) | **Not in scope** (Phase 2 / non-goals) |

### Functional scope

| Item | Status |
|------|--------|
| Detect math-bearing HTML (`<math`) | **Done** |
| Segment into ordered HTML + math runs | **Done** |
| Preserve order in document | **Done** |
| Inline formulas | **Done** |
| Display (`display="block"`, `mode="display"`) as own paragraph | **Done** |
| Non-math HTML unchanged when no `<math>` | **Done** |

### Tasks

1. [x] Math detection (`html_fragment_contains_mathml`)
2. [x] Segmentation (`segment_html_with_mathml`, tag-boundary scanner)
3. [x] Formula insertion (`math_formula_insert.py`)
4. [x] MathML → StarMath (`math_mml_convert.py`, LO Math doc + `collapse_starmath_newline_tokens_for_writer_embed`)
5. [x] Orchestration (`format_support.py`, all `insertDocumentFromURL` entry paths + cursor-to-end after each segment)
6. [x] Unsupported conversion: `[Math import failed]…` text + debug log (not structured tool JSON)
7. [x] Tests: unit + UNO (not every row of the “minimum test matrix” below—see **Not done yet**)

### Acceptance criteria

| Criterion | Status |
|-----------|--------|
| Inline MathML → inline editable formula | **Done** (UNO + real usage) |
| Display MathML → block formula | **Done** |
| Surrounding prose order | **Done** |
| Headings / paragraphs / tables without math unchanged | **Done** (regression via existing + new tests) |
| Unsupported math not silently dropped | **Done** (visible fallback string) |

### Code map (WriterAgent)

| Area | Module |
|------|--------|
| Segmentation | [`plugin/modules/writer/html_math_segment.py`](../plugin/modules/writer/html_math_segment.py) |
| MathML → StarMath + `newline` mitigation | [`plugin/modules/writer/math_mml_convert.py`](../plugin/modules/writer/math_mml_convert.py) |
| OLE insert | [`plugin/modules/writer/math_formula_insert.py`](../plugin/modules/writer/math_formula_insert.py) |
| HTML + math orchestration, `content_has_markup` `<math` | [`plugin/modules/writer/format_support.py`](../plugin/modules/writer/format_support.py) |
| Unit tests | `plugin/tests/test_html_math_segment.py`, `plugin/tests/test_math_mml_convert.py` |
| UNO tests | `plugin/tests/uno/test_writer_mathml_import.py` |
| Agent orientation | [`AGENTS.md`](../AGENTS.md) |

### Implementation notes (LibreOffice / Writer)

- **StarMath `newline` (word)** — Not ASCII `\n`. LibreOffice MathML import builds a root layout that serializes with the `newline` **operator** (`starmath` `SmNodeToTextVisitor`, root table-of-lines from `mathmlimport.cxx`). Writer’s **embedded** formula paint often showed **`?`** per `newline`; we **collapse** those tokens for Writer embeds in `math_mml_convert.py` (comment points at LO sources). **Tradeoff:** true multi-line / `mtable` via `newline` is not preserved until we add a policy (see **Current status**).
- **List `content` arrays** — `"\n".join` still used in `apply_document_content`; newlines stay **between** HTML fragments only; optional change to `"".join` is backlog, not done.
- **Cursor** — After each `insertDocumentFromURL`, cursor must move to document end or later segments overwrite earlier HTML.
- **DEBUG logging** — Optional verbose logs in `content.py` / `format_support.py` / `math_mml_convert.py`; trim or gate when stable.

### Not done yet (still Phase 1 scope or immediate backlog)

These are **not** Phase 2 TeX work unless noted:

- [ ] Full **minimum test matrix** row coverage (e.g. malformed MathML fixture, KaTeX-shaped HTML file fixture, table-with-math) beyond what `test_writer_mathml_import` already hits.
- [ ] **Structured** tool errors / `warnings` array on `apply_document_content` return JSON (today: plain fallback string + logs).
- [ ] **KaTeX/MathJax DOM** detection without relying on `<math>` (only needed if we must handle math with no MathML).
- [ ] **Reviewer-facing** example document set in-repo (optional `docs/` or fixtures zip).

## Phase 2: TeX-aware fallback

**Status: Not started.**

### Objective

Handle common sources where the HTML contains TeX source or TeX-style delimiters rather than clean MathML.

### Supported inputs

- `$...$`
- `$$...$$`
- `\(...\)`
- `\[...\]`
- TeX annotations preserved in upstream KaTeX/MathJax output

### Tasks

1. [ ] TeX source detection
2. [ ] Precedence rules between MathML and TeX when both are present
3. [ ] TeX normalization/conversion approach
4. [ ] Convert TeX → MathML or StarMath
5. [ ] Reuse Phase 1 formula insertion path
6. [ ] Tests for mixed HTML + TeX

### Acceptance criteria

- common TeX snippets render as editable formulas
- structured MathML remains preferred when both forms are available
- conversion failures are visible and non-destructive

## Phase 3: Robustness and quality

**Status: Not started.**

### Objective

Improve quality and reduce edge-case regressions after MVP ships.

### Focus areas

- spacing and baseline behavior for inline formulas
- better support for matrices and aligned expressions
- improved fallback messaging
- better diagnostics in logs
- broader compatibility across real-world generated HTML

### Acceptance criteria

- formula placement feels natural in ordinary Writer paragraphs
- representative complex formulas degrade gracefully
- debugging failed conversions is possible from logs and test output

## Work breakdown by implementation area

Cross-cutting status: **1–3 done** for the MathML MVP path; **4 partial**; **5 partial** (see Phase 1 **Not done yet**).

## 1. Parsing and detection

**Status: Partial — MathML path done; TeX / extra DOM heuristics not done.**

### Goal

Detect supported math reliably before content hits the normal HTML filter.

### Expected work

| Item | Status |
|------|--------|
| `<math>` tags | **Done** (`html_math_segment.py`) |
| KaTeX / MathJax DOM signatures (without `<math>`) | **Not done** |
| TeX delimiters | **Not done** (Phase 2) |
| Segmentation preserving source order | **Done** |
| DOM vs token vs hybrid | **Done** for MVP: tag-boundary scan on string (not full HTML5 DOM) |

### Review questions

- does the parser avoid corrupting non-math HTML? — **Yes** for segments; HTML still goes through LO filter per chunk.
- does detection prefer structured sources over heuristic guesses? — **N/A** until TeX path; MathML-first by construction.

## 2. Conversion

**Status: Done for LO-backed MathML; internal converter not built.**

### Goal

Convert detected math into a form LibreOffice Writer can insert as editable formulas.

### Preferred implementation

Use LibreOffice's own MathML import/conversion path where practical. — **Done.**

### Fallback implementation

Internal MathML → StarMath subset converter if UNO path fails. — **Not done** (not required after spike).

### Review questions

- what exact subset of MathML is supported in MVP? — **Whatever LO imports**; failures → fallback string.
- where does conversion fail, and how is that reported? — **Visible text + debug log**; not structured API.
- undocumented LO behavior? — **Yes** (accepted); `newline` behavior documented in Phase 1 notes.

## 3. Writer insertion

**Status: Done.**

### Goal

Insert formulas as real document objects without breaking the existing content flow.

### Expected work

| Item | Status |
|------|--------|
| Create / insert formula objects | **Done** |
| Set `Formula` via UNO | **Done** |
| Inline vs display | **Done** |
| Paragraph handling for block display | **Done** (breaks around object) |

## 4. Error handling and fallback

**Status: Partial.**

### Goal

Make unsupported math visible, recoverable, and non-destructive.

### Expected behavior

| Item | Status |
|------|--------|
| Do not silently drop | **Done** |
| Visible fallback | **Done** (`[Math import failed]…`) |
| Structured warning in tool return | **Not done** |
| Log context for debug | **Done** (DEBUG lines; deep redaction TBD) |

## 5. Testing

**Status: Partial** — core unit + UNO present; matrix rows in “minimum test matrix” not all dedicated tests.

### Goal

Meet the repo standard for both logic coverage and UNO-backed document behavior.

### Required test layers

| Layer | Status |
|-------|--------|
| Unit: detection, segmentation, conversion helpers, collapse | **Done** |
| UNO: insert, mixed HTML, `apply_document_content` | **Done** |
| UNO: every matrix row below | **Partial** |

### Minimum test matrix

| Case | Status |
|------|--------|
| simple inline fraction | **Covered** (via real MathML + UNO) |
| simple display equation | **Covered** (UNO) |
| multiple formulas in one fragment | **Covered** (usage + segments) |
| mixed text + formula + text | **Covered** (UNO / integration) |
| KaTeX-style HTML with embedded MathML | **Light** (unit segment test; no large fixture file) |
| malformed or unsupported MathML | **Light** (segmentation unclosed-math; dedicated malformed conversion test optional) |

## Milestones

## Milestone 1: Spike complete

**Status: Done.**

Deliverables:

- [x] documented result (this plan + code comments)
- [x] chosen conversion strategy (LO Math temp `.mml`)
- [x] blockers identified and mitigated (`newline`, cursor)

Go/no-go decision:

- proceed to MVP only if formula insertion is proven viable — **Done; MVP shipped.**

## Milestone 2: MathML MVP complete

**Status: Done (core);** reviewer examples + full test matrix still optional (Phase 1 **Not done yet**).

Deliverables:

- [x] math-aware HTML import for MathML-backed input
- [x] passing unit tests (core)
- [x] passing UNO tests (core)
- [ ] reviewer-ready examples (optional backlog)

Go/no-go decision:

- ship behind normal feature flow if regression risk is acceptable — **Product decision** (implementation is in tree).

## Milestone 3: TeX fallback complete

**Status: Not started.**

Deliverables:

- TeX-aware support for common delimiters
- extended tests
- documented support boundaries

## Risks

## Risk 1: LibreOffice MathML import quality is inconsistent

### Impact

Some formulas may import imperfectly even if the pipeline works.

### Mitigation

- start with a narrow supported subset
- keep graceful fallback behavior
- verify representative fixtures early

## Risk 2: UNO insertion is more brittle than expected

### Impact

The preferred architecture may stall on integration details.

### Mitigation

- isolate the spike first
- keep the fallback path open
- avoid coupling the whole HTML system to one uncertain API trick

## Risk 3: Mixed content ordering becomes hard to preserve

### Impact

Text and formulas could appear in the wrong order or with broken paragraph structure.

### Mitigation

- design around ordered segments from day one
- add mixed-content UNO tests before broad rollout

## Risk 4: Edge-case parsing expands scope too quickly

### Impact

The implementation could become an open-ended parser project.

### Mitigation

- clearly define MVP input types
- reject unsupported forms explicitly
- postpone broad TeX coverage until Phase 2

## Open product decisions

| # | Question | Resolution (as of 2026-04) |
|---|-----------|----------------------------|
| 1 | Visible fallback for unsupported formulas: plain text, marker, or both? | **Marker + snippet** — `[Math import failed]` plus truncated source in document; not silent. |
| 2 | Ship MathML-only before TeX? | **Yes** — Phase 2 not started. |
| 3 | Auto-repair malformed MathML? | **No** — unclosed `<math>` tails fall back to HTML segment behavior; no repair pass. |
| 4 | Warnings in logs only vs tool JSON? | **Logs + visible insert** — structured `warnings` in `apply_document_content` return **not implemented** (backlog). |

## Task sequence (for agents)

**Completed (Phase 0–1):**

1. [x] Formula insertion from MathML via UNO
2. [x] Conversion path (LO Math `.mml` import)
3. [x] Writer formula helper
4. [x] Math detection + segmentation
5. [x] Wire into `format_support` HTML import paths
6. [x] Unit tests (segmentation, StarMath collapse)
7. [x] UNO tests (conversion, mixed insert, tool path)
8. [x] Non-math HTML regression (existing suite + new cases)
9. [x] StarMath `newline` mitigation for Writer embeds

**Remaining (pick order):**

- [ ] Phase 1 backlog: full test matrix, optional `warnings` in tool return, reviewer examples
- [ ] Optional: `"".join` for list `content` in `apply_document_content`
- [ ] Phase 2: TeX fallback (then Milestone 3)
- [ ] Phase 3: robustness
- [ ] Trim DEBUG logging when stable

## Reviewer checklist

- is the supported scope narrow and realistic for MVP? — **Yes** (MathML in HTML only).
- is the hardest technical unknown resolved before broad coding starts? — **Yes** (spike folded into MVP).
- does the plan preserve the existing HTML import path for non-math content? — **Yes**.
- are failure modes visible and non-destructive? — **Yes** (string fallback; structured warnings optional).
- are both unit tests and UNO tests included? — **Core yes**; full matrix partial — see §5 Testing.
- are inline and display behaviors explicitly covered? — **Yes** (code + UNO).

## Implementation handoff summary

Work **after** Phase 1 should assume:

- **Done:** MathML-in-HTML → editable Writer formulas via preprocess + LO conversion + `newline` collapse.
- **Primary backlog:** Phase 2 TeX, then Phase 3 quality; Phase 1 “Not done yet” polish.
- **Architecture rule:** keep preprocessing in front of the StarWriter HTML filter; do not teach the HTML filter math.
- **Testing rule:** extend matrix rows and tool JSON when adding features.
- **Fallback rule:** never silently lose formulas (unchanged).

## Related documents

- Research and architecture proposal: `docs/libreoffice-html-math-proposal.md`
- Writer HTML import + math orchestration: `plugin/modules/writer/format_support.py`
- MathML segmentation: `plugin/modules/writer/html_math_segment.py`
- MathML → StarMath + Writer newline mitigation: `plugin/modules/writer/math_mml_convert.py`
- Formula OLE insert: `plugin/modules/writer/math_formula_insert.py`
