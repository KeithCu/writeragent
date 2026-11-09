# Collabora engine house rules for `=PY()` work

This is the one document a planner, coder, or reviewer must read in full before touching the Python compute code in the Collabora Online and Collabora Office trees (`engine/scaddins/source/pythoncompute/`, `engine/sc/`, `kit/`, `wsd/`). It holds the rules. It does not hold history or architecture rationale; those are in [numpy-jailsafe.md](numpy-jailsafe.md). Feature designs live in `docs/calc/` ([plot plan](../calc/collabora-engine-plot-plan.md), [spill plan](../calc/collabora-engine-spill-plan.md)).

The rules come from four sources: Michael Meeks's and Tomaž Vajngerl's Gerrit reviews of 8122 and 8123, the bug classes fixed during those series, and the twenty-odd review rounds of the plot-image branch in November 2026. Where a rule says "never", a reviewer has already asked for it once.

## 1. The shape, in one page

| Layer | Thread | Does | Does not |
|---|---|---|---|
| Compute service (Python, writeragent) | its own process, out of the kit | Runs user code in a sandboxed worker, dumps kit-safe JSON once (`status`, `result`, `error`, `stdout`, `images[]` with `format` and `data_b64`), forwards those bytes | Touch the document, run inside the kit jail |
| coolwsd `DocumentBroker` | broker thread (`ASSERT_CORRECT_THREAD`) | Feature flag, `http::Session` POST, 8 in flight per document plus FIFO, request and response capped by `max_payload_bytes`, result sent as a document-level message | Decode images, retry a POST, hold a strong broker reference across threads |
| kit `PythonComputeEmitter` | kit poll thread | Sets `COOL_PYTHON_COMPUTE`, calls `pythoncompute_take_requests()` without Solar, hands results to `pythoncompute_complete()` which takes Solar itself; spreadsheet documents only | Mutate the document, decode base64, insert graphics |
| AddIn `scaddins/pythoncompute` (`addin`, `anyjson`, `bridge`, `volatile`) | SolarMutex only | UNO `getPy` / `getPython`, `#BUSY!` volatile, param to volatile weak cache, JSON to `Any`, pass-through of image bytes | Link `svx` or `sc`, construct draw objects, keep a timer, hold a `std::mutex` |
| `sc` core | main thread; `ocExternal` is never threaded | Everything that touches cells, the draw page, or undo. Queues work during interpret and flushes it after the outermost interpret | Mutate the draw page while `IsInInterpreter()`, during XML import, or while undo or redo is still restoring objects |

Error policy, fixed. Do not add `FormulaError` values or a new Online error protocol.

| Cell shows | Kind | Meaning |
|---|---|---|
| `#BUSY!` | string | Request queued or in flight; never touches existing shapes |
| `#DISABLED` | string | Feature flag off |
| `#VALUE!` | `FormulaError::NoValue` | Deterministic failure: `status=error`, bad JSON, missing `result`, malformed images-only payload. Sticky until params change. Clears dependent state (for example plots) |
| `#N/A` | `FormulaError::NotAvailable` | No emitter, pending timeout, superseded. Not cached |
| transient `#VALUE!` | `TransientError` | Payload over cap, held bytes refused, 503-style replies. Retries after `kRetryAfterMs` (30 s). Keeps the last good state |

## 2. Invariants

Never regress these. Each has a test or a reviewer note behind it.

