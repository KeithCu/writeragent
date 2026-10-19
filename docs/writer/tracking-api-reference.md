# Writer Tracking API Reference

## Introduction
The LibreOffice Writer Track Changes functionality (also known internally as "Redlines") allows users to record, review, accept, and reject modifications made to a document by various authors over time.

This document details the relevant UNO services, interfaces, properties, and Dispatch commands that WriterAgent uses to manage track changes.

## Document Properties

The `com.sun.star.text.TextDocument` service (and its underlying implementation `GenericTextDocument`) provides several important properties related to Track Changes.

### RecordChanges
- **Type:** `boolean`
- **Description:** Start or stop recording changes in the document. When `True`, any insertions, deletions, or formatting changes are tracked as Redlines.

### RedlineDisplayType and ShowChanges

Document-level properties controlling how tracked changes markup is rendered in the document.

| Value | Name | What stuck | `ShowChanges` read back |
|---|---|---|---|
| 0 `NONE` | cannot be stored | Python `0` and `uno.Any("short", 0)` both store as **1** | `False` |
| 1 `INSERTED` | `IsHideChanges` (insertions as normal text, deletions hidden) | sticks | `False` |
| 2 `INSERTED_AND_REMOVED` | `IsShowChanges` | sticks | `True` |
| 3 `REMOVED` | `IsShowOriginal` (show original, hide insertions) | sticks | `False` |

WriterAgent show/hide writes `2` (`INSERTED_AND_REMOVED` / `IsShowChanges`) and `1` (`INSERTED` / `IsHideChanges`). Note that `0` cannot be stored on LO 26.8.1.1 (it stores as `1`), and `3` is show-original (`REMOVED` / `IsShowOriginal`).

On LibreOffice 26.8.1.1, the boolean `ShowChanges` property setter is one-way: from type 1, setting `ShowChanges = True` moves to type 2 and `ShowChanges` reads `True`. Setting `ShowChanges = False` while type is 2 does nothing (stays `True` and type 2). Therefore, WriterAgent sets `RedlineDisplayType` directly.

### ShowChangesInMargin
- **Type:** `boolean` (on `controller.getViewSettings()`)
- **Description:** The view's "deletions in the margin" preference. The tool does not write this property.

## Redlines Collection (`XRedlinesSupplier`)

To access the changes made to a document, the document model provides the `com.sun.star.document.XRedlinesSupplier` interface.

### Methods
- `getRedlines()`: Returns a `com.sun.star.container.XEnumerationAccess` (or similar collection) containing all the redlines (tracked changes) in the document.

## Individual Redlines (`XRedline`)

When enumerating the collection returned by `getRedlines()`, each element represents an individual tracked change. A Redline typically exposes several properties:

### Key Properties
- `RedlineType` (string): The type of change, typically `"Insert"`, `"Delete"`, `"Format"`, or `"Move"`.
- `RedlineAuthor` (string): The name of the user who made the change.
- `RedlineDateTime` (`com.sun.star.util.DateTime`): The timestamp when the change was made (contains Year, Month, Day, Hours, Minutes, etc.).
- `RedlineComment` (string): An optional comment attached to the tracked change.
- `RedlineIdentifier` (string): An internal identifier for the redline. It is not a stable identity by itself. `manage_tracked_changes` treats a resolve as successful only when `redline_identity_key` for that index drops by one and every other key stays. A stacked Delete reuses the identifier (`review_scan.py`).

## Comments / Annotations

In LibreOffice Writer, "Comments" are internally referred to as `Annotation` text fields. They are a different UNO subsystem from redlines. WriterAgent exposes them as core `add_comment` plus the comments domain (`comment_list`, `comment_delete`, `comment_resolve`, workflow tools) — not as `track_changes_*` tools.

### Inserting an Annotation
1. Instantiate the `com.sun.star.text.textfield.Annotation` service via `doc.createInstance()`.
2. Set properties:
   - `Author` (string): The author's name.
   - `Content` (string): The comment text.
   - `Date` (com.sun.star.util.Date): The date the comment was made.
3. Attach the field to a `TextRange` (e.g., a search match, not only the view cursor) using `insertTextContent` on that range. `add_comment` spans the matched passage and can reply via `parent_name`.

