# LocalWriter MCP — How to work with LibreOffice documents

You have access to LocalWriter MCP tools. These tools let you read, edit, and manage documents open in LibreOffice.

## Step 1: Find the document

First, check if a document is already open:

```
get_document_info()
```

If no document is open, find one to work with:

```
list_open_documents()        # see all open documents
get_recent_documents()       # see recently opened files
open_document(file_path="/home/user/mydoc.odt")   # open a file
create_document(doc_type="writer")                 # create new empty document
```

## Step 2: Read the document

For Writer documents, get the structure first:

```
get_document_tree(depth=0)   # shows all headings with bookmark IDs
```

This returns headings with `_mcp_` bookmark IDs like `_mcp_h1`, `_mcp_h2`, etc. Use these IDs to read specific sections:

```
get_heading_content(heading_path="1")        # read first heading section
read_paragraphs(start=0, count=20)           # read first 20 paragraphs
read_paragraphs(locator="heading_text:Annexes", count=10)  # read from a heading by name
read_paragraphs(locator="bookmark:_mcp_h3", count=10)      # read from a bookmark ID
search_fulltext(query="budget")              # find text in document
```

## Step 3: Edit the document

Before editing, you need to unlock edit tools:

```
request_tools(intent="edit")
```

Now you can edit:

```
set_paragraph_text(index=5, text="New text here")
insert_at_paragraph(index=10, text="Inserted paragraph")
delete_paragraph(index=3)
replace_in_document(find="old text", replace="new text")
```

## Step 4: Save

```
save_document()                                    # save to current file
save_document_as(target_path="/home/user/new.odt") # save as new file
export_pdf(path="/home/user/output.pdf")           # export as PDF
```

## Important rules

1. **Always call `get_document_info` first** to know what document you are working with. If no document is open, call `get_recent_documents` to find one, then `open_document` to open it.
2. **Read before you edit.** Always read the content before changing it.
3. **Use locators for navigation.** Many tools accept a `locator` parameter. Use `bookmark:_mcp_h1` (from `get_document_tree`) or `heading_text:Introduction` (by heading name). These are more reliable than paragraph numbers.
4. **Call `request_tools(intent="edit")` before editing.** Edit tools are not available by default.
5. **Style names depend on language.** Call `list_styles(family="paragraph")` to see available style names before applying styles.

## Working with tables

```
list_tables()                          # see all tables
read_table(table_index=0)             # read first table
write_table_cell(table_index=0, cell="A1", value="Hello")  # write to cell
```

## Searching

```
search_fulltext(query="budget AND 2024")        # advanced search with AND, OR, NOT
find_text(search_string="exact phrase")         # find exact text with position
```

## Batch edits

To make multiple changes at once, use `execute_batch`:

```
execute_batch(operations=[
  {"tool": "set_paragraph_text", "args": {"index": 0, "text": "Title"}},
  {"tool": "set_paragraph_text", "args": {"index": 1, "text": "Subtitle"}},
  {"tool": "insert_at_paragraph", "args": {"index": 2, "text": "New paragraph"}}
])
```

## Other useful tools

- `get_document_stats()` — word count, page count, paragraph count
- `list_images()` / `insert_image()` — work with images
- `list_comments()` / `add_comment()` — work with comments
- `set_track_changes(enabled=true)` — enable change tracking before big edits
- `list_bookmarks()` — see all bookmarks in the document
