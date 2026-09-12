# Native UNO test lifecycle (Draw flakes / URP `DisposedException`)

**Not a product fix.** This page is the diagnostic contract for intermittent
`DisposedException` on `desktop.loadComponentFromURL` in the native runner
(`plugin/testing_runner.py`, `make test-uno`). Do not treat a green soak as
proof that LibreOffice or WriterAgent is healthy.

Related: [archive/test_architecture_analysis.md](../archive/test_architecture_analysis.md)
(TEST start/end lines, Darwin URP abort, keeper document).

## What the fixture actually does

| Path | Reuse? | Open | Close |
|------|--------|------|-------|
| Calc `@with_native_doc` | Yes (wipe-and-reuse pool) | Factory only on first use / dead pool | Close only if reset fails |
| Writer `@with_native_doc` | Windows yes (leftover pool; leftover notebook host uses `_wa_notebook_host`) | Factory on first use / dead pool | `close_doc` (Windows **skips** Writer close while leftovers remain) |
| Draw / Impress | **Never** | Factory each test (`private:factory/sdraw`); Windows **skips** leftover Draw/Impress when leftover_open>2 | `close_doc` (Windows **skips** close when the Draw still holds Math OLE) |

`create_native_doc` is a thin `loadComponentFromURL`. Draw tests do **not**
share a pooled document. A keeper hidden Writer is opened once in
`run_all_tests` so Windows bootstraps do not shut down when a suite closes
its last hidden Draw doc.

`_ensure_live_ctx` only runs **between suites**, and only if
`getServiceManager()` already fails. Inside a suite, the next
`@with_native_doc` open is the first place a dead bridge is noticed.

`close_doc` still swallows most errors (a failed close must not hide the
test body). Dispose during close is now **logged** (`LIFECYCLE close_doc dispose`).
After a non-pooled close, the harness probes `desktop.getComponents()` and
prints `LIFECYCLE office dead after close` if URP is already gone.

Before `doc.close(True)`, `close_doc` runs `gc.collect()` then sleeps 50 ms
(all apps, not Draw-only). That is a **release-order settle** so URP can
finish `~SvxShape` / `SdrObject` before the drawing item pool dies — not
proof LibreOffice is healthy. If SalAbort still prints, `#698` fail-closed
still names that test. Details and soak rates:
[salabort-svxshape-close.md](salabort-svxshape-close.md).

**Peer Impress → next Writer factory (Windows):** GHA 34419828920 hung 30s
in `tests/chatbot/test_peer_message_uno.py` at `_load("private:factory/swriter")`
(line 109), immediately after `test_peer_impress_rejected_on_resolved_model`
passed. That predecessor used a local `doc.close(True)` and skipped
`close_doc`. Office stayed alive (`kill-libreoffice.ps1` still found PIDs).
`#710` routed peer closes through `TestingFactory.close_doc` and added
`settle_after_draw_family_close` (GC + 0.75s Windows / 0.15s else) *after*
Impress close.

**Peer Impress `close_doc` hang (Windows):** GHA 34518091151 (master
`e01439f1`) hung 30s *inside* `close_doc` at `doc.close(True)` while that
same test closed Impress with Writer still open. Both factory loads had
printed `peer_message_uno: load start/done` for `swriter` then `simpress`.
`#710`'s post-close settle never ran. Office stayed alive
(`kill-libreoffice.ps1` still found soffice PIDs).

GHA 34532953982 hung at `close(True)` after Writer-first + 0.75s settle.
GHA 34535868114 hung the same way at `dispose() start` (office still
alive; `kill-libreoffice.ps1` then killed soffice). GHA 34537826720
(`9adff169`, skip-teardown) printed `close_draw_family: skip uno teardown`
and the first Impress test ended OK, then
`test_peer_catalog_draw_label_is_not_enough_for_impress` hung 30s on
`private:factory/swriter` load — leftover Impress poisons later Writer
factory loads (same family as `#710`).

Peer tests: POSIX closes Writer first, then `close_draw_family_doc`
(`setModified(False)`, GC, pre-close settle, `close(True)`). **Windows
keeps Writer open** and uses a bare Impress `close(True)` (no pre-close
GC/sleep — the only close that returned, `#710` / 34419828920), drops
the proxy, `settle_after_draw_family_close`, and re-activates Writer
(`setActiveFrame`, `#707`). Do **not** then `close_doc` that Writer:
GHA 34540353452 (`c511aab7`) raw-closed Impress in ~25 ms, settle +
`writer reactivated` printed, then `TestingFactory.close_doc` hung 30s
at Writer `close(True)` (office still alive; `kill-libreoffice.ps1`
then killed soffice). Leftover Writer is not the poison (the keeper
Writer already exists); leftover Impress is.

GHA 34542928132 (master `fa60bd5e`, tip of `#717`): both Impress tests
`TEST end … OK` (`raw close(True)` + `skip writer close after impress`).
Then `test_peer_missing_deck_is_clear_error` loaded **two**
`private:factory/swriter` docs (both `load done`), ran the tool assert,
and hung 30s in `TestingFactory.close_doc` from `_close(other)` in
`finally`. That test never opens Impress — leftover Impress/process
state poisons later Writer `close_doc` in the same soffice, not only
factory load.

GHA 34544965319 (`#718` isolate: Impress **last**):
`test_peer_missing_deck_is_clear_error` ran **first**, before any
Impress in this file. First `close_doc` (`other`, uid=30) returned;
the second (`writer`, uid=29) hung 30s at `doc.close`. Dual
hidden-Writer `close_doc` is the hang — leftover Impress from *this*
suite is not required. Isolation by reordering did not unblock
`missing_deck`. Office stayed alive (`kill-libreoffice.ps1` then
killed soffice).

Windows `_close` therefore drops the proxy on every Writer/Calc
(`skip close (windows) uid=…`) and asks the runner to recycle soffice
(`LIFECYCLE recycle office after impress`) after this suite. Impress
still runs last; teardown keeps the raw Impress `close(True)` +
`setActiveFrame` path and does not `close_doc` the sibling Writer.

