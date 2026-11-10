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
| 4 | Partial match: ignore style but warn with `apply_style` call | 2.1 | 🟡 Style warning is appended to the result. The `suggested_call` shape from §4 isn't confirmed ❓ | #3263 |
| 5 | Paragraph alignment, colour, margins on write | 2.2 | ✅ Alignment, margins, background via `data-lo-para`. Also top/bottom margin, indent, line-height | #3260, #3270 |
| 6 | Accept `data-lo-para` on write (read/write symmetry) | 2.2 | 🟡 Geometry round-trips with no warnings. Paragraph-level font/size/weight/style/colour stay read-only on purpose, because setting `Char*` on the paragraph merges spans. A unit test guards the read table against drifting from the write table | #3260, #3270 |
| 7 | `full_document` summary result | 2.1 | ✅ Block summary with a shared slot extractor | #3262 |
| 8 | Strip HTML and normalize quotes in `old_content` | 2.4 | ✅ Tags stripped, quotes normalized, space runs collapsed | #3261 |
| 9 | Oversized `old_content` → suggest `full_document` | 2.4 | ✅ Needles ≥100 chars, checked on the miss path. Near-miss takes priority | #3261 |
| 10 | Anchor-based range replace (`start_anchor` / `end_anchor`) | 2.4 | ✅ Includes both anchors. Refuses spans containing a table and `hyperlink_url`. In the prompt | #3268, #3278 |
| 11 | One case-sensitivity default across search tools | 2.3 | ✅ Writer, Calc, Draw, page, comments, footnotes, indexes, TOC. Result carries `matched_case_sensitive` | #3265 |
| 12 | Split `dry_run` into `replaceable` / `not_replaceable` | 2.3 | ✅ Occurrence preview cap documented. Outline-rewrite fields omitted when the replacement is empty | #3264, #3269 |
| 13 | Header/footer contradiction removed | 2.5 | ✅ Prompt now says `target='search'` edits headers and footers. Page-domain delegation dropped. UNO test added | #3256 |
| 14 | No table deletion as a side effect | 2.5 | 🟡 Deletion is now reported (`table_deleted`, tracked-change aware). An empty replacement that removes a table's last text still deletes it. No `allow_table_delete` opt-in found | pre-existing |
| 15 | Literal `code` / `pre` content, no math parsing | 2.7 | ⬜ `html_import` still detects `$$`, `\(`, `\[` in content with no code-span exemption found | n/a |
| 16 | Report every Math object created | 2.7 | ✅ Successful `apply_document_content` reports `math_objects` for attempted TeX/MathML conversions (kind, display_block, snippet, ok, error) | #3286 |
| 17 | Resolve bare `#Heading` links / `$` math | 2.7 | ⬜ Prompt still tells the model to write `\|outline` and avoid `$` | n/a |
| 18 | Rename `clear_direct` options | 2.6 | ⬜ Still `none` / `style_props` / `all` | n/a |
| 19 | Report removed indents on restyle / track style changes | 2.6 | ⬜ `style_unreviewed` rule still in the prompt | n/a |
| 20 | Explicit delete (`position='delete'`) | 2.1 | ⬜ Not found | n/a |
| 21 | Regex engine validation (ICU vs Python `re`) | 2.3 | ⬜ Not found | n/a |
| 22 | Stable `error_code` + `suggested_call` on every error | §3, §4 | ⬜ Not in the writer edit path | n/a |

**Tally:** 9 shipped, 4 partial, 9 not started. Section 5's two top priorities (warnings, near-miss) are both at least partly done.

## 2. What changed, in plain terms

- **Search and matching (§2.3, §2.4):** the biggest area. Search now strips tags, normalizes quotes, reports near-misses, supports anchors, uses one case default, and reports it.
- **Style on search/selection (§2.1):** `data-lo-style` now works when the match covers whole paragraphs.
- **Paragraph formatting (§2.2):** the read/write asymmetry is mostly closed. Alignment, margins, indent, line-height, and background now round-trip. The remaining read-only properties are deliberate.
- **Feedback on writes (§2.1):** `full_document` now returns a summary, so the largest edit no longer gives the least feedback.
- **Prompt (§2.5):** the header/footer contradiction is gone. The prompt also gained anchor guidance and the `data-lo-style` change.
- **Not touched:** the §2.6 and §2.7 items. Nothing shipped on styles' side effects, `code` span literalness, or heading-link forgiveness.

## 3. Open work, by payoff

1. **Literal `code`/`pre` content (§2.7).** Low effort, high payoff, and still open. It is the failure the report was written to illustrate, and it is silent corruption.
2. **Generalize `warnings` (§2.2).** Extend the `data-lo-para`/`data-lo-style` mechanism to every ignored attribute, including table-cell styles. Add Math-object reporting.
3. **Table deletion opt-in (§2.5).** The deletion is now reported, but it is still a side effect of an edit.
4. **`clear_direct` rename (§2.6).** Cheap. The parameter still needs a paragraph of explanation.
5. **Bare `#Heading` links and `$` math (§2.7).** Accept the natural form, or warn.
6. **Style-change reporting (§2.6).** Report removed indents, or fold the style change and text edits into one undo step.
7. **Structured errors (§3, §4).** `error_code` + `suggested_call`. This is the broadest item and the one that most reduces prompt size.
8. **Regex engine validation (§2.3).** Name the engine that rejected a pattern.

## 4. Prompt shrinkage check (§6 of the original)

The original goal was to delete about half of the `apply_document_content` section of the prompt once the tool enforces the rules. Several rules can now be trimmed or already have been:

- ✅ Header/footer delegation: removed in #3256.
- This PR trimmed the oversized-`old_content` lecture, the multi-line paragraph grammar, the lenient-match and near-miss restatements, drawing-shape routing, the partial `data-lo-style` warning restatement, and the html/body wrap note; case-sensitivity wording and an ignored-on-search line were already absent.
- Must stay until the matching items ship: `|outline` links, no `$` math, `style_unreviewed`, `clear_direct` explanation.

## 5. Regression tests

The original list of "natural mistake" tests is mostly covered for shipped items: tests exist in `test_search.py`, `test_content_style_model_uno.py`, `test_apply_document_content_anchor_uno.py`, `test_apply_document_content_structured_return.py`, `test_html_import.py`, and `test_prompts.py`. Still missing:

- A document-about-the-API test with escaped tags and math delimiters inside `code` spans (§2.7). Add it when that fix lands. Write it first as a failing test.
- Clearing a table's cells one at a time while checking the table survives (§2.5).
- A `text-align:center` write that is either centered or reported as dropped. Alignment now applies, so this is a quick positive test.

## 6. Notes on this status

- Status comes from commit messages, diffs, and spot greps of `plugin/`. Items marked ❓ or ⬜ were not found in a quick search, not proven absent. Verify before acting on them.
- The report's effort estimates were guesses from outside the codebase. The commits show the Low/Medium estimates were about right.
