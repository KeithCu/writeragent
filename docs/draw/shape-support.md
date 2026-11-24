# LibreOffice UNO Shape Support

LibreOffice provides a comprehensive and flexible API for drawing shapes through the `com.sun.star.drawing` module. This document outlines the different shape types available in the UNO API and how they are exposed to the AI assistant.

## Base Shapes
The most common and simple shapes are implemented as direct UNO classes. These include:
- `com.sun.star.drawing.RectangleShape` (available by full UNO name; the `rectangle` alias creates a `CustomShape`)
- `com.sun.star.drawing.EllipseShape` (available by full UNO name; the `ellipse` alias creates a `CustomShape`)
- `com.sun.star.drawing.TextShape`
- `com.sun.star.drawing.LineShape` — an axis-aligned line has a zero width (vertical) or a zero height (horizontal). `shape_upsert` with `shape_type` `line` accepts that box. Other shapes still need both sides positive. A negative side, or both sides zero, is rejected (`DRAW_INVALID_SIZE`).
- `com.sun.star.drawing.ConnectorShape`

These shapes are straightforward to instantiate and configure. They have dedicated properties for styling (e.g., `FillColor`, `LineColor`).

## Custom Shapes (The Gallery of Shapes)
For more complex standard shapes like stars, smileys, arrows, and notably **octagons**, LibreOffice uses `com.sun.star.drawing.CustomShape`.

Instead of having a unique UNO class for every single geometric possibility (e.g., no `OctagonShape`), a `CustomShape` acts as a container whose geometry is defined by its properties.

To create one of these specific shapes:
1. Instantiate `com.sun.star.drawing.CustomShape`.
2. Set `CustomShapeEngine` to `com.sun.star.drawing.EnhancedCustomShapeEngine` and configure `CustomShapeGeometry` **before** `page.add`. This property accepts a sequence of `com.sun.star.beans.PropertyValue`.
3. Set a `PropertyValue` with `Name="Type"` and `Value="<shape_name>"`.
4. Add the shape to the drawing page, then set `CustomShapeGeometry` (`Type`) again after `page.add`. The pre-add write (`SvxShape::setPropertyValue` buffers `Type` in `maUrsAnys`, `svx/source/unodraw/unoshape.cxx:566`) ensures the inserted `SdrObjCustomShape` already has that `Type` instead of swapping a live default `SdrRectObj` (which can abort in `SfxItemPool::unregisterNameOrIndex`); the post-add write (`SvxCustomShape::setPropertyValue` → `MergeDefaultAttributes()`, `svx/source/unodraw/unoshap2.cxx:1733-1749`) expands the preset geometry (`ViewBox`, `Path`, `Equations`, `Handles`) so the shape paints across Writer, Calc, Draw, and Impress.

Writer-only: set `AnchorType=AT_PAGE` before add (already inside `safe_create_shape`). Fill/line properties stay after add.

Commonly supported `<shape_name>` values include:
- `octagon`
- `star5` (5-pointed star)
- `smiley`
- `heart`
- `cloud`
- `sun`
- `moon`
- `up-arrow`
- `diamond`
- `flowchart-process`

By leveraging `CustomShape`, the AI assistant can draw a vast array of geometric figures and symbols without needing to specify complex paths.

## Arbitrary Polygons and Paths (Advanced API)
For truly arbitrary shapes that don't fit into the predefined custom shape types, LibreOffice provides:
- `com.sun.star.drawing.PolyPolygonShape`: A shape defined by an array of arrays of points (allowing for holes).
- `com.sun.star.drawing.PolyLineShape`: A line defined by multiple points.
- `com.sun.star.drawing.PolyPolygonPathShape`: Similar to PolyPolygon but using bezier paths.
- `com.sun.star.drawing.PolyLinePathShape`: A multi-segment bezier line.

These require specifying the `Polygon` or `PolyPolygon` properties, which expect an array of `com.sun.star.awt.Point` structures. *(Note: Support for these advanced arbitrary polygon creations via AI tool calls may be implemented as a separate API in the future to keep standard shape creation simple and distinct).*

## WriterAgent Shape Implementation
The `shape_upsert` tool unifies the simple and CustomShape UNO APIs into a single interface. Its results carry `action` (`"create"` or `"edit"`) and the read-back `name` (`""` when unnamed), while `shape_connect`, `shape_group`, and `shape_delete` results carry the resolved `page` index. `shape_connect` and `shape_create_diagram` connections accept `line_start` and `line_end` (`"arrow"` or `"none"`) like `shape_upsert`.

When the user requests a `shape_type`:
1. **Base Aliases**: `text`, `line`, and `connector` map to the corresponding base UNO shape (`TextShape`, `LineShape`, `ConnectorShape`).
2. **UNO Classes**: If it matches a specific UNO class name, it instantiates that class directly.
3. **Custom Shapes**: If it's none of the above (including `"rectangle"`, `"ellipse"`, `"round-rectangle"`, `"octagon"`, or `"smiley"`), the tool instantiates a `CustomShape` and applies the requested string to the `Type` geometry property. Empty custom shapes are created with autogrow off (`TextAutoGrowHeight=False`, `TextAutoGrowWidth=False`) so later text writes do not resize the box.

The tool's JSON schema summarizes CustomShape types **by category** (with a few examples each); the full set is defined by LibreOffice (see `svx/source/customshapes/EnhancedCustomShapeTypeNames.cxx` in LibreOffice core). Any valid type string from that catalog can be passed as `shape_type`.

