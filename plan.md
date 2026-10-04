1.  **Fix `diff_chunk_rows` to explicitly accept `doc_url`:**
    *   In `plugin/embeddings/venv/embeddings_sqlite.py`, modify `diff_chunk_rows_in_db` to take `doc_url: str` as an argument.
        *   If `chunks` is empty, use the provided `doc_url` to query `conn.execute("SELECT ... FROM chunks WHERE doc_url = ?", (doc_url,))`.
        *   Ensure that when `chunks` is empty, the returned `to_delete` list correctly contains all existing chunks for that `doc_url`.
    *   In `plugin/embeddings/embeddings_cache.py`, modify `diff_chunk_rows` to accept `doc_url: str` and pass it to `diff_chunk_rows_in_db`.
    *   In `plugin/embeddings/venv/embeddings_folder_maintain.py`, update the call to `diff_chunk_rows` in `_incremental_refresh` to pass `entry.url`.

2.  **Fix marking of failed extractions:**
    *   In `plugin/embeddings/venv/embeddings_folder_maintain.py`, modify `_cold_build` and `_incremental_refresh`.
    *   We need to differentiate between an empty successful extraction and a failed extraction.
    *   In `plugin/embeddings/venv/embeddings_folder_maintain.py`, `_extract_file_chunks(entry)` delegates to `indexable_chunks_from_path`, which calls `extract_indexable_passages(norm)`.
    *   The `extract_calc_rows` and `extract_spreadsheet_rows` currently return `[]` on failure (missing pandas/engine or exception). We need to distinguish success (0 rows) from failure.
    *   Actually, a simpler way is to raise an explicit exception like `ImportError` or a custom `ExtractionFailedError` instead of returning `[]` and swallowing the exception in `extract_calc_rows`/`extract_spreadsheet_rows`. But we should check how they are currently used. Let's look at `extract_calc_rows`. Currently it catches `ImportError` and `Exception` and returns `[]`.
    *   Alternatively, return `None` on failure from `extract_calc_rows` and `extract_spreadsheet_rows`, and handle `None` down the chain? The return type is `list[str]`.
    *   Let's check `extract_calc_rows`. If it returns `[]` on failure, `indexable_chunks_from_path` will return `(0, [])`.
    *   Let's modify `extract_calc_rows` to raise an exception, or return `None`, or have `indexable_chunks_from_path` catch it.
    *   Wait, the memory says: "Distinguish empty success from extract failure. Add unit tests that fail when an emptied sheet keeps old chunk rows and when a failed extract is marked fresh."
    *   If `extract_calc_rows` returns `None` on failure, we can distinguish. Or it can just raise the exception. Let's look at `extract_calc_rows` and `extract_spreadsheet_rows`. I'll modify them to raise an `ExtractError` or `ValueError` or just let the exception propagate, but wait, `extract_calc_rows` has logging. If we change it to return `None`, we need to adjust the typing.
    *   Let's check the return type of `extract_calc_rows` in `plugin/embeddings/venv/embeddings_odf_extract.py`: `def extract_calc_rows(path: str) -> list[str]:`. We can change it to `list[str] | None`.

3.  **Implement the Unit Tests:**
    *   Write test `test_emptied_calc_retains_chunks` in `tests/embeddings/venv/test_embeddings_extract_failures.py`.
    *   Write test `test_failed_extract_cold_build_skips` in `tests/embeddings/venv/test_embeddings_extract_failures.py`.
    *   Write test `test_failed_extract_incremental_refresh_skips` in `tests/embeddings/venv/test_embeddings_extract_failures.py`.

4.  **Complete pre-commit steps.**
    *   Run tests, verify no linting errors, check the format.

5.  **Submit the PR.**
