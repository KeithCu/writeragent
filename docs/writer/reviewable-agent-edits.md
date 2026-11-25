# Reviewable Agent Edits

When review mode is on, supported agent edit paths land as **native LibreOffice tracked changes
(redlines)** the user can accept or reject; on the blocking path the agent also learns the
per-change outcome. Review is Writer-only.

See **Current scope** below for which paths record vs block today.

## Configuration

Settings live on the **Doc** tab in WriterAgent Settings ([`plugin/doc/module.yaml`](../../plugin/doc/module.yaml)).

| Key | Default | Meaning |
|-----|---------|---------|
| `doc.agent_edit_review_mode` | `off` | **Off** — direct edits. **Record** — tracked changes you accept/reject yourself. **Wait** — same as Record, and `apply_content` blocks (on chat/MCP worker thread) until you review. |
| `doc.edit_review_timeout` | `900` (seconds) | Max time `apply_content` blocks when mode is **Wait**. Internal — not shown in Settings; set in `writeragent.json` if you need to change it. |

> [!NOTE]
> The two original boolean configuration flags (`writer.track_changes_reviewable` and `writer.require_edit_review`) have been consolidated into the single dropdown setting `doc.agent_edit_review_mode`.
> The timeout setting `doc.edit_review_timeout` is now an internal-only configuration value and is hidden from the Settings UI dialog.

`review_recording_enabled()` returns true when mode is **record** or **wait**.
`edit_review_wait_seconds()` returns the timeout when mode is **wait**, else `0`.

Use `get_agent_edit_review_mode()` for the raw mode string (`off` / `record` / `wait`).

## Current scope

What is implemented today (not every Writer mutation tool):

