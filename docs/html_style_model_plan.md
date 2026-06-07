# PM & Development Plan: Semantic Style Models in HTML

**Status:** Design for review — implementation deferred until approved.

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

## Rejected Alternative: Hybrid Model-Walk

A contributor explored reading `ParaStyleName` from the UNO model in lockstep with XHTML body blocks (one `getPropertyValue` per paragraph) to recover real style names when the export emits autostyle classes like `paragraph-P1` instead of `paragraph-Standard`.

**We reject this approach** for the read path:
- It reintroduces N UNO RPC calls and ordering fragility (tables, frames, lists) on every `get_document_content`.
- It duplicates work that `_range_to_content_via_temp_doc` already does when building temp docs for selection/range export.
- It conflicts with the core bet: one fast `XHTML Writer File` export + string post-process.

**String massaging is the read path:** decode ODF-encoded class suffixes for named styles, strip spaces for agent-facing `data-lo-style`, resolve autostyle `P*` classes via the paired flat-ODF parent map (`style:parent-style-name`, see the autostyle-name-recovery note above) with the XHTML `<style>` block CSS fingerprint as fallback; omit `data-lo-style` only when still unresolvable. Write path maps compact tokens back to UNO via the document style list.

We trust LibreOffice's XHTML export and UNO style APIs — no parallel model-walk, no alternate read path.

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

## Phase 0: XHTML Export Probe (completed)

Probe harness: [`tests/writer/test_xhtml_export_uno.py`](../tests/writer/test_xhtml_export_uno.py) (UNO tests; run via `make test` when `soffice` is available).

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

**Hook points:** Named-style application runs on `replace_full_document` only. Targeted inserts/replaces (`insert_content_at_position`, `replace_single_range_with_content`) still insert the content but **skip** style application: the first imported block merges into the cursor's existing paragraph, so applying its `data-lo-style` would restyle the adjacent (pre-existing) text. For styling existing text use `apply_style`. (Applying styles only to genuinely-new paragraphs on partial edits is a follow-up — see the checklist.)

---

## Phase 3: Testing & Documentation

1. **Unit tests (pytest):** `decode_lo_css_class_suffix`, CSS map extraction, char inline transform, paragraph transform, autostyle fingerprint — no LibreOffice.
2. **UNO tests:** [`tests/writer/test_xhtml_export_uno.py`](../tests/writer/test_xhtml_export_uno.py) documents export shapes; add round-trip test: read emits `data-lo-style="Heading1"`, agent writes it back, verify UNO `ParaStyleName == "Heading 1"` and bold override.
3. **Prompts:** `WRITER_APPLY_DOCUMENT_HTML_RULES` in [`plugin/framework/constants.py`](../plugin/framework/constants.py) — agent reads/writes compact `data-lo-style` tokens (no spaces: `Heading1`, `Textbody`); inline `style` for overrides only.
4. **Docs:** [`docs/llm-styles.md`](llm-styles.md) — `data-lo-style` is the agent-facing convention; legacy `class="Style Name"` via StarWriter remains for non-agent HTML.
5. **`get_paragraph_metadata`:** Keep as optional `specialized` tier only if needed for debugging; not required for core read/write.

---

## Implementation Details

### Compact style tokens (agent ↔ UNO)

- **Read:** `data-lo-style="Heading1"` — always space-free.
- **Write:** resolve compact token against `ParagraphStyles` in the target document; apply the matched UNO name.
- **Collision rule:** if two styles compact to the same token (rare), prefer exact UNO name match when the agent passes the spaced form anyway; otherwise log and fall back to `Standard`. Document in prompt: use tokens exactly as returned by `get_document_content`.

### Localization

On localized LibreOffice, UNO names may be translated (`Überschrift 1`). Compact tokens drop spaces from whatever UNO returns on read; write resolves against the same document's style list. Round-trip stays consistent within one LO instance.

### Unsupported styles

If `apply_document_content` cannot resolve a compact `data-lo-style` token to a document style, fall back to `Standard`.

### Performance

One `storeToURL` export + in-memory string/CSS parsing on the read path; UNO `setPropertyValue` / `apply_paragraph_style_preserving_direct_char` on the write path.

---

## Design Approval Checklist

Status (implemented in this PR):

- [x] Read path: XHTML export (+ paired flat-ODF parent map) + string pipeline only
- [x] Autostyle: flat-ODF `parent-style-name` recovery (primary) + CSS fingerprint (fallback) + omit/collision-safe (no `P1` passed to the LLM as a style name)
- [x] Write path: strip `data-lo-style`, import HTML, apply styles via `apply_paragraph_style_preserving_direct_char` (applied on `full_document`; targeted inserts/replaces skip apply to avoid restyling adjacent text — follow-up)
- [x] Agent prompt documents compact `data-lo-style` tokens (no spaces) vs inline `style`
- [x] Write path resolves compact tokens → UNO `ParaStyleName` via style list lookup (collision → fail-safe `Standard`)
- [x] Unit + UNO round-trip tests included (incl. the write→read FODT recovery)
