# Footnotes and Endnotes API Reference

## Overview

LibreOffice provides UNO APIs to manage Footnotes and Endnotes within a `TextDocument`. Both Footnotes and Endnotes are considered text contents that can be inserted into the document at a specific `XTextRange` (anchor). They also provide an `XText` interface allowing for rich text formatting and insertion inside the note area itself.

## Core Interfaces and Services

### 1. Document Level Collections
The main document (`com.sun.star.text.TextDocument`) implements two supplier interfaces to access all notes in the document:

- **`com.sun.star.text.XFootnotesSupplier`**
  - `getFootnotes()`: Returns an `XIndexAccess` collection of all footnotes in the document.
  - `getFootnoteSettings()`: Returns an `XPropertySet` of footnote formatting settings (e.g., prefix, suffix, numbering style).

- **`com.sun.star.text.XEndnotesSupplier`**
  - `getEndnotes()`: Returns an `XIndexAccess` collection of all endnotes in the document.
  - `getEndnoteSettings()`: Returns an `XPropertySet` of endnote formatting settings.

### 2. Note Instances
A single footnote or endnote is created using the document's `XMultiServiceFactory` (`createInstance` method):

- `com.sun.star.text.Footnote` (for footnotes)
- `com.sun.star.text.Endnote` (for endnotes)

Both instances implement the following key interfaces:
- **`com.sun.star.text.XFootnote`**
  - `getLabel()`: Gets the custom anchor mark string (if any). If it's an automatically numbered note, this is usually empty.
  - `setLabel(label: str)`: Sets a custom anchor mark string for the note. If set to an empty string `""`, it reverts to automatic numbering.
- **`com.sun.star.text.XTextContent`**
  - Allows it to be attached (inserted) into the document via `doc.getText().insertTextContent(cursor, note, False)`.
  - `getAnchor()`: Returns the `XTextRange` in the document body where the note is anchored.
- **`com.sun.star.text.XText`**
  - The note object *itself* is an `XText`, meaning you can insert strings, paragraphs, or other elements inside the note's text area (at the bottom of the page or document).
  - You can use `note.setString("This is the note text.")` or create a cursor inside it via `note.createTextCursor()`.

## Workflows

### 1. Creating a Footnote/Endnote
```python
# Create the instance
note = doc.createInstance("com.sun.star.text.Footnote") # or Endnote

# Optional: set a custom label instead of auto-numbering
note.setLabel("*")

# Insert it at the current cursor position in the document
doc.getText().insertTextContent(cursor, note, False)

# Set the text inside the footnote area
note.setString("This is the text at the bottom of the page.")
```

### 2. Listing Footnotes/Endnotes
```python
footnotes = doc.getFootnotes()
for i in range(footnotes.getCount()):
    note = footnotes.getByIndex(i)
    label = note.getLabel() # "" if auto-numbered
    text = note.getString()
    print(f"Footnote {i}: [{label}] {text}")
```

### 3. Editing a Footnote/Endnote
```python
footnotes = doc.getFootnotes()
if footnotes.getCount() > 0:
    note = footnotes.getByIndex(0)
    # Update text
    note.setString("Updated note text.")
    # Update label
    note.setLabel("†")
```

### 4. Deleting a Footnote/Endnote
The `footnote_insert`, `footnote_edit`, and `footnote_delete` tools run through `record_write`, so in Record/Wait or when change tracking is on, clearing the anchor records a tracked deletion instead of removing the note immediately.
```python
footnotes = doc.getFootnotes()
if footnotes.getCount() > 0:
    note = footnotes.getByIndex(0)
    # The note object itself is anchored somewhere.
    # We can delete it by setting its anchor's string to empty, or by using removeTextContent.
    anchor = note.getAnchor()
    anchor.setString("")
    # Alternatively: anchor.getText().removeTextContent(note)
```

## Settings (FootnoteSettings / EndnoteSettings)
The `getFootnoteSettings()` and `getEndnoteSettings()` methods return an `XPropertySet` that allows managing formatting and numbering rules for the respective note type. The `EndnoteSettings` service includes the properties from `FootnoteSettings`.

Key Properties:
- `Prefix` (string): Text before the footnote symbol.
- `Suffix` (string): Text after the footnote symbol.
- `StartAt` (short): The first number for automatic numbering.
- `NumberingType` (short): Specifies the numbering style (e.g., Arabic, Roman).
- `CharStyleName` (string): Character style for the note symbol.
- `AnchorCharStyleName` (string): Character style for the note anchor in the text.
- `PageStyleName` (string): Page style for the page containing note texts.
- `ParaStyleName` (string): Paragraph style for the note text.
- `BeginNotice` (string): Text at the restart of the note text after a page break (footnotes only).
- `EndNotice` (string): Text at the end of a note part before a page break (footnotes only).
- `PositionEndOfDoc` (boolean): If TRUE, footnote text is shown at the end of the document (footnotes only).
- `FootnoteCounting` (short): How note numbers are counted (e.g., per page, per chapter, per document).

Example of updating settings:
```python
settings = doc.getFootnoteSettings()
settings.setPropertyValue("Prefix", "[")
settings.setPropertyValue("Suffix", "]")
settings.setPropertyValue("StartAt", 1)
```

## Exceptions and Edge Cases
- Inserting a note at an invalid position or inside another note may throw `IllegalArgumentException`.
- Modifying notes inside read-only sections or locked text frames may fail.
- Auto-numbered footnotes have a label of `""`. When retrieving the visible number, it's maintained by LibreOffice formatting; the API mostly exposes the label property if it's explicitly overridden.
