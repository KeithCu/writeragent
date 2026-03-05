# Goal Description

The codebase currently has a large number of fine-grained tools exposed to the LLM for managing various document entities (e.g., separate tools for listing, getting info, adding, deleting, modifying). This bloats the tool list and can overwhelm the model with too many options.
The goal is to simplify without removing features by grouping similar APIs into single tools with an `action` or `operation` enumeration parameter.

## Proposed Changes

We can merge the following tool groups by converting multiple classes into single classes that dispatch to the appropriate behavior based on an `action` parameter.

### 1. Paragraph Management ([content.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py))
Combine `read_paragraphs`, `insert_at_paragraph`, `modify_paragraph`, `delete_paragraph`, `duplicate_paragraph`, `insert_paragraphs_batch`, and `clone_heading_block` into a single tool:
**`manage_paragraphs`**
- `action` enum: `["read", "insert", "modify", "delete", "duplicate", "insert_batch", "clone_heading_block"]`
- Reduces 7 tools down to 1.

### 2. Comments Management ([comments.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/comments.py))
Combine `list_comments`, `add_comment`, `delete_comment`, `resolve_comment`, `add_ai_summary`, `get_ai_summaries`, and `remove_ai_summary` into a single tool:
**`manage_comments`**
- `action` enum: `["list", "add", "delete", "resolve", "add_ai_summary", "get_ai_summaries", "remove_ai_summary"]`
- Reduces 7 tools down to 1.

### 3. Image Management ([images.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/images.py))
Combine `list_images`, `get_image_info`, `set_image_properties`, `download_image`, `insert_image`, `delete_image`, and `replace_image` into a single tool:
**`manage_images`**
- `action` enum: `["list", "get_info", "set_properties", "download", "insert", "delete", "replace"]`
- *Note:* `generate_image` and `edit_image` should probably remain separate because they invoke AI modals and have vastly different prompt/parameter signatures.
- Reduces 7 tools down to 1.

### 4. Styles Management ([styles.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/styles.py))
Combine `list_styles` and `get_style_info` into a single tool:
**`manage_styles`**
- `action` enum: `["list", "get_info"]`
- Reduces 2 tools down to 1.

### 5. Search Management ([search.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/search.py))
Combine `search_in_document` and `replace_in_document` into a single tool:
**`search_document`**
- `action` enum: `["search", "replace"]`
- Reduces 2 tools down to 1.

### 6. Tracked Changes ([tracking.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/tracking.py))
Combine `set_track_changes`, `get_tracked_changes`, and `manage_tracked_changes` into a single tool:
**`manage_tracked_changes`**
- `action` enum: `["set_state", "get", "accept", "reject"]`
- Reduces 3 tools down to 1.

### 7. Outline Management ([outline.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/outline.py))
Combine `get_document_tree` and `get_heading_children` into a single tool:
**`manage_outline`**
- `action` enum: `["tree", "children"]`
- Reduces 2 tools down to 1.

### 8. Text Frames ([frames.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/frames.py))
Combine `list_text_frames`, `get_text_frame_info`, and `set_text_frame_properties` into:
**`manage_text_frames`**
- `action` enum: `["list", "get_info", "set_properties"]`
- Reduces 3 tools down to 1.

### 9. Structural & Navigation ([structural.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/structural.py), [navigation.py](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/navigation.py))
Instead of `list_sections`, `read_section`, `goto_page`, `get_page_objects`, `refresh_indexes`, `resolve_bookmark`, `update_fields`, `list_bookmarks`, etc., we can group these into:
- **`manage_sections`** (`list`, `read`)
- **`manage_bookmarks`** (`list`, [resolve](file:///home/keithcu/Desktop/Python/localwriter/plugin/modules/writer/content.py#883-897))
- **`manage_document_indexes`** (`refresh`, `update_fields`)

## User Review Required

Please review these groupings. If they look good, I will refactor the files to combine the `ToolBase` subclasses and consolidate their parameter schemas.

## Verification Plan

### Automated Tests
- Run `pytest plugin/modules/core/format_tests.py` (and any other relevant writer tool tests).
- Start LibreOffice with the extension loaded from the source folder and make sure MCP server endpoint `/tools` reports the correct consolidated schemas.

### Manual Verification
- Provide instructions and commands to spin up the local plugin and test formatting a document via the chat interface to verify paragraph operations and comment management.
