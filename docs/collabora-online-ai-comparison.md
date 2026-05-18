# Architectural Analysis & Dev Guide: Collabora Online AI vs. WriterAgent

> [!NOTE]
> **Source Repository Location**: `/home/keithcu/Desktop/collaboffice`

This document provides a comparative analysis of the AI features and underlying architectures between **Collabora Online's Server-Driven AI** (implemented in `coolwsd` under `wsd/AIChatSession`) and **WriterAgent's Client-Side Plugin Architecture**. It identifies features to consider adopting and provides concrete, copy-pasteable PyUNO Python implementations of these features.

---

## Architectural Comparison

```mermaid
graph TD
    subgraph Collabora Online (Server-Driven)
        A[Browser Client / Sidebar UI] <-->|WebSockets: aichat, aichatapprove| B[coolwsd C++ Daemon]
        B -->|Async HTTP| C[External LLM API]
        B <-->|Kit Protocol| D[ChildSession C++ Kit]
        D <-->|C++ LOKit API| E[LibreOffice Core Engine]
    end

    subgraph WriterAgent (Client-Side Plugin)
        F[LibreOffice UI / Python Sidebar Panel] <-->|Direct Method Calls| G[WriterAgent Python Core]
        G -->|Async Background Thread| H[LlmClient Python HTTP]
        H <-->|HTTPS| I[External LLM API]
        G <-->|Direct PyUNO Bridge| J[Local LibreOffice In-Process DOM]
    end
    
    style B fill:#f9f,stroke:#333,stroke-width:2px
    style G fill:#bbf,stroke:#333,stroke-width:2px
```

### Key Differences at a Glance

| Architectural Dimension | Collabora Online AI (`coolwsd` + `coolkit`) | WriterAgent (Python Sidebar Plugin) |
| :--- | :--- | :--- |
| **Orchestration Location** | **Server-side** inside the C++ `coolwsd` daemon (`AIChatSession.cpp`). | **Client-side** directly inside the user's LibreOffice instance. |
| **Document Interaction Bridge** | **Protocol Serialization**: Messages are serialized to protocol text (`extractdocumentstructure`, `transformdocumentstructure`) and sent down via IPC sockets to jail-sandboxed child processes (`ChildSession.cpp`) to run LOKit APIs. | **In-Process Object Bridge**: Direct, high-speed Python-to-C++ interaction using the **PyUNO bridge**, accessing the rich UNO service graph directly. |
| **Tool Calling Loop (FSM)** | Driven by a multi-round loop inside C++ using `Poco::JSON` and asynchronous HTTP clients (`http::Session`). | Driven by Python FSM threads using standard library threading/queues and custom MCP configurations. |
| **User Approvals** | Managed via WebSocket messages: server pushes an `aichatapproval:` frame containing a summary, client responds with `aichatapprove:` containing `action="approve\|reject"`. | Managed locally using modal/non-modal UNO settings dialogs and FSM UI hooks. |
| **Security & Jailing** | Uses isolated child processes (`coolkit` mount namespaces) to jail documents, preventing malicious or broken actions from affecting other users. | Relies on the host operating system's standard permissions for the LibreOffice application. |

---

## Features We Should Consider Adopting

Collabora Online has implemented several robust, structured AI interactions. Since WriterAgent runs in-process with a full PyUNO bridge, we can implement these features **more efficiently and natively** without the overhead of WebSocket serialization.

### 1. Spreadsheet Function Discovery (`list_calc_functions`)
*   **The Feature**: The LLM needs to know what Calc functions are available in the current sheet to prevent it from hallucinating non-existent formulas or using incorrect localized function names.
*   **Collabora's Path**: Sends `.uno:CalcFunctionList` down to LOKit and parses the returned JSON string.
*   **WriterAgent Path**: Query `com.sun.star.sheet.FunctionDescriptions` natively via PyUNO to build a robust, dynamic function signature list for the LLM!