### Managing Annotations
- **Listing:** Annotations are part of the document's text fields. You can retrieve them via `doc.getTextFields()`. Iterate through the enumeration, and if `field.supportsService("com.sun.star.text.textfield.Annotation")`, it is a comment. Agent-facing list is `comment_list` (name + author + resolved), not an index from tracking. `comment_list` reports `location` (body, table cell, header/footer) via `describe_match_location`; `paragraph_index` is present only when the comment anchor belongs to the main body text (`uno_same(anchor.getText(), doc.getText())`).
- **Resolving:** `comment_resolve` resolves an annotation and optionally inserts a threaded reply. Resolve replies are assigned a unique, non-empty `Name` prior to insert so they appear in subsequent `comment_list` calls with their own addressable name.
- **Deleting:** Call `removeTextContent` on the text field, which disposes one field (`SwXTextField::dispose`). Agent-facing `comment_delete` by `name` removes that annotation and every reply whose `ParentName` chain reaches it. Delete by `author` removes annotations with that author only.

## Dispatch Commands (.uno:)

Because the standard UNO `XRedline` objects in Python don't consistently expose direct `accept()` and `reject()` methods that are easily callable without complex text cursor manipulation, LibreOffice's Dispatch API is the most robust way to accept and reject changes, as well as toggle their visibility.

To use these, obtain a `com.sun.star.frame.DispatchHelper` and execute the command against the document's current frame (`doc.getCurrentController().getFrame()`).

### Available Commands
- `.uno:AcceptAllTrackedChanges`: Accepts all tracked changes in the document at once. On a document with `getRedlines`, success is `getCount() == 0` (or the count was already 0). The dispatch `any` is ignored. On a document without `getRedlines`, a `None` dispatch is still `UNSUPPORTED`.
- `.uno:RejectAllTrackedChanges`: Rejects all tracked changes in the document at once. On a document with `getRedlines`, success is `getCount() == 0` (or the count was already 0). The dispatch `any` is ignored. On a document without `getRedlines`, a `None` dispatch is still `UNSUPPORTED`.
- `.uno:AcceptTrackedChange`: Accepts the currently selected tracked change (requires navigating the view cursor to the change first).
- `.uno:RejectTrackedChange`: Rejects the currently selected tracked change (requires navigating the view cursor to the change first).
- `.uno:ShowTrackedChanges`: On this build (LO 26.8.1.1) the dispatch returns a `DispatchResultEvent` and does not change `ShowChanges` or `RedlineDisplayType`. The tool sets `RedlineDisplayType` directly instead of dispatching this command.


## Example Usage (Python-UNO)

### Listing Tracked Changes

```python
doc = ctx.doc
redlines = doc.getRedlines()
enum = redlines.createEnumeration()

changes = []
while enum.hasMoreElements():
    redline = enum.nextElement()
    change = {
        "author": redline.getPropertyValue("RedlineAuthor"),
        "type": redline.getPropertyValue("RedlineType"),
        # Extract DateTime fields
    }
    changes.append(change)
```

### Accepting All Changes

```python
smgr = ctx.ctx.ServiceManager
dispatcher = smgr.createInstanceWithContext(
    "com.sun.star.frame.DispatchHelper", ctx.ctx
)
frame = ctx.doc.getCurrentController().getFrame()

dispatcher.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
```

### Accepting or rejecting a single change

`.uno:AcceptTrackedChange` / `.uno:RejectTrackedChange` act on the *selected* change, so the view cursor must sit on it first. `XRedline` objects from `getRedlines()` are property sets with **no** `getAnchor()` method; select the change by its live span instead:

```python
start = redline.getPropertyValue("RedlineStart")
cursor = start.getText().createTextCursorByRange(start)
cursor.gotoRange(redline.getPropertyValue("RedlineEnd"), True)
# then dispatch .uno:AcceptTrackedChange / .uno:RejectTrackedChange
```

Note that `compareRegionStarts` throws `IllegalArgumentException` across `XText`s (a table cell, footnote, header, or frame). The tool skips those redlines during overlap checking. Same-text comparison errors still refuse.

Before `select`, the tool saves `controller.getSelection()`. After the dispatch and the identity-key check, a `finally` restores that selection, including when the tool returns an error. If the saved selection is missing, nothing is restored. A failure to read it is logged at debug and a failure to restore it is logged with `log.exception`; neither changes the tool result. If `select` of the change itself throws, the previous selection is not restored.

### What the shipped tool returns

`track_changes_list` reports, per change, the author and type plus the change **text** (for a deletion, the removed text via `RedlineText`) and its **location** (body, table cell, header/footer, and so on), along with the document's current review mode. That is enough for a model to decide what to accept or reject without a second lookup. After a single accept or reject the tool tells the caller to run `track_changes_list` again because indexes shift. `track_changes_show` takes the Writer `RedlineDisplayType` path only when the document has `getRedlines`. Calc gets the stub.
