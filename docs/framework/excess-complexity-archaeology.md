# Excess complexity: a field guide to subtle AI slop

Notes from the "s55" cleanup loops (Oct 27–31, 2026; 329 commits,
one file or module per commit, each titled `NN-module: s55 complexity
cleanup`). The Oct 30–31 overnight run covered `plugin/`; the `compute_service/`
directory (parallel to `plugin/`) was done earlier, on Oct 28–29. The loop's
whole prompt is a short open-ended instruction to remove *excess* complexity.
A second round on Nov 1 (75 commits, including `writer/` and `chatbot/`) is
summarized near the end; it adds kinds 17 and 18 and a list of new risks.
A structural review on Nov 12 that followed one request across files adds
kinds 19 to 25, the kinds a one-file pass cannot see.
This document records what that turned out to cover. It is a description of
the kinds of excess found, not a checklist for the next pass.

Evidence is the git history: `git log --grep=s55 -p -- <file>`. Commit hashes
below are examples, not a complete list. Counts are impressions from reading a
few dozen files, not measurements.

## The thesis

When people say "AI slop" they usually mean **duplicate functions**: the same
helper written twice. That kind exists, and it is the smallest part.

Most of the excess in this codebase was something quieter. It was **code that
was individually reasonable when written, and that nothing needs now.** Each
piece was added by a loop or a fix with a good local reason: a guard, a flag, a
fallback, a wrapper, a comment about a past bug. None of it is wrong, tests
pass with it, and a reviewer reading one diff sees nothing to object to. The
excess only shows up when you read the surrounding code and ask what each piece
is *for*, and the answer is "a case that can't happen" or "a distinction
nobody uses."

Tests do not find this kind of slop. Tests exercise behavior someone thought
to specify. Unreachable guards, one-caller wrappers, and mirrored data
structures never fail a test, so they accumulate.

## What has to be true before this works

The loop is safe and productive here because three things were already in
place. Without them, "remove excess complexity" would delete real safety.

1. **Centralized error handling.** Tool failures funnel through one place
   (`ToolRegistry.execute` → `_tool_error`, `format_error_payload`,
   `WriterAgentException`, `is_disposed_exception`; see
   [exception-policy.md](exception-policy.md)). When a function raises, the
   caller gets a structured error with the original type and message. So a
   per-function `try/except` that logs and returns a default is *usually*
   redundant: the platform's error already reaches the model and the user.
2. **Validation at the boundary.** Tool arguments are schema-validated at the
   API level, so tool bodies don't need to re-check types. The reverse is a
   known gap: a few paths run *before* schema validation (for example
   `requires_document_lock` in `_prepare_mcp_execution`), and internal calls
   between the host and the venv worker bypass schemas. Those keep their
   guards.
3. **A well-factored codebase and full tool access.** The reviewer (human or
   AI) has to be able to read one file plus the few functions it calls and
   reason about what is truly dead. That means helpers live where you'd look
   for them (`text_helpers`, `errors`, `llm_lanes`) and the agent can open any
   file to confirm a claim like "this value is always a dict by the time it
   gets here." A cleanup pass that can't verify its own assumptions is just
   deleting code on a guess.

A policy that makes this easier to apply: **if Python, LibreOffice, or the
schema layer would already raise on this input, a custom guard is not
needed.** The platform's error is often better (and localized).

## The decision rule

For any guard, fallback, or special case, ask what happens when it's removed
and the bad input arrives:

| Next operation… | Verdict |
|---|---|
| raises anyway (`TypeError`, `KeyError`, UNO exception) | The guard is redundant. Remove it. |
| succeeds with a **wrong result** (silent overwrite, negative counter, `bool("false")`, last-duplicate-wins) | The guard is load-bearing. Keep it, or make the caller pass the right thing. |
| runs **before** validation or via an internal call that skips the schema | Keep the guard. |
| swallows the error (inside `except Exception: return False`) | Check what "no match" now means. |

This is the one rule worth stating in a prompt. Everything below follows from
it.

## A taxonomy

Roughly from most obvious to most subtle. Examples are real diffs from the
loop.

### 1. Surface duplication (obvious, least common)

Repeated blocks folded into a helper or loop.

- `tts_service`: three `ManagedSubprocess.run([...], check=False,
  stdout=DEVNULL, stderr=DEVNULL, timeout=N)` calls became
  `_run_quiet(cmd, timeout)`.
- `writer_importer`: seven `try: obj.setPropertyValue(n, v) / except: log.debug`
  loops became `_set_prop_map(obj, {...}, fmt)`.
- `trusted_action_registry`: seven identical `TrustedActionWiring("units", …)`
  rows became one generator over `_SPEC_DOMAINS`.

### 2. Defensive code for states that can't occur

The biggest category by volume, and the one tests can't see.

- `try: float(x) except OverflowError` around `float(float_value)`: Python
  doesn't raise there.
- `assert isinstance(config_data, dict)` right after a function that returns
  a dict.
- `if not isinstance(block, dict): continue` over a list the parser just built.
- `getattr(doc, "lockControllers", None)` then `callable(...)` for a method that
  always exists on a document.
- `hasattr(os, "set_blocking")` ladders, replaced by a single
  `sys.platform == "win32"` check.
- `try: ex.pending_work_count() except Exception: qdepth = -1`.

Removing these is safe when the next line would raise anyway. It is where the
decision rule above matters most.

### 3. Special cases the general path already covered

- `coerce_index`: `if isinstance(value, bool): return int(value)` before
  `if isinstance(value, int)`. `bool` is an `int`.
- `_coerce_spill_value`: separate date, datetime, and time branches each built
  their own return dict. They now parse into one value and recurse into the
  general function.
- `flatten_result_values`: a hand-written flatten loop replaced by a call to the
  existing `_result_as_spill_grid`.
- `if after: return s[idx:]; return s[:idx]` → `return s[idx:] if after else s[:idx]`.

### 4. Hand-rolled mechanisms the standard library already provides

- `llm_lanes`: a manual deadline/remaining/`wait(timeout=remaining)` loop became
  `Condition.wait_for(predicate, timeout)`.
- `event_bus.unsubscribe`: a 25-line `_same_callback` for comparing bound
  methods. Bound-method `!=` already compares `__self__` and `__func__`, so it
  became `live != callback`.
- `__slots__` plus a manual `__init__` became `@dataclass(slots=True)`.
- `tool.py`: a "merge without overwriting existing keys" loop became
  `{**common, **specific}`.
- `proc.terminate()` on Windows vs `proc.kill()` elsewhere: on Windows `kill()`
  is already `TerminateProcess`, so the branch was dropped.

### 5. Wrappers, aliases, and constants that name one thing

A helper or name that exists for a single caller adds a hop for the reader.

- `_config_path()` wrapped by `get_config_file_path()`.
- `_get_request_protocol_version(handler)`, one caller, two lines.
- `_writer_embed_is_chart(host)` ≡ `_is_chart_host(host, is_embed=True)`.
- `_MAX_IMAGE_DISPLAY_WIDTH_MM = 170`, used once.
- `_SEND_CANCELLATION_UNSET`: a sentinel for "caller didn't pass it," replaced by
  a required keyword.

### 6. One-instance generalization

Flexibility nobody used: extra parameters, two slots where one list works.

- `generation: int | None = None` became `generation: int` in `tts_service`;
  four `if generation is None: return` guards went. This is a **forcing
  function** change: callers now have to say which utterance they belong to,
  and `make typecheck` finds any that don't.
