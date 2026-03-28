# LibreOffice UNO Shape Support

LibreOffice provides a comprehensive and flexible API for drawing shapes through the `com.sun.star.drawing` module. This document outlines the different shape types available in the UNO API and how they are exposed to the AI assistant.

## Base Shapes
The most common and simple shapes are implemented as direct UNO classes. These include:
- `com.sun.star.drawing.RectangleShape`
- `com.sun.star.drawing.EllipseShape`
- `com.sun.star.drawing.TextShape`
- `com.sun.star.drawing.LineShape`
- `com.sun.star.drawing.ConnectorShape`

These shapes are straightforward to instantiate and configure. They have dedicated properties for styling (e.g., `FillColor`, `LineColor`).

## Custom Shapes (The Gallery of Shapes)
For more complex standard shapes like stars, smileys, arrows, and notably **octagons**, LibreOffice uses `com.sun.star.drawing.CustomShape`.

Instead of having a unique UNO class for every single geometric possibility (e.g., no `OctagonShape`), a `CustomShape` acts as a container whose geometry is defined by its properties.

To create one of these specific shapes:
1. Instantiate `com.sun.star.drawing.CustomShape`.
2. Add it to the drawing page.
3. Configure its `CustomShapeGeometry` property. This property accepts a sequence of `com.sun.star.beans.PropertyValue`.
4. Set a `PropertyValue` with `Name="Type"` and `Value="<shape_name>"`.

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
The `CreateShape` tool unifies the simple and CustomShape UNO APIs into a single interface.

When the user requests a `shape_type`:
1. **Base Aliases**: If it's a known simple alias (e.g., `"rectangle"`, `"ellipse"`), it maps to the corresponding base UNO shape (`RectangleShape`, `EllipseShape`).
2. **UNO Classes**: If it matches a specific UNO class name, it instantiates that class directly.
3. **Custom Shapes**: If it's none of the above, the tool assumes the user is requesting a specific `CustomShape` geometry type (like `"octagon"` or `"smiley"`). It will instantiate a `CustomShape` and apply the requested string to the `Type` geometry property.

The tool's JSON schema summarizes CustomShape types **by category** (with a few examples each); the full set is defined by LibreOffice (see `svx/source/customshapes/EnhancedCustomShapeTypeNames.cxx` in LibreOffice core). Any valid type string from that catalog can be passed as `shape_type`.
