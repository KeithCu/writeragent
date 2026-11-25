# Collabora engine house rules for `=PY()` work

This is the one document a planner, coder, or reviewer must read in full before touching the Python compute code in the Collabora Online and Collabora Office trees (`engine/scaddins/source/pythoncompute/`, `engine/sc/`, `kit/`, `wsd/`, `net/`). It holds the rules. It does not hold history or architecture rationale; those are in [numpy-jailsafe.md](numpy-jailsafe.md). Feature designs live in `docs/calc/` ([plot plan](../calc/collabora-engine-plot-plan.md), [spill plan](../calc/collabora-engine-spill-plan.md)).

The rules come from five sources: Michael Meeks's and Tomaž Vajngerl's Gerrit reviews of 8122 and 8123, the bug classes fixed during those series, the twenty-odd review rounds of the plot-image branch in November 2026, and the eight-change stack of 21–22 November 2026 (19188 to 21287), which took 77 fix jobs and up to 16 review rounds per change. Where a rule says "never", a reviewer has already asked for it once.

Every review prompt and every coding prompt includes this document. A prompt that asks for "a thorough code review" without it gets findings these rules already settle.

## 1. The shape, in one page

| Layer | Thread | Does | Does not |
|---|---|---|---|
| Compute service (Python, writeragent) | its own process, out of the kit | Runs user code in a sandboxed worker, dumps kit-safe JSON once (`status`, `result`, `error`, `stdout`, `images[]` with `format` and `data_b64`), forwards those bytes | Touch the document, run inside the kit jail |
| coolwsd `DocumentBroker` | broker thread (`ASSERT_CORRECT_THREAD`) | Feature flag, `http::Session` POST, 8 in flight per document plus FIFO, shared and reset: one at a time per document, request and response capped by `max_payload_bytes`, result sent as a document-level message | Decode images, retry a POST, hold a strong broker reference across threads |
| kit `PythonComputeEmitter` | kit poll thread | Sets `COOL_PYTHON_COMPUTE`, calls `pythoncompute_take_requests()` without Solar, hands results to `pythoncompute_complete()` which takes Solar itself; spreadsheet documents only | Mutate the document, decode base64, insert graphics |
| AddIn `scaddins/pythoncompute` (`addin`, `anyjson`, `bridge`, `volatile`) | SolarMutex only | UNO `getPy` / `getPython`, `#BUSY!` volatile, param to volatile weak cache, JSON to `Any`, pass-through of image bytes | Link `svx` or `sc`, construct draw objects, keep a timer, hold a `std::mutex` |
| `sc` core | main thread; `ocExternal` is never threaded | Everything that touches cells, the draw page, or undo. Queues work during interpret and flushes it after the outermost interpret | Mutate the draw page while `IsInInterpreter()`, during XML import, or while undo or redo is still restoring objects |

A change that alters what a layer does (a new wire part, a queue limit, a session mode, a new setting) updates this table and the invariants in the same commit that changes the code, so the next planner does not start from a stale shape.

Error policy, fixed. Do not add `FormulaError` values or a new Online error protocol.

| Cell shows | Kind | Meaning |
|---|---|---|
| `#BUSY!` | string, display only | Request queued or in flight; never touches existing shapes. Code learns "pending" from the pending flag on the result, never from this text |
| `#DISABLED` | string | Feature flag off |
| `#VALUE!` | `FormulaError::NoValue` | Deterministic failure: `status=error`, bad JSON, missing `result`, malformed images-only payload. Sticky until params change. Clears dependent state (for example plots) |
| `#N/A` | `FormulaError::NotAvailable` | No emitter, superseded. Not cached |
| transient `#VALUE!` | `TransientError` | Payload over cap, held bytes refused, 503-style replies. Retries after `kRetryAfterMs` (30 s). Keeps the last good state |

## 2. Invariants

Never regress these. Each has a test or a reviewer note behind it.

