# PM & Development Plan: Semantic Style Models in HTML

**Status:** v1 **implemented** (read/write `data-lo-style` in [`format.py`](../plugin/writer/format.py) + [`xhtml_style_postprocess.py`](../plugin/writer/xhtml_style_postprocess.py)). Post-v1 performance path documented below.

## Problem Statement

Currently, when the agent reads a Writer document via `get_document_content`, the extension flattens LibreOffice paragraph and character styles into computed inline CSS (e.g., `<p style="font-size: 10pt; font-style: italic">`).

This creates a significant gap in the agent's understanding:
1. **Loss of Semantic Context:** The agent cannot tell that a paragraph uses the `Caption` or `Heading 1` named style. It only sees the final visual properties.
2. **Indistinguishable Overrides:** The agent cannot differentiate between properties inherited from a named style and manual "direct overrides" applied by the user (e.g., highlighting a single word).
3. **Write-Path Degradation:** Because the agent only sees inline styles, it generates inline styles when writing back to the document via `apply_document_content`. This bypasses LibreOffice's style system and pollutes the document with hardcoded formatting.

While the original proposal to add a `get_paragraph_metadata` tool provides accurate data, it introduces overhead by forcing the agent to make secondary tool calls to inspect the document structure piecemeal.

## Proposed Solution (Product Management)

Instead of a separate diagnostic tool, we will **embed the LibreOffice style model directly into the HTML representation**.

We will achieve **Read/Write Symmetry**:
- **Read:** Paragraphs will include their named style as a custom data attribute (e.g., `<p data-lo-style="Caption">`). Values are **compact tokens with no spaces** (`Heading1`, `Textbody`, `Standard`) so models do not have to juggle LibreOffice spacing quirks. Inline `style="..."` attributes will be reserved *exclusively* for direct formatting overrides.
- **Write:** When the agent generates HTML, it uses the same compact tokens in `data-lo-style`. The extension resolves them back to real UNO `ParaStyleName` values (e.g. `Heading1` → `Heading 1`) before `setPropertyValue`, then applies any inline CSS as direct overrides on top.

**Benefits:**
- **Zero Tool-Call Overhead:** The agent gets the full semantic structure in a single read pass.
- **Native LLM Paradigm:** LLMs excel at understanding HTML where classes/attributes define the theme and inline styles define exceptions.
- **Cleaner Documents:** The agent naturally learns to apply named styles rather than raw formatting.

### End-to-end example (target shape)

**Before (today — StarWriter export):**
```html
<p style="font-size: 14pt; font-weight: bold">This is a <span style="font-weight: bold">bold</span> word.</p>
```

**After read path (XHTML + string post-process):**
```html
<p data-lo-style="Heading1">This is a <span style="font-weight: bold">bold</span> word.</p>
```

**Agent writes the same compact tokens back; write path resolves to UNO `ParaStyleName` + direct Char* overrides.**

---

## Architecture: short-term vs long-term

Two phases solve different problems. **v1** fixes read→write→read idempotency. **Post-v1** fixes cost on large documents.

### Short-term (v1 — implement now)

**Goal:** Correct `data-lo-style` tokens after StarWriter write, without changing the write filter.

| Layer | Choice |
|-------|--------|
| **Read body + char overrides** | `XHTML Writer File` export → [`xhtml_to_semantic_html()`](../plugin/writer/xhtml_style_postprocess.py) in [`xhtml_style_postprocess.py`](../plugin/writer/xhtml_style_postprocess.py) |
| **Read autostyle names when XHTML omits them** | Second export: **OpenDocument Text Flat XML** (`.fodt`); [`extract_autostyle_parents_from_fodt()`](../plugin/writer/xhtml_style_postprocess.py) builds a **`Pn → parent-style-name`** map; joined to XHTML by matching the `paragraph-Pn` class suffix (order-independent — no block-index alignment) |
| **Write** | Keep `HTML (StarWriter)` import + strip `data-lo-style` + UNO `apply_paragraph_style_preserving_direct_char` (full document only) |

