# Section Replace and get_markdown(scope="range") — Options

This document summarizes the failure when the model uses **find_text + get_markdown(scope="range")** for section replacement, and lists several possible approaches to fix or work around it. No single approach is implemented here; pick one (or combine) when you’re ready.

---

## Observed in logs (agent run)

In a run captured in `~/localwriter_agent_debug.log`, the model **never** called get_markdown(scope="range"). Flow: find_text("## Summary") → (112, 119); get_markdown(scope="full"); then apply_markdown(target="search", search="## Summary\n\nA legendary...", markdown="## Yhteenveto\n\n...") → **0 replacements**. After retrying find_text and find_text("## Skills", start=112) → (343, 349), the run hit MAX_TOOL_ROUNDS (5) and stopped. So the **immediate** failure was the search path (0 replacements), not the range path. If the model had used get_markdown(scope="range", start=112, end=343) and then apply_markdown(target="range", ...), the coordinate mismatch below would still apply and could produce wrong range content. Steering the model to **prefer** the range path for section replace (find_text → get_markdown(scope="range") → apply_markdown(target="range")) in the system prompt would avoid depending on search and would make fixing the range coordinates worthwhile.

---

## Code note (current implementation)

Range/selection export no longer uses `_document_to_markdown_structural`; it uses **`_range_to_markdown_via_temp_doc`** (copy paragraphs into a temp Writer doc, then storeToURL with Markdown filter). That function still uses **summed paragraph lengths** (enumeration + `current_offset += len(para_text)`) for para_start/para_end, so the same coordinate mismatch applies: Option A (cursor-based paragraph offsets) would be implemented inside `_range_to_markdown_via_temp_doc`; Option F (compareRegionStarts/Ends) would use a `filter_range` from `get_text_cursor_at_range(start, end)` and filter enumerated paragraphs by UNO region comparison against that range.

---

## What’s going wrong

- **User:** e.g. “Translate my Summary Section (and word Summary) to Finnish.”
- **Model:** Calls find_text("## Summary") → `start=112, end=119`; find_text("## Skills") → `start=343, end=349`. Then get_markdown(scope="range", start=112, end=340) to read the section.
- **Result of get_markdown:** The returned markdown is wrong: it shows `"## ary\nA legendary..."` instead of `"## Summary\nA legendary..."`. So “Summary” is corrupted to “ary” and the range content is misaligned.
- **Consequence:** The model never gets correct section text, may retry or run out of tool rounds (e.g. MAX_TOOL_ROUNDS=5), and never calls apply_markdown.

So the core bug is: **character offsets from find_text (cursor-based) don’t match the offsets used inside get_markdown when scope="range"** (which uses a paragraph-enumeration and summed paragraph lengths). The same numeric range is interpreted in two different “coordinate systems,” so the structural exporter trims the wrong slice and produces corrupted output.

---

## Option A: Cursor-based paragraph offsets (align coordinates)

**Idea:** When scope is `"selection"` or `"range"`, compute each paragraph’s start/end using the **same** cursor-based measurement as find_text and get_document_length, instead of summing paragraph lengths.

**How:**

- In the range/selection exporter (currently `_range_to_markdown_via_temp_doc` in markdown_support.py), for each enumerated paragraph (or element that has getStart()/getEnd()):
  - Measure “offset from document start to this paragraph’s start” with a cursor: e.g. cursor at doc start, then gotoRange(para.getStart(), True), then len(cursor.getString()) → `para_start`.
  - Similarly measure offset to para.getEnd() → `para_end`.
- Use these `para_start` / `para_end` only for the range/selection filter and trim logic (skip when para_end <= selection_start or para_start >= selection_end; trim with selection_start/selection_end). Keep the rest of the structural export (prefixes, lines) as-is.
- If an element doesn’t support getStart()/getEnd(), or measurement fails, fall back to the current cumulative-offset behavior so full-doc and other cases don’t break.

