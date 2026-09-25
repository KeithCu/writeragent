# Collabora Online Calc — Single and Multiple Plot/Image Egress (`=PY()`)

> **Status:** Draft, 2026-09-25. Design for **F3** / checklist row **G10** in [`docs/scripting/numpy-jailsafe.md`](../scripting/numpy-jailsafe.md).  
> **Prerequisites:** Assumes the two foundational Collabora Online MVP patches (**Gerrit 8122** in `collabofficefull/engine` and **Gerrit 8123** in `collabofficefull/kit`) are merged and builds directly on top of them.  
> **Codebase:** Collabora engine tree: `collabofficefull/engine` (Calc core & `scaddins`) and `collabofficefull/kit` (Online child session & emitter). Template behavior derived from production LibrePy desktop ([`plugin/calc/python/image_egress.py`](../../plugin/calc/python/image_egress.py), [`compute_service/json_egress.py`](../../compute_service/json_egress.py)). This design note lives in WriterAgent; the engine implementation is in the C++ engine tree.

---

## 1. Background: What Exists Today vs. The MVP Patches

To understand where plot insertion fits in Collabora Online, it is essential to distinguish between what exists in production desktop LibrePy today, what is provided by the pending Collabora Online MVP patches, and how native Calc handles graphics and drawing shapes.

### 1.1 What WriterAgent / LibrePy Does Today (Production Template)
WriterAgent / LibrePy is running in production as a desktop extension for LibreOffice with comprehensive support for matplotlib, seaborn, and custom visualization payloads:
1. **Plot Generation in Python:** The sandboxed runner captures open matplotlib/seaborn figures via `_capture_open_figures_payload` in [`plugin/scripting/venv/venv_sandbox.py`](../../plugin/scripting/venv/venv_sandbox.py). If a single figure is open, it serializes it to an image payload (`{"__wa_payload__": "image", "format": "png", "data": ...}`); if multiple figures are open, it bundles them into a `multi_data` envelope containing all open figures.
2. **Result Inspection in Egress:** When `=PYTHON(...)` or `=PY(...)` executes in [`plugin/calc/python/function.py`](../../plugin/calc/python/function.py), `find_image_payloads(result)` scans the returned object. If images are present, it invokes `insert_image_result_on_sheet`:
   ```python
   images = find_image_payloads(result)
   if images:
       for img in images:
           insert_image_result_on_sheet(ctx, img, code=code, doc=target_doc)
       return _("Image inserted") if len(images) == 1 else _("Images inserted")
   ```
3. **Smart Sizing & Geometry Snapping ([`plugin/calc/python/image_egress.py`](../../plugin/calc/python/image_egress.py)):**
   * **Unmerged Single Cell:** Sized to a standard chart overlay of $10\text{ cm} \times 6\text{ cm}$ ($10000 \times 6000$ in $1/100\text{ mm}$), with `ResizeWithCell = False`.
   * **Merged Placeholder Cell:** If the user pre-merged a block of cells (e.g. `B2:H18`) as a chart area, LibrePy detects `target_cell.IsMerged` and checks if width $\ge 40\text{ mm}$ and height $\ge 30\text{ mm}$. If so, the graphic shape snaps exactly to the merged cell's geometry with `ResizeWithCell = True`.
   * **Thin Banner Protection:** If the target cell is a 1-row merged strip (e.g. `A1:H1` title banner), setting shape size to cell size would crush the plot to a 5mm unreadable sliver. LibrePy detects thin banners and falls back to standard $10\text{ cm} \times 6\text{ cm}$.
4. **Lifecycle & Recalculation Replacement:**
   * Before creating a new shape, LibrePy inspects the sheet's `DrawPage` via `_shape_anchor_matches_cell`. If an existing shape is already anchored to the target formula cell, it updates the graphic on that shape in place rather than creating duplicate stacked shapes.
5. **Main-Thread Marshaling:**
   * Drawing layer mutations are marshaled to the main VCL UI thread via `post_to_main_thread` / `execute_on_main_thread`.

### 1.2 The Collabora Online MVP Patches (Gerrit 8122 & 8123)
The foundational Collabora Online Python integration is introduced by two patchsets:
* **Gerrit 8122 (`collabofficefull/engine`):** Adds the core C++ AddIn (`scaddins/source/pythoncompute/`) registering `=PY()` and `=PYTHON()`. It implements UNO’s `XVolatileResult` interface, displays an interim `"#BUSY!"` string, and parses the returned JSON into an `ScMatrix` via `anyjson.cxx`.
* **Gerrit 8123 (`collabofficefull/kit`):** Implements the broker in `kit` (`kit/PythonComputeEmitter.cpp`) and `coolwsd` that extracts arguments, sends requests over WebSocket to `coolwsd`, and forwards them to the remote Python compute service over HTTP.

