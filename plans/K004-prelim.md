# K004 Preliminary Planning

## Summary Table
Total items: 14 (VALID: 14 / ALREADY-FIXED: 0 / INVALID: 0)

| File | Item | Status |
| --- | --- | --- |
| plugin/audio/kokoro_g2p.py | K1 | VALID |
| plugin/audio/kokoro_g2p.py | K2 | VALID |
| plugin/audio/kokoro_g2p.py | K3 | VALID |
| plugin/audio/kokoro_g2p.py | K4 | VALID |
| plugin/audio/kokoro_g2p.py | K5 | VALID |
| plugin/audio/kokoro_g2p.py | K6 | VALID |
| plugin/audio/kokoro_pool.py | K1 | VALID |
| plugin/audio/kokoro_pool.py | K2 | VALID |
| plugin/audio/kokoro_pool.py | K3 | VALID |
| plugin/audio/kokoro_pool.py | K4 | VALID |
| plugin/audio/kokoro_pool.py | K5 | VALID |
| plugin/audio/kokoro_pool.py | K6 | VALID |
| plugin/audio/kokoro_pool.py | K7 | VALID |
| plugin/audio/kokoro_pool.py | K8 | VALID |

## plugin/audio/kokoro_g2p.py

### [K1] delete KOKORO_ONNX_SCRIPT string-built program; one synthesis path.
- **Status:** VALID
- **Reason:** The file `kokoro_g2p.py` still contains `_build_kokoro_onnx_script` and `KOKORO_ONNX_SCRIPT` which dynamically generate Python scripts as strings, duplicating logic in `kokoro_worker.py:handle_kokoro_job`. Both scripts use `Misaki` to phonemize and `Kokoro` to synthesize.
- **Lines:** `plugin/audio/kokoro_g2p.py:277-337` and `plugin/audio/tts_service.py:1009`.
- **Fix sketch:**
  1. Extract `kokoro_worker.py:handle_kokoro_job` body to a new `synthesize_to_file` in a new file (e.g. `plugin/audio/kokoro_synth.py`) or as a shared helper function in `plugin/audio/kokoro_worker.py`.
  2. Add `--once` flag to `kokoro_worker.py`.
  3. Replace `tts_service.py` one-shot command `["-c", KOKORO_ONNX_SCRIPT, ...]` with calling `kokoro_worker.py --once`.
  4. Delete string generation functions from `kokoro_g2p.py`. Update tests in `test_kokoro_g2p.py` and `test_tts_service.py`.

### [K2] cache successful Misaki probe across replies.
- **Status:** VALID
- **Reason:** `ensure_kokoro_misaki` calls `_venv_probe_ok` which forks a probe script to test G2P every reply (because `_cached_misaki_ready` relies on generation index, which resets). No long-term success cache exists.
- **Lines:** `plugin/audio/kokoro_g2p.py:450-470`.
- **Fix sketch:**
  1. Introduce a module-level lock and success cache mapping `(py_exe, lang)` to `True`.
  2. In `ensure_kokoro_misaki`, short-circuit if `(py_exe, lang)` is in the cache.
  3. Tests: ensure subsequent calls use the cache, different py_exe probes again. Add fixture to clear cache in tests.

### [K3] compute kokoro_g2p_lang once per utterance.
- **Status:** VALID
- **Reason:** `kokoro_g2p_lang` scans the text via `_text_is_latin_script` and is called multiple times throughout the speech pipeline per clip (prepare, one-shot, warm etc).
- **Lines:** `plugin/audio/tts_service.py:835, 954, 1005, 1090, 1146` and `plugin/audio/kokoro_g2p.py:177-194`.
- **Fix sketch:**
  1. Resolve `lang = kokoro_g2p_lang(text, voice)` early in `tts_service._speak_kokoro_local`.
  2. Pass `lang` down to `_prepare_kokoro_misaki`, `_kokoro_audio_file`, `_kokoro_warm_to_file`, `_kokoro_oneshot_to_file`.

### [K4] TEST: probe/catalog drift test (tests).
- **Status:** VALID
- **Reason:** `kokoro_misaki_probe_code` just calls the frontend with the probe text. It doesn't exit with non-zero if the returned phoneme string is empty. `KOKORO_VOICE_PREFIX_TO_LANG` maps prefix to lang, which must be kept in sync with the real voice catalog.
- **Lines:** `plugin/audio/kokoro_g2p.py:250-261` and `plugin/audio/kokoro_g2p.py:157-174`.
- **Fix sketch:**
  1. Modify `kokoro_misaki_probe_code` to check the length of returned phonemes and raise `SystemExit` if empty.
  2. Add test comparing `KOKORO_VOICE_PREFIX_TO_LANG` against all entries in the `KOKORO_CATALOG_ITEMS` voice list.

### [K5] structured fallback info, not string parsing.
- **Status:** VALID
- **Reason:** `_notify_kokoro_g2p_fallback` looks for `G2P_FAILED_MARK` in stderr. This requires sniffing strings instead of a clean, structured payload.
- **Lines:** `plugin/audio/tts_service.py:793-803`.
- **Fix sketch:**
  1. Have the shared `synthesize_to_file` return a structured dictionary containing fallback warnings/states.
  2. Read this structure from the process result (stdout/IPC) instead of `G2P_FAILED_MARK` on stderr.
  3. Simplify `_notify_kokoro_g2p_fallback` logic.