### 2. Calc Formula Pre-evaluation (`evaluate_formula`)
*   **The Feature**: Evaluates a spreadsheet formula *before* writing it to the document, returning the result to the LLM. This prevents broken formulas (`#VALUE!`, `#NAME?`) from entering the sheet during multi-step tasks.
*   **Collabora's Path**: Dispatches `.uno:EvaluateFormula?cell=C5&formula==SUM(A1:B2)` and catches the result.
*   **WriterAgent Path**: Write a clean Python utility that sets a formula on a temporary hidden sheet or cell, reads the evaluated type/result, and immediately undoes or clears the cell!

### 3. Structured Slide Transformations (`transform_document_structure`)
*   **The Feature**: Instead of sending ad-hoc commands, the LLM generates a single unified JSON transaction mapping slides to slide indices, slide layout IDs, and content shapes (e.g. `ChangeLayoutByName: "AUTOLAYOUT_TITLE_CONTENT"`, `SetText.1: "bullet 1\nbullet 2"`).
*   **Collabora's Path**: Parses the JSON transformation in `AIChatSession.cpp`, prepares placeholder assets, and pushes the layout change.
*   **WriterAgent Path**: Build a highly stable Impress Slide Transformation Tool that processes slide addition, deletion, rearrangement, layout setting, and text formatting in a single, atomic operation.

### 4. Progressive Slide Layout & Image Sequencing
*   **The Feature**: When generating complex slide layouts containing AI-generated images, presentation structures are modified *instantly* with placeholder graphics (e.g. gray boxes or spinners) so that layout editing remains ultra-responsive. The actual image generation tasks are queued and processed asynchronously in the background. As each image finishes downloading, the placeholder is hot-swapped for the final asset without locking up the user interface.
*   **Collabora's Path**: Scans the transformation schema for `GenerateImage` requests, places loading placeholders, applies the main slide structure, queues generations via their server-side worker, and pushes a patch transform as each finishes.
*   **WriterAgent Path**: Execute layout modifications and insert named placeholder shapes immediately, then hand off image generation tasks to background worker threads, replacing each shape's graphic target on the main thread as they complete.

### 5. Dynamic Link Target Mapping (`extract_link_targets`)
*   **The Feature**: Allows the LLM to inspect the active document and compile a dictionary of anchor targets (such as sections, tables, text frames, images, headings, and bookmarks) formatted as standard target addresses (e.g. `Heading1|outline`). This lets the AI create exact document hyperlinks or build a custom table of contents.
*   **Collabora's Path**: Invokes `_docManager->getLOKit()->extractRequest(...)` which generates a JSON-serialized list of targets.
*   **WriterAgent Path**: Query local DOM collections (`getBookmarks()`, `getTextTables()`, etc.) directly via PyUNO and compile a mapping natively.

### 6. Document Outline / Structure Extraction (`extract_document_structure`)
*   **The Feature**: The LLM needs a unified structured view of the entire document tree to understand where text, headings, sections, worksheets, or slides are positioned prior to performing complex transformations.
*   **Collabora's Path**: Calls `extractDocumentStructureRequest(...)` in the Kit child session and returns a stringified JSON layout.
*   **WriterAgent Path**: Build a lightweight Python inspector that queries document elements (e.g. paragraph enumeration for Writer, sheets for Calc, slides and shape types for Impress) to output a clean, standard JSON outline.

### 7. Transactional Undo Context Grouping (`XUndoManager` Integration)
*   **The Feature**: When the LLM performs a sequence of multiple actions (like batch cell editing, slide insertions, or styled text generation), these actions should not flood the user's Undo history as separate, primitive actions. They must be grouped into a single, labeled transaction.
*   **Collabora's Path**: Implicitly grouped via LOKit core dispatches or single postUnoCommand execution.
*   **WriterAgent Path**: Access the document's `XUndoManager` natively and wrap the entire LLM tool execution logic in an entry/exit context. This ensures that the user can undo the entire AI operation cleanly with a single "Ctrl+Z"!

