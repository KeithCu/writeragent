# Draw/Impress Specialized Toolsets

This document describes Draw/Impress tool organization, current implementation status, and roadmap for **WriterAgent**.

> **Note**: Draw and Impress share the same UNO foundation. WriterAgent treats them as a unified domain with presentation-specific extensions.

----

## 1. Architecture Overview

Draw/Impress tools follow the same **nested delegation** pattern as Writer:

| Component | Purpose | Location |
|-----------|---------|----------|
| `ToolDrawSpecialBase` | Base class for specialized Draw tools | `plugin/draw/base.py` |
| `specialized_domain` | Domain identifier (e.g., `"charts"`, `"web_research"`, `"forms"`) | Class attribute |
| `tier = "specialized"` | Marks tool for domain-specific sub-agent | Class attribute |
| `delegate_to_specialized_draw_toolset` | Gateway tool for delegation | `plugin/draw/specialized.py` |
| `uno_services` | Document type filtering | Class attribute |

**Delegation flow:**
```
Main chat → delegate_to_specialized_draw_toolset → Sub-agent (filtered tools) → final_answer
```

----

## 2. Current Implementation

### 2.1 Core Tools (tier = "core")

These tools are **always available** to the main agent for Draw/Impress documents:

| Tool | Module | Services | Description |
|------|--------|----------|-------------|
| `list_pages` | `pages.py` | Drawing+Presentation | Lists all pages/slides |
| `add_slide` | `pages.py` | Drawing+Presentation | Add a new page/slide |
| `delete_slide` | `pages.py` | Drawing+Presentation | Delete a page/slide |
| `duplicate_slide` | `pages.py` | Drawing+Presentation | Duplicate a page/slide after the source |
| `move_slide` | `pages.py` | Drawing+Presentation | Reorder slides by 0-based index |
| `rename_slide` | `pages.py` | Drawing+Presentation | Set the slide Name |
| `set_active_page` | `pages.py` | Drawing+Presentation | Switch current view to slide |
| `read_slide_text` | `pages.py` | Drawing+Presentation | Extract text from all shapes on a page |
| `get_presentation_info` | `pages.py` | Drawing+Presentation | Metadata: slide count, dimensions, masters |
| `get_draw_tree` | `tree.py` | Drawing+Presentation | JSON DOM of shapes and layout hierarchy |
| `list_placeholders` | `placeholders.py` | Presentation | List placeholder shapes (title, subtitle, body) |
| `get_placeholder_text` | `placeholders.py` | Presentation | Get text from a placeholder |
| `set_placeholder_text` | `placeholders.py` | Presentation | Set text in a placeholder |
| `delegate_to_specialized_draw_toolset` | `specialized.py` | Drawing+Presentation | Gateway for sub-agent delegation |

### 2.2 Specialized Tools (tier = "specialized")

These are available only via `delegate_to_specialized_draw_toolset`:

| Tool | Domain | Module | Purpose | Services |
|------|--------|--------|---------|---------|
| `shape_summary` | `shapes` | `draw/shapes.py` | Summary of shapes on page | Drawing+Presentation |
| `shape_upsert` | `shapes` | `draw/shapes.py` | Create or edit shapes (1/100mm coordinates) | Drawing+Presentation |
| `shape_delete` | `shapes` | `draw/shapes.py` | Delete a shape by index | Drawing+Presentation |
| `shape_connect` | `shapes` | `draw/shapes.py` | Connect two shapes with a connector line | Drawing+Presentation |
| `shape_group` | `shapes` | `draw/shapes.py` | Group multiple shapes | Drawing+Presentation |
| `align_shapes` | `shapes` | `draw/shapes.py` | Align shapes to an edge or center | Drawing+Presentation |
| `distribute_shapes` | `shapes` | `draw/shapes.py` | Evenly space shapes along an axis | Drawing+Presentation |
| `create_diagram` | `shapes` | `draw/shapes.py` | Batch nodes + connectors (flowchart) | Drawing+Presentation |
| `image_insert` / `image_list` / `image_delete` / `image_generate` | `images` | `writer/images/images.py` | Same image tools as Writer/Calc; Draw/Impress uses millimetres (`page`, `x_mm`, `y_mm`) | Drawing+Presentation |
| `table_insert` / `table_list` / `table_get_cells` / `table_set_cell` / `manage_table_structure` | `tables` | `writer/specialized/tables.py` + `draw/tables.py` | Same names as Writer; Draw uses TableShape | Drawing+Presentation |
| `get_slide_transition` | `slide_transitions` | `draw/transitions.py` | Get transition effect/speed/duration | Presentation |
| `set_slide_transition` | `slide_transitions` | `draw/transitions.py` | Set transition effect/speed/duration | Presentation |
| `get_slide_layout` | `slide_layouts` | `draw/transitions.py` | Get current slide layout | Presentation |
| `set_slide_layout` | `slide_layouts` | `draw/transitions.py` | Set slide layout by name | Presentation |
| `list_master_slides` | `slide_masters` | `draw/masters.py` | List all master slides | Drawing+Presentation |
| `get_slide_master` | `slide_masters` | `draw/masters.py` | Get master for a slide | Drawing+Presentation |
| `set_slide_master` | `slide_masters` | `draw/masters.py` | Assign a master to a slide | Drawing+Presentation |
| `get_speaker_notes` | `speaker_notes` | `draw/notes.py` | Read speaker notes (Impress) | Presentation |
| `set_speaker_notes` | `speaker_notes` | `draw/notes.py` | Set speaker notes (Impress) | Presentation |
| `get_headers_footers` | `headers_footers` | `draw/headers_footers.py` | Read slide/master header and footer settings (Impress) | Presentation |
| `set_headers_footers` | `headers_footers` | `draw/headers_footers.py` | Update slide/master header and footer settings (Impress) | Presentation |
| `manage_charts` | `charts` | `draw/charts.py` | Unified charts CRUD | Drawing+Presentation |
| `form_*` (6 tools) | `forms` | `writer/forms.py` | Form controls | Drawing+Presentation+Spreadsheet+Text |
| `insert_math` | `math` | `math_insert.py` | Insert LibreOffice Math (OLE) from LaTeX or MathML | Drawing+Presentation |
| `WebResearchTool` | `web_research` | `web_research.py` | Web search for context | All |

### 2.3 Unit & Coordinate System

- **Slide Dimensions**: `get_presentation_info` reports slide width and height in **millimeters** (e.g. $280\text{mm} \times 157\text{mm}$ for 16:9).
- **Shape Coordinates & Sizes**: Shape tools (`shape_upsert`, `shape_connect`) operate in **$1/100\text{ths}$ of a millimeter** ($1\text{mm} = 100\text{ units}$, $10\text{mm} = 1000\text{ units}$, $100\text{mm} = 10000\text{ units}$).

### 2.4 insert_math (math domain)

> **Follow-up — shape size / bounding box:** `insert_math` does not take width/height from the model. It attempts content-based sizing via the embedded object’s `XVisualObject.getVisualAreaSize` (after the formula is set), then falls back to a simple heuristic from formula length. **In practice this often still looks wrong** (too small or large, wrong aspect, or inconsistent across LibreOffice versions and headless vs GUI). This area **needs more engineering**: validate UNO sizing across builds, consider map-unit edge cases, optional post-insert resize once the OLE is realized, or expose optional max dimensions while keeping defaults automatic.

----



### 2.4 Feature: Advanced Impress Layouts

WriterAgent leverages the native Draw/Impress toolset to manage presentation layouts, including centering generated images on slides using explicit shape manipulation. **This layout strategy is derived directly from the explicit shape-handling implementation in LibreAI's `UnoHelper.cpp`.**

#### The Concept
When an AI generates an image for a slide, standard insertion logic often defaults to anchoring it to a generic position. The LibreAI approach, which we have adopted, uses explicit coordinate and dimension management to ensure images are correctly placed and sized within the `DrawingDocumentDrawView`.