1. **Compute is out of the kit.** No NumPy, `split_grid`, or Pickle in C++. The kit never loads a user venv.
2. **Default off, and off means gone.** `security.python_compute.enable` defaults to `false`. `--disable-python-compute` removes the library, the CppUnit, and the IDL (`gb_Helper_optional,PYTHON_COMPUTE`). Tests and call sites that name the feature are under `HAVE_FEATURE_PYTHON_COMPUTE`. Build with the feature off before upload.
3. **One knob set.** `enable`, `url`, `api_key`, `timeout_secs`, `max_payload_bytes`. Every other limit is a named constant or is derived from `max_payload_bytes`. No new knobs, no new count or byte caps, no retry loops. A limit has one byte definition in both components (wsd trims before it counts, like `bridge.cxx`).
4. **SolarMutex 1+ε.** Shared AddIn state is Solar-only. `take_requests` asserts Solar is not held; `complete` takes it. Snapshot, release, then `finish()`.
5. **Callbacks only queue work.** The AddIn wakeup does `addCallback`. Interpreter hooks queue; the flush runs at the existing `ProcessPendingMatrixResizes` sites after the outermost interpret. Do not move or rename that drain, and do not add idle or posted flushes.
6. **Never cache a failure the next recalc should retry.** Finished success and service errors are sticky; no-emitter, pending timeout, and transient errors are not.
7. **Nothing escapes an `extern "C"`, UNO, or poll-callback boundary.** `catch (...)` at the edge; UNO exceptions do not derive from `std::exception`.
8. **Never lock a broker `weak_ptr` off the broker thread.** Capture `weak_ptr<SocketPoll>`, `addCallback`, lock inside.
9. **Transient keeps, deterministic clears.** A transient error keeps the last good cell state and shapes; a deterministic error removes them. `#BUSY!` touches nothing.
10. **Typed dumb JSON, hand parser, strict UTF-8.** `anyjson.cxx` owns the Core side of the wire. No Poco in Core, no merge into jsuno. `null`, `[]`, `[[]]` are an empty cell; lists become `ScMatrix`.
11. **Pass bytes through.** The service is trusted. Image bytes cross the AddIn without decode or sniff; `sc` imports them once. A graphic never crosses the AddIn/sc UNO boundary.
12. **Cost scales with the pending work, not the document.** One page walk per sheet per flush. Skip when nothing changed. No `O(cells × objects)` paths on a hot path.

## 3. House style

These are the review notes that came back on 8122 and 8123. Apply them before upload so they do not come back again.

- New files use `engine/TEMPLATE.SOURCECODE.HEADER` (MPL only, `fill-column: 100`). Do not rewrite headers of existing files.
- C++ namespace `co::pythoncompute`. UNO identity `org.collaboraoffice.sheet.addin.*`. UNO types are `cpo::uno`, not `com::sun::star`. Source files are `addin`, `anyjson`, `bridge`, `volatile`; no `pythoncompute_` prefix on filenames. C ABI symbols keep the prefix.
- Use the helper that already exists: `o3tl::trim`, `tools::JsonWriter::finishAndGetAsStdString()`, `comphelper::containerToSequence`, `comphelper::Base64`, `GraphicFilter`, the `ScDrawLayer` anchor helpers, `ScDrawObjData::Type`. Before writing a helper, grep for one.
- Smallest correct diff. No drive-by refactors, forwarders, re-exports, renames of shared names, or header tidying for its own sake. If a line is unnecessary, remove it.
- Comments are present-tense invariants. No "used to", "Bugfix:", war stories, round numbers, or model names in code, comments, or test names.
- Feature guards go on call sites in the `.cxx` that includes `config_features.h`. Do not pull `config_features.h` into `document.hxx` or another widely included header. A small generic helper may stay unguarded only if it is correct for every object and build and is listed in the commit message.
- A fix to shared Calc behavior found while working on a feature is a separate commit with its own generic test, not a hunk inside the feature commit.
- Agents add no `Signed-off-by`, `Co-Authored-By`, or `Change-Id`. Only Keith or the supervisor commits. Cursor's git wrapper appends a `Co-authored-by: Cursor` trailer to `git commit` and `--amend`; use the `commit-tree` recipe in section 9 instead.
- Leave untracked local files alone: `coolkitconfig.xcu`, `diff.txt`, `diff2.txt`, `gerrit-commit-tree.sh`. Never stage them. Never `pkill soffice` or switch the laptop checkout's branch.
- Build files take upstream's current shape. Forkit objects go in `gbuild/StaticLibrary_forkit.mk`; unit tests link `libtesthelpers.la $(CPPUNIT_LIBS)`. After a rebase, run `git range-diff` and confirm the change is mergeable.
- English-only help and cell strings are accepted for now; say so in the cover letter rather than translating two strings in isolation.
- Delete dead branches. Do not look up an id you just minted with a monotonic counter.

