# K006 Preliminary Planning Evaluation

## Summary

| Status | Count |
| :--- | :--- |
| **VALID** | 15 |
| **ALREADY-FIXED** | 0 |
| **INVALID** | 0 |

---

## plugin/audio/tts_endpoint.py

### [K1] use provider_detection instead of substring host test
- **Status:** VALID
- **Reason:** The code still uses a raw `.lower()` substring check for `openrouter.ai`, hand-rolls the URL path, hard-codes the User-Agent and Authorization headers, and leaks raw URLs with potential secrets in `log.info()`.
- **References:** `plugin/audio/tts_endpoint.py:246` (substring check), `256` (URL join), `265` (User-Agent), `284, 296, 352` (logging raw URL).
- **Fix Sketch:** Use `is_openrouter_endpoint()` from `provider_detection`. Construct URLs properly. Always wrap logged URLs with `public_target()`. Use `build_auth_headers()` for auth setup instead of manual Bearer injection. Update tests to assert raw secrets never appear in logs and URLs are formed correctly.

### [K2] learned response_format keyed by endpoint+model
- **Status:** VALID
- **Reason:** `cached_tts_response_format` currently accepts only a `model_id` without the endpoint. It also only remembers the format if a retry happened or if the type strictly matched PCM/WAV.
- **References:** `plugin/framework/client/model_fetcher.py:825` (cache takes only model_id); `plugin/audio/tts_endpoint.py:336-341` (conditional remembering).
- **Fix Sketch:** Update the cache keying in `model_fetcher.py` to include the `endpoint` origin. In `tts_endpoint.py`, remember the actual format on every successful HTTP fetch. Simplify the alternate format parsing into a lookup table. Update tests to check multi-endpoint ping-pong caching.

### [K3] magic bytes over Content-Type
- **Status:** VALID
- **Reason:** The ladder in `_download_endpoint_speech` still allows `Content-Type` to win over the sniffed magic bytes if the server labels it (e.g., `audio/wav` header with `.mp3` bytes).
- **References:** `plugin/audio/tts_endpoint.py:321-325`.
- **Fix Sketch:** Restructure the conditional to prioritize the sniffed format first (`if sniffed == "wav": ...`). Add more PCM variants to `_content_type_is_pcm`. Consolidate format names into a single table/dataclass. Update tests to assert precedence rules.

### [K4] return bytes not temp path
- **Status:** VALID
- **Reason:** `_download_endpoint_speech` handles temp file creation and cleanup directly, causing circular/lazy imports from `tts_service` at the start of the function.
- **References:** `plugin/audio/tts_endpoint.py:231-236` (lazy imports), `358-368` (temp file creation).
- **Fix Sketch:** Have `_download_endpoint_speech` return `(bytes, suffix)` instead of a path. Move the temp file creation (`_new_speech_temp`) and cleanup to the caller `_speak_endpoint` in `tts_service.py`. Remove the lazy imports. Update `test_tts_endpoint.py` to not require temp file mocks.

### [K5] pass max_bytes to sync_request; report write failures
- **Status:** VALID
- **Reason:** `_post_audio_speech` does not pass a `max_bytes` limit to `sync_request`, and if temp file writing fails (`OSError`), it logs but silently returns `None` without notifying status.
- **References:** `plugin/audio/tts_endpoint.py:177-196` (`sync_request` missing `max_bytes`), `358-366` (silent write failure).
- **Fix Sketch:** Pass `max_bytes=33554432` to `sync_request`. Call `_notify_tts_status()` if the write fails with `OSError`, or if an empty body (200 OK) is returned. Add tests to verify max bytes rejection and empty body errors.

### [K6] pipeline endpoint chunks (fetch next while playing)
- **Status:** VALID
- **Reason:** `_speak_endpoint` iterates through chunks serially, waiting for the audio file playback to completely finish before requesting the next chunk.
- **References:** `plugin/audio/tts_service.py:1397-1418`.
- **Fix Sketch:** Introduce a `_ReadyQueue` and a background thread (`run_in_background`) in `_speak_endpoint` to download chunks in parallel with playback. Make the first chunk smaller (~600 chars) for faster time-to-first-audio. Update tests for pipelining.

### [K7] TEST: tests
- **Status:** VALID
- **Reason:** There's no autouse fixture cleaning up the `tts_endpoint` caches in `test_tts_endpoint.py`, leading to duplication. Several edge cases (host spoofing, sniffing precedence) lack tests.
- **References:** `tests/audio/test_tts_endpoint.py`.
- **Fix Sketch:** Add the autouse cleanup fixture. Add the missing table-driven tests for URL building, sniffing, and `_content_type_is_pcm`. Emit structured INFO logs per clip to measure inter-chunk gaps.

