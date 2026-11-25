# AUDIT-HEADED-PASS: headed LibreOffice pass on a real contract (brief only, not queued)

Source: Fable's audit, ~/Desktop/Python/wa-reviews/followup-loop-audit-2026-11-24.md, "Headed pass" section.
Needs a headed LibreOffice with the current WriterAgent master extension installed, driven over MCP. Not run.
Best run after the A items (AUDIT-SCRATCH-PREVIEW, AUDIT-HIDDEN-SECTION, AUDIT-NOTHING-LANDED) merge, and again
after the B items, so the results show what users get now.

## Document
One real Word-origin contract of 100+ pages (.docx), with:
- merged-cell tables
- a TOC and cross-references
- numbered, justified clauses with direct formatting
- another author's tracked changes, including a multi-paragraph deletion

Work on a copy. Recording (Track Changes) on unless a step says otherwise. For each step, note what happened,
any error text, timings, and a screenshot if something looks wrong.

## Steps
1. Full `get_content`, then open Edit > Track Changes > Manage, then close the document. Time the read.
   Watch for a LibreOffice abort. (audit tasks 1, 14; AUDIT-SCRATCH-PREVIEW, AUDIT-FULLREAD-NO-HIDDEN-COPY)
2. Replace a numbered, justified clause with `<p>` HTML. Compare layout before and after (numbering, alignment,
   indents, tabs). Then Accept, Reject and Undo, checking each. Repeat once with recording off. (tasks 4, 5;
   AUDIT-IMPORT-DEFAULTS)
3. In a merged-cell table: delete a row and a column, with recording on and off. Insert a new table and delete a
   row from it. Check the refusal messages say what to do instead. (task 9; AUDIT-TABLE-REFUSALS)
4. Time `outline` and a heading rename with redlines present. Check the TOC and cross-references still point at
   the right headings. (tasks 13, 15; AUDIT-HOIST-LOOPS, AUDIT-HEADING-RETARGET)
5. Replace-all on a term that also occurs inside a tracked deletion. Check the reported count matches what the
   user sees, and the deleted occurrence is not touched. (task 19; FU-READS-STRUCK-5)
6. `apply_style` on a paragraph with recording on. Check Manage Changes shows it and that one Undo reverts it.
   (task 10; AUDIT-APPLY-STYLE-UNDO)

## Output
Write the results to chain-work/AUDIT-HEADED-PASS.results.md: one section per step with what happened,
timings, and any bug found (with the audit item it belongs to). Do not queue fixes; Keith decides.
