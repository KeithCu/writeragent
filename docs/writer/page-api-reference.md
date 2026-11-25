# LibreOffice Writer Page API Reference

This document outlines the LibreOffice UNO API components used for advanced page layout in Writer, covering page styles, headers, footers, margins, and columns.

## Overview

In LibreOffice Writer, document layout is primarily controlled through **Page Styles** (`com.sun.star.style.PageStyle`). Unlike simple character or paragraph properties, page layout properties are applied to specific styles, which are then applied to the pages in the document.

The document's style families can be accessed via `doc.getStyleFamilies()`. The family for page styles is `"PageStyles"`.

To copy a page style (direct properties, header/footer content, and floating graphics) call `style_clone(style=..., new_style=..., family="PageStyles")`. That tool lives with the other style tools; the page sub-agent can call it, and `style_list(family="PageStyles")`, without switching domains. It does not assign the new style to a page. `FollowStyle` on the clone stays the clone's own name. That clone copies header and footer flow inside the same document, cloning graphics in place with wrap, margins, and crop and leaving out tracked deletions (including struck tables; a wholly struck region clones as empty). It does not use the XHTML round-trip described below. `page_get_header_footer_text` and `page_set_header_footer_text` still do.

## 1. Page Styles and Dimensions (`com.sun.star.style.PageStyle`)

### Services and Interfaces
- **Service:** `com.sun.star.style.PageStyle`
- **Interfaces:** `com.sun.star.beans.XPropertySet`

### Key Properties (Dimensions in 1/100th mm)
- **`Width`** (`int`): The width of the page.
- **`Height`** (`int`): The height of the page.
- **`IsLandscape`** (`bool`): Indicates if the page orientation is landscape. `page_set_style_properties` sets the `IsLandscape` flag only and does not swap dimensions; pass `width_mm` and `height_mm` to change dimensions (written after the flag).
- **`LeftMargin`**, **`RightMargin`**, **`TopMargin`**, **`BottomMargin`** (`int`): Page margins.
- **`BackColor`** (`int`): Page background color. `page_set_style_properties` accepts hex (`#FF0000`), color names, `rgb()`, `-1`/`transparent`/`none`/`automatic` (no fill), or an integer (written unchanged so transparency in the high byte is preserved).
- Note: `page_set_style_properties` reports `updated` when a later property throws, and those earlier properties stay applied.

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
- **`HeaderIsOn`** / **`FooterIsOn`** (`bool`): Enables or disables the header/footer. `page_set_style_properties` with `header_is_on=false` / `footer_is_on=false` **refuses** if any *independent* matching region still holds text, fields, images, or tables (including content pending a tracked deletion; LibreOffice’s “delete header?” spirit — no silent disable). First/left variants that only *mirror* the shared region (`FirstIsShared` / `HeaderIsShared` / `FooterIsShared` is true) are not counted — Windows can still report leftover `getString()` on `HeaderTextFirst` after the shared header was cleared (GHA 35466498641). F5: disabling the region **clears** its content; the refuse exists so we do not silently wipe. Clear first with `page_set_header_footer_text(region=…, content='')`, then disable. Enabling (`true`) is always allowed. There is no force-off flag. The region walkers always raise on a read failure; `page_get_header_footer_text` and the style clone treat an unreadable walk as empty, while the disable guard and `page_replace_header_footer_text` refuse.
- **`HeaderIsShared`** / **`FooterIsShared`** (`bool`): If true, the same header/footer is used for left and right pages.
- **`HeaderText`** / **`FooterText`** (`com.sun.star.text.XText`): The text object representing the header/footer content. This is a full text object, just like the main document body.
- **`HeaderIsDynamicHeight`** / **`FooterIsDynamicHeight`** (`bool`): When true, the region grows with its content. Needed when inserting a logo via `image_insert(target="header"|"footer"|"header_first"|"footer_first")` — without it a taller image keeps the fixed header height and spills into the body. WriterAgent enables this by default for header/footer image inserts (`auto_height`, also on `page_set_header_footer_text`).
- **`HeaderTextLeft`** / **`HeaderTextRight`** / **`FooterTextLeft`** / **`FooterTextRight`**: Used when left and right pages have different headers/footers (i.e., when `HeaderIsShared` is False).
- **`FirstIsShared`** (`bool`): If false, the first page has its own header/footer — the usual setup for a letterhead. Its content then lives in **`HeaderTextFirst`** / **`FooterTextFirst`**, which are separate text objects: `HeaderText` does not reach them.