---

## plugin/audio/tts_models.py

### [K1] cancellation as first-class result
- **Status:** VALID
- **Reason:** `_download_to` returns a raw boolean for success, causing `_resolve_kokoro_model_files` and others to treat Stop/cancellations the same as network failures, polluting the failed download cache and UI status.
- **References:** `plugin/audio/tts_models.py:132-144` (`_download_to` returns `bool`), `236` (records failure unconditionally if not ok).
- **Fix Sketch:** Change `_download_to` to return an enum (e.g., `DownloadOutcome.OK | CANCELLED | FAILED`). Update resolvers to ignore `CANCELLED` states without marking the model as failed. Add cancellation tests.

### [K2] Piper pair deletion race deletes dest
- **Status:** VALID
- **Reason:** On failure, `_download_piper_pair` actively calls `os.remove()` on the destination files, which could delete files just successfully written by a concurrent generation request.
- **References:** `plugin/audio/tts_models.py:257-260`.
- **Fix Sketch:** Only remove temporary files in `_download_to` and rename them to the destination as a transactional final step. Never `os.remove` the destination paths. Add overlapping download tests.

### [K3] validate downloaded bytes (HTML/0-byte)
- **Status:** VALID
- **Reason:** The cache relies solely on `os.path.exists()`, meaning 0-byte files or captive-portal HTML pages saved as `.onnx` are accepted forever.
- **References:** `plugin/audio/tts_models.py:189`, `278`.
- **Fix Sketch:** In `_download_to`, check `Content-Type` for binary types, enforce a minimum size, and verify `Content-Length`. Treat under-sized existing files as missing and re-download. Add tests for truncated and HTML responses.

### [K4] single-flight per destination
- **Status:** VALID
- **Reason:** There's no lock around model downloading. If two generations request the same missing model concurrently, both download it fully and race on `os.replace`.
- **References:** `plugin/audio/tts_models.py:132-181`.
- **Fix Sketch:** Add a global `dict[str, Condition]` download lock registry. The first caller downloads; subsequent callers wait and return the shared result. Add tests for concurrent identical downloads.

### [K5] encapsulate generation state
- **Status:** VALID
- **Reason:** `clear_generation_downloads` has a comment dictating it must be called with a lock held in another module (`tts_service`), breaking encapsulation and causing lazy imports.
- **References:** `plugin/audio/tts_models.py:81-85`.
- **Fix Sketch:** Move the generation download failure cache and cancellation event state into a new `plugin/audio/speech_state.py`. Both modules can import it, dropping the lazy imports and lock contract. Add tests asserting state cleanup.

### [K6] _ensure_asset helper
- **Status:** VALID
- **Reason:** `_resolve_piper_model_file` is extremely complex, nesting checks and status clear/notify pairs in a 70-line ladder.
- **References:** `plugin/audio/tts_models.py:264-333`.
- **Fix Sketch:** Extract an `_ensure_asset` (and `_ensure_piper_pair`) helper to wrap the status, download, and failure-remembering sequence. The resolver loop becomes a simple iteration over candidates. Update tests to assert exactly one status message is emitted.

### [K7] total deadline so Stop isn't delayed 30-60 s
- **Status:** VALID
- **Reason:** `urllib.request.urlopen(timeout=...)` is a per-read timeout. A stalled read can block the main loop (and Stop) for 60 seconds. There's also no retry on 5xx or disk-space preflight.
- **References:** `plugin/audio/tts_models.py:163`.
- **Fix Sketch:** Use a short read timeout (e.g., 5s) inside a time-bounded loop (total 20 min). Add HTTP 429/5xx retries with backoff. Add `shutil.disk_usage` preflight check. Add throttled progress callback. Update tests for timeouts and disk-full scenarios.

### [K8] PiperModel NamedTuple/descriptor
- **Status:** VALID
- **Reason:** `PiperModel` is defined as a `tuple[str, str, str, str]`, making code at call sites rely on magic index access (e.g., `PIPER_VOICE_MODELS[voice_id][0]`).
- **References:** `plugin/audio/voice_catalog.py:20`.
- **Fix Sketch:** Convert `PiperModel` to a `NamedTuple`. Create descriptors for all downloadable assets (filenames, URLs, sizes, hashes). Move `_notify_tts_status` to `plugin/audio/tts_status.py`. Update tests to verify catalog consistency.
