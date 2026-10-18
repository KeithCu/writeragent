# Venv worker incident history

Incident write-ups removed from comments in [`plugin/scripting/venv_worker.py`](../../plugin/scripting/venv_worker.py). Each code site keeps a one-line rule and a pointer here. Moving the comments did not change behavior.

Live rules for the same machinery are in [threading.md](../framework/threading.md) (the venv stdin bullet, and "Subprocess retry & replay semantics"). This page is history. The fix is already in the source.

## Llm request caller gate

`_maybe_dispatch_intermediate_response`. Only the ppt-master worker may emit `llm_request`. Every other caller (including `"script"` and `=PY()`) used to fall through into that dispatcher, which runs `llm_request` with the host's API credentials. `tool_call` stays open to every caller.

## Reap off caller thread

`_reap_worker_process`. `_terminate_worker` used to wait on this thread for the kill, a second `wait`, and the stderr join (about 12s when the tree ignores the first signal). `PythonWorkerManager.get` did that on the caller, which is the UI thread when Settings changes the venv path. The caller has already dropped `_proc` under `_proc_lock`. This thread only reaps that snapshot. The next spawn joins it first, so the new child does not overlap the old process tree.

## Settings path retires old child

`PythonWorkerManager.get`. The registry key is `pool:exe`. A new Settings path used to leave the previous child running until LibreOffice exited.

## Io lock no unbounded acquire

`_reentry_error`. Any other thread used to call `Lock.acquire()` with no timeout. The holder can be inside a tool RPC that is waiting on the thread stuck in acquire (the UI pump, or whichever worker must run the host callback), so the wait never ends and the UI stays frozen. Refuse the owner, anyone while a tool call is in progress, and the UI thread while the pipe lock is held.

## Allow heartbeat on actions

`_ExecRequest.wire`. `allow_heartbeat` belongs on every long job, not only the code branch. `run_trusted_action` (embeddings, folder index) never received it, so the child sent no heartbeat and the host killed a long job.

## Dispatched intermediate before handler

`_read_until_terminal`, immediately before `response.get("type") in ("tool_call", "llm_request", "worker_event")`. A `tool_call` (or ppt `llm_request` / `worker_event`) can mutate the document and then raise. The flag used to flip only after dispatch returned, so a `RuntimeError` with no `exec_started` left `may_have_run` false and the attempt loop sent the request again.

## Resolve before write

`_execute_ipc_unlocked`, immediately before `resolve_allowed_tools`. `resolve_allowed_tools` and `host_script_session_id` ran after the request frame was written and before `_read_until_terminal`. Tool RPC was added in the attempt loop after the write (`24a737750`). `OSError` and `RuntimeError` from that import are in the retry tuple, and `may_have_run` is still false because the read has not started, so the same request id was sent to a new child. Both values are host-only. A failure here is a pre-write retry. The child does not have the request yet.

## Oversize before write

`_execute_ipc_unlocked`, `except IpcFrameError`. An oversize request fails in `pack_pickle_frame` before any byte reaches the pipe. That must not hit the retry handler, which killed the warm worker twice and wiped every shared session.

## Partial frame timeout no replay

`_execute_ipc_unlocked`, `except (subprocess.TimeoutExpired, IpcPartialFrameTimeout)`. User code or a C extension that hangs must not be killed and replayed, because that doubles the wait. Windows raises `IpcPartialFrameTimeout` (an `OSError`) once any byte of a frame is buffered. That missed this branch, and with `exec_started` only partly read `may_have_run` was still false, so the outer handler resent the script. The child writes `exec_started` and then runs user code immediately, so file and DuckDB side effects ran twice. Both platforms now raise `IpcPartialFrameTimeout` once any byte of the current chunk has been taken; an empty chunk raises `TimeoutExpired`. Both are caught here, so neither replays.

## Runtimeerror after exec started

`_execute_ipc_unlocked`, `except (OSError, RuntimeError)` inside the read `try`. `RuntimeError` must not look only at `dispatched_intermediate`. A later `RuntimeError` (bad frame, closed pipe) after `exec_started` re-raised into the attempt loop and ran the same script on a new child. `_NonReplayableIpcWriteTimeout` is a `RuntimeError`, so a mid-turn host-response write timeout is caught here first. Re-raising when the turn has not started lets the dedicated handler below refuse the replay; after `exec_started` this branch already does.

## Empty id harness reject

