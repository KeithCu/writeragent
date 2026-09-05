# UNO utility helpers — inventory, overlaps, and future-work plan

**Status:** analysis, plus three landed P1 unifies (public `normalize_doc_url`; shared `normalize_file_url` / `get_document_path` `file:/` repair; shared UNO-free `url_utils.path_to_file_url`). Remaining recommendations below are deferred.

This is the catalog of **shared UNO utility helpers** — component context, document identity, LibrePy-safe text/path helpers, chat `DocumentService`, type guards, user-defined properties, dialog accessors, listener bases, visual property helpers, and UNO error wrappers. It is **not** a catalog of domain tools (`plugin/writer/`, `plugin/calc/`, `plugin/draw/` feature modules).

Related catalogs (do not duplicate here):

| Topic | Doc |
|-------|-----|
| XDL dialogs, wizards, control accessors | [uno-dialogs.md](uno-dialogs.md) |
| Main-thread / `guard_uno` | [uno-thread-safety.md](uno-thread-safety.md) |
| Disposed vs leaf catches | [exception-policy.md](exception-policy.md) |
| Streaming drain / toolkit pump | [streaming-and-threading.md](streaming-and-threading.md) |
| Entry-point map | [repo-map.md](../repo-map.md) |

## Invariants (do not undo)

- **No monolithic `uno_helpers.py`.** Helpers stay split: `uno_context`, `text_helpers` / `doc_type` / `udprops`, `document_helpers`, `dialogs`. That split is an AGENTS.md rule.
- **LibrePy must not import `document_helpers`.** Light helpers only (`text_helpers`, `doc_type`, `udprops`). Do not re-export the light helpers from `document_helpers`.
- **Use the extension’s `self.ctx`**, stored via `set_fallback_ctx` / `get_ctx()`. Do not prefer `uno.getComponentContext()` (wrong context in test runners; can segfault on Desktop).
- **HTTP / LLM URL policy stays in `url_utils`.** One stdlib `path_to_file_url` lives in a separate filesystem / document `file:` section; do not fold it into `normalize_endpoint_url`. PathSettings stay out of `url_utils`.

---

## 1. Inventory of shared helpers

Each row is a **shared utility** (location + one-line purpose). Private `_foo` names are included only when they are the actual implementation other modules copy or should call.

### 1.1 Component context, desktop, toolkit, package URL

Module: [`plugin/framework/uno_context.py`](../../plugin/framework/uno_context.py)

| Symbol | Purpose |
|--------|---------|
| `set_fallback_ctx` / `get_ctx` | Store / return the extension bootstrap component context (prefer this over `uno.getComponentContext()`). |
| `get_service_manager` | `ctx.ServiceManager` or `ctx.getServiceManager()`. |
| `get_desktop` | `com.sun.star.frame.Desktop` from the extension context. |
| `get_toolkit` | `com.sun.star.awt.Toolkit` (event pump / focus). |
| `process_events_to_idle` | Drain VCL via the approved toolkit pump (skips when a chat/MCP drain owner is active). |
| `get_package_info` | `PackageInformationProvider` singleton. |
| `set_package_extension_id` / `resolve_package_extension_id` | Pin / detect LibrePy vs WriterAgent vs LibreHarper OXT id. |
| `get_extension_url` | Package location URL, else `vnd.sun.star.extension://<id>`. |
| `get_extension_path` | Filesystem path of the OXT when the package URL is `file://`. |
| `menu_icon_asset_url` | `GraphicProvider` URL for an icon under OXT `assets/`. |
| `menu_icon_filesystem_paths` | Local PNG paths (bundle `assets/` then git `extension/assets/`). |
| `product_display_name` / `is_libreharper` | User-visible product name / LibreHarper probe. |
| `get_active_document` | `desktop.getCurrentComponent()` (Start Center possible when nothing is open). |
| `get_document_from_frame` | Model from a sidebar frame controller (preferred over Desktop for panels). |
| `focus_preserved` / stream-focus helpers | Sidebar query-field focus restore after RichTextControl; **UI-specific, not generic UNO**. |

`get_toolkit` / `get_ctx` are also the drain chokepoint for [`plugin/framework/async_stream.py`](../../plugin/framework/async_stream.py). Thread-affinity wrappers live in [`plugin/framework/thread_guard.py`](../../plugin/framework/thread_guard.py) (`guard_uno`, `main_thread_only`) — see [uno-thread-safety.md](uno-thread-safety.md).

### 1.2 Document resolve-by-URL-or-uid

Still [`uno_context.py`](../../plugin/framework/uno_context.py), plus the research listing layer.