These variants are exposed as the `region` values of `page_get_header_footer_text` / `page_set_header_footer_text`: `header`, `footer`, `header_first`, `footer_first`, `header_left`, `footer_left`. When the matching `*IsShared` flag is on, the variant mirrors the shared text: get still returns the shared text, while `page_set_header_footer_text` and `page_replace_header_footer_text` skip a mirror and do not write it. `page_get_style_properties` reports `first_is_shared`, and `page_set_style_properties` writes it. `style_get_info(family=PageStyles)` reads through the same `get_page_style_properties` function so the caller does not need a second tool hop. **Turn the header/footer on first** — `FirstIsShared` reads/writes as nothing while `HeaderIsOn` / `FooterIsOn` is false. The same goes for the header/footer measures (`HeaderHeight`, `HeaderBodyDistance`, `FooterHeight`, `FooterBodyDistance`): they are void while that header or footer is off, so `page_get_style_properties` reports them as `null`, not as a failure. `image_insert` uses the same names as `target` for `header`, `footer`, `header_first`, and `footer_first`. A first-page letterhead logo needs `first_is_shared=false` so `HeaderTextFirst` is its own text object; then `target=header_first` (or `footer_first`). Shared `target=header` still writes `HeaderText`, which does not reach the first page when `FirstIsShared` is false.

Images in a header/footer must be anchored **`AS_CHARACTER`** (in the text flow). A floating `AT_CHARACTER` image does not contribute to line height, so even with dynamic height the region may not grow.

Region membership (is this draw-page shape or search hit in *this* header `XText`?) uses [`uno_same`](../framework/uno-utilities.md) (`is` → `==` → `uno.isSame`). PyUNO often returns distinct Python wrappers for the same UNO object, so bare `==` / `!=` can miss a letterhead logo in `page_get_header_footer_text` metadata. That is a LibreOffice / PyUNO wrapper issue, not the debug UNO thread proxy. A false miss is still wrong for get/scan; historically it also made a wipe look safe.

### Reading and writing: shared body HTML pipeline

`getString()` cannot represent a letterhead — a logo is an empty line and a page-number field is its rendered digits. `page_get_header_footer_text` therefore exports the region's `XText` through the same XHTML + postprocess stack as `get_content` (`xtext_to_content` in `html_export.py`): copy the region into a hidden Writer body, then `document_to_content`. The copy walks portions (leaving out tracked deletions, joining paragraphs across deletions and keeping blank struck paragraphs as Accept All does while dropping a trailing struck paragraph left by a recorded rewrite, leaving out struck tables whether spanned by a deletion or deleted via Table > Delete Table while recording, and keeping logos that Accept All keeps), inserts each paragraph unformatted and then paints it with `paint_direct_formatting`, clones anchored graphics where the portion walk meets them (including inside table cells, leaving out cell logos Accept All removes), and preserves original table and graphic names. The portion copy also copies paragraph styles the scratch document does not already have, so a header or footer custom style name is in the HTML. It does not select the live document: controller selection plus a transferable plus the XHTML export is the pattern that corrupted the heap (see `range_copy.copy_flow_into_xtext`). Fields appear as `<span title="page-number"/>` (and the other restored XHTML titles: `page-count`, `date`, `time`, `chapter`, `author-name`, `author-initials`, `file-name`, `title`, `subject`); tables and `<img>` logos are in the HTML. `include_images` defaults to **true** so a letterhead is visible. The result still lists `images` / `fields` / `paragraph_count` as machine-readable extras: `fields`, `images`, and `paragraph_count` describe the region as Accept All would leave it (`fields` covers table cells; struck tables, struck fields, and struck logos are left out, while a struck logo still blocks turning the region off). A closed document reports `DOCUMENT_DISPOSED` from every page tool; `fields` and `images` are omitted, not partial, when the walk cannot be read. The full LibreOffice XHTML `title=` list and the restore subset live in `field_restore.py` next to `_FIELD_TITLE_TO_SERVICE`.

