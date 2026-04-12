# Draw/Impress specialized toolsets (nested delegation)

This document describes how Draw and Impress implement **nested delegation** for specialized toolsets, similar to Writer's and Calc's approaches. For detailed background on the delegation model, API design philosophies (Fine-grained vs. Fat APIs), and architecture overview, see [Writer specialized toolsets](writer-specialized-toolsets.md).

This document focuses on **Draw/Impress-specific** domains, implementation status, and feature coverage.

---

## 1. Draw/Impress-specific domains and implementation

LibreOffice Draw and Impress support a large surface area through UNO: shapes, slides/pages, master slides, transitions, animations, charts, speaker notes, and more. WriterAgent implements **nested delegation** for Draw/Impress using the same architecture as Writer and Calc:

- **Tier filtering** in `ToolRegistry.get_tools` / `get_schemas`
- **Domain bases** (`ToolDraw*Base`) with `tier = "specialized"` and `specialized_domain`
- **Gateway tool**: `delegate_to_specialized_draw_toolset` (`tier = "core"`, `is_async()`)
- **System prompt**: `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT` in `constants.py`

For implementation details, see the [Writer documentation](writer-specialized-toolsets.md#3-implementation-reference).

---

## 2. Draw/Impress domains and feature coverage

WriterAgent organizes Draw/Impress tools into specialized domains to keep the main chat toolset focused. Below is the current implementation status and roadmap.

---

## 3. Implementation status and roadmap

### 3.1 Current implementation

| Domain / area | WriterAgent status | Module & tools | Notes |
|---------------|--------------------|----------------|-------|
| **Shapes** | ✅ Implemented | `shapes.py`: CreateShape, EditShape, DeleteShape, ConnectShapes, GroupShapes, GetDrawSummary | Core tools on main list |
| **Pages/Slides** | ✅ Implemented | `pages.py`: ListPages, AddSlide, DeleteSlide, ReadSlideText, GetPresentationInfo | Core tools on main list |
| **Master Slides** | ✅ Implemented | `masters.py`: ListMasterSlides, GetSlideMaster, SetSlideMaster | Core tools on main list |
| **Speaker Notes** | ✅ Implemented | `notes.py`: GetSpeakerNotes, SetSpeakerNotes | Core tools on main list |
| **Placeholders** | ✅ Implemented | `placeholders.py`: ListPlaceholders, GetPlaceholderText, SetPlaceholderText | Core tools on main list |
| **Transitions** | ✅ Implemented | `transitions.py`: GetSlideTransition, SetSlideTransition, GetSlideLayout, SetSlideLayout | Core tools on main list |
| **Charts** | ✅ Implemented | `charts.py`: ListCharts, GetChartInfo, CreateChart, EditChart, DeleteChart (shared with Writer/Calc) | Specialized tier |
| **Web Research** | ✅ Implemented | Web research integration via gateway tool | Specialized tier |
| **Tree Structure** | ✅ Implemented | `tree.py`: GetDrawTree | Core tools on main list |

### 3.2 Future enhancements (roadmap)

| Feature | Status | Notes |
|---------|--------|-------|
| **Animations** | ❌ Not implemented | Slide/element animations, timing, effects (UNO: `com.sun.star.presentation.Animation*`) |
| **Interactive Controls** | ❌ Not implemented | Buttons, hyperlinks, action settings (UNO: `com.sun.star.presentation.Action*`) |
| **Custom Shows** | ❌ Not implemented | Custom slide show sequences (UNO: `com.sun.star.presentation.CustomShow*`) |
| **Slide Timings** | ❌ Not implemented | Rehearse timings, automatic advancement (UNO: `com.sun.star.presentation.SlideShow*`) |
| **Presentation Console** | ❌ Not implemented | Presenter view, notes display (UNO: `com.sun.star.presentation.Presentation*`) |
| **Export Options** | ❌ Not implemented | PDF export, image export, video export (UNO: `com.sun.star.document.ExportFilter*`) |
| **Templates** | ❌ Not implemented | Template management, custom templates (UNO: `com.sun.star.document.DocumentTemplates`) |
| **Themes** | ❌ Not implemented | Color schemes, font schemes, effects (UNO: `com.sun.star.presentation.Theme*`) |
| **Layers** | ❌ Not implemented | Layer management, visibility, locking (UNO: `com.sun.star.drawing.Layer*`) |
| **Guides & Grids** | ❌ Not implemented | Custom guides, snap settings (UNO: `com.sun.star.drawing.Guide*`, `Grid*`) |
| **3D Objects** | ❌ Not implemented | 3D shape creation and manipulation (UNO: `com.sun.star.drawing.Shape3D*`) |
| **Media** | ❌ Not implemented | Audio/video insertion and control (UNO: `com.sun.star.presentation.Media*`) |
| **OCR** | ❌ Not implemented | Text recognition from images |
| **Collaboration** | ❌ Not implemented | Comments, annotations, review tools (UNO: `com.sun.star.text.TextField*`) |
| **Versioning** | ❌ Not implemented | Document versions, history (UNO: `com.sun.star.document.DocumentVersion*`) |
| **Macros** | ❌ Not implemented | Macro recording/execution, event handling (UNO: `com.sun.star.script.*`) |
| **Forms** | ❌ Not implemented | Form controls, data entry (UNO: `com.sun.star.form.*`) |
| **Advanced Chart Features** | ❌ Not implemented | Animation effects, data labels, trends (UNO: `com.sun.star.chart2.*`) |
| **Shape Effects** | ❌ Not implemented | Shadows, glows, reflections (UNO: `com.sun.star.drawing.Shadow*`, `Glow*`) |
| **Text Effects** | ❌ Not implemented | Text animations, 3D text (UNO: `com.sun.star.drawing.Text*`) |
| **Slide Masters Advanced** | ❌ Not implemented | Custom layouts, layout editing (UNO: `com.sun.star.presentation.MasterPage*`) |

### 3.3 Cross-cutting improvements

- **MCP / API opt-in:** Config or query parameter to list `specialized` tools on `tools/list`
- **Performance tuning:** Timeouts and step limits for sub-agent execution
- **Telemetry:** Track domain usage to prioritize development
- **Documentation:** Keep [`AGENTS.md`](../../AGENTS.md) synchronized

For testing and operations details, see the [Writer documentation](writer-specialized-toolsets.md#4-testing-and-operations).

---

## 4. Draw/Impress-specific considerations

### 4.1 Document Type Detection

Draw and Impress share the same UNO service base but have different use cases:
- **Draw**: `com.sun.star.drawing.DrawingDocument` - Vector graphics, diagrams, posters
- **Impress**: `com.sun.star.presentation.PresentationDocument` - Slide presentations, animations

The `delegate_to_specialized_draw_toolset` gateway tool supports both service types in its `uno_services` declaration.

### 4.2 Page vs Slide Terminology

WriterAgent uses consistent terminology:
- **Draw**: "pages" (via `list_pages`, `get_draw_summary`)
- **Impress**: "slides" (via `list_pages`, `add_slide`, `delete_slide`)

The underlying implementation uses the same tools, with context-aware naming in the UI.

### 4.3 Shape and Layout Differences

- **Draw**: Shapes are placed on infinite canvas pages
- **Impress**: Shapes are placed within slide layouts with placeholders

Tools like `create_shape` and `edit_shape` work across both, but layout-aware tools (placeholders, master slides) are Impress-specific.

---

## 5. Summary

| Concern | Mechanism |
|---------|-----------|
| Smaller default tool list | `exclude_tiers` default in `ToolRegistry.get_tools` / `get_schemas` |
| Domain grouping | `ToolDraw*Base.specialized_domain` + `tier = "specialized"` |
| User/model entry point | `delegate_to_specialized_draw_toolset` (`tier = "core"`, async) |
| Sub-agent completion | `final_answer` (`tier = "specialized_control"`) |
| Prompt teaching | `DEFAULT_DRAW_CHAT_SYSTEM_PROMPT` in `constants.py` |
| Execution by name | Unchanged `execute()` — tier only affects **listing**, not **dispatch** |

This design trades a second LLM hop (delegation) for a **cleaner main conversation** and **safer tool choice**, while preserving a path to **full** Draw/Impress automation per domain.

---

## 6. References

For complete LibreOffice Draw/Impress UNO API documentation:
- [Official LibreOffice API Reference](https://api.libreoffice.org/) - Comprehensive UNO IDL API documentation
- [LibreOffice Programming - Draw/Impress APIs](https://flywire.github.io/lo-p/11-Draw_Impress_APIs.html) - Practical guide with examples
- [LibreOffice Developer's Guide](https://wiki.documentfoundation.org/Documentation/DevGuide) - Technical foundation and UNOIDL language
- [LibreOffice Development Tools](https://help.libreoffice.org/latest/en-US/text/shared/guide/dev_tools.html)
- [LibreOffice Impress Guide](https://documentation.libreoffice.org/en/english-documentation/impress/)
- [LibreOffice Draw Guide](https://documentation.libreoffice.org/en/english-documentation/draw/)

For recent feature additions:
- [LibreOffice 26.2 Release Notes](https://www.howtogeek.com/libreoffices-first-big-update-for-2026-has-arrived/)
- [LibreOffice 26.2 New Features](https://9to5linux.com/libreoffice-26-2-open-source-office-suite-officially-released-this-is-whats-new)

For presentation design best practices:
- [Presentation Design Principles](https://www.nngroup.com/articles/presentation-design-principles/)
- [Effective Slide Design](https://www.edwardtufte.com/bboard/q-and-a-fetch-msg?msg_id=0001yB)
- [LibreOffice Impress Tutorials](https://www.libreoffice.org/discover/impress/)

### 6.1 Key UNO Services and Interfaces

**Core Document Services:**
- `com.sun.star.drawing.DrawingDocument` - Draw documents
- `com.sun.star.presentation.PresentationDocument` - Impress presentations

**Shape and Drawing:**
- `com.sun.star.drawing.Shape` - Base shape interface
- `com.sun.star.drawing.RectangleShape`, `EllipseShape`, etc. - Specific shape types
- `com.sun.star.drawing.ConnectorShape` - Connection lines between shapes
- `com.sun.star.drawing.ShapeCollection` - Shape grouping
- `com.sun.star.drawing.EnhancedCustomShapeEngine` - Complex custom shapes
- `com.sun.star.drawing.FillStyle`, `LineStyle` - Shape formatting

**Presentation Specific:**
- `com.sun.star.presentation.PresentationObjectType` - Placeholder types
- `com.sun.star.presentation.AnimationSpeed` - Transition timing
- `com.sun.star.presentation.FadeEffect` - Transition effects

**Text and Layout:**
- `com.sun.star.text.Shape` - Text in shapes
- `com.sun.star.text.TextContentAnchorType` - Shape anchoring
- `com.sun.star.text.HoriOrientation`, `VertOrientation` - Positioning

**UI and Selection:**
- `com.sun.star.view.XSelectionSupplier` - Shape selection
- `com.sun.star.awt.Point`, `Size` - Coordinates and dimensions

---

## 7. Draw/Impress-specific tool details

### 7.1 Shape Tools

The shape tools (`create_shape`, `edit_shape`, `delete_shape`) support various shape types:
- Rectangle
- Ellipse
- Text
- Line
- Polyline
- Polygon
- Path
- Connector (for connecting shapes)
- Enhanced Custom Shapes (complex shapes via `com.sun.star.drawing.EnhancedCustomShapeEngine`)

**Advanced Shape Features:**
- **Shape Grouping**: Multiple shapes can be grouped using `group_shapes` and `connect_shapes`
- **Shape Collection**: Uses `com.sun.star.drawing.ShapeCollection` for managing grouped shapes
- **Enhanced Custom Shapes**: Support for complex custom shapes with adjustable parameters
- **Shape Formatting**: Fill styles, line styles, colors, and transparency
- **Shape Anchoring**: Positioning relative to page, paragraph, or character

Each shape type has specific properties that can be manipulated through the `edit_shape` tool, including geometry, styling, text content (for text shapes), and layering.

### 7.2 Page/Slide Management

Page and slide management tools include:
- `list_pages`: List all pages/slides with their properties
- `add_slide`: Add a new slide (Impress-specific)
- `delete_slide`: Remove a slide (Impress-specific)
- `read_slide_text`: Extract text content from a slide
- `get_presentation_info`: Get metadata about the presentation

### 7.3 Master Slide System

Master slides provide consistent styling across presentations:
- `list_master_slides`: List available master slides
- `get_slide_master`: Get information about a specific master slide
- `set_slide_master`: Apply a master slide to a page

### 7.4 Speaker Notes

Speaker notes enhance presentation delivery:
- `get_speaker_notes`: Retrieve notes for a slide
- `set_speaker_notes`: Set or update notes for a slide

### 7.5 Placeholders

Placeholders provide structured content areas:
- `list_placeholders`: List all placeholders on a slide
- `get_placeholder_text`: Get text content from a placeholder
- `set_placeholder_text`: Set text content in a placeholder

### 7.6 Transitions and Layouts

Slide transitions and layouts control presentation flow:
- `get_slide_transition`: Get current transition for a slide
- `set_slide_transition`: Set transition type and properties
- `get_slide_layout`: Get current layout for a slide
- `set_slide_layout`: Change slide layout

### 7.7 Chart Integration

Charts can be embedded in Draw/Impress documents:
- `list_charts`: List all charts in the document
- `get_chart_info`: Get detailed information about a chart
- `create_chart`: Create a new chart
- `edit_chart`: Modify an existing chart
- `delete_chart`: Remove a chart

---

## 8. Future Development Priorities

Based on the roadmap and user needs, future development should focus on:

1. **Animation Support**: Slide and element animations for professional presentations
2. **Interactive Elements**: Buttons, hyperlinks, and action settings
3. **Media Integration**: Audio and video support
4. **Advanced Export**: PDF, image, and video export options
5. **Template Management**: Custom templates and themes
6. **Collaboration Tools**: Comments and annotations
7. **Macro Support**: Automation and custom functionality

These priorities align with common presentation and graphics workflows, providing the most value to users.