- `tts_service` had `_play_proc` plus `_synth_procs`, a `slot="play"|"synth"`
  argument that was always `"play"` at the surviving call sites, and a
  `_speech_cancelled` event derived from them. Now one `_procs` list.
- Parameters with one caller: `timeout_label=`, `select_target=`,
  `missing_code=`, `run_threaded=`.

### 7. Mirrored data and caches around cheap things

- `RpsDomainSpec` mirrored `DomainWiring`, with `build_rps_spec` generating
  closures to convert one to the other. Now `DomainWiring.insert` does it.
- `_PendingConfigEvent` plus `_event_for_changes` collapsed into
  `_emit_pending(effective, event_key=, batch=)`.
- `_picker_cache` that copied a constant list, now a tuple constant.

### 8. Plumbing: a dependency passed through layers that don't use it

Easy to miss in a single-file read.

- `vision_html_export`: `convert_fn` (the latex2mathml function) was passed
  through `_replace_tex_outside_tags` → `_convert_tex_in_text` →
  `_latex_to_math_element`. Only the innermost function called it. Now it
  resolves its own converter and three signatures lose a parameter.
- `trusted_rpc.run_trusted_worker_action` had `headers=` and `header_row=`
  parameters that duplicated what `additional_data` already carried.

### 9. Sentinel-slot return values and exception ladders

- `vision_docling._run_docling_conversion` returned a 7-tuple. On failure six
  slots were placeholders: `(error_dict, None, "", [], [], [], [])`, repeated in
  eight `except` branches. It now returns the error dict or a 5-tuple, callers
  check `isinstance(converted, dict)`, and the eight `except` clauses became one
  `_docling_error(exc, helper)` function with ordered `isinstance` checks and a
  comment about which exception subclasses which. The trade is a union return
  type the caller must check, in exchange for no placeholder slots.

### 10. Whole mechanisms replaced by a simpler design

The deepest kind, and it needs a few earlier passes first.

- `event_bus`: an `RLock`, a `_guard_depth` counter, `_pending_cleanups`, and
  `_applying_cleanups`, all there so a weakref callback could safely re-enter
  the lock from GC. Replacing the weakref callback with lazy removal of dead
  entries on the next `subscribe`/`unsubscribe`/`emit` removed the problem the
  machinery solved. 317 lines → ~208.
- `testing_runner`: nine module globals and three `global a, b, c, …` lines
  became one `SimpleNamespace`.

### 11. Layers of hardening that later became redundant

Hardening loops add guards. Later loops, working on stable code, find that
newer fixes made the older guards moot.

- Windows `ty` failed on POSIX-only `os.get_blocking`. The fix added a
  `hasattr` ladder and a long comment. Later the ladder was reduced to one
  platform check.
- `mcp_protocol`: `_arguments_without_thread_guard_bypass` was inlined as a
  dict comprehension, with the same stripping also present in
  `ToolRegistry.execute`.

### 12. Comments that describe the past

Not "too many comments" but comments about *what the code used to do*. A
future reader doesn't need the old behavior, only the rule.

- Before: `# ``html`` used to be in the key loop above, so this branch never ran…`
- After: `# Table HTML is not prose; the grid is rendered separately.` plus one
  clause about the old failure.
- Interleaved `# Reject if…` comments inside a regex-heavy function became a
  single docstring that states the rule once.

The "what was wrong / how it happened / why this fixes it" convention in
`AGENTS.md` still applies. The loops shorten these blocks but keep them at the
fix.

### 13. Production seams that exist only for a test

`compute_service/worker_base.py`: the lazy pool holder had a `_before_handout`
method, a no-op in production, whose docstring says "Tests override this." It
opened a window between publishing the pool and handing it out so a test could
race a shutdown into it. The race it tested existed only because of the
machinery in kind 14. When that went, the hook went too.

### 14. A concurrency protocol whose only job was to handle a rare path

Same file. `get()` tracked `_build_id` and `_epoch` so that a shutdown arriving
mid-build would discard the new pool and make the shutdown wait for the build.
That is about 45 lines of state and a long "what was wrong" docstring, for a
case that only happens when a *non-permanent* shutdown races a pool build. The
production server calls `shutdown(permanent=True)` on the way out, and a
non-permanent shutdown happens only in tests and restarts. The new `get()`
checks `_closed` once after the build and shuts down the pool it just made if
the holder is closed. The trade (see Risks) is that a non-permanent shutdown
during a build no longer blocks until the build ends.

### 15. Dead configuration modes and repeated rationale

- `wsgi_server`: `if getattr(server, "park_idle_connections", False): … else:
  super().setup()` in both `setup` and `finish`. Only the parked mode is used,
  so the flag and the `else` branch went, along with its `hasattr(server,
  "_conn_states")` probes. The server class always defines `_conn_states`.
- The same paragraph about why the listener pool is sized `workers + vision +
  2` appeared in the `ListenerBudget` docstring, `service_listener_threads`,
  and `sticky_listener_slots`. It now lives in one place, and the two
  accessors have one-line docstrings.
- Small helper pulled out of repeated code: three copies of `close_connection =
  True; state.close_reason = …; state.close_connection = True` became
  `_mark_close(state, reason)`.

One of these moves cost readability. `ListenerBudget` was a frozen dataclass
with named fields; it became a `(listeners, sticky)` tuple, so the accessors
read `_listener_budget(settings)[1]`. That is shorter but less self-describing
than `.sticky`. Replacing a named record with a tuple is a trade the loop makes
now and then, and it's worth checking when you see one.

## How the strata show up over time

Reading the history of one file shows layers, like a dig:

1. **Original code.** Written quickly.
2. **A hardening era.** Bug-hunt loops add guards, flags, fallbacks, and
   long bugfix comments (`queue_executor` Oct 6–17; `mcp_protocol` Oct 7–15).
3. **A trim.** An early "remove excess error handling" pass strips
   `try/except` blocks. One of these was too aggressive: `46b967e4b` is titled
   "Restore regressions from recent cleanup sweep".
4. **Structural moves.** Code is extracted into other modules.
5. **The s55 passes.** Each pass finds the next layer down.

In the s55 run:

- **Early passes** mostly caught surface duplication, annotation repeats,
  and control-flow compaction (kinds 1, 3, 5).
- **Mid passes** did structural replacements: collapsing slots, replacing
  mirrored structures, rewriting `event_bus` (kinds 6, 7, 10).
- **Late passes** reached the subtler kinds: capability probes, error-handling
  removal, plumbing, and comment rewriting (kinds 2, 8, 9, 12).

The later kinds only become visible once the code is otherwise stable. You
can't tell that a `getattr(…)`+`callable()` check is paranoid until the code
around it has been reliable long enough to show the method always exists.

## What it does for readability

Gains that held up in the diffs:

- **Fewer concepts, not just fewer lines.** `stop_speech` lost `was_active`,
  `_speech_cancelled`, `play`, `synths`, and `all_procs`. A reader no longer
  asks what each one means.
- **Standard behavior replaces custom behavior.** `wait_for`, `!=` on bound
  methods, `proc.kill()`, and `range(32)` loops mean the reader's existing
  knowledge applies.
- **A comment disappears because the question is gone.** The `_same_callback`
  comments disappeared with the function. The remaining comments explain real
  constraints.

Costs to watch:

- **Density.** Boolean-identity returns (`return s if (inst < 0) == after else ""`)
  and nested comprehensions are shorter but harder to check against a spec.
  A one-line comment in words restores it.
- **Merged mode-specific branches.** `XMATCH` mode handling went from
  one-block-per-mode to a shared tail. It's shorter, but you read control flow
  instead of looking up a block.
- **Closures with `nonlocal`.** `_take_table` replaced a four-line unpack at
  two call sites with a closure that mutates `table_index`.

None of what was found was clever for its own sake: no `reduce`/`lambda`
chains, no dense one-liners beyond the occasional boolean identity.

## What is not excess

Things the loop correctly left alone, and that a pass should keep leaving:

- UNO and LibreOffice quirk handling that looks redundant but encodes real
  behavior (disposal re-raising, `PyUNO Enum.__eq__` being false for ints).
- Guards on paths that run before schema validation, or on internal calls that
  skip it.
- Checks where the next operation would **succeed with a wrong result**.
- Bugfix comments at the fix site.
- The "do not redo" list in `AGENTS.md` (alphabet-split `calc_functions_*`,
  the three scripting registries, etc.).

## Known risks of this process

The labels say "no behaviour change." Several commits did change behavior on
edge inputs, which is acceptable when deliberate but should be known:

- Reserved-key collision in `trusted_rpc` no longer raises; the routing keys
  written last win.
- Duplicate domains in `_TRUSTED_ACTION_WIRING` no longer raise; a dict
  comprehension keeps the last.
- `_tool_needs_document_mutation_gate` used to fall back to `detects_mutation()`
  if `requires_document_lock` raised. Now the error propagates. Combined with
  unvalidated arguments in `_prepare_mcp_execution`, that path deserves a test
  with non-string, `None`, and missing-key arguments.
- Two "log once" flags merged into `_logged_callback_problem` in
  `queue_executor`: after one warning fires, the other stays quiet until reset.
- `event_bus` removes a garbage-collected subscriber lazily, not immediately.
- `compute_service` pool holder: a non-permanent `shutdown()` that lands during
  a pool build no longer discards that build or waits for it. The pool can
  appear after `shutdown()` returns. Production uses `permanent=True`, so this
  affects tests and restarts. The protocol that prevented it (`_epoch`,
  `_build_id`, `_before_handout`) was removed.
- A cleanup pass lost Kokoro TTS test coverage (`4e86cbd81`). A pass that
  removes code can remove tests with it, so check the diff for deleted tests.

Mitigations that already exist: the typecheck suite, the pytest and UNO
suites, and the habit of reading the diff of the commit after the loop.

## Running a pass

The prompt that works is short and open-ended. The first version had too many
"do not" rules and the loop produced less. Loosening it found more. The model
is trusted to interpret "excess" using the codebase's own conventions.

What the reviewing agent needs access to:

- the file under review plus the callers and callees it names,
- the centralized error path (`errors.py`, `tool.py`),
- `AGENTS.md` and the nearest area `AGENTS.md`,
- the ability to run the relevant tests and `make typecheck`,
- the ability to open any other file to answer "is this value really always
  a dict here?"

A useful one-line addition if density creeps up: *prefer removing a concept
over compressing an expression.* It is not currently needed.

When reviewing a pass, the questions that find problems are:

1. Did the commit delete a test? Which one, and is the behavior still covered?
2. Did a removal change a failure from loud to silent (kinds 2 and 11)?
3. Does any removed check sit before schema validation?
4. Did a merge of branches change the order in which checks run
   (`domain_registry.supports`)?
