# WriterAgent NITs since 3342d1d (3342d1da6 @ 2026-11-23T16:03:30-05:00)

Generated: 2026-11-24 21:31 EST

## Summary
- Raw NITs extracted: **163**
- After deduplication: **162** (open: **161**, already handled in followups ledger: **1**)
- By source kind (raw): chain-work: 147, followups-triage: 3, wa-reviews: 13

## Per-area breakdown

| Area | Open | Handled | Total |
|---|---:|---:|---:|
| `docs/archive` | 1 | 0 | 1 |
| `docs/calc` | 2 | 0 | 2 |
| `docs/chat` | 2 | 0 | 2 |
| `docs/eval` | 1 | 0 | 1 |
| `docs/framework` | 2 | 0 | 2 |
| `docs/images` | 2 | 0 | 2 |
| `docs/writer` | 6 | 0 | 6 |
| `general` | 4 | 0 | 4 |
| `git-commit` | 10 | 0 | 10 |
| `github` | 2 | 0 | 2 |
| `plugin` | 2 | 0 | 2 |
| `plugin/calc` | 18 | 0 | 18 |
| `plugin/chatbot` | 3 | 0 | 3 |
| `plugin/doc` | 2 | 0 | 2 |
| `plugin/draw` | 2 | 0 | 2 |
| `plugin/framework` | 2 | 0 | 2 |
| `plugin/ppt_master` | 1 | 0 | 1 |
| `plugin/scripting` | 9 | 0 | 9 |
| `plugin/vision` | 2 | 0 | 2 |
| `plugin/writer` | 31 | 1 | 32 |
| `tests` | 3 | 0 | 3 |
| `tests/calc` | 8 | 0 | 8 |
| `tests/chatbot` | 2 | 0 | 2 |
| `tests/compute_service` | 2 | 0 | 2 |
| `tests/draw` | 5 | 0 | 5 |
| `tests/framework` | 1 | 0 | 1 |
| `tests/ppt_master` | 1 | 0 | 1 |
| `tests/scripting` | 6 | 0 | 6 |
| `tests/scripts` | 2 | 0 | 2 |
| `tests/writer` | 27 | 0 | 27 |

## NITs by area and file

### docs/archive (1 open, 0 handled)

#### `docs/archive/test_architecture_analysis.md`