## 4. Build and test inventory

| Target | What it reaches | Source |
|---|---|---|
| `make -C engine CppunitTest_scaddins_pythoncompute` | JSON to `Any`, envelope, images array, cache identity, limits, volatile `finish` | `engine/scaddins/qa/pythoncompute.cxx` |
| `make -C engine CppunitTest_sc_ucalc` | `ScDocument` without a view; links `pythoncompute` and calls the `bridge.h` hooks directly (no `dlsym`); `CalcAll`, `ProcessPendingMatrixResizes`, undo manager, `MoveBlock`, sort, sheet operations | `engine/sc/qa/unit/ucalc.cxx` (plot tests belong in their own file with shared fixtures) |
| `make -C engine CppunitTest_sc_anchor_test` | Anchors, `saveAndReload` (ODS), live view where the test opens one | `engine/sc/qa/unit/anchor.cxx` |
| `make -C engine CppunitTest_sc_tiledrendering` | Live `ScTabViewShell` and `ScDrawView`, `.uno:` dispatch, `paintTile`, LOK invalidation callbacks | `engine/sc/qa/unit/tiledrendering/tiledrendering.cxx` |
| `scripts/run-wsdunit unit-python-compute` | kit to wsd to HTTP wire, caps, FIFO, disabled, close mid-flight. Asserts cell text only; it cannot see shapes | `test/UnitPythonCompute.cpp` |
| `--disable-python-compute` configure | The feature-off build. Compile at least the guarded files | |

`saveAndReload` already exists in `sc/qa/unit` (`anchor.cxx`, `cond_format.cxx`, `scshapetest.cxx`, `subsequent_export_test*.cxx`). A view-level test already exists in tiledrendering. Do not say a persistence or view test needs new infrastructure.

Run `make -sr <target>` from `engine/`, in the background, and wait on the make PID. Do not `pgrep` for the test name; it matches your own argv. Do not build in a checkout that reviewers are reading.

A test counts only if it fails when the fix is reverted. Run that mutation check before reporting a fix as pinned.

## 5. Planning a feature

The plan is the spec that implementation and review are measured against. It must contain, in this order:

1. **Base and scope.** The base commit recorded as a sha, the branch, what is out of scope (shared sessions, new wire formats, UI).
2. **Decisions.** One sentence each, with the `file:line` evidence, for every choice the brief left open. Where two mechanisms were possible, name the rejected one and why.
3. **The one mechanism.** Identity, ownership, or lifetime is decided once. Prefer an existing Calc concept (an `ScDrawObjData::Type`, a listener, a reference-update hook) over a name, a position, or a side table. A mechanism that Calc rewrites on ordinary paths (shape names, main-anchor cells, positions) is not an identity.
4. **Lifecycle matrix.** Every event in the template below, with `handle`, `accept`, or `out of scope`, a one-line why, and the test binary for each `handle`. The matrix is frozen after round 1; new cells go to a follow-up list.
5. **Hard tests named for round 1.** At least: a persistence round trip, a view-level path, a structural operation, and undo of the most common edit. These are written with the first implementation, not debated later.
6. **Pre-existing bugs.** Anything found in shared code during research, each as a separate commit.
7. **Landing bar.** Section 7's default unless the plan says otherwise.
8. **Acceptance.** What the supervisor's build and narrow tests verify, and what only a local binary verifies.

The planner reads this document, the brief, the feature's design doc, and the code. The planner does not read NumPy documentation, history sections, or prior review logs.

### 5.1 Lifecycle matrix template

Use these rows for any feature that owns draw objects or hooks the cell store. Delete rows that cannot apply; do not skip rows because they look unlikely. The plot-image branch found one new row per review round for ten rounds; every one of them is on this list.