| Path | Records redlines | Blocks until reviewed | Returns `review` outcomes |
|------|------------------|----------------------|---------------------------|
| `apply_content` (chat/MCP worker thread) | Yes | Yes, when wait enabled | Yes |
| `apply_content` (main thread, e.g. debug tests) | Yes | No (UI would freeze) | No |
| Extend / edit selection (menu + sidebar) | Yes | No | No |
| Vision / `python_runner` / units / text-analytics / symbolic-math Writer insert | Yes | No | No |
| `table_delete`, `table_manage_structure` (row/column delete) | Yes (single-cell and multi-cell deletes are anchored as one agent change; both tracked and untracked row/column deletes refuse protected cells; row deletes refuse a row where a non-empty cell spanning multiple rows starts, both tracked and untracked; a tracked row delete also refuses when an empty spanning cell is the row's first or last cell, because LibreOffice would strike the rows it spans; column deletes refuse any column covered by a wider merged cell, both tracked and untracked) | No | No |
| `style_format_text` | Yes for `properties` and recordable `reset` (`Char*` via Format redline, `Para*` / `NumberingStyleName` via `ParagraphFormat` redline; headings, list items, empty paragraphs, `BreakType`, `WritingMode`, `PageDescName`, `NumberingLevel`, `CharAutoStyleName`, `CharInteropGrabBag`, and `Char*` border complex-colour resets are not tracked and report `style_unreviewed`) | No | No |
| `apply_style` (paragraphs) | Yes while the document is recording and in Record/Wait (`ParagraphFormat` redline via `makeRedline`, authored `WriterAgent` in Record/Wait or office user when Off) | No | No |
| `apply_style` (character) | Yes in Record/Wait or while the document is recording (Format redline on non-collapsed targets) | No | No |
| `image_insert`, `image_generate`, `image_delete`, `shape_delete` (Writer `TextGraphicObject`), OCR `delete_image_after_insert` | Yes (both while the document is recording and in Record/Wait via `run_writer_mutation_with_optional_review`; embedded `image_insert` and `AS_CHARACTER` frames while recording use `insert_recorded_content` with frame content unrecorded; `image_insert target=header/footer` flags `style_unreviewed` when it turns the region on or changes `auto_height`; `image_generate` inserts and tracked deletes return `pending_review`) | No | No |
| `chart_upsert` (Writer), `frame_insert` (`AS_CHARACTER`), `embedded_insert` (Writer), `field_insert`, `index_add_mark`, `mail_merge_insert_field`, `form_create`, `form_create_control`, `form_generate` (Writer), math formula insert | Yes while recording and in Record/Wait (for `frame_insert`, inline records in Record/Wait and its search-match deletion is recorded for every anchor type; `insert_recorded_content` wraps inline objects and Input fields in recorded placeholders; `field_insert`, `index_add_mark` and `mail_merge_insert_field` return `tracked: true` and `pending_review: true` when LibreOffice recorded the insert, the same as `frame_insert` inline and `embedded_insert`) | No | No |
| Style tools and page layout (`page_set_style_properties`, `page_set_header_footer_text`, `page_replace_header_footer_text`, `page_set_columns`, `page_set_watermark`) | No (`style_unreviewed: true`, also when the user is recording with review Off — except header/footer text-only writes, character `apply_style`, and paragraph `apply_style`; turning a header/footer on or changing `auto_height` is flagged; `page_insert_break` is not in this set) | No | No |
| `section_create` | No (inserted untracked so `SwDoc::InsertSwSection` does not mark the wrapped text as a tracked insertion; flags `style_unreviewed: true`; refuses with `SECTION_IN_TRACKED_CHANGE` when a pending Insert or Delete redline spans past both ends) | No | No |
| `frame_delete`, `chart_delete`, `shape_delete` (non-picture), `form_delete_control`, `shape_upsert` (create), `shape_connect`, `diagram_create`, `frame_insert` (non-inline; with `target="search"`, also returns `pending_review: true` when the replaced text is a pending tracked deletion), `image_replace` / `image_generate` (in-place replace), `index_create`, `index_update_all` | No (`tracked: false` with a `warning` when recording or review is on; `index_create` refuses with `INDEX_IN_TRACKED_CHANGE` if it would split a pending tracked change, and while the document is recording or in Record/Wait its `target='search'` deletion is recorded — tagged as an agent change only in Record/Wait — and the result carries `pending_review: true` while the index itself is not tracked; `index_update_all` refuses with `INDEX_HAS_TRACKED_CHANGES` if any index holds pending insertions or deletions) | No | No |
| Other Writer tools (`index_delete_toc_entry` is recorded; comments, …) | No | No | No |

Extend / Edit Selection stop when the selection contains, or lies wholly inside, a tracked insertion or deletion.
`WriterStreamedRewriteSession` and `WriterStreamedAppendSession` raise before the undo context,
before `RecordChanges` changes, and before the first `setString`, and the menu shows a message.
The redlines stay as they are. A selection with no Insert/Delete redlines keeps the rewrite.
Edit selection clears the range while tokens stream. If the model returns no text,
`WriterStreamedRewriteSession.finish()` writes the original back and does not record an empty
replacement. Whitespace the model actually sent is kept.

Extend/Edit read `extend_selection_system_prompt` / `edit_selection_system_prompt` from `writeragent.json`; `additional_instructions` stays the chat prompt (the shared Edit dialog still saves its Extra instructions there for Calc and Settings, but Writer does not use it as its system prompt).

`table_delete` cannot use `removeTextContent` under tracking: Writer records nothing for it (nor for
removing every row), so the table would vanish unreviewable. It places the caret in a non-nested
cell, confirms via the view cursor that it landed in the target table (refusing with
`TABLE_DELETE_NOT_TRACKABLE` if the table is hidden so it cannot strike another table), and
dispatches `.uno:DeleteTable`, which Writer records as a tracked deletion inside an
`UndoStepOrRollback` guard. `EditReviewSession.record_mutation` anchors multi-cell `table_delete` and row/column deletes in the
first cell that changes, and `inline_review` resolves them one cell at a time in a single compound
undo step. Writer anchors each **empty** row with a U+200D but records no redline for it, so the
tool deletes those anchors with tracking on — without that, Accept All left the empty rows behind as
a table. Rejecting keeps the anchor in the empty cell, as LibreOffice's own Delete Table does.
`apply_content` uses that same delete when an empty replacement is the last text left in the table,
inside the edit's existing review session. Clearing one cell while another cell still has text stays
a text edit.

Blocking wait applies only to **`apply_content`** from a **background thread** (sidebar
chat worker or MCP HTTP thread). The main thread never block-waits so the user can click accept/reject.

The edit runs on the main thread in `_do_edit`. Checks finish before `EditReviewSession` is
constructed. That object is appended to a one-element list before the target method runs, because
`MainThread.call` re-raises and drops the return value; the list is how anchor bookmarks are released
when the edit raises. Wait mode keeps the returned session and `wait_for_review` cleans it up. Every
other path releases it, including a successful non-wait edit.

## How a change is tracked

`plugin/writer/review_scan.py` owns fail-closed `getRedlines()` enumeration and `wa-review:` token helpers. `plugin/writer/edit_review.py` owns the session via `EditReviewSession`:

* `record_mutation()` snapshots the document's redline identifiers, runs the edit, and tags every
  **new** redline's `RedlineComment` with a per-change token `wa-review:<session>:<n>`. Only an exact
  `wa-review:<session>:<n>` comment (no whitespace, no extra colons, numeric `<n>`) counts as an
  agent token; a user comment that merely starts with `wa-review:` does not. `<n>` is the session's
  per-attempt counter, not the number of registered changes, so a tag left behind on an unregistered
  edit cannot be the token of the next registered change.
* Tagging is **fail-closed**: if the pre- or post-edit redline scan is incomplete, the edit still
  applies but its redlines are **not** tagged — they stay untagged (treated as the user's own) so
  Accept/Reject All never touches a misclassified user redline; an unreadable `RedlineDateTime` or
  `RedlineSuccessorData` also makes tagging refuse (fail closed).
* Completion is a reliable empty intersection of redline comments with tokens on **registered**
  changes — **not** "zero redlines in the document", and **not** "every `wa-review:<session>:`
  comment is gone". A tag left behind when the change was not registered (tagging failed, or the
  missing-bookmark clear left the token) does not keep `wait_for_review` blocked. `<n>` is the
  session's per-attempt counter, not the number of registered changes, so a tag left behind on an
  unregistered edit cannot be the token of the next registered change. An incomplete
  scan still fail-closes. The user's own pre-existing redlines never block or confuse it.
* Each change is anchored with a `wa_review_<session>_<n>` bookmark spanning the **change's own
  redline span** (not the whole paragraph; for a change spanning several texts such as table cells,
  the first text that changes), so several changes in one paragraph each get a correct
  outcome and `final_text` preview. The anchor bookmark lives inside the edit's undo step without
  locking the undo manager, so one Undo removes both the edit and the bookmark together. Before
  dispatching Accept or Reject, any leftover `wa_review_*` bookmarks are removed under undo lock so
  undoing the reject never encounters unregistered bookmark indexes.
* Tracking is forced on for the duration even if the user didn't have it on, and their prior
  `RecordChanges` state is restored afterward. Markup is forced **visible** (`ShowChanges = True`)
  so a reviewable change is never left invisible.
* Insertions are authored `WriterAgent` and deletions `WriterAgent (deletions)`, so LibreOffice's
  by-author coloring renders new vs removed text in two distinct colors.
* Format redlines recorded by `style_format_text` and character `apply_style` in record/wait mode
  when the document is recording during the call are authored `WriterAgent` so formatting the
  agent's own pending insertion folds into it and keeps it rejectable (`PreAppendFormatRedline` in
  `DocumentRedlineManager.cxx`). When the target overlaps a pending insertion by another author, the
  office author stays so rejecting does not keep the other author's text. `ParagraphFormat` redlines
  recorded by `style_format_text` and paragraph `apply_style` are authored `WriterAgent` in
  record/wait mode (they never fold into an insertion).