### 1.3 Native Graphics and Drawing Layer in Calc Core
Calc core possesses a mature, fully integrated vector and raster drawing subsystem:
* **`ScDrawLayer` & `SdrPage`:** Each Calc sheet owns an `SdrPage` within the document's `ScDrawLayer`. Shapes on the sheet are instances of `SdrObject` (specifically `SdrGrafObj` for raster and SVG images).
* **Cell Anchoring (`ScDrawLayer::SetCellAnchoredFromPosition`):** Calc supports anchoring drawing objects directly to spreadsheet cells (`SCA_CELL` or `SCA_CELL_RESIZE`). When rows/columns are inserted, hidden, or resized, cell-anchored shapes adjust their position (and optionally dimensions) automatically.
* **Cell Metric Queries (`rDoc.GetMMRect`):** Calc core calculates exact metric cell and merged range rectangles in $1/100\text{ mm}$ via `ScDocument::GetMMRect(nCol1, nRow1, nCol2, nRow2, nTab)`.
* **Tile Invalidations in Collabora Online (LOKit):** When an `SdrObject` is inserted or modified on an `SdrPage`, Calc broadcasts `ScPaintHint` via `ScDocShell::PostPaint`. Collabora Online’s LibreOfficeKit layer captures these paint hints and automatically invalidates and repaints the corresponding visual tiles for all connected browser clients. No custom client-side WebSocket message or browser drawing protocol is required.
* **Document Persistence:** Shapes on `SdrPage` round-trip natively in OpenDocument Spreadsheet (`.ods`) using standard `<draw:frame>` / `<draw:image>` XML elements anchored with `table:cell-address`, and in Microsoft Excel (`.xlsx`) using DrawingML (`xdr:twoCellAnchor` / `xdr:oneCellAnchor`).

---

## 2. The Problem: What Happens Under the MVP Patches Today

With the MVP patchset (Gerrit 8122 & 8123) in place, when a user enters `=PY(...)` in Collabora Online and the Python script generates a matplotlib or seaborn plot, **no image is placed on the sheet, and the cell displays an informational placeholder string**.

### 2.1 Why Plots Are Discarded Today
The compute service (`compute_service/json_egress.py`) already recognizes visualization payloads and packages them into a top-level `images` array in the JSON response:
```json
{
  "status": "ok",
  "result": null,
  "stdout": "",
  "images": [
    {
      "format": "png",
      "data_b64": "iVBORw0KGgoAAAANSUhEUgAA..."
    }
  ]
}
```
However, in Collabora Online's `scaddins/source/pythoncompute/anyjson.cxx` (lines 821–826 & 934–945):
```cpp
else if (k == "images")
{
    if (!skipJsonValue(cur))
        return false;
    rOut.hasImages = true;
}
...
// images[] only — Classic inserts plots; Online v1: short message
if (env.hasImages && bResultNull)
{
    rOut <<= u"Image generated (plot insert not supported yet)"_ustr;
    return true;
}
```
1. `anyjson.cxx` skips parsing the actual image data in the `images` array.
2. It simply sets a boolean flag `hasImages = true`.
3. If `result` is null, it returns the static string:  
   `"Image generated (plot insert not supported yet)"`.
4. In `kit/ChildSession.cpp` (`handlePythonComputeResult`), the raw JSON string is passed directly to `completeFromJson()`, which delivers the string to the cell via `xVol->finish(aResult)`. The image base64 data is dropped on the floor.

### 2.2 Single vs. Multiple Plots: The Missing Multi-Figure Pipeline
Real-world data science workflows frequently produce more than one plot:
```python
import matplotlib.pyplot as plt
# Figure 1: Distribution
fig1, ax1 = plt.subplots()
ax1.hist(data, bins=20)
ax1.set_title("Distribution")

# Figure 2: QQ Plot
fig2, ax2 = plt.subplots()
stats.probplot(data, dist="norm", plot=ax2)
ax2.set_title("Normal Q-Q")
```
When this script runs, the compute service extracts both figures and delivers:
```json
"images": [
  {"format": "png", "data_b64": "<fig1_bytes>"},
  {"format": "png", "data_b64": "<fig2_bytes>"}
]
```
Today, Collabora Online has no mechanism to decode, unpack, or position multiple images.

### 2.3 The Recalculation Lifecycle Defect
Even if a naive image insertion hook were added to kit or core without lifecycle management, it would fail catastrophically during recalculation:
* **Duplicate Accumulation:** Whenever the user edits upstream data or modifies formula parameters, `=PY(...)` re-executes. Inserting a new shape on each calculation without checking for existing shapes would stack duplicate images on top of each other.
* **Zombie Plots on Scalar Conversion:** If a user modifies formula code from `plt.show()` to `result = 42`, the previously inserted plot would remain stranded on the spreadsheet indefinitely.
* **Flickering During `#BUSY!`:** While an asynchronous recalculation is in flight, existing plots must remain stable rather than disappearing and reappearing.

---

## 3. The Goal and Architectural Insights

