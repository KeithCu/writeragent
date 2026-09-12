# Windows CI harness cleanup note (Sep 9–12 trail)

**Not a product fix.** Inventory of leftover / close / Hidden / peer
harness on current master after #687–#746. Goal: keep what still
earns leftover pressure; cut only aliases that add names without
behavior. Logging stays. Do not gut a working skip without a simpler
equivalent that still names the hang class.

Hang-class breadcrumb contract lives in
[uno-test-lifecycle.md](uno-test-lifecycle.md). This page is the
keep / simplify / delete call, not a second GHA diary.

## Hang classes (do not merge)

These are different failure modes. One helper per class.

| Class | Helper / mechanism | Skip log |
|-------|--------------------|----------|
| Leftover Writers → Hidden `_blank` / `_default` | `skip_windows_leftover_hidden_load(reason)` | `windows leftover skip: <reason>` |
| Bitmap → later Hidden `_blank` | `skip_windows_hidden_open_after_bitmap` + `create_native_doc` `_blank` guard | `windows hidden skip:` |
| leftover_open>2 cross-app Draw/Impress | `skip_windows_cross_app_factory` (`_WINDOWS_CROSS_APP_LEFTOVER_MAX=2`) | `windows leftover skip: leftover sdraw\|simpress` |
| TOP `createPeer` / `setVisible` | `skip_windows_awt_top_dialog` | `windows awt skip:` |

Leftover Hidden skip is leftover_open>0. Bitmap skip is leftover_open=0
after a system-bitmap fail. AWT TOP skip is win32 headless VCL, leftover
not required. Cross-app skip is leftover Draw/Impress factory only;
leftover Writer / leftover Calc still load.

## Keep as-is

Each item has a named GHA hang. Closing leftovers or merging classes
reopens it.

- **#687 breadcrumbs + Draw soak.** `format_lifecycle_breadcrumb`,
  `probe_uno_bridge`, `collect_post_test_death`, `make test-uno-soak`.
  Still how a victim factory open names the previous TEST. Keep.
- **#709 Windows UNO bootstrap.** Longer connect budget, one retry,
  stderr_tail on pipe miss. Different class (accept, not leftover).
  Keep.
- **#710 settle after peer Impress close.**
  `settle_after_draw_family_close` after dropping the proxy. Do **not**
  fold into `close_doc` (taxes Writer/Calc). Keep.
- **#716 / #717 peer Impress close.** Windows `close_doc` on Impress
  hung (34518091151). Skip-teardown then wedged the next Writer
  factory (34537826720). Raw `close(True)` + post-close settle is the
  only sequence that returned. Keep `close_draw_family_doc` split.
- **#718 skip Windows Writer/Calc `close_doc` in the peer suite.**
  Dual hidden-Writer close hung before any Impress in that file
  (34544965319). POSIX still closes. Keep.
- **#719 skip second Windows Impress close; peer suite last.** First
  raw close must run (leftover Impress poisons later Writer load).
  Second raw close killed soffice (34547869791). Recycle flag must
  write both runner modules (`python -m` vs `import
  plugin.testing_runner`). In-process rebootstrap after recycle is
  not a healthy office (34551644954) — skip rebootstrap when no
  suites remain. Keep.
- **#720 leftover HTML-paste Writers.** Title said “close”; the
  shipped path does **not** close (34556185752 hung inside leftover
  `close(True)`). `prepare_windows_writer_factory` logs leftovers and
  `setActiveFrame`s the keeper. Keep. Do not restore a leftover close.
- **#722 leftover Writer reuse + stable `_wa_factory`.** Consecutive
  leftover Hidden `_blank` swriter hung even after keeper reactivate.
  Unique `_wa_factory_N` stacked empty frames (`_wa_factory_5`).
  Reuse one CREATE\|GLOBAL name; pool the first leftover Writer; skip
  Writer `close_doc` while leftovers remain. Keep.
- **#723 Draw close breadcrumbs + dual-module adopt.** Same `-m` vs
  import split as #719 recycle. `close_doc` dispose printed
  `previous=-` until lifecycle trail writes both runner copies. Keep.
- **#724 skip Windows Draw close after `insert_math` OLE.**
  `mark_windows_math_ole_doc` uid only. Do **not** walk pages/shapes
  at teardown (34612145495 died on the first unmarked Draw close after
  that walk). `_draw_doc_has_math_ole` stays a unit-test probe. Keep
  both; do not wire the walk back into `close_doc`.
- **#725 leftover Calc after HTML-paste.** Unique leftover scalc
  failed then hung (`_wa_factory_1` / `_wa_factory_2`). Stable
  `_wa_scalc`; later leftover Calc wipe-and-reuses the pooled
  workbook. Keep.
- **#731 Budget copy + leftover Calc reuse + `_wa_notebook_host`.**
  Hidden-open of the `storeAsURL` path bitmap-failed; copy URL is the
  harness workaround. Notebook host is not leftover HTML-paste
  `_wa_factory` (34643210006 listener bleed). Keep. Product
  `open_document_for_read` `_wa_doc_research` is out of this pass.
