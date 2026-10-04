# Concurrent headless LO eval in one process

**Status:** LoLane agent pool **landed**. `run_eval.py` and `run_eval_multi.py` take `--lo-workers` (default **4**, hard cap **5**). One Python process, one headless soffice, one URP bridge, one `_lo_thread`. Up to that many LO example bodies run at once so LLM waits overlap; `LOBackend.call` is still the only UNO critical section. `_lo_docs` stays keyed by caller thread id. The N=2 / N=4 text proof is #933 (`prove_lo_multi_doc.py`).

**get_ctx + current-component pin landed.** `LOBackend.start` calls `set_fallback_ctx(_lo_ctx)` after the pipe context exists, and `stop` restores the previous fallback. Each queued `LOBackend.call` sets `_lo_desktop`'s active frame to `_lo_docs[caller_tid]` when that slot exists, before the callable runs. The N=2 flag sketch is `prove_lo_flag_pin.py` (host RPC shape into A's Writer doc while B `setString`s during A's sleep).

**Parallel URP dispose (eval-1 flag):** LoLane workers must not assemble system prompts via MagicMock → ``get_document_type`` (``@main_thread_only``). Use ``get_chat_system_prompt_for_kind``. With ``WRITERAGENT_EVAL_HARNESS=1``, QueueExecutor disables AsyncCallback so a stray ``execute_on_main_thread`` cannot poke the VCL/bridge path while ``_lo_thread`` holds the shared URP pipe (``Binary URP bridge already disposed`` under ``-j N --lo-workers N``).

**Still open:** the warm venv worker’s `_io_lock` still holds the pipe for a whole script, so two `run_venv_python_script` flag bodies do not overlap their UNO. The sketch calls `host_rpc.execute_tool` directly and does not take that lock. Headed eval-2 / AFC is a different process model. Do not raise the pool past 5 until the UNO queue is shown idle at the cap. A second soffice is still out of scope.

