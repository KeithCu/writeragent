# Follow-up loop audit, non-Writer: keep, remove, improve, change

2026-11-24, origin/master at `91a3a6312`. Scope: the ~110 non-Writer commits since 0.9.6.
Companion to `followup-loop-audit-2026-11-24.md` (Writer). Method: five read-only audits (Calc =PY and
spill; Calc helpers; framework; document tools, Draw, vision and browser; tests, CI and eval). Nothing
was run or timed. Every risk is from reading code unless it says otherwise, and "suspected" items
need a probe before any change.

## Verdict

Non-Writer code is in better shape than Writer. Almost nothing needs reverting. Most chains can be
closed by one consolidation each instead of another generation of call-site patches. Three areas
need real attention: spill ownership (a user-data rule is broken), the keystore on the UI thread,
and the absence of any real-document test.

| Area | State | Action |
|---|---|---|
| Calc helpers | Unbounded parity chain on a parked prototype | Stop the chain; one small PR |
| Calc =PY and spill | Centralised but heuristic; one data-loss path | Fix ownership, cut nested-=PY machinery |
| Framework | Good consolidations; three chains to close | One change per chain |
| Doc tools, Draw, vision, browser | Mostly keeps | Two consolidations, a few decisions |
| Tests, CI, eval | Sound tests, wrong gaps | Real-document corpus; fix test-bot selection |

## Keep