1. **Compute is out of the kit.** No NumPy, `split_grid`, or Pickle in C++. The kit never loads a user venv.
2. **Off at run time by default, and a disabled build means gone.** `security.python_compute.enable` defaults to `false`. `--disable-python-compute` removes the library, the CppUnit, and the IDL (`gb_Helper_optional,PYTHON_COMPUTE`). Tests and call sites that name the feature are under `HAVE_FEATURE_PYTHON_COMPUTE`.
3. **One knob set, and every input has a bound.** The settings are `enable`, `url`, `api_key`, `timeout_secs`, `max_payload_bytes`, and `session`. No new settings and no retry loops. Every new string or array that comes from the service or from the document has a maximum, written as one named constant and decided in the plan (section 5, item 6); "unbounded" is never the default. A limit has one byte definition in both components (wsd trims before it counts, like `bridge.cxx`).
4. **SolarMutex 1+ε.** Shared AddIn state is Solar-only. `take_requests` asserts Solar is not held; `complete` takes it. Snapshot, release, then `finish()`.
5. **Callbacks only queue work.** The AddIn wakeup does `addCallback`. Interpreter hooks queue; the flush runs at the existing `ProcessPendingMatrixResizes` sites after the outermost interpret. Do not move or rename that drain. A new `Idle`, `Timer`, or posted event anywhere is a plan decision that names why the existing drain cannot serve; a fix round never adds one from a review sketch.
6. **Never cache a failure the next recalc should retry.** Finished success and service errors are sticky; no-emitter, pending timeout, and transient errors are not.
7. **Nothing escapes an `extern "C"`, UNO, or poll-callback boundary.** `catch (...)` at the edge; UNO exceptions do not derive from `std::exception`.
8. **Never lock a broker `weak_ptr` off the broker thread.** Either lock inside an `http::Session` handler guaranteed to run on the broker's `SocketPoll`, or capture `weak_ptr<SocketPoll>` and `addCallback` to lock on the broker thread; never lock a broker `weak_ptr` in a destructor or arbitrary-thread callback.
9. **Transient keeps, deterministic clears.** A transient error keeps the last good cell state and shapes; a deterministic error removes them. `#BUSY!` touches nothing. With two `=PY()` calls in one formula, one call's empty or pending result never clears what the other call owns.
10. **Shared `comphelper/json.hxx` JSON parsing, strict UTF-8.** `anyjson.cxx` owns the Core side of the wire and should reuse `comphelper/json.hxx` (`parseJsonToInferredAny`, `findJsonValueEnd`, `splitJsonArray`, `splitJsonObject`) and `tools::JsonWriter` rather than carrying a duplicate hand parser. No Poco in Core, no merge into QuickJS/jsuno. `null`, `[]`, `[[]]` are an empty cell; lists become `ScMatrix`. Build JSON with a writer (`tools::JsonWriter`, `Poco::JSON::Object`), never by string concatenation. When a parser is replaced or moved, port the old parser's accept and reject tests first; the swap to the shared splitters lost whitespace handling and strictness twice.
11. **Pass bytes through, bound them before decode.** Image bytes cross the AddIn without decode or sniff; `sc` imports them once. A graphic never crosses the AddIn/sc UNO boundary. The service is trusted to run code, but its bytes are still bounded: `sc` applies the byte and count caps before it splits or allocates, decodes with the same format it detected (not `GRFILTER_FORMAT_DONTKNOW` after a separate sniff), and checks the decoded size again.
12. **Cost scales with the pending work, not the document.** One page walk per sheet per flush. Skip when nothing changed. No `O(cells × objects)` paths on a hot path. In a paint or per-cell path the cheapest test (`HasPythonPlots()`, an empty-map check) comes first.
13. **State travels out of band.** Pending, cleared, disabled, and failed are flags or enum values on the result interface. Code never compares a display string to decide state, and nothing that Python or a cache key sees is localized text or can collide with what a user might type. A literal shared by two modules has one constant, or a test that asserts both sides match.
14. **Every exit of an async start replies, frees, and drains.** Each `return`, `catch`, stop, and never-ran-handler path of a `startPythonCompute*`-style function does three things: replies to the kit, erases its entry from the in-flight set, and calls `finishPythonCompute` so the queue moves. A missed exit is a permanent `#BUSY!`. Use a scope guard, and force each exit in a `UnitPythonCompute` scenario.
15. **Secrets are redacted at the sink.** A capability (session id, API key, bearer token) does not go in a URL query unless the plan says so. Redaction lives in the dump and log functions themselves (`Request::dumpState`, `Response::dumpState`, `StreamSocket::dumpState` for both buffers, `Session::create` errors, hex dumps, `ex.what()` from `Poco::URI`), not at each caller. The test asserts the secret is absent from the dump output, not only that a helper redacts.
16. **Destructors and side tables do not go through the address.** Never look up the cell store, table, or column from `~ScFormulaCell` or another destructor; the column can already be gone on document close and `DeleteTabs`. A side table keyed by `ScAddress` either follows every reference update (`URM_INSDEL`, `URM_MOVE`, `CopyTab`, `DeleteTab`, undo clone) or is keyed by the object.

## 3. House style

These are the review notes that came back on 8122 and 8123. Apply them before upload so they do not come back again.