| Group | Events |
|---|---|
| Cell store | recalc; lazy or async interpret after `TrackFormulas`; overwrite with a value; overwrite with a non-feature formula; overwrite with another feature formula; Delete Contents and the Delete key; formula-to-value; bulk `SetValues`, `SetFormulaCells`, `TransferCellValuesTo`, `CopyCellValuesFrom`; defined-name redefine; `IF`, `CHOOSE`, `IFERROR` branch not reached; error-argument short-circuit; two calls in one formula; function wizard and `ScSimpleFormulaCalculator`; conditional format and validation formulas; `LET` or `LAMBDA` in the cell's own formula; a `LAMBDA` value held in another cell |
| Sheet structure | insert and delete row or column; insert, delete, move, copy sheet; sort with and without "include images"; hide, filter, unhide row or column; merge; right-to-left sheet |
| Draw layer | user move and resize; Anchor menu To Cell, To Cell resize-with-cell, To Page; draw-mode copy and paste; Ctrl+drag copy; cross-sheet drag move; cell copy and paste; cut and paste; drag-move of a block; name-dialog rename; `MakeNameUnique` on paste; group and ungroup |
| Persistence and undo | ODS save and reload; XLSX save and reload; undo and redo of every `handle` row above; clipboard and undo documents; flush during import or in-progress undo |
| Online | lazy interpret during tile paint; interpret during export; result arriving for an off-screen cell; several views and tile invalidation; manual recalc mode |

## 6. Implementing and fixing

- Build and run the named test binaries yourself before reporting. The supervisor reruns them and does not trust self-reports.
- **Test first for every reported bug.** Write the test the review asked for, run it, and only then change production code. If the test passes unchanged, report `NO-REPRO` with the trace and do not add code. On the plot-image branch, three of eleven approved fixes in one round were redundant, and test-first was the only step that caught them.
- **Mutation check.** After the fix, revert it, confirm the test fails, restore. Report tests that did not fail under mutation.
- A fix sketch from a review is a hint. Reproduce, then take the smallest correct change, which may differ from the sketch.
- When a fix adds a hook at one site, grep for every sibling site of that class in the same round and list each as handled or parked in `STATUS.md`. Row delete, formula-to-value, `SetValues`, and name recompilation are one class; they surfaced in four separate rounds because nobody did this.
- Put deviations in `STATUS.md`: what was skipped, why, which test could not be reached and by which binary.
- Do not edit the settled-decisions file or the matrix from a fix round. Propose the sentence in `STATUS.md`.
- End with `STATUS: DONE` or `STATUS: STUCK <reason>`.

## 7. Reviewing

Severity, defined once so the verdict means something:

| Severity | Means | Needs |
|---|---|---|
| blocker | Data loss, crash or use-after-free, unbounded growth, silent deletion without undo, wrong-cell or wrong-sheet mutation, build or link failure in a supported configuration, a test that cannot pass | A `file:line` trace the fixer can walk, or test code |
| high | A `handle` cell of the matrix is not met, or an invariant in section 2 is violated | A trace |
| medium | A gap outside the matrix, a `handle` cell without a test, or cost that grows with document size on a hot path | A sentence |
| nit | Style, naming, comments, duplication | Optional |

**Landing bar.** Ready to land means no blocker and no high. Mediums and nits are listed and go to the next commit or a follow-up; they do not block, and a reviewer is not asked whether they should. The reviewer ends with `Blocking findings: N`. The human decides landing.

Rules for the reviewer:

- Read the settled decisions and the matrix before the diff. Do not reopen an `accept` or `out of scope` cell without new evidence, and say what the evidence is (`file:line`, a trace, a failing test).
- Round 1 reviews the whole diff against the matrix. Every later round reviews the delta since the last reviewed commit plus closure of the prior findings. Do not re-read the squash for new nits.
- At most three change proposals per reviewer per round, ranked. Everything else goes in an appendix without severity.
- A fix sketch names the smallest change and the existing helper. A test proposal is test code using existing helpers and names the binary. "Add a test for X" without code is a nit.
- A bug that predates the patch is labelled `pre-existing` and proposed as a separate commit. It is not a finding against the patch.
- Do not build in a checkout other reviewers are reading. Hand the test code to the fixer, who runs it first (section 6).
- Output in two parts: findings with severity and `file:line`; then triage with root causes marked `change` or `accept`, and DO, DO NOT, PARK lists. End with `Blocking findings: N`.

