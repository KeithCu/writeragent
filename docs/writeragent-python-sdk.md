# WriterAgent Python Script SDK

The `writeragent` Python SDK provides high-level, Python-native access to LibreOffice document automation tools. It wraps the extension's `ToolRegistry` and exposes all major capabilities (Calc, Writer, Draw, Bookmarks, Comments, etc.) as clean, autocompletable Python modules.

## Dual Execution Modes

The SDK operates transparently in two modes depending on where the script runs:

1. **In-Process Mode**: Used when executing scripts directly within the LibreOffice host (e.g., embedded stdlib scripts). Calls bypass serialization and invoke tools directly using the host's `ToolRegistry` and active `ToolContext`.
2. **Out-of-Process Mode**: Used when executing scripts inside the sandboxed python venv worker (e.g., for code using numpy/pandas/matplotlib). The SDK automatically packs arguments, initiates a secure length-prefixed pickle IPC call to the LibreOffice host, blocks for main-thread execution, and returns the result.

## Basic Usage

To use the SDK, import it in your LibreOffice Python scripts:

```python
import writeragent as wa

# List all sheets in the active Calc workbook
sheets_info = wa.sheet.list_sheets()
print("Sheets:", sheets_info.get("sheets"))
```

## Namespace Proxies

The SDK organizes tools into intuitive namespace modules:

### 1. Calc Spreadsheet Automation (`wa.sheet`, `wa.calc`, `wa.conditional_formatting`)
Read, write, and manipulate cells, ranges, sheets, and formats:

```python
import writeragent as wa

# Create a new sheet
wa.sheet.create_sheet(sheet_name="SalesData")

# Write formulas to a cell range
wa.calc.write_formula_range(
    range_name="SalesData.A1:B2",
    formulas=[["Month", "Revenue"], ["Jan", 15000]]
)

# Read cell range values back
result = wa.calc.read_cell_range(range_name="SalesData.B2:B2")
print("January Revenue:", result.get("data"))
```

### 2. Writer Document Automation (`wa.writer`, `wa.bookmark`, `wa.comment`)
Read, insert, and search text, styles, and annotations in text documents:

```python
import writeragent as wa

# Insert heading and content
wa.writer.apply_document_content(
    content=[
        {"type": "heading", "level": 1, "text": "Q2 Performance Report"},
        {"type": "paragraph", "text": "This report details our performance."}
    ]
)

# Add a bookmark
wa.bookmark.create_bookmark(name="Q2Summary", paragraph_index=0)

# List all bookmarks
print("Bookmarks:", wa.bookmark.list_bookmarks())
```

### 3. Drawing and Presentation Automation (`wa.draw`, `wa.shape`)
Insert and manipulate shapes, slides, and layouts in Draw and Impress:

```python
import writeragent as wa

# Add a new slide to the active presentation
wa.draw.add_slide(slide_name="MarketAnalysis")

# Read text from slide placeholders
slide_text = wa.draw.read_slide_text(page=1)
print("Slide 1 Text:", slide_text)
```

### 4. Analysis and Helpers (`wa.analysi`, `wa.python`, `wa.web_research`)
Perform advanced SQL queries, run mathematical optimization, or fetch web research:

```python
import writeragent as wa

# Execute read-only SQL queries against Calc sheets or files
sql_res = wa.analysi.query_folder_sql(
    sql="SELECT Month, Revenue FROM SalesData WHERE Revenue > 10000"
)
print("High performing months:", sql_res.get("result"))

# Query public web search for context
search_res = wa.core.web_research(query="LibreOffice API updates 2026")
print("Research:", search_res)
```

## Monaco Editor Autocompletion

When using the built-in Monaco Python Editor (**Tools -> Run Python Script...**), autocompletion and type hinting for the `writeragent` namespace are served dynamically in the background via a persistent `jedi` environment running in the child process.

Type `wa.` or `writeragent.` inside the editor to explore all available methods and view their docstrings.