**Tree:** `master` after the LoLane pool (research #932, document isolation #933, pool #934, `python_shapes_flag` as the only `backend=lo` row).

This is headless eval-1 (`--accept=pipe`, private `UserInstallation`, tools running in the client process). It is a different process model from the headed eval-2 / AFC matrix, where the extension runs inside the GUI office. Do not read one as evidence for the other.

## Recommendation

1. **Do this first.** Keep `LOBackend` (one soffice, one URP connection, one `_lo_thread`). Stop putting the whole LO example — including `LlmClient.request_with_tools` — on a single `LoLane` thread. Run a pool of 2–5 agent threads. Each thread is one example. `LOBackend.call` remains the only UNO critical section. **Landed:** `LoLane(workers=...)`, default 4, clamp 1..5, `--lo-workers` on both eval CLIs. `workers=1` is the old FIFO body.
2. **Prove documents stay apart before any flag work.** N=2, then N=4, with plain Writer text (`setString` / `get_content`). The document map in `tools_lo` is already keyed by caller thread id; the lane is what collapses every LO example onto one id today.
3. **Flag host RPC sees this caller’s document.** **Landed.** Direct `host_rpc.execute_tool` (no session id) still calls `get_active_document(get_ctx())`, which is `Desktop.getCurrentComponent()`. `LOBackend.start` points `get_ctx()` at `_lo_ctx`, and each queued `call` activates `_lo_docs[caller_tid]` first. Chat `run_venv_python_script` also pins `ToolContext.doc` for the script, so `wa.shape` inside that script does not follow a later focus change. That is not a second soffice. Two full flag scripts still serialize on the venv `_io_lock` (still open).
4. **Leave multiprocess and a second soffice until that pool misbehaves.** One office already serializes document-model work on the solar mutex. A second process does not make the LLM any more overlapped than N agents on one pipe, and it multiplies profiles, acceptors, and cleanup.
5. **Do not aim at 20-wide for v1.** Two to five concurrent LLM waits is where the wall-clock win is. The non-LLM parts (UNO queue, one warm venv pipe, one office mutex) stay serial either way.

## What serializes LibreOffice today

Two queues, and they serialize different things.

```
model / string threads                eval-lo-lane-N (pool)                _lo_thread
(run_eval_multi -j, or                LoLane workers (default 4, cap 5)    LOBackend._worker_loop
 run_eval string pool)                 one example body per worker          one UNO callable at a time
        |                                     |                                      |
        |  string example: HTTP + mock tools  |  LO example: HTTP + tools            |  prepare / execute_lo_tool
        |  never touches UNO                  |  LLM waits overlap across workers    |  get_eval_export / venv RPC
        +------------------------------------>+  UNO only via LOBackend.call        +--> soffice (one pipe)
```

| Piece | Where | What it holds |
| --- | --- | --- |
| `LoLane` | `scripts/prompt_optimization/eval_scheduler.py` | Every example whose resolved backend is `lo`. A pool of agent threads (default 4, clamp 1..5). The callable is the full `run_eval_on_examples_llm` body (`_one` → `run_llm_chat_eval`), so each worker sits in HTTP while another worker’s `LOBackend.call` can use `_lo_thread`. `workers=1` is single-thread FIFO for those bodies. |
| `LOBackend._lo_thread` | `scripts/prompt_optimization/tools_lo.py` | Only `LOBackend.call` work: `prepare_example`, `execute_lo_tool`, export. `call` runs inline if the caller already is `_lo_thread`; otherwise it queues and waits. Each queued task activates `_lo_docs[caller_tid]` on `_lo_desktop` when that slot exists, before the callable. |
| `set_fallback_ctx(_lo_ctx)` | `LOBackend.start` → `plugin/framework/uno_context.py` | `get_ctx()` is the remote office context, not local pyuno. `stop` restores the previous fallback. Same API as `main_core` / `testing_runner`. |
| `set_designated_main_thread(_lo_thread)` | `LOBackend.start` → `plugin/framework/thread_guard.py` | One designated UNO thread for the process. `on_main_thread()` is that thread, not `threading.main_thread()`. |
| `bypass_thread_guard=True` | `tools_lo` → `ToolRegistry.execute` | Eval tools call `tool.execute` on `_lo_thread` directly. They do not go through `execute_on_main_thread`. That path needs a VCL `AsyncCallback` pump; this harness has none, and blocking on it deadlocks (same family as #402). |
| `WRITERAGENT_EVAL_HARNESS=1` | `LOBackend.start` | Skips menu-icon preload and core menu registration in `plugin/main.py` `bootstrap`. Those call `get_desktop()` in this process and can segfault soffice. |
| `WRITERAGENT_TESTING` must stay unset | `QueueExecutor._should_run_inline` | `WRITERAGENT_TESTING=1` runs marshalled work on the **caller** thread. That is the wrong thread for this harness. |

`run_dual_lane` submits every LO job to the pool **before** string jobs start. String jobs use a pool of width `-j` on `run_eval.py` (default 1). A string job never waits on the LO queue. The unit contract is in `tests/scripts/test_eval_scheduler.py`: with `workers=2`, two LO sleeps overlap; with `workers=1`, they stay serial; the string sleep overlaps LO and does not wait on the LO queue. That test does not start soffice.

### How much overlap exists after #931

The live pack is 19 tasks. `dataset.py` marks **two** rows `backend=lo`: `python_shapes_flag` and `org_chart_gen`. The other 17 omit `backend` and stay `string`.

| Invocation | Who overlaps | Who does not |
| --- | --- | --- |
| `--backend string` | Nothing LO. The flag row is skipped (or an error if you `-e` it). | |
| `--backend auto` | The string pool overlaps LO example bodies. Up to `--lo-workers` of those bodies run at once, so their LLM waits overlap each other and the string pool. | UNO sections stay serial on `_lo_thread`. A sixth LO example waits for a pool slot. |
| `--backend lo` | No string lane. Selected rows share the LO pool (default 4). LLM waits overlap. | UNO stays serial. Past the cap, example bodies queue. `_lo_thread` can run one agent’s tool call while others sit in `request_with_tools`. |
| `run_eval.py -j` | String-pool width only. | Does not change `--lo-workers` (default 4, cap 5). |
| `run_eval_multi.py -j` | Model workers (default comment in the module docstring: 20). They share **one** `LoLane` created in `main` (`--lo-workers`) and passed into every `_run_one_model`. | `_run_one_model` does not pass `string_jobs`, so it stays 1: within a model, string rows run one after another on the model thread, overlapping the shared pool, not each other. LO bodies from every model share those agent threads. |

The pool is “several native LO tasks at once,” capped at 5, still one soffice. It is not 20-wide LO. Chat flag scripts pin `ctx.doc` for `wa.shape`. Direct `execute_tool` still follows the current component, which each `LOBackend.call` sets. The venv `_io_lock` still serializes two full scripts.

During a single LO example the split is already “HTTP outside the UNO queue”:

- `run_llm_chat_eval` calls `prepare_example` once (`LOBackend.call`).
- Each round calls `client.request_with_tools` on the lane thread. That does not enter `LOBackend.call`.
- A tool call goes through `_dispatch_lo_tool` → `execute_lo_tool` → `LOBackend.call`.
- After the loop, `get_eval_export` is another `call`.
- The judge in `eval_core._one` runs after the example returns, still on the lane thread, still not holding `_lo_thread`.

The idle UNO thread during those HTTP waits is the budget a pool of 2–5 agents would use.

`_lo_docs` / `_lo_kinds` are keyed by caller thread id. `LOBackend.call` stores that id on a thread-local for the duration of the queued task, and `acquire_document` / `reset_document` use it. The comment in `tools_lo` says this is so parallel threads do not share a document. Each in-flight pool worker is its own `eval-lo-lane-N` thread, so concurrent examples get distinct slots (close + factory per `prepare_example` on that id). #933 proved N=2 and N=4 on that map with plain Writer text. `workers=1` still uses one id at a time. Direct `host_rpc.execute_tool` still follows `get_active_document(get_ctx())`. A chat script pins `ctx.doc` before that fallback.

## Is pyuno safe on several threads if each thread has its own connection?

**For this harness, keep every UNO call on `_lo_thread` even when several documents are open.**

What the office and this repo actually guarantee:

- LibreOffice document/VCL state is single-threaded inside soffice. `docs/framework/uno-thread-safety.md` states that directly. Incoming URP calls take the solar mutex. Two client threads hammering one bridge do not get parallel Writer mutations; they race the bridge.
- `OfficeThreadAssert` (and `Application::IsMainThread` checks) fire **inside** the soffice process when VCL work runs off the office main thread. An external `--accept` client is not that thread. Headless `loadComponentFromURL` / text / shape APIs usually run on the bridge thread under the solar mutex and succeed. Menu, controller, and message-box paths are the ones that segfault or assert. Eval already refuses those (`WRITERAGENT_EVAL_HARNESS`, no `execute_on_main_thread` pump). Headed AFC does the opposite: Python runs inside the GUI process, and the thread guard’s “main thread” is the real office thread.
- `thread_guard.set_designated_main_thread` stores **one** thread. `QueueExecutor._may_run_marshal_inline` runs inline only on that thread. A second UNO thread in-process is a guard violation in dev builds (`GUARD_ON` defaults on).
- pyuno in one interpreter, one bridge: treat it as one client thread. A second `UnoUrlResolver.resolve` from another Python thread, sharing objects with the first, is not a supported layout here. The local `uno.getComponentContext()` is a different context from the remote office context (`uno_context.get_ctx` docstring, issue #768: Desktop on the local context takes `SolarMutexGuard` and can SEGV when there is no VCL).

Several documents on **one** connection, touched only from `_lo_thread`, matches both the office mutex and the guard. Several bridges in one process does not, until the single-bridge pool has failed a concrete test.

## One soffice, N documents, N agents

Feasible in one process. This is the layout to try.

`_bootstrap_headless` starts one soffice:

- `--headless --nologo --nodefault --norestore --nolockcheck --nofirststartwizard`
- a private `-env:UserInstallation=`
- `--accept=pipe,name=<random>;urp;`

`LOBackend.start` resolves `uno:pipe,name=...;urp;StarOffice.ComponentContext` once, creates one `com.sun.star.frame.Desktop`, and calls `plugin.main.bootstrap(_lo_ctx)`. `Desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, Hidden)` can open a second Writer document on that desktop. Nothing in the acceptor setup is one-document-only.

The agent loop that should run on each pool thread is the one that already exists in `run_llm_chat_eval`:

1. `prepare_example` — one short UNO section, creates **this thread’s** doc.
2. Many `request_with_tools` — overlap across the pool. Each `run_llm_chat_eval` builds its own `LlmClient`. `LlmHttpTransport` is one keep-alive connection per client and is not safe for two threads to share; per-example clients are the right shape (`http_transport` module docstring). Paid OpenRouter traffic is not spaced by the process-wide host gap; that gap applies after a retry, and the 3s floor is only for `:free` / `openrouter/free`.
3. Tool calls — short (or, for the flag, script-long) sections on `_lo_thread`.
4. Export — short UNO section. Judge HTTP can stay on the agent thread afterward and overlap too.

While step 2 runs, `_lo_thread` can run another agent’s step 3. That is the speedup. N=2 already hides one model’s wait behind the other’s tool calls. N=4 or 5 helps when several tasks are in HTTP at once. Past that, the office mutex and the single venv pipe dominate, and a failure is harder to attribute.

### What has to stay true for the documents not to cross

These are the breakages to design the proof around. They are all single-process, single-soffice issues.

**Current component.** `get_active_document` is `desktop.getCurrentComponent()` (`plugin/framework/uno_context.py`). The last `loadComponentFromURL` becomes current, including a `Hidden` factory doc. Plain eval tools do **not** use that. `_execute_lo_tool_impl` passes `LOBackend.acquire_document()` into `ToolContext`, and Writer tools such as `apply_document_content` / `get_document_content` use `ctx.doc`. Two agents whose tools only see `ctx.doc` can share a desktop **if** each `LOBackend.call` is tagged with the caller id, which `call` already does.

**Flag / venv path.** Direct `execute_tool` with no session id still builds `ToolContext` from `get_active_document(get_ctx())`. Chat `RunVenvPythonScript.execute` (`plugin/calc/python/venv.py`) pins `ctx.doc` and passes that id into `run_code_in_user_venv`. The child’s `wa.shape` RPC comes back through `execute_tool`, which resolves the pin before the focused window. A script with no pin (Run Python Script in Isolated mode, or a direct `execute_tool`) still uses the current component.

**`get_ctx()` and the current component are pinned.** `plugin/main.py` `bootstrap` stores the remote context on the service registry and on `QueueExecutor.set_context`. It still does not call `set_fallback_ctx` (that call lives in `main_core.py` and `testing_runner.py`). `LOBackend.start` now does, with `_lo_ctx`, and `stop` puts the previous fallback back. Without that, `get_ctx()` returns `uno.getComponentContext()`, the local pyuno context, and shape RPC misses the headless desktop. Each queued `LOBackend.call` then calls `_lo_desktop.setActiveFrame` on `_lo_docs[caller_tid]` when that document exists, after the caller id is stashed and before the callable. The queue is single-consumer, so the component stays stable until the task returns, including a nested `host_rpc.execute_tool`. The N=2 flag sketch checks the exported `.odt`, not only the live model.

**One warm venv worker (still open).** `PythonWorkerManager.get` is one child per `(pool, interpreter)`, and `execute` holds `_io_lock` for the whole script (`plugin/scripting/venv_worker.py`). Host RPC on that same stack calls `execute_on_main_thread`. On `_lo_thread` that inlines (`_may_run_marshal_inline`), so a script’s shape calls run on the UNO thread before the script returns. Two flag scripts therefore run one after another, and each holds `_lo_thread` for the whole script, not just for one shape call. Their **LLM rounds still overlap**. The current-component pin does not remove this lock. `prove_lo_flag_pin.py` calls `host_rpc.execute_tool` itself so the sketch does not take `_io_lock`. Do not move the venv read onto a second thread while `_lo_thread` is inside another script: the child’s tool RPC would block on the UNO queue while the UNO thread blocks on the pipe (`_io_lock` plus a single stdout). `run_venv_python_script` is in `_BLOCKED_FROM_VENV` for the same re-entry reason.

`RunVenvPythonScript.is_async` is true, but `ToolBase` has no `timeout`, and `_get_tool_timeout` defaults to 0, so `_execute_with_timeout` stays inline. A future timeout would `run_in_background` the script off `_lo_thread` and then the host RPC would try `AsyncCallback` from a tagged worker. In this harness that callback is the headless miss path (`QueueExecutor` warns and either refuses or runs UNO on the caller). Leave the script on `_lo_thread`.

**Shared registry and config.** `get_tools()` / `bootstrap` are process singletons. That is fine while `execute` stays on `_lo_thread` and agents do not register tools. `writeragent.json` is the private eval profile (`init_config` during bootstrap). Reads and writes take `_config_write_lock`. N agents should not each rewrite it.

**Dispose.** `reset_document` closes only `_lo_docs[this caller]`. A second caller’s doc stays open. Anything that closes “the current document” or walks `desktop.getComponents()` and disposes them will cross agents. `prepare_example` must keep using the caller id, not a single global doc.

**Thread guard and testing mode.** One designated thread stays correct for N documents. Do not set `WRITERAGENT_TESTING=1`. Do not clear the designated thread while agents are alive. `bypass_thread_guard=True` stays required on the eval `execute` calls.

**Extension singleton inside soffice.** This client does not load the OXT into the headless office. Tool Python runs in the eval process and talks UNO over the pipe. There is no second in-office WriterAgent singleton to race. The singletons that matter are the ones in the client: `get_tools`, `get_ctx`, `PythonWorkerManager`, `set_designated_main_thread`.

## Several connections, or several soffice processes, from one Python process

The connector can do it, and v1 should not.

- `--accept=pipe,name=...;urp;` (what `_bootstrap_headless` passes) accepts connections until the process exits. `--singleaccept` is a different mode (`desktop_create_is_unsafe` in `uno_context.py` looks for it on `uno.bin` / unopkg helpers). One Python process may resolve that URL more than once, or start a second soffice on a second pipe and a second `UserInstallation`.
- Each extra bridge still shares one office solar mutex if it is the same soffice. Parallel UNO calls do not become parallel Writer edits.
- Each extra soffice is a second profile, a second Desktop, and a second place the client singletons are wrong: one `get_tools()`, one designated thread, one venv worker, one `_lo_ctx`.
- Multiprocess workers (one soffice each) avoid the shared pyuno bridge. They also duplicate bootstrap and the venv, and they are a larger change than a 2–5 thread pool in the process `run_eval` already uses. Worth it only if the N=4 single-soffice proof shows cross-talk or an office fault that a current-component pin does not fix.

`tools_lo` already says not to `ProcessPool` one soffice. That warning is about forking workers onto the **same** pipe. It is not a claim that one pipe can serve only one document.

## Why not 20 at once

`run_eval_multi.py -j 20` is model parallelism for the **string** board. Native LO rows share the agent pool (default 4, cap 5), not 20 soffice clients. Turning that 20 into 20 simultaneous LO agents on one soffice spends complexity on the part that is not the wait:

- UNO edits stay serial on `_lo_thread` and on the office mutex. Twenty agents in HTTP is fine; twenty agents in `apply_document_content` or in a flag script is a queue.
- The flag script holds the only venv pipe for the whole script. Twenty flag tasks would mostly wait on that pipe, not on the model.
- Twenty hidden Writer documents in one headless soffice is a memory and dispose-order problem the 2–5 proof does not need.
- A cross-write is obvious with two documents and two known strings. It is not obvious with twenty.

Run the 20-model string board the way #931 already does. For native LO rows, a pool of 2, then 4, then at most 5, is the v1 knob. If those five are busy and the UNO queue is still mostly idle, raise the cap. If the UNO queue or the venv pipe is saturated at 4, more agents will not help on one soffice.

## Experiment Keith can run next

No full matrix. The text proof is `scripts/prompt_optimization/prove_lo_multi_doc.py`: one `LOBackend`, N agent threads (default 2), no OpenRouter, no second soffice. It does not go through `LoLane`. N=2 and N=4 passed on master (#933) before the pool landed; the harness pool is the follow-up that uses that isolation. The flag sketch is `scripts/prompt_optimization/prove_lo_flag_pin.py`. Do not set `WRITERAGENT_TESTING=1`. A green run is headless only; it is not evidence about headed AFC.

```bash
make manifest   # once; plugin/_manifest.py is gitignored
.venv/bin/python scripts/prompt_optimization/prove_lo_multi_doc.py
.venv/bin/python scripts/prompt_optimization/prove_lo_multi_doc.py --n 4
.venv/bin/python scripts/prompt_optimization/prove_lo_flag_pin.py
```

The process exits non-zero unless the sleeps overlap, each Writer document contains only that worker’s tokens, UNO enter/exit intervals on `_lo_thread` do not overlap, and `len(_lo_docs)` is N after the first write. Enter/exit is logged inside the closure `LOBackend.call` runs, not around the agent’s queue wait.

### N=2 text, no API

Start `LOBackend` once. Spawn two threads. Give each a unique token.

On each thread, three steps, with a timestamp log around the sleep:

1. `LOBackend.call` a closure that `acquire_document("writer")` and `setString("alpha-1")` (the other thread uses `beta-1`).
2. `time.sleep(0.5)` on the **agent** thread. This stands in for `request_with_tools`. It must not run inside `LOBackend.call`.
3. `LOBackend.call` again: append `alpha-2` / `beta-2`, then `getString()`.

Pass criteria:

- The two sleeps overlap (each start is before the other’s end). Same shape as `test_lo_pool_workers_two_overlap`, but both jobs are real `LOBackend.call` callers.
- Doc A’s string contains only `alpha-*`. Doc B’s string contains only `beta-*`.
- A log line at the start and end of each queued closure shows the UNO sections do not overlap, and `_lo_thread` is the thread that runs them.
- `len(_lo_docs)` is 2 between step 1 and process teardown (both caller ids live).

If the strings cross, stop. The bug is current-component or a shared doc slot, and N=4 will not clarify it.

### N=4 text, no API

Same script, four tokens, four threads (`--n 4`). Pass criteria are the same, with all four sleeps overlapping and four distinct documents. This is the “small pool” check. If N=2 passes and N=4 fails, the failure is office load or dispose, not the locking model.

### N=2 with a real short model call

Only after the text proof. Two threads, each calling `run_llm_chat_eval(..., backend="lo")` on a tiny Writer ask (one `apply_document_content`, not the flag) **without** wrapping those calls in `LoLane`. Watch the banners: `Calling model` for thread B should print before thread A’s export. Confirm the two exports differ. One `LlmClient` per `run_llm_chat_eval` is already how the function is written; do not share a client across the two threads.

### N=2 flag sketch, still one soffice

`prove_lo_flag_pin.py` (command above). `make manifest` once. Do not set `WRITERAGENT_TESTING=1`. No OpenRouter.

Thread A opens a Writer doc, sleeps on the agent thread, then `LOBackend.call`s `host_rpc.execute_tool("shape_upsert", ...)` to insert one named rectangle (`wa-flag-pin-shape`). That is the venv `wa.shape` lookup (`get_active_document(get_ctx())`) without holding `_io_lock`. Thread B opens a second Writer doc and, during A's sleep, `setString`s its own text. After A's shape task returns, B re-reads on B's thread and both docs are exported with `export_writer_odt`.

Pass:

- B's text is only B's token. A's text is only A's token.
- A's `.odt` `content.xml` has `draw:name="wa-flag-pin-shape"`. B's `.odt` does not.
- B's `setString` starts while A is asleep, and the UNO sections on `_lo_thread` do not overlap.

The warm-venv `_io_lock` is unchanged: two real `run_venv_python_script` bodies still run one after another. Do not compare this to a headed AFC run of the same Ask.

### What not to do in the first proof

- Do not start a second soffice, a second pipe, or a process pool.
- Do not put 20 threads on one desktop.
- Do not set `WRITERAGENT_TESTING=1` or route tools through `execute_on_main_thread`.
- Do not treat a green string-lane overlap test as evidence that two LO documents stayed apart. That test never opens a document.