GHA 34547869791 (master `0e570b15`, tip of `#718`): all four Writer/Calc
peer tests `TEST end … OK` (`skip close (windows)`). First Impress
raw close returned in ~21 ms (`writer reactivated`). Second Impress
`raw close(True)` raised `DisposedException` after ~7 s and soffice
**exited 0** — fail-closed
`test_peer_catalog_draw_label_is_not_enough_for_impress` and skipped
remaining suites. Two Windows Impress raw closes in one soffice (after
leftover skipped Writer docs) is the killer. Skip the *second* Impress
close (`skip second impress close (windows)`); leftover last Impress
dies with recycle. Do not skip the first close — leftover Impress
before a later Writer load hangs (34537826720).

GHA 34549510317 (`#719`): all six peer tests `TEST end … OK` (skip
second Impress close printed). Recycle **did not run** — next suite
started on the same soffice (`6052,1752`). Later
`doc.test_text_helpers_uno` hung 30s in `create_native_doc` (leftover
Impress + skipped Writer docs; office still alive). Cause:
`python -m plugin.testing_runner` is `__main__`; tests imported
`plugin.testing_runner` and set the recycle flag on that copy.
`request_office_recycle_after_suite` / `consume_office_recycle_request`
now touch both module objects.

GHA 34551644954 (`0177648d`): recycle **did** run (`start` / `done`
pids=`8188,6868`). `test_slash_popup` OK on the new office. Then
`test_list_nearby_excludes_active` failed `Could not create system
bitmap!` and the next Calc `create_native_doc` hung 30s. In-process
rebootstrap is not a healthy office. Windows now runs
`test_peer_message_uno` **last** so other suites keep the original
office; after that suite, skip rebootstrap and terminate soffice
(`recycle office skipped; no remaining suites`).

GHA 34554275072 (master `633f8a39`, tip of `#719`) and 34553944171
(pre-merge `#719` tip): peer suite never reached. `document_research_uno`
(including `test_list_nearby_excludes_active`) passed — those tests are
**Calc** (`@with_native_doc("calc")`), not a Writer factory cycle. Then
`doc.test_text_helpers_uno.test_get_string_without_tracked_deletions_paragraph_bold_run_no_newline`
OK (first Writer factory after leftovers + `close_doc` returned; same
soffice pids `4480,2176`). The next test,
`…_multi_para_joins_with_newline`, hung 30s in `create_native_doc`
(`loadComponentFromURL(private:factory/swriter)`). Earlier
`insert_cell_html_rich` left temp Writers open
(`close skipped pasted=True`; `reused_existing=False`): uid=26 then
uid=27, `writers_open` 1→2. Calc teardown does not close them. Product
must not close those Writers during a live paste (33771766524). After
the first harness Writer `close_doc`, desktop current becomes a leftover
paste Writer; the next factory then hangs (same family as leftover
Impress, 34537826720). CharWeight itself is not the hung call.

GHA 34556185752 (`#720` leftover-close attempt): first text_helpers
factory called leftover `close(True)` on uid=27 (`frame=-`) and hung
30s *inside that close* — same “do not close after paste” rule, still
true minutes later. Do **not** close leftover paste Writers from the
harness. Before a Windows Writer factory, log leftovers and
`setActiveFrame` the keeper. After a Windows Writer `close_doc` (test
docs still close — that returned on 34554275072), reactivate the
keeper again (`html_paste_writer: leftovers open=…`,
`html_paste_writer: keeper reactivated`).

GHA 34593327841 (master `754620ba`, tip of `#720`): first Windows UNO
run after that merge. `insert_cell_html_rich` again left uid=26/27
(`close skipped pasted=True`). Calc + chatbot suites stayed green.
`test_list_nearby_excludes_active` failed in ~1s with PyUNO
`Couldn't convert <traceback object> … getTypes` (no Python traceback;
office alive). The next test hung 30s in
`create_native_doc(private:factory/scalc)` inside
`_create_nearby_test_env`. `#720` only prepared *Writer* factory loads.
Leftover paste Writers as desktop current also wedge a later **Calc**
factory, and `list_nearby_files` walked those leftovers without a
per-component try/except. Product: skip a broken desktop component in
`_office_model_from_desktop_element` / `_collect_open_file_urls` (do
not `log.exception` that walk — formatting a UNO exception can raise
the same traceback conversion). Harness: `prepare_windows_writer_factory`
before every Windows `private:factory/` load. Breadcrumbs:
`document_research_uno: create/store/close/list_nearby start/done`.

GHA 34595675515 (`#722` first tip): breadcrumbs named the fail
*after* `create budget calc start` + `leftovers open=3
uids=['27','26','1'] keeper=-` and *before* `create budget calc done`.
The traceback exception is `loadComponentFromURL(scalc)`, not
`list_nearby_files`. `#720` stored the keeper on `tests.testing_utils`;
suites import `plugin.tests.testing_utils` (same file, second object)
so every prepare printed `keeper=-` and never `setActiveFrame`. Same
dual-module family as #719 recycle. `set_harness_keeper_uid` now
imports both names and writes both; `prepare` / `reactivate` adopt
from the sibling if this copy is empty
(`html_paste_writer: keeper adopted from sibling`).

