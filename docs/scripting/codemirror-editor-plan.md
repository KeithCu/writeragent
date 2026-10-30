# Plan: CodeMirror 6 Python editor alongside Monaco

**Goal:** add a CodeMirror 6 (CM6) frontend to the existing Python editor (Edit Python in Cell, Run Python Script, Edit Init Script, LaTeX). One config option picks the editor. Monaco stays the default. No other settings and no debug-only paths. Shared by WriterAgent and LibrePy: LibrePy is not a separate repo. It's built from this tree (`plugin/librepy/`, `extension-core/`, and `scripts/librepy_bundle_paths.py`, which already includes `plugin/contrib/scripting/assets/editor/`).

## 1. How the editor works today
- **It's a pywebview window in a child process, not a UNO dialog.** The child runs in the user's configured venv, not in LibreOffice's Python. A native UNO dialog is the fallback.
  - Host side: `plugin/scripting/editor_host.py`. `resolve_editor_python` finds the venv interpreter, `probe_webview_import` checks it, and `spawn_editor_process` starts the child. `PersistentEditor` keeps one child alive across opens. `EditorSession` and `launch_monaco_editor` drive each open, and `monaco_editor_available` decides between the web editor and the native dialog. Dirty-buffer handling lives in `confirm_unsaved_monaco_edit` and `queue_save_then_load`.
  - IPC: `plugin/scripting/editor_ipc.py` over stdin/stdout, using `!I` length-prefixed pickle protocol 5 (same as `worker_harness.py`). UNO calls are marshalled to the main thread with `QueueExecutor`.
  - Child: `plugin/scripting/venv/editor_main.py`. `MonacoEditorApi` is the pywebview `js_api` (`poll_messages`, `notify_save`, `notify_run`, `notify_dirty`, `notify_cancel`, `get_completions` via jedi, `is_jedi_available`). `wsgi_app` serves `assets/editor/*` and Monaco `vs/*`, and `monaco_static_path` guards against path traversal. `main()` refuses to start without the `rocher` venv package, which provides Monaco's `vs/` files.
  - Frontend: `plugin/contrib/scripting/assets/editor/{index.html, editor.js, scripts_manager.js, style.css}`. `index.html` loads `vs/loader.js`. `editor.js` mixes the toolbar/IPC shell with Monaco calls: `applyLoadMessage`, `getEditorCode`, `require(["vs/editor/editor.main"])` (around line 384), a LaTeX Monarch language, a jedi completion provider (around line 498), and `monaco.editor.create` (around line 552).
- **Callers.**
  - Edit Cell: `plugin/calc/python/editor.py` `_open_python_cell_editor_impl` checks `monaco_editor_available`, then calls `_launch_editor_with_code`, which calls `launch_monaco_editor`. Otherwise it calls `cell_editor_ui.show_native_python_cell_editor` (`NativePythonCellEditorDialog`, `extension/Dialogs/PythonCellEditorDialog.xdl`).
  - Run Script: `plugin/scripting/python_runner.py` (Monaco), or the native `python_runner_ui.py` (`PythonScriptDialog.xdl`; `scripting.native_run_script_modeless`).
  - Init script: `plugin/calc/python/init_script_editor.py` `open_init_script_editor`.
  - LaTeX: `plugin/writer/math/latex_dialog.py`.
- **Load and save.** `_load_cell_editor_code` / `_parse_cell_python_formula` strip `=PY("…")` to source, and follow `=PY($A$1;…)` refs through `_resolve_code_ref_cell`. Save goes through `_apply_cell_save`, which calls one of:
  - `_apply_formula_save`: `setFormula` wraps `=PY("…")` and keeps the data args, via `formula_edit.py`.
  - `_apply_plain_text_save`: "Save without =PY()", which is `setString(code)`.
  - `_apply_followed_ref_save`: writes the referenced text cell.

  One save is one undo step. Monaco and CM6 only produce *text*, so all of this is backend-independent and stays as-is.

## 2. Web view options
- The only web view is **pywebview**, always in the venv child. It's never loaded inside soffice's Python (`editor_host.py` around line 113: "LibreOffice's built-in Python cannot run the Monaco editor"). `venv_diagnostics.py` (`_UI_PACKAGE_KEYS`: webview, rocher, jedi, PyQt6/QtWebEngine, qtpy) reports the GUI backend: GTK/WebKit2 or Qt WebEngine on Linux, Edge WebView2 on Windows, WKWebView on macOS. Flatpak spawn is still open (2F in `monaco-editor-dev-plan.md`).
- IPC is the pipe protocol above. CM6 needs **no new IPC**: it runs in the same child and talks to the same `MonacoEditorApi`.

## 3. Shipping CM6
- CM6 is MIT-licensed, and its packages are modular: `@codemirror/{state,view,commands,language,autocomplete,lint,search}`, `@codemirror/lang-python`, `@lezer/python`, plus a theme such as `@codemirror/theme-one-dark`.
- Ship it as a **prebuilt, vendored, single IIFE bundle** with no npm at runtime:
  - The bundle is `plugin/contrib/scripting/assets/editor/cm6/cm6.bundle.js` (about 250–400 KB minified, about 100 KB gzipped; measure in PR 2), next to `LICENSE-codemirror.txt`.
  - It's built once with `scripts/build_cm6_bundle.sh` (esbuild, pinned versions in a small `scripts/cm6/package.json` + lockfile), and the output is committed. The build isn't part of `make build` or `make release`. `plugin/contrib` is already excluded from lint (`Makefile` OPENGREP_EXCLUDES), and LibrePy picks it up through `librepy_bundle_paths.py`.