### 3.1 The Goal
The goal of this design (F3) is to enable native single- and multiple-plot egress for `=PY(...)` in Collabora Online Calc:
* **Seamless Formula Entry:** The user enters `=PY(...)` containing plotting code into a single cell using regular Enter (`.uno:EnterString`).
* **Single-Plot Sizing Intelligence:** Automatically size unmerged cells to standard $10\text{ cm} \times 6\text{ cm}$; automatically snap to pre-merged placeholder areas (e.g. `B2:H18`) with `ResizeWithCell = true`; protect thin 1-row banners from crushing.
* **Multiple-Plot Stacking:** Automatically layout multiple figures vertically (or with standard offsets) anchored adjacent to the formula cell.
* **Dual Results (Data + Plot):** When a script returns both tabular data and a plot (e.g. `result = df.describe()`), the formula cell spills the data grid via dynamic arrays (F7) while simultaneously rendering the plot on the sheet.
* **Recalculation Lifecycle & Garbage Collection:** Automatically replace existing shapes on recalculation; delete shapes if the formula changes to a scalar or error; preserve existing shapes while in `#BUSY!`.
* **Zero Disk I/O & Memory Safety:** Stream and decode base64 payloads entirely in memory using `comphelper::Base64` and `SvMemoryStream`; enforce strict size and count caps.

### 3.2 Architectural Analysis: Where Should Plot Insertion Live?

We must evaluate two potential locations for the plot insertion logic: **Kit-Side (ChildSession / LOKit)** vs. **Core Engine (`sc/` & `scaddins`)**.

```
+---------------------------------------------------------------------------------------+
| Architectural Decision: Kit-Side vs. Core Engine                                      |
+------------------------------------+--------------------------------------------------+
| Approach                           | Verdict & Technical Assessment                   |
+------------------------------------+--------------------------------------------------+
| Option A: Kit-Side via LOKit       | REJECTED: Fatal race conditions.                 |
| `.uno:InsertGraphic` in            | - Uses view selection (rViewSh.GetInsertPos()).  |
| ChildSession.cpp                   | - If user moved cursor during async calc, image  |
|                                    |   drops into the wrong cell!                     |
|                                    | - Multi-user conflict: which user's view owns it?|
|                                    | - Fails on workbook recalc (no active view).     |
|                                    | - Cannot clean up old shapes on recalculation.   |
+------------------------------------+--------------------------------------------------+
| Option B: Core Engine Drawing      | RECOMMENDED: Clean, deterministic, robust.       |
| Layer in sc / scaddins             | - Operates directly on ScDocument and SdrPage.   |
| (Mirroring F7 Dynamic Array Spill) | - Knows exact ScAddress(Col, Row, Tab).          |
|                                    | - Updates/replaces shapes on recalc; cleans up.  |
|                                    | - Automatic LOKit tile invalidation via paint.   |
|                                    | - Fully functional in headless tests and batch.  |
+------------------------------------+--------------------------------------------------+
```

#### Why Option A (`.uno:InsertGraphic` in Kit) Fails
An initial thought documented in early scratch notes was to have `ChildSession::handlePythonComputeResult` in `kit/` write decoded bytes to a jail directory and invoke `.uno:InsertGraphic`. However, engine inspection reveals that `.uno:InsertGraphic` routes to `FuInsertGraphic` in `sc/source/ui/drawfunc/fuins1.cxx`:
```cpp
Point aInsertPos = rViewSh.GetInsertPos();
...
ScDrawLayer::SetCellAnchoredFromPosition(*pObj, rData.GetDocument(), rData.CurrentTabForData(), ...);
```
* **Cursor Drift:** Because Python compute is asynchronous (taking 200ms to 2s), the user will frequently click another cell while computation is running. `.uno:InsertGraphic` anchors the image to whatever cell happens to be selected when the HTTP response returns!
* **Multi-User Chaos:** In Collabora Online, multiple users edit simultaneously. A view dispatch would anchor the graphic to the active view of whoever triggered the broker.
* **No Lifecycle Association:** `.uno:InsertGraphic` has no concept of formula dependency. It cannot update an existing chart or delete a chart when the formula is cleared.

#### Why Option B (Core Drawing Layer) is the Correct Architecture
By implementing plot management inside Calc core (`sc/`) and the `pythoncompute` AddIn:
1. Calc core already knows the exact cell position `ScAddress(Col, Row, Tab)` during formula evaluation.
2. The drawing object (`SdrGrafObj`) is created directly on the sheet's `SdrPage` and anchored with `ScDrawLayer::SetCellAnchoredFromPosition`.
3. Existing shapes tagged with the formula cell address can be queried and updated in place.
4. LOKit tile invalidation happens automatically via `ScDocShell::PostPaint`.

---

## 4. The Plot Pipeline and Data Flow

### 4.1 Python Compute Service (`json_egress.py`) → JSON Wire Format
The Python compute worker converts matplotlib and PIL images into the standard wire format:
* **Single Plot Output:**
  ```json
  {
    "status": "ok",
    "result": null,
    "stdout": "",
    "images": [
      {
        "format": "png",
        "data_b64": "iVBORw0KGgoAAAANSUhEUgAA..."
      }
    ]
  }
  ```
* **Multiple Plot Output:**
  ```json
  {
    "status": "ok",
    "result": null,
    "stdout": "Captured 2 open figures.\n",
    "images": [
      {"format": "png", "data_b64": "...fig1..."},
      {"format": "png", "data_b64": "...fig2..."}
    ]
  }
  ```
