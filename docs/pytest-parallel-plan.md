# Pytest Parallelization Plan (Phase 0)

This document categorizes tests and shared state to prepare for future execution of `pytest -n` (multiprocessing).
Currently, LibreOffice/UNO tests remain serial, and this phase only introduces classification and markers.

## Shared Mutable State Inventory (Risks for `-n` concurrency)

| State / Resource | Risk Description | Remediation Plan (Future Phase) |
|---|---|---|
| Temp Directories | Tests writing to hardcoded paths (e.g., `/tmp/wa_test`) may collide. | Use pytest `tmp_path` fixture or `tempfile.TemporaryDirectory`. |
| CWD (Current Working Dir) | Scripts changing `os.chdir()` affect the entire process. | Avoid `os.chdir()`, use absolute paths or monkeypatch `os.chdir` per test. |
| Ports (MCP, HTTP) | Hardcoded ports (e.g., 9000, 5000) will bind-fail on concurrent startup. | Assign dynamic open ports or use port pools. |
| Environment Variables | Mutating `os.environ` (e.g., `WRITERAGENT_*`) leaks across tests. | Use `monkeypatch.setenv()` fixture exclusively. |
| Soffice Pipe / Profile | Single `officehelper.bootstrap()` or headless process cannot handle parallel UNO calls safely. | LO tests stay serial (`-m lo` runs separately or restricted to `-n 0`). |
| Caches | In-memory LRU or singleton caches across modules (e.g. formula cache). | Clear caches in `autouse` fixtures per-test. |
| SQLite/History DBs | Hardcoded `history.db` or embeddings DB will get locked/corrupted. | Use `:memory:` or temporary per-process DB files. |
| Module Globals | Shared dicts/flags in `plugin/framework/config.py` mutated directly. | Reset state in fixtures or use `unittest.mock.patch`. |
| Downloaded Fixtures | Concurrent downloads (e.g., HuggingFace datasets) might write to same file. | Pre-download in `pytest_configure` or use lock files/flock. |

## Test Inventory Table

Based on a static analysis scan of the `tests/` directory:

| Category | Definition | File / Target | Target Concurrency |
|---|---|---|---|
| `unit` | Pure Python code. No LibreOffice interaction. | ~352 files | `-n auto` (High priority) |
| `mocked_uno` | Python code mocking UNO interfaces (`MagicMock(spec=XInterface)`). | ~140 files | `-n auto` (High priority) |
| `lo` | Live LibreOffice instance, requires `uno` imports and `officehelper.bootstrap()`. | ~79 files (`*_uno.py` etc.) | `-n 0` (Serial only) |
| `eval` | Prompt evaluation / script heavy scenarios. | ~8 files (`pytest.mark.eval`) | `-n auto` (if state allows) |

## Plan

1. In this phase (Phase 0), we have registered `unit`, `lo`, and `eval` markers in `pyproject.toml`.
2. Tests are tagged automatically with these markers based on content.
3. CI continues to run tests serially (`-n` is NOT enabled yet).
4. In future phases, `pytest -m "not lo" -n auto` will run the pure unit and mocked UNO tests in parallel, while `pytest -m lo` will run serially.
