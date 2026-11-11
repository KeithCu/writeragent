# Delegation smoke-test scenarios

Reusable headed checks that the main chat still delegates Writer and Calc work to the right specialized toolset, that the sub-agent finishes honestly, and that the document actually changes. These are short everyday prompts, not an eval pack.

## How to re-run

- Build and install a **debug** OXT (`scripts/build_oxt.py`, then register). Never use a release build: release strips the UNO thread-violation message box.
- Start each scenario from a **fresh chat** (Clear) on a **fresh copy** of the fixture. Type the prompt exactly.
- Record the chat model. The 2026-11-11 run used `openai/gpt-oss-120b` on OpenRouter, with AI grammar off. That model is capable enough to follow the prompts but not so strong that it papers over wording a smaller local model would trip on. Other models are fine; write down which one you used.
- Writer review mode may be `record`. Tracked changes from a successful edit are expected, not a failure.
- Confirm what happened from the debug log (tool names, `domain`, `task`, `finished`, error codes) and from a document inspect (UNO or the file itself), not only from the sidebar's final sentence. The document canvas can paint black on some displays.

## Pass criteria (every scenario)

1. Delegation went to the expected domain, or no delegation where none is expected. A reasonable alternative domain is a pass — note which.
2. If delegated: the result has `"finished": true`. It is not `SPECIALIZED_WORKFLOW_INCOMPLETE`, `SPECIALIZED_NO_TOOL_CALLS`, or `SPECIALIZED_TOOL_ERROR`.
3. The report makes sense: for an edit, it names where the change is and gives counts; for a read, it returns what was asked. It is not empty and does not dump the whole document.
4. No loops: no repeated identical delegate, no "Reached max steps". The main agent takes about six rounds or fewer.
5. The document actually shows the change.

## Fixtures

Recreate these the same way for a re-run. They are not checked in.

- **toc.odt** — Writer. Title "Quarterly Business Report" plus 10 headings (H1/H2/H3), each with a body paragraph. No table of contents.
- **toc-existing.odt** — The same outline, but with wrong manual numbers in the heading text ("3. Introduction", "3.4 Purpose of This Report", "1.1 Scope", "7. Background", "2.9 Market Overview", "5.5.5 Regional Trends", "1. Findings", "8.2 Sales Performance", "4.4.1 Quarterly Results", "2. Conclusion") and an updated TOC titled "Table of Contents" after the title.
- **plain.odt** — H1 "Team Offsite Planning Notes" plus four Text body paragraphs.
- **sales.ods** — Sheet "Sales", A1:D25, headers Region, Month, Units, Revenue, 24 data rows (four regions × January–June), unformatted.

## Expected domains

Writer: `delegate_to_specialized_writer_toolset`. Calc: `delegate_to_specialized_calc_toolset`.

| Area | Expected domain | Notes |
|------|-----------------|-------|
| TOC create, update, or list | `indexes` | A list-with-links request should call `indexes_list_toc_entries` with `include_links=true` (default is false). |
| Add a row to an existing table | `tables` | Creating a new table may be core `apply_document_content` HTML, or `tables`. |
| Line spacing via the Text body style | `styles` | |
| Generate an image and place it | `images` (`image_generate`) | |
| Equation / quadratic formula | none | `apply_document_content` with LaTeX, which should become a Math object. `embedded` is not expected. Writer has no math domain. |
| Sum of the first 100 primes | `python` | The correct sum is 24133. |
| Web lookup, then add a sentence | `web_research` | |
| Column or line chart | `charts` | |
| Header bold + currency format | none | `set_style` is a core tool. |
| Highlight units over 300 | `conditional_formatting` | |
| Sort by revenue | `ranges` (`range_sort`, `has_header` true) | |
| Only show the North rows | `sheets` | AutoFilter / `sheet_filter_apply`. Not `ranges`. |
| Price per Unit formula column | none | `write_formula_range` is a core tool. |
| Pivot of revenue by region on a new sheet | `pivot_tables` | May also use `sheets` to create the tab. |

## Writer scenarios

