# Draw/Impress Specialized Toolsets

This document describes Draw/Impress tool organization, current implementation status, and roadmap for **WriterAgent**.

> **Note**: Draw and Impress share the same UNO foundation. WriterAgent treats them as a unified domain with presentation-specific extensions.

----

## 1. Architecture Overview

Draw/Impress tools follow the same **nested delegation** pattern as Writer and Calc:

| Component | Purpose | Location |
|-----------|---------|----------|
| `ToolDrawSpecialBase` | Base class for specialized Draw tools | `plugin/modules/draw/base.py` |
| `specialized_domain` | Domain identifier (e.g., `"charts"`, `"web_research"`) | Class attribute |
| `tier = "specialized"` | Marks tool for domain-specific sub-agent | Class attribute |
| `delegate_to_specialized_draw_toolset` | Gateway tool for delegation | `plugin/modules/draw/specialized.py` |
| `uno_services` | Document type filtering | Class attribute |

** Delegation flow:**
```
Main chat → delegate_to_specialized_draw_toolset → Sub-agent (filtered tools) → final_answer
```

----

## 2. Current Implementation

### 2.1 Core Tools (tier = "core")

These tools are **always available** to the main agent for Draw/Impress documents:

| Tool | Module | Services | Description |
|------|--------|----------|-------------|
| `list_pages` | `pages.py` | Drawing | Lists all pages/slides |
| `get_draw_summary` | `shapes.py` | Drawing | Shape summary for a page |
| `create_shape` | `shapes.py` | Drawing | Create rectangle, ellipse, text, line, connector, custom |
| `edit_shape` | `shapes.py` | Drawing | Modify shape properties (color, size, text, rotation) |
| `delete_shape` | `shapes.py` | Drawing | Remove a shape |
| `connect_shapes` | `shapes.py` | Drawing | Connect two shapes with a line |
| `group_shapes` | `shapes.py` | Drawing | Group multiple shapes |
| `get_draw_tree` | `tree.py` | Drawing+Presentation | JSON DOM of shapes (for flowcharts) |
| `add_slide` | `pages.py` | Drawing | Add a new page/slide |
| `delete_slide` | `pages.py` | Drawing | Delete a page/slide |
| `read_slide_text` | `pages.py` | Drawing | Extract text from all shapes on a page |
| `get_presentation_info` | `pages.py` | Drawing | Metadata: slide count, dimensions, masters |
| `get_slide_transition` | `transitions.py` | Presentation | Get transition effect/speed/duration |
| `set_slide_transition` | `transitions.py` | Presentation | Set transition effect/speed/duration |
| `get_slide_layout` | `transitions.py` | Presentation | Get current layout |
| `set_slide_layout` | `transitions.py` | Presentation | Set layout by name (title, text, two_column, etc.) |
| `list_master_slides` | `masters.py` | Drawing+Presentation | List all master slides |
| `get_slide_master` | `masters.py` | Drawing+Presentation | Get master for a slide |
| `set_slide_master` | `masters.py` | Drawing+Presentation | Assign a master to a slide |
| `get_speaker_notes` | `notes.py` | Presentation | Read speaker notes |
| `set_speaker_notes` | `notes.py` | Presentation | Set speaker notes |
| `list_placeholders` | `placeholders.py` | Drawing+Presentation | List placeholder shapes (title, subtitle, body) |
| `get_placeholder_text` | `placeholders.py` | Drawing+Presentation | Get text from a placeholder |
| `set_placeholder_text` | `placeholders.py` | Drawing+Presentation | Set text in a placeholder |
| `list_charts` | `charts.py` | Drawing+Presentation+Spreadsheet+Text | List all charts |
| `get_chart_info` | `charts.py` | Drawing+Presentation+Spreadsheet+Text | Get chart details |
| `create_chart` | `charts.py` | Drawing+Presentation+Spreadsheet+Text | Create a new chart |
| `edit_chart` | `charts.py` | Drawing+Presentation+Spreadsheet+Text | Modify a chart |
| `delete_chart` | `charts.py` | Drawing+Presentation+Spreadsheet+Text | Remove a chart |
| `delegate_to_specialized_draw_toolset` | `specialized.py` | Drawing+Presentation | Gateway for sub-agent delegation |