* Secondary link-target updates after a heading rename (`EditReviewSession.tag_side_effects`,
  including `target='selection'` and streamed Extend/Edit Selection) are
  recorded as tracked Delete + Insert of the link text, tagged with an agent token so Accept/Reject
  resolves them, and omitted from `ChangeRecord` so `wait_for_review` neither waits for them nor
  reports them in `review.changes`. Both the link deletion and insertion are authored
  `WriterAgent` (not `WriterAgent (deletions)`). When the user records changes with review off,
  the link redlines are still authored `WriterAgent` so they do not merge with the user's own
  redlines across a paragraph mark.

### Surgical word-level edits

Small edits in long paragraphs use **surgical** tracked changes instead of one whole-block redline
([`plugin/writer/word_diff_split.py`](../../plugin/writer/word_diff_split.py); the split decision is
made in `record_preserve_replace` in
[`plugin/writer/edit_review.py`](../../plugin/writer/edit_review.py), called from
[`plugin/writer/apply_document_content.py`](../../plugin/writer/apply_document_content.py)):

* A word-level diff splits the replace into several tight Delete+Insert pairs — one reviewable
  change per sub-edit — when the changed-word fraction is below a threshold (default **0.6**).
  Identical `old` and `new` produce no sub-edits and no redline, including when the selection is longer than the 4000-word cap. That cap applies to the whole word list only when it fits; when it does not, the common word prefix and suffix are removed first, and only a middle that is still over 4000 words becomes one whole-block edit.