`_execute_ipc_unlocked`, the `response.get("id") == ""` check. The harness writes `{"id": "", "status": "error", "message": "Invalid pickle request: ..."}` and loops. The host treated that as an id mismatch and `_fail_no_replay` killed a child that was already back at `read()`. `read_pickle_frame` raises `ValueError` for a non-dict or a bad unpickle after the length prefix and payload are consumed (`worker_harness.main`). The pipe stays aligned. The host only compared ids. That empty id is only used on this pre-exec reject. Return the child's message and leave the process up. Any other id, or this frame after `exec_started` or a tool frame, still kills.

## Terminal id mismatch

`_execute_ipc_unlocked`, `response.get("id") != req_id`. `exchange_tool_call` already checks ids. A mismatched terminal frame used to be accepted, so one cell could receive another's result.

## Unpack errors no replay

`_execute_ipc_unlocked`, `except _HOST_UNPACK_ERRORS`. `host_unpack_data` runs after the script has finished. A bad envelope used to hit the outer handler and replay the request, so a tool call that already mutated the document ran twice. The catch is the unpack contract, not only `ValueError`: `int(inf)` on an int column is `OverflowError`, and a bad shape can be `TypeError`, `AttributeError`, or `KeyError`. Those miss the outer replay tuple and used to escape as an uncaught exception.

## Stdin write timeout handler

`_execute_ipc_unlocked`, the outer `except subprocess.TimeoutExpired`. This handler is the initial stdin write only. `_write_packed_frame_with_timeout` already detached the child and started the reap. Terminating again made it unclear who kills the worker, and a second wait would block on that reap. Host read timeouts return above.

## Retired recheck live proc

`_ensure_running`, the live-proc return after `_join_reaps`. `_retired` was checked only on entry. `get()` and `shutdown_all` set the flag, then kill. `_join_reaps()` can block, so the flag can flip during that wait and this return handed the still-live child to the writer. The `_retired` re-check under `_proc_lock` runs only before `Popen`, to stop an untracked spawn. The live-proc return skipped it. Refuse the return with the same `_WorkerRetired` the spawn path raises. Do not take a lock here. `get()` must not take `_io_lock`, and holding `_proc_lock` across the later write would deadlock a retire that already needs `_proc_lock` inside `_terminate_worker`.

## Popen on spawn thread

`_ensure_running`, the `self._start_child(managed)` call. `Popen` ran on the execute / warm caller. Linux `PR_SET_PDEATHSIG` kills the child when that thread exits, so a warm worker died while LibreOffice was still running and shared sessions were dropped. `_die_with_parent` arms `SIGKILL` for the creating thread. `warm_venv_worker`, corpus index, and chat tool workers are `dedicated=True` threads that return after the call. `ManagedSubprocess.start` runs on this manager's `venv-worker-spawn` thread, which stays alive until the manager is retired or `shutdown_all`.

## Heartbeat absolute ceiling

`_read_response_with_heartbeats`, above the `cap` / `ceiling` assignment. Each heartbeat used to set the deadline to now+grace with no end. Trusted actions do not use the sandbox alarm, so a loop that kept emitting held `_io_lock` until something else killed the child. Heartbeats may move the short deadline forward, but never past an absolute ceiling from the start of this call. They do not reset it. `_read_until_terminal` starts a new ceiling after a non-heartbeat frame. Trusted actions emit heartbeats until one result, so for them this call is the whole job.

## Heartbeat first frame deadline

`_read_response_with_heartbeats`, the `window = float(timeout_sec)` block. The first-frame deadline was `max(timeout_sec, grace_sec)`, so a grace longer than the caller timeout became the wait, and `TimeoutExpired` reported grace. The heartbeat reader in `13301df45` used that max so a short timeout could not fire before grace. Production grace is 90s and the long trusted timeout is 300s, so the max was a no-op there. Grace is silence allowed after a heartbeat. The first byte is bounded by `timeout_sec`, then each heartbeat sets `deadline = min(now + grace, ceiling)`.

## Unknown frame logging

`_read_response_with_heartbeats`, the unknown-`frame_type` branch. Unknown frames must not extend the deadline. A protocol drift then fails as a timeout instead of looking like a silent stall. Each ignored frame logged a warning before the next deadline check, so logging latency counted as wait. In run `38800801506` gw2, with `timeout_sec=0.2` and `grace_sec=2.0`, stubbed reads returned an unknown frame. The captured log has 185 warnings and wall time `1.031606279s`, so `elapsed < 1.0` failed. Grace was not the deadline. Log the first unknown type once. Further unknown frames only recheck the deadline.
