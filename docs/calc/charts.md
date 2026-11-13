# Chart Architecture and Management

WriterAgent provides chart management through four specialized tools (`chart_list`, `chart_get_info`, `chart_upsert`, `chart_delete`) across LibreOffice Calc, Writer, and Draw/Impress.

## Tool surface

- **`chart_list`**: List charts (optional `sheet` / `page` parameters are accepted; listing still scans all hosts today).
- **`chart_get_info`**: Metadata for a chart by `name` (required).
- **`chart_upsert`**: Create or update. Omit `name` to create (`chart_type` plus a host data source). A non-blank `name` edits when the chart exists; when it does not, the same call creates under that name if `chart_type` and a valid source are present, otherwise returns not found.
- **`chart_delete`**: Delete by `name` (required).

## Validation and `doc_type`

`validate()` receives `doc_type` from the caller; it does not open the document.

- **Chat** builds `ToolContext` with `doc_type` from the active document.
- **Script / `host_rpc.execute_tool`** passes `doc_type=""` only when the document is not calc, writer, or draw; otherwise calc, writer, or draw.
- **MCP `direct_flat`** runs tools with real `doc_type` from `DocumentService.detect_doc_type`.
- **MCP delegate mode** rejects specialized tiers (including all four chart tools) before `validate`.
- When `doc_type` is omitted, `None`, or not `"calc"`, create validation uses the Writer/Draw/Impress array path (`headers` and `rows` required). Wrong-host keys stripped from the schema may still fail at execute with messages naming `data_range` or `headers`/`rows` (not a single cross-host sentence).

## Host Kinds and Deletion

Create and delete obtain the chart host adapter from `chart_host`; delete does not branch on `ref.kind` again after lookup.

Charts live in different container structures depending on the host application:

- **Calc sheets (`calc_sheet`)**: Charts are embedded table charts (`ScChartObj`) belonging to an individual sheet's `XTableCharts` collection. Deletion is performed via `sheet.getCharts().removeByName(name)`.
- **Writer body text (`writer_embed`)**: Charts inserted into Writer documents are inline `TextEmbeddedObject` instances in the text body. Deletion removes the text content from the text that anchors it via `host.getAnchor().getText().removeTextContent(host)` (body, frame, or table cell), with the embedded-objects container as the fallback.
- **Writer and Draw draw pages (`draw_page`)**: OLE2 shape charts on draw pages are removed from their immediate parent via `parent.remove(shape)` (the draw page or the `GroupShape` containing the chart). Page scans descend `GroupShape`.
- **Writer create name read-back**: if the Writer host assigns a different name to the inserted embed, create returns that read-back name (and rollback targets it), so edit/delete work with the name in the result.

## Draw and Impress Page Scope

In Draw and Impress documents, create takes the lowest `Chart_N` free on any Draw/Impress page unless a unused requested name is supplied; list still includes `page`; get/edit/delete still take optional `page` and still return `CHART_AMBIGUOUS` when that name is on more than one page.

## Data Representation: Calc vs. Writer/Draw

- **Calc (`data_range`, `has_header`)**: Calc charts bind directly to cell ranges in spreadsheet sheets using `data_range` (e.g. `"A1:B10"` or qualified like `"'Sheet1'.A1:B10"`). On create or edit, the range is resolved via `CalcBridge.get_cell_range()`. Setting `has_header` toggles `setHasColumnHeaders` and `setHasRowHeaders`. Raw data arrays (`headers`, `rows`) are rejected in Calc.
- **Writer and Draw (`headers`, `rows`)**: Writer and Draw do not possess spreadsheet cell grids; chart data is supplied programmatically as `headers` and `rows` arrays and committed via `com.sun.star.chart.XChartDataArray.setData()`. On `chart_upsert`, data arrays are applied before diagram creation and series colors. If `getDiagram()` is missing on the chart document, data arrays are already applied and styling records a non-fatal `"No diagram found on chart document."` warning while returning `status: "ok"`.
- **Cell Cap**: Programmatic data arrays enforce a maximum payload limit of `len(rows) * len(headers) <= 10_000` cells to protect UI thread responsiveness. Values must be finite numbers (`math.isfinite`).

## Styling Failures and Chart Rollback

- **Non-fatal styling warnings**: Failures setting optional visual properties—including chart background color (`bg_color`), series colors (`colors`), axis titles (`x_axis_title`, `y_axis_title`), chart title (`title`), subtitle (`subtitle`), legend visibility/position (`has_legend`, `legend_position`), missing diagram (`"No diagram found on chart document."`), and unsupported diagram flags (`Dim3D`, `Stacked`, `Percent`, `Vertical`)—do not abort chart creation or edit. They produce `warnings` strings while returning `status: "ok"`. A `legend_position` placement turns `HasLegend` on; `none` clears it; `has_legend` false plus a placement leaves it off and adds a warning. `colors` walks every data series in the diagram in order and does not restart at each chart type.
- **Fatal creation failures (rollback)**: Hard errors during creation—such as failure to instantiate the diagram service, `setDiagram()` failure, or `_apply_chart_data_arrays()` failure (`ValueError` or `setData()` error)—trigger immediate rollback (`removeByName`, `removeTextContent`, or `page.remove`) so no orphan chart remains in the document before returning an error payload. On edit, existing charts are never removed on failure.

## Errors

Chart actions (`list_charts`, `get_chart_info`, `upsert_chart`, `delete_chart`) and host adapters (`SheetCharts`, `WriterEmbeds`, `DrawPage`) raise `ToolExecutionError` on refusal or failure, and `ToolBase.execute_safe` formats the exception into the error payload. A LibreOffice or range failure inside create, edit, or delete becomes `CHART_CREATE_ERROR`, `CHART_EDIT_ERROR`, or `TOOL_EXECUTION_ERROR` with the operation prefix; a refusal (`ToolExecutionError`) raised inside create passes through with its own message and code unchanged. A Draw/Impress lookup with multiple matches or an out-of-range `page` raises `CHART_AMBIGUOUS`.

## Python Matplotlib Visualizations

Note that programmatic charts via `chart_upsert` insert native LibreOffice Chart2 OLE objects. Scripted matplotlib/seaborn plots generated via `plot_data` (in `plugin/calc/viz.py` and Python formula evaluations) follow a completely separate path, rendering raster images (PNG) and inserting them as graphical shape objects onto the sheet.