- New files use `engine/TEMPLATE.SOURCECODE.HEADER` (MPL only, `fill-column: 100`). Do not rewrite headers of existing files.
- C++ namespace `co::pythoncompute`. UNO identity `org.collaboraoffice.sheet.addin.*`. UNO types are `cpo::uno`, not `com::sun::star`. Source files are `addin`, `anyjson`, `bridge`, `volatile`; no `pythoncompute_` prefix on filenames. C ABI symbols keep the prefix.
- Use the helper that already exists: `o3tl::trim`, `tools::JsonWriter::finishAndGetAsStdString()`, `comphelper::containerToSequence`, `comphelper::Base64`, `GraphicFilter`, the `ScDrawLayer` anchor helpers, `ScDrawObjData::Type`. Before writing a helper, grep for one.
- Smallest correct diff. No drive-by refactors, forwarders, re-exports, renames of shared names, or header tidying for its own sake. If a line is unnecessary, remove it.
- Comments are present-tense invariants. No "used to", "Bugfix:", war stories, round numbers, or model names in code, comments, or test names. No sentence in a comment, README, or commit message describes an earlier patch set.
- Feature guards go on call sites in the `.cxx` that includes `config_features.h`. Do not pull `config_features.h` into `document.hxx` or another widely included header. A small generic helper may stay unguarded only if it is correct for every object and build.
- Keep a whole feature together in one commit (or one intentional feature series); do not split out small shared-code helpers or refactors into separate commits unless a Collabora reviewer asks for it.
- A bug that already exists in shared code and that the feature does not need fixed in order to work (an upstream undo slip, existing `net/` logging) goes into its own change and does not block the feature. Folding existing `net/` log redaction into the shared-session commit cost seven consecutive review rounds.
- Each unguarded change to shared behavior gets one test that does not use `=PY()`, in a binary that is not feature-gated (`sc_ucalc_spilled_range` style).
- Agents add no `Signed-off-by`, `Co-Authored-By`, or `Change-Id`. Only Keith or the supervisor commits. Cursor's git wrapper appends a `Co-authored-by: Cursor` trailer to `git commit` and `--amend`; use the `commit-tree` recipe in section 9 instead.
- Leave alone every file that was untracked when you started (`coolkitconfig.xcu`, `diff.txt`, `diff2.txt`, `gerrit-commit-tree.sh`, and the like). Never stage them. Never `pkill soffice` or switch the laptop checkout's branch.
- Build files take upstream's current shape. Forkit objects go in `gbuild/StaticLibrary_forkit.mk`; unit tests link `libtesthelpers.la $(CPPUNIT_LIBS)`. After a rebase, run `git range-diff` and confirm the change is mergeable.
- English-only help and cell strings are accepted for now; say so in the cover letter rather than translating two strings in isolation. This covers text a person reads. Text handed to Python or used in a cache key is a stable ASCII token (invariant 13).
- Delete dead branches. Do not look up an id you just minted with a monotonic counter.

## 4. Build and test inventory

| Target | What it reaches | Source |
|---|---|---|
| `make -C engine CppunitTest_scaddins_pythoncompute` | JSON to `Any`, envelope, images array, cache identity, limits, volatile `finish` | `engine/scaddins/qa/pythoncompute.cxx` |
| `make -C engine CppunitTest_comphelper_json` | The shared JSON splitters and inferred-`Any` parser; every strictness case belongs here | `engine/comphelper/qa/unit/json.cxx` |
| `make -C engine CppunitTest_sc_ucalc` | `ScDocument` without a view; `CalcAll`, `ProcessPendingMatrixResizes`, undo manager, `MoveBlock`, sort, sheet operations | `engine/sc/qa/unit/ucalc.cxx` |
| `make -C engine CppunitTest_sc_ucalc_pythoncompute` | The `=PY()` tests on a view-less `ScDocument`: links `pythoncompute` and calls the `bridge.h` hooks | `engine/sc/CppunitTest_sc_ucalc_pythoncompute.mk` |
| `make -C engine CppunitTest_sc_ucalc_spilled_range` | Dynamic-array spill without `=PY()`; the home for non-PY tests of shared spill changes | `engine/sc/CppunitTest_sc_ucalc_spilled_range.mk` |
| `make -C engine CppunitTest_sc_anchor_test` | Anchors, `saveAndReload` (ODS), live view where the test opens one | `engine/sc/qa/unit/anchor.cxx` |
| `make -C engine CppunitTest_sc_tiledrendering` | Live `ScTabViewShell` and `ScDrawView`, `.uno:` dispatch, `paintTile`, LOK invalidation callbacks | `engine/sc/qa/unit/tiledrendering/tiledrendering.cxx` |
| `unithttplib` | `http::Request`, `Session`, multipart body, dump and log redaction | `test/HttpRequestTests.cpp` |
| `scripts/run-wsdunit unit-python-compute` | kit to wsd to HTTP wire, caps, FIFO, disabled, close mid-flight, every async-start exit. Asserts cell text only; it cannot see shapes | `test/UnitPythonCompute.cpp` |

