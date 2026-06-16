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
`doc.agent_edit_review_mode` can change between that decision and execution. This is handled
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
* **Split insert/delete authors** apply on the `apply_document_content` / `format.py` path only.
  Streamed extend/edit collapse via `setString` under a single INSERT author — both redlines may
  show as `WriterAgent` instead of `WriterAgent (deletions)` for deletions.
* **`ShowChanges` is forced on** during review and is not restored afterward; the user must toggle
  display back manually if they had it off.

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

### Inline review and menu handlers

* **Document targeting:** accept/reject action handlers in [`main.py`](../plugin/main.py) use
  `get_desktop().getCurrentComponent()`. AGENTS.md prefers `frame.getController().getModel()` when
  the sidebar window and "current component" can diverge (multiple Writer windows).
* **Foreign redline detection** in [`inline_review.py`](../plugin/writer/inline_review.py) fails
  open on enumeration errors so the common case is never blocked; consider fail-closed if touching
  a user redline via paragraph-wide dispatch becomes a reported bug.
* **Context menu lifecycle:** [`change_context_menu.py`](../plugin/writer/change_context_menu.py)
  keeps strong refs to controllers/interceptors without dispose cleanup — one pair may linger per
  closed view; release on view dispose when that shows up in long sessions.
* **Click popup:** left-click inside every agent change opens the popup; right-click-only may be
  less noisy if users find it intrusive.

### Review session behavior

* Restore the user's prior **`ShowChanges`** after `EditReviewSession` / streamed session cleanup,
  or document permanently forcing markup visible as intentional.
* When review mode is on but an edit produces **no redlines** (no-op replace, tracking failure),
  `wait_for_review` can report `complete: true` with an empty `changes` list — the agent may assume
  success when nothing was reviewable; consider surfacing that explicitly in the tool response.

### Tests

* Add an end-to-end UNO test: `apply_document_content` on a **background thread** with
  mode **wait**, user accepts/rejects, tool returns a `review` payload with outcomes.
  Session-level `wait_for_review` is covered; the full tool + marshalling path is not.
* Cover `edit_review_timeout: 0` with mode **wait** (immediate return with pending changes).
* Context menu / click-popup registration is hard to UNO-test; manual checklist is fine.

### Release hygiene

* Run **`make extract-strings`** for new `_()` strings in the context menu and click popup
  ([`locales/writeragent.pot`](../locales/writeragent.pot)).
* Optional: link this doc from the AGENTS.md deep-dive index.