| Symbol | Module | Purpose |
|--------|--------|---------|
| `normalize_doc_url` | `uno_context` | Strip + drop a trailing `/` so URL identity compares. |
| `get_runtime_uid` | `uno_context` | Per-session id (`getRuntimeUID` / attribute / property); works for untitled docs. |
| `resolve_document_by_url` | `uno_context` | Walk desktop components; match normalized URL **or** RuntimeUID; return `(model, doc_type)`. |
| `get_open_documents` | `document_research` | List open OfficeDocuments with name/url/uid/path/type/active/modified (untitled kept). |
| `_office_model_from_desktop_element` | `document_research` | Frame-or-model → `guard_uno(model)` for desktop walks. |
| `open_document_for_read` | `document_research` | Hidden+read-only `loadComponentFromURL`, or reuse an already-open component. |
| `close_document_research_document` | `document_research` | Close only if this call loaded a hidden sibling (not a user-visible doc). |
| `list_open_documents` (tool) | `document_research_tools` | Tool facade over `get_open_documents` (not a second resolver). |

`DocumentService.resolve_document_by_url` / `get_active_document` are thin wrappers (see §1.4). MCP mutation-gate keys import `normalize_doc_url` + `get_runtime_uid` from `uno_context`.

### 1.3 LibrePy-safe text / path / selection

Module: [`plugin/doc/text_helpers.py`](../../plugin/doc/text_helpers.py)

LibrePy Run Python Script, text analytics, Excel auto-open, and Writer selection offsets use this module **without** loading `document_helpers`.

| Symbol | Purpose |
|--------|---------|
| `normalize_linebreaks` | `\r\n` / `\r` → `\n` so offsets match (Windows UNO/clipboard). |
| `get_string_without_tracked_deletions` | Skip redline Delete portions when reading a text range. |
| `normalize_file_url` | Repair `file:/path` → `file:///path` (legacy `urljoin`). Shared with research. |
| `get_document_path` | `file:` URL → repair then `uno.fileUrlToSystemPath`; `None` if untitled / non-file. |
| `get_selection_range` | Writer `(start, end)` character offsets (cursor = equal ends). |
| `get_selection_text` | Selected string for Writer / Calc / Draw; `None` if empty. |
| `build_heading_tree` | Single-pass outline tree (`HeadingTreeNode`). |
| `collect_tracked_changes` | Bounded list of redlines in a range. |
| `get_full_writer_text` | Prefix of Writer body, hiding tracked deletions. |
| `get_document_length` / `get_document_end` | Character length / tail slice. |
| `get_text_cursor_at_range` | Cursor covering `[start, end)` (chunked `goRight`). |
| `_writer_char_count` / `_read_writer_text_slice` | O(1) `CharacterCount` when possible; slice reads for chat excerpts. |

Paragraph index helpers used by `DocumentService` live in [`plugin/doc/paragraph_search.py`](../../plugin/doc/paragraph_search.py) (`get_paragraph_ranges`, `find_paragraph_for_range`, `search_paragraph_texts`) — shared, but Writer-oriented.

### 1.4 Chat `DocumentService` and full-text dispatch

Module: [`plugin/doc/document_helpers.py`](../../plugin/doc/document_helpers.py) — WriterAgent only.

| Symbol | Purpose |
|--------|---------|
| `get_full_document_text` | Dispatch: Writer → `text_helpers`; Calc → lazy `plugin.calc.analyzer`; Draw/Impress → `plugin.draw.bridge`. |
| `get_document_context_for_chat` | `[DOCUMENT CONTENT]` assembler (Writer start/end + selection markers; Calc/Draw delegated). |
| `resolve_locator` | `paragraph:` / `heading:` / `bookmark:` → paragraph index. |
| `DocumentService` | Chat/MCP facade: active doc, resolve-by-url, type flags, full text, length, chat context, page helpers, paragraph ranges. |

`DocumentService` methods that are **wrappers**, not new logic:

| Method | Delegates to |
|--------|----------------|
| `get_active_document` | `uno_context.get_active_document` |
| `resolve_document_by_url` | `uno_context.resolve_document_by_url(get_ctx(), url)` |
| `is_writer` / `is_calc` / `is_draw` | `doc_type` |
| `detect_doc_type` | local map (Impress → `"draw"`, unknown → `"writer"`) |
| `get_full_text` | `get_full_document_text` |
| `get_document_length` | `text_helpers.get_document_length` |
| `get_document_context_for_chat` | module function + `get_ctx()` |
| `get_paragraph_ranges` / `find_paragraph_for_range` / `resolve_locator` | `paragraph_search` / module `resolve_locator` |

`get_page_for_paragraph` / `get_page_count` are Writer view-cursor walks (lockControllers + restore). `doc_key` is `id(doc)` — **not** RuntimeUID (see overlap).

### 1.5 Type guards

Module: [`plugin/doc/doc_type.py`](../../plugin/doc/doc_type.py)

| Symbol | Purpose |
|--------|---------|
| `DocumentType` | `UNKNOWN` / `WRITER` / `CALC` / `DRAW` / `IMPRESS`. |
| `get_document_type` | `supportsService` against the four canonical document services. |
| `is_writer` / `is_calc` / `is_draw` | Enum predicates (`is_draw` includes Impress). |
| `get_document_uno_services` | Live `supportsService` set for tool filtering. |
| `uno_services_for_doc_type_label` / `uno_services_for_document` | Label → services without (or with) a live model. |
| `doc_type_label_for_enum` | Lowercase label; `impress_as_draw=True` for research/visual family. |
| `doc_type_title_for_label` | Sidebar title (`impress` displays as Draw). |