`saveAndReload` already exists in `sc/qa/unit` (`anchor.cxx`, `cond_format.cxx`, `scshapetest.cxx`, `subsequent_export_test*.cxx`). A view-level test already exists in tiledrendering. Do not say a persistence or view test needs new infrastructure.

Nothing is built or run locally. Jenkins builds and runs these targets on each uploaded patch set.

### 4.1 Jenkins is the build

**No local builds.** Code is compiled and tests run only by Jenkins on an uploaded patch set. Reviews may read code before Jenkins has finished, but a test does not count as passing until Jenkins has run it. Because nothing compiles before upload, read new code as a compiler would: includes, declarations, types, `->` versus `.`, and whether each new test's setup is accepted by the code it calls. On the 21–22 November stack, reviewers found a missing include, a `->` typo, a test whose setup the code refuses, and a crash in `~ScFormulaCell`, each one or more rounds late.

- A status file that says a check was "reasoned" or done "by inspection" is not `DONE`; it says `UNBUILT` until Jenkins has run it.
- After a restack, check the restacked change against the new base's declarations by reading them; a patch that applies cleanly can still fail to compile against the new base.
- When you change behavior, grep the existing tests for the old behavior before you finish; an unchanged test that asserts the opposite is a build break waiting for Jenkins.

### 4.2 A test counts only if it can fail

A test counts only if it fails when the fix is reverted. Since tests do not run locally, the status names the branch the test reaches and why reverting the fix changes the result; Jenkins then runs it. Each of these let a real bug through:

- **Use an input that tells the two outcomes apart.** Non-ASCII and escaped text for an encoding claim, not `result=1`. An error other than `#DIV/0!` for "the error is kept", since division produces `#DIV/0!` by itself. A function whose result differs between the two paths, not `N`/`TYPE` where `IsErrFunc` makes both agree.
- **Exact counts.** `==`, never `>= 1`. A loose count hid a double recalc.
- **Reach the path you name.** A matrix-argument test uses `InsertMatrixFormula`; `SetString` does not give array context and never reaches `svMatrix`. A threading test contains a `=PY()` call.
- **No wall-clock windows** as proof that something did not happen, and no assert on state that is in memory by construction (a flag checked after `saveAndReload` that was never written to the file).
- **One negative control per gate**: a non-PY add-in through the same path, a non-plot shape, a document without the property.
- **A new bound is tested at 0, 1, and limit + 1.**
- **A fix to a predicate or heuristic gets one test row per input shape** (each byte alignment, each RPN sibling shape, each operator). Fixes that handled only the reported shape regressed the neighbouring one three times.

## 5. Planning a feature

The plan is the spec that implementation and review are measured against. It must contain, in this order:

1. **Base and scope.** The base commit recorded as a sha, the branch, and what is out of scope for this change.
2. **Decisions.** One sentence each, with the `file:line` evidence, for every choice the brief left open. Where two mechanisms were possible, name the rejected one and why.
3. **Questions for Keith, asked before coding.** Every behavior, Excel-compatibility, policy, or interface question the planner cannot settle from the code, each with options and a recommendation. Coding does not start on a mechanism with an open question. Section 5.2 lists the questions that come up on every feature.
4. **The one mechanism.** Identity, ownership, or lifetime is decided once. Prefer an existing Calc concept (an `ScDrawObjData::Type`, a listener, a reference-update hook) over a name, a position, or a side table. A mechanism that Calc rewrites on ordinary paths (shape names, main-anchor cells, positions) is not an identity.
5. **Sibling-site table.** For each new flag, guard, hook, or branch, every reader and writer of the state it protects, one line per site, each `handled` or `parked`. Section 5.3 names the sets to grep.
6. **Bounds table.** For each new string or array from the service or the document: its maximum, where it is stored, and how often it is re-sent, re-hashed, or copied (per cell, per paint, per state frame).
7. **Skew table**, for any change to request or response JSON, a URL, a `dlsym` symbol, or IDL: new kit with old service, old kit with new service, new `sc` with old AddIn, each with the resulting cell text. New `dlsym` symbols are optional at load. Service claims are checked by running the exact emitted JSON through WriterAgent (`calc_range.materialize_inputs` for inputs), not assumed. The README Wire section names the WriterAgent version required.
8. **Lifecycle matrix.** Every event in the template below, with `handle`, `accept`, or `out of scope`, a one-line why, and the test binary for each `handle`. An `accept` on a formula-composition row (`IFERROR`, two calls, `LET`/`LAMBDA`, function wizard) also states what the user sees. There is one matrix per feature, kept with the change's decided file (`collabora-decided/<change>.md`); it is frozen after round 1, fix jobs do not write their own, and new cells go to a follow-up list.
9. **Hard tests named for round 1.** At least: a persistence round trip, a view-level path, a structural operation, and undo of the most common edit. These are written with the first implementation, not debated later.
10. **Pre-existing bugs.** Anything found in shared code during research, each as its own change (section 3).
11. **Landing bar.** Section 7's default unless the plan says otherwise.
12. **Acceptance.** Which Jenkins targets (section 4.1) cover the change's tests.

