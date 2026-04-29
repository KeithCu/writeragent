# Detailed LLM Evaluation Suite for LocalWriter

This document defines the test cases for the LocalWriter evaluation suite. We use an **LLM-as-a-Judge** (Grok 4.1 Fast) to evaluate submissions against high-tier **Gold Standards** (Claude Sonnet 4.6).

Each test is assigned a **Mode** to ensure appropriate weight distribution:
- **Structural Mode**: Weighs Accuracy (60%) and Formatting (40%). Used for tables, cleanup, and data-entry.
- **Creative Mode**: Weighs Naturalness (50%), Accuracy (30%), and Formatting (20%). Used for emails, resumes, and editing.

## 📝 Writer: Document Engineering (20 Tests)

### Level 1: Formatting & Precision (Essentials)
1.  **Format Preservation**: "Replace 'John Doe' with 'Jane Smith' in the header (Bold, 14pt)." -> Verify formatting remains. **[String-ok via apply_document_content + HTML preservation]**
2.  **Style Application**: "Make 'Introduction' a Heading 1." -> Verify `set_style` call. **[LO-required for real styles]**
3.  **Comment Management**: "Add a comment 'Review this' to the word 'Uncertain'." -> Verify `add_comment`. **[LO-required (comments)]**
4.  **Bullet Consistency**: "Ensure all bullet points in this list end with a period." -> Agent must iterate and edit. **[String-ok (text cleanup via apply_document_content)]**
5.  **Font Audit**: "Change all text in 'Comic Sans' to 'Inter'." -> Search and replace formatting. **[LO-required for font styles; partial with text search]**

### Level 2: Structural Manipulation (Advanced)
6.  **Table Engineering**: "Convert this comma-separated list into a 2-column table with headers." -> Verify `write_table_cells` (2D batch). **[String-ok (HTML table via apply_document_content)]**
7.  **Markdown Import**: "Replace the second paragraph with a Markdown table from the clipboard." -> Verify `apply_markdown`. **[String-ok (HTML/markdown support in format_support)]**
8.  **TOC Generation**: "Insert a Table of Contents at the start of the document." -> Verify TOC structure nodes. **[LO-required (TOC fields/indexes)]**
9.  **Section Break**: "Insert a section break and set the next page to Landscape orientation." -> Complex layout tool call. **[LO-required (page styles, sections)]**
10. **Bulk Cleanup**: "Remove all double spaces and ensure every sentence is followed by exactly one space." -> Regex-style cleanup. **[String-ok (text normalization)]**
11. **Header/Footer**: "Add page numbers in the footer and the document title in the header." -> Template manipulation. **[LO-required (headers/footers, fields)]**

### Level 3: Agentic Reasoning (Expert)
12. **Style Consistency**: "Find all text in 'Default' style and change it to 'Quotations'." -> Multi-step maneuver. **[LO-required (styles)]**
13. **Track Changes Audit**: "Accept all changes made by 'Reviewer A' but reject all by 'Reviewer B'." -> Selective auditing. **[LO-required (tracking tools)]**
14. **Bibliography Fix**: "Locate all brackets [1], [2] and ensure they are superscripted." -> Pattern matching + formatting. **[LO-required (fields, superscripts)]**
15. **Smart Summarization**: "Summarize the 'Finding' section into 5 bullet points and insert it into the 'Executive Summary'." -> Multi-part extraction. **[String-ok (extraction + apply_document_content); already in dataset.py]**
16. **Logical Rewriting**: "Rewrite the third paragraph to be 'professional and concise' while preserving all technical terms." -> Content-aware editing. **[String-ok (already in dataset.py)]**
17. **Refactoring Sections**: "Move the 'Conclusion' after the 'Intro' and rename it 'Goal'." -> Structural movement. **[String-ok (already added to dataset.py)]**
18. **Style Mapping**: "Map all 'Heading 2' text to become 'Heading 1' and adjust subsequent levels down." -> Recursive styling. **[LO-required (styles)]**
19. **Conflict Resolution**: "There are two definitions for 'API' in this doc. Merge them into one comprehensive definition." -> Semantic analysis. **[String-ok (text merge)]**
20. **Final Polish**: "Apply a consistent color theme (Blue/Gray) to all headings and tables." -> Global styling. **[LO-required (styles, colors)]**

## 📊 Calc: Analytical Fidelity (20 Tests) **[Most require LO or dedicated Calc mock for formulas/sheets/charts/conditional formatting]**

### Level 1: Data Entry & Formulas (Essentials)
1.  **Formula Mapping**: "Calculate the tax (8%) for Column B and put it in Column C." -> Relative references. **[LO-required (formulas)]**
2.  **Sheet Creation**: "Create a new sheet called 'Projections' and copy Column A there." -> Basic sheet manipulation. **[LO-required (sheets)]**
3.  **Row Clean**: "Remove all empty rows in Sheet1." -> Utility tool call. **[LO-required]**
4.  **Auto-Formatting**: "Highlight all cells in Column D greater than 1000 in Red." -> Conditional formatting. **[LO-required (conditional formatting tools)]**
5.  **Lookup Logic**: "Use VLOOKUP to find the price of 'Apple' from the 'Prices' sheet." -> Cross-sheet formula. **[LO-required]**