`page_set_header_footer_text` imports that HTML into the same `XText` via `replace_xtext_with_html` (`html_import.py`) — StarWriter `insertDocumentFromURL`. The region is cleared including tables (`SwXText.setString("")` when not recording or when the region starts or ends with a table, with explicit table removal first when not recording so the surviving paragraph keeps its style; otherwise a text cursor clear while recording). When recording, any empty helper paragraph before a leading table is merged forward untracked, and the import opens a fresh paragraph (clearing direct properties and switching off any heading-like style whose `FollowStyle` differs, so heading style does not reach other paragraphs) and drops any empty leftover afterward. A trailing table keeps its required following paragraph instead of leaving the region ending in a table. When Track Changes is recording on a header or footer whose content originally starts or ends with a table, Reject All leaves at most one blank paragraph at the top (original leading table) and one at the end (original trailing table) across repeated writes, inherent to `SwXText.setString`. Wrapped pictures keep their name, anchor type, placement, wrap, margins, crop, description, and link across writes (`image_placeholders.capture_region_images` / `restore_region_images`); data-less placeholders (`src=""`) are restored by name from the document or refused before anything changes. (While recording Track Changes, if a wrapped `AT_PARAGRAPH` image's paragraph — or an `AT_CHARACTER` image at the end of the last paragraph under a single-paragraph clear — survives the clear, the surviving original is kept and edits to that image's data, alt, or link in the same write are not applied.) The clear, import, and field restoration are wrapped in a document undo context: if any step fails, the action is undone so the previous fields and graphics are restored. If undo cannot be entered or rolled back, it reports a failure. Field spans are restored as live UNO fields after import (the HTML filter drops the empty span). That includes fields inside tables in the region. If a field cannot be created or inserted, the write fails. It does not report success with a literal `[[WA-FIELD:…]]` left in the region. Plain text is wrapped as a paragraph and still goes through import. Get the HTML, edit it, set it back — logos, tables, and fields stay live.

`page_replace_header_footer_text` changes one substring in place (`old_content` / `content`, one pair per call, same shape as `apply_content` search-replace). It walks the region's portions (skipping tables and struck text, so `occurrence` counts live matches only) and selects with `goRight`, counting each text field and inline `AS_CHARACTER` image as one cursor step, so a page-number field and an `AT_PARAGRAPH` or `AS_CHARACTER` graphic stay. Call it again for a second change. It does not turn the region on. A `_first` or `_left` region that only mirrors the shared header or footer is skipped; edit `header` or `footer` instead. An image anchored `AT_CHARACTER` inside the match is refused, because `setString` on that range can drop it. Failures report `UNDO_UNAVAILABLE`, `ROLLBACK_FAILED`, or `UNDO_CONTEXT_LEAK`. Use `page_set_header_footer_text` when the whole region should be replaced.

### Python Example: Enabling and Writing to a Header
```python
# Prefer the page tools: get returns HTML, set imports it back.
# page_get_header_footer_text(region="header")
# page_set_header_footer_text(region="header", content="<p>My Document Header</p>")

# Low-level UNO equivalent of enabling the region (the tool does this):
default_style.setPropertyValue("HeaderIsOn", True)
header_text = default_style.getPropertyValue("HeaderText")
# Write via replace_xtext_with_html / page_set so logos, tables, and fields stay live.
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
- **`Width`** (`int`): Relative width of the column (`page_get_columns` returns `width_unit: "relative"`).
- **`LeftMargin`** (`int`): Spacing to the left of the column (1/100th mm; reported in mm).
- **`RightMargin`** (`int`): Spacing to the right of the column (1/100th mm; reported in mm).

Setting `spacing_mm=0` in `page_set_columns` explicitly writes the inner margins to `0`, clearing any previous gap. `page_set_columns` accepts `column_count` 1–99 (matching the Columns dialog's 99-slot arrays) and `spacing_mm` as a finite number ≥ 0, and rejects anything else with `INVALID_PARAM` before touching the style.

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
- A page break inside a table cell is rejected with an error and does not set `BreakType`.
- **`PageDescName`** (string): page style to use from this paragraph onward. `page_insert_break(page_style=...)` writes the name `resolve_page_style` accepted. An unknown name is an error and does not set `BreakType`. Setting `PageDescName` also requests a page break ([ParagraphProperties](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1style_1_1ParagraphProperties.html)); the tool still sets `BreakType` to `PAGE_BEFORE`.
- **`PageNumberOffset`** (short): new page number at that break. `page_insert_break(restart_page_number=N)` writes it only when `page_style` is set, and only for an integer `N` from 1 to 32767. Omitted means the current sequence continues. `0` is not written. `PageStyleName` is read-only and is not set.

### Python Example: Forcing a Page Break Before a Paragraph
```python
from com.sun.star.style.BreakType import PAGE_BEFORE

cursor = doc.getText().createTextCursor()
# ... move cursor to the paragraph that should start the new page ...
cursor.setPropertyValue("BreakType", PAGE_BEFORE)
cursor.setPropertyValue("PageDescName", "Annex")
cursor.setPropertyValue("PageNumberOffset", 1)
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
* `NumberingType` (short): Page numbering format (e.g., 0=CHARS_UPPER_LETTER, 1=CHARS_LOWER_LETTER, 2=ROMAN_UPPER, 3=ROMAN_LOWER, 4=ARABIC).
* `PageStyleLayout` (short): 0=ALL, 1=LEFT, 2=RIGHT, 3=MIRRORED.
* `RegisterParagraphStyle` (string): Register-true (line up lines of text on both sides of a page).