* **Dual Output (Tabular Data + Plot):**
  ```json
  {
    "status": "ok",
    "result": [[1, 2], [3, 4]],
    "stdout": "",
    "images": [
      {"format": "png", "data_b64": "...histogram..."}
    ]
  }
  ```

### 4.2 In-Memory Decoding: Base64 to `vcl::Graphic`
The C++ engine avoids writing intermediate files to disk or jail directories. Image data is decoded directly into memory:
1. **Base64 Decode:** `comphelper::Base64::decode(aByteSeq, sBase64Data)` converts ASCII base64 into a `Sequence<sal_Int8>`.
2. **Memory Stream:** Wrap the sequence in an `SvMemoryStream`:
   ```cpp
   SvMemoryStream aMemStream(
       const_cast<sal_Int8*>(aByteSeq.getConstArray()),
       aByteSeq.getLength(),
       StreamMode::READ);
   ```
3. **Graphic Import:** `GraphicFilter::GetGraphicFilter().ImportGraphic(aGraphic, OUString(), aMemStream)` parses the byte stream into a `Graphic` object. `GraphicFilter` natively supports PNG, SVG, JPEG, and WebP, verifying headers and rejecting corrupt streams safely.

### 4.3 Sizing and Positioning Rules (The LibrePy Template)
Calc core computes cell metrics in $1/100\text{ mm}$ via `ScDocument::GetMMRect`:

| Target Cell Condition | Applied Dimensions ($W \times H$) | Anchor Mode | `bResizeWithCell` |
| --- | --- | --- | --- |
| **Standard 1×1 Cell** | $100\text{ mm} \times 60\text{ mm}$ ($10000 \times 6000$) | `SCA_CELL` | `false` |
| **Merged Placeholder** ($\ge 40\text{mm} \times 30\text{mm}$) | Exactly matches bounding box of merged range | `SCA_CELL_RESIZE` | `true` |
| **Thin Banner** (e.g. $1\text{ row} \times 8\text{ cols}$) | $100\text{ mm} \times 60\text{ mm}$ (fallback to prevent crushing) | `SCA_CELL` | `false` |

```cpp
// Geometry determination logic in C++
tools::Rectangle aCellRect = rDoc.GetMMRect(nCol, nRow, nCol, nRow, nTab);
SCCOL nEndCol = nCol;
SCROW nEndRow = nRow;
bool bIsMerged = rDoc.IsMerged(aPos);
if (bIsMerged)
    rDoc.ExtendMerge(nCol, nRow, nEndCol, nEndRow, nTab);

tools::Rectangle aMergedRect = rDoc.GetMMRect(nCol, nRow, nEndCol, nEndRow, nTab);
tools::Long nWidth = aMergedRect.GetWidth();
tools::Long nHeight = aMergedRect.GetHeight();

constexpr tools::Long MIN_PLACEHOLDER_W = 4000; // 40 mm
constexpr tools::Long MIN_PLACEHOLDER_H = 3000; // 30 mm
constexpr tools::Long DEFAULT_CHART_W   = 10000; // 100 mm
constexpr tools::Long DEFAULT_CHART_H   = 6000;  // 60 mm

bool bLargePlaceholder = bIsMerged && (nWidth >= MIN_PLACEHOLDER_W) && (nHeight >= MIN_PLACEHOLDER_H);

Size aShapeSize;
Point aShapePos = aMergedRect.TopLeft();
bool bResizeWithCell = false;

if (bLargePlaceholder)
{
    aShapeSize = aMergedRect.GetSize();
    bResizeWithCell = true;
}
else
{
    aShapeSize = Size(DEFAULT_CHART_W, DEFAULT_CHART_H);
    bResizeWithCell = false;
}
```

### 4.4 Multi-Plot Layout Strategy
When $N > 1$ plots are returned, stacking all $N$ images at identical coordinates would occlude all except the top-most graphic.

**Vertical Stacking Layout:**
* **Plot 0:** Placed at `aShapePos` (top-left of formula cell / placeholder).
* **Plot $k$ ($1 \le k < N$):** Placed directly below Plot $k-1$:
  $$\text{Top}_k = \text{Top}_{k-1} + \text{Height}_{k-1} + \text{Margin}$$
  where $\text{Margin} = 500$ ($5\text{ mm}$ spacing).
* Each shape is anchored to the row matching its vertical position or cell-anchored to `aPos` with individual vertical offsets.

```
+-----------------------------------------------------------+
| Multi-Plot Vertical Stacking Arrangement                  |
+-----------------------------------------------------------+
| [Formula Cell: =PY(...)]                                  |
| +-------------------------------------------------------+ |
| | Plot 0: Primary Chart (10cm x 6cm)                    | |
| +-------------------------------------------------------+ |
|                           | (5mm margin)                  |
| +-------------------------------------------------------+ |
| | Plot 1: Secondary Chart (10cm x 6cm)                  | |
| +-------------------------------------------------------+ |
|                           | (5mm margin)                  |
| +-------------------------------------------------------+ |
| | Plot 2: Tertiary Chart (10cm x 6cm)                   | |
| +-------------------------------------------------------+ |
+-----------------------------------------------------------+
```

---

