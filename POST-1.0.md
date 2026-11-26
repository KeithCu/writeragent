# Post-1.0 follow-on list

Written Tue Nov 24, 2026 (America/Detroit). Leftover follow-up and audit
items, kept here so they are not lost while 1.0 work continues. This is a
pointer, not a queue. The full text lives under
[docs/post-1.0/](docs/post-1.0/).

Items already queued from the 24 Nov audits are omitted: Writer safety,
default-path review, performance, simplify, and real bugs E19-24, plus the
agreed non-Writer A, B, C, E, and F work. Row numbers are `followups.tsv`
ids from the 1124f proposal.

Sources copied into `docs/post-1.0/`:

- [followup-loop-audit-2026-11-24.md](docs/post-1.0/followup-loop-audit-2026-11-24.md) (Writer)
- [followup-loop-audit-nonwriter-2026-11-24.md](docs/post-1.0/followup-loop-audit-nonwriter-2026-11-24.md)
- [FOLLOWUPS-PROPOSAL-1124f.md](docs/post-1.0/FOLLOWUPS-PROPOSAL-1124f.md)
- [OLD-NITS-0.9.6.md](docs/post-1.0/OLD-NITS-0.9.6.md)

Of the 40 skipped 1124f rows, restatements and already-shipped items are
omitted. Accepted limitations and the three carried deferred rows are
kept below. FU-HEADING-RENAME-4 was dropped in favor of the one-rule
heading retarget now in the pre-1.0 queue; revisit leftover
numeral/ordinal edges only if that work still leaves real misses.

## Writer

Leftovers from the 1124f Writer bundles after E19-24 and the other
queued audit slices.

- Paragraph-background properties can lose their values on save, the same
  way the already-fixed property did (FU-WRITER-SMALL-1124F row 2324).
- A heading imported over a different list inherits that list's restart
  and level (FU-WRITER-SMALL-1124F row 2347).
- Empty cells of a struck row stay unmarked (FU-STRUCK-READS-2 row 2332;
  2345 is the same case).
- A deletion that ends on an `AT_PARAGRAPH` image is marked only in full
  reads (FU-STRUCK-READS-2).
- `table_visible_text` still treats a Delete-Table'd table as blank cells
  (FU-STRUCK-READS-2).
- Images in table cells are cloned without the `removes_anchored` filter
  (FU-STRUCK-READS-2).
- `table_is_struck` ignores a live untracked image in an already struck
  table (FU-STRUCK-READS-2).
- Summary `paragraph_count` nit on that same import path (FU-STRUCK-READS-2
  row 2331).
- Accept can drop a frame when a recorded whole-text search match is the
  anchor (type 0 or 4) (FU-REVIEW-FLAGS-3 rows 2354 / 2168).
- Redo still drops the review tag; re-tag if a practical hook exists
  (FU-REVIEW-FLAGS-3 rows 2355 / 2180).
- The `table_insert` caret-in-cell hint never matches LibreOffice's
  message (FU-CARET-INSERT-2).
- Restore the selection pin after a rolled-back edit (FU-CARET-INSERT-2).
- `chart_upsert(position="cursor")` silently goes to the document end when
  the range is in a cell, frame, or header (FU-CARET-INSERT-2).
- Wording nits in the same bundle: chart "view cursor", footnote
  "insertion point" (FU-CARET-INSERT-2 rows 2389, 2390).
- `apply_content` dry_run, comment anchors, and index entries still report
  raw hit text (FU-READS-STRUCK-5).
- Search `context` omits MetadataField text (FU-READS-STRUCK-5).
- Ctrl+A with a leading table still reads only the table
  (FU-READS-STRUCK-5).
- Astral text before a range that starts inside an Input or MetadataField
  is the leftover limit of the last fix (FU-READS-STRUCK-5).
