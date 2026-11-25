# Follow-up loop audit: keeping the good, dropping the rest

2026-11-24, master at `91a3a6312`. Scope: the follow-up (`FU-*`) PRs merged since 0.9.6, Writer first.
Method: five read-only audits (three PR clusters read diff by diff, the ledger, and a 0.9.6-to-HEAD
comparison of the common Writer path). Nothing was run or timed; every risk below is from reading code
unless it says otherwise.

## Verdict

The loop is not converging, but it did real work early. Each chain fixed its common-case bugs in the
first one or two generations and then kept going on cases found by reading code. The plan is to keep
the early generations, stop the later ones, and repair the handful of places where late PRs changed
the ordinary path.

Numbers behind that:

- A merged follow-up bundle closes about 4.6 ledger rows and creates about 4.3 that survive triage.
  That ratio (about 0.9) has been flat for ten generations.
- In a 160-row sample: no row came from a user, 2% were seen in an eval or test run, 7% were
  reproduced with a probe, 64% were a reviewer reasoning from code. About 14% of kept rows would
  touch an ordinary Track Changes user.
- Writer non-test code: 37.8k lines at 0.9.0, 41.9k at 0.9.6, 45.6k now.
- 87 of 190 `FU-*` plan prompts carry the rule "WriterAgent has no users".

## What to keep

These are consolidations or common-case fixes. Leave them alone.

