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

The benchmark test in [`tests/writer/test_redlined_contract_uno.py`](../../tests/writer/test_redlined_contract_uno.py) is opt-in via `WRITERAGENT_BENCH=1`:

```bash
WRITERAGENT_BENCH=1 ~/Desktop/Python/wa-pipeline-scripts/uno-probe.sh --timeout 900 tests/writer/test_redlined_contract_uno.py test_redlined_contract_read_timings_uno
```

## Reference numbers

Measured on `2026-11-25` at commit `7fbf6accc`, LibreOffice `26.8.3.1`, CPU `Intel Core Ultra 7 258V` (8 cores):

| Document | Pages | Changes | Read | First (s) | Median (s) | Hits | Warning |
|---|---|---|---|---|---|---|---|
| redlined | 301 | 484 | get_content full | 8.286 | 7.557 |  |  |
| redlined | 301 | 484 | search, 20 of many hits | 28.554 | 25.215 | 3788 |  |
| redlined | 301 | 484 | get_tree heading_only | 1.738 | 0.548 |  |  |
| redlined | 301 | 484 | get_tree default | 0.629 | 0.603 |  |  |
| redlined | 301 | 484 | table_get_cells | 0.438 | 0.430 |  |  |
| no changes | 295 | 0 | get_content full | 3.118 | 5.172 |  |  |
| no changes | 295 | 0 | search, 20 of many hits | 0.260 | 0.260 | 3794 |  |
| no changes | 295 | 0 | get_tree heading_only | 1.794 | 0.311 |  |  |
| no changes | 295 | 0 | get_tree default | 0.331 | 0.340 |  |  |
| no changes | 295 | 0 | table_get_cells | 0.045 | 0.046 |  |  |
| redlined, one list style | 301 | 484 | get_content full | 7.428 | 7.428 |  | XHTML export failed; content came from the legacy HTML export and has no style tokens. It still includes text that pending tracked deletions remove. |

## Observations

- **`search` dominates (~97× slower):** `plugin/writer/search.py` calls `TrackedDeletions.overlaps` (`plugin/doc/text_helpers.py`) per hit, doing `compareRegionStarts` across all deletion bounds for hits that overlap nothing, which is most of them (~3,788 hits).
- **`table_get_cells` (~9.3× slower):** `plugin/writer/specialized/tables.py` calls `TrackedDeletions.state_at` per cell.
- **`get_tree` (~1.8× slower median):** First `get_tree heading_only` is also slower on both documents because it creates heading bookmarks (`ensure_heading_bookmarks`, 300 headings).
- **`get_content` full (~1.5× slower median):** The hidden-copy XHTML export route adds modest overhead compared to `search`.
- **Single shared list style falls back:** With 1,680 items in one list style (`shared_list=True`), LibreOffice's `odf2xhtml` XSLT hits `xsltMaxDepth` (3000) in `countListItem` / `countListItemTillStartValue` (`filter/source/xslt/odf2xhtml/export/xhtml/body.xsl`). `get_content` falls back to legacy StarWriter HTML export (`plugin/writer/html_export.py`), returning a warning that style tokens are missing and pending tracked deletions remain in the text.
- **Intermittent `soffice` crash:** Both the planner (once in four runs) and runs on this branch (once before the `origin/master` merge and twice after it before the third run succeeded) saw `soffice` exit with `Unspecified Application Error` (`DisposedException` on the URP bridge), cause unknown.