* Pre-flight offset checks and right-to-left apply avoid UNO position drift; grouped undo context
  rolls back on failure.
* HTML/import paths use atomic delete-then-import so review mode never leaves a bare deletion
  stranded mid-import. A single-paragraph plain replace (`replace_preserving_format`) inserts the new
  text after the longest run so the new text inherits that run's formatting, and carries the common
  link only when the whole range shares one URL. Whole-paragraph block replaces
  (whether Track Changes is recording or not)
  are owned by `_replace_whole_paragraphs`, which while recording deletes across the following
  paragraph break, imports new blocks into a fresh paragraph before the struck `old¶` (resetting the fresh paragraph's copied direct paragraph and character formatting untracked, leaving
  new blocks first, then the struck paragraph while pending), and drops the synthetic leftover
  paragraph untracked. Where no following break exists (document end, before a table, section
  edge), one trailing empty paragraph remains after Accept. In `full_document` replace, any
  synthetic leftover paragraph opened before the import is also dropped untracked (`RecordChanges`
  temporarily off) via `_drop_leftover_paragraph` — merging forward after a trailing table — so
  the paragraph break is not a tracked delete, keeping separate paragraphs intact without word
  concatenation. Before a leading table, `_insert_mixed_or_plain_html` is the single owner that
  disposes the empty split-node paragraph untracked via `_drop_empty_paragraph_before_table`
  across all body write paths, while `insert_html_after_paragraph` owns only the trailing
  leftover. A change starting at a table has no text range for per-change inline resolution and
  resolves through Accept/Reject All or Manage. `TABLE_INSERT_FAILED` checks run inside the edit's
  undo group, so a failed table insert rolls the edit back. `full_document` refuses any hidden
  or conditional section (`HIDDEN_SECTION`) or protected non-index section (`PROTECTED_SECTION`) outside headers
  and footers and the tables and frames in them, and places new content outside a leading section
  or TOC (while recording, a paragraph is added untracked before a leading section and after a
  trailing one so Accept removes those sections whole; Reject leaves those empty paragraphs). `selection`
  refuses a range starting in a table and ending outside it (`SELECTION_SPANS_TABLE`), a non-text
  selection, or a view with no caret such as Print Preview (`SELECTION_NOT_TEXT`).

Optional internal configuration keys (set in `writeragent.json`, no Settings UI; read on every replace, out-of-range values are repaired to the default on load):

| Key | Default | Meaning |
|-----|---------|---------|
| `doc.agent_edit_diff_threshold` | `0.6` | Max changed-word fraction (0–1) before falling back to whole-block replace |
| `doc.agent_edit_max_surgical_runs` | `40` | Cap on sub-edits per replace |
| `doc.agent_edit_split_author_colors` | `true` | Set `false` to disable split insert/delete authors on whole-block edits |

## Outcomes

After review (or timeout) each change reports one of:

| `outcome` | Meaning |
|-----------|---------|
| `accepted` | The anchored text now matches the proposed form. |
| `rejected` | The anchored text now matches the original form. |
| `modified` | The user edited the area during review — the agent must not assume either text survived. |
| `pending` | Still unresolved at timeout (`complete: false`), or the pending scan was unreliable. |

Each change entry also includes `final_text` (preview of the anchored region after review).

A tracked change disappears on **both** accept and reject, so the outcome is derived by comparing
the anchored region against the pre-computed accept-form and reject-form, not by reading the
redline afterward.

If a portion read fails while building that accept-form or reject-form, the accumulated skipped prefix is used (without falling back to the raw string which could glue deleted text).

## Tool response shape

When blocking wait is active, `apply_content` (chat sidebar and MCP on a worker thread)
returns this schema:

```json
{
  "status": "ok",
  "review": {
    "complete": true,
    "timed_out": false,
    "changes": [
      { "id": "wa-review:ab12cd34:0", "outcome": "accepted",
        "original_preview": "…", "proposed_preview": "…", "final_text": "…" }
    ]
  }
}
```

On timeout: `"complete": false, "timed_out": true`, pending entries report `outcome: "pending"`,
and the tool message asks the agent to have the user finish reviewing before continuing.

Outside a blocking wait, `apply_content` adds `review_mode`, `pending_review: true`, and the
do-not-resolve note only when the call left a tracked change (`EditReviewSession.left_tracked_changes`).
A change counts even when it is untagged — for example, a deletion stacked on the user's pending
insertion. A replacement equal to the match gets no review fields. Text that LibreOffice merges
into an earlier pending agent insertion also leaves no new key, and the call that made that
insertion already reported it. In Wait mode, a call that
registered no change returns at once and carries the same fields only if it left an untagged redline.

## MCP vs sidebar

Both shells run the same `EditReviewSession.wait_for_review()`:

* **MCP** marks the mutating tool `long_running` so the call blocks on the HTTP thread (one
  request, one response — the response comes back after review). The per-document mutation gate
  stays held while waiting, so a concurrent mutating call on the same document queues behind it.
* **Sidebar** sets `is_async()` on the mutating tool so the chat worker thread blocks without
  freezing the drain loop; a `status_callback` shows "Review the agent's changes…".

In both, every document read/cleanup during the wait is marshalled to the main thread, which stays
free for the user's accept/reject clicks.

### Toggling review on/off mid-run