## 8. Round budget

| Round | Work | Exit |
|---|---|---|
| 0 | Plan with matrix, decisions, hard tests named | Plan has every section in 5 |
| 1 | Implement with the hard tests; one parallel regional review against the matrix; test-first fixes | Every `handle` cell has code and a test |
| 2 | Delta review on the landing bar; test-first fixes | No blocker, no high |
| 3 | One reviewer confirms the blocker list is empty | Land |

At the limit, land as experimental with the accepted limitations in the commit message, or split the commit. "Not ready" with no finish line is not an outcome. The plot-image branch ran eleven single rounds and four parallel rounds; the last two parallel rounds found no blocker and no high and still said "not ready" five times each, because no bar had been set.

## 9. Commits and Gerrit

- Series shape: shared Calc fixes first, each with a generic test; then the AddIn change; then the core mechanism; then lifecycle hooks and their tests. Each commit builds and passes on its own.
- The commit message states what the patch does, the mechanism in one paragraph, every unguarded change to shared behavior, the tests run, and the accepted limitations. `Change-Id` and `Signed-off-by: Keith Curtis` only.
- One push approval covers one push. No Gerrit comments or reviewer adds without approval. Diff the patch-set file list against the previous one before pushing.
- Cover letter: issue #16010, experimental and default off, non-goals, residual risk.

Commit without Cursor's trailer:

```bash
# index already has what you want (git add / git add -u)
MSG_FILE=$(mktemp)
# write the intended message into $MSG_FILE: body, Change-Id, Signed-off-by; nothing else
export GIT_AUTHOR_NAME='Keith Curtis' GIT_AUTHOR_EMAIL='keithcu@gmail.com'
export GIT_AUTHOR_DATE="$(date -R)"          # or: git log -1 --format=%aD  when replacing HEAD
export GIT_COMMITTER_NAME='Keith Curtis' GIT_COMMITTER_EMAIL='keithcu@gmail.com'
TREE=$(git write-tree)
PARENT=$(git rev-parse HEAD)                 # new commit on top of HEAD
# PARENT=$(git rev-parse HEAD^)              # instead: replace HEAD (new Gerrit patch set)
NEW=$(git commit-tree "$TREE" -p "$PARENT" -F "$MSG_FILE")
git reset --soft "$NEW"
rm -f "$MSG_FILE"
git log -1 --format='%B' | grep -iE 'Co-authored-by|Cursor' || echo clean
git push cogerrit HEAD:refs/for/main
```

## 10. Do not

- Add a knob, a cap, a count limit, or a retry loop.
- Add a `std::mutex` to the AddIn, or a timer.
- Put NumPy, `split_grid`, or Pickle in C++, or a user venv in the kit.
- Insert graphics from the kit (`.uno:InsertGraphic` lands on a view cursor; `=PY()` is async and multi-view).
- Add `FormulaError` values or a new Online error protocol.
- Merge `anyjson` into jsuno, or use Poco in Core.
- Use a shape name, a position, or a main-anchor cell as identity.
- Mutate the draw page during interpret, XML import, or in-progress undo or redo. Move the Interpret-tail drain. Add idle or posted flushes or skip-while-saving.
- Pull `config_features.h` into `document.hxx`. Guard a generic fix behind the feature flag.
- Implement a review's fix sketch before reproducing the bug.
- Forbid a test in a fix prompt. If a test is expensive, the plan says so with a reason.
- Grow the settled-decisions file by appending reviewer prose. Add one rule row with a one-line why.
- Commit, amend, push, stash, rebase, or switch branches from an agent session.