| Area | Keep | Why |
|---|---|---|
| Reads | `TrackedDeletions` / `DeletionState` / `visible_portions` as the one owner (#3899, #3927) | Most readers now go through it; no cost when a document has no redlines |
| Reads | One body walk for chat context (#3886) | Removed 188 lines |
| Reads | One offset unit through `_body_blocks` (#3938) | 0.9.6 had three inconsistent offset spaces |
| Reads | Tables included in range reads (#3856); multi-paragraph deletions hidden (#3873) | Common on redlined contracts |
| Writes | Whole-paragraph replace while recording (#3820); row delete tracked (#3855) | Common cases |
| Writes | Caret inserts through the pinned selection (#3965); index tools lose the `review=` flag (#3931) | Net deletions, one owner |
| Import | One placeholder mechanism for fields, math and ruby (#3918, #3955) | Deleted the segment inserter and ruby offset code |
| Links | Redlines read once per retarget (#3958) | Simple cost fix |
| Tables | Refusals that prevent silent data loss on merged or protected cells | Correct, though see task 9 |

## What went wrong in the later generations

1. **Fix-of-fix churn.** In the import/sections/links cluster, 11 of 19 PRs repair an earlier PR in
   the same list. `replace_full_document` was rewritten in six PRs in one day; `ApplyStyle._execute`
   in five; the range-copy walkers in nine of sixteen.
2. **Edge-case fixes changed the default path.** Several late PRs altered what an ordinary edit does
   with Track Changes off (see tasks 1 to 5).
3. **Re-implementing LibreOffice.** Struck logos, struck header tables, heading numeral parsing and
   table pre-flight checks each mirror LibreOffice internals in Python, and each round finds the next
   mismatch.
4. **No owner for recording-mode writes.** 34 `is_recording_changes(` checks in 17 files, 26 on/off
   toggles, about 35 hand-built `tracked` / `pending_review` result keys.
5. **Redlined documents were never timed,** though that is the main users' normal state.

## Tasks

Ordered by what a law office would notice first. Each is one PR unless noted. "Verify" means confirm
by probe or headed run before changing code; several of these may turn out to be fine.

### A. Safety (do first)

1. **Hidden-copy load during full reads.** `html_export.py:764` calls `load_scratch_document` without
   `page_preview_view_prop()`. `scratch_writer.py:41-50` says a normal hidden view makes Manage
   Changes abort LibreOffice. Verify with a probe, then pass the property. One line if confirmed.
2. **Hidden conditional section abort.** Merge #3989 (not on master yet), then add one refusal at the
   shared range-delete entry for a range enclosing a section with `IsVisible=False`. No chain.
3. **`nothing_landed` after a partial write** (`plugin/doc/compound_undo.py:161-163`, from #3942).
   Use the undo-stack rule so a nested failure cannot report "Nothing was changed".

### B. Default-path behaviour changes to review, then keep, narrow or revert

4. **Whole-paragraph replace strips direct formatting** (`html_import.py:913-963`, #3926). Test on a
   Word-origin numbered, justified clause. If numbering or layout is lost, narrow the reset to
   character formatting or revert.
5. **`replace_preserving_format` rewrite** (`html_import.py:1723-1890`, #3847). No bug found; needs a
   headed check with fields, links and another author's redlines at run edges.
6. **Block insert on an empty paragraph deletes that paragraph** (`html_import.py:1157-1173`, #3942).
   Check page breaks and bookmarks survive.
7. **`target='selection'` with a non-text selection now errors** (`html_import.py:1104`). Decide
   whether the old append-at-end behaviour was better for MCP after `image_insert`.
8. **`full_document` refuses on any hidden or protected body section**
   (`apply_document_content.py:1344`). Confirm that is wanted for templates with protected sections.
9. **Table refusals are dead ends for the model** (`tables.py:1018-1091`). Keep the refusals; make
   each message say what the model should do instead, and state in the result that rows do not shift
   while recording.
10. **`apply_style` returns `UNDO_UNAVAILABLE`** when the undo manager is locked
    (`styles.py:1709-1716`). Decide whether applying without grouping is the better failure.
11. **Deleted-paragraph markers in `get_content`** (#3933). Run the Writer eval with a document that
    has a long deleted clause; keep only if models handle the markers.

### C. Performance on redlined documents

12. **Time first.** Full read, search, outline and a table read on a 300-page contract with a few
    hundred redlines. Record the numbers.
13. **Hoist the loops** if the timing warrants: per-hit `overlaps` in `search.py:544,1730`; per-cell
    `state_at` in `tables.py:844`; the double portion walk in `tree.py:259-260`, gated on deletions
    rather than any redline.
14. **Full read with a multi-paragraph deletion** (`html_export.py:747-778`): if slow, find a route
    that does not load a hidden copy of the whole document.

### D. Simplify

15. **Heading-link retargeting: cut to one rule.** Retarget only when heading count and levels are
    unchanged and only the edited positions differ; otherwise warn. Deletes the fuzzy title matching
    and the numeral regexes in `hyperlink_fixup.py` (about 150 lines). Drop FU-HEADING-RENAME-4.
16. **One owner for recording-mode results.** A shared helper for `tracked` / `pending_review`, and
    stop annotating on mode alone. This is the useful part of FU-REVIEW-FLAGS-3.
17. **One "raw unless a deletion overlaps" rule.** It is written three times (`search.py:1730`,
    `selection.py:36`, `text_helpers.py:1072`).
18. **Flatten `insert_content_at_position`** (151 lines, nesting 6) and the repeated selection logic
    in `replace_single_range_with_content`. Readability only; do after B settles.

### E. Real bugs still open (from the 1124f proposal)

19. Search for a phrase that runs across a pending deletion (row 2386). Ship the search; refuse the
    replace if the policy is unclear.
20. Header field "1 of 3" becomes "1 of C" (FU-HEADER-TABLES-6).
21. Import style shift for `<hr>` and nested lists (FU-STRUCK-READS-2), with a plain round-trip test.
22. Image insert reads the live cursor after a slow generate; selection pin leaks to a sibling
    document (FU-CARET-INSERT-2).
23. One protected-cell rule for tracked and untracked table deletes; `style_format_text` skipping a
    leading table (FU-WRITER-SMALL-1124F).
24. Nested-field and Input-field text in the portion walkers (FU-CONTENT-CONTROL-3).

Everything else in the Writer bundles of 1124f can go to a post-1.0 file. The Calc, scripting, test
and CI bundles were not audited here.

## Changing the loop

- **Stop harvesting "Later" lists** from follow-up items at generation 2 or deeper.
- **Queue bar:** reproduced (probe, eval or user), and one of data loss, wrong Accept/Reject, crash or
  hang, secret leak, always-failing tool. Park the rest unbundled.
- **Prompt rule:** replace "WriterAgent has no users" with a rule that keeps the no-shim intent and
  adds: real users edit redlined Word-origin contracts; any change to behaviour with Track Changes
  off, or any new refusal, must be named in the plan and justified.
- **Plan check:** each plan states whether the diff touches a shared function on the default path,
  and if so what a plain edit now does differently.
- **Close a chain** when a generation yields no row that passes the bar.
- **Source of new rows:** a headed pass on real documents, not review of the last PR.

## Headed pass

One real 100+ page Word-origin contract with merged-cell tables, a TOC, cross-references and another
author's tracked changes including a multi-paragraph deletion. Drive it over MCP with recording on.

1. Full `get_content`, open Manage Changes, close the document. Time the read. (tasks 1, 14)
2. Replace a numbered, justified clause with `<p>` HTML; compare layout; Accept, Reject, Undo. (4, 5)
3. Delete a row and a column in a merged-cell table, recording on and off; insert a table and delete
   a row from it. (9)
4. Time outline and a heading rename with redlines present; check TOC and cross-references. (13, 15)
5. Replace-all on a term that also occurs inside a tracked deletion; check the count. (19)
6. `apply_style` on a paragraph while recording; check Manage Changes and that one Undo reverts. (10)
