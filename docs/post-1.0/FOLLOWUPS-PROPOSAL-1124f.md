# Follow-ups proposal, round 8 (1124f) (2026-11-24, 10:16pm ET), REPORT ONLY

Proposal only. Nothing has been queued, dropped, or marked, and chain-items.tsv, followups.tsv and pool.conf were not touched. The follow-up loop stays paused.

Scope: followups.tsv rows 2323–2524 (202 ST=new rows left after 1124e). Master checked at `1f66f4929` (#3942, merged 9:55pm ET). Almost every parent of these rows is already merged. Still in CI, so later leftovers must wait: FU-WRITE-MARKUP-2 (#3988), FU-CONTENT-CONTROL-2 (#3989), FU-SPILL-CLEAR-CASES (#3990). The three carried deferred rows (1409, 1439, 1449) stay parked, as in 1124e.

## Counts and convergence

- 202 new rows in this harvest, plus the 3 carried deferred rows.
- 162 unique rows in 29 proposed WriterAgent bundles (no Collabora bundles; none of these rows are engine work).
- 40 rows skipped (restatements of work already in a bundle, already covered, accepted limitations, or no PR can do them).
- Of the 162 unique rows, about 115 are real bugs or remaining Calc/Writer parity gaps, about 35 are cleanups (tests, CI, docs, imports), and about 12 are valid nits riding in the same-area bundles. One bundle is nits-only, and it groups several leftover wording fixes in the same cleanup area rather than giving any nit its own PR.

New ledger rows per round, 1124a–f:

| Round | When (ET) | New rows | Range | Queued or proposed | Dropped or skipped |
|---|---|---|---|---|---|
| 1124a | 12:19am | 172 | 1592–1763 | 86 in 20 bundles | 81 dropped, 5 questions |
| 1124b | 2:00am | 137 | 1764–1900 | 84 in 15 bundles | 48 dropped, 5 questions |
| 1124c | 1:39pm | 124 | 1901–2024 | 78 in 17 bundles | 42 dropped, 4 questions |
| 1124d | 4:15pm | 179 | 2025–2203 | 151 in 29 bundles | 28 dropped, 2 questions |
| 1124e | 6:15pm | 108 + 10 mid-triage | 2204–2312, 2313–2322 | 92 WriterAgent + 2 Collabora, then the 10 | 18 dropped, 7 questions (later answered) |
| 1124f | 10:16pm | 202 | 2323–2524 | 162 in 29 bundles (proposal) | 40 skipped |

The raw count rose in 1124d and again here, but the source changed. All 202 rows in this round come from earlier follow-up items (the 1124d and 1124e bundles that have now finished planning or review). This is a second-order harvest, not a new independent wave. The drop rate fell from about 47% in 1124a to 16–17% in 1124d/e; in this round 40 of 202 rows (20%) are restatements or already-decided leftovers, which is the residual of those finished items restating their own “later” lists. The unique work is still real, but it is almost all leftover edges in the same series (Calc helpers, header tables, recording, spills, markup, content controls), which is what you would expect if the loop is tightening rather than spreading into new areas.

## Proposed bundles

Each bundle is Opus plan, g40f code, Opus review, if later queued. Planners verify against current master and may reject a row that is already gone.

### FU-WRITER-SMALL-1124F
Rows: 2323 2324 2325 2347
Kind: bug
Depends on: FU-WRITER-SMALL-1124D (#3957, merged) and FU-REPLACE-PARA-INHERIT-2 (#3960, merged)

Tracked `table_delete` on a protected cell fails with a generic error, while the untracked path deletes through the protection; use one rule for both, and refuse in both paths. The other paragraph-background properties likely have the same save loss as the one already fixed. `style_format_text` with `target='full_document'` skips a leading table even though the tool says it covers tables. A heading imported over a different list should not inherit that list’s restart or level.

### FU-STRUCK-READS-2
Rows: 2329 2330 2331 2332 2333 2334 2335 2346
Kind: bug
Depends on: FU-STRUCK-READS (#3959, merged)

HTML import still shifts later paragraph styles when a nested list item shares a slot or when `<hr>` takes no slot (review row 2344: `filled` is never set because `handle_data` does not call super). Empty cells of a struck row stay unmarked (2345 is the same as 2332); a deletion that ends on an `AT_PARAGRAPH` image is marked only in full reads; `table_visible_text` still treats a Delete-Table’d table as blank cells; images in table cells are cloned without the `removes_anchored` filter; `table_is_struck` ignores a live untracked image in an already struck table. 2331 is the summary `paragraph_count` nit in the same import path.

### FU-HEADING-RENAME-4
Rows: 2336 2337 2501 2502 2504
Kind: bug
Depends on: FU-HEADING-RENAME-3 (#3986, merged)

Counter labels still ignore outline level, so a moved, renumbered heading can warn. Number-style changes that keep the same value (“Chapter One” → “Chapter 1”) are refused after a move, and ordinary words that begin with a counter character (`第一条件`) are still treated as a label. Recognise the cheaper leftover ordinals and number-words that share the existing path; skip the rarest forms if they need a new table. 2337 (visit a paragraph once per link) is a valid nit in the same code.

### FU-CALC-HELPERS-9
Rows: 2339 2340 2343 2469 2470 2471 2472 2473 2474 2475 2476 2477 2478 2479 2480 2506 2507 2508 2509 2518
Kind: bug
Depends on: FU-CALC-HELPERS-8 (#3984, merged)

The next Calc helper-parity wave: domain and error codes still listed under 2315–2319, `CalcRange` values that `IS*`/`XOR`/`IFERROR` treat as the range object (high value), `iferror` still swallowing `InterpreterError` and hiding a sandbox refusal, `np.int64` of a NaN, ISEVEN/ISODD of an error, lookup and text-function mismatches, and blank-cell `str()` cases. Also bound `_cum_payment`’s `np.arange`, and put `@propagate_errors` on the remaining distribution helpers. This is large; the planner may ship PART 1.

### FU-UNO-IMPORTS-3
Rows: 2348 2349 2363 2364
Kind: cleanup
Depends on: FU-UNO-IMPORTS-2 (#3961, merged)

Hoist the remaining ~60 function-local `import uno` sites, and the `com.sun.star…` imports in `image_mark.py`, `page_png.py` and `shapes.py`, after the stubs are in place. Run `generate_tool_proxies.py` `main` in a child process in the tests so a throwaway `uno` cannot leak. Add the missing `load_frame_styles` and `load_numbering_styles` parameters to `docs/writer/llm-styles.md`.

### FU-CALC-PY-SCRIPTS
Rows: 2350 2378 2351 2353
Kind: bug
Depends on: FU-CALC-PY-FIXES-5 (#3964, merged)

A successful New still replaces unsaved script edits without asking, and a New-list update while the Save box is open can drop the edit. A failed Delete also replaces the editor text. With the flag on, opening a hidden research copy still reconciles formulas and can show the cap-hit box for that hidden document.

### FU-REVIEW-FLAGS-3
Rows: 2354 2355 2357 2358 2371
Kind: bug
Depends on: FU-REVIEW-FLAGS-2 (#3962, merged)

2168 can still lose a frame on Accept when a recorded whole-text search match is the anchor (anchor type 0 or 4). Recommend refusing with a new code, following `INDEX_IN_TRACKED_CHANGE`, rather than an untracked delete. 2180 still loses the review tag on Redo; detect Redo and re-tag if a practical hook exists. Gate `images.py` and `styles.py` so they do not annotate on mode alone, let `pending` drive the flag when the mode is switched off mid-call, and share the `tracked` / `pending_review` helper.

### FU-TEST-PROFILE-3
Rows: 2360 2361 2375 2377
Kind: cleanup
Depends on: FU-TEST-PROFILE-2 (#3963, merged)

`_ensure_live_ctx` can start a second office without stopping the first, then delete both profiles and leave the first soffice running. Check whether Windows recycle leaves `soffice.bin` behind. Drop the now-redundant second `_GEO_FLAG_KEY` write, and `wait()` after a kill so profile delete does not race the process.

### FU-CARET-INSERT-2
Rows: 2365 2366 2367 2368 2388 2389 2390
Kind: bug
Depends on: FU-CARET-INSERT-PIN (#3965, merged)

Image inserts still read the live view cursor, so a caret move during a slow generate lands in the wrong place. The `table_insert` caret-in-cell hint never matches LibreOffice’s message. `run_inner_read_agent` can leak `selection_pin` onto a sibling document. Restore the pin after a rolled-back edit. `chart_upsert(position="cursor")` silently goes to the document end when the range is in a cell, frame or header. The two wording nits (chart “view cursor”, footnote “insertion point”) ride along.

### FU-SPILL-EDGES
Rows: 2374 2383 2384 2462 2463 2499
Kind: bug
Depends on: FU-SPILL-CLEAR-CASES (#3990, still in CI), and already-merged FU-SPILL-REMAP (#3983) and FU-SPILL-AFTER-REGISTRY (#3966)

`=PY(B1)` code-in-cell formulas never auto-spill, because `is_matching_py_formula` is called without `sheet`/`doc` (confirmed on master; 2523 is the same bug as 2463). `load_spill_registry_for_doc` marks the state loaded even when the read fails, so a later save can write `{}`. A deferred spill can inherit a stale record at the origin before `cleanup_orphans`. A sheet renamed before the open hook ran keeps the old name. After #3990, only include the timer-order clear (2462) if that case is still open. `forget_key` / `clear` should `removeModifyListener`.

### FU-READS-STRUCK-5
Rows: 2385 2387 2409 2410 2386
Kind: bug
Depends on: FU-READS-STRUCK-4 (#3972, merged)

`apply_content` dry_run, comment anchors and index entries still report raw hit text. Search `context` omits MetadataField text. Ctrl+A with a leading table still reads only the table. Astral text before a range that starts inside an Input or MetadataField is the leftover limit of the last fix. 2386 (find a needle that runs across a deletion) is worth doing for the visible-text search, but replacing a range that holds someone else’s pending deletion needs a policy; if that part is unclear, ship the search and leave the replace rule out.

### FU-TEST-LOG-HYGIENE
Rows: 2392 2393 2394 2395 2399 2400 2401 2342
Kind: cleanup
Depends on: FU-IMAGE-LOG-CAPLOG (#3967, merged)

Add `test_image_download_logs_hide_signed_query` to `DESELECTED_K`, because `--strip` removes the log calls and the test cannot pass in `make release-verify`. Fix the root-logger leak, the timing-flaky worker-pool test, and the split ownership of `writeragent` logger state (fixture name, double restore). The leftover `writeragent.test.*` loggers and the mock-LLM word-list markers (`[tracked deletion]`) are valid nits in the same tests.

### FU-CI-CACHE
Rows: 2396 2397 2398 2403
Kind: cleanup
Depends on: FU-CI-OPENGREP (#3968, merged)

Seed the LibreOffice `.deb` cache from a master or scheduled run so PRs stop storing about 287 MB each. Move `actions/cache@v4` to a Node 24 release. Use the same pinned OpenGrep version on macOS/Windows CI and `make opengrep-install`. If the download is still flaky after that, cache the binary keyed on `OPENGREP_SHA256`.

### FU-GROUPED-SHAPES-3
Rows: 2328 2404 2405 2406 2417
Kind: bug
Depends on: FU-GROUPED-SHAPES-2 (#3971, merged) and FU-GROUPED-TEXTBOXES (#3953, merged)

Vertical align/distribute still compares coordinates from mixed anchor frames (inline vs page, and so on). Geometry on Writer `FrameShape` entries still raises a raw LibreOffice error, including for an inline picture edited with `x` or a negative width, because `getPosition()` runs before the new check. The partial-group warning should also name mark-protected objects and objects on another page. Merge the three near-copy rows in `docs/writer/reviewable-agent-edits.md`.

### FU-SECRET-LOGS-7
Rows: 2407 2408 2413
Kind: bug
Depends on: FU-SECRET-LOGS-6 (#3969, merged)

The CORS forbidden-`Host` warning can log a stale tunnel host (2412 is the same line as 2407). `_full_url_for_request_path` can leak part of an endpoint password that contains a slash, if such a URL is accepted. A few `llm_client` debug lines still call `public_target` (which can raise) instead of `log_target`.

### FU-CLEANUP-8
Rows: 2414 2415 2416 2422
Kind: nit bundle
Depends on: FU-CLEANUP-7 (#3970, merged)

`parse_table_source_spec` still `str()`s a bool, list or dict in `tables[].file` / `sheet` / `named_range` / `range` and names the Python repr. Finish the remaining 0-based page wording on Draw context and on the `image_insert` / slide-tool descriptions, in the same “index N (the user’s page N+1)” style already used on canvas, not as a 1-based API. `DrawBridge.get_active_page_index` reports 0 on a total miss and now also says “the user’s page 1”; report a miss instead.

### FU-VISION-IMAGE-3
Rows: 2418 2419
Kind: bug
Depends on: FU-VISION-IMAGE-2 (#3974, merged)

`remove_graphic_from_draw_pages` can delete an unrelated same-name image when the given shape is no longer on any page (probed; also used by `image_delete` and `shape_delete`). A custom script that returns a vision-shaped result without a discovered shape still tries to roll back “Delete image after OCR”.

### FU-SCRATCH-DOCS-7
Rows: 2421 2431
Kind: bug
Depends on: FU-SCRATCH-DOCS-6 (#3975, merged)

A UI-thread bind still waits out the 1.5 s failed-bind backoff even though it needs no thread hop. An embedded OLE Writer edited in place can bind the host document under the embedded document’s id, after which the host’s own id can never bind (read from the code; also true on master).

### FU-RECORD-STYLE-7
Rows: 2424 2425 2426 2427 2428 2435
Kind: bug
Depends on: #3942 and FU-RECORD-STYLE-6 (#3976), both now merged

Replace #3942’s undo-title special case with the exact landed/empty rule from the undo stack (`after == inside` vs `after == inside[1:]`), and drive `nothing_landed` from that. After #3942, a failed insert inside `record_mutation` can stay partly written. Track-delete the leftover empty paragraph when the next paragraph can support it. Group the 6–7 undo steps of display math in mode Off. `math_run_symbolic` now records in Record/Wait but still has no pending-review note for the model.

### FU-RECORD-EDGES-4
Rows: 2438 2439 2440 2441
Kind: bug
Depends on: FU-RECORD-EDGES-3 (#3979, merged)

A recorded search replace of a heading with display math leaves the struck heading in place, so Accept All can leave an empty Heading 1; this needs its own design, but the empty heading is a real bug. Inserting display math at the start of a heading can drop a direct page break, or leave an empty heading above the formula. Header/footer writes still do not turn TeX and MathML into formula objects.

### FU-HEADER-TABLES-6
Rows: 2442 2443 2444 2445 2446 2447 2448 2449 2503 2437
Kind: bug
Depends on: FU-HEADER-TABLES-5 (#3987, merged) and FU-SECTIONS-FULLDOC-2 (#3977, merged)

A restored page-count field loses `NumberingType` (`1 of 3` becomes `1 of C`). Recorded header writes still leave blank paragraphs on Reject All or Accept All. Untracked header writes can carry leftover direct formatting. Header cell reads do not join partly struck paragraphs; range/selection reads still hide an `AT_CHARACTER` logo at a deletion’s end; a live blank header paragraph round-trips as `\xa0`; `page_replace_header_footer_text` does not search content-control text; a paragraph whose only pre-deletion content is an `AT_CHARACTER` frame is treated as live for style choice. Search header labels miss matches in a header table cell or frame.

### FU-PY-ARRAY-3
Rows: 2450 2451 2452 2453 2454 2455
Kind: bug
Depends on: FU-PY-ARRAY-2 (#3980, merged)

A native array formula that reads its own empty-growth anchor is accepted and shows zeros. Nested `=PY` list returns before LibreOffice 26.8, generic `Error: …` text treated as ok, and ordinary nested writes over their own data are still incomplete. `PyFormula.parse` can swallow a trailing `+SUM(...)` or `+TAKE(...)` as part of one `=PY` call, which also drops an unknown-function hint (review row 2482). Point `uno-probe.sh` at the worktree add-in, not whichever OXT was last installed.

### FU-EVAL-CLEAN-6
Rows: 2456 2457 2458 2459 2485 2486
Kind: bug
Depends on: FU-EVAL-CLEAN-5 (#3982, merged)

The string-harness `apply_style` still restyles only Default paragraphs on `full_document`, accepts unknown style names, and wraps bare list or table markup (which can mis-nest). `apply_content` stringifies non-string list items instead of returning `INVALID_PARAM`. A bare line with an entity and no tags is not decoded, so a needle like `a & b` misses.

### FU-WARMCHILD-4
Rows: 2460 2461
Kind: cleanup
Depends on: FU-WARMCHILD-3 (#3978, merged)

`test_pool_cancel_before_child_published` overlaps the new deterministic test and can land the cancel too early; remove it or make its window explicit. A Stop during a hung venv handshake still waits out the handshake cap (up to 15 s); end it sooner when `cancel` is set. That last row is a behaviour change, but it is the same class of hang-on-stop bug as the last warm-child item.

### FU-WRITE-MARKUP-3
Rows: 2464 2465 2466 2467 2468 2512
Kind: bug
Depends on: FU-WRITE-MARKUP-2 (#3988, still in CI)

Put an embedded object back on write, or drop its wrapper marker when the object survives, instead of refusing. Text in `draw:custom-shape` / `draw:g` never appears in full reads. A text-frame (or bare-shape) wrapper write-back can split the host paragraph and shift following styles. Header/footer and range reads skip `simplify_drawings`, so bare shapes or OLE may still leak there. The header unlinked-inline picture URL should be checked if #3988 does not already close it. Chart series / `embedded_edit` stubs (2464) only if they stay a small read-side fix; otherwise leave that part as later.

### FU-AGENT-OBS-4
Rows: 2483
Kind: bug
Depends on: FU-AGENT-OBS-3 (#3981, merged)

PPT-Master plain-text replies still fail once when smolagents calls `reply_to_user` with `answer`, because `plugin/ppt_master/venv/runner.py` `ReplyToUserTool` only accepts `message` (confirmed on master; the chatbot sticky tool already uses `answer`). Accept `answer` or alias it.

### FU-CONTENT-CONTROL-3
Rows: 2487 2488 2489 2490 2491 2492 2493 2520 2521 2327
Kind: bug
Depends on: FU-CONTENT-CONTROL-2 (#3989, still in CI)

Other deletions that cover a leading hidden conditional section, including a select-all replace, can still abort LibreOffice (reproduced with pure UNO). Search offsets drop Input-field hits; region scans and several portion walkers still see only top-level fields; UNO exceptions that cross a `contextlib` generator still become `RuntimeException`; `math_mml_export._xhtml_id` misses OLE names with `-` or non-ASCII; `full_document` can lose a leading plain section or rename sections. Pass error codes through `form_generate`, and keep a useful step on `rich_html` import failures. Unify the leftover `goRight` in `find_tokens` while that file is open.

### FU-COMPUTE-DOCS-1124F
Rows: 2495 2496 2497 2513 2514 2515 2516
Kind: cleanup
Depends on: Gerrit 19188 and 21286 for the deletions and the one “in review” line; the stale-status rows can start now

#3985 already merged and these are still stale on master: `numpy-jailsafe.md` still says F3/F6/F7 are “not yet on Gerrit” / “not coded”, AddIn `timeout_ms` is still called “deferred”, `calc_range.py` and `formula_worker.py` comments state unmerged Gerrit behaviour as fact, and the README labels `stateful_session` with `mode="shared"`, which the service ignores. When 19188 merges, delete `json_peel.py` and the peel branch. When 21286 merges, drop “in review” from `py-data-shapes.md`. Pin the multi-argument flat Online cases if they still have no test.

### FU-TEST-HYGIENE-8
Rows: 2429 2430 2433 2524
Kind: cleanup
Depends on: FU-TEST-HYGIENE-7 (#3973, merged)

Bind same-shape refetch lists in `test_image_egress_uno.py` if those calls are still unbound on master. A small lint for partial UNO exception constructors would catch fakes that the strict stub cannot see. Watch the two tests that died while reading `redlines.getByIndex`, and consider `createEnumeration`. `test_py_addin_token_formula_errors` failed on unmodified master in the spill-clear probe environment; fix the test or the helper mapping, whichever is wrong.

## Skipped rows

Restatements of a plan, or of rows already in a bundle above:
- 2326: the four OFFSET-UNITS-2 leftovers are already the work in in-flight FU-CONTENT-CONTROL-2 (#3989); later leftovers are 2487+.
- 2338: restates the 2315–2322 list already shipped in FU-CALC-HELPERS-8; remaining work is 2469+.
- 2362: restates 2348.
- 2376: restates 2360.
- 2379: restates 2351, 2352 and 2353.
- 2381: PR-body restatement of 2168, 2170, 2180 and the shared helper.
- 2382: note of adaptations already made in FU-REVIEW-FLAGS-2; no action.
- 2391: restates 2365–2368.
- 2402: restates 2393–2395.
- 2411: restates 2385–2387.
- 2412: folded into 2407 (same Host-header line).
- 2423: restates 2416.
- 2432: restates 2420.
- 2436: restates 2424–2428.
- 2482: folded into 2454 (same parse bug, with the added unknown-function hint).
- 2484: restates 2456–2459.
- 2505: restates 2501–2502.
- 2510: restates the FU-CALC-HELPERS-8 “Later” list (2469+).
- 2511: restates 2464–2468.
- 2517: restates 2495–2497 and 2494.
- 2519: restates 2472 / row 2319.
- 2522: restates 2487–2493.
- 2344: folded into 2329 (implementation note, not a separate task).
- 2345: folded into 2332.
- 2369 2370 2372 2373: folded into 2354 2355 2357 2358.
- 2523: folded into 2463 (same `=PY(B1)` auto-spill miss).

Already done, covered, or not a task:
- 2341: says row 2160 can be closed; that work already shipped in FU-TRACKED-DELETIONS-PERF.
- 2494: Keith’s untracked local `docs/calc/future-research-questions.md` is not in the repo, so no PR can close it; close the 1.2 row locally if 21281 has merged.
- 2498: a waiting note about merged Online plus service 0.9.6 until 21281, already documented, not a code change.

Keith’s past decisions, or accepted limitations from an already-approved plan:
- 2352: documented, unchanged plot-drag onto another `=PY()`; left as a limitation in FU-CALC-PY-FIXES-5.
- 2356: stacked deletes on another author’s insertion stay untagged by design (2170); do not add a new review category here.
- 2359: row 2150 (recorded Undo with a character style at the paragraph start) stays a documented limitation, as FU-REPLACE-PARA-INHERIT-2 concluded.
- 2380: expected result of the planned RTL rule; earlier rounds also dropped RTL XLSX edges (1752 1753); nothing is deleted.
- 2434: the math dialog is a user insert, not an agent edit, so skipping the review wrapper is correct.
- 2481: header/footer `\n` inside `$…$` staying literal matches the body path and the documented rule; no change unless a report comes in.
- 2500: slow blocked-check on a 100k-cell checkerboard was accepted in the spill-remap plan.

Out of scope or undiagnosed:
- 2420: `LinguServiceManager.setConfiguredServices` hang is undiagnosed, product code does not call it, and it may be upstream LibreOffice; not a WriterAgent patch we can specify.

Carried deferred rows, unchanged from 1124e:
- 1409: FU-HTTP-STOP stays parked (DISCUSS).
- 1439: web-research eval suite, still deferred.
- 1449: manual `USER.md` cleanup for Keith.