Canonical service strings are `_DOCUMENT_SERVICE_MAP`. `visual_helpers` duplicates those strings plus `WebDocument` (must be checked first).

### 1.6 User-defined document properties

Module: [`plugin/doc/udprops.py`](../../plugin/doc/udprops.py)

| Symbol | Purpose |
|--------|---------|
| `get_document_property` | Read `UserDefinedProperties` by name (PropertyBag, not `XNameAccess`). |
| `set_document_property` | Add or set a removable string UD prop. |
| `_user_defined_property_exists` | `PropertySetInfo.hasPropertyByName` (and `hasByName` fallback). |

Used by grammar persistence, document-attached Python scripts, Calc session ids. Intentionally **not** `@main_thread_only` (notebook `XFilter.filter` runs on LO’s dispatch thread).

### 1.7 Dialog load and control accessors

Module: [`plugin/chatbot/dialogs.py`](../../plugin/chatbot/dialogs.py)

LibrePy Settings / Run Python Script / message boxes share this kit. Full how-to: [uno-dialogs.md](uno-dialogs.md). **Never** load XDL via `vnd.sun.star.script:…?location=application` (deadlocks with sidebar components).

| Symbol | Purpose |
|--------|---------|
| `load_writeragent_dialog` / `load_writeragent_dialog_detail` | Load `Dialogs/<name>.xdl` via DialogProvider (+ DP2), then `translate_dialog`. |
| `load_module_dialog` / `load_framework_dialog` / `_load_xdl` | Same provider path for `plugin/<module>/` and `plugin/framework/` XDL. |
| `translate_dialog` | gettext walk of Label/Text/Title/HelpText. |
| `get_optional` | Control by name or `None` (missing / disposed). |
| `get_control_text` / `set_control_text` | Text via method or model. |
| `is_checkbox_control` / `get_checkbox_state` / `set_checkbox_state` | Checkbox State quirks. |
| `set_control_enabled` / `set_control_visible` | Enable/visible via method or model. |
| `add_dialog_button` / `add_dialog_label` / `add_dialog_edit` / `add_dialog_hyperlink` | Programmatic control insert. |
| `msgbox` / `msgbox_with_copy` / `msgbox_with_report` | Message boxes. |
| `copy_to_clipboard` | `SystemClipboard`. |
| `format_exception_detail` | Printable Python + UNO exception (nested Target/Context). |
| `TabListener` | Multi-page `dlg.getModel().Step` (not `tabpagecontainer`). |

### 1.8 Listener bases

Module: [`plugin/framework/uno_listeners.py`](../../plugin/framework/uno_listeners.py)

Empty defaults + `_catch_and_log` so Python exceptions do not leak into the C++ bridge.

| Class | UNO interface |
|-------|----------------|
| `BaseListener` | `XEventListener` (`disposing`) |
| `BaseActionListener` | `XActionListener` |
| `BaseItemListener` | `XItemListener` |
| `BaseTextListener` | `XTextListener` |
| `BaseKeyListener` | `XKeyListener` |
| `BaseWindowListener` | `XWindowListener` |
| `BaseContainerListener` | `XContainerListener` |
| `BaseDocumentEventListener` | `XDocumentEventListener` |
| `BaseCloseListener` | `XCloseListener` |
| `BaseTerminateListener` | `XTerminateListener` |
| `BaseActivationEventListener` | Calc `XActivationEventListener` |

Dummy parents when PyUNO is absent (unit tests). New listeners should subclass these, not raw `unohelper.Base`.

### 1.9 Visual property helpers

Module: [`plugin/doc/visual_helpers.py`](../../plugin/doc/visual_helpers.py)

Shared across Writer / Calc / Draw / Impress image and shape tools. **Not** insertion/gallery (that is `plugin/writer/images/image_tools.py`).

| Symbol | Purpose |
|--------|---------|
| `get_visual_doc_type` | Label for image/shape helpers; WebDocument first, else `doc_type` (unknown → `"writer"`). |
| `has_uno_property` / `safe_set_property` / `safe_get_property` | PropertySetInfo probes (never `hasattr` on UNO attrs). |
| `safe_try_method` | Call a method if present; log and continue. |
| `parse_color_to_uno_int` | Hex / name / `rgb()` / int / tuple → 24-bit UNO RGB. |
| `apply_character_properties` | Batch Char* on a shape/cell/style. |
| `mm_to_units` / `px_to_units` / `units_to_px` / `mm_to_px` | 1/100 mm ↔ 96-DPI px. |
| `is_graphic_object` / `selected_graphic_object` / `graphic_objects_in_selection` | Graphic detection and selection. |
| `list_graphic_objects` / `get_graphic_object_by_name` / `graphic_from_object` | Name lookup across Writer text + Draw pages. |
| `get_active_draw_page` / `remove_graphic_from_draw_pages` | Draw/Impress page helpers. |
| `SHAPE_TOOL_UNO_SERVICES` | Union of Writer/Calc/Draw/Impress services for shared tool names. |

`image_tools.get_type_doc`, `_has_uno_property`, `_safe_set_property`, `_mm_to_units`, `_selection_graphic_object` are **already** one-line delegates into this module.