### Level 2: Complex Analysis (Advanced)
6.  **Data Sorting**: "Sort A1:D100 by 'Revenue' descending, after detecting the column." -> Detection + Action. **[LO-required]**
7.  **Error Debugging**: "The formula in D10 is failing. Find out why and fix it." -> Trace and fix. **[LO-required]**
8.  **Named Ranges**: "Create a named range 'SalesData' for A2:Z200." -> Metadata management. **[LO-required]**
9.  **Validation**: "Restrict Column F to only allow dates between 2020 and 2025." -> Input validation setup. **[LO-required]**
10. **Data Transpose**: "Take the row headers from A1:E1 and turn them into column headers in A1:A5." -> Structural shift. **[LO-required or string table transform]**
11. **Pivot Setup**: "Create a pivot table summary of this data onto a new sheet." -> Complex object creation. **[LO-required (pivot tools)]**

### Level 3: Visualization & Experts (Expert)
12. **Auto-Charting**: "Create a line chart for the trends in A1:B12." -> Chart creation. **[LO-required (charts)]**
13. **Data Recovery**: "Fix the broken CSV import that shifted everything by one column." -> Data shifting logic. **[String-ok (text/CSV cleanup)]**
14. **Consolidation**: "Sum all Column B values from Sheet1, Sheet2, and Sheet3 into Sheet4." -> Multi-sheet sum. **[LO-required]**
15. **Conditional Chains**: "If Column A is 'Profit', set Column B to 'Green'; if 'Loss', set to 'Red'." -> Logic mapping. **[LO-required (conditional formatting)]**
16. **Trend Analysis**: "Look at the last 6 months of data and predict the 7th month using a formula." -> Statistical reasoning. **[LO-required (formulas)]**
17. **Chart Styling**: "Change the theme of the existing chart to 'Dark' and add a title 'Revenue 2026'." -> Object manipulation. **[LO-required]**
18. **Sensitivity Analysis**: "Increase all 'Cost' values by 10% and record the change in 'Total Profit'." -> Scenario testing. **[LO-required]**
19. **Sheet Protect**: "Lock all cells with formulas so they cannot be edited." -> Security/metadata tool. **[LO-required]**
20. **Audit Log**: "Create a log entries sheet tracking every time 'Net Profit' falls below 0." -> Logic + Log creation. **[LO-required]**

## 🎨 Draw: Spatial Reasoning (5 Tests) **[Feasible with DrawJSONBackend using get_draw_tree JSON "DOM" (see eval-dev-plan.md); full LO for precise geometry/z-order]**

1.  **Shape Creation**: "Add a blue rectangle in the center of the page." -> Drawing Bridge. **[DrawJSON-ok (mock create_shape + get_draw_tree)]**
2.  **Simple Layout**: "Create three circles and align them horizontally." -> Offset calculation. **[DrawJSON-ok (positions in tree)]**
3.  **Flowchart Gen**: "Create a 'Start' oval connected to a 'Process' box." -> Connection points. **[DrawJSON-ok / priority for implementation (connectors via tree); example added below]**
4.  **Z-Order**: "Move the blue square behind the red circle." -> Layer management. **[DrawJSON partial (order in list); LO for full fidelity]**
5.  **Group Scale**: "Group all objects on page 1 and double their size." -> Aggregate manipulation. **[DrawJSON-ok with group children in tree]**

## 🖼️ Multimodal: Vision-to-Action (5 Tests) **[Mostly LO-required or advanced vision mock + generate_image; image insertion via tree/HTML sentinel]**

1.  **Chart OCR**: "Extract data from this chart image and put it into Sheet2." -> Vision + Calc tools. **[LO or vision mock]**
2.  **Image Captioning**: "Add a caption below this image based on its content." -> Vision + Writer tools. **[generate_image + insert; mockable in DrawJSON/String]**
3.  **UI Code-Gen**: "Translate this UI sketch into an ODF table mockup." -> Visual structural mapping. **[LO or string table]**
4.  **Spatial Audit**: "Looking at this diagram, is the 'Database' icon correctly connected to the 'Web Server'?" -> Visual logic check. **[DrawJSON tree perfect for this (connections)]**
5.  **Infographic Summary**: "Summarize the key takeaways from this infographic image into the document." -> High-level visual reasoning. **[LO/vision + summarization]**

## 🧠 Metrology (Benchmarking Formula)
- **Primary Score**: 0.0 - 1.0 (Composite of Accuracy, Formatting, and Naturalness).
- **Utility Multiplier**: We use **Quadratic Value Weighting** to find the "bang for buck" for document editing:
  $$Value = \frac{Correctness^2}{Total Cost (USD)}$$
- **IpD**: Intelligence-per-Dollar (Quadratic value above).
- **Reject Rate**: Models are penalized or rejected for hallucinating rejected keywords.