> ✅ **Fixed**: All tools now correctly include both `DrawingDocument` and `PresentationDocument` in their `uno_services` declarations.

### 2.2 Specialized Tools (tier = "specialized")

These are available only via `delegate_to_specialized_draw_toolset`:

| Tool | Domain | Module | Purpose |
|------|--------|--------|---------|
| `WebResearchTool` | `web_research` | `web_research.py` | Web search for context |

----

## 3. Domain Coverage Matrix

| Domain | Status | Tools | Notes |
|--------|--------|-------|-------|
| **Shapes (core)** | ✅ Complete | 6 tools | Create, edit, delete, connect, group, summary |
| **Pages/Slides (core)** | ✅ Complete | 4 tools | List, add, delete, read text |
| **Master Slides (core)** | ✅ Complete | 3 tools | List, get, set |
| **Speaker Notes (core)** | ✅ Complete | 2 tools | Get, set |
| **Placeholders (core)** | ✅ Complete | 3 tools | List, get text, set text |
| **Transitions (core)** | ✅ Complete | 4 tools | Get/set transition, get/set layout |
| **Charts (specialized)** | ✅ Complete | 5 tools | Full CRUD + info |
| **Tree Structure (core)** | ✅ Complete | 1 tool | JSON DOM for LLM understanding |
| **Web Research (specialized)** | ✅ Complete | 1 tool | Delegated search |
| **Animations** | ❌ Missing | — | Slide + shape-level animations |
| **Layers** | ❌ Missing | — | Draw layer management |
| **Slide Show** | ❌ Missing | — | Start, stop, presenter mode |
| **Media (Audio/Video)** | ❌ Missing | — | Insert, control |
| **Custom Shows** | ❌ Missing | — | Non-linear presentation paths |
| **Timings** | ❌ Missing | — | Rehearse, auto-advance |
| **Themes** | ❌ Missing | — | Color/font schemes |
| **Templates** | ❌ Missing | — | Document templates |
| **Headers/Footers** | ❌ Missing | — | Slide numbering, date |
| **Tables** | ❌ Missing | — | Insert/edit tables in Draw |
| **3D Shapes** | ❌ Missing | — | 3D objects and scenes |
| **Guides/Grid** | ❌ Missing | — | Snap settings, custom guides |
| **OCR** | ❌ Missing | — | Text from images |
| **Export** | ❌ Missing | — | PDF, image, video export |
| **Forms** | ❌ Missing | — | Interactive form controls |
| **Macros** | ❌ Missing | — | Automation scripts |
| **Versioning** | ❌ Missing | — | Document history |

----

## 4. Service Coverage Issues

### 4.1 `uno_services` Fix Applied ✅

**Completed**: Added `"com.sun.star.presentation.PresentationDocument"` to `uno_services` for all 9 tools:

- `ListPages`, `GetDrawSummary`, `CreateShape`, `EditShape`, `ConnectShapes`, `GroupShapes`, `DeleteShape` (in `shapes.py`)
- `ReadSlideText`, `GetPresentationInfo` (in `pages.py`)

These tools now work with both Draw and Impress documents.

### 4.2 Chart Tools Already Correct

The chart tools in `plugin/modules/draw/charts.py` correctly include all document types:
```python
_ALL_CHART_DOCS = [
    "com.sun.star.drawing.DrawingDocument",
    "com.sun.star.presentation.PresentationDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.text.TextDocument",
]
```

----

## 5. Roadmap

### 5.1 Priority 1: Fixes (High Impact, Low Effort)