- **#732 leftover `_wa_notebook` + leftover_open=0 Writer reuse.**
  Do not close leftover notebook docs (34646877587). Consecutive
  Hidden `_blank` swriter hung with leftovers=0 once paste suites
  were deferred (34652644656). `_windows_should_reuse_writer` stays
  true on Windows. Keep.
- **#734 skip later Hidden sibling opens after bitmap.** Attempt the
  first Hidden-open (34652644656 was 3/3). After
  `Could not create system bitmap!`, skip — do not hang the next
  sibling. Keep.
- **#737 leftover Draw/Impress one CREATE\|GLOBAL name.**
  `_wa_sdraw` / `_wa_simpress`. Unique `_wa_factory_10` hung at
  leftover_open=15. Named leftover factories still load at
  leftover_open<=2. Keep.
- **#742 leftover Draw/Impress skip when leftover Writers stack +
  leftover notebook-host reuse.** High leftover Writer count wedges a
  new app factory; leftover swriter at leftover_open=15 returned.
  Keep the skip helper + notebook-host reuse.
- **#743 Hidden `_blank` after system bitmap in `create_native_doc`.**
  #734 skipped later `document_research` siblings; next suite
  leftover writer reuse then leftover_open=0 `target=_blank` hung
  (34670295632). Named leftover factories still load. Keep.
- **#744 Windows headless slash TOP dialog.** leftover_open not
  required. `skip_windows_leftover_hidden_load` does not cover
  `dlg.setVisible(True)` after `createPeer`. Keep
  `skip_windows_awt_top_dialog` separate. ENABLE_SLASH stays parked.
- **#745 `_WINDOWS_CROSS_APP_LEFTOVER_MAX` 4→2.** leftover_open=3
  hung leftover `_wa_simpress` (34672065355). leftover_open=1
  leftover Draw/Impress succeeded (34657826349). Threshold 2 is the
  proven line. Keep.
- **#746 leftover Hidden load skips (the skips themselves).** Latex /
  MathML `.mml`, document-scripts reopen, apply Hidden `_default`,
  apply-style canary, format `_blank` `finally` close, inline-review
  view cursor, page-header / ops / html_export temp_doc, track-changes
  wait-timeout on the **test** thread (SkipTest on the worker does
  not skip the parent). Keep every skip; keep the reason string
  (that is the log line).
- **#711 pytest `import plugin.doc` before Layer B guard.** Import
  order, not leftover. Keep. Do not touch.
- **#708 LF on Windows checkout for eval gold/prompt.** `.gitattributes`
  `eol=lf`. Not leftover. Not weird. Keep. Do not touch.

## Simplify (this PR)

- **#746 thin wrappers `skip_windows_leftover_hidden_apply` and
  `skip_windows_leftover_hidden_mathml`.** Both only called
  `skip_windows_leftover_hidden_load(reason)`. Collapse call sites to
  the one helper + reason string. Log lines stay
  `windows leftover skip: <reason> leftovers=N`. Per-file math
  `_skip_windows_leftover_hidden_mathml()` locals stay: they hold the
  GHA comment and one reason for many tests in that file; they now
  call `skip_windows_leftover_hidden_load`.

## Delete candidates (not this PR)

Do not delete unless a later tip proves a simpler equivalent.

- **Unique `_wa_factory_N` fallback** in `_windows_factory_load_args`.
  Known leftover factories never hit it (swriter / scalc / sdraw /
  simpress have stable names). Unknown leftover URLs still get a
  unique CREATE name instead of `_blank`. Keep the fallback; do not
  re-enable unique names for known apps.
- **Generic dual-module adopt.** Keeper, leftover count, bitmap flag,
  notebook host, Math OLE uids, recycle flag, and lifecycle trail
  each write both module copies. A shared adopt helper would be a
  behavior-risk refactor, not a leftover-pressure win. Keep the
  copies.
- **`_draw_doc_has_math_ole` walk.** Unused at teardown on purpose.
  Unit tests still pin the CLSID. Keep as probe.
- **Peer-suite local `_windows_skip_*` in `test_peer_message_uno.py`.**
  File-local, hang-specific, already logged. Do not fold into the
  leftover Hidden helper (different class).
- **`uno-test-lifecycle.md` GHA diary.** Long, but it is the proof
  trail. Do not rewrite into this note. Add only a pointer.

## Out of this pass

#738 tool-count/schema, #733 Calc fill-down, #736 / #727 / #726
benchmarks, #700 `get_image`, #705 Calc RPS, #693 / #680 eval-2 ports.
Product `_wa_doc_research` / `_wa_calc_html` names stay.

## How to add the next leftover skip

Call `skip_windows_leftover_hidden_load("<suite> <hang class>")` on
the **test** thread before the Hidden load or leftover-poisoned
assert. Do not add a new wrapper unless the hang class is new
(bitmap, AWT TOP, leftover Draw/Impress). Keep the print. Do not
close leftover paste / notebook Writers.
