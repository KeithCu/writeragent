# Reviewable Agent Edits

When review mode is on, supported agent edit paths land as **native LibreOffice tracked changes
(redlines)** the user can accept or reject; on the blocking path the agent also learns the
per-change outcome. Review is Writer-only.

See **Current scope** below for which paths record vs block today.

## Configuration

Settings live on the **Doc** tab in WriterAgent Settings ([`plugin/doc/module.yaml`](../plugin/doc/module.yaml)).

| Key | Default | Meaning |
|-----|---------|---------|
| `doc.agent_edit_review_mode` | `off` | **Off** — direct edits. **Record** — tracked changes you accept/reject yourself. **Wait** — same as Record, and `apply_document_content` blocks (on chat/MCP worker thread) until you review. |
| `doc.edit_review_timeout` | `900` (seconds) | Max time `apply_document_content` blocks when mode is **Wait**. Internal — not shown in Settings; set in `writeragent.json` if you need to change it. |

> [!NOTE]
> The two original boolean configuration flags (`writer.track_changes_reviewable` and `writer.require_edit_review`) have been consolidated into the single dropdown setting `doc.agent_edit_review_mode`.
> The timeout setting `doc.edit_review_timeout` is now an internal-only configuration value and is hidden from the Settings UI dialog.

`review_recording_enabled(ctx)` returns true when mode is **record** or **wait**.
`edit_review_wait_seconds(ctx)` returns the timeout when mode is **wait**, else `0`.

Use `get_agent_edit_review_mode(ctx)` for the raw mode string (`off` / `record` / `wait`).

## Current scope

What is implemented today (not every Writer mutation tool):

| Path | Records redlines | Blocks until reviewed | Returns `review` outcomes |
|------|------------------|----------------------|---------------------------|
| `apply_document_content` (chat/MCP worker thread) | Yes | Yes, when wait enabled | Yes |
| `apply_document_content` (main thread, e.g. debug tests) | Yes | No (UI would freeze) | No |
| Extend / edit selection (menu + sidebar) | Yes | No | No |
| Vision / `python_runner` Writer insert | Yes | No | No |
| Style tools | No (`style_unreviewed: true`) | No | No |
| Other Writer tools (comments, images, …) | No | No | No |

Blocking wait applies only to **`apply_document_content`** from a **background thread** (sidebar
chat worker or MCP HTTP thread). The main thread never block-waits so the user can click accept/reject.

## How a change is tracked

`plugin/writer/edit_review.py` owns the whole story via `EditReviewSession`:

* `record_mutation()` snapshots the document's redline identifiers, runs the edit, and tags every
  **new** redline's `RedlineComment` with a per-change token `wa-review:<session>:<n>`.
