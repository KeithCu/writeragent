# UNO exception policy (disposed vs expected failures)

Do **not** catch `com.sun.star.uno.Exception` in order to “avoid” `DisposedException`. In UNO, `DisposedException` subclasses `RuntimeException`, which subclasses `uno.Exception`, so that catch still swallows disposal.

Use the helpers in `plugin/framework/errors.py`: `is_disposed_exception`, `suppress_disposed`, `DocumentDisposedError`.

| Layer | On disposal / bridge teardown | On expected UNO/Python errors | Silent `except Exception: pass` |
|--------|-------------------------------|-------------------------------|----------------------------------|
| UI lifecycle (sidebar, rich text, panel) | `with suppress_disposed(...)` | Keep fallbacks; unexpected errors are logged (`suppress_all=True`) | Replace with `suppress_disposed` |
| Document tools (`visual_helpers`, edit review, charts, shapes, notebook) | Re-raise or wrap `DocumentDisposedError` | Leaf types only: `UnknownPropertyException`, `NoSuchElementException`, `IndexOutOfBoundsException`, `IllegalArgumentException`, `AttributeError`, `ValueError` | `log.exception` / `log.debug(..., exc_info=True)` or drop the catch |
| Draw/Impress slide tools (`notes`, `transitions`, `placeholders`, `masters`) | Re-raise via `DrawBridge.get_slide_for_tool` (`is_disposed_exception`) | `IndexError` → `ToolExecutionError` (page out of range); other errors wrapped | n/a |

Do **not** wrap UNO dispose as `ToolExecutionError(str(e))`. That strips dispose identity, so `execute_safe` returns `TOOL_EXECUTION_ERROR` instead of `DOCUMENT_DISPOSED`, and the native test runner treats a dead URP as a normal tool failure instead of aborting the remaining suite.

`execute_safe` maps dispose via `is_tool_document_disposed`, not the raw `is_disposed_exception` heuristic. A bare `RuntimeException` from a still-live document (`is_document_disposed` is false) is a real UNO error — e.g. `createTextCursorByRange(ViewCursor)` on a Writer body — and must surface as `TOOL_EXECUTION_ERROR`, not the lying “Document was closed or disposed by LibreOffice” chat string. `DisposedException` / `DocumentDisposedError` still map to `DOCUMENT_DISPOSED`. Do **not** narrow `is_disposed_exception` itself; UI lifecycle (`suppress_disposed`) still needs the RuntimeException name match.

Best-effort probes (missing properties, optional controllers, “is this a graphic?”) may still catch `Exception` and return empty. That is not the hang class of bug. Re-raise disposal where a UI callback or tool loop would otherwise keep running on a dead object; do not sprinkle re-raises through every helper.

Related: [chat sidebar lifecycle](../chat/sidebar-implementation.md#ui-lifecycle-exception-handling-suppress_disposed), [UNO thread safety](uno-thread-safety.md).
