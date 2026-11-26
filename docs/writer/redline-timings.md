# Redlined 300-Page Contract Fixture and Read Benchmark

## Fixture

[`tests/writer/redlined_contract_fixture.py`](../../tests/writer/redlined_contract_fixture.py) generates a synthetic ~300-page flat ODF (`.fodt`) contract in plain Python (`build_redlined_contract`):

- 60 articles × 4 sections × 7 clauses (1,680 numbered list items, 300 headings with `1.1`/`1.2` legal numbering, plus a table of contents).
- Default redlined variant: 244 deletions and 240 insertions (484 tracked changes) by `Opposing Counsel`, including 4 three-paragraph deletions and 60 changes inside 6 fee-schedule tables (each 20×4 with horizontal and vertical cell merges).
- `redline_every=0`: identical base text and direct formatting (`Bold` spans), apart from the 4 multi-paragraph-deletion clauses (where the multi-paragraph branch takes precedence in the redlined variant).
- `shared_list=True`: uses one shared list style for all 1,680 clauses instead of one list style per article.

## Why the benchmark runs inside `soffice`

`plugin/testing_runner.py` drives LibreOffice over a URP pipe where every UNO call is an IPC round trip. Measuring tools over URP exaggerates call-heavy operations and ranks the wrong hot spots (for example, `get_tree` over URP took ~145 s on first call vs ~1.4 s inside `soffice`; `get_content` took ~44.9 s vs ~4.5 s; `table_get_cells` took ~7.0 s vs ~0.31 s).

[`tests/writer/redlined_contract_bench.py`](../../tests/writer/redlined_contract_bench.py) registers a Python UNO component (`com.sun.star.loader.Python.activate`) that dispatches the benchmark onto `soffice`'s main thread via `com.sun.star.awt.AsyncCallback`, purges any pre-imported `plugin*` modules from the throwaway profile's installed OXT, and imports `plugin` from the checkout.

## Running the benchmark

The benchmark test in [`tests/writer/test_redlined_contract_uno.py`](../../tests/writer/test_redlined_contract_uno.py) is opt-in via `WRITERAGENT_BENCH=1` and fails if any redlined median exceeds `MEDIAN_CEILINGS_S`:

```bash
WRITERAGENT_BENCH=1 ~/Desktop/Python/wa-pipeline-scripts/uno-probe.sh --timeout 900 tests/writer/test_redlined_contract_uno.py test_redlined_contract_read_timings_uno
```

The same command is what a nightly job would call.

## Reference numbers

Measured on `2026-11-25` on base `96ffb0404` with `AUDIT-HOIST-LOOPS`, LibreOffice `26.8.3.1`, CPU `Intel Core Ultra 7 258V` (8 cores):

| Document | Pages | Changes | Read | First (s) | Median (s) | Hits | Warning |
|---|---|---|---|---|---|---|---|
| redlined | 301 | 484 | get_content full | 4.801 | 4.591 |  |  |
| redlined | 301 | 484 | search, 20 of many hits | 0.750 | 0.746 | 3791 |  |
| redlined | 301 | 484 | get_tree heading_only | 1.380 | 0.491 |  |  |
| redlined | 301 | 484 | get_tree default | 0.482 | 0.482 |  |  |
| redlined | 301 | 484 | table_get_cells | 0.070 | 0.072 |  |  |
| no changes | 295 | 0 | get_content full | 2.670 | 2.494 |  |  |
| no changes | 295 | 0 | search, 20 of many hits | 0.247 | 0.244 | 3794 |  |
| no changes | 295 | 0 | get_tree heading_only | 0.858 | 0.278 |  |  |
| no changes | 295 | 0 | get_tree default | 0.279 | 0.282 |  |  |
| no changes | 295 | 0 | table_get_cells | 0.039 | 0.039 |  |  |
| redlined, one list style | 301 | 484 | get_content full | re-measure | re-measure |  | re-measure |

Before vs. after `TrackedDeletions` binary search on the redlined 301-page fixture (median seconds; "Before" from `7fbf6accc`, "After" includes #4006):

| Read | Before (`7fbf6accc`) | After |
|---|---|---|
| search, 20 of many hits | 25.215 | 0.746 |
| table_get_cells | 0.430 | 0.072 |
| get_tree heading_only | 0.548 | 0.491 |
| get_tree default | 0.603 | 0.482 |

## Observations

- **`search` (~3.1× slower than no-changes):** `TrackedDeletions` (`plugin/doc/text_helpers.py`) splits pending deletions into ordered runs and binary searches `SwXTextRange` bounds in `overlaps`, instead of scanning all 244 deletions per hit across ~3,791 hits. Part of the remaining cost over no-changes is #4006's portion walk of each split paragraph and `visible_text` call for overlapping hits.
- **`table_get_cells` (~1.8× slower than no-changes):** `TrackedDeletions.state_at` (`plugin/doc/text_helpers.py`) uses the same binary search per cell.
- **`get_tree` (~1.7–1.8× slower median):** The portion walk in `paragraph_deletion_flags` runs twice on a cold tree (`build_heading_tree` in `plugin/doc/text_helpers.py` and `_load_tree_context` in `plugin/writer/tree.py`) and costs about 0.2 s on 300 pages. Gating it on deletions saved under 0.1 s on an insertion-only document, so it stays as is. First `get_tree heading_only` is also slower on both documents because it creates heading bookmarks (`ensure_heading_bookmarks`, 300 headings).
- **`get_content` full (~1.8× slower median):** The hidden-copy XHTML export route adds modest overhead compared to `search`.
- **Single shared list style falls back:** LibreOffice's `odf2xhtml` XSLT hits `xsltMaxDepth` (3000) in `countListItem` / `countListItemTillStartValue` (`filter/source/xslt/odf2xhtml/export/xhtml/body.xsl`) at about 380 items of one style split across `text:list` blocks, and in the `listItemSibling` recursion at about 787 items in one `text:list` (measured on LibreOffice 26.8.3.1). `get_content` falls back to legacy StarWriter HTML export (`plugin/writer/html_export.py`), hiding pending tracked deletions via a hidden copy without `<text:tracked-changes>` and warning to use `scope='range'`.
- **Intermittent `soffice` crash:** Both the planner (once in four runs) and #3999 runs saw `soffice` exit with `Unspecified Application Error` (`DisposedException` on the URP bridge), cause unknown.