#### Implementation (Ported Logic)
We adapt LibreAI's `insertImage` strategy from `UnoHelper.cpp` to create and position a `GraphicObjectShape` explicitly.

```python
def insert_image_into_impress(ctx, doc_model, image_path: str):
    # 1. Get the current Draw Page (Slide)
    controller = doc_model.getCurrentController()
    current_page = controller.getCurrentPage()
    
    # 2. Create the GraphicObjectShape
    factory = doc_model
    shape = factory.createInstance("com.sun.star.drawing.GraphicObjectShape")
    
    # 3. Add to slide
    current_page.add(shape)
    
    # 4. Set Image URL (needs file:/// conversion)
    from com.sun.star.beans import PropertyValue
    file_url = uno.systemPathToFileUrl(image_path)
    shape.setPropertyValue("GraphicURL", file_url)
    
    # 5. Set Layout (LibreAI magic numbers: X:3000, Y:5000, W:14000, H:10500 in 1/100mm)
    from com.sun.star.awt import Point, Size
    shape.setPosition(Point(3000, 5000))
    shape.setSize(Size(14000, 10500))
    
    return True
```

#### FSM Integration
We update the "Generate Image" tool registry (`plugin/framework/tool.py`) to detect the document type:
- **Writer:** Maintains the existing `TextGraphicObject` insertion logic (anchored to text).
- **Impress/Draw:** Delegates to the `insert_image_into_impress` logic, which ignores text anchoring and uses the explicit `setPosition` coordinates to place the image as a standalone shape on the page.

#### UI Updates
The existing sidebar doesn't need new UI elements; the "Insert Image" action dynamically switches behavior based on the document's UNO service, providing a seamless "intelligent" insertion experience regardless of whether the user is in a slide or a document.

**Velocity Advantage:** Python's UNO bindings allow us to translate the shape creation logic almost 1:1 from the C++ source, but with significantly less boilerplate and no need for manual memory management or complex `Reference<>` templates. Estimated dev time: 1-2 hours.