- A failed insert inside `record_mutation` can stay partly written
  (FU-RECORD-STYLE-7, after #3942).
- Track-delete the leftover empty paragraph when the next paragraph can
  support it (FU-RECORD-STYLE-7).
- Group the 6-7 undo steps of display math when recording is off
  (FU-RECORD-STYLE-7).
- `math_run_symbolic` records in Record/Wait but still has no
  pending-review note for the model (FU-RECORD-STYLE-7).
- A recorded search-replace of a heading with display math leaves the
  struck heading in place, so Accept All can leave an empty Heading 1
  (FU-RECORD-EDGES-4). Needs its own design.
- Inserting display math at the start of a heading can drop a direct page
  break, or leave an empty heading above the formula (FU-RECORD-EDGES-4).
- Header and footer writes still do not turn TeX and MathML into formula
  objects (FU-RECORD-EDGES-4).
- Recorded header writes still leave blank paragraphs on Reject All or
  Accept All (FU-HEADER-TABLES-6).
- Untracked header writes can carry leftover direct formatting
  (FU-HEADER-TABLES-6).
- Header cell reads do not join partly struck paragraphs
  (FU-HEADER-TABLES-6).
- Range and selection reads still hide an `AT_CHARACTER` logo at a
  deletion's end (FU-HEADER-TABLES-6).
- A live blank header paragraph round-trips as `\xa0` (FU-HEADER-TABLES-6).
- `page_replace_header_footer_text` does not search content-control text
  (FU-HEADER-TABLES-6).
- A paragraph whose only pre-deletion content is an `AT_CHARACTER` frame
  is treated as live for style choice (FU-HEADER-TABLES-6).
- Search header labels miss matches in a header table cell or frame
  (FU-HEADER-TABLES-6).
- Put an embedded object back on write, or drop its wrapper marker when
  the object survives, instead of refusing (FU-WRITE-MARKUP-3).
- Text in `draw:custom-shape` / `draw:g` never appears in full reads
  (FU-WRITE-MARKUP-3).
- A text-frame or bare-shape wrapper write-back can split the host
  paragraph and shift following styles (FU-WRITE-MARKUP-3).
- Header, footer, and range reads skip `simplify_drawings`, so bare
  shapes or OLE may still leak there (FU-WRITE-MARKUP-3).
- Check the header unlinked-inline picture URL if #3988 did not already
  close it (FU-WRITE-MARKUP-3).
- Chart series / `embedded_edit` stubs (row 2464) only if they stay a
  small read-side fix (FU-WRITE-MARKUP-3).
- UNO exceptions that cross a `contextlib` generator still become
  `RuntimeException` (FU-CONTENT-CONTROL-3).
- `math_mml_export._xhtml_id` misses OLE names with `-` or non-ASCII
  (FU-CONTENT-CONTROL-3).
- `full_document` can lose a leading plain section or rename sections
  (FU-CONTENT-CONTROL-3).
- Pass error codes through `form_generate`, and keep a useful step on
  `rich_html` import failures (FU-CONTENT-CONTROL-3).
- Unify the leftover `goRight` in `find_tokens` while that file is open
  (FU-CONTENT-CONTROL-3).

## Calc

- Remaining helper-parity wave in FU-CALC-HELPERS-9 after the four rows
  already queued (audit task 8): domain and error codes still listed
  under 2315-2319, ISEVEN/ISODD of an error, lookup and text-function
  mismatches, blank-cell `str()` cases, and `@propagate_errors` on the
  remaining distribution helpers (rows 2339, 2340, 2343, 2469-2480,
  2506-2509, 2518, minus the queued four).
- Shrink the Calc helper list only if the importer prototype is revived
  (non-Writer audit item 28).
- A native array formula that reads its own empty-growth anchor is
  accepted and shows zeros (FU-PY-ARRAY-3).
- `PyFormula.parse` can swallow a trailing `+SUM(...)` or `+TAKE(...)`
  as part of one `=PY` call, and drop an unknown-function hint
  (FU-PY-ARRAY-3 row 2454; 2482 is the same case).
- Nested `=PY` leftovers in FU-PY-ARRAY-3 (list returns before
  LibreOffice 26.8, generic `Error: ...` treated as ok, nested writes
  over their own data): revisit only if anything remains after the
  queued nested-`=PY` cut (non-Writer audit C13).
- A deferred spill can inherit a stale record at the origin before
  `cleanup_orphans` (FU-SPILL-EDGES).
- A sheet renamed before the open hook ran keeps the old name
  (FU-SPILL-EDGES).
- After #3990, include the timer-order clear only if that case is still
  open (FU-SPILL-EDGES row 2462).
- `forget_key` / `clear` should `removeModifyListener` (FU-SPILL-EDGES).
- A New-list update while the Save box is open can drop the unsaved
  script edit (FU-CALC-PY-SCRIPTS row 2378).
- A failed Delete also replaces the script editor text
  (FU-CALC-PY-SCRIPTS row 2351).
- With the flag on, opening a hidden research copy still reconciles
  formulas and can show the cap-hit box for that hidden document
  (FU-CALC-PY-SCRIPTS row 2353).
- Remaining Python traceback follow-ups after the two merged traceback
  commits: chained exceptions and `<lambda>` / `<genexpr>` frames on
  thread-pool timeouts (rows 1508, 1543), `return` through a failing
  `__exit__`, and the `except 5:` message (FU-PY-TRACEBACKS-2
  follow-ups).

## Draw and shapes

- Vertical align/distribute still compares coordinates from mixed anchor
  frames (inline vs page, and so on) (FU-GROUPED-SHAPES-3).
- Geometry on Writer `FrameShape` entries still raises a raw LibreOffice
  error, including for an inline picture edited with `x` or a negative
  width, because `getPosition()` runs before the new check
  (FU-GROUPED-SHAPES-3).
- The partial-group warning should also name mark-protected objects and
  objects on another page (FU-GROUPED-SHAPES-3).
- Merge the three near-copy rows in `docs/writer/reviewable-agent-edits.md`
  (FU-GROUPED-SHAPES-3).

## Framework, scripting, and logs

- The CORS forbidden-`Host` warning can log a stale tunnel host
  (FU-SECRET-LOGS-7 row 2407; 2412 is the same line). The pre-1.0 work
  extends `RedactingFormatter` instead of this bundle.
- `_full_url_for_request_path` can leak part of an endpoint password that
  contains a slash, if such a URL is accepted (FU-SECRET-LOGS-7).
- A UI-thread bind still waits out the 1.5 s failed-bind backoff even
  though it needs no thread hop (FU-SCRATCH-DOCS-7).
- An embedded OLE Writer edited in place can bind the host document
  under the embedded document's id, after which the host's own id can
  never bind (FU-SCRATCH-DOCS-7; read from the code).
- `test_pool_cancel_before_child_published` overlaps the newer
  deterministic test and can land the cancel too early (FU-WARMCHILD-4
  row 2460). The handshake-cancel hang is already queued.
- Hoist the remaining ~60 function-local `import uno` sites, and the
  `com.sun.star...` imports in `image_mark.py`, `page_png.py`, and
  `shapes.py`, after the stubs are in place (FU-UNO-IMPORTS-3).
- Run `generate_tool_proxies.py` `main` in a child process in the tests
  so a throwaway `uno` cannot leak (FU-UNO-IMPORTS-3).
- Add the missing `load_frame_styles` and `load_numbering_styles`
  parameters to `docs/writer/llm-styles.md` (FU-UNO-IMPORTS-3).

## Tests, CI, and eval

- Skip FU-EVAL-CLEAN-6 as a bundle: the string-harness `apply_style`
  still restyles only Default paragraphs on `full_document`, accepts
  unknown style names, and wraps bare list or table markup;
  `apply_content` stringifies non-string list items instead of returning
  `INVALID_PARAM`; a bare line with an entity and no tags is not decoded
  (rows 2456-2459, 2485, 2486). Fidelity belongs in the real-LibreOffice
  lane after `eval_worlds.py` is frozen.
- `parse_table_source_spec` still `str()`s a bool, list, or dict in
  `tables[].file` / `sheet` / `named_range` / `range` and names the
  Python repr (FU-CLEANUP-8). The page-index wording work is already
  queued; skip the rest of that bundle.
- `DrawBridge.get_active_page_index` reports 0 on a total miss and now
  also says "the user's page 1"; report a miss instead (FU-CLEANUP-8).
- Seed the LibreOffice `.deb` cache from a master or scheduled run so
  PRs stop storing about 287 MB each; move `actions/cache@v4` to a
  Node 24 release; use the same pinned OpenGrep version on
  macOS/Windows CI and `make opengrep-install` (FU-CI-CACHE rows 2396,
  2397, 2398, 2403).
- After the orphan-soffice flake fix, check whether Windows recycle
  leaves `soffice.bin` behind, drop the redundant second `_GEO_FLAG_KEY`
  write, and `wait()` after a kill so profile delete does not race the
  process (FU-TEST-PROFILE-3).
- Leftover log-hygiene nits after the release-verify deselect and flaky
  pool test: root-logger leak, split ownership of `writeragent` logger
  state, leftover `writeragent.test.*` loggers, and mock-LLM word-list
  markers (`[tracked deletion]`) (FU-TEST-LOG-HYGIENE).
- Bind same-shape refetch lists in `test_image_egress_uno.py` if those
  calls are still unbound, and add a small lint for partial UNO
  exception constructors (FU-TEST-HYGIENE-8). The `getByIndex` deaths
  and the master-failing `test_py_addin_token_formula_errors` case are
  already queued.

## Docs

- Stale compute/Online status on master after #3985: `numpy-jailsafe.md`
  still says F3/F6/F7 are "not yet on Gerrit" / "not coded", AddIn
  `timeout_ms` is still called "deferred", `calc_range.py` and
  `formula_worker.py` comments state unmerged Gerrit behaviour as fact,
  and the README labels `stateful_session` with `mode="shared"`, which
  the service ignores (FU-COMPUTE-DOCS-1124F rows 2495-2497, 2513-2516).
- When Gerrit 19188 merges, delete `json_peel.py` and the peel branch;
  when 21286 merges, drop "in review" from `py-data-shapes.md`
  (FU-COMPUTE-DOCS-1124F).
- Pin the multi-argument flat Online cases if they still have no test
  (FU-COMPUTE-DOCS-1124F).

## Parked from earlier rounds

Carried unchanged from 1124e (1124f skipped section):

- Row 1409: FU-HTTP-STOP stays parked (DISCUSS).
- Row 1439: web-research eval suite, still deferred.
- Row 1449: manual `USER.md` cleanup.
- Row 2494 is not a repo PR: the local `docs/calc/future-research-questions.md`
  1.2 row can be closed locally if Gerrit 21281 has merged.

## Already decided

Do not reopen these unless a new report comes in. From the 1124f skipped
rows and the non-Writer audit decisions:

- Row 2352: documented limitation; plot-drag onto another `=PY()` stays
  unchanged (FU-CALC-PY-FIXES-5).
- Row 2356: stacked deletes on another author's insertion stay untagged
  by design (2170); do not add a new review category.
- Row 2359: recorded Undo with a character style at the paragraph start
  (row 2150) stays a documented limitation (FU-REPLACE-PARA-INHERIT-2).
- Row 2380: expected result of the planned RTL rule; earlier rounds also
  dropped RTL XLSX edges (1752, 1753); nothing is deleted.
- Row 2434: the math dialog is a user insert, not an agent edit, so
  skipping the review wrapper is correct.
- Row 2481: header/footer `\n` inside `$...$` staying literal matches the
  body path and the documented rule.
- Row 2500: slow blocked-check on a 100k-cell checkerboard was accepted
  in the spill-remap plan.
- Row 2420: `LinguServiceManager.setConfiguredServices` hang is
  undiagnosed, product code does not call it, and it may be upstream
  LibreOffice; not a WriterAgent patch we can specify.
- Leave `web_research` hidden from MCP (non-Writer audit item 19).
- A blank field in Settings may delete the stored key (item 24); that
  behaviour is accepted.
- Keep the Firefox BiDi client (item 27).
- Keep the blocked-spill erase of previous output (#3966) and the plot
  position / cell-text change (#3863) (item 22).

## Old nits (0.9.6)

162 nits gathered after 0.9.6, not queued. 161 are still open; 1 was
already handled in the follow-ups ledger. Full list:
[OLD-NITS-0.9.6.md](docs/post-1.0/OLD-NITS-0.9.6.md).

| Area | Open | Area | Open |
|---|---:|---|---:|
| `docs/archive` | 1 | `plugin/ppt_master` | 1 |
| `docs/calc` | 2 | `plugin/scripting` | 9 |
| `docs/chat` | 2 | `plugin/vision` | 2 |
| `docs/eval` | 1 | `plugin/writer` | 31 (plus 1 handled) |
| `docs/framework` | 2 | `tests` | 3 |
| `docs/images` | 2 | `tests/calc` | 8 |
| `docs/writer` | 6 | `tests/chatbot` | 2 |
| `general` | 4 | `tests/compute_service` | 2 |
| `git-commit` | 10 | `tests/draw` | 5 |
| `github` | 2 | `tests/framework` | 1 |
| `plugin` | 2 | `tests/ppt_master` | 1 |
| `plugin/calc` | 18 | `tests/scripting` | 6 |
| `plugin/chatbot` | 3 | `tests/scripts` | 2 |
| `plugin/doc` | 2 | `tests/writer` | 27 |
| `plugin/draw` | 2 | | |
| `plugin/framework` | 2 | | |

## Headed contract pass (planned, not deferred)

A headed LibreOffice pass on a real 100+ page Word-origin contract,
driven over MCP with recording on. Best run after the Writer A audit
items merge, and again after B. The brief is
[AUDIT-HEADED-PASS.md](docs/post-1.0/AUDIT-HEADED-PASS.md). Write
results to `chain-work/AUDIT-HEADED-PASS.results.md` and leave queuing
to Keith.