**Why flat ODF:** Probe B showed UNO still has `ParaStyleName = Caption` after write, but XHTML re-export emits autostyle `paragraph-P1` with no `.paragraph-Caption` rule — string-only XHTML post-process cannot recover the token. Flat ODF keeps `style:parent-style-name` on automatic styles; the autostyle name `Pn` is identical in both exports. Probe C ruled out symmetric **XHTML Writer File** import on write (0 body paragraphs inserted on test LO build).

**Entry points (v1):** [`document_to_content()`](../plugin/writer/format.py) calls `_export_xhtml` + `_autostyle_parents`, then `xhtml_to_semantic_html(xhtml, parents)`. Write path: `_extract_block_lo_styles` → StarWriter import → `_apply_block_lo_styles`.

**v1 cost:** Two full `storeToURL` exports on every `get_document_content(scope=full)` (XHTML + FODT). Acceptable for correctness; **not** the long-doc architecture.

**Explicitly out of scope for v1:**
- Custom full Flat ODF → HTML parser (would duplicate xmloff + odf2xhtml — ~7.5k lines XSL + ~158k lines xmloff; see feasibility notes in maintainer research)
- Symmetric XHTML import on write
- Per-paragraph UNO walk on every full read (see long-term exception below)

### Long-term (post-v1 — large documents)

**Goal:** Paragraph style metadata without serializing the whole document twice.

LibreOffice has **no filter** for “styles only.” Any `storeToURL` path (XHTML, FODT, StarWriter) runs full xmloff serialization. For book-length docs, dual export is wasteful when the agent only needs styles for the slice it is editing.

**Recommended shape:**

1. **Cached UNO paragraph style index** — one `text.createEnumeration()` pass per document revision: top-level block index → compact token via `ParaStyleName` + `compact_lo_style_name()`. Cache invalidated on `document:cache_invalidated` (same pattern as [`TreeService`](../plugin/writer/tree.py)). Cost: O(paragraphs) RPC, **no filter pipeline**.
2. **Scoped HTML export** — agent uses `get_document_content(scope=range)` (existing [`_range_to_content_via_temp_doc`](../plugin/writer/format.py) already copies `ParaStyleName` into temp docs). Merge styles from the index for that para range only.
3. **Navigation first** — `get_document_tree`, `search_in_document`, `get_heading_children` before full reads ([`docs/lo-dom-semantic-tree.md`](lo-dom-semantic-tree.md), [`docs/multi-document-dev-plan.md`](multi-document-dev-plan.md)).

**Why UNO index is better than FODT sidecar for scale:** Probe B proved UNO `ParaStyleName` stays correct when XHTML export lies. The sidecar fixes the same gap via export strings (v1 purity bet); UNO is lighter and more authoritative for **paragraph names only**.

**What still needs export (no cheap shortcut):**
- Inline char overrides (`<span style="...">`) — XHTML export or heavy UNO text-portion walk
- Tables, lists, math, frames — agent HTML structure from filters today

**Optional later:** dedicated `get_paragraph_styles` tool; add `ParaStyleName` to heading nodes in the tree’s single enumeration pass.

**Migration:** Once a cached UNO paragraph-style index is proven on range reads, drop the second full FODT export on `scope=full` (or gate FODT to fallback-only when the index and XHTML disagree).

---

## Rejected Alternative: Hybrid Model-Walk (v1 full read)

A contributor explored reading `ParaStyleName` from the UNO model in lockstep with XHTML body blocks (one `getPropertyValue` per paragraph) to recover real style names when the export emits autostyle classes like `paragraph-P1` instead of `paragraph-Standard`.

**We reject per-paragraph UNO walks paired with every full `get_document_content` in v1** because:
- v1 bet is export-string normalization (XHTML + sidecar), not mixing UNO RPC with HTML block alignment on every read.
- Lockstep model-walk + XHTML body blocks reintroduces ordering fragility (tables, frames, lists).

