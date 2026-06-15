# Reviewable Agent Edits

When review mode is on, every edit the agent makes lands as a **native LibreOffice tracked
change (redline)** the user can accept or reject, and the agent learns the per-change outcome.
Review is Writer-only.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `writer.require_edit_review` | `false` | Master switch. When on, agent edits are recorded as tracked changes **and** the edit call blocks until the user has reviewed them. |
| `writer.track_changes_reviewable` | `false` | Records agent edits as tracked changes without the block-and-wait. `require_edit_review` implies this. |
| `writer.edit_review_timeout` | `900` (seconds) | Max time an edit call blocks waiting for review. `0` disables waiting (record only). |

`review_recording_enabled(ctx)` is the single helper every edit path checks — it returns true
when **either** flag is set.

## How a change is tracked

`plugin/writer/edit_review.py` owns the whole story via `EditReviewSession`:

* `record_mutation()` snapshots the document's redline identifiers, runs the edit, and tags every
  **new** redline's `RedlineComment` with a per-change token `wa-review:<session>:<n>`.
* Completion is *"no redline carrying this session's token remains"* — **not** "zero redlines in
  the document" — so the user's own pre-existing redlines never block or confuse it.
* Each change is anchored with a `wa_review_<session>_<n>` bookmark spanning the affected range, so
  it survives positions shifting as other changes are resolved. Bookmarks are always removed when
  the review finishes (success, timeout, or error) — including when the edit raises part-way
  through a multi-match replace.
* Tracking is forced on for the duration even if the user didn't have it on, and their prior
  `RecordChanges` state is restored afterward. Markup is forced **visible** (`ShowChanges = True`)
  so a reviewable change is never left invisible.
* Insertions are authored `WriterAgent` and deletions `WriterAgent (deletions)`, so LibreOffice's
  by-author coloring renders new vs removed text in two distinct colors.

## Outcomes

After review (or timeout) each change reports one of:

| `outcome` | Meaning |
|-----------|---------|
| `accepted` | The anchored text now matches the proposed form. |
| `rejected` | The anchored text now matches the original form. |
| `modified` | The user edited the area during review — the agent must not assume either text survived. |
| `pending` | Still unresolved at timeout (`complete: false`). |

A tracked change disappears on **both** accept and reject, so the outcome is derived by comparing
the anchored paragraph against the pre-computed accept-form and reject-form, not by reading the
redline afterward.

## Tool response shape

Mutating tools (chat sidebar and MCP) return the same schema:

```json
{
  "status": "ok",
  "review": {
    "complete": true,
    "timed_out": false,
    "changes": [
      { "id": "wa-review:ab12cd34:0", "outcome": "accepted",
        "original_preview": "…", "proposed_preview": "…" }
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
`writer.require_edit_review` can change between that decision and execution. This is handled
without error: a mutating tool already dispatched to a worker thread keeps `is_async() == True`
there (so the main-thread guard doesn't reject it) and `execute()` marshals the now-wait-free edit
to the main thread. Turning review **on** mid-round simply means the in-flight edit runs without a
wait that round.

## Inline review UI

Reviewing through *Edit ▸ Track Changes ▸ Manage* is heavy, and the native UI exposes a replace's
Delete and Insert as two independent rows. On top of the native redlines, a small inline surface
(`plugin/writer/inline_review.py`, wired by `change_context_menu.py` / `review_click_popup.py`)
resolves a whole agent change at the cursor as one unit (the Delete+Insert pair together), via
left-click popup or right-click menu, plus accept/reject-all.

It is strictly token-scoped — only `wa-review` changes are listed.

### Limitations

* **Style changes are not reviewable.** `apply_style`, `update_style`, `create_style` and
  `import_styles` apply directly and return `style_unreviewed: true`; the agent is prompted to tell
  the user it changed a style (styles don't produce redlines).
* **Inline resolve refuses on a shared paragraph.** `.uno:Accept/RejectTrackedChange` resolves
  every redline in the selection, and the selection is widened to whole paragraphs (the dispatch
  won't fire on an exact-bounds selection). So when one of the **user's own** tracked changes or
  another agent change shares a paragraph with the selected agent change, the inline single-change
  resolve refuses rather than touch more than the requested change. Accept/reject-all can still
  resolve all agent changes when no user redlines are present; otherwise shared paragraphs are left
  for the native *Manage* dialog.
* **Extend selection** records only the appended continuation as a single tracked **insertion**
  (the original is never struck); it streams live with tracking off, then collapses to one redline
  at the end.