## 5. Baseline vs. Proposed Execution Flow

### 5.1 Baseline Execution Flow (Images Swallowed Today)

```mermaid
sequenceDiagram
    participant User
    participant Cell as ScFormulaCell
    participant Interp as ScInterpreter
    participant AddIn as PythonComputeAddIn
    participant Kit as ChildSession (LOKit)
    participant WSD as coolwsd
    participant Svc as compute_service

    User->>Cell: Enter =PY("import matplotlib.pyplot as plt; ...")
    Cell->>Interp: Interpret (Pass 1)
    Interp->>AddIn: getPy(code, data)
    AddIn-->>Cell: XVolatileResult ("#BUSY!")
    AddIn->>Kit: pythoncompute: {id: "py-1", code: "..."}
    Kit->>WSD: Forward HTTP request
    WSD->>Svc: POST /v1/execute
    Svc-->>WSD: 200 OK {"status":"ok", "images":[{"format":"png", "data_b64":"..."}]}
    WSD->>Kit: pythoncomputeresult: {id: "py-1", ...}
    Kit->>AddIn: pythoncompute_complete_json()
    Note over AddIn: anyjson.cxx parses envelope; sees "images"; skips array payload!
    Note over AddIn: Returns string "Image generated (plot insert not supported yet)"
    AddIn->>Cell: Finish volatile result
    Cell->>Interp: Interpret (Pass 2)
    Note over Cell: Cell displays placeholder text; sheet drawing page is untouched!
```

### 5.2 Proposed Execution Flow (Native Plot Insertion & Lifecycle)

```mermaid
sequenceDiagram
    participant User
    participant Cell as ScFormulaCell
    participant Interp as ScInterpreter
    participant AddIn as PythonComputeAddIn
    participant Bridge as pythoncompute bridge
    participant Kit as ChildSession (LOKit)
    participant WSD as coolwsd
    participant Svc as compute_service
    participant Draw as ScDrawLayer / SdrPage

    User->>Cell: Enter =PY("plt.plot(...); result=None")
    Cell->>Interp: Interpret (Pass 1)
    Interp->>AddIn: getPy(code, data)
    AddIn-->>Cell: XVolatileResult ("#BUSY!")
    AddIn->>Kit: pythoncompute: {id: "py-1", ...}
    Kit->>WSD->>Svc: Execute script
    Svc-->>WSD-->>Kit: Return JSON with images[]
    Kit->>Bridge: pythoncompute_complete_json()
    Note over Bridge: anyjson parses images[] into vector<PythonComputeImage>
    Bridge->>AddIn: xVol->finish(aResult, aImages)
    AddIn->>Cell: ScAddInListener::modified() -> TrackFormulas
    Cell->>Interp: Interpret (Pass 2)
    Interp->>Draw: ProcessPendingPlotInserts(aPos, aImages)
    Note over Draw: 1. Clean up stale shapes for aPos<br/>2. Decode PNG/SVG via GraphicFilter<br/>3. Compute MMRect & check merged placeholder<br/>4. Insert SdrGrafObj & SetCellAnchored<br/>5. PostPaint(Objects) invalidates tiles
    Note over Cell: Cell text set to "" or "Image inserted"; tiles repaint in browser!
```

---

## 6. Proposed Engine & Kit Modifications

The implementation requires targeted modifications across `scaddins/source/pythoncompute/` and Calc core (`sc/`).

### 6.1 Data Structures and JSON Parsing (`anyjson.cxx` / `anyjson.hxx`)
Introduce a typed struct for parsed image payloads:
```cpp
namespace co::pythoncompute
{
struct PythonComputeImage
{
    OUString format;   // "png", "svg", "jpeg"
    OUString data_b64; // Base64-encoded graphic data
};

struct ExecutionResult
{
    cpo::uno::Any aValue;                          // Scalar or ScMatrix result
    std::vector<PythonComputeImage> aImages;        // Zero or more plots
    OUString sError;                               // Detail error string
    bool bHasImages = false;
};
}
```

Update `parseEnvelope` in `anyjson.cxx`:
1. When encountering key `"images"`, iterate the JSON array.
2. For each element object, parse `"format"` (string) and `"data_b64"` (string).
3. **Safety Caps:**
   * Maximum 8 images per request (clamped; remaining ignored with warning log).
   * Maximum 10 MiB base64 string length per image (~7.5 MiB decoded).
   * Format allowlist: `"png"`, `"svg"`, `"jpeg"`, `"jpg"`.
4. Store parsed items in `ExecutionResult::aImages`.

### 6.2 Propagating Image Payloads Through `PythonComputeVolatileResult`
Extend `PythonComputeVolatileResult` in `scaddins/source/pythoncompute/volatile.hxx`:
* Store `std::vector<PythonComputeImage> m_aImages`.
* Provide thread-safe accessor `const std::vector<PythonComputeImage>& getImages() const`.
* In `finish()`, store the image vector alongside the UNO `Any` result value before notifying listeners.

