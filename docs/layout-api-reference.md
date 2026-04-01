# LibreOffice Writer Layout API Reference

This document outlines the LibreOffice UNO API components used for advanced document layout in Writer, covering page styles, headers, footers, margins, and columns.

## Overview

In LibreOffice Writer, document layout is primarily controlled through **Page Styles** (`com.sun.star.style.PageStyle`). Unlike simple character or paragraph properties, page layout properties are applied to specific styles, which are then applied to the pages in the document.

The document's style families can be accessed via `doc.getStyleFamilies()`. The family for page styles is `"PageStyles"`.

## 1. Page Styles and Dimensions (`com.sun.star.style.PageStyle`)

### Services and Interfaces
- **Service:** `com.sun.star.style.PageStyle`
- **Interfaces:** `com.sun.star.beans.XPropertySet`

### Key Properties (Dimensions in 1/100th mm)
- **`Width`** (`int`): The width of the page.
- **`Height`** (`int`): The height of the page.
- **`IsLandscape`** (`bool`): Indicates if the page orientation is landscape.
- **`LeftMargin`**, **`RightMargin`**, **`TopMargin`**, **`BottomMargin`** (`int`): Page margins.

### Python Example: Retrieving and Modifying Page Dimensions
```python
style_families = doc.getStyleFamilies()
page_styles = style_families.getByName("PageStyles")
default_style = page_styles.getByName("Standard") # or "Default Style" depending on locale/version

# Get properties
width = default_style.getPropertyValue("Width")
left_margin = default_style.getPropertyValue("LeftMargin")

# Set to Landscape A4
default_style.setPropertyValue("IsLandscape", True)
default_style.setPropertyValue("Width", 29700)  # 297mm
default_style.setPropertyValue("Height", 21000) # 210mm
```

## 2. Headers and Footers

Headers and footers are also controlled via the Page Style properties. Each page style has separate properties for turning headers/footers on and accessing their text objects.

### Key Properties
- **`HeaderIsOn`** / **`FooterIsOn`** (`bool`): Enables or disables the header/footer.
- **`HeaderIsShared`** / **`FooterIsShared`** (`bool`): If true, the same header/footer is used for left and right pages.
- **`HeaderText`** / **`FooterText`** (`com.sun.star.text.XText`): The text object representing the header/footer content. This is a full text object, just like the main document body.
- **`HeaderLeftText`** / **`HeaderRightText`** / **`FooterLeftText`** / **`FooterRightText`**: Used when left and right pages have different headers/footers (i.e., when `HeaderIsShared` is False).

### Python Example: Enabling and Writing to a Header
```python
# Enable header
default_style.setPropertyValue("HeaderIsOn", True)

# Get the text object
header_text = default_style.getPropertyValue("HeaderText")

# Clear existing content and insert new text
header_text.setString("My Document Header")
```

## 3. Columns (`com.sun.star.text.TextColumns`)

Columns can be applied to Page Styles, Sections, or Text Frames. They are managed using the `com.sun.star.text.TextColumns` service.

### Services and Interfaces
- **Service:** `com.sun.star.text.TextColumns`
- **Struct:** `com.sun.star.text.TextColumn`
- **Interface:** `com.sun.star.text.XTextColumns`

### Key Properties & Methods
- `XTextColumns.getColumnCount()`: Returns the number of columns.
- `XTextColumns.setColumnCount(short)`: Sets the number of columns.
- `XTextColumns.getColumns()`: Returns a tuple of `TextColumn` structs.
- `XTextColumns.setColumns(tuple)`: Applies the configuration defined by a tuple of `TextColumn` structs.

### The `TextColumn` Struct
Each column configuration is represented by a `TextColumn` struct:
- **`Width`** (`int`): Relative width of the column.
- **`LeftMargin`** (`int`): Spacing to the left of the column (1/100th mm).
- **`RightMargin`** (`int`): Spacing to the right of the column (1/100th mm).