### 8. Dynamic AI Model Discovery (`fetch_models`)
*   **The Feature**: The plugin dynamically queries the available AI models directly from the user's active API provider. This populates selection dropdowns with newly released models automatically without requiring hardcoded static arrays in the extension.
*   **Collabora's Path**: Exposes a `/fetch-models` endpoint that proxies queries directly to standard `/v1/models` LLM endpoints.
*   **WriterAgent Path**: Implement a standard `LlmClient` request to query the configured provider's `/v1/models` route in Python and populate the settings UI dropdown list dynamically.

### 9. Host Allowlisting & Security Safeguards (SSRF Protection)
*   **The Feature**: Restricts custom AI model server URLs to approved, verified domains or corporate proxies to protect against Server-Side Request Forgery (SSRF) and data exfiltration inside locked enterprise environments.
*   **Collabora's Path**: Checks custom provider base URLs against a `KIT_HOST_ALLOWLIST` regex environment variable before dispatching HTTP calls.
*   **WriterAgent Path**: Add domain validation logic in our settings manager or HTTP client before querying external models, restricting calls strictly to trusted platforms (OpenAI, Anthropic, Groq) or corporate gateways.

---

## Bits of Code to Use (Python PyUNO Adaptations)

Here is how to translate Collabora Online's C++ LOKit commands into **native Python PyUNO code** that you can integrate directly into your specialized toolsets.

### A. Dynamic Calc Function Catalog (`list_calc_functions`)

Instead of parsing string-based JSON lists, we can build a dynamic catalog by accessing the LibreOffice Service Manager's `FunctionDescriptions` registry.

```python
def get_calc_function_catalog(ctx) -> list[dict[str, Any]]:
    """Queries LibreOffice Calc function descriptions and returns a structured list.
    
    This is highly useful for LLM system prompts to prevent hallucinating functions
    or utilizing incorrect locales.
    """
    smgr = ctx.ctx.ServiceManager
    # Access the function descriptions service
    func_descr_service = smgr.createInstanceWithContext(
        "com.sun.star.sheet.FunctionDescriptions", ctx.ctx
    )
    
    catalog = []
    
    # FunctionDescriptions implements XIndexAccess to list all available formulas
    for i in range(func_descr_service.getCount()):
        try:
            # Each element is a PropertyValue sequence representing com.sun.star.sheet.FunctionDescription
            props = func_descr_service.getByIndex(i)
            func_data = {}
            for prop in props:
                func_data[prop.Name] = prop.Value
            
            # Extract key metadata
            name = func_data.get("Name", "")
            description = func_data.get("Description", "")
            category = func_data.get("Category", 0)  # Numeric category ID
            arguments = func_data.get("Arguments", ())  # Tuple of com.sun.star.sheet.FunctionArgument info
            
            arg_list = []
            for arg in arguments:
                arg_list.append({
                    "name": arg.Name,
                    "description": arg.Description,
                    "optional": arg.IsOptional
                })
            
            catalog.append({
                "name": name,
                "description": description,
                "category_id": category,
                "arguments": arg_list
            })
        except Exception as e:
            continue
            
    return catalog
```

### B. Formula Pre-evaluation (`evaluate_formula`)

To evaluate a formula string in Calc without modifying the user's undo stack or mutating their visual workspace, we can execute the formula on a temporary, hidden worksheet.

