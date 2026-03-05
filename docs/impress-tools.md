# Impress Tools Sketch & Roadmap

This document outlines the vision and toolset for the **Impress Agent**, which enables AI-driven presentation creation, editing, and delivery. It leverages and extends the existing Draw document infrastructure.

## 1. Current Capabilities (Shared with Draw)

The Impress agent can currently perform basic slide and shape manipulation:

- **Shape Management**:
  - `create_shape`: Create rectangles, ellipses, text boxes, and lines.
  - `edit_shape`: Move, resize, recolor, and change text of existing shapes.
  - `delete_shape`: Remove shapes.
  - `get_draw_summary`: List all shapes and their properties on the current slide.
- **Slide Management**:
  - `list_pages`: List all slides in the presentation.
  - `add_slide`: Insert a new slide.
  - `delete_slide`: Remove a specific slide.

## 2. Advanced Slide & Layout Management (Near Term)
Powerful structural manipulation for professional decks:

- `add_slide(index, layout)`: Insert a new slide with a chosen layout (e.g., "Title Slide", "Title and Content").
- `reorder_slides(from_index, to_index)`: Change slide sequence.
- `duplicate_slide(index)`: Clone slides for rapid templating.
- **Master Slides**:
  - `list_master_slides()`: List available designs.
  - `apply_master_slide(master_name, page_index)`: Change the theme of a slide.

## 3. High-Fidelity Content & Context (Medium Term)
Deep understanding of presentation semantics:

- **Speaker Notes**:
  - `get_notes(page_index)`: Read existing scripts (currently in context).
  - `set_notes(text, page_index)`: Draft or refine speaker scripts.
- **Semantic Extraction**:
  - `get_slide_content_full(page_index)`: Returns structured text (Title, Bullets, Subtitles) instead of raw shape lists.
- **Formatting**:
  - `apply_conditional_layout(data_type)`: Auto-adjust layout based on content (e.g., "Comparison" if two objects are sent).
  - `create_table`: Native Impress table support.

## 4. Generative Workflows & AI Intelligence (Long Term)
End-to-end presentation generation:

- **Outline to Deck**: Transform a Writer outline or a transcript directly into a multi-slide presentation.
- **Presenter Logic**: AI-driven "slide audits" for text density, contrast, and visual balance.
- **Generative Assets**:
  - **Image-Driven Slides**: Automatically generate relevant DALL-E/Stable Diffusion visuals for slide content.
  - **Style Themes**: AI-generated color palettes and typography pairings.
- **Interactive Controls**: `start_presentation`, `goto_slide`, and basic navigation.

## AI Guidance

The Impress agent should follow a "Think-Structure-Design" loop:
1. **Understand**: Parse user intent and document context (notes vs. content).
2. **Inspect**: Use `get_draw_summary` to see what's on the slide.
3. **Draft**: Create structure via slide/shape tools.
4. **Refine**: Apply layouts and aesthetics.

---

> [!IMPORTANT]
> **Implementation Note**: Prefer using high-level presentation services (`com.sun.star.presentation`) when available, while falling back to the robust drawing layer for fine-grained shape control.
