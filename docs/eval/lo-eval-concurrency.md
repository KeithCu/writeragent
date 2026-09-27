# Concurrent headless LO eval in one process

**Status:** research note for Keith. No harness change in this PR.
**Tree:** `master` after #931 (`eval_scheduler.py` dual-lane, `python_shapes_flag` as the only `backend=lo` row).

**Target to try first:** one Python process, one headless soffice, **N = 2, then 4** (cap around 5) live agent loops. Each agent owns one Writer document. Their LLM waits overlap. UNO stays on the existing `_lo_thread`. A pool of 20 models or 20 tasks at once is a later question, not the first experiment.

This is headless eval-1 (`--accept=pipe`, private `UserInstallation`, tools running in the client process). It is a different process model from the headed eval-2 / AFC matrix, where the extension runs inside the GUI office. Do not read one as evidence for the other.

## Recommendation

1. **Do this first.** Keep `LOBackend` (one soffice, one URP connection, one `_lo_thread`). Stop putting the whole LO example — including `LlmClient.request_with_tools` — on `LoLane`. Run a pool of 2–5 agent threads. Each thread is one example. `LOBackend.call` remains the only UNO critical section.
2. **Prove documents stay apart before any flag work.** N=2, then N=4, with plain Writer text (`setString` / `get_content`). The document map in `tools_lo` is already keyed by caller thread id; the lane is what collapses every LO example onto one id today.
3. **Then one flag-shaped script beside a second document.** `run_venv_python_script` and `host_rpc.execute_tool` do not use the eval `ToolContext` document. They call `get_active_document(get_ctx())`, which is `Desktop.getCurrentComponent()`. That is the thing most likely to write shapes into the wrong doc. Fixing that is a small pin (activate this caller’s doc at the start of each `LOBackend.call`, and point `get_ctx()` at `_lo_ctx`). It is not a second soffice.
4. **Leave multiprocess and a second soffice until that pool misbehaves.** One office already serializes document-model work on the solar mutex. A second process does not make the LLM any more overlapped than N agents on one pipe, and it multiplies profiles, acceptors, and cleanup.
5. **Do not aim at 20-wide for v1.** Two to five concurrent LLM waits is where the wall-clock win is. The non-LLM parts (UNO queue, one warm venv pipe, one office mutex) stay serial either way.

## What serializes LibreOffice today

Two queues, and they serialize different things.

```
model / string threads                eval-lo-lane                         _lo_thread
(run_eval_multi -j, or                LoLane._loop                         LOBackend._worker_loop
 run_eval string pool)                 one whole example at a time          one UNO callable at a time
        |                                     |                                      |
        |  string example: HTTP + mock tools  |  LO example: HTTP + tools            |  prepare / execute_lo_tool
        |  never touches UNO                  |  blocks here for the whole task      |  get_eval_export / venv RPC
        +------------------------------------>+  including every LLM round           +--> soffice (one pipe)
```