```python
import datetime

def evaluate_calc_formula(ctx, formula_string: str) -> dict[str, Any]:
    """Evaluates a Calc formula without mutating the document.
    
    Returns the result value, result type, or error information.
    """
    if not formula_string.startswith("="):
        formula_string = "=" + formula_string
        
    doc = ctx.doc
    sheets = doc.getSheets()
    sheet_names = sheets.getElementNames()
    
    # 1. Create a unique temporary sheet name
    temp_sheet_name = f"__wa_eval_{int(datetime.datetime.now().timestamp())}__"
    
    try:
        # 2. Insert sheet at the very end
        sheets.insertNewByName(temp_sheet_name, len(sheet_names))
        temp_sheet = sheets.getByName(temp_sheet_name)
        
        # 3. Target cell A1 on our evaluation sheet
        cell = temp_sheet.getCellByPosition(0, 0) # position A1
        
        # 4. Set formula (Calc evaluates this immediately in-memory)
        cell.setFormula(formula_string)
        
        # 5. Extract results
        result_type = cell.getType() # com.sun.star.table.CellContentType
        formula_result = None
        error_code = cell.Error
        
        from com.sun.star.table.CellContentType import VALUE, TEXT
        
        if error_code != 0:
            return {
                "status": "error",
                "error_code": error_code,
                "message": f"Formula evaluation error code: {error_code}"
            }
            
        if result_type == VALUE:
            formula_result = cell.getValue()
        elif result_type == TEXT:
            formula_result = cell.getString()
        else:
            # Reading formula result if it evaluates as a complex formula content
            formula_result = cell.getString() # fallback to formatted string
            
        return {
            "status": "ok",
            "formula": formula_string,
            "result": formula_result,
            "result_type": str(result_type)
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to evaluate formula: {e}"
        }
    finally:
        # 6. Cleanup temporary evaluation sheet (removes all traces from document)
        try:
            if sheets.hasByName(temp_sheet_name):
                sheets.removeByName(temp_sheet_name)
        except Exception as cleanup_err:
            log.error(f"Failed to cleanup evaluation sheet: {cleanup_err}")
```

### C. Unified Slide layout engine (`transform_document_structure`)

Porting Collabora's structured slide modifications to Python allows the LLM to design presentations easily. In PyUNO, we set a slide's layout ID directly on the `DrawPage` via its `Layout` property.

#### Layout IDs Map (LibreOffice Core Constants)
```python
IMPRESS_LAYOUTS = {
    "AUTOLAYOUT_TITLE": 0,                   # Title + Subtitle
    "AUTOLAYOUT_TITLE_CONTENT": 1,           # Title + 1 Content area (default)
    "AUTOLAYOUT_TITLE_2CONTENT": 3,          # Title + 2 Columns
    "AUTOLAYOUT_TITLE_CONTENT_2CONTENT": 12, # Title + 1 Content Left, 2 stacked Right
    "AUTOLAYOUT_TITLE_CONTENT_OVER_CONTENT": 14,# Title + 2 Vertical Content Blocks
    "AUTOLAYOUT_TITLE_2CONTENT_CONTENT": 15, # Title + 2 stacked Left, 1 Content Right
    "AUTOLAYOUT_TITLE_2CONTENT_OVER_CONTENT": 16,# Title + 2 Columns over 1 Row
    "AUTOLAYOUT_TITLE_4CONTENT": 18,         # Title + 4 Content areas (2x2 Grid)
    "AUTOLAYOUT_TITLE_ONLY": 19,             # Title only
    "AUTOLAYOUT_NONE": 20,                   # Blank Slide
    "AUTOLAYOUT_ONLY_TEXT": 32,              # 1 Centered text block, no title
    "AUTOLAYOUT_TITLE_6CONTENT": 34,         # Title + 6 Content areas (3x2 Grid)
}
```

