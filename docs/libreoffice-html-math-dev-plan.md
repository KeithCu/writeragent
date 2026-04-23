# Dev Plan: HTML Math -> Editable LibreOffice Math

## Purpose

This document turns the research in `docs/libreoffice-html-math-proposal.md` into an execution plan that a coding agent can implement incrementally and that a human reviewer can use to track scope, milestones, and acceptance criteria.

The goal is to add **editable math support** to WriterAgent's HTML import path for LibreOffice Writer.

## Current status (read this first)

| Stage | Status | Notes |
|--------|--------|--------|
| **Phase 0** (spike) | **Done** | MathML → temp `.mml` → LO Math `Formula` → Writer `TextEmbeddedObject`; proven in shipped code + UNO tests. |
| **Phase 1** (MathML MVP) | **Done** (core) | Segmentation, mixed HTML import, inline/display, tests, StarMath `newline` collapse for Writer embeds. Gaps called out in [Phase 1](#phase-1-mathml-mvp) below. |
| **Phase 2** (TeX fallback) | **Done** (core) | Delimiters `$…$`, `$$…$$`, `\(...\)`, `\[...\]`; `latex2mathml` → same LO MathML path; mixed scan + precedence in `html_math_segment.py`; `convert_latex_to_starmath` in `math_mml_convert.py`; prompts in `constants.py` / `apply_document_content` schema. Optional: KaTeX `<annotation encoding="application/x-tex">` retry on MathML failure (not implemented). |
| **Phase 3** (robustness) | **Not started** | See [Phase 3](#phase-3-robustness-and-quality). |

**Shipped modules (WriterAgent):** `html_math_segment.py` (MathML + TeX segmentation), `math_mml_convert.py` (MathML + LaTeX→MathML→StarMath), `math_formula_insert.py`, orchestration in `format_support.py` (`html_fragment_contains_mixed_math`, `content_has_markup` TeX patterns); vendored **`latex2mathml`** via `requirements-vendor.txt` (see `pyproject.toml` dev group for typecheck); tests under `plugin/tests/` and `plugin/tests/uno/`; agent context in `AGENTS.md`; model hints in `plugin/framework/constants.py` (`WRITER_APPLY_DOCUMENT_HTML_RULES`) and `plugin/modules/writer/content.py` (`ApplyDocumentContent`).

**Next priorities (pick from):** Phase 3 quality; Phase 1 backlog (test matrix, optional `warnings` in tool return); optional `"".join` for `apply_document_content` list `content`; policy for true multi-line / `mtable` vs global `newline` stripping; trim DEBUG logging; upstream LO Writer OLE + `newline` rendering; optional KaTeX annotation fallback.

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

**As of 2026-04:** (1)–(4) are **met** for MathML-backed HTML; (5) is **met for core paths** with gaps listed under Phase 1 **Not done yet** (full matrix, structured tool warnings). **As of Phase 2 ship:** common TeX-delimited math in the same HTML import path meets the same visibility and test bar as MathML for core cases (subset of LaTeX via `latex2mathml`; not full TeX compatibility).

## Recommended release strategy

Ship this in three stages (Phase 0–2 are **complete in code** for core paths; Phase 3 remains):

1. **Prototype spike** — **done** (validated as part of Phase 1 delivery).
2. **Phase 1 MVP** — **done** (MathML-aware HTML import; see gaps in the Phase 1 section).
3. **Phase 2 TeX** — **done** (core delimiter path + tests + prompts); Phase 3 robustness / quality **not done**.

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
| Math-heavy HTML **without** `<math>` but **with** TeX delimiters (`$…$`, `$$…$$`, `\(...\)`, `\[...\]`) | **Done** (Phase 2; subset of LaTeX) |
| Math-heavy HTML **without** `<math>` or TeX (CSS-only / no machine-readable math) | **Not in scope** (non-goals) |

### Functional scope

| Item | Status |
|------|--------|
| Detect math-bearing HTML (`<math`) | **Done** |
| Segment into ordered HTML + math runs | **Done** |
| Preserve order in document | **Done** |
| Inline formulas | **Done** |
| Display (`display="block"`, `mode="display"`) as own paragraph | **Done** |
| Non-math HTML unchanged when no math markers (`<math` / TeX delimiters) | **Done** |

### Tasks

1. [x] Math detection (`html_fragment_contains_mathml`; Phase 2 adds `html_fragment_contains_tex_math` / `html_fragment_contains_mixed_math`)
2. [x] Segmentation (`segment_html_with_mixed_math`; `segment_html_with_mathml` is an alias)
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
| Segmentation (MathML + TeX order) | [`plugin/modules/writer/html_math_segment.py`](../plugin/modules/writer/html_math_segment.py) |
| MathML → StarMath; LaTeX → MathML (`latex2mathml`) → StarMath; `newline` mitigation | [`plugin/modules/writer/math_mml_convert.py`](../plugin/modules/writer/math_mml_convert.py) |
| OLE insert | [`plugin/modules/writer/math_formula_insert.py`](../plugin/modules/writer/math_formula_insert.py) |
| HTML + math orchestration, `content_has_markup` (`<math`, `$$`, `\(`, `\[`) | [`plugin/modules/writer/format_support.py`](../plugin/modules/writer/format_support.py) |
| Chat / tool text for `apply_document_content` | [`plugin/framework/constants.py`](../plugin/framework/constants.py) (`WRITER_APPLY_DOCUMENT_HTML_RULES`), [`plugin/modules/writer/content.py`](../plugin/modules/writer/content.py) (`ApplyDocumentContent`) |
| Unit tests | `plugin/tests/test_html_math_segment.py`, `plugin/tests/test_math_mml_convert.py` |
| UNO tests | `plugin/tests/uno/test_writer_mathml_import.py` (MathML + TeX cases) |
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

**Status: Done (core).** Stretch / backlog: KaTeX-style `<annotation encoding="application/x-tex">` retry when MathML import fails (not implemented).

### Objective

Handle common sources where the HTML contains TeX source or TeX-style delimiters rather than clean MathML.

### Supported inputs

- `$...$`
- `$$...$$`
- `\(...\)`
- `\[...\]`
- TeX annotations preserved in upstream KaTeX/MathJax output (**not** auto-mined yet; prefer embedded `<math>` when present)

### Tasks

1. [x] TeX source detection (`html_fragment_contains_tex_math`, `html_fragment_contains_mixed_math`)
2. [x] Precedence rules between MathML and TeX when both are present (left-to-right; earliest opener wins)
3. [x] TeX normalization/conversion approach (vendored `latex2mathml` → MathML string → existing LO path)
4. [x] Convert TeX → MathML → StarMath (`convert_latex_to_starmath` in `math_mml_convert.py`)
5. [x] Reuse Phase 1 formula insertion path (`insert_writer_math_formula` unchanged)
6. [x] Tests for mixed HTML + TeX (unit + UNO in `test_writer_mathml_import.py` and `test_html_math_segment.py`)

### Acceptance criteria

| Criterion | Status |
|-----------|--------|
| common TeX snippets render as editable formulas | **Done** (core UNO + unit coverage) |
| structured MathML remains preferred when both forms are available | **Done** (document order: earliest `<math` or TeX opener wins; prompts tell models to emit MathML first when the source already has it) |
| conversion failures are visible and non-destructive | **Done** (same `[Math import failed]…` path as MathML) |

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

Cross-cutting status: **1** done for MathML + common TeX delimiters; KaTeX/MathJax **without** `<math>` or TeX markers still **not done**. **2** done (LO MathML + `latex2mathml` → MathML). **3** done (insertion). **4** partial (structured tool warnings). **5** partial (see Phase 1 **Not done yet**).

## 1. Parsing and detection

**Status: Partial — MathML + TeX delimiter path done; KaTeX DOM-only / `<annotation>` retry not done.**

### Goal

Detect supported math reliably before content hits the normal HTML filter.

### Expected work

| Item | Status |
|------|--------|
| `<math>` tags | **Done** (`html_math_segment.py`) |
| KaTeX / MathJax DOM signatures (without `<math>`) | **Not done** |
| TeX delimiters (`$`, `$$`, `\(\)`, `\[...\]`) | **Done** (`html_math_segment.py`) |
| Segmentation preserving source order | **Done** |
| DOM vs token vs hybrid | **Done** for MVP: tag-boundary scan on string (not full HTML5 DOM) |

### Review questions

- does the parser avoid corrupting non-math HTML? — **Yes** for segments; HTML still goes through LO filter per chunk.
- does detection prefer structured sources over heuristic guesses? — **Partial**: left-to-right earliest opener; prompts and typical KaTeX output favor emitting `<math>` first when both exist.

## 2. Conversion

**Status: Done** for LO-backed MathML and for **TeX → MathML** via vendored `latex2mathml` then the same LO path; internal standalone MathML→StarMath converter **not built** (not required).

### Goal

Convert detected math into a form LibreOffice Writer can insert as editable formulas.

### Preferred implementation

Use LibreOffice's own MathML import/conversion path where practical. — **Done.**

### Fallback implementation

Internal MathML → StarMath subset converter if UNO path fails. — **Not done** (not required after spike).

TeX → MathML (`latex2mathml`) before LO import. — **Done** (Phase 2).

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
| TeX-only + mixed MathML + TeX in HTML | **Covered** (unit + UNO core cases) |
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

**Status: Done (core).**

Deliverables:

- [x] TeX-aware support for common delimiters
- [x] extended tests (unit + UNO; not every matrix row)
- [x] documented support boundaries (`AGENTS.md`, this plan, `WRITER_APPLY_DOCUMENT_HTML_RULES`, tool schema)

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
- postpone exotic TeX / package coverage beyond `latex2mathml` (Phase 3 / ongoing)

## Open product decisions

| # | Question | Resolution (as of 2026-04) |
|---|-----------|----------------------------|
| 1 | Visible fallback for unsupported formulas: plain text, marker, or both? | **Marker + snippet** — `[Math import failed]` plus truncated source in document; not silent. |
| 2 | Ship MathML-only before TeX? | **Yes** historically; **Phase 2 TeX path is now shipped** (MathML still preferred in prompts). |
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

**Completed (Phase 2 — core):**

10. [x] TeX delimiter detection + mixed segmentation (`html_math_segment.py`)
11. [x] `convert_latex_to_starmath` + `format_support` wiring + `content_has_markup` TeX patterns
12. [x] Unit + UNO tests for TeX and mixed MathML/TeX
13. [x] Model/tool copy (`constants.py`, `content.py`)

**Remaining (pick order):**

- [ ] Phase 1 backlog: full test matrix, optional `warnings` in tool return, reviewer examples
- [ ] Optional: `"".join` for list `content` in `apply_document_content`
- [ ] Phase 2 stretch: KaTeX `<annotation encoding="application/x-tex">` retry on failed MathML import
- [ ] Phase 3: robustness
- [ ] Trim DEBUG logging when stable

## Reviewer checklist

- is the supported scope narrow and realistic for MVP? — **Yes** (MathML in HTML; TeX is a bounded subset via `latex2mathml`, not full LaTeX).
- is the hardest technical unknown resolved before broad coding starts? — **Yes** (spike folded into MVP).
- does the plan preserve the existing HTML import path for non-math content? — **Yes**.
- are failure modes visible and non-destructive? — **Yes** (string fallback; structured warnings optional).
- are both unit tests and UNO tests included? — **Core yes**; full matrix partial — see §5 Testing.
- are inline and display behaviors explicitly covered? — **Yes** (code + UNO).

## Implementation handoff summary

Work **after** Phase 2 (core) should assume:

- **Done:** MathML-in-HTML → editable Writer formulas via preprocess + LO conversion + `newline` collapse.
- **Done (core):** TeX delimiters in the same HTML strings → `latex2mathml` → MathML → same LO path + insertion.
- **Primary backlog:** Phase 3 quality; Phase 1 “Not done yet” polish; optional Phase 2 stretch (annotation retry).
- **Architecture rule:** keep preprocessing in front of the StarWriter HTML filter; do not teach the HTML filter math.
- **Testing rule:** extend matrix rows and tool JSON when adding features.
- **Fallback rule:** never silently lose formulas (unchanged).

## Related documents

- Research and architecture proposal: `docs/libreoffice-html-math-proposal.md`
- Writer HTML import + math orchestration: `plugin/modules/writer/format_support.py`
- MathML + TeX segmentation: `plugin/modules/writer/html_math_segment.py`
- MathML → StarMath + Writer newline mitigation; LaTeX (`latex2mathml`) → MathML → StarMath: `plugin/modules/writer/math_mml_convert.py`
- Formula OLE insert: `plugin/modules/writer/math_formula_insert.py`
- Vendored `latex2mathml`: `requirements-vendor.txt` (and dev mirror in `pyproject.toml` for typecheck)