| Domain | Status | Tools | Notes |
|--------|--------|-------|-------|
| **Shapes (core)** | ✅ Complete | 11 tools | Create, edit, delete, connect, group, summary, tree, align, distribute, graphic, diagram |
| **Pages/Slides (core)** | ✅ Complete | 7 tools | List, add, delete, duplicate, move, rename, read text |
| **Master Slides (specialized)** | ✅ Complete | 3 tools | `slide_masters`: list, get, set |
| **Speaker Notes (specialized)** | ✅ Complete | 2 tools | `speaker_notes`: get, set (Impress only — Draw has no speaker notes) |
| **Placeholders (core)** | ✅ Complete | 3 tools | List, get text, set text (Impress only) |
| **Transitions (specialized)** | ✅ Complete | 4 tools | `slide_transitions`: get/set transition, get/set layout |
| **Charts (specialized)** | ✅ Complete | 5 tools | Full CRUD + info |
| **Tree Structure (core)** | ✅ Complete | 1 tool | JSON DOM for LLM understanding |
| **Web Research (specialized)** | ✅ Complete | 1 tool | Delegated search |
| **Forms (specialized)** | ✅ Complete | 6 tools | Form controls (shared with Writer) |
| **Math (specialized)** | partial | 1 tool (`insert_math`) | LaTeX/MathML → OLE Math on slide; **bounding-box sizing still unreliable** — see [§2.3](#23-insert_math-math-domain) |
| **Animations** | ❌ Missing | — | Slide + shape-level animations |
| **Layers** | ❌ Missing | — | Draw layer management |
| **Slide Show** | ❌ Missing | — | Start, stop, presenter mode |
| **Media (Audio/Video)** | ❌ Missing | — | Insert, control |
| **Custom Shows** | ❌ Missing | — | Non-linear presentation paths |
| **Timings** | ❌ Missing | — | Rehearse, auto-advance |
| **Themes** | ❌ Missing | — | Color/font schemes |
| **Templates** | ❌ Missing | — | Document templates |
| **Headers/Footers (specialized)** | ✅ Complete | 2 tools | `get_headers_footers`, `set_headers_footers` (Impress only) |
| **Tables** | ✅ | same names as Writer | `table_insert`, list/get/set, `manage_table_structure` on TableShape |
| **3D Shapes** | ❌ Missing | — | 3D objects and scenes |
| **Guides/Grid** | ❌ Missing | — | Snap settings, custom guides |
| **OCR** | ❌ Missing | — | Text from images |
| **Export** | ❌ Missing | — | PDF, image, video export |
| **Macros** | ❌ Missing | — | Automation scripts |
| **Versioning** | ❌ Missing | — | Document history |

----

## 4. Service Coverage Notes

### 4.1 `uno_services` Fix Applied ✅

**Completed**: Added `"com.sun.star.presentation.PresentationDocument"` to `uno_services` for 9 core tools:

- `ListPages`, `GetDrawSummary`, `CreateShape`, `EditShape`, `ConnectShapes`, `GroupShapes`, `DeleteShape` (in `shapes.py`)
- `ReadSlideText`, `GetPresentationInfo` (in `pages.py`)

These tools now work with both Draw and Impress documents.

### 4.2 Impress-Only vs Delegated APIs

The following are **Impress-only** (Draw has no equivalent): speaker notes; slide placeholders; slide transitions and Impress slide layouts. They are exposed via **`delegate_to_specialized_draw_toolset`** with domains `speaker_notes` and `slide_transitions` (not on the default main-agent tool list).

**Slide master** tools (`list_master_slides`, `get_slide_master`, `set_slide_master`) work in both Draw and Impress but are **`slide_masters`** specialized tools—delegate when the user needs master assignment or listing beyond what `get_presentation_info` summarizes.

Core **placeholders** remain on the default list (`list_placeholders`, `get_placeholder_text`, `set_placeholder_text`).

> Speaker notes and transition/layout tools use `uno_services = ["com.sun.star.presentation.PresentationDocument"]` only. Master slide tools include both `DrawingDocument` and `PresentationDocument` where applicable.

### 4.3 Shared Tools (Draw + Impress + Other Types)

Some tools are implemented in shared modules but work with Draw/Impress:

- **Charts** (`plugin/draw/charts.py`): Chart tools work across all document types that support charts
- **Forms** (`writer/forms.py`): Form tools inherit from `ToolDrawFormBase` (`plugin/draw/base.py`) and work across document types that support form controls

> This document focuses on Draw/Impress-specific usage of these shared tools.

----

## 5. Roadmap

### 5.1 Priority 1: Fixes (High Impact, Low Effort)

| Task | Effort | Impact | Status |
|------|--------|--------|--------|
| Add `PresentationDocument` to `uno_services` for 9 tools | 1 hour | Unblocks Impress users from core shape/page tools | ✅ **Done** |
| Improve `insert_math` OLE shape sizing (`math_insert.py`) | Medium | Correct default box for formulas without model-supplied width/height | Open — see [§2.3](#23-insert_math-math-domain) |


### 5.2 Priority 2: High-Value Features & Technical Designs

| Feature | UNO Area | User Value | Effort |
|---------|----------|-------------|--------|
| **Batch Diagrams & Flowcharts** | `com.sun.star.drawing` | Single-turn diagram & flowchart creation | ✅ `create_diagram` |
| **Shape Alignment & Distribution** | `com.sun.star.drawing` | Clean, aligned, professional diagram layouts | ✅ `align_shapes` / `distribute_shapes` |
| **Tables** | TableShape + Model | Insert and edit tables | ✅ `table_insert` + Writer table_* names |
| **Slide Duplicate & Move** | `com.sun.star.drawing.DrawPages` | Reorder, copy, and organize slides | ✅ `duplicate_slide` / `move_slide` / `rename_slide` |
| **Images on slides** | Existing `image_*` tools | Logos and generated images | ✅ `image_insert` (`page`, `x_mm`, `y_mm` in millimetres) |
| **Slide Animations** | `com.sun.star.presentation.Animation*` | Professional presentation builds & entrances | Medium |
| **Slide Show Controls** | `com.sun.star.presentation.Presentation` | Start/stop presentation mode | Low |
| **Layers** | `com.sun.star.drawing.Layer*` | Advanced Draw organization | Medium |

---

#### 5.2.1 Batch Diagrams & Flowcharts (`batch_upsert_shapes` / `create_diagram`)

**Problem:** Generating a 4-step flowchart currently requires 8 sequential tool turns (`shape_upsert` $\times 4$, `shape_connect` $\times 4$).

**Design:** Add a batch creation tool to `domain="shapes"`:
```python
class CreateDiagram(ToolDrawShapeBase):
    name = "create_diagram"
    description = "Create a complete diagram/flowchart with multiple nodes and connectors in a single turn."
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)"},
            "layout": {"type": "string", "enum": ["horizontal_flow", "vertical_flow", "grid", "custom"], "default": "horizontal_flow"},
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Node identifier for connecting"},
                        "text": {"type": "string", "description": "Text content"},
                        "shape_type": {"type": "string", "default": "rectangle"},
                        "x": {"type": "integer", "description": "X position (1/100mm, optional in auto layouts)"},
                        "y": {"type": "integer", "description": "Y position (1/100mm, optional in auto layouts)"},
                        "width": {"type": "integer", "default": 4000},
                        "height": {"type": "integer", "default": 2000},
                        "fill_color": {"type": "string", "default": "#E8F0FE"},
                    },
                    "required": ["id", "text"]
                }
            },
            "connections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string", "description": "Source node ID"},
                        "to": {"type": "string", "description": "Target node ID"},
                        "line_color": {"type": "string", "default": "#1A73E8"}
                    },
                    "required": ["from", "to"]
                }
            }
        },
        "required": ["nodes"]
    }
```

---

#### 5.2.2 Shape Alignment & Distribution (`align_shapes`, `distribute_shapes`)

**Problem:** LLM-placed shapes are often slightly misaligned, creating amateurish diagrams.

**Design:** Add alignment tools to `domain="shapes"`:
```python
class AlignShapes(ToolDrawShapeBase):
    name = "align_shapes"
    description = "Align multiple shapes to an edge or center axis."
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index"},
            "indices": {"type": "array", "items": {"type": "integer"}, "description": "Shape indices to align"},
            "alignment": {
                "type": "string",
                "enum": ["left", "center_horizontal", "right", "top", "center_vertical", "bottom"],
                "description": "Alignment reference axis"
            }
        },
        "required": ["indices", "alignment"]
    }
```
**Implementation:**
- Reads bounding boxes of all target shapes via `getPosition()` / `getSize()`.
- Calculates reference anchor (e.g., minimum $X$ for `left`, average center $X$ for `center_horizontal`).
- Adjusts each shape's position using `setPosition(Point(new_x, new_y))`.

---

#### 5.2.3 Tables (`domain="tables"`)

**Problem:** Draw/Impress had no table tools. This is a slide table, not a Writer text table (`table_list` / `table_set_cell` / `manage_table_structure`). Those Writer tools are not reused here.

**Design:** Create a specialized domain `domain="tables"` (`plugin/draw/tables.py`):
```python
class InsertTable(ToolDrawSpecialBase):
    name = "table_insert"
    specialized_domain = "tables"
    description = "Insert a table onto the slide with specified rows and columns."
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based slide index"},
            "rows": {"type": "integer", "description": "Number of rows"},
            "columns": {"type": "integer", "description": "Number of columns"},
            "x": {"type": "integer", "description": "X position in 1/100mm (default: 3000)"},
            "y": {"type": "integer", "description": "Y position in 1/100mm (default: 4000)"},
            "width": {"type": "integer", "description": "Table width in 1/100mm (default: 20000)"},
            "height": {"type": "integer", "description": "Table height in 1/100mm (default: 10000)"},
            "data": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "2D array of cell strings: [[row0_col0, row0_col1], [row1_col0, ...]]"
            }
        },
        "required": ["rows", "columns"]
    }
```
**Implementation:**
- Create instance of `com.sun.star.drawing.TableShape`.
- After `page.add`, grow the TableShape model with row/column `insertByIndex` (Rows/Columns properties on a detached shape are unreliable), then populate cells via `getCellByPosition(col, row)`.

---

#### 5.2.4 Slide Lifecycle Operations (`duplicate_slide`, `move_slide`)

**Design:** Expose `DrawBridge.duplicate_slide` and `DrawBridge.move_slide` as first-class core tools in [`plugin/draw/pages.py`](plugin/draw/pages.py):
- `duplicate_slide(page: int, activate: bool = True)`
- `move_slide(from_page: int, to_page: int)`
- `rename_slide(page: int, name: str)`

---

#### 5.2.5 Images on slides (`image_insert`)

Use the existing Writer/Calc `image_*` tools (`domain="images"`). On Draw/Impress, `image_insert` places a graphic on a page via `_insert_image_to_drawpage`. Position and size are **millimetres** (`page`, `x_mm`, `y_mm`, `width_mm`, `height_mm`). Omitted `x_mm`/`y_mm` centers the image. Do not add a second Draw-only insert tool.

---

### 5.3 Priority 3: Specialized Domains

| Domain | Tools | Use Case |
|--------|-------|---------|
| `shapes` | `shape_upsert`, `create_diagram`, `align_shapes`, `distribute_shapes`, `shape_connect`, `shape_group` | Vector graphics & flowcharts |
| `images` | `image_insert`, `image_list`, `image_delete`, `image_generate` | Images on slides (millimetres) |
| `tables` | `table_insert`, `table_list`, `table_get_cells`, `table_set_cell`, `manage_table_structure` | Slide tables |
| `animations` | `get_animations`, `set_animations`, `add_animation` | Element entrance/motion builds |
| `slide_transitions` | `get_slide_transition`, `set_slide_transition` | Slide-to-slide advance effects |
| `slide_layouts` | `get_slide_layout`, `set_slide_layout` | Impress slide layouts |
| `slide_masters` | `list_master_slides`, `get_slide_master`, `set_slide_master` | Presentation templates |
| `speaker_notes` | `get_speaker_notes`, `set_speaker_notes` | Presenter talking points |
| `headers_footers` | `get_headers_footers`, `set_headers_footers` | Page numbers, dates, footers |
| `charts` | `manage_charts` | Data visualizations |
| `media` | `insert_audio`, `insert_video`, `control_media` | Multimedia presentations |
| `export` | `export_pdf`, `export_image`, `export_video` | Document output |

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
- `tests/draw/test_draw_uno.py` - Draw/Impress UNO shape and page coverage
- `tests/draw/test_draw_specialized_tiers.py` - Specialized tier registration
- `tests/draw/test_draw_forms_uno.py` - Forms
- `tests/draw/test_draw_headers_footers.py` - Headers/footers

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
- Custom shapes still need engine + `CustomShapeGeometry` **before** `page.add` (post-add Type swap can abort soffice)

The current `create_shape` implementation handles both cases with conditional logic.

### 8.3 Bridge Pattern

The `DrawBridge` class (`plugin/draw/bridge.py`) provides a unified interface for:
- Page/slide management (Draw/Impress `getDrawPages`)
- Shape creation
- Document navigation

For **shape tools**, the same bridge also wraps Writer `getDrawPage()` and the active Calc sheet `getDrawPage()` as a one-page collection. Slide insert/delete/duplicate still require a real Draw/Impress `XDrawPages`.

This pattern could be extended to other domains.

----

## 9. References

- [LibreOffice API Reference — Draw](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1drawing_1_1XDrawPage.html)
- [LibreOffice API Reference — Presentation](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1star_1_1presentation_1_1XPresentation.html)
- [LibreOffice Draw/Impress UNO Examples](https://wiki.documentfoundation.org/Documentation/DevGuide/Drawings/Tutorial)
- [Writer specialized toolsets](../writer/specialized-toolsets.md) — Architecture reference
- [AGENTS.md](../../AGENTS.md) — Project overview