#### Structured Transformation Handler
```python
def apply_slide_transformations(ctx, slide_commands: list[dict[str, Any]]) -> dict[str, Any]:
    """Applies a sequence of high-level slide transformation commands.
    
    Commands:
      - {"JumpToSlide": index}
      - {"ChangeLayoutByName": name}
      - {"SetText.N": "text"}
      - {"InsertSlide": index}
    """
    doc = ctx.doc
    draw_pages = doc.getDrawPages()
    current_slide_idx = 0
    
    def get_current_slide():
        return draw_pages.getByIndex(current_slide_idx)
        
    changes_made = 0
    
    for cmd in slide_commands:
        try:
            # 1. Slide Navigation
            if "JumpToSlide" in cmd:
                val = cmd["JumpToSlide"]
                if val == "last":
                    current_slide_idx = draw_pages.getCount() - 1
                else:
                    current_slide_idx = max(0, min(int(val), draw_pages.getCount() - 1))
                    
            # 2. Slide Insertion
            elif "InsertSlide" in cmd:
                idx = int(cmd["InsertSlide"])
                draw_pages.insertNewByIndex(idx)
                current_slide_idx = idx
                changes_made += 1
                
            # 3. Layout Transformations
            elif "ChangeLayoutByName" in cmd:
                layout_name = cmd["ChangeLayoutByName"]
                layout_id = IMPRESS_LAYOUTS.get(layout_name)
                if layout_id is not None:
                    slide = get_current_slide()
                    slide.Layout = layout_id
                    changes_made += 1
                    
            # 4. Text Placement
            else:
                # Handle SetText.N where N is the placeholder shape index
                for key, text_val in cmd.items():
                    if key.startswith("SetText."):
                        placeholder_idx = int(key.split(".")[1])
                        slide = get_current_slide()
                        
                        # Find placeholder shape by its Index or Layout shape position
                        # In PyUNO, we iterate through shapes looking for placeholders
                        shape_count = slide.getCount()
                        matched_placeholder = None
                        current_ph_idx = 0
                        
                        for s_idx in range(shape_count):
                            shape = slide.getByIndex(s_idx)
                            if shape.supportsService("com.sun.star.drawing.TextShape"):
                                # Check if shape is a placeholder
                                if getattr(shape, "IsPlaceholder", False):
                                    if current_ph_idx == placeholder_idx:
                                        matched_placeholder = shape
                                        break
                                    current_ph_idx += 1
                                    
                        if matched_placeholder:
                            matched_placeholder.setString(str(text_val))
                            changes_made += 1
                            
        except Exception as e:
            return {"status": "error", "message": f"Failed executing {cmd}: {e}"}
            
    return {"status": "ok", "commands_executed": len(slide_commands), "changes_made": changes_made}
```

### D. Progressive Image Generation & Placement

For presenting a dynamic UI where AI images generation shouldn't freeze LibreOffice:
1. Insert a loading graphic shape instantly in Python.
2. Spin up a background thread to fetch the real image from the AI provider.
3. Use the asynchronous stream dispatcher in `writeragent` to replace the placeholder's graphic.

```python
import tempfile
import urllib.request

def replace_placeholder_with_ai_image(ctx, shape_name: str, prompt: str):
    """Asynchronously generates an AI image and replaces a target shape's graphic."""
    
    # 1. Locate shape in main thread
    slide = ctx.doc.getCurrentController().getCurrentPage()
    target_shape = None
    for i in range(slide.getCount()):
        shape = slide.getByIndex(i)
        if shape.Name == shape_name:
            target_shape = shape
            break
            
    if not target_shape:
        return
        
    # 2. Run API fetch inside a background worker thread
    def bg_worker():
        try:
            # Request image from AI model (e.g. DALL-E / local stable diffusion)
            # For demo purposes, writing to a temp png file:
            temp_path = tempfile.mktemp(suffix=".png")
            
            # Query LLM image client (pseudo-code)
            image_url = query_ai_image_model(prompt)
            urllib.request.urlretrieve(image_url, temp_path)
            
            # 3. Schedule graphic replacement on VCL Main thread
            def main_thread_update():
                try:
                    # Load the local temp file URL into LibreOffice GraphicProvider
                    graphic_provider = ctx.ctx.ServiceManager.createInstanceWithContext(
                        "com.sun.star.graphic.GraphicProvider", ctx.ctx
                    )
                    from com.sun.star.beans import PropertyValue
                    prop = PropertyValue()
                    prop.Name = "URL"
                    prop.Value = f"file://{temp_path}"
                    
                    graphic = graphic_provider.queryGraphic((prop,))
                    target_shape.Graphic = graphic
                except Exception as ex:
                    log.error(f"VCL Thread graphic update failed: {ex}")
                    
            # Dispatch to main thread (using writeragent's async_stream loop queue)
            ctx.main_thread_queue.put(main_thread_update)
            
        except Exception as e:
            log.error(f"Background image generation failed: {e}")
            
    # Launch worker thread
    ctx.run_in_background(bg_worker)
```