- **NIT-0001** `docs/archive/test_architecture_analysis.md:22` (PR #3961 | 2026-11-24 18:56 | opus | `chain-work/FU-UNO-IMPORTS-2.rev1.md:54`) — `docs/archive/test_architecture_analysis.md:22` still lists `loadStylesFromURL` as a Writer document stub method. Archive doc; optional.

### docs/calc (2 open, 0 handled)

#### `docs/calc/duckdb-dev-plan.md`

- **NIT-0002** `docs/calc/duckdb-dev-plan.md:74` (PR #3970 | 2026-11-24 19:59 | opus | `chain-work/FU-CLEANUP-7.rev1.md:29`) — `docs/calc/duckdb-dev-plan.md:74` — "a list is rejected by the schema" is about the whole `files` argument, while the new clauses are about each value in the map. Run together, "any other non-string value" can be read as the same subject. The wording is the plan's own. Fix (optional): "a list is rejected by the schema; within the map, a null or blank value is skipped, …".

#### `docs/calc/specialized-toolsets.md`

- **NIT-0003** `docs/calc/specialized-toolsets.md:67,113` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:53`) — `docs/calc/specialized-toolsets.md:67,113` — nested parentheses, "array measure (measured outside the cells the formula references))". Fix: "array measure outside the cells the formula references".

### docs/chat (2 open, 0 handled)

#### `docs/chat/sidebar-implementation.md`

- **NIT-0004** `docs/chat/sidebar-implementation.md:513` (PR #3965 | 2026-11-24 19:19 | opus | `chain-work/FU-CARET-INSERT-PIN.rev1.md:32`) — `docs/chat/sidebar-implementation.md:513` — "`target="selection"` tools and `get_content(scope="selection")` and the insert-at-caret tools (…)" has two "and"s. Use a comma list.

#### `docs/chat/smol-tool-architecture.md`

- **NIT-0005** `docs/chat/smol-tool-architecture.md:190` (PR #3981 | 2026-11-24 20:55 | opus | `chain-work/FU-AGENT-OBS-3.rev1.md:32`) — `docs/chat/smol-tool-architecture.md:190` — the bullet names `ppt_master_finished`, then repeats it at the start of its own parenthesis. Fix: for example "`workflow_finished`; `ppt_master_finished`, which ends a PPT-Master session when its call succeeds in a turn that did not fail. Text in other tools' results does not end it."

### docs/eval (1 open, 0 handled)

#### `docs/eval/string-harness-upgrade.md`

- **NIT-0006** `docs/eval/string-harness-upgrade.md:224` (PR #3982 | 2026-11-24 20:46 | opus | `chain-work/FU-EVAL-CLEAN-5.rev1.md:21`) — `docs/eval/string-harness-upgrade.md:224` — "any `target` other than `search` ..., or non-Quotations `target="full_document"`" contradicts itself: the first part already covers `full_document`, yet Quotations on it works. The wording came from the plan. Fix: "any `target` other than `search` or `full_document` (omitted means `selection`), or non-Quotations `full_document`".

### docs/framework (2 open, 0 handled)

#### `docs/framework/type-checking.md`

- **NIT-0007** `docs/framework/type-checking.md:25` (PR #3968 | 2026-11-24 19:35 | opus | `chain-work/FU-CI-OPENGREP.rev1.md:18`) — `docs/framework/type-checking.md:25` — "Ubuntu PR CI opengrep pin: Ubuntu PR CI downloads a pinned…" repeats itself. Fix: start the body with "The job downloads…".

#### `docs/framework/uno-test-lifecycle.md`

- **NIT-0008** `docs/framework/uno-test-lifecycle.md:1000-1002` (PR #3963 | 2026-11-24 19:08 | opus | `chain-work/FU-TEST-PROFILE-2.rev1.md:20`) — `docs/framework/uno-test-lifecycle.md:1000-1002` — "Unit test: `test_exit_after_summary_*` in `tests/framework/test_testing_runner.py`" now follows the cleanup description, but the tests pinning that are in `tests/scripts/test_testing_runner_cli.py`. The inserted text is also one very long line in a section wrapped at about 72 columns. Failure: a reader looking for the test behind "a failing run keeps both" opens a file that does not test it. Fix: name `test_clean_exit_*` and `test_failed_exit_keeps_profile_and_names_it` too, and wrap the paragraph.

### docs/images (2 open, 0 handled)

#### `docs/images/recognition.md`

- **NIT-0009** `docs/images/recognition.md:162` (PR #3974 | 2026-11-24 20:06 | opus | `chain-work/FU-VISION-IMAGE-2.rev1.md:31`) — `docs/images/recognition.md:162` — "discovered unnamed images" follows the Track Changes sentences, away from the `delete_image_after_insert` clause it qualifies, and "discovered" is internal wording. Suggest "In Calc, an unnamed selected image is deleted too." next to that clause.
- **NIT-0010** `docs/images/recognition.md:662` (PR #3974 | 2026-11-24 20:06 | opus | `chain-work/FU-VISION-IMAGE-2.rev1.md:33`) — `docs/images/recognition.md:662` — the last two sentences now overlap: the first says an export on a closed document ends in `DocumentDisposedError`, the second repeats it for kept shapes. Fine to merge into one.

### docs/writer (6 open, 0 handled)

#### `docs/writer/html-style-model-plan.md`

- **NIT-0011** `docs/writer/html-style-model-plan.md:205` (PR - | 2026-11-24 21:23 | opus | `chain-work/FU-WRITE-MARKUP-2.rev2.md:41`) — `docs/writer/html-style-model-plan.md:205` — "On range reads" is slightly too narrow: when a full read reloads a hidden copy, the copy's own sidecar export can still fail and give an empty map. Say "On range reads and on the hidden copy".

#### `docs/writer/llm-styles.md`

- **NIT-0012** `docs/writer/llm-styles.md:43` (PR #3847 | 2026-11-24 13:38 | fable | `fable-structural/349-writer-range-read.md:115`) — `docs/writer/llm-styles.md:43` still describes the `full_document` pre-import reset as paragraph properties only ("margins, indent, alignment, ... list style name"). The character formatting and character style reset is documented only in `html-style-model-plan.md:229`. One clause would do.

#### `docs/writer/reviewable-agent-edits.md`

- **NIT-0013** `docs/writer/reviewable-agent-edits.md:164-165` (PR #3977 | 2026-11-24 20:27 | opus | `chain-work/FU-SECTIONS-FULLDOC-2.rev1.md:15`) — `docs/writer/reviewable-agent-edits.md:164-165`, `docs/writer/specialized-toolsets.md:504,506` — the inserted text leaves lines well past the surrounding wrap (about 100 columns), and the parenthetical is repeated twice in each sentence. Fix: rewrap; in those two docs say it once, e.g. "refuses any hidden section (`HIDDEN_SECTION`) or protected non-index section (`PROTECTED_SECTION`) outside headers and footers and the tables and frames in them". Keep the reason in `specialized-toolsets.md` (a full read returns hidden text and re-importing it would show or delete it).
- **NIT-0014** `docs/writer/reviewable-agent-edits.md:321` (PR #3960 | 2026-11-24 18:55 | opus | `chain-work/FU-REPLACE-PARA-INHERIT-2.rev1.md:22`) — `docs/writer/reviewable-agent-edits.md:321` — the bold label ("Recorded `full_document` and whole-paragraph replace Undo with character style at start") is hard to parse and repeats the body. Something like "**Undo of a recorded replace:**" is enough.
- **NIT-0015** `docs/writer/reviewable-agent-edits.md:36,42` (PR #3976 | 2026-11-24 20:16 | opus | `chain-work/FU-RECORD-STYLE-6.rev1.md:21`) — `docs/writer/reviewable-agent-edits.md:36,42` — symbolic math is now named in row 36, and row 42 still says "math formula insert" without saying which path. Fix: none needed here; see Follow-ups.

#### `docs/writer/structured-edit-diagnostics-plan.md`

- **NIT-0016** `docs/writer/structured-edit-diagnostics-plan.md:114` (PR #3986 | 2026-11-24 21:09 | opus | `chain-work/FU-HEADING-RENAME-3.rev2.md:23`) — `docs/writer/structured-edit-diagnostics-plan.md:114` — "(otherwise links to them stay unchanged with `LINK_TARGET_STALE`)" still reads as covering only the after-a-move rule, not an unconfirmed number-only title. The last sentence of the paragraph ("lost titles with no clear successor") does cover it, so nothing is wrong. Optional fix: move the clause next to "requiring position to confirm number-only titles".

### general (4 open, 0 handled)

#### `(no file path)`

- **NIT-0017** `-` (PR #3876 | 2026-11-24 | opus | `followups.tsv#1869 (chain-work/FOLLOWUPS-REVIEW-1124b.md)`) — `_last_user_message_with_image(session: Any)` and `TurnController.session: Any` could be typed as `ChatSession | None`, so the checker enforces the guard at line 988.
- **NIT-0018** `-` (PR #3968 | 2026-11-24 19:35 | opus | `chain-work/FU-CI-OPENGREP.rev1.md:19`) — plan note, no code change — curl honours a server `Retry-After` in place of `--retry-delay`, so a 429 with a long value waits until the step timeout instead of using six retries. Still fatal, so nothing to fix.
- **NIT-0019** `-` (PR #3973 | 2026-11-24 20:03 | opus | `chain-work/FU-TEST-HYGIENE-7.rev1.md:32`) — the three shape-binding comments say "Bind every shape proxy once before inspecting it" and point at the lifecycle page. The why is only behind the pointer. Fix: add a few words, e.g. "a late release of a dropped proxy detaches the refetched one".
- **NIT-0020** `-` (PR - | 2026-11-24 21:01 | opus | `chain-work/FU-WRITE-MARKUP-2.rev1.md:55`) — `_LINK_TAG_RE` `<(/?)a\b` also matches `<a-b>`/`<a:b>`; use `a(?=[\s/>])`. `m.group(2).endswith("/")` also skips an unquoted `href=https://x/`. This only leaves that link unsplit, as on master.

### git-commit (10 open, 0 handled)

#### `(no file path)`

- **NIT-0021** `-` (PR #3962 | 2026-11-24 19:01 | opus | `chain-work/FU-REVIEW-FLAGS-2.rev1.md:39`) — commit body is "wip: code v1". Replace it before the PR.
- **NIT-0022** `-` (PR #3965 | 2026-11-24 19:19 | opus | `chain-work/FU-CARET-INSERT-PIN.rev1.md:38`) — Commit body is "wip: code v1". Replace it with the behaviour-change list before opening the PR.
- **NIT-0023** `-` (PR #3967 | 2026-11-24 19:28 | opus | `chain-work/FU-IMAGE-LOG-CAPLOG.rev1.md:14`) — commit message — the body is `wip: code v1`. Fix: replace it at landing with the cause: conftest restored the `plugin` level but not the `writeragent` level.
- **NIT-0024** `-` (PR #3971 | 2026-11-24 19:56 | opus | `chain-work/FU-GROUPED-SHAPES-2.rev1.md:33`) — commit message body is still "wip: code v1". Replace it when the PR is prepared.
- **NIT-0025** `-` (PR #3970 | 2026-11-24 19:59 | opus | `chain-work/FU-CLEANUP-7.rev1.md:28`) — commit message of `a9dd971fd` — the body is only `wip: code v1`, so the reasons do not reach history. Fix: when squashing, drop the wip line and give one sentence per change.
- **NIT-0026** `-` (PR #3974 | 2026-11-24 20:06 | opus | `chain-work/FU-VISION-IMAGE-2.rev1.md:35`) — commit body is "wip: code v1". Replace it before the PR.
- **NIT-0027** `-` (PR #3979 | 2026-11-24 20:41 | opus | `chain-work/FU-RECORD-EDGES-3.rev1.md:31`) — commit message — the body is `wip: code v1`. Fix: drop it, or state the three behaviour changes, when squashing.
- **NIT-0028** `-` (PR #3978 | 2026-11-24 20:43 | opus | `chain-work/FU-WARMCHILD-3.rev1.md:27`) — commit `9bfa0c62e` — the message body is `wip: code v1`. Failure: none at run time; the reason for the new seam is missing from history if this is not squashed. Fix: replace the body at landing with one line on what each test pins.
- **NIT-0029** `-` (PR - | 2026-11-24 21:01 | opus | `chain-work/FU-WRITE-MARKUP-2.rev1.md:57` | also: `chain-work/FU-WRITE-MARKUP-2.rev2.md:43`) — Commit body is still `wip: code v2`; the PR text needs the nine behaviour changes from plan §4, plus the region link-rule change below.
- **NIT-0030** `-` (PR #3986 | 2026-11-24 21:09 | opus | `chain-work/FU-HEADING-RENAME-3.rev2.md:25`) — branch history — `24ea1265b` and `6c573a3ba` have the same message. Squash when the branch lands.

### github (2 open, 0 handled)

#### `github/workflows/pr-ci.yml`

- **NIT-0031** `github/workflows/pr-ci.yml:117` (PR #3968 | 2026-11-24 19:35 | opus | `chain-work/FU-CI-OPENGREP.rev1.md:16`) — `.github/workflows/pr-ci.yml:117` — the comment lost "unlike macOS/Windows". "Fatal: typecheck runs opengrep-lint" does not explain the difference, because those runners run the same `make test-full` and use `|| true`. Fix: say "Fatal here (unlike macOS/Windows)", and that the speed floor exists because the ~160 MB asset must finish inside the 5-minute step.
- **NIT-0032** `github/workflows/pr-ci.yml:121` (PR #3968 | 2026-11-24 19:35 | opus | `chain-work/FU-CI-OPENGREP.rev1.md:17`) — `.github/workflows/pr-ci.yml:121` — the reason for `-o` over a pipe is not written: curl can restart a file on retry but cannot rewind a pipe, so `| bash` would get duplicated bytes. The macOS step still shows the pipe form, so someone may copy it back with `--retry`. Fix: one clause in the existing comment.

### plugin (2 open, 0 handled)

#### `plugin/testing_runner.py`

- **NIT-0033** `plugin/testing_runner.py:1573` (PR #3973 | 2026-11-24 20:03 | opus | `chain-work/FU-TEST-HYGIENE-7.rev1.md:34`) — `plugin/testing_runner.py:1573` — the rewritten comment is one ~230-character line where its neighbours wrap. The text matches the plan. Fix: wrap it.
- **NIT-0034** `plugin/testing_runner.py:950-951` (PR #3963 | 2026-11-24 19:08 | opus | `chain-work/FU-TEST-PROFILE-2.rev1.md:21`) — `plugin/testing_runner.py:950-951` — `_terminate_bootstrap_soffice` still says "before a bootstrap retry". It now also runs at clean exit (and already ran on recycle). Failure: the docstring names one of four uses. Fix: "Best-effort stop of the harness soffice (retry, recycle, clean exit)."

### plugin/calc (18 open, 0 handled)

#### `plugin/calc/array_formula.py`

- **NIT-0035** `plugin/calc/array_formula.py:122` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:55`) — `plugin/calc/array_formula.py:122` — `_call_end` knows only `"` and `'`, but `PyFormula.parse` also accepts a curly-quoted code argument. `=PY(“result = "it's"”; A1:A3)` with `array=true` was found by the old whole-formula parse; now the apostrophe runs to the end, the call is skipped, and neither `PY_CIRCULAR` nor the pre-26.8 refusal fires. I did not check how Calc compiles curly quotes; the write most likely fails there anyway. Very rare, so no change asked; listed so it is a known difference.
- **NIT-0036** `plugin/calc/array_formula.py:143` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:51`) — `plugin/calc/array_formula.py:143` — the docstring omits the two rules a caller needs: text inside quotes is ignored, and an unclosed call is skipped. Fix: add them.

#### `plugin/calc/formula_dialect.py`

- **NIT-0037** `plugin/calc/formula_dialect.py:193` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:49`) — `plugin/calc/formula_dialect.py:193` — the docstring lists defined names and INDIRECT/OFFSET as unseen. Table references are unseen too: `tokenuno.cxx` returns a `TableRefToken`, which has neither field. Fix: add "table references".

#### `plugin/calc/manipulator.py`

- **NIT-0038** `plugin/calc/manipulator.py:2013` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:45`) — `plugin/calc/manipulator.py:2013` — "1x2 scratch block": the block is two rows by one column. This file writes sizes as rows x columns. Fix: "two-row, one-column scratch block".
- **NIT-0039** `plugin/calc/manipulator.py:2023` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:47`) — `plugin/calc/manipulator.py:2023` — the `row < MAX_ROW` limit leaves room for the second scratch row (`COLUMNS`), which used to be visible as `row + 1 > MAX_ROW`. Fix: write `row + 1 <= MAX_ROW`, or leave it.
- **NIT-0040** `plugin/calc/manipulator.py:742` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:43`) — `plugin/calc/manipulator.py:742` — the moved `_py_run_detail` comment says "the entry present before setFormula"; it now also serves `setArrayFormula`. Fix: "before the write".

#### `plugin/calc/python/geometric_recalc.py`

- **NIT-0041** `plugin/calc/python/geometric_recalc.py:461` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:48` | rev2: `rev1->fixed:Conditional dropped; `reconcile` says UI thread only.`) — `plugin/calc/python/geometric_recalc.py:461`: `after_py_cell_save` still has `get_ctx … if main.is_current() else None`, but `reconcile` now returns off the UI thread, so the `else None` branch does nothing. Say "UI thread only" in the `reconcile` docstring and drop the conditional.

#### `plugin/calc/python/spill.py`

- **NIT-0042** `plugin/calc/python/spill.py` (PR #3966 | 2026-11-24 19:16 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev1.md:35` | rev2: `rev1->fixed:Fixed`) — `plugin/calc/python/spill.py` `load_spill_registry_for_doc` — the docstring dropped the source ("from its UserDefinedProperties"). Keep it: "Load the registry from UserDefinedProperties once per workbook state; later calls do nothing."
- **NIT-0043** `plugin/calc/python/spill.py` (PR #3966 | 2026-11-24 19:16 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev1.md:37` | rev2: `rev1->fixed:Fixed`) — `plugin/calc/python/spill.py` `prepare_auto_spill` — the release is scheduled even with no record, which looks wasteful. It is needed to replace a pending write. Add that to the comment so nobody guards it away.
- **NIT-0044** `plugin/calc/python/spill.py` (PR #3966 | 2026-11-24 19:16 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev1.md:39` | rev2: `rev1->fixed:No action was asked`) — `plugin/calc/python/spill.py` `_live_origin_sheet` — it carries `hasattr(sheet, "getName")` and the `resolved_sheet` juggling over from the old code. Meaning is unchanged as the plan asked; no action needed here.
- **NIT-0045** `plugin/calc/python/spill.py` (PR #3966 | 2026-11-24 19:23 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev2.md:31`) — `plugin/calc/python/spill.py` `SPILL_CONTENT_FLAGS` — uncommented and repeats part of the mask above. Fix: define it first and write `SPILL_CLEAR_FLAGS = SPILL_CONTENT_FLAGS | 32`.
- **NIT-0046** `plugin/calc/python/spill.py` (PR #3966 | 2026-11-24 19:23 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev2.md:33`) — `plugin/calc/python/spill.py` `_clear_blocked_spill` — "ownership is kept if content remains" is in the docstring and again in the inline comment above `if not _clear_rect(...)`. Keep one.
- **NIT-0047** `plugin/calc/python/spill.py` (PR #3966 | 2026-11-24 19:23 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev2.md:35`) — `plugin/calc/python/spill.py` `_clear_rect` docstring — `cellsuno.cxx:1682-1693` is the caller of the ignored result; a function name (`ScCellRangesBase::clearContents`) outlives line numbers.
- **NIT-0048** `plugin/calc/python/spill.py:526` (PR #3983 | 2026-11-24 20:50 | opus | `chain-work/FU-SPILL-REMAP.rev1.md:62`) — `plugin/calc/python/spill.py:526` — "A pending remap is applied first." sits after the blocked-recheck sentence, so "first" is unclear. Move it before "After rechecking that targets are still free", for example "A remap the modify pass has not done yet is applied first, so the recheck uses the right rectangle."
- **NIT-0049** `plugin/calc/python/spill.py:695-697` (PR #3983 | 2026-11-24 20:50 | opus | `chain-work/FU-SPILL-REMAP.rev1.md:64`) — `plugin/calc/python/spill.py:695-697` — the sheet-edge branch lost its debug line, and the collision line lost the cell type and formula. The plan's snippet did the same and the diagnostics message still names the reason, so this is acceptable.
- **NIT-0050** `plugin/calc/python/spill.py:737-743` (PR #3983 | 2026-11-24 20:50 | opus | `chain-work/FU-SPILL-REMAP.rev1.md:58`) — `plugin/calc/python/spill.py:737-743` and `:209-211` — the witness delta (`_used_end` minus `state.used_ends`) is now computed in two places. Both must agree for prepare and cleanup to pick the same cell. A small `_used_end_delta(state, sheet, sheet_name) -> tuple[int, int] | None` would give the rule one owner. Not required: the plan did not ask for it, and the docstring already names the link to `_classify_orphan_record`.
- **NIT-0051** `plugin/calc/python/spill.py:787` (PR #3983 | 2026-11-24 20:50 | opus | `chain-work/FU-SPILL-REMAP.rev1.md:60`) — `plugin/calc/python/spill.py:787` — "A formula moved by an insert or delete before it uses its unmoved record's rectangle" first reads as "before it uses". Suggest: "When rows or columns before a formula were inserted or deleted, it uses its unmoved record's rectangle; the deferred write remaps and saves."

#### `plugin/calc/python/workbook_lifecycle.py`

- **NIT-0052** `plugin/calc/python/workbook_lifecycle.py:204-207` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:52` | rev2: `rev1->fixed:fixed`) — `plugin/calc/python/workbook_lifecycle.py:204-207`: the blank line between the two `best_effort` calls is left over from `_clear_geo`. Also, `prior_keys` are not cleared. This matters only if a key is upgraded to a RuntimeUID mid-session, which does not happen with real documents, so no code is needed.

### plugin/chatbot (3 open, 0 handled)

#### `plugin/chatbot/dialogs.py`

- **NIT-0053** `plugin/chatbot/dialogs.py:334` (PR #3969 | 2026-11-24 19:53 | opus | `chain-work/FU-SECRET-LOGS-6.rev1.md:21`) — `plugin/chatbot/dialogs.py:334` — `msgbox` uses `redact_urls_in_text(str(message))`; here it is `redact_urls_in_text(message)`. The parameter is annotated `str` and every caller I checked passes a string, so there is no live failure. A `None` would now raise `TypeError` where the old line logged it, although later lines of the same function tolerate it (`(message or "")`, `str(message)`). The plan wrote it this way. Optional.
- **NIT-0054** `plugin/chatbot/dialogs.py:334` (PR #3969 | 2026-11-24 19:53 | opus | `chain-work/FU-SECRET-LOGS-6.rev1.md:23`) — `plugin/chatbot/dialogs.py:334` — no reason at this site. The helper's docstring carries the reason (no auth, so the host is the credential) and the import points to it, so this is acceptable as is.

#### `plugin/chatbot/rich_text_paste.py`

- **NIT-0055** `plugin/chatbot/rich_text_paste.py` (PR #3876 | 2026-11-24 | opus | `followups.tsv#1871 (chain-work/FOLLOWUPS-REVIEW-1124b.md)`) — `fold_transcript_chunk` still uses `messages[-1].get("content") or ""` on an open row whose content is always a `str`.

### plugin/doc (2 open, 0 handled)

#### `plugin/doc/text_helpers.py`

- **NIT-0056** `plugin/doc/text_helpers.py` (PR #3972 | 2026-11-24 20:01 | opus | `chain-work/FU-READS-STRUCK-4.rev2.md:38`) — `plugin/doc/text_helpers.py`, `visible_text` — `_span_cursor` builds a cursor at the start and extends it, then `wide` builds it again. Creating `lead` from `span.getStart()` and building `wide` only inside the astral branch would keep the common path to what it needs.
- **NIT-0057** `plugin/doc/text_helpers.py` (PR #3972 | 2026-11-24 20:01 | opus | `chain-work/FU-READS-STRUCK-4.rev2.md:40`) — same method — the docstring's search-hit sentence ("Cursors come from the range's own text because ...") is last, after the two exceptions. It is the main rule, so it reads better first.

### plugin/draw (2 open, 0 handled)

#### `plugin/draw/shape_clone.py`

- **NIT-0058** `plugin/draw/shape_clone.py:231` (PR #3975 | 2026-11-24 20:06 | opus | `chain-work/FU-SCRATCH-DOCS-6.rev1.md:33`) — `plugin/draw/shape_clone.py:231` — `_fill_clone` has no docstring, and its keyword defaults are dead because the only caller passes all five. Drop the defaults and add one line: "Copy *src_shape* onto *clone*, which is already in its container."

#### `plugin/draw/shapes.py`

- **NIT-0059** `plugin/draw/shapes.py:322-326` (PR #3961 | 2026-11-24 18:56 | opus | `chain-work/FU-UNO-IMPORTS-2.rev1.md:50`) — `plugin/draw/shapes.py:322-326` — the touched module keeps a second spelling, `uno_mod: Any = uno` then `uno_mod.Any(...)`, beside `cast("Any", uno).Any` at 241 and 246. `indexes.py:730` `uno_rt` is the same, but the plan keeps that one. For one spelling, use the cast here and keep the comment's reason that PyUNO rejects `uno.Any` outside `uno.invoke` and refuses bare tuples.

### plugin/framework (2 open, 0 handled)

#### `plugin/framework/config.py`

- **NIT-0060** `plugin/framework/config.py:150-151` (PR #3963 | 2026-11-24 19:08 | opus | `chain-work/FU-TEST-PROFILE-2.rev1.md:22`) — `plugin/framework/config.py:150-151` — `reset_config_for_tests` says "(pytest isolation)", but the UNO runner now calls it too. Failure: someone moving it into a pytest-only helper breaks `_rebootstrap_ctx`. The comment at the call site carries the reason, so this is optional; the plan kept `config.py` untouched. Fix: "(pytest isolation; the UNO runner on a new office)".

#### `plugin/framework/scratch_writer.py`

- **NIT-0061** `plugin/framework/scratch_writer.py` (PR #3882 | 2026-11-24 | opus | `followups.tsv#1883 (chain-work/FOLLOWUPS-REVIEW-1124b.md)`) — `clear_writer_body` logs "dropped default-template content" on each reuse that had pasted content. Reword when next touched.

### plugin/ppt_master (1 open, 0 handled)

#### `plugin/ppt_master/venv/runner.py`

- **NIT-0062** `plugin/ppt_master/venv/runner.py:261` (PR #3981 | 2026-11-24 20:55 | opus | `chain-work/FU-AGENT-OBS-3.rev1.md:31`) — `plugin/ppt_master/venv/runner.py:261` — `finished is None and` makes the first of two `ppt_master_finished` calls in one turn win. This is the plan's behaviour change 3, but no test pins it. Failure: someone drops the condition as redundant, the last call wins, and the suite stays green. Fix: add a short case using `_run_scripted_turn` with two finish calls in one turn, expecting the first message.

### plugin/scripting (9 open, 0 handled)

#### `plugin/scripting/calc_range.py`

- **NIT-0063** `plugin/scripting/calc_range.py` (PR #3984 | 2026-11-24 20:59 | opus | `chain-work/FU-CALC-HELPERS-8.rev1.md:48` | rev2: `rev1->fixed:Partly fixed: imports moved except in `test_propagate_errors`; some blank lines at the ends of files remain.`) — Extra blank lines at the end of several test files, and the new tests import `CalcRange`, `nan_error_code` and friends inside function bodies; move them to the top of each file.

#### `plugin/scripting/symbolic.py`

- **NIT-0064** `plugin/scripting/symbolic.py:79` (PR #3976 | 2026-11-24 20:16 | opus | `chain-work/FU-RECORD-STYLE-6.rev1.md:20`) — `plugin/scripting/symbolic.py:79` — nothing says why `starmath` is bound, and it sits apart from its use. A reader may inline it and break typecheck. Fix: move it to just above the call, with a short "a lambda does not keep the None check on conv.starmath" comment.

#### `plugin/scripting/text_analytics.py`

- **NIT-0065** `plugin/scripting/text_analytics.py:47` (PR #3972 | 2026-11-24 19:41 | opus | `chain-work/FU-READS-STRUCK-4.rev1.md:44`) — `plugin/scripting/text_analytics.py:47` — the deleted docstring said selection scope returns "" on failure and does not fall back to the full document. The surviving docstring keeps only "first selection range only". Add the failure half; it is why `or ""` is there.

#### `plugin/scripting/venv/calc_functions_d_h.py`

- **NIT-0066** `plugin/scripting/venv/calc_functions_d_h.py:727,783` (PR #3917 | 2026-11-24 14:40 | opus | `chain-work/FU-CALC-HELPERS-4.rev1.md:26` | rev2: `rev1->fixed:Fixed`) — `plugin/scripting/venv/calc_functions_d_h.py:727,783`: the floor is new behaviour with no reason at the site, unlike `tdist` and `tinv`. Fix: name ScFDist and ScFInv (they floor df with approxFloor before the `< 1` check).

#### `plugin/scripting/venv/calc_functions_i_m.py`

- **NIT-0067** `plugin/scripting/venv/calc_functions_i_m.py` (PR #3984 | 2026-11-24 20:59 | opus | `chain-work/FU-CALC-HELPERS-8.rev1.md:47` | rev2: `rev1->fixed:Fixed.`) — `calc_functions_i_m.py` `_regression_line`: `y_arg` is used once; pass `args[0]`. `docs/calc/py-data-shapes.md`: "the three `#N/A`-index exception types" is stale now that INDEX does not raise; call them the exception types.
- **NIT-0068** `plugin/scripting/venv/calc_functions_i_m.py` (PR #3984 | 2026-11-24 21:07 | opus | `chain-work/FU-CALC-HELPERS-8.rev2.md:26`) — `calc_functions_i_m.py` `ifna` comment — "INT_MIN on x86 or date" lost its noun and half of it no longer applies: the index case raised `IndexError`, which is not caught now. Say "a date built from INT_MIN". End the last clause with "use helpers that return error values".

#### `plugin/scripting/venv/calc_functions_t_z.py`

- **NIT-0069** `plugin/scripting/venv/calc_functions_t_z.py` (PR #3900 | 2026-11-24 13:53 | fable | `fable-structural/352-calc-py-nan-spill.md:18`) — `xlookup` → `#VALUE!` and `xmatch` → plain NaN (`#NUM!`) for the same bad `match_mode`; Calc gives `Err:504` for both. `growth` size mismatch → `#VALUE!`; Calc gives `Err:502`. Since #3900, `formula_error_nan(504)` / `"Err:502"` would be exact.

#### `plugin/scripting/venv/calc_functions_util.py`

- **NIT-0070** `plugin/scripting/venv/calc_functions_util.py` (PR #3984 | 2026-11-24 21:07 | opus | `chain-work/FU-CALC-HELPERS-8.rev2.md:25`) — `calc_functions_util.py` `_OMITTED` — the module docstring says names with a leading underscore are used only inside that module, and it is now imported by `n_s` and `t_z`. Rename it `OMITTED`, and give it the missing one-line reason: a present `None` is a blank cell, so it cannot mean "omitted".
- **NIT-0071** `plugin/scripting/venv/calc_functions_util.py:661` (PR #3917 | 2026-11-24 14:40 | opus | `chain-work/FU-CALC-HELPERS-4.rev1.md:28` | rev2: `rev1->fixed:Fixed`) — `plugin/scripting/venv/calc_functions_util.py:661`: a nested conditional on one 130-plus character line. Fix: use an `if / elif / else`. No behaviour change.

### plugin/vision (2 open, 0 handled)

#### `plugin/vision/vision_egress.py`

- **NIT-0072** `plugin/vision/vision_egress.py:319` (PR #3974 | 2026-11-24 20:06 | opus | `chain-work/FU-VISION-IMAGE-2.rev1.md:29`) — `plugin/vision/vision_egress.py:319` — with `graphic` added, the remaining unnamed refusal has no reason at the site. It is there because without a shape the only target would be whatever is selected now (custom-script route). Add that to the docstring or above the raise.

#### `plugin/vision/vision_runner.py`

- **NIT-0073** `plugin/vision/vision_runner.py:54` (PR #3974 | 2026-11-24 20:06 | opus | `chain-work/FU-VISION-IMAGE-2.rev1.md:27`) — `plugin/vision/vision_runner.py:54` — the docstring says Writer images are looked up by name but not why. The reason is that the Writer insert resolves and presence-checks by name (`insert_vision_result_into_writer`). Without it, a later reader may "finish" the change by keeping Writer shapes too. One clause is enough.

### plugin/writer (31 open, 1 handled)

#### `plugin/writer/apply_document_content.py`

- **NIT-0074** `plugin/writer/apply_document_content.py:818` (PR #3862 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:196`) — `plugin/writer/apply_document_content.py:818` and `:1367` enter `with session, UndoStepOrRollback(...)`, while `:1779` enters them in the opposite order with a comment saying rollback must stay outside the session. `EditReviewSession.__enter__` only toggles `RecordChanges` and the office author, so both orders work; either the comment is stale or the two #3862 sites should match it.

#### `plugin/writer/edit_review.py`

- **NIT-0075** `plugin/writer/edit_review.py:196` (PR #3962 | 2026-11-24 19:01 | opus | `chain-work/FU-REVIEW-FLAGS-2.rev1.md:31`) — `plugin/writer/edit_review.py:196` — the docstring still says "Only annotates the non-wait path". It now also runs on the Wait early return. Say "Only annotates results that did not block for review".
- **NIT-0076** `plugin/writer/edit_review.py:554` (PR #3903 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:190`) — `plugin/writer/edit_review.py:554`: redline ends grouped with `text_group[0].getText() == host`; the rest of the tree uses `uno_same` (`plugin/framework/uno_context.py:513`) for UNO identity. Probe showed `==` is sound for body/cell today; still two idioms for one fact.
- **NIT-0077** `plugin/writer/edit_review.py:572-582` (PR #3883 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:163`) — F6. Multi-cell tracked delete can be untagged while the result says `pending_review: true` (nit, by reading; #3883): Where: `plugin/writer/edit_review.py:572-582`: with several text groups, the anchor goes only into a span whose accepted and rejected text differ. Deleting a row or column of empty cells gives `acc == rej == ""` everywhere, `anchor_span` stays None, the change is untagged (`:607-615`) and has no `ChangeRecord`, yet `tables.py:958-968` still returns `tracked: True, pending_review: True`, and `wait_for_review` returns at once. Degenerate input; one sentence in the docs or a `pending_review` keyed on the session's record count.

#### `plugin/writer/html_export.py`

- **NIT-0078** `plugin/writer/html_export.py` (PR - | 2026-11-24 21:01 | opus | `chain-work/FU-WRITE-MARKUP-2.rev1.md:53`) — `html_export.py` `_picture_urls` / `_picture_url` have no docstring. One line should say why lookup is by name (the `#a_` href is built from all descendant text, including the base64 image, so it cannot be paired by count).
- **NIT-0079** `plugin/writer/html_export.py` (PR - | 2026-11-24 21:01 | opus | `chain-work/FU-WRITE-MARKUP-2.rev1.md:54`) — `_hyperlink_urls` docstring: add why an empty `Frame` with the same URL does not split. The source document reports the text link's URL on the Frame, the scratch copy reports `''`, and that difference is what tells one anchor from two.

#### `plugin/writer/html_import.py`

- **NIT-0080** `plugin/writer/html_import.py` (PR - | 2026-11-24 21:29 | opus | `chain-work/FU-CONTENT-CONTROL-2.rev1.md:21`) — `plugin/writer/html_import.py` `raise_if_hidden_section` docstring — "LibreOffice aborts when such a section starts the body and is deleted" gives no source. Add the pure-UNO reproduction in a few words (paragraph before the section, select all, `setString("")`) so the reason can be rechecked.
- **NIT-0081** `plugin/writer/html_import.py:1287` (PR #3960 | 2026-11-24 18:55 | opus | `chain-work/FU-REPLACE-PARA-INHERIT-2.rev1.md:23`) — `plugin/writer/html_import.py:1287` — the comment stops at `DontExpandFormat`. Adding "which turns paragraph attributes into text attributes" (`FormatToTextAttr`) would let a reader see why formatting is lost.
- **NIT-0082** `plugin/writer/html_import.py:1411` (PR #3987 | 2026-11-24 20:55 | opus | `chain-work/FU-HEADER-TABLES-5.rev1.md:60` | rev2: `rev1->fixed:Fixed`) — `plugin/writer/html_import.py:1411` — "adds empty helper paragraphs before the first table and after the last one" is not what `SwXText::setString` does. It appends one at the very start and one at the very end of the text whenever the text holds any table or section. This matters: it is why a leading helper exists even when only the last element is a table, which is what makes the `tables[-1]` merge safe. Say so.
- **NIT-0083** `plugin/writer/html_import.py:1426,1438` (PR #3987 | 2026-11-24 20:55 | opus | `chain-work/FU-HEADER-TABLES-5.rev1.md:58` | rev2: `rev1->fixed:Fixed`) — `plugin/writer/html_import.py:1426,1438` — `tables and (tables[0] or tables[-1])` is written twice. Name it once (`table_edge = …`) so the two uses cannot drift. A real XText always enumerates something, so if the guard is for mocks, fix the mock.

#### `plugin/writer/html_import_styles.py`

- **NIT-0084** `plugin/writer/html_import_styles.py:768` (PR #3979 | 2026-11-24 20:41 | opus | `chain-work/FU-RECORD-EDGES-3.rev1.md:29`) — `plugin/writer/html_import_styles.py:768` — the docstring line was edited in place and now runs well past its neighbours. Fix: rewrap the paragraph.

#### `plugin/writer/html_ruby.py`

- **NIT-0085** `plugin/writer/html_ruby.py:175` (PR - | 2026-11-24 21:29 | opus | `chain-work/FU-CONTENT-CONTROL-2.rev1.md:17`) — `plugin/writer/html_ruby.py:175` — `if kind is None: break` has no reason at the site; "stopping at the first unreadable portion" used to live in the deleted wrapper's docstring. Add: offsets after an unreadable portion would be wrong, and the caller matches them to the visible text.

#### `plugin/writer/hyperlink_fixup.py`

- **NIT-0086** `plugin/writer/hyperlink_fixup.py:110` (PR #3986 | 2026-11-24 20:58 | opus | `chain-work/FU-HEADING-RENAME-3.rev1.md:22`) — `plugin/writer/hyperlink_fixup.py:110` — a dash after a CJK label is not a separator, so `第一章 — 绪论` is not stripped, and `[第一章 — 绪论, 费用]→[费用, 第一章 — 背景]` still pairs on the shared label. Not a regression (HEAD~1 does the same). Adding `\-–—` to `[\s:：、.．]*` matches the first arm.
- **NIT-0087** `plugin/writer/hyperlink_fixup.py:292` (PR #3903 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:193`) — `plugin/writer/hyperlink_fixup.py:292` `hasattr(para, "supportsService")` (pre-existing, 2c0f0240f) and `heading_links.py:51,73` `isinstance(level, int)` after `uno_read(..., 0)`: guards for states that cannot occur.
- **NIT-0088** `plugin/writer/hyperlink_fixup.py:786-800` (PR #3903 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:201` | HANDLED: `merged:FU-TRACKED-DELETIONS-PERF#3958`) — `retarget_outline_links` (`hyperlink_fixup.py:786-800`) re-walks a paragraph once per link it contains and calls `redline_kinds_covering` (full redline enumeration, `record_changes.py:96-109`) per candidate portion: O(redlines × link portions). Harmless (already-renamed URLs no longer match), cost only.
- **NIT-0089** `plugin/writer/hyperlink_fixup.py:832` (PR #3986 | 2026-11-24 20:58 | opus | `chain-work/FU-HEADING-RENAME-3.rev1.md:24`) — `plugin/writer/hyperlink_fixup.py:832` — `_differ_only_in_number` has no docstring while its two neighbours do. Its non-obvious point is that a case-only difference returns False.

#### `plugin/writer/locale/ai_grammar_proofreader.py`

- **NIT-0090** `plugin/writer/locale/ai_grammar_proofreader.py:184-191` (PR #3975 | 2026-11-24 20:06 | opus | `chain-work/FU-SCRATCH-DOCS-6.rev1.md:31`) — `plugin/writer/locale/ai_grammar_proofreader.py:184-191` — the rewritten docstring joins two unrelated halves with "so". The old text gave the cause: the map has no model on the first call, so it cannot be the lookup. Suggest: "The map holds no model until one is bound here, so the first call resolves the Writer…".

#### `plugin/writer/locale/grammar_obs.py`

- **NIT-0091** `plugin/writer/locale/grammar_obs.py:446` (PR #3828 | 2026-11-23 21:32 | opus | `chain-work/FU-CONCURRENCY-RACES.rev1.md:34` | rev2: `rev1->fixed:Both rewrapped, wording unchanged.`) — over-long reflow: `grammar_obs.py:446` ("terminal phase, paint when … so a prior") and `specialized_catalog.py:351` ("status, code and message and may add edit counts. A dict …") run far past their docstring's wrap. Rewrap.

#### `plugin/writer/page.py`

- **NIT-0092** `plugin/writer/page.py` (PR - | 2026-11-24 21:29 | opus | `chain-work/FU-CONTENT-CONTROL-2.rev1.md:22`) — `plugin/writer/page.py` `_OPAQUE_CURSOR_STEP` comment — "\x01 per step" still describes whole-paragraph `goRight` counting. "One \x01 per field and per struck character" is what the code does.

#### `plugin/writer/range_copy.py`

- **NIT-0093** `plugin/writer/range_copy.py:794` (PR #3987 | 2026-11-24 20:55 | opus | `chain-work/FU-HEADER-TABLES-5.rev1.md:62` | rev2: `rev1->fixed:Fixed`) — `plugin/writer/range_copy.py:794` — `_accepted_blocks` is now the owner of the rule and has no docstring. One line is enough: what Accept All leaves, with `_Block` describing the result.

#### `plugin/writer/search.py`

- **NIT-0094** `plugin/writer/search.py:1737` (PR #3972 | 2026-11-24 19:41 | opus | `chain-work/FU-READS-STRUCK-4.rev1.md:42`) — `plugin/writer/search.py:1737` — "stop the sweep so read failures are not treated as benign empty hits" was changed by word substitution and leaves an odd line break. A read failure now leaves through the `except` above, so it should say why a zero-width hit stops the walk.

#### `plugin/writer/specialized/fields.py`

- **NIT-0095** `plugin/writer/specialized/fields.py` (PR #3962 | 2026-11-24 19:01 | opus | `chain-work/FU-REVIEW-FLAGS-2.rev1.md:33`) — `plugin/writer/specialized/fields.py`, `mail_merge.py`, `indexes.py` — the comment "The review session has already turned recording on in Record/Wait." is repeated three times, and so is the flag pair. The planner deferred a shared helper, so this is acceptable here.

#### `plugin/writer/specialized/indexes.py`

- **NIT-0096** `plugin/writer/specialized/indexes.py:1405-1421` (PR #3962 | 2026-11-24 19:01 | opus | `chain-work/FU-REVIEW-FLAGS-2.rev1.md:35`) — `plugin/writer/specialized/indexes.py:1405-1421` — two `flag_untracked` calls that differ only in the message. Choosing the message first and calling once reads shorter; keep each message as one full `_()` string.

#### `plugin/writer/specialized/textframes.py`

- **NIT-0097** `plugin/writer/specialized/textframes.py:338-395` (PR #3895 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:172`) — F7. Non-inline `frame_insert` with `target="search"` in Record/Wait is half-tracked (nit, by reading; #3895): `plugin/writer/specialized/textframes.py:338-395`: the match deletion is recorded and registered as a `ChangeRecord`; the frame is not; the result says `tracked: false` with "This text frame was inserted directly and is not tracked". The user still has a pending Delete to review and Wait mode waits on it. Documented at docs line 42; the warning should say the replaced text is a pending tracked deletion.

#### `plugin/writer/structural.py`

- **NIT-0098** `plugin/writer/structural.py:215` (PR #3827 | 2026-11-23 21:33 | opus | `chain-work/FU-WRITER-PAGES-OBJECTS.rev1.md:21`) — `plugin/writer/structural.py:215` — `def frame_entry` directly follows the closing `)` of `tables = _scan(...)`. Fix: add a blank line before it.
- **NIT-0099** `plugin/writer/structural.py:236` (PR #3861 | 2026-11-24 13:30 | fable | `fable-structural/348-writer-nav-word-boxes.md:82`) — `_scan_page` reads the same shape's name as `getattr(shape, "Name", "")` (pre-existing, structural.py:236/254) and `shape.getName()` (new, structural.py:283); `Name` is on every XNamed shape so the getattr form is the defensive one AGENTS.md discourages. `page_get_objects` floors millimetres (`// 100`, 40) while `frame_list` keeps two decimals (40.06); pre-existing for images and frames alike.

#### `plugin/writer/styles.py`

- **NIT-0100** `plugin/writer/styles.py:1162` (PR #3961 | 2026-11-24 18:56 | opus | `chain-work/FU-UNO-IMPORTS-2.rev1.md:52`) — "Why kept": the deleted stub-gap comments were the only reason for the workaround in seven files; `styles.py:1162` explains only its own casts. The plan asked for the deletion and a type checker catches a bare `uno.Any`, so no change is required.
- **NIT-0101** `plugin/writer/styles.py:2023` (PR #3903 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:188`) — `plugin/writer/styles.py:2023`: `getattr(state, "value", None)` on a `PropertyState` enum the cursor always returns.
- **NIT-0102** `plugin/writer/styles.py:2549-2550` (PR #3961 | 2026-11-24 19:07 | opus | `chain-work/FU-UNO-IMPORTS-2.rev2.md:25`) — `plugin/writer/styles.py:2549-2550` — "silently skips non-Writer ODF storage packages" reads as "skips ODF packages that are not Writer's". The skip in `LoadStylesFromFile` is for files that are not ODF packages at all (plain text, binary `.doc`, flat `.fodt`). Fix: "silently skips files that are not ODF packages and returns ERRCODE_NONE".
- **NIT-0103** `plugin/writer/styles.py:341-357` (PR #3847 | 2026-11-24 13:38 | fable | `fable-structural/349-writer-range-read.md:110`) — `_capture_paragraphs` (`styles.py:341-357`) keeps `if not hasattr(el, "supportsService")`. A text enumeration only yields paragraphs and tables, both with `supportsService`; the guard exists for the MagicMock tests. It is on the "habits reviews keep removing" list, but it was in the code it replaced too. The per-cell `record_walk_cap` call only writes a note when a cap is hit (`plugin/doc/text_helpers.py:109-117`), so recursion adds no noise.
- **NIT-0104** `plugin/writer/styles.py:500-502` (PR #3903 | 2026-11-24 13:33 | fable | `fable-structural/346-writer-record-gaps.md:182`) — `plugin/writer/styles.py:500-502`: `import uno # local: tests swap sys.modules["uno"]` then `uno_any = getattr(uno, "Any")`. The in-function import reason is a test accommodation, not one of the three allowed reasons; this idiom now appears seven times in `styles.py` (`:32,37,187,500,984,1025,1103,1120`) while `hyperlink_fixup.py:84` imports `uno` at the top. One module-level import and plain `uno.Any` would remove all of them. `getattr` on a guaranteed attribute is the "defensive code for states that can't occur" habit.

#### `plugin/writer/xhtml_style_postprocess.py`

- **NIT-0105** `plugin/writer/xhtml_style_postprocess.py:490` (PR #3987 | 2026-11-24 20:55 | opus | `chain-work/FU-HEADER-TABLES-5.rev1.md:64` | rev2: `rev1->fixed:Fixed`) — `plugin/writer/xhtml_style_postprocess.py:490` — the lookahead `[^\s<&\xa0]` leaves the nbsp when the next text is an entity or a tag (`1 &amp; 2`, `1 <b>of</b>`). It does not grow, but the plan's "a space after a field is a plain space" is narrower than stated. Note it in the comment.

### tests (3 open, 0 handled)

#### `tests/conftest.py`

- **NIT-0106** `tests/conftest.py` (PR #3973 | 2026-11-24 20:03 | opus | `chain-work/FU-TEST-HYGIENE-7.rev1.md:36`) — `tests/conftest.py` `_uno_exception` — `__init__` uses `_UnoRuntimeException`, which is bound only after the function's first two calls. This works because nothing constructs an exception before then. No change needed.
- **NIT-0107** `tests/conftest.py:784` (PR #3967 | 2026-11-24 19:28 | opus | `chain-work/FU-IMAGE-LOG-CAPLOG.rev1.md:13`) — `tests/conftest.py:784` — the restore line has no reason beside it. The reason (a leaked WARNING hides INFO records from later caplog tests) is only in `tests/framework/test_logging.py`. Failure: someone tidying the fixture sees a bare `setLevel`; the regression test would still catch removal, but as a teardown ERROR. Fix: one line above it, e.g. `# init_logging and some tests set this level; a leaked WARNING drops INFO records in later caplog tests.` The plan did not require this.

#### `tests/testing_utils.py`

- **NIT-0108** `tests/testing_utils.py` (PR #3965 | 2026-11-24 19:19 | opus | `chain-work/FU-CARET-INSERT-PIN.rev1.md:36`) — The setup, pin and caret move are repeated in seven new UNO tests across six files (chart, table, page break, two forms, and a search variant in footnote and bookmark). A small helper beside `native_writer_tool_context` in `tests/testing_utils.py` (for example, `pin_selection_and_move_caret(doc, tctx)`) would give the protocol one owner. Optional.

### tests/calc (8 open, 0 handled)

#### `tests/calc/python/test_geometric_recalc.py`

- **NIT-0109** `tests/calc/python/test_geometric_recalc.py` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:50` | rev2: `rev1->fixed:`ensure_geometric_strip_index_for_eval` runs for the copy, and the user's record is asserted.`) — `tests/calc/python/test_geometric_recalc.py` `test_two_documents_with_one_url_keep_separate_strip_state`: it covers the strip read and the clear, but not the load. `ensure_geometric_strip_index_for_eval` for `doc_copy` should leave the `uid-user` records alone.
- **NIT-0110** `tests/calc/python/test_geometric_recalc.py:60-64` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:54` | rev2: `rev1->fixed:fixed`) — `tests/calc/python/test_geometric_recalc.py:60-64`: the nested conditional for `state` can be `WorkbookState.for_doc(doc) if on_main else WorkbookState.known(doc)`; both accept `None`.

#### `tests/calc/python/test_image_egress_uno.py`

- **NIT-0111** `tests/calc/python/test_image_egress_uno.py:829` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:42` | rev2: `rev1->fixed:Per-shape X and owner, in draw order.`) — `tests/calc/python/test_image_egress_uno.py:829`: the RTL test compares sorted X values, while the plan said each plot's X. Two plots that swapped owners and positions would pass. Compare per shape `Name`, or record X by draw order, and assert the owner with `_read_plot_tag`.
- **NIT-0112** `tests/calc/python/test_image_egress_uno.py:903` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:44` | rev2: `rev1->fixed:Name ends in `" 1"`; a second recalc still leaves two shapes.`) — `tests/calc/python/test_image_egress_uno.py:903`: assert that the second shape's name ends in `" 1"` and that a second recalc still leaves two shapes. The count alone does not show that the copy stopped being a slot.

#### `tests/calc/python/test_spill_uno.py`

- **NIT-0113** `tests/calc/python/test_spill_uno.py` (PR #3966 | 2026-11-24 19:16 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev1.md:41` | rev2: `rev1->fixed:Added to the existing UNO test.`) — `tests/calc/python/test_spill_uno.py` — it covers only the late-collision path. The prepare-time release has no native coverage; adding a `prepare_auto_spill` + `flush_for_tests` step would be cheap.
- **NIT-0114** `tests/calc/python/test_spill_uno.py` (PR #3966 | 2026-11-24 19:23 | opus | `chain-work/FU-SPILL-AFTER-REGISTRY.rev2.md:37`) — `tests/calc/python/test_spill_uno.py` `test_blocked_respill_protected_sheet_keeps_record_uno` — it stops at "record kept". Unprotecting, clearing A4 and spilling 4 rows again would pin the outcome that mattered: the formula can reclaim its cells.
- **NIT-0115** `tests/calc/python/test_spill_uno.py:367` (PR #3983 | 2026-11-24 20:50 | opus | `chain-work/FU-SPILL-REMAP.rev1.md:66`) — `tests/calc/python/test_spill_uno.py:367` — the plan named visible A2:A5 strings; the test reads A3:A5 values. This is fine if the add-in is not installed in the runner, since A2 is then an error.

#### `tests/calc/test_array_formula.py`

- **NIT-0116** `tests/calc/test_array_formula.py:135,141` (PR #3980 | 2026-11-24 20:44 | opus | `chain-work/FU-PY-ARRAY-2.rev1.md:41`) — `tests/calc/test_array_formula.py:135,141` — the `["$C$1_REF"]` sentinel and its branch are not needed. `("$C$1", ["B2"], True)` goes through the ordinary comparison. Fix: use the tuple, drop the branch, and type `expected` as `list[tuple[str, list[str], bool]]` (this removes the `Any` import).

### tests/chatbot (2 open, 0 handled)

#### `tests/chatbot/test_dialogs.py`

- **NIT-0117** `tests/chatbot/test_dialogs.py:564` (PR #3969 | 2026-11-24 19:53 | opus | `chain-work/FU-SECRET-LOGS-6.rev1.md:25`) — `tests/chatbot/test_dialogs.py:564` — one test with a four-way `if/elif` on a string id. Four setups under one set of asserts is what the plan asked for and it is readable; no change needed.

#### `tests/chatbot/test_rich_text.py`

- **NIT-0118** `tests/chatbot/test_rich_text.py:461-470` (PR #3961 | 2026-11-24 18:56 | opus | `chain-work/FU-UNO-IMPORTS-2.rev1.md:48`) — `tests/chatbot/test_rich_text.py:461-470` — the fixture patches `sys.modules["uno"]`, while `rich_text.uno` is now bound at import, and this file imports `rich_text` only inside functions. Normal runs also collect `test_rich_text_paste.py`, which imports it at top. A run combining only this file with an in-process `_mock_uno` run first could bind the throwaway mock. No failing order was found. Importing `plugin.chatbot.rich_text` at the top of the test file closes it.

### tests/compute_service (2 open, 0 handled)

#### `tests/compute_service/test_online_py_json_contract.py`

- **NIT-0119** `tests/compute_service/test_online_py_json_contract.py` (PR #3985 | 2026-11-24 21:01 | opus | `chain-work/FU-COMPUTE-DOCS-1124E.rev1.md:20`) — same test — the old comment also said the worker dict is hand-built after `parse_execute_request`. That half was still true and explains why the test does not use the HTTP path. One clause could stay.
- **NIT-0120** `tests/compute_service/test_online_py_json_contract.py:146` (PR #3985 | 2026-11-24 21:01 | opus | `chain-work/FU-COMPUTE-DOCS-1124E.rev1.md:19`) — `tests/compute_service/test_online_py_json_contract.py:146` — "(Gerrit 8123) … flattens": the flattening is in the AddIn (8122); 8123 does the POST. The README splits them correctly. Suggest "(Gerrit 8122, 8123)".

### tests/draw (5 open, 0 handled)

#### `tests/draw/test_shape_clone_uno.py`

- **NIT-0121** `tests/draw/test_shape_clone_uno.py:60-72` (PR #3975 | 2026-11-24 20:06 | opus | `chain-work/FU-SCRATCH-DOCS-6.rev1.md:35`) — `tests/draw/test_shape_clone_uno.py:60-72` — `except RuntimeError: pass` would also accept "Unsupported shape type", which is raised before `add`; the count assertion would then pass vacuously. Check the message contains "injected Position failure". Move the `raise AssertionError` out of the `try` while there.

#### `tests/draw/test_shapes.py`

- **NIT-0122** `tests/draw/test_shapes.py` (PR #3971 | 2026-11-24 19:56 | opus | `chain-work/FU-GROUPED-SHAPES-2.rev1.md:25`) — `tests/draw/test_shapes.py` (`out_both` case, `x=3000, width=-400`) — does not assert that nothing was changed, unlike the two cases above it. Add `setPosition.assert_not_called()` and `setSize.assert_not_called()`.

#### `tests/draw/test_shapes_uno.py`

- **NIT-0123** `tests/draw/test_shapes_uno.py` (PR #3971 | 2026-11-24 19:56 | opus | `chain-work/FU-GROUPED-SHAPES-2.rev1.md:27`) — `tests/draw/test_shapes_uno.py` (reloaded group, child `"0.1"` with `width=-1500`) — pins only the absolute size. The reason group children are exempt is that their x really moves, and this edit does not check it. Record `child.getPosition().X` before and assert it dropped by about 1500, with a wider tolerance than the size check if conversion needs it.
- **NIT-0124** `tests/draw/test_shapes_uno.py` (PR #3971 | 2026-11-24 19:56 | opus | `chain-work/FU-GROUPED-SHAPES-2.rev1.md:29`) — `tests/draw/test_shapes_uno.py` (same block, align `["0.0", "0.1"]` left) — asserts equality but not that the two x values differed beforehand. From the fixture (x=1000 and 5000) plus the edits they should differ, but one `!=` assert before the call makes the test impossible to pass vacuously.
- **NIT-0125** `tests/draw/test_shapes_uno.py` (PR #3971 | 2026-11-24 19:56 | opus | `chain-work/FU-GROUPED-SHAPES-2.rev1.md:31`) — `tests/draw/test_shapes_uno.py` (`test_writer_inline_shape_align_distribute_uno`) — checks `code` but not `details["parameter"] == "indices"`. It is pinned in the unit test, so this is optional.

### tests/framework (1 open, 0 handled)

#### `tests/framework/client/test_http_security.py`

- **NIT-0126** `tests/framework/client/test_http_security.py` (PR #3908 | 2026-11-24 14:21 | opus | `chain-work/FU-SECRET-LOGS-4.rev1.md:26`) — `tests/framework/client/test_http_security.py` `test_invalid_url_traceback_hides_userinfo` — `"https://user:hunter2" + "@ho℀st/x"` is split for no stated reason. Use one literal, or write the reason.

### tests/ppt_master (1 open, 0 handled)

#### `tests/ppt_master/test_uno_pptx_import.py`

- **NIT-0127** `tests/ppt_master/test_uno_pptx_import.py:583,609` (PR #3975 | 2026-11-24 20:06 | opus | `chain-work/FU-SCRATCH-DOCS-6.rev1.md:37`) — `tests/ppt_master/test_uno_pptx_import.py:583,609` and `tests/draw/test_shape_clone.py:424` — `FailingPosShape` is written three times. Put it once in `tests/draw/shape_fakes.py`.

### tests/scripting (6 open, 0 handled)

#### `tests/scripting/test_calc_functions_a_c.py`

- **NIT-0128** `tests/scripting/test_calc_functions_a_c.py` (PR #3984 | 2026-11-24 20:59 | opus | `chain-work/FU-CALC-HELPERS-8.rev1.md:46` | rev2: `rev1->fixed:Fixed.`) — `tests/scripting/test_calc_functions_a_c.py`: `assert calc.countif([1, 2, 3], NA_NAN) != 0` is true for any NaN; the next line already pins it. Delete it.

#### `tests/scripting/test_calc_functions_n_s.py`

- **NIT-0129** `tests/scripting/test_calc_functions_n_s.py:600` (PR #3917 | 2026-11-24 14:40 | opus | `chain-work/FU-CALC-HELPERS-4.rev1.md:30` | rev2: `rev1->fixed:Fixed`) — `tests/scripting/test_calc_functions_n_s.py:600`: one test covers five helpers, so the first failure hides the rest. `import struct` is inside the function, and there are extra blank lines at the end of the file. Split it per helper and move the import to the top. Also pin text × NaN (`sumproduct(["text"], [calc.na()]) == 0`), which the plan lists as unchanged; only the text × token form is tested.

#### `tests/scripting/test_calc_functions_util.py`

- **NIT-0130** `tests/scripting/test_calc_functions_util.py` (PR #3984 | 2026-11-24 21:07 | opus | `chain-work/FU-CALC-HELPERS-8.rev2.md:27`) — `tests/scripting/test_calc_functions_util.py` `test_propagate_errors` — still imports `DIV0_NAN` and `nan_error_code` in the body, though both are now imported at the top of the file.

#### `tests/scripting/test_editor_shell.py`

- **NIT-0131** `tests/scripting/test_editor_shell.py` (PR #3964 | 2026-11-24 19:09 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev1.md:46` | rev2: `rev1->fixed:See finding 2.`) — `tests/scripting/test_editor_shell.py`: nothing pins that `load` and `load_follows` clear `pendingNew`. Deleting either line (`scripts_manager.js:193, 324`) passes. One scenario is enough: New, then `load` of another session with the same script and text, then a list that holds the template under that key.
- **NIT-0132** `tests/scripting/test_editor_shell.py` (PR #3964 | 2026-11-24 19:18 | opus | `chain-work/FU-CALC-PY-FIXES-5.rev2.md:31`) — `tests/scripting/test_editor_shell.py` scenario 6: the `load_follows` variant also delivers the `load` in the same batch, and `load` clears `pendingNew` too. So deleting `scripts_manager.js:193` still passes; only line 325 is pinned. Fix: for that variant, skip the `load` and deliver the `s1` list with `B` right after `load_follows`, then assert the editor stays on A.

#### `tests/scripting/test_text_analytics.py`

- **NIT-0133** `tests/scripting/test_text_analytics.py:767` (PR #3972 | 2026-11-24 19:41 | opus | `chain-work/FU-READS-STRUCK-4.rev1.md:48`) — `tests/scripting/test_text_analytics.py:767` — `get_selection_text` is patched, so the controller and selection parts of that `FakeDoc` are dead. Keep only what `get_doc_type`/language needs.

### tests/scripts (2 open, 0 handled)

#### `tests/scripts/test_string_eval_tools.py`

- **NIT-0134** `tests/scripts/test_string_eval_tools.py` (PR #3982 | 2026-11-24 20:46 | opus | `chain-work/FU-EVAL-CLEAN-5.rev1.md:22`) — `tests/scripts/test_string_eval_tools.py` — three parametrized tests repeat a fixed success check in every case: `clear_direct="none"` 5 times, empty `full_document` 12 times, `[["a", null]]` 4 times. Fix: move each to its own test, so a failure there names the right behaviour.

#### `tests/scripts/test_toc_eval.py`

- **NIT-0135** `tests/scripts/test_toc_eval.py` (PR #3982 | 2026-11-24 20:46 | opus | `chain-work/FU-EVAL-CLEAN-5.rev1.md:23`) — `tests/scripts/test_toc_eval.py` `_finish` — it calls the private `llm_chat_eval._trace_entry`. It was planned, and it is better than hand-built entries because the test goes through the production builder. No change needed.

### tests/writer (27 open, 0 handled)

#### `tests/writer/math/test_math_formula_insert_uno.py`

- **NIT-0136** `tests/writer/math/test_math_formula_insert_uno.py:741` (PR #3979 | 2026-11-24 20:41 | opus | `chain-work/FU-RECORD-EDGES-3.rev1.md:25`) — `tests/writer/math/test_math_formula_insert_uno.py:741` — the new test's merging has no stated reason. Recording and undo can leave a paragraph's text in several portions while the test only cares about text versus formula order. Fix: say so in one line, if that is what the coder saw.
- **NIT-0137** `tests/writer/math/test_math_formula_insert_uno.py:741-756` (PR #3979 | 2026-11-24 20:41 | opus | `chain-work/FU-RECORD-EDGES-3.rev1.md:23`) — `tests/writer/math/test_math_formula_insert_uno.py:741-756` — this is a second copy of the merged helper, and `_paras` is also copied. Fix: once finding 1 is settled, keep one merging helper for the new test. If both tests need merging, make it one module-level function.

#### `tests/writer/specialized/test_bookmarks_uno.py`

- **NIT-0138** `tests/writer/specialized/test_bookmarks_uno.py` (PR #3965 | 2026-11-24 19:19 | opus | `chain-work/FU-CARET-INSERT-PIN.rev1.md:30`) — `tests/writer/specialized/test_bookmarks_uno.py` and `test_footnotes_uno.py` (new tests) — `setString("Alpha beta gamma\nSecond para")` makes one paragraph. `DocInsertStringSplitCR` splits only on `\r` (`sw/source/core/unocore/unocrsrhelper.cxx:1205`). The asserts still hold, but the plan asks for two paragraphs. Use `insertControlCharacter` as the other new tests do.

#### `tests/writer/specialized/test_forms.py`

- **NIT-0139** `tests/writer/specialized/test_forms.py:729` (PR #3965 | 2026-11-24 19:19 | opus | `chain-work/FU-CARET-INSERT-PIN.rev1.md:28`) — `tests/writer/specialized/test_forms.py:729` — `test_draw_form_generate_interleaved_text_shares_cursor` gained `selection_pin=None`. It is a Draw path that never reads the pin, and it is not one of the plan's 14. Revert it.

#### `tests/writer/specialized/test_forms_uno.py`

- **NIT-0140** `tests/writer/specialized/test_forms_uno.py` (PR #3965 | 2026-11-24 19:19 | opus | `chain-work/FU-CARET-INSERT-PIN.rev1.md:34`) — `tests/writer/specialized/test_forms_uno.py` (`test_form_generate_recorded_over_selection_keeps_text_uno`) — builds `DispatchHelper` by hand. The test just above it uses `execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")`; use that.
- **NIT-0141** `tests/writer/specialized/test_forms_uno.py:119` (PR #3973 | 2026-11-24 20:03 | opus | `chain-work/FU-TEST-HYGIENE-7.rev1.md:30`) — `tests/writer/specialized/test_forms_uno.py:119` — the new comment sits under "# Verify draw page count increased", which describes neither line. Pre-existing, but now two unrelated comments are stacked. Fix: replace the older line with what is checked (control names).

#### `tests/writer/specialized/test_indexes.py`

- **NIT-0142** `tests/writer/specialized/test_indexes.py` (PR #3962 | 2026-11-24 19:01 | opus | `chain-work/FU-REVIEW-FLAGS-2.rev1.md:37`) — `tests/writer/specialized/test_indexes.py` and `tests/writer/test_edit_review.py` — extra blank lines at end of file.

#### `tests/writer/specialized/test_textframes_uno.py`

- **NIT-0143** `tests/writer/specialized/test_textframes_uno.py:352` (PR #3973 | 2026-11-24 20:03 | opus | `chain-work/FU-TEST-HYGIENE-7.rev1.md:28`) — `tests/writer/specialized/test_textframes_uno.py:352` — the list now keeps every shape proxy alive through `reloaded.close(True)` in the `finally`; before, only the matched `shape` was kept. `tests/testing_utils.py` (comment above `_CLOSE_DOC_URP_SETTLE_S`) names held SvxShape proxies at close as a soak amplifier. Failure: possible, not shown; the plan's UNO probe of this file passed. Fix: optional `del shapes` after the `next(...)`. `shape` itself is needed later. The forms and draw lists end with the test function, before fixture close.

#### `tests/writer/test_html_export.py`

- **NIT-0144** `tests/writer/test_html_export.py` (PR - | 2026-11-24 21:23 | opus | `chain-work/FU-WRITE-MARKUP-2.rev2.md:47`) — `tests/writer/test_html_export.py` ends with a blank line.

#### `tests/writer/test_html_export_uno.py`

- **NIT-0145** `tests/writer/test_html_export_uno.py` (PR - | 2026-11-24 21:23 | opus | `chain-work/FU-WRITE-MARKUP-2.rev2.md:39`) — `tests/writer/test_html_export_uno.py` — `id="RectText"[^>]*>.*?TEXT-R1` with `DOTALL` also matches text after the wrapper closes. Use `id="RectText"[^>]*>(?:(?!</span>|</div>).)*TEXT-R1`.

#### `tests/writer/test_html_import.py`

- **NIT-0146** `tests/writer/test_html_import.py:175` (PR #3979 | 2026-11-24 20:41 | opus | `chain-work/FU-RECORD-EDGES-3.rev1.md:27`) — `tests/writer/test_html_import.py:175` — `assert "a\nb" in imported[0] or "a<br" in imported[0]`. The content has a `<p>`, so `ensure_html_linebreaks` takes the block-markup branch and does not turn newlines into `<br>`. The second alternative is an unneeded case. Fix: assert `"a\nb" in imported[0]` only, as the plan said.

#### `tests/writer/test_html_import_uno.py`

- **NIT-0147** `tests/writer/test_html_import_uno.py` (PR #3960 | 2026-11-24 18:55 | opus | `chain-work/FU-REPLACE-PARA-INHERIT-2.rev1.md:24`) — `tests/writer/test_html_import_uno.py`, `test_full_document_undo_restores_formatting_uno` — `_state` skips empty paragraphs (as planned), so an extra empty paragraph after Undo would pass. Also asserting `len(_body_paragraphs(doc)) == len(words)` after Undo would pin that.
- **NIT-0148** `tests/writer/test_html_import_uno.py` (PR #3960 | 2026-11-24 18:55 | opus | `chain-work/FU-REPLACE-PARA-INHERIT-2.rev1.md:25`) — `tests/writer/test_html_import_uno.py`, `test_full_document_undo_restores_formatting_uno` — the asserts inside the nested loops carry no `(char_style, words)` message, so a failure does not name its case. The sibling test does this.
- **NIT-0149** `tests/writer/test_html_import_uno.py:3188` (PR #3976 | 2026-11-24 20:16 | opus | `chain-work/FU-RECORD-STYLE-6.rev1.md:23`) — `tests/writer/test_html_import_uno.py:3188` — `_seed(empty_first=False)` spells out the default. Fix: `_seed()`.

#### `tests/writer/test_hyperlink_fixup.py`

- **NIT-0150** `tests/writer/test_hyperlink_fixup.py:910-939` (PR #3986 | 2026-11-24 21:09 | opus | `chain-work/FU-HEADING-RENAME-3.rev2.md:24`) — `tests/writer/test_hyperlink_fixup.py:910-939` — the five new cases follow the "English number words" comment, but four are CJK and one is number-only. Move each under its own heading, or add a comment.

#### `tests/writer/test_image_placeholders.py`

- **NIT-0151** `tests/writer/test_image_placeholders.py` (PR - | 2026-11-24 21:01 | opus | `chain-work/FU-WRITE-MARKUP-2.rev1.md:56`) — `test_image_placeholders.py` `_run`: use `dict(c.args for c in orig.setPropertyValue.call_args_list)`, as the next test already does.

#### `tests/writer/test_navigation_uno.py`

- **NIT-0152** `tests/writer/test_navigation_uno.py:148` (PR #3841 | 2026-11-24 13:38 | fable | `fable-structural/351-writer-at-page-frames.md:56`) — 6. Test style nit — convention gap, not this PR's: `test_get_surroundings_frames_include_text_boxes_uno` walks paragraphs with a raw `while para_enum.hasMoreElements()` (`tests/writer/test_navigation_uno.py:148`). `AGENTS.md` asks for `enum_has_more` on all `XEnumeration` walks; 19 other `*_uno.py` files do the same raw walk, and it terminates on a real document, so this is a repo-wide convention question rather than a regression.

#### `tests/writer/test_page_header_html_uno.py`

- **NIT-0153** `tests/writer/test_page_header_html_uno.py:1628` (PR #3987 | 2026-11-24 20:55 | opus | `chain-work/FU-HEADER-TABLES-5.rev1.md:68` | rev2: `rev1->fixed:Fixed`) — `tests/writer/test_page_header_html_uno.py:1628` — the comment says "at most one leading blank" but the assert is exact; and the `empty` case makes no Reject assertion (the plan only promised constant length there, but a line saying so helps).

#### `tests/writer/test_page_uno.py`

- **NIT-0154** `tests/writer/test_page_uno.py` (PR - | 2026-11-24 21:29 | opus | `chain-work/FU-CONTENT-CONTROL-2.rev1.md:19`) — `tests/writer/test_page_uno.py` — the frame case asserts only `endswith(" ZZ")`, while the plan asked for the final string. It also does not pin that "xx" survives. The refusal across a control edge, which `page-api-reference.md` now documents, has no test. Add one: needle `x CT` over `xx [CTRL] yy` gives an error and unchanged text.
- **NIT-0155** `tests/writer/test_page_uno.py:1285` (PR #3987 | 2026-11-24 20:55 | opus | `chain-work/FU-HEADER-TABLES-5.rev1.md:66` | rev2: `rev1->fixed:Fixed`) — `tests/writer/test_page_uno.py:1285` `test_replace_header_after_inline_logo_uno` — asserts `NewText` is present but not that `OldText` is gone.

#### `tests/writer/test_range_copy.py`

- **NIT-0156** `tests/writer/test_range_copy.py` (PR #3987 | 2026-11-24 21:15 | opus | `chain-work/FU-HEADER-TABLES-5.rev2.md:29`) — `tests/writer/test_range_copy.py` and `tests/writer/specialized/test_tables.py:835` — `TrackedDeletions` is imported inside each of the five tests, in one place between setup and the call. One import at the top of each file is simpler. Also, the branch has two commits with the same subject; squash them.

#### `tests/writer/test_search.py`

- **NIT-0157** `tests/writer/test_search.py` (PR - | 2026-11-24 21:29 | opus | `chain-work/FU-CONTENT-CONTROL-2.rev1.md:20`) — `tests/writer/test_search.py` `test_find_text_ranges_skips_unmeasurable_hits_and_fills_limit` patches the private `_iter_descriptor_hits`. The call-count bound needs `read_visible_body` patched, but the hits could come from a fake `findFirst`/`findNext`.

#### `tests/writer/test_search_uno.py`

- **NIT-0158** `tests/writer/test_search_uno.py` (PR #3972 | 2026-11-24 19:41 | opus | `chain-work/FU-READS-STRUCK-4.rev1.md:40`) — `tests/writer/test_search_uno.py` `test_search_skips_hit_struck_by_two_deletions_uno` — the last assert (`compareRegionStarts(selected, text start) < 0`) is also true for the struck hit at offset 3. Only the `visible_range_text` assert pins anything. Compare with the live hit's visible offset, or drop the line.
- **NIT-0159** `tests/writer/test_search_uno.py` (PR - | 2026-11-24 21:29 | opus | `chain-work/FU-CONTENT-CONTROL-2.rev1.md:18`) — `tests/writer/test_search_uno.py` `test_find_text_ranges_metadata_and_input_field_limit_uno` — `rows[0]["start"] > 3` is loose. The plan's probe gives an exact row; pin `{"start": 6, "end": 8, "text": "yy"}`.

#### `tests/writer/test_selection_uno.py`

- **NIT-0160** `tests/writer/test_selection_uno.py:154` (PR #3972 | 2026-11-24 19:41 | opus | `chain-work/FU-READS-STRUCK-4.rev1.md:46`) — `tests/writer/test_selection_uno.py:154` — copies `_build_chain_deletion_doc` line by line with a mid-function import. "Reuse the shape" was allowed, but a shared helper would keep one copy.

#### `tests/writer/test_styles.py`

- **NIT-0161** `tests/writer/test_styles.py` (PR #3928 | 2026-11-24 15:47 | opus | `chain-work/FU-STYLES-UNO-IMPORT.rev1.md:25`) — `tests/writer/test_styles.py`, `test_styles_uno.py` and `test_apply_document_content_heading_links_uno.py` — each ends in extra blank lines. Trim to one newline; the last one goes away with finding 1.

#### `tests/writer/test_track_changes_reviewable_uno.py`

- **NIT-0162** `tests/writer/test_track_changes_reviewable_uno.py:643` (PR #3976 | 2026-11-24 20:16 | opus | `chain-work/FU-RECORD-STYLE-6.rev1.md:22`) — `tests/writer/test_track_changes_reviewable_uno.py:643` — `_body_styles_and_texts` is close to the file's `_paragraphs` (line 1552) and `_all_paras` (979). Fix: reuse one if its shape fits; otherwise leave it.