**Pros:** Fixes the bug at the source; scope="range" and find_text then share one coordinate system; section markdown (headings, lists) stays correct.  
**Cons:** Slightly more code; need to handle enumeration elements that aren’t simple paragraphs (tables, frames) so we don’t assume every element has getStart()/getEnd().

---

## Option B: Range extraction via cursor only (plain text for range)

**Idea:** For scope="range", don’t use the structural paragraph walk at all. Use the same cursor API as get_text_cursor_at_range to get the exact character span [start, end), and return that string.

**How:**

- In document_to_markdown (or a dedicated path for scope="range"), when scope is "range":
  - Call get_text_cursor_at_range(model, range_start, range_end), then cursor.getString() (or the equivalent that returns the selected text).
  - Return that string as the “markdown” for the range. Optionally document that scope="range" returns **plain text** (no heading/list structure), or run a minimal “plain to markdown” heuristic (e.g. first line as ## if short) if you want a bit of structure.
- No change to the structural walk; range becomes a thin wrapper around the cursor slice.

**Pros:** Simple; guaranteed same coordinate system as find_text; no offset bugs.  
**Cons:** Range output has no real markdown structure (no ## from Writer styles); the model would get “Summary\nA legendary...” instead of “## Summary\n\nA legendary...”. For “replace this section with translated markdown” that might still be enough if the model can infer structure.

---

## Option C: Don’t use get_markdown(range) for section replace (prompt/flow only)

**Idea:** Avoid relying on get_markdown(scope="range") for section content. Prefer the search path that already works: the model sends the **full section text** (heading + body) as the search string and the translated full section as markdown.

**How:**

- In the system prompt (core/constants.py), state clearly that for “translate / replace section”:
  - **Preferred:** Use get_markdown(scope="full") (or the relevant slice from full markdown in memory), then apply_markdown(target="search", search=<full section text from get_markdown>, markdown=<translated full section including heading>). Do not use find_text + get_markdown(scope="range") for section content.
  - **If** the model must use find_text, then use apply_markdown(target="range", start=..., end=..., markdown=...) with a **translated** markdown string the model builds itself (e.g. "## Yhteenveto\n\nLegendaarinen...") and **not** by reading get_markdown(scope="range") until the range bug is fixed.
- No code change to markdown_support.py; only prompt/docs.

**Pros:** No risk of breaking the structural exporter; works with current search/replace behavior.  
**Cons:** Doesn’t fix the underlying bug; get_markdown(scope="range") remains wrong for anyone or any flow that uses it; model may still try find_text + get_markdown(range) unless the prompt is strict.

---

## Option D: Increase MAX_TOOL_ROUNDS (mitigation only)

**Idea:** Give the model more tool-calling rounds so it can retry or try a different strategy (e.g. fall back to search) after get_markdown(range) returns bad content.

**How:**

- In chat_panel.py, increase MAX_TOOL_ROUNDS from 5 to something higher (e.g. 8).

**Pros:** Trivial change; may let the model complete in some cases.  
**Cons:** Does **not** fix the corrupted range content; the model still gets "## ary..." and may keep failing or waste rounds.

---

## Option E: Hybrid — cursor slice + optional structure hint

**Idea:** For scope="range", get the exact text with the cursor (Option B), then optionally try to add minimal structure (e.g. if the first line is short and the next line is blank, treat first line as a heading) so the model gets something like "## Summary\n\nA legendary..." without touching the structural walk.

**How:**

- Implement the cursor-based range slice as in Option B.
- Optionally run a heuristic on that string: e.g. split on first "\n\n", if first segment is one line and short, prefix it with "## " and rejoin. Document that scope="range" is “best-effort” structure.

**Pros:** Same coordinates as find_text; slightly nicer output for section-like ranges.  
**Cons:** Heuristic can be wrong; still not true Writer-style markdown.

---

## Recommendation (for later)

- **If you want a proper fix and are okay with a bit of code:** Option A (cursor-based paragraph offsets) fixes the root cause and keeps correct markdown for range/selection.
- **If you want a minimal, low-risk change:** Option B (range = cursor slice, possibly documented as plain text) is simple and reliable; the model can still do section replace by building the replacement markdown itself.
- **If you want to avoid touching the exporter for now:** Option C (prompt-only, prefer search with full section text) avoids the broken path until you implement A or B.
- Option D is only a mitigation; Option E is a middle ground between B and A.

---

## Expert opinion: simplest and most robust

**Simplest and most robust solution (when you fix it):** Implement **Option A** (cursor-based paragraph offsets) inside `_range_to_markdown_via_temp_doc`. For each enumerated paragraph, measure `para_start` and `para_end` with the same cursor technique used by find_text (cursor at doc start, `gotoRange(el.getStart(), True)`, `len(cursor.getString())`; same for `getEnd()`). Use those values only for the range filter and trim. One coordinate system everywhere; no new APIs; the change is localized to the paragraph loop and has a clear fallback (if an element has no getStart()/getEnd(), skip or use cumulative offset). You keep full native markdown for range/selection and fix the bug at the source.

**Proper future fix to work on one day:** Option A as above is the right long-term fix. Option F (compareRegionStarts/Ends) is elegant and delegates overlap to UNO, but it depends on UNO behavior and may need more care with edge cases (e.g. getting the exact substring for partial paragraphs). Option A is explicit, easy to reason about, and matches how find_text and get_document_length already work. Combine it with a small prompt tweak: in the system prompt, state that for section replacement (e.g. translate the Summary section), the preferred flow is find_text(section heading) → find_text(next section heading) to get end → get_markdown(scope="range", start, end) → apply_markdown(target="range", start, end, markdown=...). That way the model uses the range path first and does not depend on the search path matching document line endings.

**Until then:** Option C (prompt-only: prefer range path, or avoid get_markdown(range) until fixed) plus optionally Option D (bump MAX_TOOL_ROUNDS) reduces pain without code changes to the exporter.

---

## Relevant code and logs

- **Range/selection handling:** markdown_support.py — `_range_to_markdown_via_temp_doc` (scope and trim; temp doc + storeToURL), `document_to_markdown` (range_start/range_end). Option A/F would be applied in `_range_to_markdown_via_temp_doc`; the old `_document_to_markdown_structural` has been removed.
- **Cursor-based length/range:** core/document.py — `get_document_length`, `get_text_cursor_at_range`; markdown_support.py — `_find_text_ranges` (measure_cursor.gotoRange(found.getStart(), True)).
- **Logs:** localwriter_chat_debug.log (e.g. under `~/.config/libreoffice/4/user/config/` or paths from clear_logs.sh) shows get_markdown(scope="range", start=112, end=340) returning "## ary\nA legendary...". Agent NDJSON: `~/localwriter_agent_debug.log` (see clear_logs.sh for all paths).
- **Issue summary:** SECTION_REPLACE_ISSUE.md (search path 0 replacements); this doc (range coordinate mismatch).

---

## Option F: (Recommended) Use UNO Region Comparison

**Idea:** Instead of trying to fix the offset math (Option A), use LibreOffice's `XText.compareRegionStarts` and `XText.compareRegionEnds` to filter paragraphs against the `find_text` range directly. This effectively delegates the "overlap" check to UNO, ensuring 100% alignment with the `find_text` coordinates regardless of hidden text or complex structures.

**How:**
1. In `markdown_support.py`, update `_range_to_markdown_via_temp_doc`: when scope is "range" or "selection", resolve integer `start`/`end` into a `filter_range` (XTextRange) using `get_text_cursor_at_range`.
2. Inside the paragraph loop:
   - Use `text.compareRegionEnds(paragraph, filter_range)` and `text.compareRegionStarts(paragraph, filter_range)` to check intersection.
   - If they intersect, use `filter_range.getString()` logic or range intersection logic to get the exact text.
3. Fall back to the old integer math if the UNO comparison API throws (safeguard).

**Pros:** Robust coordinate alignment; preserves structure (headings/lists) for the range; strictly correct for what `find_text` returns.
**Cons:** Requires using `compareRegion` APIs (standard in defined UNO, but good to wrap in try/except).