---

## 6. Watermarks

Text watermarks are driven through LibreOffice's own `.uno:Watermark` command (`plugin/writer/watermark.py`, `page` domain):

* **`page_get_watermark`**: Queries `.uno:Watermark` status (`SwDocShell::StateStyleSheet` → `SwEditShell::GetWatermark`) via `URLTransformer.parseStrict` (`SfxBaseController::queryDispatch` requires `.uno:` protocol) and a synchronous `XStatusListener` (`SfxDispatchController_Impl::addStatusListener` in `sfx2/source/control/unoctitm.cxx`). Returns `watermark` (`{"text", "font", "angle", "transparency", "color"}` with `angle` normalized via `% 360` because `edfcol.cxx` `lcl_GetAngle` returns `360` for `0°`, or `null` when `Text` is empty) and `picture_watermark` (whether any draw-page shape's name starts with `WordPictureWatermark`, `oox/source/export/vmlexport.cxx` `IsWaterMarkShape`).
* **`page_set_watermark`**: Validates inputs (`text`, `font`, `angle` `0..359`, `transparency` `0..100`, `color`), reads the current state so omitted fields preserve existing values (or LibreOffice defaults: `Liberation Sans`, `45`, `50`, `#c0c0c0` from `SfxWatermarkItem` in `sfx2/source/doc/watermarkitem.cxx`), dispatches `.uno:Watermark` (`SwDocShell::Execute` case `SID_WATERMARK` → `SwWrtShell::SetWatermark`), verifies the state changed, and disposes any `WordPictureWatermark*` shapes so DOCX export does not show both text and picture watermarks; on a verified removal (`text == ""`), it also disposes any `PowerPlusWaterMarkObject*` shapes still on the draw page; on a verified non-empty set, it copies the new text and look into any `PowerPlusWaterMarkObject*` shapes in inactive headers. Returns `style_unreviewed: true`.

### LibreOffice behaviour and quirks

* **Shape & DOCX/ODT round-trip**: `lcl_placeWatermarkInHeader` (`sw/source/core/edit/edfcol.cxx`) inserts a `fontwork-plain-text` shape named `PowerPlusWaterMarkObject…` anchored `AT_CHARACTER` in the header (`THROUGH` wrap, `Opaque=False`). Font size is fitted automatically to one line. VML export (`vmlexport.cxx`) round-trips text, font, angle, and color; transparency may drift by 1 on some values due to VML opacity rounding (50 round-trips exactly).
* **Headers forced on**: `SwEditShell::SetWatermark` turns on `HeaderIsOn` for every page style when setting a watermark; removal skips styles whose header is off and leaves on the headers that are on (`sw/source/core/edit/edfcol.cxx`).
* **Undo stack cleared**: `SwXPageStyle::SetPropertyValues_Impl` (`sw/source/core/unocore/unostyle.cxx`) calls `DelAllUndoObj()` on every page-style property write (`i64460`), so setting a watermark, or removing one from headers that are on, clears the undo history and cannot be wrapped in `WriterCompoundUndo`. When removal also disposes leftover copies, the tool clears the history itself (unless an undo context is open, in which case only a header-on dispatch marks the stack to clear on close; with every header off the deletions stay in that group), because each dispose records an undo entry (it does not lock undo, because with every header off the stack is not emptied and deleting frames unrecorded would leave dangling entries). Removing when every header is off and no copy exists leaves the history alone. Deleting a Word picture watermark stays undoable.
* **Read-only documents**: `SID_WATERMARK` has `ReadOnlyDoc = FALSE`. Both tools check `IsEnabled` and return `UNSUPPORTED` when disabled or when no controller/frame exists.
* **Inactive Word headers**: Word files can carry first-page and even-page headers that are switched off; LibreOffice keeps them as stashed formats (`SwPageDesc::StashFrameFormat`) that `SwXPageStyle::GetPropertyValues_Impl` (`unostyle.cxx`) falls back to the master header for while shared, so `SwEditShell::SetWatermark` cannot reach them. Stashed headers are written on DOCX export (`MSWordExportBase::SectionProperties` in `wrtw8sty.cxx`) and restored on unshare (`SwDoc::ChgPageDesc` in `docdesc.cxx`). `page_set_watermark(text="")` clears them by disposing remaining `PowerPlusWaterMarkObject*` shapes on the draw page and then clearing the undo history; setting non-empty text updates those copies in place, recognised by `getAnchor().getText()` being `None` (`sw/source/core/unocore/unoobj2.cxx` `sw::CreateParentXText`). An inactive header without a watermark does not get one, and the updated copy takes its size from an active copy, which may belong to a page style of another size.