| Piece | Where | What it holds |
| --- | --- | --- |
| `LoLane` | `scripts/prompt_optimization/eval_scheduler.py` | Every example whose resolved backend is `lo`. FIFO. The callable is the full `run_eval_on_examples_llm` body (`_one` → `run_llm_chat_eval`), so the lane thread sits in HTTP. |
| `LOBackend._lo_thread` | `scripts/prompt_optimization/tools_lo.py` | Only `LOBackend.call` work: `prepare_example`, `execute_lo_tool`, export. `call` runs inline if the caller already is `_lo_thread`; otherwise it queues and waits. |
| `set_designated_main_thread(_lo_thread)` | `LOBackend.start` → `plugin/framework/thread_guard.py` | One designated UNO thread for the process. `on_main_thread()` is that thread, not `threading.main_thread()`. |
| `bypass_thread_guard=True` | `tools_lo` → `ToolRegistry.execute` | Eval tools call `tool.execute` on `_lo_thread` directly. They do not go through `execute_on_main_thread`. That path needs a VCL `AsyncCallback` pump; this harness has none, and blocking on it deadlocks (same family as #402). |
| `WRITERAGENT_EVAL_HARNESS=1` | `LOBackend.start` | Skips menu-icon preload and core menu registration in `plugin/main.py` `bootstrap`. Those call `get_desktop()` in this process and can segfault soffice. |
| `WRITERAGENT_TESTING` must stay unset | `QueueExecutor._should_run_inline` | `WRITERAGENT_TESTING=1` runs marshalled work on the **caller** thread. That is the wrong thread for this harness. |

`run_dual_lane` submits every LO job to the lane **before** string jobs start. String jobs use a pool of width `-j` on `run_eval.py` (default 1). A string job never waits on the LO queue. The unit contract is in `tests/scripts/test_eval_scheduler.py` (`test_lanes_overlap_and_lo_stays_fifo`): two LO sleeps stay FIFO, the string sleep overlaps the first, wall clock stays under the serial sum. That test does not start soffice.

### How much overlap exists after #931

The live pack is 18 tasks. `dataset.py` marks **one** row `backend=lo`: `python_shapes_flag`. The other 17 omit `backend` and stay `string`.

| Invocation | Who overlaps | Who does not |
| --- | --- | --- |
| `--backend string` | Nothing LO. The flag row is skipped (or an error if you `-e` it). | |
| `--backend auto` | The string pool overlaps **that one** LO example’s full wall time (LLM rounds, tools, judge). Scheduler docstring: wall clock approaches `max(string_parallel, lo_serial)`. | A second LO example waits until the first example **function** returns. Its LLM wait does not overlap the first example’s LLM wait. |
| `--backend lo` | No string lane. Every selected row is queued on `LoLane`. | LLM waits are serial. `_lo_thread` is idle the whole time the lane thread is inside `request_with_tools`. |
| `run_eval.py -j` | String-pool width only. | Does not add LO workers. |
| `run_eval_multi.py -j` | Model workers (default comment in the module docstring: 20). They share **one** `LoLane` created in `main` and passed into every `_run_one_model`. | Each model’s LO examples are FIFO on that lane. `_run_one_model` does not pass `string_jobs`, so it stays 1: within a model, string rows run one after another on the model thread, overlapping the shared lane, not each other. |

So dual-lane answers “string board plus one headless flag.” It does not answer “several native LO tasks at once.” Forcing `--backend lo` on a pack is the slow case: the lane holds the HTTP wait, and the UNO thread has nothing to do.

During a single LO example the split is already “HTTP outside the UNO queue”:

- `run_llm_chat_eval` calls `prepare_example` once (`LOBackend.call`).
- Each round calls `client.request_with_tools` on the lane thread. That does not enter `LOBackend.call`.
- A tool call goes through `_dispatch_lo_tool` → `execute_lo_tool` → `LOBackend.call`.
- After the loop, `get_eval_export` is another `call`.
- The judge in `eval_core._one` runs after the example returns, still on the lane thread, still not holding `_lo_thread`.

The idle UNO thread during those HTTP waits is the budget a pool of 2–5 agents would use.

`_lo_docs` / `_lo_kinds` are keyed by caller thread id. `LOBackend.call` stores that id on a thread-local for the duration of the queued task, and `acquire_document` / `reset_document` use it. The comment in `tools_lo` says this is so parallel model threads do not share a document. With today’s `LoLane`, every LO example body runs on `eval-lo-lane`, so they all share **one** id and one document slot (close + factory per `prepare_example`). The map is ready for N callers. The lane never gives it N callers.

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

**Flag / venv path does use the current component.** `plugin/scripting/host_rpc.py` `execute_tool` builds a new `ToolContext` from `get_active_document(get_ctx())`, then `get_tools().execute(...)`. It ignores the document `RunVenvPythonScript` was given. `RunVenvPythonScript.execute` (`plugin/calc/python/venv.py`) does pass `ctx.ctx` into `run_code_in_user_venv`, but the child’s `wa.shape` RPC comes back through `execute_tool`, not through that `ToolContext`.

**`get_ctx()` is not `_lo_ctx` in this harness.** `plugin/main.py` `bootstrap` stores the remote context on the service registry and on `QueueExecutor.set_context`. It does not call `set_fallback_ctx`. That call exists in `main_core.py` and `testing_runner.py` only. `get_ctx()` then returns `uno.getComponentContext()`, the local pyuno context, unless a fallback was stored. Shape RPC that trusts `get_ctx()` is aimed at the wrong context even for **one** document. The N=2 text proof can pass while the flag still writes nowhere or into a crash. The N=2 flag proof has to check the exported `.odt` of each doc. The smallest pin, when someone implements this, is: `set_fallback_ctx(_lo_ctx)` at `LOBackend.start`, and at the start of each queued task set the desktop’s current component to `_lo_docs[caller_tid]` before any tool or host RPC runs. Because the queue is single-consumer, that component stays stable until the task returns.

**One warm venv worker.** `PythonWorkerManager.get` is one child per `(pool, interpreter)`, and `execute` holds `_io_lock` for the whole script (`plugin/scripting/venv_worker.py`). Host RPC on that same stack calls `execute_on_main_thread`. On `_lo_thread` that inlines (`_may_run_marshal_inline`), so a script’s shape calls run on the UNO thread before the script returns. Two flag scripts therefore run one after another, and each holds `_lo_thread` for the whole script, not just for one shape call. Their **LLM rounds still overlap**. Do not move the venv read onto a second thread while `_lo_thread` is inside another script: the child’s tool RPC would block on the UNO queue while the UNO thread blocks on the pipe (`_io_lock` plus a single stdout). `run_venv_python_script` is in `_BLOCKED_FROM_VENV` for the same re-entry reason.

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

`run_eval_multi.py -j 20` is model parallelism for the **string** board, with LO tasks squeezed through one lane. Turning that 20 into 20 simultaneous LO agents on one soffice spends complexity on the part that is not the wait:

- UNO edits stay serial on `_lo_thread` and on the office mutex. Twenty agents in HTTP is fine; twenty agents in `apply_document_content` or in a flag script is a queue.
- The flag script holds the only venv pipe for the whole script. Twenty flag tasks would mostly wait on that pipe, not on the model.
- Twenty hidden Writer documents in one headless soffice is a memory and dispose-order problem the 2–5 proof does not need.
- A cross-write is obvious with two documents and two known strings. It is not obvious with twenty.

Run the 20-model string board the way #931 already does. For native LO rows, a pool of 2, then 4, then at most 5, is the v1 knob. If those five are busy and the UNO queue is still mostly idle, raise the cap. If the UNO queue or the venv pipe is saturated at 4, more agents will not help on one soffice.

## Experiment Keith can run next

No full matrix. No harness rewrite. The text proof is `scripts/prompt_optimization/prove_lo_multi_doc.py`: one `LOBackend`, N agent threads (default 2), no OpenRouter, no second soffice. Do not set `WRITERAGENT_TESTING=1`. A green run is headless only; it is not evidence about headed AFC.

```bash
make manifest   # once; plugin/_manifest.py is gitignored
.venv/bin/python scripts/prompt_optimization/prove_lo_multi_doc.py
.venv/bin/python scripts/prompt_optimization/prove_lo_multi_doc.py --n 4
```

The process exits non-zero unless the sleeps overlap, each Writer document contains only that worker’s tokens, UNO enter/exit intervals on `_lo_thread` do not overlap, and `len(_lo_docs)` is N after the first write. Enter/exit is logged inside the closure `LOBackend.call` runs, not around the agent’s queue wait.

### N=2 text, no API

Start `LOBackend` once. Spawn two threads. Give each a unique token.

On each thread, three steps, with a timestamp log around the sleep:

1. `LOBackend.call` a closure that `acquire_document("writer")` and `setString("alpha-1")` (the other thread uses `beta-1`).
2. `time.sleep(0.5)` on the **agent** thread. This stands in for `request_with_tools`. It must not run inside `LOBackend.call`.
3. `LOBackend.call` again: append `alpha-2` / `beta-2`, then `getString()`.

Pass criteria:

- The two sleeps overlap (each start is before the other’s end). Same shape as `test_lanes_overlap_and_lo_stays_fifo`, but both jobs are LO callers.
- Doc A’s string contains only `alpha-*`. Doc B’s string contains only `beta-*`.
- A log line at the start and end of each queued closure shows the UNO sections do not overlap, and `_lo_thread` is the thread that runs them.
- `len(_lo_docs)` is 2 between step 1 and process teardown (both caller ids live).

If the strings cross, stop. The bug is current-component or a shared doc slot, and N=4 will not clarify it.

### N=4 text, no API

Same script, four tokens, four threads (`--n 4`). Pass criteria are the same, with all four sleeps overlapping and four distinct documents. This is the “small pool” check. If N=2 passes and N=4 fails, the failure is office load or dispose, not the locking model.

### N=2 with a real short model call

Only after the text proof. Two threads, each calling `run_llm_chat_eval(..., backend="lo")` on a tiny Writer ask (one `apply_document_content`, not the flag) **without** wrapping those calls in `LoLane`. Watch the banners: `Calling model` for thread B should print before thread A’s export. Confirm the two exports differ. One `LlmClient` per `run_llm_chat_eval` is already how the function is written; do not share a client across the two threads.

### N=2 flag sketch, still one soffice

Only after the text proof. Thread A runs a short `run_venv_python_script` that inserts one named shape. Thread B, overlapping A’s sleep or A’s next LLM round, `setString`s a second Writer doc. Export both (A via `export_writer_odt`, B via `getString`).

- If B’s text is intact and A’s `.odt` has the shape, the current-component pin is doing its job (or was unnecessary because nothing else touched the desktop).
- If A’s shape lands in B’s doc, or A’s RPC errors with no active document / a local-context failure, that matches `host_rpc.execute_tool` plus `get_ctx()` not being `_lo_ctx`. Pin current component per `LOBackend.call` and `set_fallback_ctx(_lo_ctx)` before trying N=4 flags.
- Do not compare this to a headed AFC run of the same Ask.

### What not to do in the first proof

- Do not start a second soffice, a second pipe, or a process pool.
- Do not put 20 threads on one desktop.
- Do not set `WRITERAGENT_TESTING=1` or route tools through `execute_on_main_thread`.
- Do not treat a green string-lane overlap test as evidence that two LO documents stayed apart. That test never opens a document.