- Unlike Monaco (which comes from `rocher` in the venv), CM6 lives in the .oxt. So the CM6 path needs only pywebview, not rocher. Add the bundle to whatever third-party notice list the release uses. `make release` (Makefile around lines 330–353: vendor, manifest, compile-translations, schema-docs...) needs no new target.

## 4. Design
- **Config key:** `scripting.python_editor` in `plugin/scripting/module.yaml`. Use `type: string`, `widget: select`, `default: monaco`, options `monaco` (Monaco, default) / `codemirror` (CodeMirror), `public: true`, in the same shape as `python_session_mode`. Regenerate `plugin/_manifest.py` (`make manifest`) and `docs/writeragent-config-schema.md` (`make schema-docs`). This is the only new setting. `force_internal_script_editor` keeps its meaning: native dialog, which overrides both.
- **Frontend interface:** split `editor.js` into:
  - `editor_shell.js`: toolbar, IPC polling, dirty/save/run, theme, and strings. It's almost all of today's `editor.js`, with Monaco calls replaced by `backend.*`.
  - A backend object `{ create(el, opts) -> Promise, getValue(), setValue(text), setLanguage(lang), setTheme(isDark, name), setMarkers([{line, col, endLine, endCol, message, severity}]), onChange(cb), focus() }`.
  - `backend_monaco.js` (today's code moved over) and `backend_cm6.js` (wraps the `CM6` global from the bundle).
- **Selection:** the host adds `editor_backend` to the load message (`editor_host.py`, where load messages are built). `index.html` loads `editor_shell.js`, and the shell loads `vs/loader.js` + `backend_monaco.js`, or `cm6/cm6.bundle.js` + `backend_cm6.js`, on first load. Changing the setting while the persistent child runs is handled like `_on_config_changed` already handles `force_internal_script_editor`: terminate the child, and the next open respawns it.
- **Fallback chain:**
  - codemirror: needs pywebview; otherwise the native dialog.
  - monaco: needs pywebview + rocher; if rocher is missing, the native dialog (today's behaviour).
  - Make the rocher check backend-aware: `main()` should `_fatal` on a missing rocher only when Monaco is selected. `monaco_editor_available` should become `web_editor_available` (keep the old name as an alias, or rename it in the same PR).
- **Sharing with Online:** keep `backend_cm6.js` and the bundle free of pywebview and UNO. Only `editor_shell.js` knows about `window.pywebview.api`. The same backend interface and Python language setup can be reused by the Collabora Online CM6 modal (`~/Desktop/jailsafe-plan/RESEARCH-online-monaco.md`), with an Online-specific shell that talks to the socket instead.

## 5. Python features
- **Highlighting and indentation:** `@codemirror/lang-python` (Lezer) gives highlighting, smart indent, bracket matching, folding, and `indentUnit` 4 spaces; Tab indents (`indentWithTab`).
- **Completion:** reuse jedi through `pywebview.api.get_completions` as a CM6 `autocompletion` source, so both backends behave the same, including the jedi-missing hint.
- **Error markers:** diagnostics already exist. `plugin/calc/python/diagnostics.py` (`DiagnosticEntry` with `status`, `message`, `traceback`) feeds the sidebar. When the editor opens a cell that has a recent error, or after Run fails, the host sends `markers` (line parsed from the traceback's last `File "<cell>", line N`) in the load/status message. The shell calls `backend.setMarkers` (CM6 `@codemirror/lint` `setDiagnostics`; Monaco `setModelMarkers`). This needs no new settings.
- **LaTeX mode:** CM6 has `@codemirror/legacy-modes` stex. Include it in the bundle so LaTeX works the same, or keep LaTeX on Monaco for the first version and document that.

## 6. Tests
- **Today:** Python unit tests with pytest under `tests/scripting/`: `test_editor_host.py`, `test_editor_ipc.py`, `test_editor_main.py`, `test_editor_main_closed.py`, `test_editor_static_path.py`, `test_editor_jedi.py`, `test_editor_ui_strings.py`, `test_editor_errors.py`. Native dialog state is covered with `reset_native_cell_editor_for_tests`. There are no browser or JS tests.
- **Testable headless:**
  - Config default and options (manifest/schema).
  - The load message carrying `editor_backend`.
  - `wsgi_app` serving `cm6/cm6.bundle.js` and not depending on rocher when CM6 is selected.
  - Fallback decisions (pywebview missing, rocher missing, per backend).
  - Traceback-to-marker line parsing.
  - The child restarting on config change.
- **Manual:** opening the window, per platform. Optionally, one smoke check that the bundle file exists and exports `CM6.EditorView` (a plain string or size check, no browser).

## PR steps (each reviewable on its own)
1. **Refactor only:** split `editor.js` into `editor_shell.js` + `backend_monaco.js` behind the backend interface. No behaviour change; existing tests pass.
2. **Vendor CM6:** `scripts/build_cm6_bundle.sh`, pinned `scripts/cm6/package.json` + lock, committed `cm6/cm6.bundle.js` + LICENSE, and the notice entry. Not wired in yet.
3. **Config + selection:** add `scripting.python_editor` (module.yaml, manifest, schema doc), put `editor_backend` in the load message, add `backend_cm6.js` (text, language, theme, dirty, save/run), make the rocher requirement backend-aware, and restart the child on config change. Add tests.
4. **CM6 completion via jedi:** parity with Monaco.
5. **Error markers for both backends:** traceback-to-line parsing on the host, plus `setMarkers`.
6. **LaTeX in CM6** (stex legacy mode), or explicitly keep it on Monaco.
7. **Later:** after Keith decides, flip the default to `codemirror` (one-line YAML change), possibly drop the rocher requirement from docs, and reuse `backend_cm6.js` for Online.