| Task | Effort | Impact | Status |
|------|--------|--------|--------|
| Add `PresentationDocument` to `uno_services` for 9 tools | 1 hour | Unblocks Impress users from core shape/page tools | ✅ **Done** |
| Add `DrawingDocument` to speaker notes tools | 30 min | Allows notes in Draw | ⏸️ Pending |
| Add `DrawingDocument` to placeholder tools | 30 min | Allows placeholders in Draw | ⏸️ Pending |

### 5.2 Priority 2: High-Value Features

| Feature | UNO Area | User Value | Effort |
|---------|----------|-------------|--------|
| **Slide Animations** | `com.sun.star.presentation.Animation*` | Professional presentations | Medium |
| **Slide Show Controls** | `com.sun.star.presentation.Presentation` | Start/stop presentations | Low |
| **Headers/Footers** | `com.sun.star.presentation.*` | Page numbering, dates | Low |
| **Layers** | `com.sun.star.drawing.Layer*` | Advanced Draw organization | Medium |
| **Media Insertion** | `com.sun.star.presentation.Media*` | Audio/video in slides | Medium |
| **Tables in Draw** | `com.sun.star.drawing.TableShape` | Tabular data | Medium |

### 5.3 Priority 3: Specialized Domains

Create new specialized domains for sub-agent delegation:

| Domain | Tools | Use Case |
|--------|-------|---------|
| `animations` | `get_animations`, `set_animations`, `add_animation` | Complex animation workflows |
| `media` | `insert_audio`, `insert_video`, `control_media` | Multimedia presentations |
| `export` | `export_pdf`, `export_image`, `export_video` | Document output |
| `layers` | `list_layers`, `create_layer`, `set_layer_visibility` | Draw organization |
| `tables` | `insert_table`, `edit_table`, `format_table` | Tabular content in Draw |

### 5.4 Priority 4: Evaluation System Integration

| Task | Effort | Impact |
|------|--------|--------|
| Add `DrawJSONBackend` to prompt optimization | 2-3 hours | Enables Draw/Impress eval without screenshots |
| Extend dataset with Draw/Impress examples | 2 hours | Better model evaluation |
| Add Draw-specific rubrics | 1 hour | Accurate quality assessment |

### 5.5 Priority 5: Future / Nice-to-Have

- **OCR**: Text recognition from inserted images
- **Custom Shows**: Non-linear presentation paths
- **Presenter Console**: Presenter view with notes timer
- **Themes**: Color schemes, font schemes
- **Templates**: Document template management
- **Forms**: Interactive form controls in presentations
- **Macros**: Recording and execution
- **Versioning**: Document history and rollback
- **3D Objects**: 3D shape creation and manipulation
- **Guides/Grid**: Custom guides, snap settings

----

## 6. Key UNO Services Reference

### 6.1 Document Services

| Service | Draw | Impress | Purpose |
|---------|------|---------|---------|
| `com.sun.star.drawing.DrawingDocument` | ✅ | ❌ | Vector graphics, diagrams |
| `com.sun.star.presentation.PresentationDocument` | ❌ | ✅ | Slide presentations |

### 6.2 Shape Services

| Service | Purpose |
|---------|---------|
| `com.sun.star.drawing.Shape` | Base shape interface |
| `com.sun.star.drawing.RectangleShape` | Rectangle |
| `com.sun.star.drawing.EllipseShape` | Ellipse/Circle |
| `com.sun.star.drawing.TextShape` | Text box |
| `com.sun.star.drawing.LineShape` | Line |
| `com.sun.star.drawing.ConnectorShape` | Connection line |
| `com.sun.star.drawing.GroupShape` | Grouped shapes |
| `com.sun.star.drawing.CustomShape` | Custom shapes |
| `com.sun.star.drawing.EnhancedCustomShapeEngine` | Complex custom shapes |

### 6.3 Presentation Services

