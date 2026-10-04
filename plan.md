## Plan
1. **Fix UNO `getControl` on Background Thread**
    - **Problem**: `getControl` is called off the main thread inside `_do_background_search` (in `_update_results_ui`) and `_do_rebuild` (in `_update_rebuild_ui`), which is unsafe.
    - **Fix**: Update `_update_results_ui` and `_update_rebuild_ui` in `plugin/embeddings/search_ui.py` to NOT accept UI controls as arguments, because `getControl` must only be called on the main thread. Instead, we should pass the required strings and query the controls inside the `execute_on_main_thread` lambda. We should capture a reference to `self._dlg` earlier or pass it.
    - **Files**: `plugin/embeddings/search_ui.py`, `tests/embeddings/test_search_ui.py`

2. **Fix `doc_url_filter` Apply During Retrieval (Not After Truncation)**
    - **Problem**: In `hybrid_corpus_search`, `doc_url_filter` is applied after fetching `fetch_k` hits from `fts_corpus_search` and `vec0_search`. If all the fetched results happen to be for other files, zero results are returned even if valid matches exist deeper in the index.
    - **Fix**: Pass `doc_url_filter` into `fts_corpus_search` and `vec0_search` directly. Modify `vec0_search` to use an `IN` subquery (`v.rowid IN (SELECT chunk_id FROM chunks WHERE doc_url = ?)`) to filter the vec0 search before/during knn evaluation. For `fts_corpus_search`, modify the FTS SQL query to `JOIN chunks c` and add `AND c.doc_url = ?` (this requires passing the param appropriately). Note that SQLite vec0 requires `k = ?` to act as an index query, but filters are possible by chaining `AND rowid IN (...)`.
    - **Files**: `plugin/embeddings/venv/embeddings_hybrid_search.py`, `plugin/embeddings/venv/embeddings_sqlite.py`, `tests/embeddings/venv/test_embeddings_sqlite.py`, `tests/embeddings/venv/test_embeddings_hybrid_search.py`

3. **Fix Rebuild Heartbeat UI Callback Not Exception-Guarded**
    - **Problem**: In `SearchDialog._run_rebuild`, `heartbeat_fn` has a `ui_update` callback passed to `execute_on_main_thread`. If the dialog is closed, `results_ctrl` may throw when its model is accessed, propagating an exception up and killing the background rebuild.
    - **Fix**: Wrap `results_ctrl.getModel().Text` operations inside `ui_update` with a `try...except Exception` block. Ensure we do not crash if the control is disposed. The `getControl` should also be moved inside the main thread execution!
    - **Files**: `plugin/embeddings/search_ui.py`, `tests/embeddings/test_search_ui.py`

4. **Add Pre-commit steps**
    - Run pre-commit instructions and fix any linter errors.