GHA 34597506651 (`054a03e0`, second #722 tip): keeper sync worked.
`document_research_uno` all three tests passed (`leftovers open=2
uids=['27','26'] frames=['-','-'] keeper=1`, `keeper reactivated`,
Calc factory loads finished, `list_nearby_files done status=ok`).
Linux PR CI 34597434533 was green. Hang moved to
`doc.test_text_helpers_uno`: first swriter (`…_paragraph_bold_run_no_newline`)
printed leftovers + keeper reactivate, created uid=34, `close_doc` OK.
The *next* swriter (`…_multi_para_joins_with_newline`) hung 30s in
`create_native_doc` after the same leftover log + keeper reactivate.
`setActiveFrame` is not enough for a second consecutive Hidden `_blank`
while leftover `_wa_calc_html` frames remain (same Windows frame-manager
collision `rich_html.py` already avoids).

GHA 34599838644 (`afae5938`, swriter-only named target): Linux PR CI
34599725706 green. Windows `workflow_dispatch` never reached
text_helpers. Same leftovers + `keeper=1` + reactivate, then Calc
`_blank` failed in ~1s (`Couldn't convert <traceback object> … getTypes`
on `loadComponentFromURL(scalc)`) and the next Calc `_blank` hung 30s
in `create_native_doc`. Calc `_blank` + leftovers is not reliable.
With leftovers open, leftover Hidden `swriter` reuses one CREATE|GLOBAL
name `_wa_factory` (same pattern as `rich_html._wa_calc_html`). Leftover
Calc later moved to `_wa_scalc` (34633295036). Leftover Draw/Impress
later moved to `_wa_sdraw` / `_wa_simpress` (34657826349). Do not close
leftovers (34556185752).

GHA 34601787293 (`fc34f0c6`) and 34602219973 (`ec40ed29`): Linux PR CI
34602130908 green. Unique targets loaded leftover Calc
(`document_research_uno` passed=3, `_wa_factory_1/2/3`) and the first
leftover Hidden swriter
(`doc.test_text_helpers_uno.test_get_string_without_tracked_deletions_paragraph_bold_run_no_newline`,
`target=_wa_factory_4`, uid=34, `close_doc` OK, leftovers still
uids=`['27','26']`). The *next* leftover Hidden swriter
(`…_multi_para_joins_with_newline`, `target=_wa_factory_5`) hung 30s in
`loadComponentFromURL` — no RuntimeException. Unique CREATE stacked
empty named frames after harness Writer close; it did not fix the
original consecutive leftover Hidden swriter hang (34597506651).
Windows then (1) reuses one CREATE|GLOBAL name `_wa_factory` for
leftover `swriter` so a direct `create_native_doc(writer)` replaces
instead of stacking, and (2) pools that first leftover Writer and
skips Writer `close_doc` while leftovers remain
(`native_doc: leftover writer reuse`,
`close_doc: skip writer close leftovers`).

GHA 34607010446 (master `3720c175`, `#722` merge, leftover Writer
reuse already past): `document_research_uno` and both text_helpers
tests OK. Draw suite: nine tests `TEST end … OK` including
`test_get_draw_tree` (same soffice `6200,2084`, leftovers
uids=`['34','27','26']` keeper=1). `test_insert_math_draw` printed
`TEST call`, factory `load done uid=48`, then
`LIFECYCLE close_doc dispose` (pids still `6200,2084`) and
`office dead after close doc_type=draw` (pids `-`). soffice exited 0.
`TEST returned` then fail-closed; remaining suites aborted. The
`close_doc dispose` line printed `previous=- current=-` because
`python -m` records the trail on `__main__` and `close_doc` imported
`plugin.testing_runner` (same dual-module family as #719 recycle).
Nine ordinary Draw `close_doc` calls survived, so leftover-Writer
reuse is not the Draw-close killer. `close_doc` of a Draw that still
holds Math OLE is. Windows therefore **skips** that close when
`mark_windows_math_ole_doc` recorded the uid
(`close_doc: skip math ole close (windows)`), reactivates the keeper,
and does **not** recycle mid-run (34551644954). POSIX still closes.

GHA 34609996253 / 34612145495 (this skip on the PR tip): leftover
Writer reuse and text_helpers still OK, then the *first* Draw close
(`draw.test_draw_forms_uno.test_draw_form_lifecycle`, uid=35) printed
`close_doc: start` / `close(True) start` and soffice exited 0.
Master 34607010446 closed that same forms Draw. The skip path had
walked pages/shapes and `_native_doc_svc` before `close(True)`.
Unmarked Draw close is again RuntimeUID + GC + 50 ms + `close(True)`
only. Do not walk for Math CLSID at teardown.
`test_insert_math_draw` runs last in `test_draw_uno.py` so a leftover
is not current for later tests in that file. On Windows the runner
also sorts `test_draw_uno.py` just before the peer suite
(`_native_suite_sort_key` band 1, peer band 2) so that leftover Draw
is not `close(True)`'d (34607010446) and is not recycled mid-run
(34551644954). GHA 34616287301: skip printed
`close_doc: skip math ole close (windows) uid=50 leftovers=3 keeper=1`,
`test_insert_math_draw` OK, later Impress suites OK, then
`notebook.test_import_filter_uno_detect_without_filtername` hung 30s.
GHA 34619751330 deferred `test_draw_uno` and the same notebook detect
hang happened *before* `insert_math` (soffice still `2444,5124`,
leftovers `['34','27','26']`). Leftover Math Draw is not that hang.
`test_import_filter_uno_load_component` Hidden `_blank` + FilterName
returned, then raw `close(True)`; the next Hidden `_blank` detect
hung — consecutive leftover Hidden `_blank` (34597506651). Windows
notebook loads now use `windows_notebook_load_args` (`_wa_notebook`,
CREATE|GLOBAL) and `TestingFactory.close_doc` (skip Writer close
while leftovers remain). Trail start/end now writes both runner
modules; `format_lifecycle_breadcrumb` adopts from the sibling if
this copy is empty.

POSIX still `close_doc`. Breadcrumbs:
`close_draw_family: raw close(True) start/done`,
`peer_message_uno: writer reactivated`,
`peer_message_uno: skip writer close after impress`,
`peer_message_uno: skip second impress close (windows)`,
`peer_message_uno: skip close (windows) uid=…`,
`peer_message_uno: close_doc start/done uid=…` (POSIX).
`html_paste_writer: leftovers open=N uids=…`,
`html_paste_writer: keeper reactivated`,
`create_native_doc: windows factory leftover_open=N url=… target=… flags=…`,
`native_doc: leftover notebook host reuse`,
`windows leftover skip: leftover simpress leftovers=`,
`windows leftover skip: latex dialog Hidden _blank .mml leftovers=`,
`windows leftover skip: math formula Hidden _blank .mml leftovers=`,
`windows leftover skip: math export Hidden _blank smath leftovers=`,
`windows leftover skip: document scripts Hidden _blank reopen leftovers=`,
`windows leftover skip: apply_document_content Hidden _default swriter leftovers=`,
`windows leftover skip: apply_style origin canary leftover reuse leftovers=`,
`windows hidden skip: create_native_doc Hidden _blank after system bitmap`,
`windows awt skip: slash_popup createPeer/setVisible`,
`slash_popup_uno: createPeer start/done setVisible start/done`,
`create_native_doc: load start/done` (leftover Windows factory),
`document_research_uno: store budget via active start/done`,
`document_research_uno: open_document_for_read start/done`,
`document_research_uno: store/list_nearby start/done`,
`close_doc: start/done uid= leftovers=` (Windows Writer),
`native_doc: leftover writer reuse`,
`close_doc: skip writer close leftovers open=N uid=…`,
`get_draw_tree: body start/execute done/body done`,
`close_doc: skip math ole close (windows) uid= leftovers= keeper= pids=`,
`insert_math_draw: insert_math start/done` and `body done`,
`import_filter_uno: load start/done` (`target=_wa_notebook` on
Windows), `html_paste_writer: noted leftover_open=`,
`windows leftover skip:`. Do **not** fold Draw-family settle into
`close_doc`. Not a product fix.

GHA 34606276107 (`248da30d`, leftover hang fixed) and master
34607010446 (`3720c175`, #722 merge): leftover paste + leftover-window
Writer stayed open (`leftovers open=3`). `document_research_uno` 3/3
and both `text_helpers_uno` tests OK. Draw factories loaded and
closed; `test_get_draw_tree` OK. `test_insert_math_draw` then
`LIFECYCLE close_doc dispose` (pids still live) and
`office dead after close doc_type=draw pids=-`. TEST returned; soffice
exited 0. `close_doc` dispose printed `previous=- current=-` — `-m`
lifecycle lives on `__main__`, close_doc imported
`plugin.testing_runner`. Nine Draw closes with the same leftovers
survived, so leftover-Writer reuse is not a proven Draw-close killer.
`#723` named Draw `close(True)` and adopted the lifecycle trail across
both runner copies. Windows now **skips** that Math OLE `close_doc`
(see the 34607010446 paragraph above). No product change.

GHA 34633295036 (master `bdb421bb`, tip of `#724`): typecheck + mock
pytest OK. `insert_cell_html_rich` again left uid=26 then uid=27
(`close skipped pasted=True`, `_wa_calc_html`). Chatbot suites OK;
grep suite skipped. `test_list_nearby_excludes_active` printed
`leftovers open=2 uids=['27','26'] frames=['-','-'] keeper=1`,
`keeper reactivated`, then unique leftover `scalc`
`target=_wa_factory_1 flags=63` failed in ~766ms (`Couldn't convert
<traceback object> … getTypes`; office alive; `pre_open` not a
dispose). `test_open_document_for_read_hidden_readonly` then hung 30s
on `target=_wa_factory_2`. Unique leftover Calc names are the same
stacking family as leftover swriter `_wa_factory_5` (34602219973).
The pooled `@with_native_doc` Calc is still the leftover_open=0
`_blank` workbook, so a shared leftover-Calc name does not replace
it. Leftover `scalc` now reuses one CREATE|GLOBAL name `_wa_scalc`.
`document_research_uno` writes Budget/Report via that pooled Calc
(`store budget via active`) and does **not** open a second factory
Calc. Leftover Draw/Impress later moved to `_wa_sdraw` /
`_wa_simpress` (34657826349).

GHA 34636251918 (this branch, after store-via-active):
`test_list_nearby_excludes_active` OK. `open_document_for_read` of
that Budget file then `loadComponentFromURL(..., "_default", 0)`
raised `Could not create system bitmap!` The next sibling open hung
30s at the same call (office alive). Leftover Hidden `_wa_calc_html`
frames poison `_default` / `_blank` (34597506651). Product
`open_document_for_read` now uses one CREATE|GLOBAL name
`_wa_doc_research` on Windows (same pattern as `rich_html._wa_calc_html`).
Hidden+ReadOnly and the reuse / close-flag contract are unchanged.
POSIX keeps `_default`.

GHA 34639913692 (`e0e75cc2`, after `_wa_doc_research`): factory hang
stayed gone (`store budget via active`, `test_list_nearby_excludes_active`
OK). Hidden sibling open of that same `storeAsURL` path still raised
`Could not create system bitmap!` in ~20ms; the next sibling open hung
30s. 34602219973 passed 3/3 when Budget lived on a closed factory Calc.
The UNO env now copies `Budget_read.ods` on Windows and Hidden-opens
that copy — a URL the pooled Calc never owned. No second factory Calc.
Do not close leftover paste Writers.

GHA 34643210006 (`60c827a8`): `document_research_uno` 3/3 OK
(`copied budget for hidden open`, `open_document_for_read done err=-`).
Later `test_calc_reuse_false_still_empty` leftover `target=_wa_scalc`
at leftover_open=5 hung 30s. `_wa_scalc` is not a safe leftover Calc
factory. `native_doc` now wipe-and-reuses the pooled Calc instead of
loading leftover scalc (`native_doc: leftover calc reuse`).

The same run's notebook fails were leftover-driven, not independent:
HTML-paste Writers (uids 26/27, `close skipped pasted=True`) set
`leftover_open>0`, so `close_doc` skipped **all** Writer closes —
including import-filter `_wa_notebook` leftovers (uids 41/42) that
still held form listeners. `form_run_listeners()` / 
`wired_run_listener_count` counted every leftover doc
(`duplicate listeners: 3`). Counts are now per-document. Notebook
suites use `_wa_notebook_host` (not leftover HTML-paste reuse).
`close_doc` still skips leftover paste Writers. GHA 34646877587
closed notebook leftover uid=41 then hung 30s on the next Hidden
`_wa_notebook` (leftovers open=3). Do not close leftover notebook
docs. Unique leftover `_wa_notebook_2` is the same stacking family
as leftover `_wa_factory_N`. The detect reload
`skip_windows_leftover_hidden_load`s when leftovers are already
open. Do **not** skip `document_research_uno` Hidden
`Budget_read.ods`.

GHA 34648929578 / 34649699848 (`86279d14`): `#732`'s
`windows_notebook_load_args` / leftover-notebook `close_doc` skip
do **not** run until `notebook.test_import_filter_uno` (after
`document_research_uno`). They do not change `_wa_doc_research`.
The bitmap fail / slash `createPeer` hang are leftover
`_wa_calc_html` paste Writers from `test_formulas_uno` /
`test_rich_html_uno` (same GDI family as 34636251918). Windows
defers those two suites until after slash, `document_research_uno`
3/3, and import-filter (`_native_suite_sort_key` band 1 with
`test_draw_uno`). HTML-paste UNO tests `note_windows_html_paste_leftover`
after a successful paste. Do not close leftover paste Writers.

GHA 34652644656 (`3d39d9f2`, paste suites deferred): slash OK.
`document_research_uno` 3/3 (`copied budget for hidden open`,
`open_document_for_read done err=-`, leftovers still 0). First
text_helpers Hidden `_blank` + `close_doc` uid=29 leftovers=0
returned; the next Hidden `_blank`
(`…_multi_para_joins_with_newline`) hung 30s. Consecutive Hidden
`_blank` swriter is unsafe even without paste leftovers.
`_windows_should_reuse_writer` now reuses the first Windows Writer
without requiring leftover_open>0 (notebook host still isolated).

GHA 34655847157 (master `f88b8749`): leftover_open=0, paste suites
deferred, slash OK, `test_list_nearby_excludes_active` OK. Hidden
`Budget_read.ods` then `Could not create system bitmap!` in ~20ms;
`test_inner_read_cell_range_on_opened_sibling` hung 30s at
`open_document_for_read`. Same copy path was 3/3 on 34652644656.
After a Windows bitmap, later Hidden sibling opens
`skip_windows_hidden_open_after_bitmap` — do not hang. Still attempt
the first Hidden-open. Not a product change.

GHA 34670295632 (PR #742, `f278ca78`): leftover Impress skip is not
that hang. `document_research_uno` Hidden `Budget_read.ods` bitmap-failed;
#734 skipped later siblings (`windows hidden skip: document_research
Hidden Budget_read after system bitmap`, suite 3/3). Next suite
`text_helpers` printed `native_doc: leftover writer reuse` then
`create_native_doc leftover_open=0 url=private:factory/swriter
target=_blank flags=0` and hung 30s. After bitmap, Hidden `_blank`
factory is unsafe. `create_native_doc` now skips Hidden `_blank`
(`windows hidden skip: create_native_doc Hidden _blank after system
bitmap`). Named leftover factories (`_wa_factory` / `_wa_scalc` /
`_wa_sdraw` / `_wa_simpress`) still load. Not a product change.

GHA 34671277292 (master `dc3be8d6`, #742 live; #743 Hidden `_blank`
skip is not this hang): after full calc UNO suites (all green),
`chatbot.test_slash_popup_uno.test_slash_popup_listbox_filter_and_keys`
printed `TEST call` then hung 30s. Main thread was
`dlg.setVisible(True)` after `createPeer(toolkit, None)` (test line 41
on that tip). Office stayed alive (`kill-libreoffice.ps1` then killed
the same soffice PIDs). Worker threads were `worker_pool._loop` /
`_drain_soffice_stderr`. Leftover_open was **not** required —
`skip_windows_leftover_hidden_load` would not have fired. Overlay
`createWindow` TOP + later `setVisible` is the same Windows-headless
VCL map. Windows now `skip_windows_awt_top_dialog`s before
`createPeer` / `setVisible` (`windows awt skip: slash_popup
createPeer/setVisible`). Linux UNO still maps the overlay. ENABLE_SLASH
stays parked. Not a product change.

GHA 34657826349 / 34657808315 (master `0bf7d223`, tip of #734):
#734's Hidden-open skip is not that hang. `document_research_uno`
3/3 and both text_helpers tests OK. Leftover Draw/Impress unique
`_wa_factory_1`–`_wa_factory_9` succeeded at leftover_open=1
(uid=29 leftover Writer). Notebook / importer suites then skipped
Writer close (`leftover_open` 1→15, `close_doc: skip writer close`).
Stable leftover `swriter` `target=_wa_factory` at leftover_open=15
returned (`scripting.test_document_scripts_uno`). Unique leftover
`simpress` `target=_wa_factory_10` then hung 30s in
`create_native_doc` (`test_lo_import_minimal_pptx_multi_slide`;
office still `4072,8924`). Same stacking family as leftover
swriter `_wa_factory_5` (34602219973) and leftover scalc
`_wa_factory_2` (34633295036). Leftover `sdraw` / `simpress` now
reuse one CREATE|GLOBAL name each (`_wa_sdraw` / `_wa_simpress`).
Do not close leftover paste / notebook Writers (34556185752 /
34646877587). Not a product change.

GHA 34661915875 (master `aa6bf058`, tip of #737): leftover
`_wa_simpress` is live. Notebook / importer skipped Writer close
(`leftover_open` 1→15). Leftover `_wa_factory` swriter at
leftover_open=15 returned (`scripting.test_document_scripts_uno`).
Leftover `simpress` `target=_wa_simpress flags=63` then hung 30s in
`create_native_doc` (`test_lo_import_minimal_pptx_multi_slide`). Hang
is **not** unique `_wa_factory_N` stacking. High leftover Writer
count wedges a new app factory. Windows now (1) reuses leftover
`_wa_notebook_host` so leftover_open does not climb
(`native_doc: leftover notebook host reuse`) and (2) skips leftover
Draw/Impress factory when leftover_open>2
(`windows leftover skip: leftover simpress leftovers=N`). Leftover
Writer / leftover Calc still load. Do not close leftover paste /
notebook Writers.

GHA 34672065355 (master tip of #742+#743): leftover `_wa_simpress`
at leftover_open=3 hung 30s (`test_lo_import_minimal_pptx_multi_slide`;
leftovers uids 40/39/29 Writer leftovers from notebook/importer).
Old max=4 only skipped leftover_open>4, so leftover_open=3 still
loaded leftover Draw/Impress. Skip leftover_open>2.

GHA 34675151298 (master `71640e30`, #744+#745): leftover `simpress`
at leftover_open=3 SKIPPED (`windows leftover skip: leftover
simpress leftovers=3`). Slash TOP dialog SKIPPED (`windows awt
skip: slash_popup createPeer/setVisible`). Then
`writer.math.test_latex_dialog_uno.test_insert_latex_math_dialog_success`
printed `native_doc: leftover writer reuse` and hung 30s in
`convert_mathml_to_starmath` `loadComponentFromURL(..., "_blank",
Hidden)` (office alive). XDL `LatexInputDialog` is patched — hang
is leftover Hidden `_blank` `.mml`, not AWT TOP `createPeer` /
`setVisible`. Converting latex-dialog UNO tests now
`skip_windows_leftover_hidden_mathml` (`windows leftover skip: latex
dialog Hidden _blank .mml leftovers=N`).

GHA 34678020608 (PR #746 tip `a9d44413`): latex converting tests
SKIPPED as intended. Next suite
`test_convert_mathml_to_starmath_fraction` printed leftover writer
reuse and hung 30s at the same Hidden `_blank` `.mml` load. Later
convert / export / document-helpers math UNO tests now use the
same leftover Hidden Math skip.

GHA 34679494812 (PR #746 tip `fefc89fc`): leftover_open=3
(`html_paste_writer: leftovers open=3 uids=['40', '39', '29']`
keeper=1 reactivated). `create_native_doc` leftover swriter
returned (`uid=41`). Then `test_document_scripts_survive_save_reopen`
hung 30s in attach / storeAsURL / raw `doc.close(True)` / Hidden
`_blank` reopen (office alive). 34678020608 returned here and died
later on Hidden `.mml`. Same leftover Hidden family as import-filter
detect. Windows now `skip_windows_leftover_hidden_load`s
(`windows leftover skip: document scripts Hidden _blank reopen`).
Do not raw-close leftover Writers. Not a product change.

GHA 34681661844 (PR #746 tip `5d5d1232`): document-scripts /
MathML leftover Hidden skips fired (suites green). Then
`test_apply_document_content_preserves_heading_level_span_uno` OK
(leftover writer reuse). Next
`…_preserves_heading_level_b_uno` hung 30s in
`html_to_plain_text` `loadComponentFromURL(private:factory/swriter,
"_default", Hidden)` (office alive). First leftover Hidden `_default`
Writer + close returned; the second hung. Same leftover Hidden
family. Apply-content UNO tests now
`skip_windows_leftover_hidden_apply`
(`windows leftover skip: apply_document_content Hidden _default
swriter leftovers=N`). Not a product change.

GHA 34683742049 (PR #746 tip `d6b6319a`): heading-rewrite /
structured-return / table-cell / whitespace leftover Hidden apply
skips fired (suites green). Then
`writer.test_content_style_model_uno.test_write_compact_heading1_resolves_to_spaced_uno`
hung 30s in `html_to_plain_text`
`loadComponentFromURL(private:factory/swriter, "_default", Hidden)`
via `ApplyDocumentContent.execute` (office alive). Same leftover
Hidden `_default` family as heading-rewrite `_b_uno`. Content-style
write `_tool_ctx` plus later apply UNO files (`test_format_uno`,
`test_track_changes_reviewable_uno`) now
`skip_windows_leftover_hidden_apply`. Not a product change.
Same run:
`test_apply_style_known_limitation_direct_equals_old_default_uno`
FAIL (`origin detection improved`) under leftover writer reuse;
suite continued (passed=10 failed=1) until the later apply hang.
Linux still pins CharWeight=150; `format.py` still compares value
vs old style default. Windows leftover skip
(`windows leftover skip: apply_style origin canary leftover reuse`)
— leftover pollution, not a product change.

**Windows proof** still needs a `workflow_dispatch` of PR CI on the
branch: `os=windows-latest`, `ci_debug=true`. Look for
`html_paste_writer: leftovers open` with a real `keeper=` uid (not
`-`), optional `keeper adopted from sibling`, and `keeper reactivated` before
Windows `private:factory/` loads (Writer **and** Calc), leftover
swriter loads using `target=_wa_factory` (not `_blank` / not
`_wa_factory_N`) plus leftover Calc using `target=_wa_scalc` (not
`_blank` / not `_wa_factory_N`) plus leftover Draw/Impress using
`target=_wa_sdraw` / `target=_wa_simpress` (not `_wa_factory_N`),
`document_research_uno: store budget via active start/done` (no
`create budget calc` / no leftover `scalc` `target=_wa_factory_1`),
`copied budget for hidden open` then `open_document_for_read done err=-`
or `windows hidden bitmap` / Hidden-open `TEST end … SKIP` after
system bitmap (no 30s hang on the next sibling open) **or**
text_helpers `TEST end … SKIP` with
`windows hidden skip: create_native_doc Hidden _blank after system
bitmap` (no 30s Timeout on leftover_open=0 `target=_blank`) **before**
`html_paste_writer: noted leftover_open` (formulas / rich_html are
deferred),
`create_native_doc: load done`, `native_doc: leftover writer reuse` and
no second leftover swriter factory after the first text_helpers Writer,
`document_research_uno` three tests
`TEST end … OK`, slash `TEST end … OK` or `TEST end … SKIP` with
`windows awt skip: slash_popup createPeer/setVisible` (no 30s Timeout
on `dlg.setVisible(True)`), both text_helpers tests
`TEST end … OK` (`native_doc: leftover writer reuse` on the
second; no second Hidden `_blank` / no 30s Timeout in
`create_native_doc` on `…_multi_para_joins_with_newline`),
`draw.test_draw_forms_uno` four tests `TEST end … OK` (first Draw
`close(True)` must return; no `close_doc: start uid= svc=draw` before
that close), `insert_math_draw: insert_math start/done` then
`close_doc: skip math ole close (windows)` (not
`LIFECYCLE close_doc dispose` / `office dead after close doc_type=draw`),
`import_filter_uno: load start target=_wa_notebook` (not `_blank`)
for both notebook import-filter tests when leftovers are not yet
open, `close_doc: skip writer close` or `close_doc: start` (not raw
`doc.close(True)`), both `notebook.test_import_filter_uno` tests
`TEST end … OK` or detect `TEST end … SKIP` if leftovers already
exist (no 30s Timeout on `detect_without_filtername`), leftover
`simpress` after notebook leftovers using `target=_wa_simpress`
(not `_wa_factory_10`) then
`TEST end uno.test_ppt_master_pptx_import_uno.test_lo_import_minimal_pptx_multi_slide OK`
or `TEST end … SKIP` with `windows leftover skip: leftover simpress`
when leftover_open>2 (no 30s Timeout in `create_native_doc` on leftover
Impress), leftover notebook host using
`native_doc: leftover notebook host reuse` (leftover_open stays low),
latex dialog converting tests `TEST end … SKIP` with
`windows leftover skip: latex dialog Hidden _blank .mml` when
leftover_open>0 (no 30s Timeout in `convert_mathml_to_starmath`
Hidden `_blank` `.mml` after leftover writer reuse), later math
convert/export tests `TEST end … SKIP` with
`windows leftover skip: math formula Hidden _blank .mml` or
`windows leftover skip: math export Hidden _blank smath` (no 30s
Timeout on `test_convert_mathml_to_starmath_fraction`),
document-scripts save/reopen `TEST end … SKIP` with
`windows leftover skip: document scripts Hidden _blank reopen`
when leftover_open>0 (no 30s Timeout on leftover Writer
`doc.close(True)`), apply-content heading rewrite, later apply UNO
(content-style write, format apply, track-changes apply)
`TEST end … SKIP` with
`windows leftover skip: apply_document_content Hidden _default
swriter` when leftover_open>0 (no 30s Timeout on
`html_to_plain_text` Hidden `_default` swriter after leftover
writer reuse — GHA 34681661844 heading `_b_uno`, GHA 34683742049
content-style `test_write_compact_heading1_resolves_to_spaced_uno`),
apply-style origin canary `TEST end … SKIP` with
`windows leftover skip: apply_style origin canary leftover reuse`
when leftover_open>0 (no FAIL claiming origin detection improved —
GHA 34683742049 leftover reuse; Linux still pins CharWeight=150),
**then**
`html_paste_writer: noted leftover_open=1` from deferred formulas /
rich_html, **then**
`TEST end draw.test_draw_uno.test_insert_math_draw OK` after
`insert_math_draw: insert_math start/done` and
`close_doc: skip math ole close (windows)`, leftover-Impress
suites, then the peer file last (six peer `TEST end … OK`), then
`LIFECYCLE recycle office skipped; no remaining suites`. Ubuntu PR CI
is the automatic gate; this cloud agent cannot run `windows-latest`.

**Harness-only attribution (not a product fix):** if a test body returns OK
but the office already aborted, the runner fails *that* test instead of
letting the next factory open be the named victim:

- `Unspecified Application Error` on office/Python stderr (VCL `SalAbort`)
- harness soffice `Popen.poll()` already exited
- `getServiceManager` disposed (`LIFECYCLE office dead after TEST returned`)

`create_native_doc` probes before load (`pre_open=disposed` skips the load
and still raises a URP-shaped error). No office auto-restart. No skip/xfail.

Headless/user-profile bootstrap now `PIPE`s soffice **stderr** and drains it
on a dedicated thread so the SalAbort line is not only inherited onto the
terminal. `TEST end … OK` also prints `exit=` (`-` while the child lives).

## Findings: Unspecified Application Error

QA soak (`make test-uno-soak PAIR=dup-move REPEAT=20`): mid
`test_duplicate_slide_copies_shapes` stderr prints `Unspecified Application
Error`, the test still returns OK, then `test_duplicate_rename_move_slide`
fails `Binary URP bridge already disposed` (pids gone). `PAIR=tree-math`:
same Error during `test_get_draw_tree_marks_blank_and_label_hint` (OK), then
the next soak iter dies at `_ensure_live_ctx`.

**This is not a Draw assertion flake.** Python finished the killer; soffice
did not.

The exact string is **not** a WriterAgent message. LibreOffice VCL
`SalAbort` (`vcl/source/app/salplug.cxx`) prints it when the abort text is
empty, then `abort()` or `_exit(1)`:

```
if (rErrorText.isEmpty())
    std::fprintf(stderr, "Unspecified Application Error\n");
```

So the Error **is** office death (empty-text SalAbort).

### gdb catch (box QA on this branch)

Attached gdb to soak `soffice.bin` during solo
`FILTER=test_duplicate_slide_copies_shapes`. Faulting `cppu_threadpool`
thread:

`Application::Abort` ← signal handler ← `SfxItemSet::ClearSingleItem_PrepareRemove`
← `ClearAllItemsImpl` / `~SfxItemSet` ← `~SdrObject` ← `~SdrRectObj` ←
`~SvxShape` ← `OWeakAggObject::release` ← URP / `uno_Environment_invoke`.

**Reading:** SalAbort during **SvxShape / SdrRectObj destruction** while
clearing an `SfxItemSet` on a URP release thread after close — same general
family as the historical octagon `SfxItemPool::unregisterNameOrIndex` abort
on rect teardown. dbgsym offline resolve confirmed `#5` is that same
`unregisterNameOrIndex` (.cold) under `SvxShapeRect` teardown (see
`salabort-svxshape-close.md`); file:line still thin under LTO.
Harness `close_doc` now GC + 50 ms then close (still not an LO cure).

Full write-up: [salabort-svxshape-close.md](salabort-svxshape-close.md).
`#687`'s post-OK `getServiceManager` probe can miss the race: SalAbort can
print while URP still answers, then the process exits before the next open.

### Ranked hypotheses

1. **VCL SalAbort during Draw UNO or `close_doc`, URP lags** (best fit).
   Confirm: fail-closed names the killer; `soffice_exit=` is `1`; stderr
   tail has `terminate called after…` or a real abort text; pids gone at
   that TEST end.
2. **`XDrawPageDuplicator.duplicate` (`doc.duplicate`) headless crash**
   (`PAIR=dup-move` body). Confirm: isolate `duplicate_slide` only; Error
   during the tool call vs during teardown; `activate=False` vs `True`.
   Does **not** explain the blank/label killer.
3. **Draw `TextShape` create / empty `getString` / `get_draw_tree` property
   walk** (`test_get_draw_tree_marks_blank_and_label_hint`). Confirm: soak
   that test alone; Error during `shape_upsert` vs `get_draw_tree` vs close.
4. **`--pair tree-math` prefix bleed** (harness fact, now fixed). Filter
   `test_get_draw_tree` also matched `test_get_draw_tree_marks_blank_and_label_hint`.
   QA’s tree-math Error on the blank test may be that extra test, not
   `test_get_draw_tree` → math OLE. `--pair` is now exact; `FILTER=` still
   prefix-matches.
5. **`close_doc` / `gc.collect` + Draw model teardown** (shared by both
   killers: factory `sdraw` + always-close). Confirm: Error after body
   return / `LIFECYCLE close_doc` vs during the tool call.
6. **Use-after-close or stale page/shape proxy** (weaker). Confirm: Error
   only when the body still holds `page.getByIndex` / `getString` across
   duplicate; not when the body is a no-op close.
7. **Headless VCL / hidden controller (`setCurrentPage`)** (weaker; would
   be more deterministic). Confirm: `--visible` soak vs headless.
8. **Heap corruption / delayed abort** (historical Arch glibc
   `test_get_draw_tree` → math). Confirm: malloc/`terminate` line before
   SalAbort; Arch-only. Do not require Arch to hunt this.

What the two killer tests do (no product change):

- `test_duplicate_slide_copies_shapes`: `shape_upsert` rectangle + text,
  `duplicate_slide(page=0, activate=False)` → `DrawBridge.duplicate_slide`
  → `self.doc.duplicate(source)`, then `getString` on the copy.
- `test_duplicate_rename_move_slide` (usual victim): `duplicate_slide`
  (activate default True), `rename_slide`, `move_slide` (`remove` +
  `insertByIndex`).
- `test_get_draw_tree_marks_blank_and_label_hint`: two `TextShape`s
  (label + empty named blank), `get_draw_tree` → `build_shape_tree`
  (`getString`, geometry, `FillColor` / `CustomShapeGeometry`).

## What the breadcrumb prints

On every native **FAIL**, stderr `TEST end … FAIL` includes:

```
previous=<suite.test> result=OK|FAIL|SKIP|- end_pids=<soffice at that end>
last_ok=<last successful TEST> dt_ms=<ms from that end to this start>
current=<victim> start_pids=… now_pids=… pids_changed=0|1
bridge=alive|disposed|no_probe|error:…
```

`bridge=` is `ctx.getServiceManager()` at **TEST start** (same check as
`_ensure_live_ctx`). Factory-open failures also include `pre_open=` from a
probe immediately before `loadComponentFromURL`:

- `pre_open=disposed` — office was already dead; the previous TEST end is the
  killer (hypothesis confirmed for that fail).
- `pre_open=alive` then dispose on load — died *during* this open (less like
  “previous close toasted the bridge”).

A URP dispose also prints `LIFECYCLE URP dispose at <victim> previous=…`.
SalAbort / child exit after an OK body prints
`LIFECYCLE application error after TEST returned` (or
`office death before TEST call` for the gap). That is harness attribution,
not a product retry.

Grep: `LIFECYCLE`, `previous=`, `Unspecified Application Error`, `soffice_exit=`.
The victim is `current=`; the likely killer is `previous=` / `last_ok=` when
`result=OK`.

Native tests do **not** run under pytest. `format_lifecycle_breadcrumb` /
`probe_uno_bridge` / `collect_post_test_death` in `plugin/testing_runner.py`
are the hook. Unit tests: `tests/framework/test_testing_runner.py`,
`tests/scripts/test_testing_runner_cli.py`, `tests/test_testing_utils.py`.

## Soak / reproduce (same soffice, no new framework)

Tight loop in **one** office process (this is the lifecycle under test):

```bash
# Full Draw UNO suite, 20 rounds (default)
make test-uno-soak

# More rounds
make test-uno-soak REPEAT=50

# Historical pair (tree → math OLE factory open). Exact names only.
make test-uno-soak PAIR=tree-math REPEAT=50

# CI victim + its predecessor in file order
make test-uno-soak PAIR=dup-move REPEAT=50

# FILTER still prefix-matches: test_get_draw_tree also selects
# test_get_draw_tree_marks_blank_and_label_hint. Prefer PAIR= for isolation.
make test-uno-soak FILTER="test_get_draw_tree test_insert_math_draw" REPEAT=50
```

Equivalent without the Make target:

```bash
WRITERAGENT_UNO_SOAK=20 make test-uno FILTER=test_draw_uno
python -m plugin.testing_runner --repeat 20 test_draw_uno
python -m plugin.testing_runner --repeat 50 --pair tree-math
```

Look for `SOAK iter i/N`, then the first `LIFECYCLE` / `previous=` /
`application error` on FAIL. Outer `for i in …; do make test-uno …; done`
restarts soffice each time and is a **weaker** repro for “bridge died
between two tests.”

`test-uno-soak` is not in default CI. Invoke it from a CI note or a
workflow_dispatch job when hunting this flake.