| Service | Purpose |
|---------|---------|
| `com.sun.star.presentation.Slide` | Individual slide |
| `com.sun.star.presentation.MasterPage` | Master slide |
| `com.sun.star.presentation.NotesPage` | Speaker notes page |
| `com.sun.star.presentation.HandoutPage` | Handout page |
| `com.sun.star.presentation.Presentation` | Slide show controller |
| `com.sun.star.presentation.Animation` | Animation effects |
| `com.sun.star.presentation.FadeEffect` | Transition effects |
| `com.sun.star.presentation.AnimationSpeed` | Transition timing |
| `com.sun.star.presentation.SlideLayout` | Layout types |

### 6.4 Drawing Services

| Service | Purpose |
|---------|---------|
| `com.sun.star.drawing.Layer` | Drawing layer |
| `com.sun.star.drawing.LayerManager` | Layer management |
| `com.sun.star.drawing.DrawPage` | Drawing page (Draw: page, Impress: slide) |
| `com.sun.star.drawing.DrawPages` | Collection of pages |
| `com.sun.star.drawing.MasterPages` | Collection of masters |

----

## 7. Testing Notes

- All Draw tools should be tested with both **Draw** and **Impress** documents
- Test with **headless LibreOffice** (some UNO calls behave differently)
- Test with **empty documents**, **single-page**, and **multi-page** scenarios
- Test **shape types**: rectangle, ellipse, text, line, connector, custom
- Test **edge cases**: deleting last slide, grouping all shapes, etc.

**Recommended test additions:**
- `plugin/tests/uno/test_draw_shapes.py` - Shape CRUD in Draw
- `plugin/tests/uno/test_impress_shapes.py` - Shape CRUD in Impress
- `plugin/tests/uno/test_draw_pages.py` - Page management
- `plugin/tests/uno/test_impress_slides.py` - Slide-specific features

----

## 8. Architecture Notes

### 8.1 Shared vs Separate Implementation

| Approach | Pros | Cons |
|----------|------|------|
| **Shared tools** (current) | Single implementation, consistent behavior | Need to handle both Draw and Impress quirks |
| **Separate tools** | Optimized for each document type | Code duplication, maintenance burden |

**Current approach**: Shared tools with `uno_services` covering both types.

### 8.2 Writer vs Draw/Impress Shape Differences

When using shape tools in Writer:
- Shapes are anchored to text (`AnchorType`, `AnchorPageNo`)
- Must set `AnchorType` before `page.add()` for visibility
- Custom shapes need `EnhancedCustomShapeGeometry` before anchoring

In Draw/Impress:
- Shapes have absolute positioning
- No anchoring required
- Custom shapes work without special handling

The current `create_shape` implementation handles both cases with conditional logic.

### 8.3 Bridge Pattern

The `DrawBridge` class (`plugin/modules/draw/bridge.py`) provides a unified interface for:
- Page/slide management
- Shape creation
- Document navigation

This pattern could be extended to other domains.

----

## 9. References

- [LibreOffice API Reference — Draw](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1drawing_1_1XDrawPage.html)
- [LibreOffice API Reference — Presentation](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1presentation_1_1XPresentation.html)
- [LibreOffice Draw/Impress UNO Examples](https://wiki.documentfoundation.org/Documentation/DevGuide/Drawings/Tutorial)
- [Writer specialized toolsets](writer-specialized-toolsets.md) — Architecture reference
- [AGENTS.md](../../AGENTS.md) — Project overview
d to other domains.

----

## 9. References

- [LibreOffice API Reference — Draw](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1drawing_1_1XDrawPage.html)
- [LibreOffice API Reference — Presentation](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1presentation_1_1XPresentation.html)
- [LibreOffice Draw/Impress UNO Examples](https://wiki.documentfoundation.org/Documentation/DevGuide/Drawings/Tutorial)
- [Writer specialized toolsets](writer-specialized-toolsets.md) — Architecture reference
- [AGENTS.md](../../AGENTS.md) — Project overview