The planner reads this document, the brief, the feature's design doc, and the code. The planner does not read history sections or prior review logs.

### 5.1 Lifecycle matrix template

Use these rows for any feature. Delete rows that cannot apply; do not skip rows because they look unlikely. The plot-image branch found one new row per review round for ten rounds, and the November stack found the rows marked † the same way.

| Group | Events |
|---|---|
| Cell store | recalc; lazy or async interpret after `TrackFormulas`; overwrite with a value; overwrite with a non-feature formula; overwrite with another feature formula; Delete Contents and the Delete key; formula-to-value; bulk `SetValues`, `SetFormulaCells`, `TransferCellValuesTo`, `CopyCellValuesFrom`; defined-name redefine; `IF`, `CHOOSE`, `IFERROR` branch not reached; error-argument short-circuit; two identical calls in one formula; two distinct calls in one formula, also in a shared session †; a formula that refers to its own cell †; function wizard and `ScSimpleFormulaCalculator`; conditional format and validation formulas; `LET` or `LAMBDA` in the cell's own formula; a `LAMBDA` value held in another cell; a threaded formula group that reaches `=PY()` through a `LAMBDA` or a name †; a cell clone (does it copy each transient bit?) †; each early return of `InterpretTail` (where is each transient bit cleared?) †; a compile-error formula † |
| Sheet structure | insert and delete row or column; insert, delete, move, copy sheet, and `DeleteTabs` of several †; cut-and-paste and drag move (`URM_MOVE`) †; sort with and without "include images"; hide, filter, unhide row or column; merge, including a merged range whose last row is hidden †; right-to-left sheet |
| Draw layer | user move and resize; Anchor menu To Cell, To Cell resize-with-cell, To Page; draw-mode copy and paste; Ctrl+drag copy; cross-sheet drag move; cell copy and paste; cut and paste; drag-move of a block; name-dialog rename; `MakeNameUnique` on paste; group and ungroup |
| Rendering † | `ScGridWindow::DrawContent`; print and PDF (`ScPrintFunc`); `DrawToDev` callers (copy as image, thumbnail, `docuno` render); `DrawRotated`; cell protection "Hide all" and "Hide when printing" (`sc::isCellContentHidden`, `lcl_HidePrint`); merged origin scrolled out of view; a state change that leaves the cell value unchanged (it needs an explicit `PostPaint` or broadcast, and a tiledrendering test of the invalidation). Grep the callers of `DrawSparklines` and `DrawStrings` for the full renderer list |
| Persistence and undo | ODS save and reload; XLSX save and reload; undo and redo of every `handle` row above; a fix that drops state on delete also restores it on undo; clipboard and undo documents; flush during import or in-progress undo; document close |
| Online | lazy interpret during tile paint; interpret during export; result arriving for an off-screen cell; a cell that was never painted (other sheet, AutoCalc off) †; several views and tile invalidation; manual recalc mode |
| UI † | read-only document and read-only view; protected sheet; in-place edit active; each surface: menubar, notebookbar, context menu (`readOnlyDisabled` is ignored for non-UNO commands, so a non-UNO command needs its own check); item shown when the feature or the service is off; each transition of a status or popup: none to error, error to the same error, error to another error, error to none, error to `#BUSY!`, cursor move between two cells with the same formula text |
| Wire and wsd † | each field absent, `null`, wrong type, empty, over its cap; pretty-printed input; trailing garbage; non-200 replies; every exit path of each new wsd command (invariant 14), with its reply, its `protocol.txt` line, and its browser string; document closing mid-request; no editable view present; when two components hold a timeout for one operation, which fires first |
| Settings and document properties † | property added, removed, re-added with the same value, changed during recalc, undo, or load; dialog remove-all then re-add; a document with no `=PY()` cells; each session mode; setting changed while kits are running; first evaluation before the setting is installed |

### 5.2 Questions to settle in the plan