5. Did the commit undo an earlier pass? Diff the file against its state two
   passes back and look for hunks that net to zero. `65ff3d45b` (#3014, Nov 2)
   turned the end/beginning insert branch in `apply_document_content.py` back
   into the if/else that `4ce9e3b53` (#2887) had replaced with a dict lookup.
   Both were listed as cleanups. A reversal is not harmful, but it is a pass
   that paid nothing, and that is the signal the stopping rule below reads.

## Stopping rule

The loop does not converge on findings. The reviewer is asked an open-ended
question and always answers it: across the tracker the mean findings per file
per round stayed between 3.5 and 6 from round 2 through round 7. Asking the
reviewer whether a file is finished gives the same nonzero answer, and capping
the number of findings per pass would be worse, because one removal is often
what makes the next layer visible (see "How the strata show up over time").
So the prompt stays loose and open, and the decision to stop comes from the
diff instead.

What does converge is the **yield** of a pass: production lines removed net of
lines added, and the ratio of deletions to insertions. Over the whole loop,
averaged by how many times a file had been passed over:

| Pass over a file | Files | Net lines removed per commit | Deleted ÷ inserted |
|---|---|---|---|
| 1st | 425 | 18.6 | 2.19 |
| 2nd | 329 | 12.9 | 1.99 |
| 3rd | 254 | 11.5 | 1.72 |
| 4th | 189 | 9.8 | 1.79 |
| 5th | 115 | 9.2 | 1.65 |
| 6th | 57 | 7.6 | 1.40 |
| 7th | 27 | 7.1 | 1.41 |
| 8th | 17 | 4.9 | 1.35 |
| 9th and later | 12 or fewer | rises again | rises again |

(`scripts/s55_convergence.py --by-pass`, Nov 2. Counts use `git log -w` over
`plugin/` with tests excluded, and every s55 commit that touched a file counts
as a pass over it, including a neighbour module's.) The ratio falling toward 1
means a pass is moving code, not removing it. The rise after the 8th pass is
the dozen hub files (`tool.py`, `panel.py`, and similar) where each pass keeps
paying, which is why the rule is applied per file, not to the run.

**A file is done when its last two passes were both low-yield**, where
low-yield means fewer than 5 net lines removed *or* fewer than 1.5 lines
deleted per line inserted. One low pass is not enough: `apply_document_content`
reshuffled on its 3rd pass (83 in, 79 out) and then removed 36 and 61 net
lines on the next two. Two in a row is the signal. The line count is still not
the metric for a *single* pass (`venv_worker` lost concepts at constant
length), but two consecutive passes that add as much as they delete is a
reviewer circling.

```bash
.venv/bin/python scripts/s55_convergence.py --continue-only   # files worth another pass
.venv/bin/python scripts/s55_convergence.py plugin/writer/    # one subtree
.venv/bin/python scripts/s55_convergence.py --csv out.csv     # rows for the tracker
```

Applied on Nov 2 to the 165 files the tracker marks "not complete", the rule
marks 68 of them done. Thresholds are flags (`--min-net`, `--min-ratio`,
`--window`); raise `--window` to 3 if the loop still looks like it is spinning
on files the rule lets through.

## Worked example: `plugin/scripting/venv_worker.py`

One file, ~1,600 lines, 100+ commits (the strata below start on Oct 10). It manages the long-lived
user-venv Python child that runs `=PY()` and trusted actions. It is a good
single specimen because it went through every layer described above, and a
reader can see how the kinds of excess interact. (Commit hashes are from
`git log --follow -- plugin/scripting/venv_worker.py`.)

### Stratum 1: hardening (Oct 10–18)

Bug-hunt commits added the machinery: heartbeat protocol and child stdout
locking (`e7c70bc21`), spawn on a manager-lifetime thread for parent-death
signalling (`03bd1e13d`), host-only setup before the frame write
(`e50f3edd8`), pipe ownership and shutdown signalling (`cbb5f1f2d`), a log-once
for unknown frames (`69519b1c2`). Each came with a long incident comment. All
of it was reasonable. The result was a file where almost every guard had a
story, and some stories had stopped being true.

### Stratum 2: consolidation (Oct 18, `ed7ffb0af`, `9eac98147`, `5bf494d52`)

Three commits in one afternoon replaced bespoke mechanisms with shared ones.

- **Kind 4, kind 10.** A per-write daemon thread existed so a stalled child
  could not hold `_io_lock`. It was replaced by
  `ipc.write_packed_frame_with_timeout` (~40 lines gone, along with
  `_stdin_writer_thread`). The reading side had a POSIX `select()` path, a
  Windows threaded path, and a per-platform `_read_exact_win32`. A comment on
  the old code said "Do not merge this with `ipc.read_pickle_frame_with_timeout`…
  a hang-regression risk for `=PY()`." In `9eac98147`, the same series, they were merged into
  `read_frame_payload_with_timeout`, with new tests in `test_ipc.py`. The caution had been about a state that
  later work made unnecessary, which is how kind 11 (hardening that becomes
  redundant) arises.
- **Kind 8, as an *addition*.** `_ExecRequest`, a frozen dataclass, replaced a
  `_build_request(code=…, data=…, …)` keyword list that was threaded through
  several methods. Not every simplification deletes. This one adds a type to
  stop the same 19 fields being passed by name through four layers, and
  `wire()` is the one place that decides what goes on the wire.
- An earlier trim (`b882b57e1`) removed a `try/except` around
  `clear_python_addin_cache()` in `_clear_host_state_after_worker_death`.

### Stratum 3: an overreach, then a correction (Oct 18, `709373f57`, `d51946eb0`)

`709373f57` moved ~150 lines of incident comments out of `venv_worker.py` into
a new `docs/scripting/venv-worker-history.md`. `d51946eb0` reverted it a few
hours later: "Put the knowledge back next to the guards it explains. Plain tips
only: no What/How/Why template blocks and no separate history doc."

This is the boundary on kind 12. Comments that narrate *what the code used to
be* can be reduced to the current rule. Knowledge about *why a guard exists*
belongs next to the guard, not in a separate file nobody opens while editing.
That is the line the s55 passes have kept to since.

### Stratum 4: the cleanup batches (Oct 28–29, `d349f172e`, `9d55670a9`, `2b4e3a34d`)

The three passes, with the kinds from the taxonomy:

| Change | Kind |
|---|---|
| `_WorkerRetired("Python worker was replaced by a new venv path and will not be restarted")` repeated as the argument at five raise sites; the message became the exception's default (`raise _WorkerRetired()`) | 1 |
| Seven `_worker_error(code, msg, details={"exe": self.exe, ...})` call sites became `self._err(code, msg, **extra)` | 1 |
| `getattr(self, "pool", WORKER_POOL_DEFAULT)` → `self.pool` (the attribute is always set in `__init__`) | 2 |
| `_worker_registry_key(exe, pool)`: a one-line f-string helper with one caller | 5 |
| `_disable_systemd_scope()`: three lines, one caller | 5 |
| `_take_reap_handles` / `_inherit_reaps`: an accessor pair for one handoff, inlined at the single call site under the same lock | 5, 8 |
| `_maybe_dispatch_ppt_master_response` inlined into `_maybe_dispatch_intermediate_response`, which was its only caller | 5 |
| `_reap_handles`, `_spawn_handle`, ... declared in the class body *and* assigned in `__init__`; the class-body declarations were deleted | 5 (declarations twice) |
| `PythonWorkerManager.pool_is_running()`: a public classmethod whose only caller was a test | 6, 13 |
| `heartbeat_grace_sec`: an optional parameter threaded through `_ExecRequest`, `_execute_ipc`, `run_code_in_user_venv` and into the heartbeat logic; no production caller passed it (tests asserted it was *not* passed), and the logic always fell back to `EMBEDDINGS_HEARTBEAT_GRACE_S` | 8 |
| `return None, err or _worker_error("VENV_NOT_FOUND", …)` fallbacks → `return None, err`; `err or _worker_error(...)` → `assert err is not None; return err` | 2, 6 |
| Two parallel `if embeddings:` branches (message + log line) → one `prefix`/`which` variable and one format string each | 3 |

The second to last row is a small one worth studying: a defensive fallback
("if there is no error, make one up") became an invariant (`_get_worker_manager`
returns `(manager, None)` or `(None, error)`). Under this codebase's rules that
is the better form, because if it is ever violated it should be loud.

### What this file shows

1. **The line count is not the metric.** From the consolidation commits to
   today the file went from 1,595 to 1,590 lines. The cleanup removed *concepts*
   (a writer thread, two platform readers, an accessor pair, a grace-period
   knob, a dead classmethod) while earlier commits added capability. A reader
   who opens the file now meets fewer things that need explaining.
2. **The strata depend on each other.** The Windows reader comment in stratum
   1 said not to merge the readers. Stratum 2's `ipc` consolidation removed
   the reason for that comment. Later passes then found smaller leftovers
   (the grace-period parameter, the accessor pair) that were only visible once
   the bigger structure had settled. A cleanup pass early in the file's life
   would have been working on code whose structure was about to change.
3. **Most items are individually trivial.** There are 8+ small moves in
   stratum 4 and none is clever. The value is that they are made *together*,
   with a reader who can open `ipc.py` and `test_trusted_rpc.py` to confirm
   that no caller passes the parameter.
4. **The preconditions did the work.** `_err` is trivially safe because every
   IPC failure becomes a structured error payload. `heartbeat_grace_sec` could
   be removed because grep and the typechecker see every caller. The `assert
   err is not None` is acceptable because `_get_worker_manager` is the only
   producer of that pair, and it is in the same file.
5. **It borrows from the other examples.** Kind 8 (plumbing) here is the same
   shape as `convert_fn` in `vision_html_export`. The `_before_handout` /
   `_epoch` removal in `compute_service/worker_base.py` is the same shape as
   the writer-thread removal (kinds 13/14 vs kind 10): a protocol or seam that
   existed to survive a race that a shared helper later made impossible.

### What to check in a file like this

- **Heartbeat timing.** `heartbeat_grace_sec` is gone, so the grace is a single
  constant. Confirm no caller needs per-call control (none did at the time).
- **Pre-start retry.** `_ExecRequest.request_id` is minted once so a retry
  writes the same id. Refactors near it must keep that.
- **Replay safety.** The comments about `exec_started` (user code running
  twice if a read timeout is retried) are why the `IpcPartialFrameTimeout`
  handling must stay. They are not excess.

## Worked example: `plugin/framework/tool.py`

The tool registry: `ToolBase`, `ToolRegistry.execute`, `get_schemas`, argument
preparation, the per-tool timeout. 149 commits; 1,124 lines before the s55
passes (`2fce84642^`), 1,060 after the first round. Eight s55 commits touched
it (`2fce84642`, `752aed78d`, `a31af6949`, `3da57fa9c`, `6ad4b310b`,
`b4c8487f7`, `e6d193ab9`, and the Nov 1 second pass `730054d1c`; the line
counts here and the "What to check" list were written before that last one).
Only this file is covered here; `tool_schema.py` and the other `tool*` modules
had their own passes. Where a change spans files, the
neighbor is named.

This file is the best specimen for the second half of the thesis. `venv_worker`
shows hardening and consolidation. `tool.py` shows how **hardening loops leave
the same fix in several places**, and how later passes find the copies.

### Hardening left several kinds of residue

Hardening commits added real fixes: reporting dropped arguments instead of
losing them (`a40e2c261`), real-disposal re-raise (`479f43bd3`), waiting on the
result queue instead of the worker thread (`5c63ed76e`), unknown-protocol check
before any document probe (`479f43bd3`). Each fix was right. Four things
accumulated around them.

1. **The same bugfix, applied wherever it was observed.** A schema whose
   `required` is a string (`"name"`) was iterated as characters. The fix existed
   at three sites: `ToolBase.validate`, `prepare_tool_call`, and the
   strict-provider schema pass in `tool_schema.py` (two of the three carried a
   `Bugfix:` comment).
   `e6d193ab9` replaced all three with one `required_keys(schema)` helper whose
   docstring carries the bugfix note once. This is a kind the taxonomy above
   does not list: **a fix duplicated per call site** (kind 16 below). Nothing
   was wrong with any copy, and a reader could not tell they were the same fix.
2. **Code shaped by a test's introspection.** `2693b7581` added
   `_unused_runner = runner` inside an inner `_invoke()` plus a six-line "What
   was wrong" comment: a test inspected `_invoke`'s closure free variables to
   prove execution targeted `execute_safe`. `479f43bd3` removed the line the
   next day. `752aed78d` removed `_invoke` itself, so the registry now calls
   `_execute_with_timeout(_run_tool, timeout, tool_name, run_threaded)` directly
   (kind 13). The seam existed to be inspected, and it outlived the test that
   needed it.
3. **A report that grew special cases.** `a40e2c261` added `ignored_parameters`
   (arguments the registry dropped) with a `had_ctx_arg` flag and a manual
   `ignored.append("ctx"); ignored.sort()`. `2fce84642` computed the list from
   the original `tool_kwargs` instead, so `ctx` falls out naturally. `e6d193ab9`
   then removed the `kwargs.pop("ctx", None)` that preceded the allow-list, since
   `without_unknown_kwargs` already drops it. Two passes, one concept: the
   report's special case disappeared once the report was computed from the
   right input (kind 3).
4. **Flattening done halfway.** `479f43bd3` hoisted the unknown-protocol check
   to the top of `get_schemas` and turned `if openai / elif mcp / else raise`
   into a straight line. That left two near-identical bodies: build schemas,
   narrow enums, filter peer messages. `2fce84642` unified them behind
   `convert = to_openai_schema | to_mcp_schema` and
   `peer_domain = active_domain | None`, selected once. The MCP-only
   difference is now two assignments instead of a copied tail.

### What the s55 passes removed

| Change | Kind |
|---|---|
| `get_tools(tier=, intent=)` filters, then (`6ad4b310b`) the `ToolBase.intent` field itself, then every `intent = "edit"` declaration on every tool: 37 files, 149 deletions. Nothing read it; its docstring was reduced to "Registry lookup does not filter on it" before it was deleted | 6, 7 |
| `ToolRegistry.batch_mode = False  # suppress per-tool cache invalidation`: declared and assigned, and nothing else in `plugin/` or `tests/` read or set it | 2 |
| `__len__` on the registry (`e6d193ab9`) | 5 |
| `_normalize_schema_for_strict_providers as _normalize_...` re-export from `tool.py` (contrary to the repo's no-re-export rule) | 5 |
| `_UNSET_EXCLUDE_TIERS = object()` sentinel; the real default (`_DEFAULT_EXCLUDE_TIERS`) is now the parameter default | 5, 6 |
| `_execute_with_timeout(..., **kwargs)` forwarded to `func(**kwargs)`; no caller passed any | 6, 8 |
| `kwargs = dict(tool_kwargs)` in `_execute_impl`: `prepare_tool_call` already copies at entry. `tools_kwargs = dict(kwargs)` in `get_schemas`: `**kwargs` is already a fresh dict | 2 (defensive copies) |
| "merge `common_details` into `result_details` without overwriting" loop → `{**common_details, **result_details}` | 4 |
| `_is_specialized_domain_tool`: `split(":")` called twice plus nested `if ":" in …` → `parts = split(":")`, `return len(parts) > 1 and tool_domain == parts[1]` | 3 |
| `supports_doc` and `_tier_excluded` local closures, each used once → a comprehension and an inline `getattr` | 5 |
| `filter_get_image_for_text_only_model(tools, has_native_vision(...))` (`a31af6949`) → `if has_native_vision(...) is False: tools = [t for t in tools if t.name != "get_image"]` with the comment "Unknown (None) keeps get_image". This followed `0d4a9f140` making the answer tri-state, which left the helper one line long | 5 |
| `run_threaded` on `_execute_with_timeout` and its inline-and-warn branch → a small `_run_sync` wrapper in the caller that warns once per tool. The caller knows whether the tool is sync. The callee shouldn't need a flag for it. **A later pass (`730054d1c`, Nov 1) deleted `_run_sync`, `_sync_timeout_warned`, and the "declares timeout= but is synchronous" warning with its test.** The first pass moved the flag; the second removed what it had been for | 8, then 17 |
| `ToolPermissionError` dropped from a comment in `b4c8487f7` because `03-errors` removed the class: a cross-file ripple, not a tool.py decision | 12 |

### New kind (16): a fix duplicated per call site

Hardening commits are made by looking at a failing site. The fix goes where the
failure was seen. If the same defect is possible at two other sites, a careful
engineer (or model) usually adds the guard there too, each with a full
`Bugfix:` comment. The copies are not duplicate functions: they differ in
variable names and context, so a text search misses them, and each one is
correct. The signal is in the comments. Three comments describing the same
failure ("a string `required` was iterated as characters") means one helper.

A pass finds this by reading one file plus the helper module it already
imports. That is why the decision to centralize needs the codebase to be
factored the way this one is: `required_keys` belongs in `tool_schema.py`, the
module `tool.py` already imports from.

### What stayed

- `ToolBase.execute_safe` / `_execute_impl` / `prepare_tool_call` keep their
  structure: they are the boundary the other sections of this doc rely on.
  The edits there were copies, sentinels and special cases, not the flow.
- Both `bypass_thread_guard` strippings (here and in `mcp_protocol.py`) remain.
- A test that reads `inspect.getsource(ToolRegistry._execute_with_timeout)` to
  assert a type annotation is still there
  (`test_execute_with_timeout_parameterizes_result_queue`). It is the same
  shape as the `_invoke` freevars check: a test pinned to source text. It does
  no harm today and would break on a harmless rewrite of that function.
- The timeout path (`5c63ed76e`) is untouched: it waits on the result queue and
  treats a queued result as winning over a live thread. The long comment there
  explains a real race and is not excess.
- The `ignored_parameters` report is still emitted. Only the way it is computed
  changed.

### What to check

- **`ctx` and the allow-list.** Dropping `kwargs.pop("ctx", None)` depends on
  `without_unknown_kwargs` discarding any key the schema does not declare.
  `prepare_tool_call` always passes a dict schema (`get_parameters(...) or {}`),
  and `call_properties` treats a dict without `properties` as closed, so `ctx`
  is dropped unless a tool declares a `ctx` property. A test pins it
  (`tests/framework/test_tool.py` asserts `ignored_parameters` contains `ctx`).
  Only a non-dict schema would let it through.
- **Dropped copies.** `_execute_impl` no longer copies `tool_kwargs` itself.
  That is safe only while `prepare_tool_call` is the first and only place that
  mutates them. A change that mutates the caller's dict before it reaches
  `prepare_tool_call` would now be visible to the caller (MCP arguments).
- **Sync-tool warning.** Gone as of `730054d1c`. A tool that declares
  `timeout=` but is synchronous is no longer reported. Nothing breaks, but the
  author gets no hint that the timeout is not enforced.
- **`intent` removal.** `rg "\.intent\b|intent="` should stay empty under
  `plugin/` and `tests/`. Anything that quietly relied on the attribute via
  `getattr(tool, "intent", None)` would now see `None` and no error.

### Why this file is useful as a model

Almost nothing here is dead code in the simple sense. Each removal was
something reasonable that earlier work added and later work made unnecessary:
a filter that nothing used, a flag that explained a special case that went
away, a closure shaped for a test that was since rewritten, a fix repeated at
three call sites. The passes read like a conversation between the hardening
loops and the cleanup loops, and the cleanup only works because the file had
a stable place to put the shared version (`tool_schema.required_keys`).

## Second round (Nov 1): what a repeat pass finds

75 s55 commits from about 8am Nov 1 (about +6.7k / −8.8k lines), this time
including `plugin/writer/` (26 commits) and `plugin/chatbot/` (14), plus 35 in
calc, scripting, framework, notebook and librepy. For the "other" group, about
30 of 35 files had already had an s55 commit, so most of that group is pass 2.
Three reviewers each read a group's diffs against the taxonomy above. Method
note: these are skim-level reads of diffs plus targeted checks, not a full audit.

**Result: no break from the 16 kinds.** The dominant kinds are the same ones, in
roughly the same order (kind 2 defensive code and kinds 1/3/5 helpers and merged
branches lead; kind 6 "make the parameter required" and kind 8 dead plumbing
follow). No commit in this round reversed an earlier s55 pass; the first
reversal found came the next day (`65ff3d45b`, see question 5 under "Running a
pass"). What changed is the *character* of what is left: more of it is residue
of earlier work than original slop. Two kinds are worth adding.

### 17. Instrumentation and investigation residue

Code added while chasing a hang, a slowdown or a UI bug, with no remaining
reader. It differs from kind 12 (comments) and kind 2 (impossible states):
the code *runs*, nothing is wrong with it, and nobody looks at what it
produces.

- `1b92a9acc` `notebook/writer_importer.py`: `read_t0`, `cells_t0`, `reg_t0`,
  `_PROGRESS_EVERY_N_CELLS`, a `run_t0` parameter threaded into `_import_cells`
  (also kind 8), and four `elapsed_ms` log lines.
- `9e55e8599` `chatbot/rich_text_control.py`: `_log_scrollish_props` dumped every
  property with "scroll", "paper" or "wrap" in the name. Same commit:
  `_assistant_stream_start_len`, set in about six places, whose old docstring said
  the value "is only logged; it is not a splice point."
- `ac1013a67` `chatbot/slash_popup.py`: a six-level `getParent` walk and a
  `dir(tk)` filter, logged only to learn which key-handler methods exist.
- `ef9f7f815`: `dropped_count` with its `nonlocal`, feeding one log message.
- `a897aeb46`: `TurnController.mode`, passed to `begin_send_turn` at five call
  sites and never read.

The signal is a value that is written and logged but never branched on.
Unlike most kinds this one sometimes removes a diagnostic a user could see
(`730054d1c`'s "declares timeout= but is synchronous" warning); decide whether
that is wanted, since the code still "works" without it.

### 18. Self-checking scaffolding around code that can't go wrong

Verification added for a thing nobody can violate, or factored out only so a
test could reach it.

- Import-time asserts: `69fe5f035` `chatbot/agent_manual.py` had ~25 lines of
  module-level asserts (unique ids, alias resolution) and a `_MANUALS_BY_APP`
  mirror dict. The asserts moved into `tests/chatbot/test_agent_manual.py`, so
  the check survives, now off the import path.
- Contracts on trivial functions: `@deal.post(0 <= result <= 1)` on an unused
  `jaccard()` (`e337a25b6`); `@deal.post(... endswith("USER.md"))` on a one-line
  path getter (`d6590a141`).
- Pure decision cores with no production caller (the functional-core cousin of
  kind 13): `098063d1a` removed `decide_language_validation`,
  `decide_grammar_completion`, `deduplicate_grammar_batch` and three decision
  dataclasses (175 added, 472 removed overall). `decide_language_validation` was only ever called with a
  one-item chunk, so its multi-item requeue branch was dead too (kind 6).
  About 23 tests went with it.
- `ed733eb14`: pure planners `sticky_restart()` and `stop_during_take()` were
  inlined as `if speaking` / `if sticky` in `panel_voice.py`; their two matrix
  tests were deleted, so hands-free restart now has no unit test.

### Refinements to existing kinds

- **Callee already validates** (a cross-function cousin of kind 16). In
  `0c35a210f`, `yielddisc` and `yieldmat` dropped their own
  `int(float(basis))` and `0..4` check because `yearfrac` repeats it and
  returns NaN, so results are unchanged. In `730054d1c`, `to_openai_schema`'s
  `deepcopy` became `copy` because `_normalize_schema_for_strict_providers`
  already deep-copies on entry.
- **Shadowed default** (kind 2). `06b9c674a` removed `"processId": os.getpid()`
  from a module-level dict whose only consumer overwrites it. A dead write that
  looks like the real value.
- **Duck-typing ladders collapsing onto one declared contract** (kind 2).
  `ac1013a67`: `_append_sidebar_line` and `_clear_ask_box` probed three host
  shapes with `getattr`+`callable`; now `host.clear_ask()`, `host.clear_chat()`.
  The reviewer confirmed both `panel.py` and `sidebar_test_hooks.py` define all
  five methods, which is what makes the deletion safe.
- **Kind 5 in reverse.** `06b9c674a` removed `HARPER_SLOW_RESULT_MS` and inlined
  `500`. A named constant became a literal, a trade the loop makes when the name
  is used once.
- **Kind 16 in the chatbot.** `b6a26d702`: the retry-status callback block was
  pasted into both `_spawn_llm_worker` and `_spawn_final_stream` and became
  `_chunk_callbacks` (with new tests); `1a5c7eac1`: one "rollback then plain
  append" block repeated four times became `_rollback_then_plain`.
- **Kind 9 variant.** `fa89826c9` `_shift_part` returned `_Part | None` and both
  callers wrote `fmt(x) if x is not None else "#REF!"`; it now returns the
  string.

### What a second pass changes

- **It deletes the previous pass's scaffolding.** `_run_sync` was created in the
  first `tool.py` pass and removed in `730054d1c`. That is the multi-pass
  pattern the strata section describes, seen in the data.
- **Cascades dominate in files already touched.** `311563cd0` (a required
  `workbook_key`) and `118-sc_helper_domain` follow earlier caller narrowing.
- **Passes now sometimes add.** `_undo_batch` in `edit_review.py` (`f4e4d9695`)
  replaced paired `try/finally` + `close_surgical_context` blocks with a context
  manager, as `_ExecRequest` did. Also `_flag_default_true` (`9563ca9a8`) and
  `_bind_spawn`. Net lines removed in production code is still a fair measure of
  a pass, even when a shared helper accounts for part of the saving. Count tests
  separately, since a pass can delete tests along with the code. It misses the
  case where a file stays the same length while it loses concepts
  (`venv_worker.py`).
- **Guard removals held up.** Across the three groups the reviewers found no
  removed guard where the next step succeeds with a wrong result and no removed
  check that sat before schema validation. The one range check the decision
  rule says to keep was kept: `56e1f5f19` merged the int and string branches of
  `resolve_bibliographic_type` into one tail, `val if 0 <= val <= _BIB_TYPE_MAX
  else None`.

### New items for "Known risks" (verified in the diffs unless noted)

- **`3327f70a3` `librepy/panel_factory.py`: cleanup removed with its test.** If
  anything fails after `PythonSidebarController(...)` is built (for example
  `panel.resize_listener = …`), the `except` path no longer calls
  `controller.disposing(from_window=False)`, and the test for it was deleted.
  This is the most substantive behavior change found this round.
- **Behavior widened by a rewrite.** `9cebf2e80` `images.py`: replace-image used
  to push every path through a download that raised on non-http schemes, so a
  local `new_image_path` failed with `DOWNLOAD_FAILED`. `_local_image_path` now
  accepts local files (as insert already did), and `_resolve_graphic` runs before
  the download.
- **A mode changed result.** `f4e4d9695` `word_diff_split.py`: `split_change` in
  block mode returned one whole-range `SubEdit` and now returns `[]`; non-str,
  NaN and `None` input guards went, and three tests with them. The only
  production caller checks `is_surgical` first.
- **Precedence change.** `61c31f5d1` `client/auth.py`: `build_auth_headers`
  always sets `Authorization: Bearer` for non-`x-api-key` styles when a key
  exists; a static `Authorization` in `extra_headers` used to suppress that.
  No provider table entry does this today.
- **Case sensitivity.** `7fa9cc314` `mcp/routes.py`: route lookup no longer
  upper-cases the method, so a lowercase method misses the route. (Reported by a
  reviewer; I did not confirm that no caller sends lowercase.)
- **Lost tests.** Tests deleted with their code: `test_sticky_restart_matrix`,
  `test_stop_during_take_steps_down_one_level` (`ed733eb14`);
  `test_sync_timeout_warning_logs_once` (`730054d1c`);
  `test_transport_send_sets_auto_open_zero` (`61c31f5d1`; `auto_open=0` is still
  set at connection creation); the cleanup test above; about 23 grammar decision
  tests (`098063d1a`; drain-loop tests still cover dedup and out-of-order).
- **Bugfix comments deleted while the guard stayed**, against `AGENTS.md`:
  `56e1f5f19` (the BibliographicType int-range and name-mapping notes),
  `b2d9cce2c` (the `uno_same` identity-ladder history), `f4e4d9695` (the
  `refuse_tracked_insert_or_delete` rationale). A cheap check for next time:
  `git show <hash> | grep '^-.*Bugfix'` and confirm each hit has a replacement.
- **Unverified reviewer notes:** `192018692` `should_emit_silence_progress`
  dropped its `self._config.enabled` check (is the caller gating?);
  `7fa9cc314` removed `if services.events:` guards in `shutdown()`.

## What a path read finds (Nov 12): kinds 19 to 25

The s55 loop reads one file and the few functions it calls. The structural
review of `plugin/scripting` and `compute_service` on Nov 12 (HEAD
`a5a185b91`, 92 files, 42k lines, eight readers) asked a different question:
follow one `=PY()` request from the cell to the child and back, and say who
owns each fact along the way. That tree had been through every s55 pass and
four structural rounds, and its files mostly read well one at a time. The
review still listed 64 items, and most of them are kinds this document does
not have, because **each one is spread across files and no single file looks
wrong.** Examples are from the review reports (finding IDs like 02-1), not
from commits; most were being fixed as this was written.

The common cause is the same as in the thesis: each piece had a good local
reason. The difference is where you have to stand to see it. Kinds 1 to 18
show up when you ask of a line "what is this for?". Kinds 19 to 23 show up
only when you ask of a *value* "where was this decided, and how many times?"

### 19. One request, re-resolved at every hop

Every layer was written or hardened on its own, so every layer makes sure of
its inputs again. No hop is wrong. The path is what costs.

- A desktop run crosses seven shapes: two 16-keyword lists (14 names the
  same), a 17-field dataclass, an 11-key dict, a frame, a dict read with nine
  `.get` calls, 11 keywords. Adding a request field means editing five places
  (02-1).
- Its timeout is clamped twice through a `configured=` keyword that all three
  callers fill with the same call, defaulted again at two later hops,
  `None`-checked at a third, and stored on the executor three times before
  either run site overwrites it (01-2, 04-2).
- On compute, "shared or isolated" is one fact (is there a session id). It
  exists as a parsed field nobody reads, a value the server derives and
  passes, a parameter the pool overwrites on its third line, a payload key,
  and a re-test in the child (08-1).
- `session_id` is validated at the server with one error code and again in
  the pool with a second code that no HTTP caller can receive.
- The prepared run in `python_runner` is a dict in three shapes, and three
  readers pick the shape by `"helper_name" in prepared` (05-3).

This is kind 2 seen along a path. The per-file decision rule cannot settle
it, because whether a re-check is redundant depends on a caller in another
file. The signals are a keyword list that repeats another's names by more
than half, a `None` default on a value this process built three lines
earlier, and a parameter the callee assigns before it reads. The rule that
came out of the review: resolve a value at the first hop that can, and make
it required after that.

### 20. A superseded owner that tests and docs keep alive

A second mechanism took over the live path. The first one stayed, with its
name, its docstring and its tests. It is not dead code: a search finds
callers, and they pass.

- `payload_codec.child_unpack_data` is named as the child's data ingress in
  its module docstring and three docs. The sandbox calls
  `calc_range.materialize_inputs`, which has its own dispatch over the same
  envelopes. Production reaches one of the old function's six arms; tests
  call it 59 times (03-1). The review's own first unit put the wrong function
  in its owner map, which is the cost in practice: a reader checks two
  dispatchers and only one runs.
- `to_wire._walk` carries a JSON target through every arm (14 `target ==`
  tests) that no production code selects. The live JSON walker is
  `compute_service/json_egress.py`, with a different image shape. A reader of
  the desktop path concludes that NaN becomes `None` there. It does not
  (03-3).
- A script-header format with a regex, a dataclass, a registry field that is
  never set and six parsers. One domain still *writes* the header. Nothing in
  production reads one: 0 production calls, 20 test calls (06-1).

It differs from kind 13 (a seam made for a test) and kind 18 (a pure core
with no caller) because this code was the production path once and still
looks like it. The check is to count production callers per *arm*, not per
function.

### 21. One thing that never became an object

State for a single thing accumulates as loose pieces, because each fix added
the one piece it needed.

- One audio recording was a process, a monitor thread handle and a stop
  handoff. The chatbot kept them in three fields and passed them back as
  `monitor=None, handoff=None` keywords at six call sites. One site passed
  only the process, so that path reaped a recording without stopping its
  monitor (04-3; now one `Recording` object, #3350).
- One open editor window is `PersistentEditor`, `EditorSessionState`, an
  `EditorSession` wrapper whose only behaviour is `_finish`, a module global
  and an `RLock`, with one save token stored in two of them (05-1). The
  session table beside them never holds two entries.

It is the opposite of kind 7. There, one thing is mirrored into two
structures. Here it has no structure, and the caller is trusted to keep the
pieces together. The signal is two or more optional keywords that always
travel as a set.

### 22. Both ends of a pipe spell the vocabulary by hand

A message type, status word, envelope tag or error code that crosses a
process boundary appears as a string literal on each side, with no table and
no test that the two sides agree.

- Editor child → parent messages: nine literals at each end. The other
  direction has a table (`PARENT_TO_CHILD_TYPES`) and a parity test against
  the JavaScript (05-4).
- Envelope builders for image, multi-data and dataframe payloads are spelled
  out at ten production sites. The *detectors* have one owner (03-9).
- The recorder's five status words and its stop command (04).

The cousin of this is **accepting spellings nobody sends.** The text
analytics child accepts 18 helper names where the registry lists 8 (`ner`,
`keyphrases`, `polarity`, `stats` and six more). No production code sends the
extra ten (06-5). They look like kindness to a caller who might guess a name.
What they do is make the registry stop being the list of names, and neither
typecheck nor grep sees a string-literal mismatch between two processes. It
is kind 1 that the tools cannot follow.

### 23. Imports inside functions, by reflex

About 110 of the 193 in-function `plugin…` imports in these trees had no
written reason. Seventeen were of a module the same file already imports at
its top. A lazy import is right at a boundary (host, child, or bundle), for
weight, or for a named cycle. The reflex is to write one whenever a cycle
*might* exist, and afterwards nobody can tell the load-bearing ones from the
habit.

Where a reason was written it had sometimes expired:
`calc_functions_i_m.py` says an import stays inside the function because
`steyx` imports back, and `steyx` stopped doing that. `venv/duckdb_sql.py`
names a bundle case and "three wrappers" that do not exist. That is kind 11
in its smallest form: the guard is one line, and its stated reason is no
longer true.

Two side effects. Tests have to patch the name at its source module, so
hoisting an import changes where tests patch. And a census by grep
undercounts (see the method notes below).

### 24. A module's API under private names, and the checker told to be quiet

A helper is written as `_name` by default. The next file needs it and imports
the underscore name. The checker complains, and the complaint is what gets
fixed.

- `venv/calc_functions_util.py`: 20 underscore names imported by five sibling
  files (53 import entries), under a file-wide
  `# pyright: reportUnusedFunction=false`. Seven other underscore names in
  the file really are private. A reader cannot tell the two groups apart
  (07-1).
- Six helper files import shared functions under aliases
  (`error_result as _error_result`), so a grep for the owner's name misses 24
  import lines and about 250 call sites (06-6).
- `wsgi_server.py`: eight `cast("Any")`, three of them to reach framework
  privates.

By AST the two trees had 76 underscore names crossing a module boundary. The
smell to look for is a pragma, an alias or a cast at the exact place where
the tool was saying something true.

### 25. Residue of the cleanup itself: comments spliced by a sweep

The comment rule changed (present tense, no incident stories), and a sweep
(`939a30424`, #3243) rewrote bug-story comments to fit it. In about 60 places
the new sentence was put in beside half of the old one:

- "A bool / Bool keys are rejected; only int…"
- "must go through / parse_date_column handles numeric…"
- "was public,."
- "The executor had already stored the", followed by a new sentence.

Six of these are in `venv_worker.py` at the replay guard, the request-id
check and the unpack failure, which are the lines where a reader most needs
the reason. The diff looked right hunk by hunk, since every hunk removed a
"what was wrong" story. The damage only shows when a changed comment is read
as a whole sentence.

This is the first kind here that a cleanup pass *made*. Kind 12 says to
shorten the story and keep the rule; a mechanical pass that does this can cut
at the wrong word. Review a comment-only PR by reading each changed comment
in full. A regex for a stray full stop inside a sentence finds some of them
(18 in the calc function files); the rest were found by reading.

The review also found about ten doc and docstring statements that
contradicted the code at HEAD, including a handshake doc that says the child
sends no ready frame (it does) and two that name functions with no
definition. Those are ordinary drift, not the sweep's doing, but they belong
in the same comment-only PR.

### Refinements to existing kinds

- **Kind 13 is wider than names.** The seam counts in earlier rounds looked
  for test-only functions. This review also found about 20 test-only
  *parameters and modes*: `policy=None` on the session store (27 of 29 test
  calls, no production call), a `doc=None` reset mode that keeps a 34-line
  document finder alive, `execute(data=, timeout_sec=, decode_result=True)`
  on the compute pool (140 test calls and one bench against one production
  caller with the opposite convention). A grep for the function name finds
  production callers and stops. The check is to grep the *keyword* in
  production callers.
- **Production shaped for a test double** (kind 13). `editor_host.py`
  compares a `bool` dataclass field with `is True` at three sites so that "a
  MagicMock stand-in … does not look dirty". `server.py` keeps a branch
  because test doubles reject a trailing `None`. `formula_pool` duck-types a
  liveness check for hand-built stand-ins. The mock's behaviour has become
  part of the production contract.
- **Kind 9's trade has a price.** That section accepted a union return in
  exchange for no placeholder slots. `coerce.resolve_columns` returns a list
  or an error dict, and 15 of its 17 callers follow with
  `isinstance(…, dict)`. One helper file has 35 hand-written error-return
  ladders, half of them from this. `venv/symbolic.py` is the model: coercers
  raise one coded exception and one wrapper per helper turns it into the
  error result.
- **Kind 5, circular.** Seven picker rows name a "module:attr" string. It
  resolves to a facade function, which lazily imports the registry it was
  called from and asks it for the same domain's templates (06-2). A wrapper
  with one caller is a hop; this one is a round trip.
- **Kind 5, in a chain.** Seven names between "route matched" and "body
  parsed" in the compute server, four of them forwarders that each add one
  argument, plus a flag (`is_body_route`) that is `True` in every row of the
  table (08-4).
- **Kind 1, drifted.** The column-kind rule in the slow flatten path is
  written nine times beside its owner, and the copies already disagree with
  it for one input (03-2). A duplicate that has diverged is no longer a "no
  behaviour change" fold: choosing the owner changes that case. Diff the
  copies before folding.

### Method notes

- **Why the per-file loop cannot find these.** In kinds 19 to 22 the
  evidence is the *count across files*: shapes per request, production
  callers per arm, literals per end of a pipe. A single-file reviewer sees
  one clamp, one dispatcher, one literal, and each is fine.
- **What does find them.** Pick one concrete request and write down every
  hop with its shape and what it re-decides. Build an owner map (concept →
  the one function) before reading for findings, and treat "no owner" and
  "two owners" as the findings.
- **Counts by grep were wrong twice.** A single-line grep reported 0 private
  cross-imports where the AST found 23 names in Writer and 76 here, because
  the imports are multi-line or inside functions. The grep for bug-story
  comments found 4 where reading found about 130. Count with `ast` when the
  number decides anything.
- **These are not all removals.** Kinds 19, 21 and 22 are fixed by adding a
  record, an object or a table. Lines removed, the yield measure in the
  stopping rule, does not see them.
- **Still holds.** The single owners that earlier rounds made (`SessionKey`,
  one lease with one cancel scope, one frame codec, the three registries, one
  `run_sandboxed_code`) were all still single. What was left was between
  them.

## See also

- `wa-reviews/fable-structural-scripting/00-index.md` (outside this repo): the
  Nov 12 review behind kinds 19 to 25.
- [exception-policy.md](exception-policy.md): the centralized error handling this
  relies on.
- [uno-thread-safety.md](uno-thread-safety.md): why UNO disposal handling is not
  "excess."
- `git log --grep=s55 --oneline`: the commits discussed here.