### Python Example: Setting 2 Columns on a Page Style
```python
# Get current columns object
columns = default_style.getPropertyValue("TextColumns")

# Set to 2 columns
columns.setColumnCount(2)

# To add spacing (e.g., 5mm) between columns:
cols = list(columns.getColumns())
# First column right margin
cols[0].RightMargin = 250 # 2.5mm
# Second column left margin
cols[1].LeftMargin = 250 # 2.5mm

# Apply back
columns.setColumns(tuple(cols))
default_style.setPropertyValue("TextColumns", columns)
```

## 4. Paragraph and Page Breaks

While not strictly layout, forcing content onto a new page is often part of layout manipulation.

### Key Property
- **`BreakType`** (`com.sun.star.style.BreakType` enum): Applied to a paragraph to force a break before or after.
  - `PAGE_BEFORE`, `PAGE_AFTER`, `COLUMN_BEFORE`, `COLUMN_AFTER`, `NONE`.

### Python Example: Forcing a Page Break Before a Paragraph
```python
from com.sun.star.style.BreakType import PAGE_BEFORE

cursor = doc.getText().createTextCursor()
# ... move cursor to desired paragraph ...
cursor.setPropertyValue("BreakType", PAGE_BEFORE)
```

## 5. Comprehensive PageStyle Properties

A complete dump of all available properties for `com.sun.star.style.PageStyle`. Most are `long` measurements in 1/100th mm, `boolean`, `string`, or specific enum/struct types.

### Page Dimensions and Margins
* `Width`, `Height` (long): Absolute page size.
* `IsLandscape` (boolean): Page orientation.
* `LeftMargin`, `RightMargin`, `TopMargin`, `BottomMargin` (long): Distance from page edge to content.
* `GutterMargin` (long): Additional space for binding.

### Background and Fill Properties
Used to set the background color or image of the page itself.
* `BackColor` (long), `BackTransparent` (boolean)
* `FillStyle` (com.sun.star.drawing.FillStyle)
* `FillColor`, `FillColor2` (long)
* `FillBitmapURL`, `FillGradientName`, etc.

### Header/Footer
* `HeaderIsOn` / `FooterIsOn` (boolean): Master toggle.
* `HeaderIsShared` / `FooterIsShared` (boolean): If left and right pages share the same header.
* `HeaderHeight` / `FooterHeight` (long): Absolute height.
* `HeaderBodyDistance` / `FooterBodyDistance` (long): Spacing between header/footer and main text.
* `HeaderIsDynamicHeight` / `FooterIsDynamicHeight` (boolean): Auto-grow header/footer height.
* `HeaderText`, `HeaderTextLeft`, `HeaderTextRight` (com.sun.star.text.XText): The text objects for headers.
* Header/Footer backgrounds (`HeaderBackColor`, `HeaderFillStyle`, etc.) and borders (`HeaderLeftBorder`, etc.).

### Borders
* `TopBorder`, `BottomBorder`, `LeftBorder`, `RightBorder` (com.sun.star.table.BorderLine)
* `BorderDistance` (long): Distance from text to borders.

### Footnotes
Properties defining how footnotes are displayed at the bottom of the page.
* `FootnoteHeight` (long): Max height of footnote area.
* `FootnoteLineWeight` (short), `FootnoteLineStyle` (byte), `FootnoteLineColor` (long)
* `FootnoteLineRelativeWidth` (byte): Width as percentage of text area width.
* `FootnoteLineDistance` (long): Distance from text area.
* `FootnoteLineTextDistance` (long): Distance from separator line to footnote text.

### Grid (Asian Typography)
* `GridBaseHeight`, `GridBaseWidth` (long)
* `GridDisplay`, `GridSnapToChars` (boolean)
* `GridMode` (short)

### Miscellaneous
* `NumberingType` (short): Page numbering format (e.g., 4=Arabic, 0=Upper Roman, etc.).
* `PageStyleLayout` (com.sun.star.style.PageStyleLayout enum): `ALL`, `LEFT`, `RIGHT`, `MIRRORED`.
* `RegisterParagraphStyle` (string): Register-true (line up lines of text on both sides of a page).