### 6.3 Queued Drawing Insertion in Calc Core (`ProcessPendingPlotInserts`)
Drawing layer operations must not occur during the recursive mathematical interpretation of formula cells. Like dynamic array resizing (`ProcessPendingMatrixResizes` in `documen4.cxx`), plot insertions are queued and dispatched during the interpret epilogue under the SolarMutex:

```cpp
void ScDocument::ProcessPendingPlotInserts(
    const ScAddress& rPos,
    const std::vector<co::pythoncompute::PythonComputeImage>& rImages)
{
    ScDrawLayer* pDrawLayer = GetDrawLayer();
    if (!pDrawLayer)
        return;

    SCTAB nTab = rPos.Tab();
    SdrPage* pPage = pDrawLayer->GetPage(nTab);
    if (!pPage)
        return;

    // 1. Tag prefix for shapes belonging to this formula cell
    OUString sPosPrefix = "PyPlot_" + rPos.Format(ScRefFlags::VALID, this) + "_";

    // 2. Identify and remove or update existing shapes for this cell
    std::vector<SdrObject*> aOldShapes;
    const size_t nObjCount = pPage->GetObjCount();
    for (size_t i = 0; i < nObjCount; ++i)
    {
        SdrObject* pObj = pPage->GetObj(i);
        if (pObj && pObj->GetName().startsWith(sPosPrefix))
            aOldShapes.push_back(pObj);
    }

    // If formula returned no images (e.g. recalculated to scalar), delete existing shapes
    if (rImages.empty())
    {
        for (SdrObject* pOld : aOldShapes)
            pPage->RemoveObject(pOld->GetOrdNum());
        GetDocumentShell()->PostPaint(GetMMRect(rPos.Col(), rPos.Row(), rPos.Col(), rPos.Row(), nTab),
                                      PaintPartFlags::Objects);
        return;
    }

    // 3. Compute base geometry and merged placeholder bounds
    tools::Rectangle aCellRect = GetMMRect(rPos.Col(), rPos.Row(), rPos.Col(), rPos.Row(), nTab);
    SCCOL nEndCol = rPos.Col();
    SCROW nEndRow = rPos.Row();
    bool bMerged = IsMerged(rPos);
    if (bMerged)
        ExtendMerge(rPos.Col(), rPos.Row(), nEndCol, nEndRow, nTab);

    tools::Rectangle aMergedRect = GetMMRect(rPos.Col(), rPos.Row(), nEndCol, nEndRow, nTab);
    bool bLargePlaceholder = bMerged
        && (aMergedRect.GetWidth() >= 4000)
        && (aMergedRect.GetHeight() >= 3000);

    Size aBaseSize = bLargePlaceholder ? aMergedRect.GetSize() : Size(10000, 6000);
    bool bResizeWithCell = bLargePlaceholder;
    Point aBasePos = aMergedRect.TopLeft();

    constexpr tools::Long nSpacingY = 500; // 5mm vertical spacing between multiple plots
    tools::Rectangle aTotalInvalidRect;

    // 4. Insert or update shapes for each image in rImages
    for (size_t k = 0; k < rImages.size(); ++k)
    {
        const auto& img = rImages[k];
        Sequence<sal_Int8> aBytes;
        comphelper::Base64::decode(aBytes, img.data_b64);
        if (aBytes.getLength() == 0)
            continue;

        SvMemoryStream aStream(const_cast<sal_Int8*>(aBytes.getConstArray()),
                               aBytes.getLength(), StreamMode::READ);
        Graphic aGraphic;
        ErrCode nErr = GraphicFilter::GetGraphicFilter().ImportGraphic(aGraphic, OUString(), aStream);
        if (nErr != ERRCODE_NONE)
            continue;

        Point aPos = aBasePos;
        if (k > 0)
            aPos.AdjustY(k * (aBaseSize.Height() + nSpacingY));

        tools::Rectangle aRect(aPos, aBaseSize);
        aTotalInvalidRect.Union(aRect);

        OUString sName = sPosPrefix + OUString::number(k);
        SdrGrafObj* pGrafObj = nullptr;

        // Reuse existing shape if available; else create new
        if (k < aOldShapes.size())
        {
            pGrafObj = dynamic_cast<SdrGrafObj*>(aOldShapes[k]);
            if (pGrafObj)
            {
                pGrafObj->SetGraphic(aGraphic);
                pGrafObj->SetSnapRect(aRect);
            }
        }

        if (!pGrafObj)
        {
            rtl::Reference<SdrGrafObj> pNewObj = new SdrGrafObj(
                pDrawLayer->getSdrModelFromSdrView(), aGraphic, aRect);
            pNewObj->SetName(sName);
            pPage->InsertObject(pNewObj.get());
            pGrafObj = pNewObj.get();
        }

        ScDrawLayer::SetCellAnchoredFromPosition(*pGrafObj, *this, nTab, bResizeWithCell);
    }

    // 5. Remove any excess old shapes (e.g. previously had 3 plots, now only 1)
    if (aOldShapes.size() > rImages.size())
    {
        for (size_t k = rImages.size(); k < aOldShapes.size(); ++k)
            pPage->RemoveObject(aOldShapes[k]->GetOrdNum());
    }

    // 6. Broadcast tile invalidation to LOKit
    GetDocumentShell()->PostPaint(aTotalInvalidRect, PaintPartFlags::Objects);
}
```

