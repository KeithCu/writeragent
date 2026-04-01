# Writer Bookmarks API Reference (com.sun.star.text.Bookmark)

This document provides a comprehensive overview of the LibreOffice UNO API for interacting with bookmarks within a Writer document, along with Python code examples.

## Overview

A bookmark (`com.sun.star.text.Bookmark`) in LibreOffice Writer is a type of `TextContent` that serves as a jump target or label within the document. It does not contain text itself but rather anchors to a specific location (a point) or spans a range of text.

When inserted, a bookmark becomes part of the text flow and can be navigated to or referenced by other features (e.g., cross-references).

### Interfaces and Services

*   **Service:** `com.sun.star.text.Bookmark`
*   **Key Interfaces:**
    *   `com.sun.star.container.XNamed`: Provides `getName()` and `setName(string)` to get and set the programmatic name of the bookmark. The name must be unique within the document.
    *   `com.sun.star.text.XTextContent`: Inherited interface. Provides `getAnchor()`, which returns an `XTextRange` representing the location where the bookmark is inserted.
    *   `com.sun.star.text.XBookmarksSupplier`: Implemented by the text document (`TextDocument`), providing `getBookmarks()`.
    *   `com.sun.star.container.XNameAccess`: Returned by `getBookmarks()`, providing `getByName(name)` and `getElementNames()`.

## API Usage Examples in Python

### 1. Retrieving All Bookmarks

To get a list of all bookmarks in a document, you query the document for its bookmarks and iterate through the names:

```python
def list_all_bookmarks(doc):
    if not hasattr(doc, "getBookmarks"):
        return []

    bookmarks = doc.getBookmarks()
    names = bookmarks.getElementNames()

    results = []
    for name in names:
        bm = bookmarks.getByName(name)
        anchor = bm.getAnchor()
        # The anchor text is what the bookmark spans, or empty if it's a point.
        text_content = anchor.getString()
        results.append({
            "name": name,
            "text": text_content
        })
    return results
```

### 2. Creating a New Bookmark

To create a new bookmark, you instantiate the service, set its name, and insert it into the document at a specific `XTextRange` (e.g., from a cursor).

```python
def create_bookmark_at_cursor(doc, cursor, bookmark_name):
    # Instantiate the bookmark service
    bookmark = doc.createInstance("com.sun.star.text.Bookmark")

    # Set its unique name
    bookmark.Name = bookmark_name

    # Get the text interface where the cursor is currently located
    text = cursor.getText()

    # Insert the bookmark at the cursor's range.
    # If the cursor spans text, passing True will make the bookmark span that text.
    # Passing False will collapse the bookmark to the start/end of the range.
    text.insertTextContent(cursor, bookmark, True)

    return bookmark
```

### 3. Deleting a Bookmark

To delete a bookmark, you must retrieve it from the bookmarks collection and then ask its parent text container to remove it.

```python
def delete_bookmark_by_name(doc, bookmark_name):
    bookmarks = doc.getBookmarks()

    if bookmarks.hasByName(bookmark_name):
        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()
        text = anchor.getText()
        text.removeTextContent(bm)
        return True

    return False
```

### 4. Renaming a Bookmark

Renaming is straightforward using the `Name` property (or `setName` method).

```python
def rename_bookmark(doc, old_name, new_name):
    bookmarks = doc.getBookmarks()

    if bookmarks.hasByName(old_name):
        bm = bookmarks.getByName(old_name)
        bm.Name = new_name
        return True

    return False
```

## Special Considerations

*   **Point vs. Span:** A bookmark can either be a single point (cursor collapsed) or span a range of text (cursor has a selection). When inserting with `insertTextContent(cursor, bookmark, bAbsorb)`, if `bAbsorb` is True and the cursor is a selection, the bookmark spans the selection. If `bAbsorb` is False, the bookmark is inserted as a point.
*   **Uniqueness:** Bookmark names *must* be unique within the document. Trying to rename a bookmark to a name that already exists will cause an error or overwrite. Creating a bookmark with a duplicate name might fail or throw an exception during insertion.
*   **Internal Bookmarks:** LibreOffice and external integrations (like MCP) often prefix internal bookmarks with an underscore or specific string (e.g., `_mcp_`). Tools manipulating user-facing bookmarks might need to filter these out.
*   **Table Cells:** If a bookmark needs to be inserted inside a table cell, you must obtain the `XText` interface of that specific cell (`cell.getText()` or `cell` directly if it implements `XText`), and use the cell's `insertTextContent`, not the main document text.