The chat loop snapshots the async-tool set once per round and MCP reads `long_running` per call, so
`doc.agent_edit_review_mode` can change between that decision and execution. This is handled
without error: a mutating tool already dispatched to a worker thread keeps `is_async() == True`
there (so the main-thread guard doesn't reject it) and `execute()` marshals the now-wait-free edit
to the main thread. Turning review **on** mid-round simply means the in-flight edit runs without a
wait that round.

## Inline review UI

Reviewing through *Edit ▸ Track Changes ▸ Manage* is heavy, and the native UI exposes a replace's
Delete and Insert as two independent rows. WriterAgent adds:

1. **Inline resolve** — [`plugin/writer/inline_review.py`](../../plugin/writer/inline_review.py),
   wired by [`change_context_menu.py`](../../plugin/writer/change_context_menu.py) /
   [`review_click_popup.py`](../../plugin/writer/review_click_popup.py): left-click popup, right-click
   menu, accept/reject-all. The context menu interceptor is registered once per view (UNO identity, not Python `==`) and removed on `OnUnload`, so closed documents do not leak. Scratch documents are skipped (view registration is deferred via `run_after_scratch_loads` so `is_scratch_document` sees the registered `RuntimeUID`).
2. **Review toolbar** — [`plugin/writer/review_view_attachment.py`](../../plugin/writer/review_view_attachment.py)
   (`refresh_review_toolbar`, dock-once): **WriterAgent Review** toolbar (◀ Prev, Next ▶, Accept all, Reject all) appears while pending
   agent changes exist; hidden when the count drops to zero. Toolbar visibility uses
   `agent_change_presence(model, stop_at_first=True)`: if the scan is unreliable, visibility is left
   unchanged. Menu actions `review_prev` / `review_next` call `goto_adjacent_agent_change`. Counting
   follows the count-without-previews rule: `agent_change_presence` returns this distinct-comment
   count without reading text previews with `XTextCursor.getString()`. The toolbar's per-document
   modify listener (`review_view_attachment.schedule_toolbar_refresh` → `refresh_review_toolbar`) queues recount per burst with `main.post`, which runs on a later main-loop
   turn so it does not walk redlines inside the modify notification.

It is strictly token-scoped — only `wa-review` changes are listed or resolved.

### Exact-bounds resolve (modern LibreOffice)

On LibreOffice **≥ 25.04**, selecting a change's **exact** Delete+Insert bounds and dispatching
`.uno:AcceptTrackedChange` / `.uno:RejectTrackedChange` resolves **only that change**. Several
agent changes in the same paragraph can be accepted/rejected individually. Pre-dispatch overlap
checks refuse when a **user** redline or another **agent** change overlaps the exact span;
post-dispatch safety requires that no foreign `redline_identity_key` count dropped (the same rule
as `tracking_manage`).

Single-change resolve returns an honest `ResolveStatus`:
* `RESOLVED`: Success message ("Change accepted." / "Change rejected.").
* `REFUSED_SIBLING`: Attempts to resolve overlapping agent changes together as a group.
* `REFUSED_FOREIGN`, `NOT_ELIGIBLE`: Refusal hint directing to *Edit ▸ Track Changes ▸ Manage*.
* `NO_OP`: Reports that nothing was changed ("Nothing was changed. Resolve remaining changes from Edit > Track Changes > Manage.").
* `UNRELIABLE`: Reports unreadability and that nothing changed.
* `PARTIAL`: Reported with `ok=False` ("Started resolving changes but couldn't confirm the result -- some may have been resolved. Please review remaining changes in Edit > Track Changes > Manage."). Only returned after a dispatch was attempted and follow-up scan could not confirm the result. The document is left in its partially resolved state rather than attempting undo.

When the only thing in the way is other **agent** changes (e.g. the agent deleted a word from a
sentence it had inserted earlier, stacking a Delete on its own Insert), the click resolves that
whole overlapping group together instead of refusing (`_resolve_overlapping_agent_changes`). A user
redline in the group still refuses. Reject undoes a stacked Delete one layer per dispatch, so the
dispatch repeats over what is left of the group while each pass makes progress; success is claimed
only when exactly the group is gone. Multi-dispatch resolves (overlapping groups and mixed resolve-all
loops) are wrapped in a compound undo context so undo restores all resolved edits together in one step.

In bulk resolve (`resolve_all_agent_changes`), changes are attempted paragraph-wide first so clean paragraphs clear sibling agent changes in one dispatch. When a user redline in the paragraph triggers `REFUSED_FOREIGN`, bulk resolve retries with exact bounds. Overlapping user redlines -- including any user redline with only whitespace between it and the agent span -- still refuse; a gap containing any other character still resolves on the exact retry. If an exact dispatch proves to be a no-op, further exact retries are disabled for the remainder of that run.

On older builds where exact-bounds dispatch is a no-op, inline resolve falls back to a
paragraph-wide selection (which resolves every redline in range) and refuses when foreign or
sibling changes share those paragraphs.

All safety scans are **fail-closed**: incomplete redline enumeration never backs a success claim;
resolve-all returns user-facing messages when verification fails.

### When the user records changes and agent review is Off

When the user turns on Track Changes > Record while agent review is Off, operations that Writer cannot natively record are either flagged (`style_unreviewed: true`) or left undone with a reason:

* **Character and paragraph resets (`style_format_text` `reset`):** `SwDoc::ResetAttrs` (`docfmt.cxx`) has no redline branch, so `style_format_text` writes each recordable `Char*` property's pool default (`_recordable_reset_values`, or `"Standard"` for `CharStyleName`) before resetting it; `lcl_SetRedline` (`DocumentContentOperationsManager.cxx`) records the old attributes in a Format redline, and the reset drops the temporary value. Recordable `Para*` and `NumberingStyleName` changes (set or reset) record a `ParagraphFormat` redline via `makeRedline` after the character write; headings, list items, empty paragraphs, `BreakType`, `WritingMode`, `PageDescName`, `NumberingLevel`, `CharAutoStyleName`, `CharInteropGrabBag`, and `Char*` border complex-colour resets cannot be recorded this way and set `style_unreviewed`.
* **Picture deletes (`image_delete`, OCR `delete_image_after_insert`):** `SwXFrame::dispose` leaves no redline, so `delete_graphic` selects a `TextGraphicObject` and dispatches `.uno:Delete` (`SwWrtShell::DelRight`, Graphic case). Pictures Writer cannot record (drawing-shape `GraphicObjectShape`s, `ContentProtected` pictures, or documents with no view) fail closed: `image_delete` raises `IMAGE_DELETE_NOT_TRACKABLE` and OCR keeps the image (`"image_kept"`, listed in `kept_images_delete_not_trackable` on the agent result). Rejecting a non-as-character picture delete leaves a U+200D anchor pair (`delete.cxx`) where Writer moved the anchor to the character.
* **Paragraph `apply_style` (`SwUnoCursorHelper::makeRedline`):** Applying a paragraph style while `RecordChanges` is on or in Record/Wait temporarily turns recording off so the `Char*` restore does not create `Format` redlines whose Reject drops the run's formatting, then records one `ParagraphFormat` redline per changed non-empty paragraph (authored `WriterAgent` in Record/Wait, or the current office user when review is Off) carrying `ParaStyleName`, direct paragraph properties, and `NumberingRules`. If the paragraph already has a `ParagraphFormat` redline, no second one is added so Reject restores the state before both changes.
* **Known paths that still bypass recording:** `frame_delete`, `chart_delete`, `shape_delete` (non-picture; a group member's delete also has no undo step and always warns), `form_delete_control`, `shape_upsert` (create), `shape_connect`, `diagram_create`, non-inline `frame_insert`, picture replace (`image_replace` and in-place `image_generate`), and `index_update_all` (these return `tracked: false` with a `warning` when recording or review is on; `index_update_all` refuses with `INDEX_HAS_TRACKED_CHANGES` when any index holds pending insertions or deletions; `index_delete_toc_entry` is recorded), picture properties, table formatting/merge/split, page break, hyperlink on existing text, and `_finish_collapse` failure fallback (`WriterStreamedAppendSession.abort_and_restore` now collapses the partial continuation via `finish()`).

### Limitations

* **Most style and page layout changes are not reviewable.** `style_update`, `style_create`, `style_clone`,
  `style_import`, `section_*`, `page_set_style_properties`, `page_set_header_footer_text`,
  `page_replace_header_footer_text`, `page_set_columns`, and `page_set_watermark` apply directly and return
  `style_unreviewed: true` when review recording is active (and, except for header/footer text and character `apply_style`, when the user has Track Changes > Record on with review Off); the agent is prompted to tell the user which change they cannot accept/reject like a text edit (they don't produce redlines). `page_insert_break` is not in that set. `section_create` inserts untracked so Reject does not delete the wrapped text, and refuses with `SECTION_IN_TRACKED_CHANGE` when a pending Insert or Delete redline spans past both ends.
* **Paragraph `apply_style` tracking limits:**
  * Cleared character formatting (`clear_direct="style_props"` or `"all"`) is not restored on Reject and sets `style_unreviewed: true`.
  * List levels, and numbers under an outline-numbered heading style, are not restored on Reject and set `style_unreviewed: true`.
  * Empty paragraphs get no `ParagraphFormat` redline (flagged only when no paragraph in the target was recorded).
  * Undo leaves the `ParagraphFormat` redline in place (matching LibreOffice's UI). Direct paragraph formatting set after such an undo is not restored on Reject, because the next change reuses the stale redline.
  * Rejecting a `Format` redline over a whole paragraph drops that paragraph's direct formatting (`RejectRedline` → `ResetAttrs`). A whole-paragraph character change recorded *after* a `ParagraphFormat` redline therefore loses the restored margins on Reject All; `apply_style` and `style_format_text` detect this redline order and set `style_unreviewed: true` with a warning (this can over-report paragraphs that had no direct formatting, because revert properties are not readable through UNO).
  * ODT save drops `ParagraphFormat` redlines while DOCX keeps them.
  * Typing into a paragraph while recording accepts its `ParagraphFormat` redline.
  * A second style change on the same paragraph reuses the first redline and its author, so Reject undoes both changes.
  * Reject restores the direct paragraph properties listed in `_REVERT_PARA_PROPERTIES` (`styles.py`). Any other hand-set one (for example `DropCapCharStyleName`, `FillBitmapSizeX/Y`) is dropped by `lcl_RejectRedline`'s `ResetAttrs`; the result sets `style_unreviewed: true` and the warning names it. Import metadata (`ParaInteropGrabBag`, `ParRsid`, foreign XML attributes) is dropped without a warning.
* **Recorded inserts at an empty paragraph:** When recording is on, inserting at a caret in an empty paragraph or at `beginning` when the first paragraph is empty places the imported content after that empty paragraph (so Reject restores the paragraph's style and direct formatting); content ending in a table keeps one extra empty paragraph after the table. The whole insert is one Ctrl+Z step.
* **Undo of a recorded replace:** While recording, Undo (not Reject) of a `full_document` or whole-paragraph replace loses direct character formatting when whole-paragraph direct formatting is combined with a character style at the paragraph start, because `SwUndoSplitNode::UndoImpl` restores only text hints; the alternatives tried lose other formatting on Undo (a first-word-bold paragraph comes back all bold). Reject restores it.
* **Extend selection** records only the appended continuation as a single tracked **insertion**
  (the original is never struck); it streams live with tracking off, then collapses to one redline
  at the end. Streamed collapse may show both marks under the INSERT author instead of split
  insert/delete colors.
* **`ShowChanges` is forced on** during review and is not restored afterward; the user must toggle
  display back manually if they had it off. Do not treat that as a missing restore bug in the wait loop.
* **Formatting across a foreign pending insertion** (or across both the user's and the agent's) still
  lets LibreOffice turn one of them into a Format redline; rejecting then keeps that text.
* **Closing the document during wait** aborts with ``complete=False`` and ``timed_out=False`` (not a
  full-timeout spin). A change that never gets an anchor bookmark is **not** registered as a
  reviewable agent edit.
* **Toolbar fast-travel** skips changes whose bounds cannot be read (`_change_bounds` fails); the
  pending counter may still include them. Stops with unreadable bounds are omitted from Next/Previous;
  a change with bounds in another text object is visited in redline-table order.
* **Second window, same document:** toolbar visibility is per-window; a rare stale toolbar can appear
  in a secondary window (documented in `review_view_attachment.refresh_review_toolbar`).
* **`frame_insert target="search"` whole-body match (anchor 0 or 4):** when a recorded match spans
  an entire text (body, cell, or frame), Accept also removes the frame (`undobj.cxx`:
  `IsSelectFrameAnchoredAtPara` / `IsDestroyFrameAnchoredAtChar`).
* **Undo then Redo of a review-session edit:** Redo brings the redline back untagged, splits an
  `Unknown Author` Insert piece, and restores the `wa_review_*` anchor bookmark (`RedlineComment`
  is not an undoable action, while the bookmark lives inside the edit's undo group).

## Ways to improve

Follow-ups and polish, roughly by priority.

### Wait and recording coverage

If "Wait for Edit Review" should mean the agent pauses until the user resolves **every** agent
edit, not just sidebar/MCP `apply_content`:

* Wire `EditReviewSession.wait_for_review()` (or equivalent) into extend/edit selection, vision
  insert, `python_runner` Writer insert, and the units / text-analytics / symbolic-math Writer
  inserts — today those paths record but return immediately.
* Decide whether other mutation tools (comments, images, page ops, clone heading, …) should go
  through `record_mutation` or stay direct with explicit flags like styles. `table_delete` already
  goes through it (`run_writer_mutation_with_optional_review`).

If the product intent stays scoped to `apply_content`, keep settings and docs aligned
with that (as in **Current scope** above).

### Streamed selection polish

`WriterStreamedRewriteSession` / `WriterStreamedAppendSession` ([`edit_review.py`](../../plugin/writer/edit_review.py))
collapse to one redline but do not use `deletion_author()` on the delete half of a rewrite. Match
the `html_import.py` split-author pattern (`_deletion_author`) so by-author coloring distinguishes insertions and deletions.

### Inline review polish

* **Post-dispatch verification failure:** exact-bounds resolve may return failure after the
  document has already changed; consider user messaging or undo recovery.
* **Toolbar navigation:** include changes with unreadable bounds (paragraph-level fallback).
* **Click popup:** the popup opens only for a plain click that leaves a collapsed caret (drags,
  Shift-click and word-select do not open it), and the click never changes the selection, so
  dismissing it cannot leave the agent change selected.

### Review session behavior

* Restore the user's prior **`ShowChanges`** after `EditReviewSession` / streamed session cleanup, or document permanently forcing markup visible as intentional.

### Tests

* Add an end-to-end UNO test: `apply_content` on a **background thread** with
  mode **wait**, user accepts/rejects, tool returns a `review` payload with outcomes.
* Cover `edit_review_timeout: 0` with mode **wait** (immediate return with pending changes).

### Release hygiene

* Run **`make extract-strings`** for new `_()` strings in the review toolbar and context menu
  ([`locales/writeragent.pot`](../../locales/writeragent.pot)).