### 1.10 UNO error wrappers

Module: [`plugin/framework/errors.py`](../../plugin/framework/errors.py) — UNO-related subset only. Policy: [exception-policy.md](exception-policy.md).

| Symbol | Purpose |
|--------|---------|
| `UnoObjectError` | Stale docs / missing properties (`UNO_OBJECT_ERROR`). |
| `DocumentDisposedError` | Disposed object (`DISPOSED_OBJECT`). |
| `is_disposed_exception` | `DisposedException` / `RuntimeException` name heuristic + UNO types. |
| `suppress_disposed` (`ignore_disposed`) | UI lifecycle: swallow disposal (and optionally other) exceptions. |
| `check_not_none` (`check_disposed`) | Null guard only — does **not** probe live disposal. |
| `is_document_disposed` | Best-effort `getImplementationName` probe. |
| `safe_uno_call` | Decorator: probes return `default`; re-raise only real disposal. |
| `safe_call` | Call a UNO method; wrap failures in `UnoObjectError` / `DocumentDisposedError`. |
| `handle_errors` | Decorator: wrap unexpected exceptions for real operations. |
| `_resolve_exception_message` | Prefer UNO `.Message` over empty `str(exc)`. |

`dialogs.format_exception_detail` is the **printable nested** formatter; `errors.format_error_payload` is the **JSON tool-error** formatter. Different jobs.

---

## 2. File / path / URL handling

Four URL worlds in this repo. They must not share one helper.

| World | Typical scheme | Home | Callers should |
|-------|----------------|------|----------------|
| Document identity | `file:` / empty / RuntimeUID | `uno_context`, `text_helpers`, `document_research` | Share **document** helpers; not HTTP. |
| HTTP / LLM endpoints | `https:` | `url_utils` | Stay separate. |
| Extension / XDL / icons | `vnd.sun.star.extension:` or `file:` package location | `uno_context.get_extension_url` | Stay on package-info helpers. |
| Dispatch / factory | `org.extension.*:`, `private:factory/`, `private:resource/` | `url_utils` (LibrePy), `main.py` (WriterAgent), local loaders | Stay separate per scheme. |

### 2.1 Path ↔ `file:` URL conversion

There is **no** single shared converter. Live implementations:

| Helper | Module | Direction | Mechanism |
|--------|--------|-----------|-----------|
| `path_to_file_url` | `url_utils` (filesystem / document `file:` section) | path → URL | `Path(abspath).as_uri()` (forces `file:///` on POSIX). **Landed.** UNO-free; used by `document_research`, `embeddings_fs`, and `format`. |
| `_to_file_url` | `writer/specialized/mail_merge.py` | path or URL → URL | Prefer `uno.systemPathToFileUrl`; fallback `Path.as_uri()`. |
| `_file_url_for_path` | `writer/images/image_tools.py` | path → URL | `uno.systemPathToFileUrl(abspath)`. |
| `_file_url` | `writer/math/math_mml_convert.py` | path → URL | `uno.systemPathToFileUrl(abspath)`. |
| (inline) | `styles.py`, `get_image.py`, `duckdb_tools.py`, `calc/python/image_egress.py`, `librepy/sidebar_menus.py` | path → URL | Raw `uno.systemPathToFileUrl`. |
| `normalize_file_url` | `text_helpers` | URL repair | `file:/path` → `file:///path`. Shared by `get_document_path` and research. **Landed.** |
| `get_document_path` | `text_helpers` | URL → path | Repair then `file://` prefix; `uno.fileUrlToSystemPath`. |
| `_system_path_from_url` | `document_research` | URL → path | Accepts `file:`; uses shared `normalize_file_url`; then `fileUrlToSystemPath` + `abspath`. |
| `get_extension_path` | `uno_context` | URL → path | `file://` → `fileUrlToSystemPath`; else returns the URL string (`vnd.sun.star.extension://…`). |
| `_path_from_file_url` | `scripting/sandbox.py` | URL → path | **stdlib only** (`urlparse`/`unquote`); Windows drive + UNC. |
| `_system_dir_from_file_url` | `scripting/session_manager.py` | URL → parent dir | **stdlib, no UNO** (off-main `=PY()`); Windows drive letter. |

**Why two conversion styles exist**

- `urljoin("file:", path)` produced `file:/home/...` (two slashes). `loadComponentFromURL` needs `file:///home/...`. `Path.as_uri()` is the documented fix (`url_utils.path_to_file_url`, tests in `tests/framework/test_url_utils.py` and `tests/doc/test_document_research.py`).
- `uno.systemPathToFileUrl` / `fileUrlToSystemPath` are OS-correct **when UNO is available on the main thread**.
- Sandbox and Calc session-id parsing **must not** call UNO (off-main / stdlib-only). They reimplement a subset with explicit Windows branches.

**Should callers share one helper?**