### 6.4 Formula Cell Display and Dual Return Handling
In `anyjson.cxx` and `formulacell.cxx`:
1. **Plot-Only Result (`result == null` or empty):**
   * Do not display `"Image generated (plot insert not supported yet)"`.
   * Return an empty string `""` (or `"Image inserted"` / `"Images inserted"`).
   * This leaves the formula cell clean and non-erroring while the graphic floats visibly above/beside it.
2. **Dual Result (Matrix / Scalar + Plots):**
   * If `result` is a $2\text{D}$ matrix, return the matrix so Calc spills it dynamically (via F7).
   * If `result` is a scalar (e.g. number or summary string), return that scalar to the origin cell.
   * In both cases, the plots are placed on the sheet as a side effect.

### 6.5 Interim State Handling (`#BUSY!`)
* When a cell recalculates, it temporarily holds `"#BUSY!"`.
* `ProcessPendingPlotInserts` must **not** clear existing shapes while holding `"#BUSY!"`.
* Stale shapes are only updated or purged when the final response arrives, avoiding flashing during recalculation.

---

## 7. Document Persistence (ODS and XLSX)

Because drawing shapes are created directly on `SdrPage` using standard `SdrGrafObj` and anchored with `ScDrawLayer::SetCellAnchoredFromPosition`:

* **ODF Format (`.ods`):**
  * Automatically saved as `<draw:frame>` containing `<draw:image>` inside `<table:shapes>`.
  * Preserves `table:end-cell-address`, `draw:z-index`, and anchor properties.
  * Reloads cleanly in desktop LibreOffice, Collabora Online, and Apache OpenOffice without any custom extension filters.
* **Microsoft Excel Format (`.xlsx`):**
  * Automatically serialized through Calc's DrawingML export filter (`xeescher.cxx` / `oox`).
  * Merged placeholder shapes export as `xdr:twoCellAnchor` (`editAs="twoCell"`), preserving dynamic cell resizing in Microsoft Excel.
  * Standard $10\text{ cm} \times 6\text{ cm}$ shapes export as `xdr:oneCellAnchor` (`editAs="oneCell"`).
  * Reloads identically in Microsoft Excel desktop and Excel Online.

---

## 8. Security, Resource Limits & Memory Hygiene

Plots originate from arbitrary Python code inside compute containers. The engine must defend against memory exhaustion and malformed graphics:

1. **Payload Size Caps:**
   * Maximum base64 string length per image: $10\text{ MiB}$ (yielding approximately $7.5\text{ MiB}$ decoded). Payloads exceeding this limit fail with `#VALUE!`.
   * Maximum number of images per formula execution: 8. Excess images are dropped and logged to `SAL_WARN`.
2. **In-Memory Decoding:**
   * Raw image bytes are never written to physical disk or shared jail directories, preventing disk exhaustion and directory traversal attacks.
3. **GraphicFilter Hardening:**
   * `vcl::GraphicFilter` inspects magic bytes (PNG `\x89PNG`, SVG `<svg`) and executes format-specific parsing in memory. Corrupt or truncated streams return `ERRCODE_GRFILTER_FILTERERROR` without crashing the process.
4. **Cache Cleansing on Teardown:**
   * When a document or session closes, `pythoncompute_clear_caches` releases all cached volatile references and decoded image memory.

---

## 9. Verification & Test Plan

Tests will be implemented as CppUnit tests in `scaddins/qa/` and `sc/qa/unit/`:

### 9.1 Test Harness Operation
1. Initialize test document and set mock emitter via `pythoncompute_set_emitter(nullptr, nullptr)`.
2. Enter formula `=PY("...")`.
3. Dispatch mock response JSON containing base64 test images via `pythoncompute_complete_json()`.
4. Trigger interpretation via `pDoc->CalcFormulaTree(false)` or `pCell->Interpret()`.
5. Inspect `SdrPage` object count, geometry, names, and cell anchor properties.

### 9.2 Comprehensive Test Matrix

