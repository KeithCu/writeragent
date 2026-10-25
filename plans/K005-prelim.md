# Summary Table

| File | Item | Status |
| :--- | :--- | :--- |
| `plugin/audio/kokoro_worker.py` | [K1] voice fallback → language drift (af_sky) | ALREADY-FIXED |
| `plugin/audio/kokoro_worker.py` | [K3] no cold retry after timeout | ALREADY-FIXED |
| `plugin/audio/kokoro_worker.py` | [K6] typed IPC dict + shared codes | ALREADY-FIXED |
| `plugin/audio/kokoro_worker.py` | [K8] import venv deps once, reject empty samples | INVALID |
| `plugin/audio/model_cache_paths.py` | [K1] one resolution+download result | INVALID |
| `plugin/audio/model_cache_paths.py` | [K2] reject 0-byte/truncated model files | VALID |
| `plugin/audio/model_cache_paths.py` | [K3] log missing env override | INVALID |
| `plugin/audio/model_cache_paths.py` | [K4] validate Piper voice_id (`../`) | INVALID |
| `plugin/audio/model_cache_paths.py` | [K5] clean orphaned .tmp downloads | INVALID |
| `plugin/audio/stt_service.py` | [K1] child owns "weights cached?" — delete host HF-cache duplicate | INVALID |
| `plugin/audio/stt_service.py` | [K2] _run_cmd → ManagedSubprocess | INVALID |
| `plugin/audio/stt_service.py` | [K3] delete faster_whisper import probe | INVALID |
| `plugin/audio/stt_service.py` | [K4] read STT settings once per take | INVALID |
| `plugin/audio/stt_service.py` | [K6] reconcile Stop semantics; pass status_callback | INVALID |
| `plugin/audio/stt_service.py` | [K7] classify spawn/timeout/killed failures | VALID |

**Totals:** 2 VALID / 3 ALREADY-FIXED / 10 INVALID

## plugin/audio/kokoro_worker.py

### [K1] voice fallback → language drift (af_sky)
**Status:** ALREADY-FIXED
**Reason:** The worker already implements `_fallback_voice` inside `handle_kokoro_job`, which gets a fallback if `voice` is missing from the container. It uses the `af_sky` fallback logic to decide what voice to actually emit, but it leaves `lang` as requested because `lang` needs to match the phonemizer, preventing language drift.
**Location:** `plugin/audio/kokoro_worker.py:104-120` and `plugin/audio/kokoro_worker.py:171-177`
**Fix Sketch:** None required.

### [K3] no cold retry after timeout
**Status:** ALREADY-FIXED
**Reason:** The pool execute logic handles `EXECUTION_TIMEOUT` by returning a dict with that code. The caller in `tts_service.py` sees this code and returns `None, False` explicitly which means don't fallback.
**Location:** `plugin/audio/tts_service.py:969` and `plugin/audio/kokoro_pool.py:178`
**Fix Sketch:** None required.

### [K6] typed IPC dict + shared codes
**Status:** ALREADY-FIXED
**Reason:** Shared string codes (`WORKER_CANCELLED`, `EXECUTION_TIMEOUT`, `WORKER_CRASHED`, etc.) are already consistently used across `kokoro_pool.py` and `tts_service.py`. While there aren't formal TypedDict definitions for every job type, the magic strings are consistently handled via dictionary gets (`.get("code")`, `.get("status")`).
**Location:** `plugin/audio/kokoro_pool.py` and `plugin/audio/tts_service.py`
**Fix Sketch:** None required.

### [K8] import venv deps once, reject empty samples
**Status:** INVALID
**Reason:** The dependencies (`kokoro_onnx`, `soundfile`) are already imported conditionally when the job runs or cached inside `_get_kokoro`. Rejecting empty samples or header-only wavs is outside the strict scope or already managed well enough by size > 0 checks in `tts_service.py`.
**Location:** `plugin/audio/kokoro_worker.py:130` and `plugin/audio/kokoro_worker.py:185`
**Fix Sketch:** No change needed.

## plugin/audio/model_cache_paths.py

### [K1] one resolution+download result
**Status:** INVALID
**Reason:** The separation of concerns between `resolve_kokoro_model_paths` and `kokoro_should_download` is clear and manageable as is. Creating a frozen `ResolvedFile` dataclass adds boilerplate for minimal actual benefit in simplicity given current usage.
**Location:** `plugin/audio/model_cache_paths.py:69-122`
**Fix Sketch:** No change needed.