Each of these was decided by Keith only after several rounds had polished the other answer. Ask the ones that apply, with a recommendation.

- **What does Excel do?** Probe live Excel or cite a source; do not write "as in Excel" from memory. (Row growth for in-cell images was tuned for four rounds, then removed to match Excel.)
- **What does the cell hold** when the visible result is not a value (an image, a pending call)? Empty, like an Excel picture in a cell, unless decided otherwise.
- **What is bounded, and at what size?** Every new field (invariant 3). Which image formats, and is a compressed or vector format worth its decode risk?
- **Who sees it?** Tracebacks, stdout, and errors in a shared document; what goes to logs and admin dumps.
- **What scope does a command have** (one user, one document, every session), does it confirm, and when is it shown?
- **How does a runtime change reach running kits**, or is "needs new kits" the documented answer?
- **Interface shape.** New methods on an existing UNO interface or a new interface; nothing has shipped, so reshape now if there is a reason.
- **Version skew.** Must kit and service ship together? Which WriterAgent version is the minimum?
- **Composition.** Two calls in one cell: which wins? Nested `LET`/`LAMBDA`? A reference to the cell itself?
- **Error vocabulary.** What Python receives for an error cell (stable ASCII tokens), and whether a 1×1 range behaves like a single reference. Describe arguments by Calc stack type (`svSingleRef`, `svDoubleRef`, `svMatrix`), not "single cell".

### 5.3 Sibling sets to grep in round 0

A guard that lands on one site and misses its twin was the largest code-defect class in November: "pending keeps the previous spill" gained one more site per round for four rounds. List these in the plan's sibling-site table before writing code.

- Every exit of `InterpretTail` (matrix, scalar, error, empty RPN, compile error) and `ScInterpreter::Init`.
- Both drain sites of `ProcessPendingMatrixResizes`, and `ScDocShell::ResolveSpilledOutputs`.
- Every caller of `Mark`/`UnmarkFormulaSpilled`, `MarkPendingMatrixResize`, `ResizeMatrixFormula`, and each `SetPython*` setter.
- Every `IsIn*()` guard the neighbouring code already checks: `IsInInterpreter`, `IsInLayoutStrings`, `IsInDtorClear`, `IsThreadedGroupCalcInProgress`, `IsMatrixResizeGuarded`, `IsInUndo`.
- The `ScFormulaCell` copy constructor and clone paths, for every new member.
- Every operator in a family (`+ - * / ^ & =` and the comparison matrix path), not only the one in the bug report.
- Every `SC_ADDINARG_*` case and every stack-type case in `ScExternal`; both `FillMixedArray` overloads.
- Every `URM_*` mode, and Insert, Delete, Move, and Copy tab.
- Every renderer (the Rendering row above).
- Every log and dump site of a URL, header, or buffer: `grep -n 'getUrl()\|dumpState\|dumpHex\|ex.what()' net/ wsd/`.
- Every UI surface that shows or gates the command; a gate duplicated in two files (`inputhdl.cxx` and `tabvwsha.cxx`) becomes one function.

## 6. Implementing and fixing

- Do not build or run tests locally (section 4.1). Jenkins (`gerrit_linux_gcc_main` and `gerrit_linux_clang_dbgutil_main`, which build with Python compute enabled and run `scaddins_pythoncompute`, `unit-python-compute`, `sc_ucalc`, and `sc_tiledrendering`) is the build. Inspect new C++ against `engine/compilerplugins/clang` before upload.
- **Test first for every reported bug.** Write the test the review asked for and trace it through the current code before changing production code. If the trace shows the test would pass unchanged, report `NO-REPRO` with the trace and do not add code. On the plot-image branch, three of eleven approved fixes in one round were redundant, and test-first was the only step that caught them.
- **Mutation check by trace.** For each new test, state in `STATUS.md` which line of the fix it depends on and why it fails without it.
- A fix sketch from a review is a hint. Reproduce, then take the smallest correct change, which may differ from the sketch. Three sketches applied as written caused the next round's finding ("erase the entry" without a reply or drain; `readOnlyDisabled` on a non-UNO command; a cell-store lookup added to a destructor). A sketch that breaks an invariant in section 2 is not applied; say so in `STATUS.md`.
- **Fix the class, not the instance.** When a finding names one site, enumerate the class (section 5.3) in the same round, fix it at the lowest common choke point, and list each site as handled or parked in `STATUS.md`. Row delete, formula-to-value, `SetValues`, and name recompilation are one class; they surfaced in four separate rounds because nobody did this. Credential redaction took seven rounds one call site at a time.
- **Do not fix a mechanism that has an open question.** If a finding needs Keith's decision, the change stops until he decides. No fix or review round runs on that mechanism in the meantime; rounds spent refining row growth, the README wire paragraphs, and the `#BUSY!` string compare were all deleted by the decisions that followed.
- **Prove the prose.** After every fix, grep the commit message, the README, `coolwsd.xml.in`, `wsd/protocol.txt`, the IDL comments, and nearby code comments for each symbol, number, and behavior you changed or removed. Every "every", "never", "all", "strict", or "RFC" in them has a test that enumerates the set, or weaker wording. Prose that disagreed with the code was the largest finding class of the whole stack (about 90 findings), and most of it was written by a fix round.
- A fix prompt for one change does not include message or documentation nits; those are batched into the last amend (section 8).
- Put deviations in `STATUS.md`: what was skipped, why, which test could not be reached and by which binary. Claims in a status file are produced by a command (`git grep`, `wc -c`, a test run), not from memory.
- Do not edit the decided file or the matrix from a fix round. Propose the sentence in `STATUS.md`. A decided-file entry says "fixed" only with the SHA and `file:line` that contain the fix; ten entries said "fixed" for code that was in no revision.
- End with `STATUS: DONE`, `STATUS: UNBUILT`, or `STATUS: STUCK <reason>`.