| Test ID | Test Scenario | Input / Setup | Expected Outcome |
| :--- | :--- | :--- | :--- |
| **T1** | **Single Plot (Standard Cell)** | `=PY(...)` in `B2` (1×1 unmerged), returns 1 PNG | 1 `SdrGrafObj` created on `SdrPage`; size is $100\text{mm} \times 60\text{mm}$; anchored to `B2`; `bResizeWithCell == false`; cell text is clean. |
| **T2** | **Single Plot (Merged Placeholder)** | Merge `B2:H18` ($120\text{mm} \times 80\text{mm}$); enter `=PY(...)` in `B2` | Shape dimensions match bounding box of `B2:H18` exactly; `bResizeWithCell == true`. |
| **T3** | **Thin Banner Protection** | Merge `A1:H1` ($160\text{mm} \times 6\text{mm}$); enter `=PY(...)` in `A1` | Large width but thin height (< 30mm); falls back to $100\text{mm} \times 60\text{mm}$ with `bResizeWithCell == false`. |
| **T4** | **Multiple Plots (2 Figures)** | Returns `images: [fig1, fig2]` | 2 shapes created; Shape 0 at `B2`; Shape 1 placed $5\text{mm}$ below Shape 0; both anchored to sheet. |
| **T5** | **Multiple Plots (Cap at 8)** | Returns `images` array with 12 figures | First 8 figures inserted; figures 9–12 ignored with warning; no memory crash. |
| **T6** | **Dual Result (Grid + Plot)** | Returns `result: [[1, 2], [3, 4]]` and 1 plot | Origin cell spills 2×2 matrix (F7); drawing layer contains 1 `SdrGrafObj` anchored to `B2`. |
| **T7** | **Recalculation Update (Plot Replacement)** | Cell has Plot A; re-evaluates returning Plot B | Shape count remains 1; `SdrGrafObj::GetGraphic()` updated to Plot B; no duplicate shape created. |
| **T8** | **Recalculation to Scalar (Garbage Collection)** | Cell has Plot A; re-evaluates returning `result = 42` | Existing shape removed from `SdrPage`; cell displays `42`; drawing layer clean. |
| **T9** | **Recalculation to Error (Cleanup)** | Cell has Plot A; re-evaluates returning `#VALUE!` | Existing shape removed from `SdrPage`; cell displays error. |
| **T10** | **`#BUSY!` Stability** | Cell has Plot A; in-flight recalc begins | Shape A remains visible and anchored while cell displays `"#BUSY!"`. |
| **T11** | **ODS Roundtrip** | Save document with plot to ODS and reload | Shape reloads intact, anchored to `B2`, with correct visual bounds. |
| **T12** | **XLSX Roundtrip** | Save document with plot to XLSX and reload | Shape reloads with DrawingML `oneCellAnchor` or `twoCellAnchor` intact. |

---

## 10. Out of Scope & Future Work

The following items are intentionally decoupled from this core plot egress implementation:
1. **Interactive Client-Side Plot Resizing:** Enabling browser drag handles to resize formula-generated shapes will be tracked as a subsequent UI task; the initial implementation respects the automatic $10\text{ cm} \times 6\text{ cm}$ and merged placeholder rules.
2. **Interactive Web Visualizations (Bokeh / Plotly / Altair):** Embedding interactive HTML/JavaScript visual canvas widgets instead of static raster/SVG graphics requires an iframe rendering sandbox in Collabora Online.
3. **In-Cell Sparklines (Micro-Charts):** Rendering miniature line/bar plots directly within the cell text background (similar to Excel sparklines) is handled via cell formatting, not floating drawing objects.

---

## 11. Code Reference Index

| Component | Responsibility | Source Location |
| :--- | :--- | :--- |
| **LibrePy Image Egress** | Desktop reference implementation for chart sizing, merged placeholder snapping, and cell anchoring | [`plugin/calc/python/image_egress.py`](../../plugin/calc/python/image_egress.py) |
| **Compute Service Egress** | Python worker response normalization, image extraction, and JSON packaging | [`compute_service/json_egress.py`](../../compute_service/json_egress.py) |
| **Sandbox Viz Capture** | Capturing open matplotlib/seaborn figures into single or `multi_data` envelopes | [`plugin/scripting/venv/venv_sandbox.py`](../../plugin/scripting/venv/venv_sandbox.py) |
| **Core JSON Parser** | Deserializing JSON responses and extracting `images[]` payload | [`scaddins/source/pythoncompute/anyjson.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/anyjson.cxx) |
| **Core AddIn Bridge** | Managing volatile result cache, timeouts, and completion notifications | [`scaddins/source/pythoncompute/bridge.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/scaddins/source/pythoncompute/bridge.cxx) |
| **Drawing Layer Engine** | Managing sheet drawing pages, inserting `SdrGrafObj`, and cell anchoring | [`sc/source/core/data/drwlayer.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/drwlayer.cxx) |
| **Cell Metric Geometry** | Calculating exact metric millimeter rectangles for cells and merged ranges | [`sc/source/core/data/documen3.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen3.cxx) `GetMMRect` |
| **Formula Epilogue Resizer** | Queued processing of matrix resizes and plot insertions | [`sc/source/core/data/documen4.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/sc/source/core/data/documen4.cxx) `ProcessPendingMatrixResizes` |
| **Online Kit Emitter** | Forwarding compute requests and receiving completions | [`kit/PythonComputeEmitter.cpp`](file:///home/keithcu/Desktop/collabofficefull/kit/PythonComputeEmitter.cpp) |
| **Online Child Session** | Handling incoming WebSocket results from coolwsd broker | [`kit/ChildSession.cpp`](file:///home/keithcu/Desktop/collabofficefull/kit/ChildSession.cpp) `handlePythonComputeResult` |
| **Graphic Filter** | In-memory decoding of PNG, SVG, and JPEG graphics | [`vcl/source/filter/graphicfilter.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/vcl/source/filter/graphicfilter.cxx) |
| **Base64 Codec** | Fast in-memory byte sequence base64 decoding | [`comphelper/source/misc/base64.cxx`](file:///home/keithcu/Desktop/collabofficefull/engine/comphelper/source/misc/base64.cxx) |
