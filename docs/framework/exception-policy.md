# UNO exception policy (disposed vs expected failures)

Do **not** catch `com.sun.star.uno.Exception` in order to “avoid” `DisposedException`. In UNO, `DisposedException` subclasses `RuntimeException`, which subclasses `uno.Exception`, so that catch still swallows disposal.

Use the helpers in `plugin/framework/errors.py`: `is_disposal`, `suppress_disposed`, `DocumentDisposedError`, `uno_read`, `uno_call`, `best_effort`, `reraise_if_disposed`.

| Layer | On disposal / bridge teardown | On expected UNO/Python errors | Silent `except Exception: pass` |
|--------|-------------------------------|-------------------------------|----------------------------------|
| UI lifecycle (sidebar, rich text, panel) | `with suppress_disposed(...)` | Keep fallbacks; unexpected `Exception`s are logged (`suppress_all=True`). `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` propagate | Replace with `suppress_disposed` |
| Document tools (`visual_helpers`, edit review, charts, shapes, notebook) | Re-raise or wrap `DocumentDisposedError` | Leaf types only: `UnknownPropertyException`, `NoSuchElementException`, `IndexOutOfBoundsException`, `IllegalArgumentException`, `AttributeError`, `ValueError` | `log.exception` / `log.debug(..., exc_info=True)` or drop the catch |
| Draw/Impress slide tools (`notes`, `transitions`, `placeholders`, `masters`, headers) | Re-raise disposal from in-range `getByIndex` (`is_disposal`) | `DrawBridge.resolve_page` raises `ToolExecutionError` (`Page index N out of range.`); other in-range `getByIndex` errors propagate to `execute_safe` (`TOOL_EXECUTION_ERROR`) | n/a |

Do **not** wrap UNO dispose as `ToolExecutionError(str(e))`. That strips dispose identity, so `execute_safe` returns `TOOL_EXECUTION_ERROR` instead of `DOCUMENT_DISPOSED`, and the native test runner treats a dead URP as a normal tool failure instead of aborting the remaining suite.

`_document_is_disposed` in `plugin/framework/tool.py` probes `getImplementationName()` and returns true only for `DisposedException` / `DocumentDisposedError`. Any other exception from that probe means the document is still live.

`WriterAgentException` translates short catalog msgids through `i18n._`. A runtime message longer than the gettext msgid bound (`DEAL_MAX_MSGID`) is stored as-is. Passing that string through `_()` raised `deal.PreContractError` in deal-enabled builds, so `NetworkError` and `make_tool_error` never constructed. The `code` argument on `WriterAgentException` is keyword-only (`code=None`) to avoid swapping error message and code.

`resolve_exception_message` and `format_error_message` are total (safely handling failing `.Message` or `str()` calls). `format_error_message` accepts `BaseException` and maps common exceptions to user-friendly, localized advice: it treats `ConnectionError` and `URLError` as network failures; fallback text comes from `resolve_exception_message` (UNO `.Message`, then `str`, then the type name), and a `URLError` whose `reason` is `ssl.SSLError` uses the TLS sentence. `FileNotFoundError`, `PermissionError`, `IsADirectoryError`, `NotADirectoryError`, `FileExistsError`, and `BlockingIOError`, plus errnos `ENOSPC`, `ENOTEMPTY`, `EROFS`, `EDQUOT`, `EFBIG`, `ENAMETOOLONG`, and `EBUSY`, stay filesystem errors. A timeout is a request-timeout hint only when it matches standard socket/HTTP timeout phrases (`timed out` or `the read operation timed out`) on `socket.timeout` / `TimeoutError` or an `OSError` / `URLError` with a non-formula timeout reason. Host timeouts (e.g. Harper LSP or marshal timeouts) preserve their original message. Messages containing `python timed out`, `python execution timed out`, or `worker failed: timed out` still use the Python-timeout sentence (the substring path). Only `urllib.error.HTTPError` is formatted from an HTTP status. Other `http.client.HTTPException` values (for example `RemoteDisconnected`) keep their text on the connection path or via `str(e)`.

`uno_call` re-raises `DocumentDisposedError` only for `DisposedException` (and an already-wrapped `DocumentDisposedError`). A bare `RuntimeException` becomes `UnoObjectError`. `reraise_if_disposed` uses the same `is_disposal` predicate and re-raises an existing `DocumentDisposedError` unchanged. A bare `RuntimeException` is left for the caller.

`execute_safe` maps dispose via `is_disposal`. A bare `RuntimeException` from a still-live document is a real UNO error — e.g. `createTextCursorByRange(ViewCursor)` on a Writer body — and surfaces as `TOOL_EXECUTION_ERROR`, not the lying “Document was closed or disposed by LibreOffice” chat string. User-facing text and `original_error` use `resolve_exception_message`, which still falls back to the type name when both `.Message` and `str` are empty. `DisposedException` / `DocumentDisposedError` map to `DOCUMENT_DISPOSED`. `suppress_disposed` uses `_looks_like_teardown` which includes the `RuntimeException` name match for UI lifecycle teardown.

`if is_disposal(exc): raise` before a generic wrap ensures real disposal propagates.

### Disposal predicate truth table

| Exception kind | `is_disposal(exc)` | `_looks_like_teardown(exc)` |
|---|---|---|
| `DocumentDisposedError` | True | True |
| `DisposedException` | True | True |
| `RuntimeException` | False | True |
| `IllegalArgumentException` | False | False |
| `ValueError` | False | False |

Chart listing and notebook Run All use `is_disposal`: real disposal stops them, a live-document `RuntimeException` propagates.

Best-effort probes (missing properties, optional controllers, “is this a graphic?”) may still catch `Exception` and return empty. That is not the hang class of bug. Re-raise disposal where a UI callback or tool loop would otherwise keep running on a dead object; do not sprinkle re-raises through every helper.

UNO listener callbacks (`_catch_and_log` in `plugin/framework/uno_listeners.py`) are the one place that classifies a callback failure. `ListenerBoundary` subclasses `BaseException`, not `Exception`, so a generic `except Exception` cannot turn it into an empty document or a swallowed main-thread check. Kinds:

| Kind | What it is | What leaves the callback |
|------|------------|--------------------------|
| `thread` | `assert_main_thread` `RuntimeError` (`UNO thread violation`) | `ListenerBoundary` |
| `disposed` | `DisposedException` / `DocumentDisposedError` only | `ListenerBoundary`, except `disposing` |
| `veto` | `CloseVetoException` / `TerminationVetoException` | the original UNO exception, so the bridge can still veto |

A bare `RuntimeException` is not disposal: the callback logs it and returns `None` or `False`. A subclass override of `BaseMouseClickHandler.mousePressed` or `mouseReleased` returns `False` on a soft failure even when the override has no `-> bool`. The value comes from the base annotation. `disposing` logs real disposal and returns, because a throw there stops the broadcaster from notifying the remaining listeners. Any other callback is a query: a dead desktop must not look like a successful empty result. `get_open_documents` calls `reraise_listener_boundary`, so a disposed enumeration is not `[]` and an empty desktop is not disposal.

`TypeError` and `ValueError` go through that same boundary before the typed log. A disposal or veto that subclasses either one is not a soft callback failure. An ordinary `TypeError` or `ValueError` is still logged under its type name and does not enter the bridge. A veto is re-raised as the original UNO exception.

Related: [chat sidebar lifecycle](../chat/sidebar-implementation.md#ui-lifecycle-exception-handling-suppress_disposed), [UNO thread safety](uno-thread-safety.md).
