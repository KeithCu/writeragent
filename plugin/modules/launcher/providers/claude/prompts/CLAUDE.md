You are working with a LibreOffice document through **LocalWriter MCP**.

## Quick start

1. Call `get_document_info` to know the document type (writer, calc, draw).
2. For **Writer** documents, call `get_document_tree` (depth=0) to see the full heading structure. Use the returned `_mcp_` bookmarks for stable addressing — prefer `bookmark:_mcp_xxx` locators over paragraph indices.
3. Drill into sections with `get_heading_children` or `read_paragraphs`.
4. Use `search_fulltext` for boolean queries (AND, OR, NOT, NEAR/N).

## Document management

- **`get_recent_documents`** — List recently opened documents from LibreOffice history.
- **`open_document`** — Open a file by its absolute path.
- **`create_document`** — Create a new empty document (writer, calc, draw, impress).
- **`save_document`** — Save the current document (must already have a file path).
- **`save_document_as`** — Save a copy to a new path (also works for unsaved documents).
- **`export_pdf`** — Export the current document as PDF.
- **`close_document`** — Close the current document.
- **`list_open_documents`** — See all currently open documents.

You can work with unsaved documents — just use `save_document_as` to save them when needed.

## Exploring large documents

For large documents, use a top-down approach to avoid reading too much content at once:

1. `get_document_tree(depth=1)` — Get top-level headings only (with `_mcp_` bookmarks).
2. `get_heading_children(locator="bookmark:_mcp_xxx")` — Drill into a specific section.
3. `get_heading_content(heading_path="2.3")` — Read content under a heading by path (e.g., 3rd child of 2nd heading).
4. `search_fulltext(query="...")` — Search across the entire document without reading it all.
5. `get_document_stats` — Check document size (word count, page count) before deciding how to read.

This lets you navigate documents of any size efficiently without hitting context limits.

## Key concepts

- **Locators** — Most tools accept a `locator` parameter. Prefer stable forms: `bookmark:_mcp_xxx` or `heading_text:Title`. Use `paragraph:N` only as a fallback.
- **Tool tiers** — Core tools are always available. Extended tools require activation: call `list_available_tools` then `request_tools(intent="edit")` to unlock editing tools.
- **Intent groups** — `navigate`, `edit`, `review`, `media`. Request the group you need.
- **Batch operations** — Use `execute_batch` to chain multiple edits efficiently. Use `$last` to reference the previous step's paragraph index.

## Common patterns

- **Read before edit** — Always read the target content before modifying it.
- **Track changes** — For significant edits, enable `set_track_changes(enabled=true)` first.
- **Style names are localized** — Call `list_styles` to discover exact names before applying styles.
- **Tables** — Use `list_tables` → `read_table` → `write_table_cell` (Excel-style A1 refs).

## Use `/localwriter` for a quick reference cheatsheet.
