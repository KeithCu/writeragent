# LibreOffice DOM (LO-DOM) & Semantic Trees

WriterAgent introduces the concept of a **LibreOffice Document Object Model (LO-DOM)** to help AI agents understand complex visual and hierarchical layouts natively, without relying on expensive and opaque image-based screenshots.

## The Problem with Screenshots

Initially, it seems logical that an AI agent should "see" a diagram by taking a screenshot and running it through a vision model (like Gemini 1.5 Flash or Gemini 3.1 Flash-Lite). However, this approach has several drawbacks:

1. **Token Cost:** A 1920x1080 screenshot can consume upwards of 1,500 tokens. While cheaper models make this affordable, it still bloats the context window unnecessarily.
2. **Opacity:** Vision models "guess" connections and relationships. A line between two boxes is just pixels; the model must infer that it's a logical connection.
3. **Inactionable:** If a model sees an error in a screenshot, it cannot easily say "move that box left by 10 pixels." It lacks the underlying structural identity of the object.

## The LO-DOM Solution

LibreOffice maintains a rigorous structural hierarchy of its documents via the UNO API. By extracting this into a semantic JSON representation, we provide the model with a "scene graph."

### Draw and Impress (Draw Tree)

The `get_draw_tree` tool extracts the hierarchical structure of a Draw page or Impress slide. It translates raw UNO objects (`com.sun.star.drawing.RectangleShape`) into semantic JSON nodes.

**Features of the Draw Tree:**
* **Hierarchy:** Grouped shapes become parent nodes with nested children.
* **Spatial Geometry:** Extracts precise `x`, `y`, `width`, and `height` properties.
* **Semantic Attributes:** Reads the underlying `text`, `name`, `alt_title`, and `alt_description`.
* **Relational Logic:** For `ConnectorShape`s, it directly extracts the `StartShape` and `EndShape`, proving unambiguous flow logic for flowcharts.
* **Visual Style:** Captures `FillColor`, `LineColor`, and `ZOrder`.

### Writer (Heading Tree)

Writer documents use a different hierarchy based on `OutlineLevel`. The `writer_tree` tool constructs a navigable index of headings and body paragraphs. This prevents the model from needing to read 100 pages of text linearly. Instead, it "skims" the table of contents and zooms into specific sections.

### Writer (Page-by-Page Context & Embedded Shapes)

While the `writer_tree` extracts the heading structure, language models suffer from the "Needle in a Haystack" problem when fed too much data. To solve this, WriterAgent employs a **Page-by-Page Strategy**.

The `get_page_objects` tool jumps the view cursor to a specific physical page and extracts a highly concentrated, multimodal JSON snapshot:
*   **Paragraphs:** The exact text visible on that physical page.
*   **Images & Tables:** Metadata about anchored graphical objects and text tables.
*   **Embedded Draw Shapes:** Writer documents contain a hidden "draw page" layer (`doc.getDrawPage()`). The tool cross-references shape anchors (`AT_PAGE`, `AT_PARAGRAPH`) with the boundaries of the physical page. It then extracts the exact same semantic LO-DOM (geometry, text, names) for flowcharts and diagrams embedded *within* the text report.

This allows the agent to safely scale its understanding of massive documents by asking: "What am I looking at on Page 12?"

## Future Extensions

### Calc Charts and Form Controls

Similarly, Calc spreadsheets maintain their own Draw layer for charts, form controls, and shapes. Exposing this through the LO-DOM pattern would allow the agent to understand dashboard layouts, spatial organization, and chart metadata programmatically, without requiring screenshots of the spreadsheet.