| id | Fixture | Prompt (type exactly) | Expected delegation | Extra pass criteria |
|----|---------|----------------------|---------------------|---------------------|
| W1 | toc.odt | Add a table of contents at the top. | `indexes` (`indexes_create`) | A real TOC is near the top and lists all 10 headings. A placeholder span is a fail. |
| W2 | toc-existing.odt | The heading numbers are wrong. Renumber them 1, 1.1, 1.2, 2 and so on, and update the table of contents. | Headings may be edited by the main agent or a delegate. The TOC goes through `indexes`. | Headings and TOC rows match, in order. The `task` string does not paste every TOC row or any link. |
| W3 | toc-existing.odt | What's in the table of contents? | `indexes` (`indexes_list_toc_entries`) | The answer gives entry text and level for all 10 rows, with no `hyperlink_url` (links were not requested). Answering from document context with no tools is a fail on criterion 1. |
| W4 | toc-existing.odt | List the table of contents entries and the link each one points to. | `indexes` (`indexes_list_toc_entries` with `include_links=true`) | Each row shows a `#…\|outline` link. |
| W5 | plain.odt | Add a small table after the second paragraph with columns Name, Team, Arrival Day and three example rows. | None needed (`apply_document_content` HTML), or `tables` | A table with those three columns and three data rows sits after paragraph 2. |
| W6 | W5's result | Add a row for Dana, Design, Tuesday to that table. | `tables` | The table has five rows; the last is Dana / Design / Tuesday; the other rows are unchanged. |
| W7 | plain.odt | Change the body text style to 1.5 line spacing. | `styles` | The Text body style is 150% line spacing and the body paragraphs follow it. |
| W8 | plain.odt | Generate a picture of a lakeside cabin in summer and put it under the heading. | `images` (`image_generate`) | One image is placed below the existing H1. The report must not claim a heading that was not added. |
| W9 | plain.odt | Add the quadratic formula as an equation at the end. | None (`apply_document_content` LaTeX) | One Math formula object appears at the end, not a literal `[Math import failed]…` paragraph. |
| W10 | plain.odt | What's the sum of the first 100 prime numbers? Add it as a sentence at the end. | `python` | The sentence says 24133. A `SPECIALIZED_TOOL_ERROR` after a successful retry is a fail on criterion 2. |
| W11 | plain.odt | Look up the population of Traverse City, Michigan and add one sentence about it at the end. | `web_research` | One sourced sentence is added; research does not loop. |

## Calc scenarios

| id | Fixture | Prompt (type exactly) | Expected delegation | Extra pass criteria |
|----|---------|----------------------|---------------------|---------------------|
| C1 | sales.ods | Make a column chart of revenue by region. | `charts` (`chart_upsert` with an explicit data range) | One chart on the Sales sheet, data from Region and Revenue only. |
| C2 | sales.ods | Add a line chart showing units by month. | `charts` | A line chart uses Month / Units; any existing chart is untouched. |
| C3 | sales.ods | Make the header row bold with a light blue background and show Revenue as currency. | None (`set_style` is core) | A1:D1 is bold with a blue fill; D2:D25 uses a currency format. |
| C4 | sales.ods | Highlight units over 300 in green. | `conditional_formatting` | A rule on C2:C25 (Units > 300) applies a green style. |
| C5 | sales.ods | Sort the data by revenue, highest first. | `ranges` (`range_sort`, `has_header` true) | Row 1 stays the header; column D is descending; complete rows stay together. A tool that returns ok without changing row order is a fail. |
| C6 | sales.ods | Only show the North rows. | `sheets` (AutoFilter / `sheet_filter_apply`), not `ranges` | Six North rows are visible and the header stays. |
| C7 | sales.ods | Add a Price per Unit column that divides revenue by units. | None (`write_formula_range`) | E1 is the header; E2:E25 is `=D2/C2` filled down. |
| C8 | sales.ods | Make a pivot table of total revenue by region on a new sheet. | `pivot_tables` (may also use `sheets` for the tab) | The new sheet has a pivot with four regions and a Revenue total. |

## This run (2026-11-11)