## 7. Reviewing

Severity, defined once so the verdict means something:

| Severity | Means | Needs |
|---|---|---|
| blocker | Data loss, crash or use-after-free, unbounded growth, silent deletion without undo, wrong-cell or wrong-sheet mutation, build or link failure in a supported configuration, a test that cannot pass | A `file:line` trace the fixer can walk, or test code |
| high | A `handle` cell of the matrix is not met, or an invariant in section 2 is violated | A trace |
| medium | A gap outside the matrix, a `handle` cell without a test, or cost that grows with document size on a hot path | A sentence |
| nit | Style, naming, comments, duplication | Optional |

**Landing bar.** Ready to land means no blocker and no high. Mediums and nits are listed and go to the final batch or a follow-up; they do not block, and a reviewer is not asked whether they should. The reviewer ends with `Blocking findings: N`, and that line is the only verdict; "MERGE AFTER FIXES" on a list of lows is not a verdict. The human decides landing.

Rules for the reviewer:

- Read this document, the change's decided file, the matrix, and `RECONSIDER-collabora.md` before the diff. Do not reopen an `accept`, an `out of scope` cell, a decided item, or a parked item without new evidence, and say what the evidence is (`file:line`, a trace, a failing test). "Not re-litigating, but" is reopening. Twelve decided or parked items were raised again in November, some three times.
- **Trace, do not skim.** Walk each new path through the layers with concrete values: the interpreter, the AddIn, the kit, wsd, and back. For each new test, trace its setup through the code and confirm the setup is accepted and the assertion can fail. Check each sentence of the commit message against the diff. Every clean verdict from a diff-reading review was overturned by a reviewer who traced: a six-step livelock, two timers racing, a destructor running after its column, a test whose row delete `HasBlockMatrixFragment` refuses.
- Verify a claim at `file:line` before reporting it, and do not write "verified" or "would fail without the fix" without having traced it. For a claim about Calc behavior, probe live Calc.
- Round 1 reviews the whole diff against the matrix. Every later round reviews the delta since the last reviewed commit plus closure of the prior findings. Do not re-read the squash for new nits. The last round is one full trace of the whole change (section 8).
- List every finding with an explicit severity tag (`**BLOCKER**`, `**HIGH**`, `**MEDIUM**`, `**LOW**`, or `**NIT**`). Keep a complete, severity-tagged inventory of all nits and minor findings (no cap). Nits are listed separately from the DO / DO NOT / PARK triage; they do not block landing.
- No "INFO, no action" entries, no finding you call unreachable yourself, and no hardening for a state that cannot occur.
- A fix sketch names the smallest change and the existing helper, and does not break an invariant in section 2 (no new idle flush, setting, or protocol). A test proposal is test code using existing helpers and names the binary. "Add a test for X" without code is a nit.
- A bug that predates the patch, including behavior of a parent change in the stack, is labelled `pre-existing` and proposed as a separate change. It is not a finding against the patch.
- Do not ask for a commit split, a clang-format reflow of untouched lines, or dropping `WIP:`; sections 3 and 9 settle those.
- Do not build in a checkout other reviewers are reading. Hand the test code to the fixer, who runs it first (section 6).
- Output in this order: findings with severity and `file:line` (including every nit); root causes marked `change` or `accept`; DO, DO NOT, PARK lists; then a separate complete nit inventory. End with `Blocking findings: N`.

## 8. Round budget

