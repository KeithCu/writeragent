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
| Writer `@with_native_doc` | No (unless `reuse=True`) | Factory each test | `close_doc` (`gc.collect` + 50 ms + `close`) |
| Draw / Impress | **Never** | Factory each test (`private:factory/sdraw`) | Always `close_doc` |

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
Impress, 34537826720). CharWeight itself is not the hung call. Before a
Windows Writer factory the harness now closes non-keeper leftover
Writers and `setActiveFrame`s the keeper
(`html_paste_writer: close leftover start/done`,
`html_paste_writer: keeper reactivated`).

POSIX still `close_doc`. Breadcrumbs:
`close_draw_family: raw close(True) start/done`,
`peer_message_uno: writer reactivated`,
`peer_message_uno: skip writer close after impress`,
`peer_message_uno: skip second impress close (windows)`,
`peer_message_uno: skip close (windows) uid=…`,
`peer_message_uno: close_doc start/done uid=…` (POSIX).
`html_paste_writer: close leftover start/done uid=…`,
`html_paste_writer: keeper reactivated`,
`create_native_doc: windows writer factory leftover_closed=N`,
`close_doc: start/done uid=…` (Windows Writer). Do **not** fold
Draw-family settle into `close_doc`. Not a product fix.

**Windows proof** still needs a `workflow_dispatch` of PR CI on the
branch: `os=windows-latest`, `ci_debug=true`. Look for
`html_paste_writer: close leftover` before the first text_helpers
Writer load, `keeper reactivated`, both text_helpers tests
`TEST end … OK`, later suites including the peer file last (six peer
`TEST end … OK`), then
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