| Cluster | Share? |
|---------|--------|
| `Path.as_uri()` sites (`document_research`, `embeddings_fs`, `format`) | **Landed** as `url_utils.path_to_file_url` (filesystem section). Same stdlib, no UNO, no `@deal`. |
| `uno.systemPathToFileUrl` at GraphicProvider / `loadComponentFromURL` image/math sites | Optional later; behavior matches `Path.as_uri()` on POSIX. Leave until a Windows mismatch is proven. |
| `get_document_path` vs `_system_path_from_url` | **Landed the `file:/` repair** on `get_document_path` via shared `text_helpers.normalize_file_url`. Research still adds `abspath`. |
| sandbox / session_manager stdlib parsers | **Stay separate** — no UNO, Windows UNC/drive, off-main. |
| `mail_merge._to_file_url` | Domain helper; can call a shared path→URL once one exists. |

### 2.2 Document-URL identity (trailing slash, `file:/` vs `file:///`)

| Helper | What it normalizes | What it does **not** |
|--------|--------------------|----------------------|
| `uno_context.normalize_doc_url` | strip; drop trailing `/` | Does not repair `file:/` vs `file:///`. |
| `document_scripts_identity` | imports `normalize_doc_url` | URL-only; empty if untitled. |
| `text_helpers.normalize_file_url` | `file:/path` → `file:///path` | Does not strip trailing slash. |
| sandbox / session_manager | same `file:/` → `file://` + rest | Then stdlib parse. Stay local (no UNO). |

`resolve_document_by_url` compares `normalize_doc_url(model.getURL())` to the request. A request of `file:/home/a.odt` will **not** match an open `file:///home/a.odt` unless something else repaired it first. `open_document_for_read` **does** run `normalize_file_url` before resolve. `get_document_path` now repairs before `fileUrlToSystemPath`.

Tests: `tests/doc/test_document.py` (`normalize_doc_url` trailing slash), `tests/doc/test_text_helpers.py` (`file:/` repair + `get_document_path`).

### 2.3 Untitled / empty URL / RuntimeUID

Untitled documents have `getURL() == ""`. Identity then **must** use RuntimeUID.

| Helper | Untitled behavior |
|--------|-------------------|
| `get_runtime_uid` | Returns uid string or `""`. Accepts only `str`/`int` (mocks cannot fake it). |
| `resolve_document_by_url` | `url` argument may be a RuntimeUID; matches unsaved docs. |
| `get_open_documents` | Keeps untitled rows (`url=""`, `path=""`, `name="Untitled"`, `uid=…`). Never drops them on type-lookup failure. |
| `get_document_path` | Returns `None` (not a `file:` URL after repair). |
| `document_scripts_identity` | Empty string for untitled (URL-only; **no** uid). |
| MCP `_resolve_mcp_doc_key` | Prefers `uid:<RuntimeUID>`; else `url:<normalized>`; else active-document sentinel. Survives Save As. |
| `DocumentService.doc_key` | `id(doc)` — proxy-unsafe; **not** uid. |
| `_is_same_document` | Compares RuntimeUID first (guard proxies break `==`). |

`plugin/framework/tool.py` documents `document_url` as “URL or RuntimeUID from `list_open_documents`”.

Other RuntimeUID users (domain, not shared utilities): notebook controls, review toolbar, grammar persistence, Calc workbook lifecycle, formula locator cache. New code should call `get_runtime_uid`, not `getattr(doc, "RuntimeUID", None)`.

### 2.4 PathSettings / work-directory vs config user-profile paths

Three **different** PathSettings properties; do not collapse the meanings.

| Call site | Lookup | Property | Meaning |
|-----------|--------|----------|---------|
| `document_research._path_settings_from_ctx` + `get_work_directory` | singleton `thePathSettings`, else create `PathSettings` | `Work` | LibreOffice “My Documents”; listing root when the active doc is unsaved. |
| `config._resolve_config_path_from_ctx` | create `PathSettings` | `UserConfig` | Directory for `writeragent.json`. |
| `image_tools.add_image_to_gallery` | singleton `thePathSettings` | `Storage_writable` | Gallery theme files. |

`document_research` also expands `$(home)` via `thePathSubstitution` (`_substitute_lo_path_variables`) and requires the result to be an existing directory (`_resolve_lo_directory_path`). Config and gallery do not substitute variables.

`resolve_listing_directory`: active doc parent (`get_document_directory` → `get_document_path`) else `get_work_directory`.

**Share lookup, not meaning.** A shared `_path_settings_from_ctx` would be reasonable; `Work` vs `UserConfig` vs `Storage_writable` stay separate.

### 2.5 HTTP / API URL helpers vs document URLs

[`plugin/framework/url_utils.py`](../../plugin/framework/url_utils.py) is primarily an HTTP / dispatch module. One stdlib filesystem helper lives in a **separate** section.

| Symbol | Purpose |
|--------|---------|
| `normalize_endpoint_url` / `get_api_version_suffix` | Strip/restore `/v1`, Open WebUI `/api`, Z.ai `/api/paas/v4`, Gemini `/v1beta/openai`. |
| `get_url_hostname` / `get_url_domain` / `get_url_path` / `get_url_query_dict` / `get_url_path_and_query` | Safe `urllib.parse` for HTTP endpoints. |
| `is_pdf_url` | HTTP path ends with `.pdf`. |
| `matches_librepy_dispatch_url` / `dispatch_command_from_url` | LibreOffice **dispatch** URLs (`org.extension.librepy:…`), not `file:`. |
| `path_to_file_url` | Filesystem path → document `file:` URL (`Path.as_uri()`). No `@deal`. |

