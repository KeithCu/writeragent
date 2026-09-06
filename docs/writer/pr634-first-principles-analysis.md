# PR #634 follow-ups — first-principles analysis

**Audience:** Keith / Chief / implementers  
**Inputs:** `/workspace/pr634-followups/initial-plan.md`, merged PR [#634](https://github.com/KeithCu/writeragent/pull/634) on `master`  
**Scope:** Research only — no product PR. Goal: proper fixes that stay **simple, robust, and easy for models**.

Symbols and paths below are current `master` unless noted.

---

## 1. Problem framing

### What #634 solved

Augusto’s PR fixed two real agent failure modes with unusual honesty about LibreOffice:

1. **Style apply that looks like success but changes nothing** — Re-applying a paragraph’s existing `ParaStyleName` does **not** clear direct `Char*` (LO 26.2). House-font updates via `style_update("Standard")` + `apply_style("Standard")` left Times-12 standing. #634 added `clear_direct` (`none` / `style_props` / `all`), capture/restore of portion-level Char*, `preserved_char_overrides` hints, and read-side `data-lo-para` from the FODT sidecar so agents can *see* whole-paragraph overrides.

2. **Silent letterhead destruction** — `page_set_header_footer_text` used `XText.setString`, which flattens logos and fields with no signal in `getString()`. #634 scans for fields/images and **refuses** unless `force=true`, points agents at `apply_document_content(target='search')`, and documents first-page regions (`header_first` / `footer_first`).

Compatibility defaults are conservative (`clear_direct` defaults to `none`; overwrite only with `force`). Prompts/docs were updated. Unit/mocks coverage of control flow is dense.

### What remains hazardous for agents

The remaining hazards are mostly **API shape and honesty**, not missing flags:

| Hazard | Why it bites models |
|--------|---------------------|
| Refuse treats **page-number fields like logos** | Normal footer (`Confidential \| Page N`) always errors; only `force` (destructive) is offered as retry |
| Refuse message steers to **document-wide search** | Firm name / “Confidential” hit the **body** first; field presentation `"1"` replaces destroy the field |
| **Region-off** leftover content not scanned | LO keeps `HeaderText` when `HeaderIsOn=False`; re-enable + write reports `ok` while deleting |
| Scan **does not walk tables** | Letterhead tables (logo \| address) never show as “held”; `setString` flattens them |
| **XText `==` / `!=` for logo anchors** | False “safe” refuse miss → original silent delete |
| Tool **description** still sells overwrite | Models pick tools from descriptions; refuse is buried in `force` + system prompt |
| First-page **logo** still half-reachable | `header_first` writable as text; `image_insert` only `header`/`footer` |
| Default same-style apply still needs a **second** `style_props` call for house font | Lawyer path remains two-step unless we teach the default path |
| Shared text helper **paragraph vs document** walk | `get_string_without_tracked_deletions` on a paragraph injects `\n` between portions |

Expedient patches (“add another prompt line”, “always force”, “always style_props”) feel wrong long-term because they either destroy content, fight preserve-inline tests, or leave the model with two contradictory tools for one intent.

---

## 2. Per open question

### 2.1 Do not treat page-number fields like logos

**Today (`plugin/writer/page.py`)**

- `_scan_region_content` collects **all** `TextPortionType == "TextField"` portions plus draw-page shapes whose anchor text matches the region.
- `_describe_region_contents(scan)` is non-empty if **either** images **or** fields exist.
- `PageSetHeaderFooterText.execute` refuses whenever `held` is non-empty and `force` is false. The error text lists fields and images together and offers: (a) `apply_document_content(target='search')`, or (b) `force=true` to delete them.

**Failure mode**

Office footers are routinely `text + PageNumber field`. After #634 every wording change requires either destroying page numbers (`force`) or a fragile document-wide search. That inverts the real need: **change words, keep fields**.

**Candidate designs**

| Option | Verdict |
|--------|---------|
| A. Keep refuse-on-any-field; improve prompt | **Reject** — prompt cannot invent a safe write path the API doesn’t offer |
| B. Split refuse: images/tables hard-refuse; fields warn but allow `setString` | **Reject** — `setString` still destroys fields; “warn then destroy” is worse honesty |
| C. Refuse images/tables/frames only; for field-only (or text+field) regions, **do not** offer `force` as the primary retry — point to surgical region edit | **Preferred direction** |
| D. Implement `page_replace_header_footer_text(region, old_content, content)` that edits only Text portions inside that `XText`, leaving field portions | **Best long-term**, still small |

**Recommended approach**

1. **Hard refuse** only when the scan finds **images** (and, once implemented, **tables/frames** — §2.3).  
2. **Fields alone do not refuse `page_set`…** *if and only if* we also stop using naive `setString` for that path — otherwise leaving fields un-refused is a lie. So:
   - Short term (honest interim): refuse fields **without** advertising `force` as the fix; error text = “use region-scoped replace / field tools; `force` deletes page numbers.”  
   - Proper fix (still small): add **`page_replace_header_footer_text`** (or extend set with `mode='replace_text'` + `old_content`) that:
     - Resolves only `_REGION_PROPS[region]`’s `XText`
     - Finds `old_content` inside that text object (not `doc.findFirst`)
     - Replaces **Text** portions only; never `setString` the whole region when fields/images exist
3. Keep `force=true` as explicit wipe for “replace entire letterhead with plain text,” reported via existing `deleted` payload.

**Tests**

- UNO: footer with only PageNumber field + surrounding text → replace “Confidential” → field still present; presentation still a number.  
- UNO: footer with AS_CHARACTER logo → plain set without force → refuse; document unchanged (`HeaderIsOn` / height unchanged).  
- UNO: `force=true` with logo → `deleted.images` non-empty; logo gone.

**Residual risk**

Surgical replace must not treat field **presentation** (`"1"`) as replaceable text. Match against visible non-field strings only (same portion filter as the scan).

---

### 2.2 `apply_document_content(target='search')` is not unconditionally safe for headers/footers

**Today**

- `plugin/writer/search.py`: `doc.findFirst` / chaining covers body, tables, frames, and `SwXHeadFootText`. `_header_footer_label` can label `HeaderTextFirst` etc. via `getattr(st, attr, None) == text_obj`.
- `plugin/writer/content.py` `ApplyDocumentContent`: `dry_run` returns locations; `all_matches` replaces every hit; description mentions search as substring find-and-replace but **does not** say matches may be in headers/footers or that first match may be body.
- #634’s refuse message **actively steers** agents to this path as the safe alternative to `page_set`.

**Failure modes**

1. Letterhead firm name also appears in the brief → first replace edits the body.  
2. `all_matches=true` edits header **and** body together.  
3. Searching the field’s rendered `"1"` + replace destroys the field (same as `setString`).  
4. Block HTML into `SwXHeadFootText` inherits nested-`XText` hazards already known for table cells.

**Candidate designs**

| Option | Verdict |
|--------|---------|
| A. Prompt-only: “always dry_run first” | Necessary but **insufficient** — models skip; description still sells unconditional safety |
| B. Require `dry_run` before non-dry search when any match location is header/footer | Better, still document-wide |
| C. Stop pointing header refuse at document-wide search; add **region-scoped** replace on the page tool | **Preferred** |
| D. Add `search_scope=body\|headers\|footers\|all` to `apply_document_content` | Useful later; larger surface than a page-region helper |

**Recommended approach**

- **API shape:** Prefer `page_replace_header_footer_text(style, region, old_content, content, dry_run?)` — one region, one `XText`, optional dry_run that only lists matches **in that region**. This is smaller and more model-friendly than teaching document-wide search discipline.  
- **Honesty:** Change `PageSetHeaderFooterText` refuse text to point at that tool (or “region replace”), not `target='search'`.  
- **Update** `apply_document_content` description: search **can** hit headers/footers; for letterhead wording use the page-region tool; always `dry_run` when unsure; never search field digits.  
- Keep document-wide search for deliberate cross-region edits, with `dry_run` + location strings.

**Tests**

- UNO: body and header both contain `"Acme LLP"`; region replace on `header` changes only header.  
- UNO: `apply_document_content` dry_run reports both locations; first non-dry replace without scope hits body (document the footgun in a test name if we keep the behavior).  
- Unit: refuse message no longer contains `target='search'` as the primary remedy once the page-region tool exists.

**Residual risk**

Until the page-region tool ships, softening refuse-on-fields without a surgical path is unsafe — ship them together or keep field refuse.

---

### 2.3 Scan leftover content when the region is off; walk tables

**Today**

```python
# PageSetHeaderFooterText.execute
if style.getPropertyValue(is_on_prop):
    existing = style.getPropertyValue(text_prop)
    if existing:
        scan = _scan_region_content(ctx.doc, existing)
```

If `HeaderIsOn` / `FooterIsOn` is false, scan stays empty → enable + `setString` → `ok`, even when LO still holds leftover `HeaderText` (logo, fields, tables).

`_scan_region_content` only:

- Enumerates **paragraphs → portions** for fields  
- Matches **draw-page shapes** by anchor text  

A letterhead **table** enumerates as a table element, not paragraphs — fields inside cells are missed; `setString` flattens the table.

**Recommended approach**

1. **Always scan** when `text_prop` resolves to a non-null `XText`, regardless of `is_on_prop`. Toggle-off is not “empty.”  
2. On enumeration, if an element is a **table** (or non-paragraph with nested text): either  
   - **refuse** (“region holds a table; use force only to wipe, or edit cells surgically”), or  
   - recursively scan cell `XText`s for fields/images (heavier).  
   Prefer **refuse-on-non-paragraph** for v1 of the follow-up — simple, model-clear, matches “don’t flatten structures.”  
3. Same rule for text frames nested in the header if encountered.

**Tests**

- UNO: write logo+text, set `HeaderIsOn=False`, call set without force → refuse; content intact when re-enabled.  
- UNO: header with 1×2 table → refuse without force; `paragraph_count` / new `structures` signal in scan optional.  
- UNO: plain empty header with region off → set allowed (true empty).

**Residual risk**

Deep nested walks are unbounded; keep caps (`_SCAN_*_LIMIT` already exist) and refuse rather than partial scan false negatives.

---

### 2.4 Header/footer tool still advertises the destructive path

**Today**

`PageSetHeaderFooterText.description` begins: *“Set the text content… Automatically enables…”* Refuse semantics live on the `force` parameter description and in the system prompt.

**Recommended approach**

Rewrite the **description to lead with constraints** (models read this first):

- Plain-text **whole-region** replace.  
- Refuses if the region holds **images or tables** (and, until surgical replace exists, fields).  
- Prefer **region-scoped replace** for wording changes.  
- `force=true` = deliberate wipe.  
- Enabling/auto_height are secondary sentences.

Parameter docs for `force` should not be the only place truth lives.

**Tests**

- Snapshot/unit: description contains refuse / surgical language; does not claim unconditional set.  
- No UNO required.

---

### 2.5 Image identity can miss the logo (`XText` `==` / PyUNO)

**Today**

```python
if shape.getAnchor().getText() != text_obj:
    continue
```

`_header_footer_label` in `search.py` uses the same fragile `getattr(st, attr, None) == text_obj`.

`plugin/draw/shapes.py` `_page_index_for` already documents the robust pattern: `is` → `==` → `uno.isSame` if present.

**Failure mode**

If `!=` is True for the same underlying `XText`, the logo is invisible to the scan → refuse skipped → `setString` deletes the letterhead — **#634’s bug with a false sense of safety**.

**Recommended approach**

Extract a tiny helper, e.g. `uno_text_same(a, b) -> bool`, next to other UNO identity helpers (or in `page.py` shared with search labeling):

1. `a is b`  
2. try `a == b`  
3. try `uno.isSame(a, b)` when available  
4. else False (fail closed for safety scans: if unsure, treat as **match** when scanning for destruction? or fail closed as “held unknown”)

For **refuse scans**, fail closed means: if identity is uncertain, **assume the shape might be in-region** only when ImplementationName/anchor suggests header — actually safer refuse rule: if `getAnchor()` succeeds and `ImplementationName` of text is `SwXHeadFootText` and we can’t prove it’s a *different* region, include it. Simpler practical rule used elsewhere: try all three comparisons; if any says same → same. If all say different → different. The false-negative (miss logo) is the disaster; false-positive (extra refuse) is recoverable with `force`.

Also reuse the helper in `_header_footer_label` so labeling and scanning agree.

**Tests**

- **Live UNO required** (mocks cannot catch this): AS_CHARACTER graphic in header; assert scan lists it; set without force refuses; document unchanged.  
- Unit: helper returns True for mocked equal paths; False when all differ.

**Residual risk**

`uno.isSame` missing on some builds — already handled in shapes.py; copy that comment.

---

### 2.6 First-page letterhead still half-reachable

**Today**

- `_REGION_PROPS` includes `header_first` / `footer_first` → text get/set works when `FirstIsShared=False`.  
- `image_insert` `target` enum is only `body|header|footer`. `insert_image_into_header_footer` keys off the same shared regions.

**Recommended approach**

Extend `target` (and the insert helper) to accept `header_first` / `footer_first`, reusing `_REGION_PROPS` / page helpers already used for text. Update description: first-page logos need `first_is_shared=false` then `target=header_first`.

If extending image insert slips, the page-tool description must **say** image_insert cannot place first-page logos yet — half-reachable without disclosure is the bug.

**Tests**

- UNO: `FirstIsShared=False`, insert into `header_first`, confirm `HeaderText` empty of that graphic and `HeaderTextFirst` holds it.  
- Extend `tests/writer/test_search_reach.py` `FakePageStyle` with `HeaderTextFirst` so label strings don’t regress.

---

### 2.7 Same-style house-font on first `apply_style` (lawyer path)

**Today (`format.py` `apply_paragraph_style_preserving_direct_char`)**

Documented LO fact: setting `ParaStyleName` to a **different** style resets Char*; re-applying the **same** style does not. Default `clear_direct="none"` restores all captured Char* overrides → house font invisible; hint tells agent to retry with `style_props`.

Initial plan proposal: on default/`none`, if current `ParaStyleName == style_name`, skip restoring only `STYLE_GOVERNED_CHAR_PROPERTIES` (font/size), still restore bold/italic/colour; do not clear `CLEARABLE_PARA_PROPERTIES`.

**Assessment:** Plan is **right**. It matches LO’s actual asymmetry and keeps preserve-inline UNO tests (red/bold only) green. It is better than “always style_props” (wipes Courier exceptions) and better than “prompt says call twice.”

**Recommended approach**

Implement the same-style rule exactly as the plan states. Optional later tighten: only auto-skip font restore when all captured portions share the same font/size (defer until a UNO test demands it).

Clarify product semantics:

- Omitted / `"none"` + **same style** → house font wins, emphasis kept, quote indents kept.  
- Omitted / `"none"` + **different style** → today’s full restore.  
- `"style_props"` / `"all"` unchanged explicit clears.

**Tests** (as plan)

- UNO lawyer sequence → Arial 9.5, bold survives.  
- Existing preserve-inline red/bold still passes.  
- Apply Quotations to Times-direct Standard preserves Times unless `style_props`.  
- Unit: same-style path skips restoring font/size only.

**Residual risk**

Courier word inside already-Standard paragraph loses Courier on house-font apply — accept until evidence says otherwise; document in tool description one line.

**Murky UNO (verify before locking copy):** `format.py` claims re-applying the same `ParaStyleName` still **drops direct Para*** (indents/alignment) even though Char* stay. If that is true on target LO builds, agent-facing language that `clear_direct='none'` “preserves formatting” overclaims for quote indents — today *and* after the same-style font rule. Do **not** paper over this with more prose: add a UNO test that applies Standard→Standard on a paragraph with only `ParaLeftMargin` direct, and record whether the indent survives. That result decides whether the house-font follow-up must also re-apply captured Para* on the `none` path (heavier) or whether docs simply stop promising indent preserve on same-style re-apply.

---

### 2.8 `get_string_without_tracked_deletions` paragraph walk

**Today (`plugin/doc/text_helpers.py`)**

Always `createEnumeration()` and treats each child as a **paragraph**, joining with `\n`. On a **paragraph** `XTextRange`, children are **portions** → bold runs become separate “paragraphs” with newlines. `html_export._visible_portions` reimplements the correct portion walk and comments that it must stay aligned for offset paint.

Call sites still passing paragraph-ish objects: `tree.py`, `text_analytics.py`, `linguistic_index.py`, plus html_export’s intentional bypass.

**Recommended approach**

- Detect paragraph (service `com.sun.star.text.Paragraph` or first child has `TextPortionType`).  
- Portion mode: concatenate **without** `\n` (same Redline/Delete rules).  
- Document mode: keep paragraph `\n` join.  
- Move `_visible_portions` beside the helper and reuse — one walk, one error policy (`continue` vs `return` must match; prefer `continue` for robustness unless paint requires abort — paint currently `return`s on portion enum failure to avoid offset drift; document that divergence if kept).

**Tests**

- UNO: single paragraph with bold run → helper returns one line equal to `getString()` visible text (no mid `\n`).  
- UNO: multi-paragraph range still joins with `\n`.  
- Offset paint still matches helper string (existing range-export path).

---

### 2.9 `style_get_info` PageStyles extra hop

**Today (`styles.py`)**

`family == "PageStyles"` returns a tool error telling the agent to call `page_get_style_properties`. Correct functionally; burns a turn.

**Recommended approach**

In-process dispatch to the same implementation `page_get_style_properties` uses (shared function, not a nested LLM call). Return that payload with a clear `family: PageStyles` (or existing page-tool shape). Error redirect was a fine first cut; dispatch is the proper small fix.

**Tests**

- Unit/mock: `style_get_info(PageStyles, Standard)` returns margin/header fields without `status=error`.

---

### 2.10 `data-lo-para` colour / escape / attribute detection

**Today (`xhtml_style_postprocess.py`)**

- `_FODT_OVERRIDE_ATTRS` includes margins, indent, align, line-height, **font-name/size/weight/style** — docstring correctly claims geometry + paragraph-level font.  
- **`fo:color` is absent** — Issue-1-style “center + red” still won’t show red in `data-lo-para` (char-level red may still appear via text-* spans). Docs that claim “colour” in `data-lo-para` are wrong; `llm-styles.md` may still say Para colour not preserved — align claims.  
- Inject: `' data-lo-para="%s"' % para_css` after `_html.unescape` of ODF values — **no `_html.escape(..., quote=True)`** on emit. A `font-family` containing `"` breaks the tag.  
- `_note_read_only_attrs` (`content.py`): `"data-lo-para" in item` substring — body text mentioning the name false-positives.

**Recommended approach**

1. Add `fo:color` → `color` to `_FODT_OVERRIDE_ATTRS` **or** drop colour claims in docs (prefer add — matches Issue 1 fixture).  
2. Escape attribute values on inject (`quote=True`).  
3. Detect the attribute with a small regex / HTML attr parse, not substring.  
4. Update `llm-styles.md` / plan docs to match reality.

**Tests**

- Unit: font name with `"` round-trips into a well-formed attribute.  
- Unit: export with fo:color in FODT autostyle → `color:` in `data-lo-para`.  
- Unit: content body containing the words `data-lo-para` without the attribute → no `ignored_attributes`.

---

### 2.11 `_paint_direct_formatting` aborts Char* if Para* fails

**Today (`html_export.py`)**

Para property copy in try/except; on failure **`return`** before the portion Char* loop. A single refused `Para*` drops the entire reason the temp-doc path exists (bold/indent visibility on range read).

**Recommended approach**

Catch Para* failures, log, **continue** to portion painting. Char* failures already `continue` per portion.

**Tests**

- Unit with stubs: Para copy raises → Char copy still invoked.  
- UNO range read of bold-inside-odd-para still shows emphasis in export when possible.

---

### 2.12 Smaller nits (brief — plan already clear)

| Item | Recommendation |
|------|----------------|
| `CLEARABLE_PARA_PROPERTIES` “cycle” comment | Define once; import; comment is stale (`styles` already imports from `format`) |
| `_COPY_PORTION_LIMIT` silent truncate | Log warning with counts |
| `_merge_reports` last-wins | Schema note or list per-range overrides — don’t invent a new confirm UX yet |
| Asian/Complex in clear but not `REPORTED_CHAR_PROPERTIES` | Add to report **or** stop clearing unreported names on `style_props` — prefer report them |
| `writeragent_api` proxies | Regenerate when page API grows `force` / regions / replace |
| Live UNO suite gap | Still the highest-value work the plan named — mocks don’t catch XText identity or `setPropertiesToDefault` |

---

## 3. Cross-cutting principles

1. **Refuse vs surgical vs force**  
   - **Refuse** = would destroy structure the tool cannot express (images, tables).  
   - **Surgical** = edit text in-region without touching fields/images.  
   - **Force** = explicit wipe, always reported in `deleted`.  
   Never use force as the “normal” retry for wording.

2. **Region-scoped beats document-wide for letterheads**  
   Headers share strings with bodies. A page-region tool is simpler for models than teaching `dry_run` + location discipline on global search.

3. **Tool description honesty**  
   If refuse/surgical is the real contract, the **description** says so first. Parameters and system prompts are backups.

4. **XText identity**  
   One shared `uno_text_same`; fail closed on refuse scans (prefer false refuse over silent delete).

5. **LO same-style vs different-style**  
   Encode LO’s asymmetry in defaults (same-style house font) rather than making agents discover `style_props` by failed visual confirms.

6. **One text walk**  
   Tracked-deletion visibility must be a single helper; exporters paint from the same string.

7. **Read reports ≠ write instructions**  
   `data-lo-para` stays read-only; detect it as an attribute; escape it; don’t pretend colour exists if FODT doesn’t emit it.

8. **Tests lock UNO quirks before behavior changes**  
   Especially: logo identity, region-off leftover, field-preserving replace, same-style font clear.

---

## 4. Recommended follow-up order

Sequenced so UNO tests pin quirks before API behavior shifts:

1. **Shared `uno_text_same` + live UNO: refuse-on-logo still works** (proves scan identity before changing refuse rules).  
2. **Always scan when `text_prop` exists (region off) + refuse-on-table**; UNO tests for both.  
3. **Field policy + `page_replace_header_footer_text` (or equivalent)** in one slice: stop steering to global search; rewrite `page_set` description; UNO field-preserve replace.  
4. **Same-style default house-font** in `apply_paragraph_style_preserving_direct_char` + lawyer UNO + preserve-inline regression.  
5. **Fix `get_string_without_tracked_deletions`** + share `_visible_portions`; paragraph UNO test.  
6. **`data-lo-para` escape + `fo:color` (or doc fix) + attribute-shaped `_note_read_only_attrs`**.  
7. **`_paint_direct_formatting` don’t abort Char* on Para* failure**.  
8. **`image_insert` + `header_first` / `footer_first`**; search-reach FakePageStyle.  
9. **`style_get_info` → in-process page props**; regenerate scripting proxies as needed.  
10. **Nits pack:** CLEARABLE single source, portion-limit warning, Asian/Complex reporting, docs (`llm-styles.md`).

Parallelizable: (5)–(7) and (8)–(9) after (1)–(3) land, if staffing allows — but do not change field refuse before surgical replace exists.

---

## 5. Non-goals

- Making `data-lo-para` **writable** / round-tripping Para* through HTML import.  
- Boiling the ocean on full header HTML import into `SwXHeadFootText`.  
- Replacing LibreOffice’s same-style Char* persistence with an extension-wide “always Ctrl+M.”  
- Dropping `apply_document_content` search reach into headers (reach is useful; **unscoped** letterhead edits are the problem).  
- Perfect CJK font reporting before the house-font path works for Latin lawyer docs.  
- Rewriting the entire page API surface (`header_right` etc.) — document unshared behavior; don’t block on it.  
- Expedient “always `force`” or “always `style_props`” prompt patches as substitutes for the API shapes above.

---

## Appendix — quick symbol map

| Concern | Primary symbols |
|---------|-----------------|
| Header refuse / scan | `PageSetHeaderFooterText`, `_scan_region_content`, `_describe_region_contents`, `_REGION_PROPS` — `plugin/writer/page.py` |
| Search reach / labels | `find_chained_range`, `_header_footer_label`, `describe_match_location` — `plugin/writer/search.py` |
| Document apply / dry_run | `ApplyDocumentContent` — `plugin/writer/content.py` |
| Style clear / house font | `apply_paragraph_style_preserving_direct_char`, `STYLE_GOVERNED_CHAR_PROPERTIES`, `CLEARABLE_PARA_PROPERTIES` — `plugin/writer/format.py`; `ApplyStyle` — `styles.py` |
| data-lo-para | `_FODT_OVERRIDE_ATTRS`, `_fodt_override_css`, inject in postprocess — `plugin/writer/xhtml_style_postprocess.py`; `_note_read_only_attrs` — `content.py` |
| Tracked deletions | `get_string_without_tracked_deletions` — `plugin/doc/text_helpers.py`; `_visible_portions`, `_paint_direct_formatting` — `html_export.py` |
| Image targets | `ImageInsert`, `insert_image_into_header_footer` — `plugin/writer/images/images.py` |
| XText identity pattern | `_page_index_for` — `plugin/draw/shapes.py` |
