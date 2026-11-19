# Editing-API Rules: Live Status Report

*Originally "Too Many Rules to Remember", Opus's review of the WriterAgent editing API and system prompt (Nov 2026). Now a status tracker. Last updated 2026-11-09 from `git log` through `8e3cdd494` (#3278).*

**Thesis (unchanged):** move API mechanics out of the prompt and into the tool. The tool should do the right thing, or fail or warn with the fix. The prompt keeps only judgment rules.

**Legend:** ✅ shipped · 🟡 partial · ⬜ not started / not found in code · ❓ unverified

## 1. Scoreboard

| # | Change | Report § | Status | Commit(s) |
|---|--------|----------|--------|-----------|
| 1 | `warnings` array for dropped attributes | 2.2 | 🟡 Exists for `data-lo-para` and `data-lo-style` (`_attach_para_warnings`). Not yet a general mechanism for all drops (table-cell styles, etc.) | #3260 |
| 2 | Near-miss report when search fails | 2.4 | ✅ Near-miss on a miss, with the miss paths unified | #3261 |
| 3 | Honor `data-lo-style` on whole-paragraph search/selection matches | 2.1 | ✅ | #3263 |
| 4 | Partial match: ignore style but warn with `apply_style` call | 2.1 | ✅ Diagnostic warning carries `suggested_call` for `apply_style`; prompt prefers `suggested_call` | #3263, #3288 |
| 5 | Paragraph alignment, colour, margins on write | 2.2 | ✅ Alignment, margins, background via `data-lo-para`. Also top/bottom margin, indent, line-height | #3260, #3270 |
| 6 | Accept `data-lo-para` on write (read/write symmetry) | 2.2 | 🟡 Geometry round-trips with no warnings. Paragraph-level font/size/weight/style/colour stay read-only on purpose, because setting `Char*` on the paragraph merges spans. A unit test guards the read table against drifting from the write table | #3260, #3270 |
| 7 | `full_document` summary result | 2.1 | ✅ Block summary with a shared slot extractor | #3262 |
| 8 | Strip HTML and normalize quotes in `old_content` | 2.4 | ✅ Tags stripped, quotes normalized, space runs collapsed | #3261 |
| 9 | Oversized `old_content` → suggest `full_document` | 2.4 | ✅ Needles ≥100 chars, checked on the miss path. Near-miss takes priority | #3261 |
| 10 | Anchor-based range replace (`start_anchor` / `end_anchor`) | 2.4 | ✅ Includes both anchors. Refuses spans containing a table and `hyperlink_url`. In the prompt | #3268, #3278 |
| 11 | One case-sensitivity default across search tools | 2.3 | ✅ Writer, Calc, Draw, page, comments, footnotes, indexes, TOC. Result carries `matched_case_sensitive`; edit/anchor tools exact first (WRITER-SEARCH-EXACT) | #3265 |
| 12 | Split `dry_run` into `replaceable` / `not_replaceable` | 2.3 | ✅ Occurrence preview cap documented. Outline-rewrite fields omitted when the replacement is empty | #3264, #3269 |
| 13 | Header/footer contradiction removed | 2.5 | ✅ Prompt now says `target='search'` edits headers and footers. Page-domain delegation dropped. UNO test added | #3256 |
| 14 | No table deletion as a side effect | 2.5 | 🟡 Deletion is now reported (`table_deleted`, tracked-change aware). An empty replacement that removes a table's last text still deletes it. No `allow_table_delete` opt-in found | pre-existing |
| 15 | Literal `code` / `pre` content, no math parsing | 2.7 | ⬜ `html_import` still detects `$$`, `\(`, `\[` in content with no code-span exemption found | n/a |
| 16 | Report every Math object created | 2.7 | ✅ `apply_content` reports `math_objects` on ok results; any formula conversion failure reports a `MATH_IMPORT_FAILED` warning with `math_objects`, marker text still inserted | #3286 |
| 17 | Resolve bare `#Heading` links / `$` math | 2.7 | ✅ Bare `#Heading` resolved to `#Heading\|outline` on import; complete `$...$` routed through HTML import; warnings on missing targets | #3295 |
| 18 | Rename `clear_direct` options | 2.6 | ⬜ Still `none` / `style_props` / `all` | n/a |
| 19 | Report removed indents on restyle / track style changes | 2.6 | ⬜ `style_unreviewed` rule still in the prompt | n/a |
| 20 | Explicit delete (`position='delete'`) | 2.1 | ⬜ Not found | n/a |
| 21 | Regex engine validation (ICU vs Python `re`) | 2.3 | ✅ Validated via LibreOffice TextSearch2 (ICU); Python-only syntax rejected, ICU classes allowed | st-regex-icu |
| 22 | Stable `error_code` + `suggested_call` on every error | §3, §4 | ⬜ Not in the writer edit path | n/a |