### [K6] small cleanups (MisakiLangSpec.lang redundant).
- **Status:** VALID
- **Reason:** `MisakiLangSpec` has a `lang` field that mirrors the dict key. Snippet string formatting uses duplication.
- **Lines:** `plugin/audio/kokoro_g2p.py:81-123`.
- **Fix sketch:**
  1. Remove `lang` field from `MisakiLangSpec`.
  2. Unify pip package tuple and snippet generation.

## plugin/audio/kokoro_pool.py

### [K1] reuse BaseProcessWorker instead of _KokoroWorker copy.
- **Status:** VALID
- **Reason:** `_KokoroWorker` re-implements a lot of standard process-worker functionality with a custom pipe framing, missing features from `BaseProcessWorker` (like IO ownership and stderr tailing).
- **Lines:** `plugin/audio/kokoro_pool.py:77-250` vs `plugin/framework/process_worker.py:323`.
- **Fix sketch:**
  1. Make `_KokoroWorker` inherit from or wrap `Child` / `BaseProcessWorker` using its IO thread model, or extract its `Deadline` / io ownership into `_KokoroWorker`.
  2. Adjust wait/reap/handshake.

### [K2] surface child stderr on failure.
- **Status:** VALID
- **Reason:** Process crashes (e.g. missing `kokoro_onnx`) print errors to stderr which is discarded, returning only `WORKER_SPAWN_FAILED` to the user/logs.
- **Lines:** `plugin/audio/kokoro_pool.py:116, 147-149, 198-205`.
- **Fix sketch:**
  1. Pull non-blocking stderr text from `ManagedSubprocess`.
  2. Include it in the error dictionary (`error_dict`) on timeout or crash.

### [K3] spawn+job on one Deadline.
- **Status:** VALID
- **Reason:** Spawn timeout and job execution timeout are separated. A slow spawn can eat 20s and then still give the job its full timeout, making jobs hang significantly over budget.
- **Lines:** `plugin/audio/kokoro_pool.py:325, 367-419`.
- **Fix sketch:**
  1. Wrap the entire request in `process_worker.Deadline`.
  2. Pass `remaining` to `respawn` and job execution. Return `EXECUTION_TIMEOUT` if spent.

### [K4] one active-job object.
- **Status:** VALID
- **Reason:** Active job status is split into `_worker`, `_inflight`, `_exec_token` making synchronization error-prone, requiring redundant token checks.
- **Lines:** `plugin/audio/kokoro_pool.py:282-293, 345-438`.
- **Fix sketch:**
  1. Encapsulate into an `_ActiveJob` class.
  2. Refactor `execute` and `cancel_inflight` to use and check `job.cancelled`.

### [K5] kill worker after interrupted handshake.
- **Status:** VALID
- **Reason:** `KeyboardInterrupt` or `SystemExit` inside `respawn` or `execute` can leave a partially-handshaked worker in the pool, ruining subsequent IPC frames.
- **Lines:** `plugin/audio/kokoro_pool.py:147, 198-205`.
- **Fix sketch:**
  1. Catch `BaseException` during handshake/read and kill/abandon the worker, re-raise the exception.
  2. Add test asserting worker drop on `KeyboardInterrupt` in handshake.

### [K6] Stop token read outside _speech_lock race (tts_service).
- **Status:** VALID
- **Reason:** `tts_service.stop_speech` fetches the cancel token *after* releasing `_speech_lock`, causing race condition where it grabs the token of a newly-started unrelated job.
- **Lines:** `plugin/audio/tts_service.py:355-375` and `plugin/audio/kokoro_pool.py:570-587`.
- **Fix sketch:**
  1. Call pool cancel logic from within `with _speech_lock` block, or capture generation index with the token.
  2. Delete global module-level wrappers `get_kokoro_inflight_token`.

### [K7] resolve venv once, pass py_exe.
- **Status:** VALID
- **Reason:** `get_kokoro_pool` resolves the Python executable path for the Kokoro venv on every call (e.g. per sentence), probing the filesystem heavily.
- **Lines:** `plugin/audio/kokoro_pool.py:543-556` and `plugin/audio/tts_service.py:1093`.
- **Fix sketch:**
  1. Change `get_kokoro_pool` signature to take `py_exe` as argument or cache it correctly.
  2. Limit the size of `_seen_warnings` (e.g., using `LRUCache` or a simple bound).

### [K8] dead-child cleanup via close_stdio/unregister.
- **Status:** VALID
- **Reason:** `kill()` unconditionally uses `kill_tree` and `abandon` (triggering heavy 5-pass reaper), even when the child is already dead.
- **Lines:** `plugin/audio/kokoro_pool.py:216-229`.
- **Fix sketch:**
  1. Check if the process is already dead using a short `wait()`.
  2. If dead, prefer `close_stdio` and `unregister` to avoid background reaper queues. Add `stderr_name`.