New shapes created by `shape_upsert` (and `shape_diagram`), `shape_connect`, `shape_group`, `table_insert` (Draw/Impress), and `math_insert` always carry a page-unique `Name` (`Shape<n>` when omitted on Draw, Impress, and Calc; Writer's `SwXDrawPage::add` already assigns one) and return it as `name`. Explicit duplicate `name` values on create or index-addressed rename are rejected (`INVALID_PARAM`). When `shape_upsert` (edit by `name`), `shape_delete`, `shape_connect`, `shape_group`, `shape_align`, or `shape_distribute` gets a Name that is not on the page or sheet, the error carries `details.available` (up to 40 shape Names on that page, group children included), `hint`, and `similar`.

### Text styling and box options

`shape_upsert` exposes six optional text and box parameters:

- `bold` / `italic`: forwarded to `apply_character_properties` (`CharWeight` 150.0 / 100.0 and `CharPosture` `FontSlant.ITALIC` / `NONE`).
- `h_align` (`left`, `center`, `right`, `justify`): sets `TextHorizontalAdjust=BLOCK` and then `ParaAdjust` via `para_adjust_to_uno`. Setting `BLOCK` is required so the text area spans the shape width and `ParaAdjust` aligns against the shape edges; raw or existing `RectangleShape` and `EllipseShape` primitives default to `TextHorizontalAdjust=CENTER`, which centres the text block at its own width and ignores left/right `ParaAdjust`. Custom shapes (including `rectangle` and `ellipse`) made by `shape_upsert` default to `center`, and `set_shape_string` (used by `shape_upsert`, `shape_fill_fields`, and `placeholder_set_text`) re-applies any direct `ParaAdjust` on the shape when writing text into a previously empty shape so the new paragraph inherits it.
- `v_align` (`top`, `center`, `bottom`): sets `TextVerticalAdjust` (`TOP`, `CENTER`, `BOTTOM`). Custom shapes (including `rectangle` and `ellipse`) made by `shape_upsert` default to `center` (same as `RectangleShape` and `EllipseShape`); `TextShape` remains top-left (`TextHorizontalAdjust=BLOCK`, `TextVerticalAdjust=TOP`).
- `wrap_text`: sets `TextWordWrap`. Custom shapes (including `rectangle` and `ellipse`) wrap by default in all four apps because `shape_upsert` sets it (LibreOffice's own default is off in Writer and Calc, `svx/source/svdraw/svdmodel.cxx:185`, and on in Draw and Impress, `sd/source/core/drawdoc4.cxx:270`); `TextShape` always wraps; raw or existing `RectangleShape` and `EllipseShape` primitives never wrap at a fixed `font_size` (they only wrap under the default `TextFitToSize=AUTOFIT`).
- `corner_radius`: sets `CornerRadius` in `1/100 mm` on primitive shapes (`RectangleShape`, plus text, circle, connector, and caption maps), or maps `rectangle` / `round-rectangle` custom shapes to the `round-rectangle` preset's `AdjustmentValues` (`round(21600 * radius / min(width, height))`, clamped to `10800`; `0` switches back to `rectangle` with square corners). Because the adjustment is relative to the shorter side, a later resize scales the corner radius until `corner_radius` is passed again. On other `CustomShape` presets (such as `ellipse`) it fails because `CustomShape` has no `CornerRadius` and is reported as a `corner_radius` warning.

## Grouping

`shape_group` (`GroupShapes` in `plugin/draw/shapes.py`, also the Writer and Calc subclasses) passes a temporary `com.sun.star.drawing.ShapeCollection` to `XDrawPage.group`. That service is the global implementation `com.sun.star.drawing.SvxShapeCollection`. Create it with the component-context service manager (`createInstanceWithContext`). Document `createInstance` does not provide it: Writer and Draw raise `ServiceNotRegisteredException`, and Calc returns None.

Writer shapes must not use an as-character anchor (`XShapeGrouper` rejects those). `shape_upsert` sets `AnchorType=AT_PAGE` before `page.add`, which grouping accepts.

`shape_group` returns the group's real page `index` (its topmost member's slot), read from `ZOrder`. Each shape may be passed once, whether by index or by Name; `shape_align` and `shape_distribute` enforce the same duplicate check. Grouping only Writer text frames is an error (`page.group` returns `None`).

## Edit Selection (Draw and Impress)

Extend Selection and Edit Selection (menu, hamburger, and rewrite slash presets) stream LLM output into the selected text shape(s). A partial in-shape text highlight still rewrites the **whole** shape string (`setString` replaces all text). Character formatting inside the shape is dropped on write. Groups are descended in index order (children only); table shapes are skipped (cells are not read). Several selected shapes queue one stream task each, capped at 250 shapes, under one compound undo step for `/undo`. With no qualifying text target, the user is asked to select a text shape (with an extra note when only a table was selected). An empty slide selection does not fall back to rewriting the whole slide.

## Shared Visual Helpers
Cross-document visual-object mechanics live in `plugin/doc/visual_helpers.py`. Writer, Calc, Draw, and Impress tools should reuse that module for safe UNO property access, document-kind detection, active draw-page lookup (including Writer `getDrawPage` for forms/shapes), graphic-object selection/listing, common `1/100 mm` unit conversions, color parsing (`parse_color_to_uno_int`), and Char* property batching (`apply_character_properties`). App-specific behavior still belongs in the app modules, especially Writer text-cursor image insertion and Writer page-anchored shape quirks.