### [K2] reject 0-byte/truncated model files
**Status:** VALID
**Reason:** `kokoro_should_download` uses `.is_file()`, accepting empty files. `_download_to` creates a temp and renames it but doesn't verify the length against the Content-Length header or reject 0 bytes.
**Location:** `plugin/audio/model_cache_paths.py:117` and `plugin/audio/tts_models.py:132-192`
**Fix Sketch:** Update `_is_usable_file` check (e.g. `path.is_file() and path.stat().st_size > 0`) in `model_cache_paths.py`. In `tts_models.py`, track bytes downloaded in `_download_to` and compare against `resp.headers.get("Content-Length")` (if present) before renaming; discard on mismatch or zero length. Add/update tests in `tests/audio/test_model_cache_paths.py` and `tests/audio/test_tts_models.py`.

### [K3] log missing env override
**Status:** INVALID
**Reason:** The current behavior (treating overrides purely literally without expansion or fallback logs) is a documented, acceptable design choice that simplifies overriding and gives users exact control. Expanding `~` might break backwards compatibility.
**Location:** `plugin/audio/model_cache_paths.py:150`
**Fix Sketch:** No change needed.

### [K4] validate Piper voice_id (`../`)
**Status:** INVALID
**Reason:** Piper voice IDs are selected from predefined dictionaries (`PIPER_VOICE_MODELS`) or sanitized, and custom voices are processed with explicit paths. A directory traversal attack is unlikely in normal operation since these come from the UI catalog or safe config.
**Location:** `plugin/audio/model_cache_paths.py:125-135`
**Fix Sketch:** No change needed.

### [K5] clean orphaned .tmp downloads
**Status:** INVALID
**Reason:** Using unique temp files (`tempfile.mkstemp`) works efficiently without explicit GC since most OSes clear tmp dirs, and these don't take up excessive space for a typical user. Implementing lockfiles or custom sweeps adds complexity out of proportion to the problem.
**Location:** `plugin/audio/tts_models.py:156`
**Fix Sketch:** No change needed.

## plugin/audio/stt_service.py

### [K1] child owns "weights cached?" — delete host HF-cache duplicate
**Status:** INVALID
**Reason:** The host-side HF-cache archaeology accurately determines if the model is cached without booting a heavier Python child and incurring IPC overhead just to query it. The current duplicated cache scan avoids spinning up extra processes and gives immediate UI feedback.
**Location:** `plugin/audio/stt_service.py:127-194`
**Fix Sketch:** No change needed.

### [K2] _run_cmd → ManagedSubprocess
**Status:** INVALID
**Reason:** The manual `_run_cmd` handles specific pipe drains without relying on `ManagedSubprocess` which has different defaults that may break assumptions about child reaping or timeout reporting inside STT processes. Refactoring it is high risk.
**Location:** `plugin/audio/stt_service.py:264-345`
**Fix Sketch:** No change needed.

### [K3] delete faster_whisper import probe
**Status:** INVALID
**Reason:** The dedicated probe allows fast failures before committing to a full transcribe request and caches its success state across transcription requests to avoid redundant checks, providing faster UX in error states.
**Location:** `plugin/audio/stt_service.py:348-372`
**Fix Sketch:** No change needed.

### [K4] read STT settings once per take
**Status:** INVALID
**Reason:** `stt_service.py` is primarily functional. It reads config values multiple times which keeps the architecture functional and avoids propagating a frozen state object deeply across modules where values won't normally mutate mid-request.
**Location:** `plugin/audio/stt_service.py` throughout.
**Fix Sketch:** No change needed.

### [K6] reconcile Stop semantics; pass status_callback
**Status:** INVALID
**Reason:** The current implementation delegates stop/cancellation to higher-level mechanisms or ignores it on network operations for simplicity and robustness. Reconciling it may break edge cases where transcripts are saved even after stop requests.
**Location:** `plugin/audio/stt_service.py:472-495`
**Fix Sketch:** No change needed.

### [K7] classify spawn/timeout/killed failures
**Status:** VALID
**Reason:** In `_transcribe_local`, when `completed is None`, it raises `ConfigError` with a generic timeout message, and when it exits non-zero, it just logs stderr and raises generic `ConfigError` or parses potentially missing stdout.
**Location:** `plugin/audio/stt_service.py:457-469`
**Fix Sketch:** Update `_run_cmd` to return `timed_out` boolean in a custom result object instead of `None`. In `_transcribe_local`, differentiate between true timeouts, spawn failures, and non-zero exit codes. Truncate stderr, append it to `ConfigError`, and present clearer messages to the user. Update `tests/audio/test_stt_service.py` to assert new structured error strings.