### E. Dynamic Link Target Mapping (`extract_link_targets`)

This PyUNO adaptation dynamically retrieves valid link targets across various categories in Writer documents.

```python
def get_document_link_targets(ctx) -> dict[str, list[str]]:
    """Gathers all linkable target addresses from the current document.
    
    Returns a dictionary grouping targets by type, matching target formats
    like 'BookmarkName|bookmark' or 'TableName|table'.
    """
    doc = ctx.doc
    targets = {
        "bookmarks": [],
        "tables": [],
        "frames": [],
        "sections": [],
        "headings": []
    }
    
    # 1. Gather Bookmarks
    if hasattr(doc, "getBookmarks"):
        bookmarks = doc.getBookmarks()
        for name in bookmarks.getElementNames():
            targets["bookmarks"].append(f"{name}|bookmark")
            
    # 2. Gather Tables
    if hasattr(doc, "getTextTables"):
        tables = doc.getTextTables()
        for name in tables.getElementNames():
            targets["tables"].append(f"{name}|table")
            
    # 3. Gather Text Frames
    if hasattr(doc, "getTextFrames"):
        frames = doc.getTextFrames()
        for name in frames.getElementNames():
            targets["frames"].append(f"{name}|frame")
            
    # 4. Gather Sections
    if hasattr(doc, "getTextSections"):
        sections = doc.getTextSections()
        for name in sections.getElementNames():
            targets["sections"].append(f"{name}|section")
            
    # 5. Gather Headings / Outline
    if hasattr(doc, "getParagraphs") or hasattr(doc, "getText"):
        # Iterate paragraphs to find Heading styles
        try:
            paragraphs = doc.getText().createEnumeration()
            while paragraphs.hasMoreElements():
                para = paragraphs.nextElement()
                style_name = para.getPropertyValue("ParaStyleName")
                if style_name and style_name.startswith("Heading"):
                    text_content = para.getString().strip()
                    if text_content:
                        # Standard format is 'HeadingText|outline'
                        targets["headings"].append(f"{text_content}|outline")
        except Exception:
            pass
            
    return targets
```

### F. Dynamic Document Structure / Outline (`extract_document_structure`)

A unified structure inspector returning a JSON-compatible tree representation of the active document.

```python
def get_generic_document_structure(ctx) -> dict[str, Any]:
    """Inspects the active document and returns a structured outline.
    
    Supports Writer (Headings/Tables), Calc (Worksheets), and Impress (Slides/Shapes).
    """
    doc = ctx.doc
    doc_type = "unknown"
    
    # Identify document type by checking supported services
    if hasattr(doc, "supportsService"):
        if doc.supportsService("com.sun.star.text.TextDocument"):
            doc_type = "writer"
        elif doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            doc_type = "calc"
        elif doc.supportsService("com.sun.star.presentation.PresentationDocument"):
            doc_type = "impress"
            
    structure = {
        "document_type": doc_type,
        "outline": []
    }
    
    # 1. Writer: Extracts outline headings
    if doc_type == "writer":
        try:
            enum = doc.getText().createEnumeration()
            while enum.hasMoreElements():
                item = enum.nextElement()
                if item.supportsService("com.sun.star.text.Paragraph"):
                    style = item.getPropertyValue("ParaStyleName")
                    if style and style.startswith("Heading"):
                        structure["outline"].append({
                            "type": "heading",
                            "level": style.replace("Heading ", ""),
                            "text": item.getString().strip()
                        })
        except Exception as e:
            structure["error"] = str(e)
            
    # 2. Calc: Extracts worksheet names
    elif doc_type == "calc":
        try:
            sheets = doc.getSheets()
            for name in sheets.getElementNames():
                structure["outline"].append({
                    "type": "sheet",
                    "name": name
                })
        except Exception as e:
            structure["error"] = str(e)
            
    # 3. Impress: Extracts slides and their textual shape summaries
    elif doc_type == "impress":
        try:
            pages = doc.getDrawPages()
            for idx in range(pages.getCount()):
                page = pages.getByIndex(idx)
                slide_info = {
                    "slide_index": idx,
                    "slide_name": page.Name,
                    "shapes": []
                }
                for s_idx in range(page.getCount()):
                    shape = page.getByIndex(s_idx)
                    shape_info = {
                        "name": shape.Name,
                        "type": shape.ShapeType
                    }
                    if hasattr(shape, "getString"):
                        text = shape.getString().strip()
                        if text:
                            shape_info["text_content"] = text
                    slide_info["shapes"].append(shape_info)
                structure["outline"].append(slide_info)
        except Exception as e:
            structure["error"] = str(e)
            
    return structure
```