Do not fold document identity or PathSettings into the HTTP helpers. Tests: `tests/framework/test_url_utils.py`.

### 2.6 Extension and dispatch URLs

**Extension / package**

- `get_extension_url` → `PackageInformationProvider.getPackageLocation(id)` or `vnd.sun.star.extension://<id>`.
- Dialogs: `base + "/Dialogs/" + name + ".xdl"` (file URL when the OXT is unpacked; vnd URL as fallback).
- Icons: `menu_icon_asset_url` then filesystem paths (`librepy/sidebar_menus.py` tries vnd first, then `systemPathToFileUrl` on disk — vnd `queryGraphic` throws in a fresh test-runner profile).

**Dispatch (menu / protocol handler)**

| Product | Protocol | Parser |
|---------|----------|--------|
| LibrePy | `org.extension.librepy:` | `url_utils.matches_librepy_dispatch_url` / `dispatch_command_from_url` (`main_core.py`) |
| WriterAgent | `org.extension.writeragent:` | `main.py` ProtocolHandler (`url.Protocol == _DISPATCH_PROTOCOL`) |
| Notebook cells | `org.extension.writeragent:notebook.run_cell.` | `notebook_runner.py` |
| Review context menu | `org.extension.writeragent:writer.accept_change` etc. | `change_context_menu.py` |

LibrePy’s extra `dispatch_command_from_url` exists because some LO paths populate `Complete` but not `Path`. WriterAgent’s handler compares `Protocol` only. **Stay separate per product** unless a third protocol appears.

**Forbidden:** `vnd.sun.star.script:…?location=application` for sidebar/XDL (deadlock). Documented in `dialogs.py` and [uno-dialogs.md](uno-dialogs.md).

### 2.7 Non-file schemes actually used

| Scheme / URL | Where | Share a helper? |
|--------------|-------|-----------------|
| `file:` / `file:///` | Documents, XDL, images, config | Document/path cluster only. |
| `vnd.sun.star.extension://<id>` | Package URL fallback | `get_extension_url` already. |
| `org.extension.librepy:` / `org.extension.writeragent:` | Menu dispatch | Product protocol handlers. |
| `private:factory/swriter` | Hidden Writer for HTML import/export, rich-text paste, Calc rich HTML, testing_runner keeper | **Local.** Each site sets Hidden/ReadOnly/`_blank` vs `_default` differently. |
| `private:factory/scalc` | Excel auto-open comment (OnNew vs OnLoadFinished) | Domain. |
| `private:factory/simpress` | PPT-Master import | Domain. |
| `private:factory/smath` | Math embed (`math_mml_export._MATH_FACTORY_URL`) | Domain. |
| `private:resource/toolbar/addon_org.extension.writeragent.toolbar` | Review toolbar | Domain. |
| `https:` / `http:` | LLM endpoints; notebook remote images | `url_utils` vs importer fetch — not document URLs. |

No other UNO URL schemes showed up in `plugin/` production code in this inventory (`smb:`, `ftp:`, `vnd.sun.star.expand:` are unused).

### 2.8 Windows vs POSIX (only where the code diverges)

Most converters rely on UNO or `Path.as_uri()` and have **no** extra Windows branch.

Explicit POSIX/Windows splits that exist today:

- `sandbox._path_from_file_url`: `os.name == "nt"` drive-letter (`/C:` → `C:\`) and UNC (`file://server/share`).
- `session_manager._system_dir_from_file_url`: same drive-letter strip; no UNC branch.
- `text_helpers.normalize_linebreaks`: comments Windows `\r\n` from UNO/clipboard.
- Tests name the POSIX `file:///` requirement (`test_path_to_file_url_uses_three_slashes_on_unix`).

Do not invent a Windows file-URL helper until a live `systemPathToFileUrl` vs `Path.as_uri()` mismatch is measured on Windows.

---

## 3. Duplication, overlap, and split-boundary issues

Classification: **intentional split** (keep) / **accidental copy** (unify later) / **thin wrapper** (fine).

### 3.1 Accidental copies (same algorithm, two homes)

| Pair | What’s duplicated | Notes |
|------|-------------------|-------|
| `uno_context.normalize_doc_url` vs `document_scripts._normalize_doc_url` | Trailing-slash strip | **Landed.** Script identity imports `normalize_doc_url`; the document_scripts copy is gone. |
| `document_research._path_to_file_url` vs `embeddings_fs.path_to_file_url` vs `format._file_url` | `Path(abspath).as_uri()` | **Landed.** Shared `url_utils.path_to_file_url` (filesystem section). Old copies deleted; no aliases. |
| `text_helpers.normalize_file_url` vs sandbox vs session_manager `file:/` repair | `file:/` → `file://` + rest | **Landed for UNO callers** (`get_document_path` + research). Sandbox / session_manager stay stdlib. |
| Desktop component walks | `resolve_document_by_url`, `get_open_documents`, `_collect_open_file_urls` | All enumerate `desktop.getComponents()`. Research already has `_office_model_from_desktop_element`; resolve has a slightly different frame-vs-model walk. |

### 3.2 Intentional splits (do not collapse)

| Split | Why it is intentional |
|-------|------------------------|
| `text_helpers` / `doc_type` / `udprops` vs `document_helpers` | LibrePy import graph. Chat context / `SheetAnalyzer` must not load in LibrePy. |
| `url_utils` HTTP helpers vs document `file:` identity | Different schemes and `@deal` contracts. The one exception is stdlib `path_to_file_url` in the filesystem section. |
| `visual_helpers` vs `doc_type` | Visual adds `WebDocument` (checked first) and unknown→`"writer"`; research uses `impress_as_draw`. |
| `visual_helpers` vs `image_tools` | Lookup/properties vs insert/gallery/GraphicProvider. Wrappers already delegate. |
| `get_document_from_frame` vs `get_active_document` | Sidebar must bind the **frame’s** document, not Desktop current (wrong-doc bug). |
| sandbox / session_manager URL→path vs UNO converters | Off-main, no PyUNO. |
| `safe_uno_call` vs `safe_call` / `handle_errors` | Probes return default; real ops treat `RuntimeException` as disposal. |
| LibrePy vs WriterAgent dispatch parsers | Two OXT protocol ids. |
| PathSettings `Work` vs `UserConfig` vs `Storage_writable` | Different directories. |

### 3.3 Thin wrappers (leave; don’t add more layers)

| Wrapper | Target |
|---------|--------|
| `DocumentService.get_active_document` / `resolve_document_by_url` / `is_*` | `uno_context` / `doc_type` |
| `image_tools.get_type_doc` / `_has_uno_property` / `_safe_set_property` / `_mm_to_units` | `visual_helpers` |
| `list_open_documents` tool | `get_open_documents` |

### 3.4 Overlapping but **not** identical (document before unifying)

**Doc-type labels (three contracts)**

| Function | Impress | Unknown |
|----------|---------|---------|
| `doc_type_label_for_enum` default | `"impress"` | `"unknown"` |
| `doc_type_label_for_enum(impress_as_draw=True)` / `resolve_document_by_url` / `get_open_documents` | `"draw"` | `"unknown"` (open-docs untitled fallback `"writer"` on success path) |
| `DocumentService.detect_doc_type` | `"draw"` | `"writer"` |
| `get_visual_doc_type` | `"impress"` | `"writer"` (+ `"web"` for WebDocument) |

Unifying `detect_doc_type` onto `doc_type_label_for_enum` would change unknown → `"unknown"` unless the `"writer"` default is preserved on purpose.

**URL → path**

`get_document_path` and `_system_path_from_url` both repair `file:/` via `text_helpers.normalize_file_url`. Research still applies `abspath` after conversion. Untitled / non-file URLs still return `None`.

**Active document**

| Helper | Filter |
|--------|--------|
| `get_active_document` | Whatever Desktop current is (Start Center possible). |
| `get_active_document_for_scripts` | Desktop current **and** `is_writer`/`is_calc`/`is_draw`. |
| MCP `_real_active_document` | OfficeDocument filter (Start Center skipped). |
| `get_document_from_frame` | Frame controller model. |

**Document identity keys**

`get_runtime_uid` (session-stable) vs `DocumentService.doc_key` (`id(doc)`, breaks across guard proxies) vs `document_scripts_identity` (URL only, empty if untitled). `_is_same_document` already documents why `==` fails on guard builds.

**Property existence**

`visual_helpers.has_uno_property` vs `udprops._user_defined_property_exists`: both use `PropertySetInfo.hasPropertyByName`; udprops adds `hasByName` because `UserDefinedProperties` is a PropertyBag. Do not merge without keeping the Bag fallback.

**Desktop / GraphicProvider duplication (lower value)**

- `uno_context._current_document_controller` creates Desktop via ServiceManager instead of `get_desktop`.
- `main.py._load_icon_graphic` vs `librepy/sidebar_menus.py` GraphicProvider-from-URL (LibrePy also tries filesystem).
- `create_property_value` (`writer/format.py`) vs inline `PropertyValue()` / `createUnoStruct` at load sites. `open_document_for_read` already imports `create_property_value`.

**Notebook markdown images**

`writer_importer._resolve_markdown_image_path`: `uno.fileUrlToSystemPath` then naive `src.replace("file://", "", 1)` on failure. That fallback is not equivalent to `_system_path_from_url` (no `file:/` repair, no unquote).

### 3.5 `uno_context.py` scope creep

The module mixes (1) true UNO globals (ctx, desktop, toolkit, package URL, resolve-by-url) with (2) sidebar stream-focus tracking (`install_stream_focus_tracker`, `note_user_left_query`, …). Focus helpers are real bugfixes; they are not generic utilities. Future work should **not** add more UI here. An optional later split is listed below; it is not required to fix overlap.

---

## 4. Ranked future-work recommendations

Do **not** re-merge a monolithic `uno_helpers.py`. Prefer the smallest existing home, or a tiny UNO-free module if embeddings must import it.

### Leave as-is / invariant

1. LibrePy vs `document_helpers` import split.
2. HTTP `url_utils` policy vs document identity (except the one stdlib `path_to_file_url` in the filesystem section).
3. `get_document_from_frame` for sidebars vs `get_active_document` for menus/MCP.
4. `visual_helpers` vs `image_tools` (wrappers already delegate).
5. sandbox / session_manager stdlib file-URL parsers (no UNO, Windows).
6. `private:factory/*` loaders (different factory + Hidden/`_blank` contracts).
7. LibrePy vs WriterAgent dispatch protocol parsers.
8. PathSettings property meanings (`Work` / `UserConfig` / `Storage_writable`).
9. `safe_uno_call` vs `safe_call` (probe vs real operation).

### P1 — candidate unify (small, real duplication)

1. **Delete `document_scripts._normalize_doc_url`.** **Landed.** Callers import public `uno_context.normalize_doc_url`. Tests: `tests/doc/test_document.py`, `tests/scripting/test_document_scripts.py`.
2. **UNO-free `path_to_file_url`.** **Landed** in `url_utils` filesystem / document `file:` section (not HTTP helpers, not `text_helpers`, not a new `file_urls.py`). `document_research`, `embeddings_fs`, and `format` import it; old copies deleted; no aliases. Tests: `tests/framework/test_url_utils.py`, `tests/doc/test_document_research.py`, `tests/embeddings/test_embeddings_fs.py`.
3. **Document URL→path: teach `get_document_path` the `file:/` repair.** **Landed.** Shared `text_helpers.normalize_file_url`; `get_document_path` and `_system_path_from_url` both use it. Sandbox / session_manager stdlib copies stay. Tests: `tests/doc/test_text_helpers.py`, `tests/doc/test_document_research.py`.

### P2 — candidate unify after documenting behavior

4. **Desktop enumeration.** Have `resolve_document_by_url` and `get_open_documents` share `_office_model_from_desktop_element` (or move that helper next to `get_desktop`). Keep match policy (URL+uid vs listing metadata) at the callers.
5. **Shared PathSettings locator** (`_path_settings_from_ctx`) used by research, config, gallery. Keep property names at call sites.
6. **`DocumentService.detect_doc_type`** → `doc_type_label_for_enum(..., impress_as_draw=True)` **only if** tests accept unknown → `"unknown"` or an explicit `default="writer"` is added.
7. **`get_active_document_for_scripts`** → `get_active_document` + type filter (drop the second Desktop create).
8. **`_current_document_controller`** → `get_desktop` / `get_active_document` instead of a third Desktop create.

### P3 — optional / low value

9. Stop adding UI to `uno_context`; optional extract of stream-focus helpers.
10. GraphicProvider icon load: `main.py` vs `librepy/sidebar_menus.py` (LibrePy’s filesystem fallback is the extra behavior).
11. Point remaining `uno.systemPathToFileUrl` image/math sites at the P1 helper **after** a Windows smoke check.
12. Notebook `file://` strip fallback → same URL→path helper.
13. `DocumentService.doc_key` → `get_runtime_uid` (or uid-or-url like MCP). This is a behavior change for any cache keyed on `id(doc)`.
14. Promote `uno_context._normalize_doc_url` to public name when P1 lands. **Landed** as `normalize_doc_url`.

### Explicit non-goals for later refactors

- Do not catalog or “helper-ize” Writer format, Calc charts, Draw shapes, PPT-Master, or Math OLE.
- Do not rewrite [uno-dialogs.md](uno-dialogs.md) / [uno-thread-safety.md](uno-thread-safety.md).
- Do not change AGENTS.md invariants or the LibrePy import split in order to make the catalog prettier.

---

## 5. Existing tests (behavior evidence, not work to extend)

| Area | Tests |
|------|-------|
| `normalize_doc_url` | `tests/doc/test_document.py` |
| resolve by URL or RuntimeUID | `tests/doc/test_resolve_document_by_url.py` |
| `file:///` + `file:/` repair, work directory | `tests/doc/test_document_research.py` |
| `get_open_documents` untitled / Start Center | `tests/mcp/test_list_open_documents.py` |
| HTTP `url_utils` + `path_to_file_url` | `tests/framework/test_url_utils.py` |
| `uno_context` getters | `tests/framework/test_uno_context.py` |
| listeners | `tests/framework/test_uno_listeners.py` |
| type guards | `tests/doc/test_doc_type.py` |
| text helpers | `tests/doc/test_text_helpers.py`, `tests/doc/test_linebreak.py` |
| udprops | `tests/doc/test_udprops.py` |
| visual helpers | `tests/doc/test_visual_helpers.py` |
| UNO errors | `tests/framework/test_errors.py`, `tests/framework/test_uno_exception_formatting.py` |
| `guard_uno` boundaries | `tests/framework/test_guard_uno_boundaries.py` |

Any P1 unify should extend the **matching** `test_*.py` for the module that becomes the new home, not add a fourth URL test file.