* Tagging is **fail-closed**: if the pre- or post-edit redline scan is incomplete, the edit still
  applies but its redlines are **not** tagged — they stay untagged (treated as the user's own) so
  Accept/Reject All never touches a misclassified user redline.
* Completion is *"no redline carrying this session's token remains"* — **not** "zero redlines in
  the document" — so the user's own pre-existing redlines never block or confuse it.
* Each change is anchored with a `wa_review_<session>_<n>` bookmark spanning the **change's own
  redline span** (not the whole paragraph), so several changes in one paragraph each get a correct
  outcome and `final_text` preview.
* Tracking is forced on for the duration even if the user didn't have it on, and their prior
  `RecordChanges` state is restored afterward. Markup is forced **visible** (`ShowChanges = True`)
  so a reviewable change is never left invisible.
* Insertions are authored `WriterAgent` and deletions `WriterAgent (deletions)`, so LibreOffice's
  by-author coloring renders new vs removed text in two distinct colors.

### Surgical word-level edits

Small edits in long paragraphs use **surgical** tracked changes instead of one whole-block redline
([`plugin/writer/word_diff_split.py`](../plugin/writer/word_diff_split.py), integrated in
[`plugin/writer/content.py`](../plugin/writer/content.py)):

* A word-level diff splits the replace into several tight Delete+Insert pairs — one reviewable
  change per sub-edit — when the changed-word fraction is below a threshold (default **0.6**).
* Pre-flight offset checks and right-to-left apply avoid UNO position drift; grouped undo context
  rolls back on failure.
* HTML/import paths use atomic delete-then-import so review mode never leaves a bare deletion
  stranded mid-import.

Optional environment overrides (no Settings UI):

| Variable | Default | Meaning |
|----------|---------|---------|
| `WRITERAGENT_AGENT_EDIT_DIFF_THRESHOLD` | `0.6` | Max changed-word fraction (0–1) before falling back to whole-block replace |
| `WRITERAGENT_AGENT_EDIT_MAX_SURGICAL_RUNS` | `40` | Cap on sub-edits per replace |
| `WRITERAGENT_AGENT_EDIT_SPLIT_AUTHOR_COLORS` | on | Set `0`/`false`/`off` to disable split insert/delete authors on whole-block edits |

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

## Tool response shape

When blocking wait is active, `apply_document_content` (chat sidebar and MCP on a worker thread)
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

1. **Inline resolve** — [`plugin/writer/inline_review.py`](../plugin/writer/inline_review.py),
   wired by [`change_context_menu.py`](../plugin/writer/change_context_menu.py) /
   [`review_click_popup.py`](../plugin/writer/review_click_popup.py): left-click popup, right-click
   menu, accept/reject-all.
2. **Review toolbar** — [`plugin/writer/review_toolbar.py`](../plugin/writer/review_toolbar.py):
   **WriterAgent Review** toolbar (◀ Prev, Next ▶, Accept all, Reject all) appears while pending
   agent changes exist; hidden when the count drops to zero. Menu actions `review_prev` /
   `review_next` call `goto_adjacent_agent_change`.

It is strictly token-scoped — only `wa-review` changes are listed or resolved.

### Exact-bounds resolve (modern LibreOffice)

On LibreOffice **≥ 25.04**, selecting a change's **exact** Delete+Insert bounds and dispatching
`.uno:AcceptTrackedChange` / `.uno:RejectTrackedChange` resolves **only that change**. Several
agent changes in the same paragraph can be accepted/rejected individually. Pre-dispatch overlap
checks refuse when a **user** redline or another **agent** change overlaps the exact span.

On older builds where exact-bounds dispatch is a no-op, inline resolve falls back to a
paragraph-wide selection (which resolves every redline in range) and refuses when foreign or
sibling changes share those paragraphs.

All safety scans are **fail-closed**: incomplete redline enumeration never backs a success claim;
resolve-all returns user-facing messages when verification fails.

### Limitations

* **Style changes are not reviewable.** `apply_style`, `update_style`, `create_style` and
  `import_styles` apply directly and return `style_unreviewed: true`; the agent is prompted to tell
  the user it changed a style (styles don't produce redlines).
* **Extend selection** records only the appended continuation as a single tracked **insertion**
  (the original is never struck); it streams live with tracking off, then collapses to one redline
  at the end. Streamed collapse may show both marks under the INSERT author instead of split
  insert/delete colors.
* **`ShowChanges` is forced on** during review and is not restored afterward; the user must toggle
  display back manually if they had it off.
* **Toolbar fast-travel** skips changes whose bounds cannot be read (`_change_bounds` fails); the
  pending counter may still include them.
* **Second window, same document:** toolbar visibility is per-window; a rare stale toolbar can appear
  in a secondary window (documented in `review_toolbar.py`).

## Ways to improve

Follow-ups and polish, roughly by priority.

### Wait and recording coverage

If "Wait for Edit Review" should mean the agent pauses until the user resolves **every** agent
edit, not just sidebar/MCP `apply_document_content`:

* Wire `EditReviewSession.wait_for_review()` (or equivalent) into extend/edit selection, vision
  insert, and `python_runner` Writer insert — today those paths record but return immediately.
* Decide whether other mutation tools (comments, images, page ops, clone heading, …) should go
  through `record_mutation` or stay direct with explicit flags like styles.

If the product intent stays scoped to `apply_document_content`, keep settings and docs aligned
with that (as in **Current scope** above).

### Streamed selection polish

`WriterStreamedRewriteSession` / `WriterStreamedAppendSession` ([`document_helpers.py`](../plugin/doc/document_helpers.py))
collapse to one redline but do not use `deletion_author()` on the delete half of a rewrite. Match
the `format.py` split-author pattern so by-author coloring distinguishes insertions and deletions.

### Inline review polish

* **Post-dispatch verification failure:** exact-bounds resolve may return failure after the
  document has already changed; consider user messaging or undo recovery.
* **Toolbar navigation:** include changes with unreadable bounds (paragraph-level fallback).
* **Click popup:** left-click inside every agent change opens the popup; right-click-only may be
  less noisy if users find it intrusive.

### Review session behavior

* Restore the user's prior **`ShowChanges`** after `EditReviewSession` / streamed session cleanup, or document permanently forcing markup visible as intentional.
* Refuse change registration when anchor bookmark creation fails (mirror tagging fail-closed).

### Tests

* Add an end-to-end UNO test: `apply_document_content` on a **background thread** with
  mode **wait**, user accepts/rejects, tool returns a `review` payload with outcomes.
* Cover `edit_review_timeout: 0` with mode **wait** (immediate return with pending changes).

### Release hygiene

* Run **`make extract-strings`** for new `_()` strings in the review toolbar and context menu
  ([`locales/writeragent.pot`](../locales/writeragent.pot)).