| Round | Work | Exit |
|---|---|---|
| 0 | Plan with every item in section 5; Keith answers the questions | No open question on any mechanism |
| 1 | Implement with the hard tests; one full-diff review against the matrix by the strongest reviewer available; test-first fixes | Every `handle` cell has code and a test |
| 2 | Delta review on the landing bar; test-first fixes | No blocker, no high |
| 3 | One full trace of the whole change by the strongest reviewer; then one final amend with every batched message, README, and nit item | `Blocking findings: 0`; land |

- **The strongest reviewer goes first, not last.** A cheaper reviewer converging means only that it has run out of things it can see. In November each such convergence was followed by a stronger reviewer finding a high or a crash, on four changes.
- **`Blocking findings: 0` ends the loop.** An approval does not queue another job. Nits never count as real bugs for the purpose of another round.
- **Prefer bottom-up.** Work on the lowest moving change first where you can; every amend below forces a restack and a re-review above. Jumping to a higher change is allowed when it is not blocked by the one below. Four message-and-docs amends of 19188 in one afternoon forced three restacks of 21132. A review of a SHA that has been superseded is discarded, not triaged.
- **Message and documentation edits happen once**, in the final amend after the code has converged, unless a sentence has become false.
- At the limit, land as experimental with the accepted limitations in the commit message, or split the commit. "Not ready" with no finish line is not an outcome. The plot-image branch ran eleven single rounds and four parallel rounds; the last two parallel rounds found no blocker and no high and still said "not ready" five times each, because no bar had been set.

## 9. Commits and Gerrit

- Series shape: keep whole features together (do not split a single feature into tiny separable helper commits unless a reviewer asks). Stack dependent features cleanly on top of each other so each commit builds and passes on its own.
- Commit message rules:
  - The subject starts with an area prefix (`sc:`, `kit:`, `wsd:`, `CODA:`, `mocha:`). Keep `WIP:` in front while the change has open `TODO:` lines.
  - The body is plain paragraphs: what the user saw or what was missing, then the cause or mechanism, then a "Now ..." paragraph with what the patch does. Aim for about 400 to 1,000 bytes of prose.
  - Leave out reviewer notes (why another approach would not help, lists of other places that look similar), guard bookkeeping, function-by-function narration, minor edge cases, test names, and any mention of earlier patch sets or review rounds.
  - Real limitations and `TODO:` lines are always kept in full, in a short `Limitations:` or `TODO:` block after the prose. They are how future work items are tracked, so never cut them to make the size target.
  - Subject plus body is at most 1690 bytes (`wc -c`), so the message with trailers stays under 1800 and Windows Jenkins does not hit `environment is too large`. If the limitations push it over, shorten the prose, not the limitations.
  - Trailers are `Change-Id` and exactly one `Signed-off-by: Keith Curtis`. No `Tests:` line and no AI or `Co-Authored-By` attribution.
- The subject starts with `WIP: ` on every change that is not yet ready for Collabora's review. Work still owed on an unguarded change is a `TODO:` line.
- Every sentence in the message is true of the diff. After trimming for the byte cap, read each remaining sentence against the diff again; trimming twice turned an accurate sentence into a false one.
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

- Add a setting or a retry loop, or leave a new service or document field without a named maximum.
- Add a `std::mutex` to the AddIn, or a timer.
- Put NumPy, `split_grid`, or Pickle in C++, or a user venv in the kit.
- Insert graphics from the kit (`.uno:InsertGraphic` lands on a view cursor; `=PY()` is async and multi-view).
- Add `FormulaError` values or a new Online error protocol.
- Merge `anyjson` into QuickJS/jsuno, or use Poco in Core (reusing `comphelper/json.hxx` is fine and preferred over duplicating JSON parsing).
- Use a shape name, a position, or a main-anchor cell as identity. Compare a display string to learn state.
- Mutate the draw page during interpret, XML import, or in-progress undo or redo. Move the Interpret-tail drain. Add an idle or posted flush, or skip-while-saving, without a plan decision.
- Look up a cell, column, or table from a destructor.
- Put a capability in a URL query, or redact it per call site.
- Pull `config_features.h` into `document.hxx`. Guard a generic fix behind the feature flag.
- Implement a review's fix sketch before reproducing the bug.
- Build or run tests locally, or report a test as passing before Jenkins has run it.
- Fix or review a mechanism while a question about it is waiting for Keith,.
- Queue another round after `Blocking findings: 0`.
- Forbid a test in a fix prompt. If a test is expensive, the plan says so with a reason.
- Grow the decided file by appending reviewer prose. Add one rule row with a one-line why.
- Commit, amend, push, stash, rebase, or switch branches from an agent session.