- **Workers:** one spawn protocol (`ChildSlot`, #3817) and the cancel fixes that followed.
- **Scratch documents:** one RuntimeUID registry in `scratch_writer.py`; MCP catalog skips them.
- **Tool arguments:** one schema-driven coercion owner (`tool_schema.py:368-422`), one colour parser.
- **Browser:** `plugin/contrib/cdp` went from 2,211 to 509 lines; all waits bounded; static fallback.
- **Save path:** refusals for open-elsewhere, changed-on-disk and locked files replace blocking
  modals. No overwrite path found.
- **Draw:** new rectangles and ellipses match the LibreOffice UI; existing documents untouched.
- **Calc =PY:** NaN spill cells, pre-26.8 scalar fallback, the result-index removal (−190 lines),
  one version probe (`accepts_returned_any`), faster remap on insert.
- **Python tracebacks:** both commits; `plt.hist` as a last line works again.
- **Delegation:** folded base class and frozen-domain-list fix (net deletions).
- **Tests:** strict UNO exception stubs (#3973 exposed a real product bug), missing-module UNO tests
  now fail, profile and config helpers centralised, the real-LibreOffice scripted eval pack in CI.

## Tasks

### A. Data loss and hangs (do first)

1. **Spill overwrites user data after a row or column delete through a spill body**
   (`plugin/calc/python/spill.py:264-266`, `:616`). Cells that shift into the old rectangle are
   cleared at the next recalc with no `#SPILL!`. #3920 fixed this and #3940 undid it. Shrink the
   record when the origin matches but the snapshot differs after a negative used-area delta.
2. **Stale spill after a scalar or empty result.** Old numbers stay and look live. When a formula
   with a multi-cell record returns a scalar, queue a 1×1 write.
3. **Keystore reads on the UI thread.** `get_secret` has no thread guard; Settings reads keys at
   `settings_fields.py:138` and `dialog_views.py:423,1083,1220`. A locked keyring can freeze Settings
   and prompt once per secret. Main-thread reads return cached-or-none unless Settings asks; one
   global locked latch. (Thread context inferred; verify.)
4. **Stop waits up to 15 s on a hung worker handshake** (`warm_child.py:346-353`). Pass `cancel` into
   the handshake read. This is the only part of FU-WARMCHILD-4 worth doing.
5. **`iferror` hides a sandbox refusal** (`calc_functions_i_m.py:129`). Re-raise `InterpreterError`.

### B. One change that closes a chain

6. **Secret redaction (seven generations).** Extend `RedactingFormatter` (`log_redaction.py:117`) to
   mask URLs and known key values, then delete the `log_target` calls at log sites (29 in 12 files).
   Keep `public_target` for error text that goes to chat, MCP or the model. Skip FU-SECRET-LOGS-7.
7. **Spill "may it clear" rule.** It is answered in four places with different rules (`spill.py:293,
   519, 616, 733`). One ownership predicate; covers task 1.
8. **Calc helpers.** Do not queue FU-CALC-HELPERS-9. One PR with the four real rows: task 5, the
   `CalcRange` object tested by `IS*`/`IFERROR`/`XOR`, `np.int64(NaN)` in the LEFT/MID/DATE
   emitters, and the unbounded `np.arange` in `_cum_payment`. Then make the importer restore the
   original formula for any cell that fails verification (`pipeline.py:110-119`), so parity holds on
   real data. Add a rule to `plugin/scripting/AGENTS.md`: a parity change needs a failing importer
   cell or a user report.
9. **Vision image identity.** Delete the name fallback in `remove_graphic_from_draw_pages`
   (`visual_helpers.py:~763`) and carry `(name, graphic)` pairs through the preview and script paths
   (`vision_preview.py:148`, `python_runner.py:422`, `vision_egress.py:446`).
10. **Page-index wording.** One page-parameter factory plus the prompt rule at
    `framework/prompts.py:399`; delete the ~15 hand-written descriptions. Skip the rest of
    FU-CLEANUP-8.
11. **Scratch-document enumeration.** Make `iter_open_models` skip scratch documents by default;
    five enumerations still do not filter.
12. **Eval string simulator.** Freeze `eval_worlds.py` (1,229 lines, 8 commits in two days). Allow
    only changes that delegate to production code. Put fidelity into the real-LibreOffice lane.
    Skip FU-EVAL-CLEAN-6.

### C. Remove or cut back

13. **Nested-=PY machinery** from #3944 and #3980: `py_calls`, `FormulaDialects.references`,
    `_free_block_right_of`, `has_setup_failure_text`. Refuse nested =PY in array writes outright
    (about 200 lines). Keep the failed-run warning and the scratch fix.
14. **`_detach_copy` and the RTL special case** from #3964.
15. **Three nested `undo_lock`s** around the plot bracket (`image_egress.py:952-975`): one lock.
16. **Registry saved per spill** (`spill.py:637`): save once per debounce drain. A full recalc of N
    spills is O(N²) today. Also check the per-modify `getDataArray` of every rectangle (`:265`).
17. **`partial_turn_agent.py`** (80 lines): record incrementally in the vendored tool-call loop.
18. **Mock-heavy unit tests with a UNO twin** and repeated UNO document-building code: an estimated
    6–9k of the +54.9k test lines. Low priority; shared builders first.

### D. Decisions for Keith

19. **`web_research` hidden from MCP clients** (#3861). The main office works through MCP.
20. **Web research refuses small or unreported context windows** (`web_research.py:879-897`, from
    #3802). Check what stock Ollama reports.
21. **`=PY(code; ROW()-1)` fill-down form** was documented at 0.9.6 and now gives `#SPILL!` or the
    first value. Needs a release note at least.
22. **Blocked spill erases its previous output** (#3966), and **plot position and cell text** changed
    (#3863). Confirm both are wanted.
23. **Multi-sheet CSV export refuses** until a sheet is named. Refusing on save is right; export could
    use the active sheet and say so.
24. **Blank in Settings deletes a stored key** (#3890).
25. **Delegate success result lost `message`** (#3936).
26. **Grammar bind lost its only-Writer fallback** (#3975); with several documents open the bind may
    return nothing and ignore rules stay unloaded (suspected).
27. **Firefox BiDi client:** 395 lines plus orphan-process handling for a fourth off-by-default option.
28. **Calc helper list:** shrink only if the importer prototype is revived.

### E. Tests and CI

29. **Writer real-document corpus suite,** modelled on `test_spreadsheet_import_corpus_uno.py`. Six to
    eight fixtures including a Word-authored contract with two authors' tracked changes, merged
    tables, a header logo and a TOC. Per document: every read tool succeeds; an unchanged round trip
    leaves redlines, tables and headers identical; one recorded edit then Reject All restores the
    text; save and reload keeps counts. A timing ceiling on a 200+ page file, run nightly.
30. **`make test-bot` selection gaps:** it reads only uncommitted files, follows no transitive
    imports, and runs only UNO files the branch edited. A change to `text_helpers.py` should run
    `test_text_helpers_uno.py`; `select_uno_files` already exists for that.
31. **Point `uno-probe.sh` at the worktree build.** At least one PR was verified against a stale
    installed OXT.
32. **Fake fidelity:** `FakeUnoPortion` / `FakeUnoPara` (`tests/testing_utils.py:1903`) always pair
    redline start and end in one paragraph, which real LibreOffice does not.
33. **Flakes:** FU-TEST-PROFILE-3 (orphan soffice), the master-failing test and the `getByIndex`
    deaths from FU-TEST-HYGIENE-8, the release-verify deselect and flaky pool test from
    FU-TEST-LOG-HYGIENE. FU-CI-CACHE is low priority.

### F. Small real bugs from the 1124f proposal

34. `=PY(B1)` code-in-cell formulas never auto-spill (`spill.py:484, 780`).
35. Spill registry marked loaded after a failed read, so a later save can write `{}` (`spill.py:313`).
36. Script editor: New discards unsaved edits without asking.
37. PPT-Master `reply_to_user` rejects `answer` (`ppt_master/venv/runner.py:124`); one line.

## Skip before 1.0

FU-CALC-HELPERS-9 (except task 8), FU-PY-ARRAY-3, FU-SECRET-LOGS-7, FU-SCRATCH-DOCS-7,
FU-GROUPED-SHAPES-3, FU-EVAL-CLEAN-6, most of FU-CLEANUP-8, the rest of FU-WARMCHILD-4 and
FU-SPILL-EDGES (`removeModifyListener`, timer order), the remaining traceback follow-ups.

## Numbers

- Tests since 0.9.6: +54,943 / −6,311 lines; 17,949 test functions (16,595 at 0.9.6). 3.3 test lines
  per production line added.
- Sample of 51 new tests: 31% real LibreOffice with a visible result, 35% real logic, 22% mock-heavy
  restatement, 6% change-detector, 6% contrived pin.
- CI on Nov 24: 58 failed and 38 cancelled of 267 runs; median green run 12.1 min, up from about 8.
- Calc helpers: 260 functions, 6,192 lines; zero helper calls in the eval results.