**Tally:** 13 shipped, 3 partial, 6 not started. Section 5's two top priorities (warnings, near-miss) are both at least partly done.

## 2. What changed, in plain terms

- **Search and matching (§2.3, §2.4):** the biggest area. Search now strips tags, normalizes quotes, reports near-misses, supports anchors, uses one case default, and reports it.
- **Style on search/selection (§2.1):** `data-lo-style` now works when the match covers whole paragraphs.
- **Paragraph formatting (§2.2):** the read/write asymmetry is mostly closed. Alignment, margins, indent, line-height, and background now round-trip. The remaining read-only properties are deliberate.
- **Feedback on writes (§2.1):** `full_document` now returns a summary, so the largest edit no longer gives the least feedback.
- **Prompt (§2.5):** the header/footer contradiction is gone. The prompt also gained anchor guidance and the `data-lo-style` change.
- **Shipped:** heading-link forgiveness, `$` math, Math-object reporting.
- **Not touched:** `code` span literalness, styles' side effects (§2.6, §2.7).

## 3. Open work, by payoff

1. **Literal `code`/`pre` content (§2.7).** Low effort, high payoff, and still open. It is the failure the report was written to illustrate, and it is silent corruption.
2. **Generalize `warnings` (§2.2).** Extend the `data-lo-para`/`data-lo-style` mechanism to every ignored attribute, including table-cell styles. (Math-object reporting shipped in `#3286`.)
3. **Table deletion opt-in (§2.5).** The deletion is now reported, but it is still a side effect of an edit.
4. **`clear_direct` rename (§2.6).** Cheap. The parameter still needs a paragraph of explanation.
5. **Bare `#Heading` links and `$` math (§2.7) — Shipped (`#3295`).** Bare `#Heading` resolves to `#Heading|outline` on import and complete `$...$` routes through HTML import, with warnings on missing targets.
6. **Style-change reporting (§2.6).** Report removed indents, or fold the style change and text edits into one undo step.
7. **Structured errors (§3, §4).** `error_code` + `suggested_call`. This is the broadest item and the one that most reduces prompt size.
8. **Regex engine validation (§2.3) — Shipped (`st-regex-icu`).** Validated via LibreOffice TextSearch2 (ICU); Python-only syntax is rejected and ICU classes are allowed.

## 4. Prompt shrinkage check (§6 of the original)

The original goal was to delete about half of the `apply_content` section of the prompt once the tool enforces the rules. Several rules can now be trimmed or already have been:

- ✅ Header/footer delegation: removed in #3256.
- This PR trimmed the oversized-`old_content` lecture, the multi-line paragraph grammar, the lenient-match and near-miss restatements, drawing-shape routing, the partial `data-lo-style` warning restatement, and the html/body wrap note; case-sensitivity wording and an ignored-on-search line were already absent.
- Must stay until the matching items ship: ~~`|outline` links~~ (bare links are resolved, #3295; the example is pull-only), no `$` math, `style_unreviewed`, ~~`clear_direct` explanation~~ (lives only in the `apply_style` schema).
- PROMPT-TRIM-2 (Opus 2026-11-10 cut list): one copy of the HTML tag rules (document side), one merged data-lo-style/data-lo-para line, a one-line TOOLS section, generic confirm-by-fields rule, review modes folded to four bullets, research routing said once (core directives + catalog), the translation old_content line and the "Failed tool call" line dropped (enforced by the tool), and the apply_style / apply_content descriptions no longer repeat their own parameter descriptions. Rejected: moving python-vs-shapes into the delegate description (no per-turn saving), the OCR line (not ambient), a manual_read pointer (same object as the ambient block).
- PROMPT-TRIM-3: dropped the TOOLS line (in `GetGuidance.description`), `data-lo-para` property lists and `clear_direct`/quote-indent lines (in tool warnings and `ApplyStyle` schema), and the `web_research` repeat; moved Writer python/shapes routing and research `task` hint into `DelegateToSpecializedWriter.description` (python/shapes moved despite the TRIM-2 rejection because it replaces the research-trigger repeat in the same description, net −1,560 chars/turn), Writer OCR routing into `ToolWriterVisionBase.specialized_domain_description` (the `vision` catalog line, gated by `vision_venv_configured` rather than the static delegate description), and the math/`|outline` examples into `HTML_MATH_AND_LINK_EXAMPLES` (served via `manual_read` `editing-html`, `full_manual`, and sub-agents). Writer system prompt (`get_chat_system_prompt_for_kind("writer")`): 12,115 → 10,053 chars.
- PROMPT-TRIM-5 (fifth Opus review, 2026-11-18): moved `PARAGRAPH_INDEX_DIRECTIVE` into `TOOL_USAGE_PATTERNS` as its single home (dropped from `nav_*`, `page_get_objects`, `get_tree`, `search`, and `WRITER_SEARCH_RULES`); moved the `web_research` vs. `document_research` boundary to `ToolWriterDocumentResearchBase.specialized_domain_description`, split reply-only vs. insert-below vs. long-document translation rules in `TRANSLATION_RULES` (section-by-section `start_anchor`/`end_anchor` avoids hitting output limits on a single `full_document` call, and keeping `<div|span ... id="Name">` wrappers preserves `image_placeholders` restore), scoped `comment_add` vs. `comments` domain in `TOOL_USAGE_PATTERNS` and `ToolWriterCommentBase`, dropped the redundant voice sentence from `HUMANIZER_PROMPT_HEADING`, steered headings to `<h1>`–`<h6>` and dropped the false single-string "Bad" example in `SUB_AGENT_HTML_CONTENT_RULES`, moved the TOC read-only reporting rule from `ToolWriterIndexBase` to `_LOOP_HINTS["indexes"]` (and forwarded it in `get_domain_guidance`), and trimmed redundant rejection/edge-case prose from `apply_content`, `get_content`, and `search`. Rejected: dropping `_mcp_` bookmark notes in `get_tree` (`page_get_objects` and `nav_list_heading_children` accept `bookmark:` and session-only is silent) and `x²` as `<sup>` (contradicts the variable-math LaTeX cutoff; added `(x², E = mc²)` examples on the LaTeX side instead); out-of-scope items were deferred (JSON-Schema `default`s landed separately in #3682). Writer system prompt (`get_chat_system_prompt_for_kind("writer")`): 9,024 → 10,084 chars; Writer main-chat schemas: 21,114 → 19,573 chars (combined 30,138 → 29,657 chars).

## 5. Regression tests

The original list of "natural mistake" tests is mostly covered for shipped items: tests exist in `test_search.py`, `test_content_style_model_uno.py`, `test_apply_document_content_anchor_uno.py`, `test_apply_document_content_structured_return.py`, `test_html_import.py`, and `test_prompts.py`. Still missing:

- A document-about-the-API test with escaped tags and math delimiters inside `code` spans (§2.7). Add it when that fix lands. Write it first as a failing test.
- Clearing a table's cells one at a time while checking the table survives (§2.5).
- A `text-align:center` write that is either centered or reported as dropped. Alignment now applies, so this is a quick positive test.

## 6. Notes on this status

- Status comes from commit messages, diffs, and spot greps of `plugin/`. Items marked ❓ or ⬜ were not found in a quick search, not proven absent. Verify before acting on them.
- The report's effort estimates were guesses from outside the codebase. The commits show the Low/Medium estimates were about right.

## Open

- `[fidelity S]` Treat `code` and `pre` content literally so `$$`, `\(` and `\[` inside code spans are not parsed as math (silent corruption today). (§ 3. Open work, by payoff, item 1)
- `[fidelity M]` Generalize the `warnings` array from `data-lo-para` and `data-lo-style` to every ignored attribute, including table-cell styles. (§ 3. Open work, by payoff, item 2)
- `[fidelity S]` Add an explicit opt-in so an edit that empties a table's last text cannot delete the table as a side effect. (§ 3. Open work, by payoff, item 3)
- `[eng S]` Rename the `clear_direct` options (`none` / `style_props` / `all`) so the parameter needs no paragraph of explanation. (§ 3. Open work, by payoff, item 4)
- `[fidelity M]` Report removed indents on restyle, or fold the style change and text edits into one undo step, so the `style_unreviewed` prompt rule can go. (§ 3. Open work, by payoff, item 6)
- `[feature S unverified]` Explicit delete through `position='delete'` on the write tool. (§ 1. Scoreboard, row 20)
- `[eng L]` Stable `error_code` plus `suggested_call` on every error in the Writer edit path, the change that most shrinks the prompt. (§ 3. Open work, by payoff, item 7)
- `[eng S]` Add the missing regression tests: code-span escaping, clearing table cells one at a time, and centered alignment on write. (§ 5. Regression tests)
