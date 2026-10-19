# Chart Architecture and Management

WriterAgent provides unified chart management through the `manage_charts` tool across LibreOffice Calc, Writer, and Draw/Impress.

## Host Kinds and Deletion

Charts live in different container structures depending on the host application:

- **Calc sheets (`calc_sheet`)**: Charts are embedded table charts (`ScChartObj`) belonging to an individual sheet's `XTableCharts` collection. Deletion is performed via `sheet.getCharts().removeByName(name)`.
- **Writer body text (`writer_embed`)**: Charts inserted into Writer documents are inline `TextEmbeddedObject` instances in the text body. Deletion removes the text content from the document text via `doc.getText().removeTextContent(host)`.
- **Writer and Draw draw pages (`writer_draw_page`, `draw_page`)**: OLE2 shape charts on draw pages are removed from their immediate parent via `parent.remove(shape)` (the draw page or the `GroupShape` containing the chart). Page scans descend `GroupShape`.

## Draw and Impress Page Scope

In Draw and Impress documents, create takes the lowest `Chart_N` free on any Draw/Impress page; list still includes `page`; get/edit/delete still take optional `page` and still return `CHART_AMBIGUOUS` when that name is on more than one page.

## Data Representation: Calc vs. Writer/Draw

- **Calc (`data_range`, `has_header`)**: Calc charts bind directly to cell ranges in spreadsheet sheets using `data_range` (e.g. `"A1:B10"` or qualified like `"'Sheet1'.A1:B10"`). On create or edit, the range is resolved via `CalcBridge.get_cell_range()`. Setting `has_header` toggles `setHasColumnHeaders` and `setHasRowHeaders`. Raw data arrays (`headers`, `rows`) are rejected in Calc.
- **Writer and Draw (`headers`, `rows`)**: Writer and Draw do not possess spreadsheet cell grids; chart data is supplied programmatically as `headers` and `rows` arrays and committed via `com.sun.star.chart.XChartDataArray.setData()`. In `manage_charts(action="create")` or `edit`, data arrays are applied before diagram creation and series colors. If `getDiagram()` is missing on the chart document, data arrays are already applied and styling records a non-fatal `"No diagram found on chart document."` warning while returning `status: "ok"`.
- **Cell Cap**: Programmatic data arrays enforce a maximum payload limit of `len(rows) * len(headers) <= 10_000` cells to protect UI thread responsiveness. Values must be finite numbers (`math.isfinite`).

## Styling Failures and Chart Rollback

- **Non-fatal styling warnings**: Failures setting optional visual properties—including chart background color (`bg_color`), series colors (`colors`), axis titles (`x_axis_title`, `y_axis_title`), chart title (`title`), subtitle (`subtitle`), legend visibility/position (`has_legend`, `legend_position`), missing diagram (`"No diagram found on chart document."`), and unsupported diagram flags (`Dim3D`, `Stacked`, `Percent`, `Vertical`)—do not abort chart creation or edit. They produce `warnings` strings while returning `status: "ok"`. A `legend_position` placement turns `HasLegend` on; `none` clears it; `has_legend` false plus a placement leaves it off and adds a warning. `colors` walks every data series in the diagram in order and does not restart at each chart type.
- **Fatal creation failures (rollback)**: Hard errors during creation—such as failure to instantiate the diagram service, `setDiagram()` failure, or `_apply_chart_data_arrays()` failure (`ValueError` or `setData()` error)—trigger immediate rollback (`removeByName`, `removeTextContent`, or `page.remove`) so no orphan chart remains in the document before returning an error payload. On edit, existing charts are never removed on failure.

## Python Matplotlib Visualizations

Note that programmatic charts via `manage_charts` insert native LibreOffice Chart2 OLE objects. Scripted matplotlib/seaborn plots generated via `plot_data` (in `plugin/calc/viz.py` and Python formula evaluations) follow a completely separate path, rendering raster images (PNG) and inserting them as graphical shape objects onto the sheet.