**Long-term exception:** a **cached** UNO style index (one enumeration per doc revision, not per tool call) is the planned replacement for the FODT sidecar on large documents — see [Architecture: short-term vs long-term](#architecture-short-term-vs-long-term). That is not “walk the model for every paragraph on every read”; it is amortized metadata.

**v1 read path:** XHTML export + `xhtml_to_semantic_html` + optional FODT `Pn → parent` map for autostyle name recovery. Write path maps compact tokens back to UNO via the document style list.

---

## LO Export Naming Quirks

The same paragraph style appears under different names depending on where you look:

| Source | Example | Notes |
|--------|---------|-------|
| UNO `ParaStyleName` / `setPropertyValue` | `Heading 1` | Real style name in LibreOffice |
| Agent `data-lo-style` | `Heading1` | Compact token — **no spaces** |
| XHTML class suffix | `Heading_20_1` | ODF URL encoding (`_20_` → space before compacting) |
| XHTML autostyle class | `P1` | Synthetic automatic style; **not** a style name |

For round-trip, `data-lo-style` carries the **compact token** (`Heading1`), not the CSS class suffix and not the spaced UNO name. The extension translates compact ↔ UNO at the boundary.

---

## Phase 0: XHTML export shapes (background)

Probe fixtures: [`tests/writer/test_xhtml_style_postprocess.py`](../tests/writer/test_xhtml_style_postprocess.py) (`REFERENCE_XHTML`). UNO coverage: [`tests/writer/test_content_style_model_uno.py`](../tests/writer/test_content_style_model_uno.py).

See **Phase 0: Filter probe** at the bottom of this doc for the active probe workflow and decision gate.

### Observed behavior

| Case | Typical XHTML body class | `<style>` block | Post-process outcome |
|------|--------------------------|-----------------|----------------------|
| Default body (`Standard`) | `paragraph-Standard` **or** `paragraph-P1` (LO/version dependent) | Named and/or autostyle rules | Decode → compact → `data-lo-style="Standard"`, or fingerprint/omit for `P1` |
| `Heading 1` | `paragraph-Heading_20_1` | Named rule present | Decode → compact → `data-lo-style="Heading1"` |
| `Text body`, `Caption` | `paragraph-Text_20_body`, `paragraph-Caption`, etc. | Named rule present | Decode suffix → compact → `data-lo-style` (e.g. `Textbody`, `Caption`) |
| Char override (e.g. bold word) | `text-T1` on `<span>` | `.text-T1 { font-weight: bold; }` | Inline `style="font-weight: bold"` on span |
| Mixed doc | Mix of above per paragraph | Both `paragraph-*` and `text-*` rules | Per-paragraph rules below |
| Trailing empty paragraph | Extra `<p class="paragraph-Standard">&nbsp;</p>` | — | Filter empty/`&nbsp;`-only `<p>` from agent-facing output (optional but recommended) |

**Important:** Contributor probe saw `paragraph-P1` for `Standard`; WriterAgent dev LO export saw `paragraph-Standard` directly. **Implementation must handle both** — do not assume one LO behavior.

**Autostyle name recovery via flat ODF (implemented).** After an edit, the StarWriter HTML import bakes extra direct char props into the paragraph, so on re-export it is an autostyle (`paragraph-Pn`) whose CSS matches no named rule — the XHTML fingerprint then can't recover the name (the write→read round-trip would drop the token). Fix (two filters): export the document ALSO as flat ODF (`OpenDocument Text Flat XML`), which keeps `<style:style style:name="Pn" style:parent-style-name="...">`; the automatic style name `Pn` is identical to the XHTML `paragraph-Pn` class suffix, so a string-only `Pn → parent` map recovers the real style name (no model walk, no order alignment). The CSS fingerprint stays as a fallback when no FODT map is available.

### Autostyle decision table (locked from probe)

| Condition | String-only rule |
|-----------|------------------|
| Class suffix is **not** `P` + digits (e.g. `Standard`, `Heading_20_1`) | `compact_lo_style_name(decode_lo_css_class_suffix(suffix))` → `data-lo-style` |
| Class is `paragraph-P*` and CSS matches **exactly one** named `paragraph-*` rule in the same export | Map to that rule's compact name |
| Class is `paragraph-P*` and no unique CSS match | **Omit** `data-lo-style`; write path treats missing attribute as default body style |
| Multiple named rules share the same CSS as the autostyle | Omit + debug log |

---

## Phase 1: Update the Read Path (`get_document_content`)

*Target files:* [`plugin/writer/format.py`](../plugin/writer/format.py) (imported elsewhere as `format_support`), new [`plugin/writer/xhtml_style_postprocess.py`](../plugin/writer/xhtml_style_postprocess.py) (pure string/CSS — no UNO).

**Pipeline (order matters):**

1. **Export (read path only):** Switch `document_to_content()` / `_range_to_content_via_temp_doc()` from `HTML (StarWriter)` to `XHTML Writer File`. Keep `HTML (StarWriter)` for **import** until Phase 2 applies styles via UNO after insert.

2. **Parse `<style>` block:** Build `class → declaration` map for `.text-*` and `.paragraph-*`. Normalize whitespace for fingerprint comparison.

3. **Inline char autostyles:** Replace `class="text-T1"` (and other `text-*`) with `style="..."` from the map. Remove empty `class` on spans.

4. **Decode named paragraph classes → compact token:** Reverse ODF encoding, then remove all spaces for the agent-facing attribute:

   ```python
   def decode_lo_css_class_suffix(suffix: str) -> str:
       return re.sub(
           r"_([0-9a-fA-F]{2})_",
           lambda m: chr(int(m.group(1), 16)),
           suffix,
       )

   def compact_lo_style_name(uno_name: str) -> str:
       """Agent-facing token: no spaces (Heading 1 → Heading1)."""
       return uno_name.replace(" ", "")
   ```

   | XHTML class suffix | UNO name (internal) | `data-lo-style` |
   |--------------------|---------------------|-----------------|
   | `Standard` | `Standard` | `Standard` |
   | `Heading_20_1` | `Heading 1` | `Heading1` |
   | `Text_20_body` | `Text body` | `Textbody` |
   | `Caption` | `Caption` | `Caption` |
   | `P1` | — | See [Autostyle decision table](#autostyle-decision-table-locked-from-probe) |

5. **Transform paragraph tags:** For each paragraph block (`<p>`, `<h1>`–`<h6>`, `<li>`, `<blockquote>`, `<pre>`) with `class="paragraph-…"`: emit `data-lo-style="…"`, strip `paragraph-*` from `class`, drop empty `class`. NOTE: `<div>` is treated as a **transparent container** (not a styleable block) on both read and write — LibreOffice does not put a paragraph style on a `<div>`, and on write a `<div>` is not its own paragraph, so counting it as a style slot desyncs the positional apply.

6. **Strip boilerplate:** Existing `_strip_html_boilerplate()` returns `<body>` inner HTML only.

7. **Optional:** Drop empty trailing paragraphs (`&nbsp;` / whitespace-only) so the agent does not see ghost blocks.

### Phase 1b: Flat ODF autostyle parent map (v1 idempotency fix — implemented)

After probe C failed, v1 adds a second export on every read (full document and range-via-temp-doc):

1. **Second export:** Same document via filter `OpenDocument Text Flat XML` ([`_autostyle_parents()`](../plugin/writer/format.py)).
2. **Parse map:** [`extract_autostyle_parents_from_fodt()`](../plugin/writer/xhtml_style_postprocess.py) scans `style:style` elements with `style:family="paragraph"`; for automatic names matching `P\d+`, record `Pn → style:parent-style-name` (ODF-encoded parent names decoded at use).
3. **Join to XHTML:** In `_paragraph_token()`, when the block class is `paragraph-Pn`, look up `Pn` in the map and emit `compact_lo_style_name(decode(parent))`. **No block-index walk** — the join is by autostyle name only, so tables and nested structure cannot desync it.
4. **Fallback:** If FODT export fails, map is `{}` and CSS fingerprint matching applies (same as pre-1b). If still ambiguous, omit `data-lo-style`.

FODT fixes probe B (write→read style **name** recovery). It does **not** replace XHTML for body markup or char overrides, and does **not** recover whole-paragraph direct overrides (see [v1 limitations](#v1-limitations-shipped)).

---

## Phase 2: Update the Write Path (`apply_document_content`)

*Target file:* [`plugin/writer/format.py`](../plugin/writer/format.py)

StarWriter HTML import does not understand `data-lo-style`. Write path:

1. **Collect:** Scan block elements for `data-lo-style="…"` **in document order**; build a list of compact tokens (one entry per block tag, `None` when attribute absent).

2. **Strip:** Remove `data-lo-style` attributes from HTML before `insertDocumentFromURL` (StarWriter filter unchanged).

3. **Import:** Existing HTML import path (`_insert_mixed_or_plain_html`, etc.).

4. **Resolve & apply:** For each compact token, resolve to UNO `ParaStyleName` via document paragraph style list (exact match first, then match where `compact_lo_style_name(style_name) == token`). Call `apply_paragraph_style_preserving_direct_char(doc, cursor, uno_name)`. Reuse this helper — it already preserves direct Char* overrides when changing `ParaStyleName`.

5. **Overrides:** Inline `style="..."` on spans continues to map to direct character formatting (StarWriter import + preserve helper).

6. **Fallback:** Unknown style name → `Standard` (or skip apply and log).

**Hook points:** Named-style application runs on `replace_full_document` only. Targeted inserts/replaces (`insert_content_at_position`, `replace_single_range_with_content`) still insert the content but **skip** style application: the first imported block merges into the cursor's existing paragraph, so applying its `data-lo-style` would restyle the adjacent (pre-existing) text. For styling existing text use `apply_style`. (Applying styles only to genuinely-new paragraphs on partial edits is a **post-v1** follow-up.)

---

## v1 limitations (shipped)

These are intentional trade-offs in v1. Tests document the behavior ([`test_xhtml_style_postprocess.py`](../tests/writer/test_xhtml_style_postprocess.py), [`test_content_style_model_uno.py`](../tests/writer/test_content_style_model_uno.py)).

| Limitation | v1 behavior | Workaround for agents/users | Post-v1 direction |
|------------|-------------|----------------------------|-------------------|
| **Whole-paragraph direct overrides** (center, para colour, margins baked into autostyle CSS) | Read omits token and drops override CSS; FODT recovers the **base style name** only | Prefer named styles; use inline `style` on **spans** for char exceptions | UNO index + optional Para* snapshot; or dedicated `get_paragraph_metadata` for debugging |
| **Table cell paragraph styles** | `paragraph-*` stripped inside `<table>`; no `data-lo-style` on cell blocks | `apply_style` on cell text; don't rely on agent HTML for table styling | Table-aware style index or cell-level tokens |
| **Partial edits** (`end` / `search` / `selection` / `beginning`) | Content inserted; `data-lo-style` **not** applied (would restyle merged adjacent text) | `target='full_document'` for styled rewrites; `apply_style` to restyle existing text | Apply only to genuinely new paragraphs after import |
| **Dual export cost** | Every full read (and range read via temp doc) runs XHTML + FODT `storeToURL` | `scope=range`, `get_document_tree`, `search_in_document` before full reads | Cached UNO paragraph-style index; drop FODT on `scope=full` |
| **Token collision** (two UNO names compact to same token, e.g. `Heading 1` + literal `Heading1`) | Read omits token; write falls back to `Standard` | Use tokens exactly as returned; avoid duplicate style names | Per-document disambiguation or spaced-token policy |
| **Unresolvable autostyle** (no FODT map, ambiguous fingerprint) | No `data-lo-style`; write treats as default body | Accept as Standard on rewrite | UNO `ParaStyleName` index as authoritative fallback |
| **XHTML export failure** | Falls back to legacy StarWriter HTML (inline CSS, no tokens) | — | Log/monitor; same as pre-v1 read |
| **Symmetric XHTML write** | Not supported (probe C: 0 body paragraphs on test LO) | Always StarWriter import + UNO style apply | Unlikely to revisit |

---

## Phase 3: Testing & Documentation

1. **Unit tests (pytest):** [`tests/writer/test_xhtml_style_postprocess.py`](../tests/writer/test_xhtml_style_postprocess.py) — decode/compact, CSS map, char inline, autostyle fingerprint, FODT parent recovery, collision, whole-para override limitation.
2. **UNO tests:** [`tests/writer/test_content_style_model_uno.py`](../tests/writer/test_content_style_model_uno.py) — read tokens, write resolution, round-trip, FODT write→read, partial-edit non-corruption, math+style.
3. **Prompts:** `WRITER_APPLY_DOCUMENT_HTML_RULES` in [`plugin/framework/constants.py`](../plugin/framework/constants.py) — agent reads/writes compact `data-lo-style` tokens (no spaces: `Heading1`, `Textbody`); inline `style` for overrides only.
4. **Docs:** [`docs/llm-styles.md`](llm-styles.md) — `data-lo-style` is the agent-facing convention; legacy `class="Style Name"` via StarWriter remains for non-agent HTML.
5. **`get_paragraph_metadata`:** Keep as optional `specialized` tier only if needed for debugging; not required for core read/write.

---

## Phase 4: Long-term — UNO style index (post-v1)

*Target files:* new helper (e.g. `plugin/writer/paragraph_style_index.py`), [`plugin/writer/format.py`](../plugin/writer/format.py), optional [`plugin/writer/tree.py`](../plugin/writer/tree.py).

1. **Build index:** Single enumeration of top-level text content; record `ParaStyleName` per block index; compact tokens; cache per doc key until invalidation.
2. **Merge on read:** `xhtml_to_semantic_html` (or caller) fills `data-lo-style` from index when XHTML omits; use index alone for range reads instead of second full FODT export.
3. **Drop dual full export:** Remove or gate FODT parent map to fallback-only once index + round-trip UNO tests pass on probe B.
4. **Agent workflow:** Document preferring `scope=range` + tree/search on large docs; optional `get_paragraph_styles` if split metadata tool is needed.

---

## Implementation Details

### Compact style tokens (agent ↔ UNO)

- **Read:** `data-lo-style="Heading1"` — always space-free.
- **Write:** resolve compact token against `ParagraphStyles` in the target document; apply the matched UNO name.
- **Collision rule:** if two paragraph styles compact to the same token (rare), **read** omits `data-lo-style` for both; **write** resolves only when exactly one UNO name matches (exact name or unique compact match), else falls back to `Standard`. Prompt: use tokens exactly as returned by `get_document_content`.

### Localization

On localized LibreOffice, UNO names may be translated (`Überschrift 1`). Compact tokens drop spaces from whatever UNO returns on read; write resolves against the same document's style list. Round-trip stays consistent within one LO instance.

### Unsupported styles

If `apply_document_content` cannot resolve a compact `data-lo-style` token to a document style, fall back to `Standard`.

### Performance

**v1:** Two `storeToURL` exports on full read (XHTML + Flat ODF sidecar) + in-memory string/CSS/XML parsing; UNO `setPropertyValue` / `apply_paragraph_style_preserving_direct_char` on the write path.

**Long-term:** One XHTML export per read **slice** + cached UNO style index (one enumeration per doc revision). Prefer `scope=range` and navigation tools over `scope=full` on large documents. Chat sidebar already uses plain-text excerpts ([`get_document_context_for_chat`](../plugin/doc/document_helpers.py)), not styled HTML export.

| Source | Paragraph styles | Char overrides | Body HTML | Scales with doc size |
|--------|------------------|----------------|-------------|----------------------|
| UNO `ParaStyleName` index | Yes (authoritative) | No | No | O(paragraphs) once per revision |
| Flat ODF parent map (v1) | Yes (`Pn → parent` for autostyles) | No | No | Full serialization |
| XHTML Writer File | Sometimes (autostyle gap) | Yes | Yes | Full serialization |

---

## Design Approval Checklist

v1 checklist (done):

- [x] Read path: XHTML export + `xhtml_to_semantic_html` + FODT `Pn → parent` map (Phase 1b)
- [x] Autostyle: decode named classes + CSS fingerprint + FODT fill + omit when still unresolvable
- [x] Write path: strip `data-lo-style`, StarWriter import, apply styles via `apply_paragraph_style_preserving_direct_char` (`full_document` only)
- [x] Agent prompt documents compact `data-lo-style` tokens (no spaces) vs inline `style`
- [x] Write path resolves compact tokens → UNO `ParaStyleName` via style list lookup (ambiguous → `Standard`)
- [x] Unit + UNO round-trip tests (probe B: `Caption` recovers after write→read via FODT)
- [x] Write path stays on StarWriter import (probe C failed for XHTML import)

---

## Phase 0: Filter probe (before idempotency fix)

Historical decision gate (pre-v1). Shipped pipeline: [`xhtml_to_semantic_html()`](../plugin/writer/xhtml_style_postprocess.py), [`extract_autostyle_parents_from_fodt()`](../plugin/writer/xhtml_style_postprocess.py), [`compact_lo_style_name()`](../plugin/writer/xhtml_style_postprocess.py).

**Tests:**

| File | Role |
|------|------|
| [`tests/writer/test_xhtml_style_postprocess.py`](../tests/writer/test_xhtml_style_postprocess.py) | pytest fixtures (no LibreOffice) |
| [`tests/writer/test_content_style_model_uno.py`](../tests/writer/test_content_style_model_uno.py) | UNO round-trip, FODT recovery, partial-edit safety |

**Run:** `pytest tests/writer/test_xhtml_style_postprocess.py` and `make test` (UNO tests need `soffice`).

### Probe scenarios

| ID | Setup | Assert |
|----|--------|--------|
| A | Native `Caption` paragraph → XHTML export | `data_lo_style="Caption"`, resolution `named_class` |
| B | Write via **StarWriter** import + UNO apply → XHTML re-read | UNO still `Caption`; analysis shows token loss / autostyle (documents the wall) |
| C | Write via **XHTML Writer File** import + UNO apply → XHTML re-read | **Decision probe:** token still `Caption`? |
| D | Native `Heading 1` + char bold → XHTML | text-* rules present; postprocess inlines char override |

### Decision gate (resolved)

Probe C **failed** (XHTML import did not insert body paragraphs on maintainer LO build). **Chosen v1 fix:** Flat ODF `Pn → parent` map on read (Phase 1b). **Write path unchanged:** StarWriter + UNO style apply.

Do not implement symmetric XHTML write and sidecar as parallel full solutions in v1.

### Probe results

| Probe | Outcome | Notes |
|-------|---------|-------|
| A — native `Caption` → XHTML read | Pass | `named_class`, token `Caption`, class `paragraph-Caption` |
| B — StarWriter write → XHTML re-read | Fixed in v1 | UNO still `Caption`; export `paragraph-P1`; FODT parent map recovers `Caption` token on re-read |
| C — XHTML import write → re-read | Fail | 0 body paragraphs inserted; symmetric XHTML write rejected |
| D — `Heading 1` + bold | Pass | `Heading1` token; char `text-T*` inlined to span `style` |

Run locally: `pytest tests/writer/test_xhtml_style_postprocess.py` and UNO tests in [`tests/writer/test_content_style_model_uno.py`](../tests/writer/test_content_style_model_uno.py) via `make test`.
