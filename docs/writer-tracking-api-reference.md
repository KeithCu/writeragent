# Writer Tracking API Reference

## Introduction
The LibreOffice Writer Track Changes functionality (also known internally as "Redlines") allows users to record, review, accept, and reject modifications made to a document by various authors over time.

This document details the relevant UNO services, interfaces, properties, and Dispatch commands that WriterAgent uses to manage track changes.

## Document Properties

The `com.sun.star.text.TextDocument` service (and its underlying implementation `GenericTextDocument`) provides several important properties related to Track Changes.

### RecordChanges
- **Type:** `boolean`
- **Description:** Start or stop recording changes in the document. When `True`, any insertions, deletions, or formatting changes are tracked as Redlines.

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
- `RedlineIdentifier` (string): An internal identifier for the redline.

## Dispatch Commands (.uno:)

Because the standard UNO `XRedline` objects in Python don't consistently expose direct `accept()` and `reject()` methods that are easily callable without complex text cursor manipulation, LibreOffice's Dispatch API is the most robust way to accept and reject changes, as well as toggle their visibility.

To use these, obtain a `com.sun.star.frame.DispatchHelper` and execute the command against the document's current frame (`doc.getCurrentController().getFrame()`).

### Available Commands
- `.uno:AcceptAllTrackedChanges`: Accepts all tracked changes in the document at once.
- `.uno:RejectAllTrackedChanges`: Rejects all tracked changes in the document at once.
- `.uno:AcceptTrackedChange`: Accepts the currently selected tracked change (requires navigating the view cursor to the change first).
- `.uno:RejectTrackedChange`: Rejects the currently selected tracked change (requires navigating the view cursor to the change first).
- `.uno:ShowTrackedChanges`: Toggles the visibility of the tracked changes markup in the document view.

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