### G. Transactional Undo Context Grouping (`XUndoManager`)

Wraps multiple PyUNO operations into a single named block on the undo stack, enabling smooth single-step rollback.

```python
import contextlib

@contextlib.contextmanager
def ai_undo_context(ctx, context_name: str):
    """Context manager to group PyUNO operations into a single Undo action.
    
    Usage:
        with ai_undo_context(ctx, "AI Slide Design"):
            # Insert shapes, set styles...
    """
    doc = ctx.doc
    undo_manager = None
    entered = False
    
    # XUndoManager is exposed directly on modern document models
    if hasattr(doc, "getUndoManager"):
        try:
            undo_manager = doc.getUndoManager()
            if undo_manager:
                undo_manager.enterUndoContext(context_name)
                entered = True
        except Exception as e:
            log.warning(f"Could not enter undo context: {e}")
            
    try:
        yield
    except Exception as err:
        # If there's an exception, the undo context is still cleanly left
        raise err
    finally:
        if undo_manager and entered:
            try:
                undo_manager.leaveUndoContext()
            except Exception as e:
                log.error(f"Could not leave undo context: {e}")
```

### H. Dynamic AI Model Discovery (`fetch_models`)

Fetches the list of standard OpenAI-compatible models directly from the provider at runtime.

```python
import urllib.request
import json

def fetch_provider_models(api_key: str, base_url: str = "https://api.openai.com") -> list[str]:
    """Queries an OpenAI-compatible /v1/models endpoint and returns a list of model IDs.
    
    This avoids static hardcoding in settings or UI dialogs.
    """
    if base_url.endswith("/"):
        base_url = base_url[:-1]
    url = f"{base_url}/v1/models"
    
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            models = [item["id"] for item in data.get("data", [])]
            # Optional: sort and filter models to only show chat-compatible models
            return sorted(models)
    except Exception as e:
        log.error(f"Failed to fetch dynamic models list: {e}")
        return []
```

### I. Endpoint Allowlisting (SSRF Safeguards)

Verifies that custom API URLs configured by users conform to authorized corporate domains or standard provider gateways.

```python
import re
from urllib.parse import urlparse

# Strict regex matching standard secure LLM API providers and trusted domains
AUTHORIZED_LLM_HOSTS = r"^(api\.openai\.com|api\.anthropic\.com|api\.groq\.com|api\.mistral\.ai|api\.together\.xyz)$"

def is_authorized_ai_endpoint(target_url: str) -> bool:
    """SSRF & security safeguard: checks if an AI API host is in the trust allowlist."""
    try:
        parsed = urlparse(target_url)
        host = parsed.hostname
        if not host:
            return False
            
        # Match standard providers
        if re.match(AUTHORIZED_LLM_HOSTS, host):
            return True
            
        # Optional: Add local intranet gateway allowance if supported
        # if host == "internal-llm.my-company.com": return True
        
        return False
    except Exception:
        return False
```
```

---

> [!NOTE]
> Since WriterAgent has direct object access via PyUNO, we do not require the massive C++ client/server routing architecture, WebSocket decoders, or system jails that Collabora relies upon. These Python equivalents are highly optimized, simpler, and run directly inside the user's LibreOffice thread environment.