Headed pass against [PR 3303](https://github.com/KeithCu/writeragent/pull/3303) (`st-toc-anchors`), plus a two-prompt comparison on master. Chat model: `openai/gpt-oss-120b` on OpenRouter, grammar off, debug OXT, LibreOffice 25.2.

W1–W7 ran on `a639092c7` (merge of master into `st-toc-anchors`). W8–C8 ran on tip `9089fa6ec` (unify inner prompt builder, eliminate alias, narrow export error). Those two later commits do not change main-agent routing text or the indexes domain description, so the W1–W7 results still apply. Master comparison ran on `6ebf31ee1`.

What this PR adds, briefly: `DELEGATE_ANCHOR_RULE` in every specialized sub-agent prompt (name where data is and give counts; for a read, return what was asked); a new sentence in `SPECIALIZED_TASK_RULES` telling the main agent not to paste rows, values, or links into `task`; `include_links` on `indexes_list_toc_entries` (default false); and a new sentence in the indexes domain description, shown to the **main** agent too: "Apply TOC edits yourself with these tools; the main chat does not need rows read from the document passed back in." The anchor rule was present in this run's PR sub-agent prompts and absent on master. When delegation happened, task strings named locations and did not dump rows.

### Writer results (PR)

| id | Delegated? | Domain | finished | Verdict | Notes |
|----|------------|--------|----------|---------|-------|
| W1 | no | — | n/a | **FAIL** | Main agent called `apply_document_content` with `<p><span title="toc"/></p>` at the beginning. Reply: a placeholder was added; "You can now update it (Insert → Table of Contents)". No real TOC. 2 rounds. **Regression vs master** — see below. Hypothesis, not a proven root cause: the new indexes domain description says "Apply TOC edits yourself…". |
| W2 | yes, twice | `structural` (expected `indexes`) | true, but no edits | **FAIL** | Main agent overwrote the existing TOC with hand-typed plain-text rows and renamed only "3. Introduction" → "1. Introduction". Both sub-agents only called `get_document_tree` and `specialized_workflow_finished`, then claimed the work was done. Final reply falsely said all headings were renumbered and the TOC regenerated. Task strings did not paste TOC rows or links (`SPECIALIZED_TASK_RULES` check passed). 6 rounds, borderline loop. Not re-run on master. |
| W3 | no | — | n/a | **FAIL** (delegation); answer content OK | 1 round, no tools. Listed all 10 entries with page numbers, no levels, no `hyperlink_url`. Same behavior on master — not a PR regression. |
| W4 | no | — | n/a | **FAIL** | 1 round, no tools. The `include_links` path never ran. Answer listed 10 entries with rendered heading-text links; no `#…\|outline` targets in the log. |
| W5 | no | (expected: none or `tables`) | n/a | **PASS** | `apply_document_content` HTML table. Table1 is 4×3 (header + three data rows). 2 rounds. |
| W6 | yes | `tables` | true | **PASS** | Sub-agent: `table_list`, `table_get_cells`, `table_manage_structure`, `table_set_cell` ×3, finished. Table1 is 5×3, Dana / Design / Tuesday present. Main agent first tried `apply_document_content` six times, then delegated. 9 main rounds (above the ~6 guideline, not an identical-delegate loop). Task named the table and did not dump rows. |
| W7 | yes | `styles` | true | **PASS** | `style_list`, `style_update`, finished. Report: 150% on Text body. 2 rounds. Line-spacing value was not independently dumped by the inspector used in this run. |
| W8 | yes | `images` | true | **FAIL** (report) | `image_generate`, `image_list`, finished. One graphic (Image1) was placed. The main agent's task told the sub-agent to "Insert a new heading titled Lakeside Cabin" — a misread of "the heading". Final report claimed that heading was added; inspect still shows one Heading 1 and five paragraphs. The images toolset has no text tools, but the sub-agent claimed it added the heading. 2 rounds. |
| W9 | no (correct) | — | n/a | **FAIL** (Math import) | `apply_document_content` with display LaTeX. Zero embedded objects. Last paragraph is the literal text `[Math import failed] x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}`. Log: `math_mml_convert.convert_mathml_to_starmath` → `doc.getPropertyValue("Formula")` raised `UnknownPropertyException: Formula`; then `with_temp_buffer` `__exit__` failed ("Couldn't convert <traceback object> to a UNO type"). The tool still returned status ok; the agent reported success. 2 rounds. Not compared on master, so it is unknown whether this is new in the PR. |
| W10 | yes | `python` | `finished` true, status **error** | **FAIL** on criterion 2; document correct | First script used `sp.prime` without `import sympy as sp` → `VENV_EXEC_ERROR`. Second script returned 24133. Delegate result: `{"status":"error","finished":true,"answer":"…24133.","code":"SPECIALIZED_TOOL_ERROR"}` — the first error sticks after a successful retry. Sidebar showed "delegate (python) failed". Main agent then inserted the correct sentence. 3 rounds. |
| W11 | yes | `web_research` | ok (no `finished` key) | **PASS** (weak) | One `web_search`, no loop. Result was the bare number "15,678" with no source or year. Sentence added: "Traverse City, Michigan has a population of approximately 15,678." (matches the 2020 Census, cites nothing). 3 rounds. The task included an odd example number, "e.g., '115,000'". |

### Calc results (PR)

| id | Delegated? | Domain | finished | Verdict | Notes |
|----|------------|--------|----------|---------|-------|
| C1 | yes, twice | `charts` | 1st error + finished with a success answer; 2nd ok | **FAIL** | First `chart_upsert` used a non-contiguous `A2:A25,D1:D25` → `ValueError: Invalid end cell address`, but still left Chart_0 on the sheet; the error result carried a success answer. Second used A1:D25, so Units is a series alongside Revenue. Two charts remain (Chart_0 and Revenue_by_Region), both on A1:D25. Report wrongly says only columns A and D are used. 3 rounds. Not re-run on master. |
| C2 | yes | `charts` | true | **PASS** | One `chart_upsert`. Chart on B1:C25 (Month / Units). 2 rounds. Plots all 24 rows (months repeat); acceptable for this smoke test. |
| C3 | partial | `set_style` core (good), then `ranges` for currency (none expected) | true, no real work | **FAIL** | `set_style` A1:D1 bold, background `#ADD8E6` — that part worked (CharWeight 150, CellBackColor 11393254). Currency was delegated to `ranges`; the sub-agent only called `specialized_workflow_finished` and claimed "Currency format applied". D2:D25 still NumberFormat 0 (General). 3 rounds. |
| C4 | yes | `conditional_formatting` | true | **PASS** | `style_list` ×2, `conditional_formatting_add`, finished. Rule on C2:C25, greater than 300, style "Good". The four cells over 300 showed green. 2 rounds. |
| C5 | yes | `ranges` | true | **FAIL** | `range_sort` A1:D25, `sort_column` 3, descending, `has_header` true, returned ok and "sorted". After save, rows were unchanged (still North / January first; East March 9281.25 would be first if sorted). 2 rounds. |
| C6 | yes | `ranges` (expected `sheets`) | true, no tools | **FAIL** | Sub-agent only called `specialized_workflow_finished` and claimed AutoFilter was applied. 24 rows still visible, 0 hidden. The main agent picked a domain that has no filter tool. 2 rounds. |
| C7 | no (correct) | — | n/a | **PASS** | E1 "Price per Unit", E2:E25 `=D2/C2` filled down. 5 rounds (unnecessary `insert_structure`, two `write_formula_range`). |
| C8 | yes | `pivot_tables`, then `sheets`, then `pivot_tables` | 1st error (honest); then ok | **PASS** | First `pivot_tables` delegate correctly reported that the target sheet did not exist. Main agent delegated to `sheets` (`sheet_create` RevenueByRegion) then `pivot_tables` again. Pivot has four regions (East 30285.03, North 18932.69, South 26383.84, West 27496.28) and Total 103097.84. 4 rounds. |

### Master comparison (`6ebf31ee1`, same model, same prompts)

The master debug OXT has no `DELEGATE_ANCHOR_RULE` and no "Apply TOC edits yourself" sentence.

| Prompt | Master | PR | Conclusion |
|--------|--------|----|------------|
| W1: Add a table of contents at the top. | **PASS**. Delegated to `indexes`, `indexes_create {kind:"toc", title:"Table of Contents", target:"beginning"}`, `finished` true. One real document index, 10 Contents paragraphs listing all headings. No `apply_document_content`. 2 rounds. | **FAIL**. No delegate; placeholder span. | **PR regression.** Master still delegates and creates a real TOC. |
| W3: What's in the table of contents? | **FAIL**. No tools, 1 round, all 10 entries with page numbers, no levels. | Same. | **Not a PR regression.** On both builds, this model answered from document text. |

W2 and C1 were not re-run on master.

### Bugs seen in this run

1. **PR regression: TOC insert no longer delegates (W1).** Master delegates to `indexes` and creates a real TOC. On `st-toc-anchors` the main agent inserts a `<span title="toc"/>` placeholder via `apply_document_content`. Hypothesis, not proven: the new indexes domain description, visible to the main agent, says "Apply TOC edits yourself with these tools…". A possible direction is to reword so "yourself" cannot be read as the main chat, or to keep that sentence out of the main-agent domain list.
2. **Sub-agents call `specialized_workflow_finished` with a success answer and no (or insufficient) edit tools**, and the main agent then tells the user the work is done. Seen in W2 (`structural`, no edits), W8 (`images` claimed a new heading it cannot create), C3 (`ranges` claimed currency format), and C6 (`ranges` claimed AutoFilter; that domain has no filter tool).
3. **`SPECIALIZED_TOOL_ERROR` sticks after a successful retry (W10).** First Python script failed, second returned 24133, but the delegate result was still `status` error with the first error message. The same pattern appeared on C1's first `charts` delegate (error + `finished` true + a success answer). C8's first pivot error was honest.
4. **LaTeX → Math object import is broken and reported as success (W9).** `UnknownPropertyException` on "Formula", then a traceback UNO conversion error. The document gets `[Math import failed] …`; the tool status is ok. Unknown whether this is new in the PR.
5. **`range_sort` reports success and does not sort (C5).**
6. **`chart_upsert` cannot take a non-contiguous Region+Revenue range; a failed upsert can still leave a chart; an error result can carry a success answer (C1).**
7. **Filter task routed to `ranges` instead of `sheets` (C6)**, and currency formatting routed to `ranges` instead of core `set_style` (C3).
8. **W4 `include_links` was never exercised**, because the main agent did not delegate a TOC read. W3, W4, and W3m all answered from context.

Repro for each: start from a fresh copy of the fixture named above, use `openai/gpt-oss-120b`, and send that scenario's prompt exactly.
