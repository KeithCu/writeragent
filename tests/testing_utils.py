# testing_utils.py
# Native UNO test harness. Pytest document stubs live in doc_stubs.py
# and are re-exported here so suites keep importing plugin.tests.testing_utils.

# Contents:
#   doc stubs        — re-exported from doc_stubs.py
#   keeper / pool    — _STATE, _NATIVE_DOC_POOL
#   Windows factory  — leftover names, skip_*
#   Draw/Impress     — close_draw_family_doc
#   reset            — _reset_writer_doc / _reset_calc_doc
#   TestingFactory   — create_native_doc, native_doc, execute_tool
#   suite helpers    — execute_calc_tool, native_writer_tool_context,
#                      writer_services_tool_context
#   HTTP mocks       — create_mock_http_response

import contextlib
import sys
from typing import Any
from unittest.mock import MagicMock

# testing_runner imports tests.testing_utils while suites import
# plugin.tests.testing_utils; alias both so keeper/pool state is shared.
def _register_testing_utils_aliases() -> None:
    this = sys.modules[__name__]
    # Parent packages must exist before the dotted names are injected
    # Import order: parent packages must exist before dotted names land in sys.modules.
    import plugin.tests  # noqa: F401
    import tests  # noqa: F401

    for name in ("plugin.tests.testing_utils", "tests.testing_utils"):
        sys.modules[name] = this


_register_testing_utils_aliases()

from . import doc_stubs as _doc_stubs  # noqa: E402

# Explicit re-exports: ruff F401 drops unused `from .doc_stubs import X`,
# and ty/basedpyright need NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS assigned here.
NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS = _doc_stubs.NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS
setup_uno_mocks = _doc_stubs.setup_uno_mocks
ElementStub = _doc_stubs.ElementStub
WriterDocStub = _doc_stubs.WriterDocStub
MockDocument = _doc_stubs.MockDocument
MockTextCursor = _doc_stubs.MockTextCursor
CalcCellStub = _doc_stubs.CalcCellStub
CalcRangeStub = _doc_stubs.CalcRangeStub
CalcSheetStub = _doc_stubs.CalcSheetStub
CalcSheetsStub = _doc_stubs.CalcSheetsStub
CalcControllerStub = _doc_stubs.CalcControllerStub
CalcDocStub = _doc_stubs.CalcDocStub
MockContext = _doc_stubs.MockContext


class _NativeDocPool(dict):
    """Pool for native LibreOffice documents, tracking clean state across tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clean_doc_ids: set = set()

    def clear(self):
        super().clear()
        self.clean_doc_ids.clear()

    def mark_clean(self, doc, clean: bool = True) -> None:
        if clean:
            self.clean_doc_ids.add(id(doc))
        else:
            self.clean_doc_ids.discard(id(doc))

    def is_clean(self, doc) -> bool:
        return id(doc) in self.clean_doc_ids


_NATIVE_DOC_POOL: _NativeDocPool = _NativeDocPool()
_BUILTIN_PARA_STYLES: set = set()
_BUILTIN_CHAR_STYLES: set = set()

# When True, stderr breadcrumbs trace reset_native_doc / _reset_calc_doc steps.
_LOG_NATIVE_DOC_TEARDOWN = False


def _native_teardown_progress(msg: str) -> None:
    if not _LOG_NATIVE_DOC_TEARDOWN:
        return
    from plugin.testing_runner import _progress_verbose as _progress

    _progress(msg)


class _HarnessState:
    """Process-wide leftover / keeper flags shared across both import aliases.

    Leftover paste Writers are never closed because ``close(True)`` hangs 30s on Windows.
    """

    __slots__ = (
        "keeper_uid",
        "keeper_doc",
        "leftover_open",
        "hidden_open_bitmap",
        "notebook_host",
        "factory_seq",
        "math_ole_uids",
    )

    def __init__(self) -> None:
        self.keeper_uid = ""
        self.keeper_doc = None
        self.leftover_open = 0
        self.hidden_open_bitmap = False
        self.notebook_host = False
        self.factory_seq = 0
        self.math_ole_uids: set[str] = set()


_STATE = _HarnessState()


def set_harness_keeper_uid(uid: str, doc=None) -> None:
    """Record the hidden keeper Writer (uid + doc) for later setActiveFrame."""
    uid_s = str(uid or "")
    if uid_s == "-":
        uid_s = ""
    _STATE.keeper_uid = uid_s
    _STATE.keeper_doc = doc if uid_s else None


def _writer_doc_uid(doc) -> str:
    try:
        return str(getattr(doc, "RuntimeUID", None) or "")
    except Exception:
        return ""


def _writer_frame_name(doc) -> str:
    try:
        frame = doc.getCurrentController().getFrame()
        name = frame.getName()
        return str(name or "")
    except Exception:
        return ""


def _native_doc_svc(doc) -> str:
    """Return writer / calc / impress / draw; ``is True`` avoids truthy MagicMock."""
    try:
        if doc.supportsService("com.sun.star.text.TextDocument") is True:
            return "writer"
        if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument") is True:
            return "calc"
        if doc.supportsService("com.sun.star.presentation.PresentationDocument") is True:
            return "impress"
        if doc.supportsService("com.sun.star.drawing.DrawingDocument") is True:
            return "draw"
    except Exception:
        return ""
    return ""


def _iter_open_writer_docs(desktop):
    """Yield (uid, doc) for open TextDocuments. Read-only enum; no close."""
    try:
        enum = desktop.getComponents().createEnumeration()
    except Exception:
        return
    n = 0
    while n < 64:
        try:
            if enum.hasMoreElements() is not True:
                break
            component = enum.nextElement()
        except Exception:
            break
        n += 1
        try:
            if component.supportsService("com.sun.star.text.TextDocument"):
                yield _writer_doc_uid(component), component
        except Exception:
            continue


def reactivate_harness_keeper(desktop=None) -> bool:
    """Reactivate the hidden keeper frame so factory loads do not target a leftover doc."""
    from plugin.testing_runner import _progress_verbose as _progress

    doc = _STATE.keeper_doc
    if doc is None:
        return False
    try:
        frame = doc.getCurrentController().getFrame()
        if desktop is None:
            desktop = frame.getCreator()
        desktop.setActiveFrame(frame)
        _progress("html_paste_writer: keeper reactivated uid=%s" % _STATE.keeper_uid)
        return True
    except Exception as exc:
        _progress(
            "html_paste_writer: keeper reactivate failed uid=%s err=%s"
            % (_STATE.keeper_uid or "-", type(exc).__name__)
        )
        return False


def prepare_windows_writer_factory(ctx) -> int:
    """Harness-only: log leftover paste Writers and reactivate the keeper.

    After HTML paste on Windows, leftover Writers (uids 26/27) stay open when
    ``close`` is skipped; closing them wedges later factory loads. Enum leftovers
    (read-only; safe before load) and ``setActiveFrame`` the keeper—do not close
    leftovers. Returns how many non-keeper Writers are still open. Windows-only
    caller. Call before every Windows ``private:factory/`` load (Writer or Calc).
    """
    if ctx is None:
        return 0
    from plugin.framework.uno_context import get_desktop
    from plugin.testing_runner import _progress_verbose as _progress

    desktop = get_desktop(ctx)
    keeper = _STATE.keeper_uid
    leftover_uids = []
    leftover_frames = []
    for uid, doc in _iter_open_writer_docs(desktop):
        if not uid or uid == keeper:
            continue
        leftover_uids.append(uid)
        leftover_frames.append(_writer_frame_name(doc) or "-")
    leftover_open = len(leftover_uids)
    _set_windows_leftover_open(leftover_open)
    _progress(
        "html_paste_writer: leftovers open=%s uids=%s frames=%s keeper=%s"
        % (leftover_open, leftover_uids, leftover_frames, keeper or "-")
    )
    reactivate_harness_keeper(desktop)
    return leftover_open


# Last leftover count from prepare lives on _STATE.leftover_open;
# close_doc and native_doc reuse read it because desktop.getComponents() can hang after paste close.


def _set_windows_leftover_open(n: int) -> None:
    """Cache leftover Writer count (prepare writes; close_doc / native_doc read)."""
    _STATE.leftover_open = int(n or 0)


def _windows_leftover_open() -> int:
    return int(_STATE.leftover_open or 0)


def _windows_should_reuse_calc(ctx) -> bool:
    """True when leftover Writers are open on Windows, because a new scalc factory hangs."""
    if ctx is None or sys.platform != "win32":
        return False
    return _windows_leftover_open() > 0


# CREATE|GLOBAL flags (8|55=63) and stable target names so Windows leftover
# factory loads reuse existing named frames instead of stacking or hanging on _blank.
_WINDOWS_FACTORY_SEARCH_FLAGS = 8 | 55
_WINDOWS_FACTORY_TARGET = "_wa_factory"
_WINDOWS_CALC_FACTORY_TARGET = "_wa_scalc"
_WINDOWS_DRAW_FACTORY_TARGET = "_wa_sdraw"
_WINDOWS_IMPRESS_FACTORY_TARGET = "_wa_simpress"
_WINDOWS_FACTORY_TARGETS = {
    "private:factory/swriter": _WINDOWS_FACTORY_TARGET,
    "private:factory/scalc": _WINDOWS_CALC_FACTORY_TARGET,
    "private:factory/sdraw": _WINDOWS_DRAW_FACTORY_TARGET,
    "private:factory/simpress": _WINDOWS_IMPRESS_FACTORY_TARGET,
}


def _windows_factory_load_args(factory_url: str, leftover_open: int) -> tuple[str, int]:
    """Return (target, FrameSearchFlag) for a Windows factory load; reuse stable names when leftovers exist."""
    if leftover_open <= 0 or not factory_url.startswith("private:factory/"):
        return "_blank", 0
    if factory_url == "private:factory/swriter" and _windows_notebook_host():
        return _WINDOWS_NOTEBOOK_HOST_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS
    target = _WINDOWS_FACTORY_TARGETS.get(factory_url)
    if target is not None:
        return target, _WINDOWS_FACTORY_SEARCH_FLAGS
    _STATE.factory_seq += 1
    return "_wa_factory_%s" % _STATE.factory_seq, _WINDOWS_FACTORY_SEARCH_FLAGS


# Stable CREATE|GLOBAL name for Hidden .ipynb loads on Windows.
_WINDOWS_NOTEBOOK_TARGET = "_wa_notebook"


def note_windows_html_paste_leftover() -> None:
    """Mark leftover_open after ``insert_cell_html_rich`` skips close on Windows."""
    if sys.platform != "win32":
        return
    if _windows_leftover_open() <= 0:
        _set_windows_leftover_open(1)
    # Do not import testing_runner here: unit tests mock sys.platform to
    # win32, and a first import of shutil then looks for _winapi.
    print(
        "html_paste_writer: noted leftover_open=%s" % _windows_leftover_open(),
        file=sys.stderr,
        flush=True,
    )


# >2 leftover Writers: a cross-app Draw/Impress factory load hangs 30s on Windows (3 hung; 4 was too loose).
_WINDOWS_CROSS_APP_LEFTOVER_MAX = 2


def _raise_windows_skip(log_line: str, skip_msg: str) -> None:
    """Print a leftover skip breadcrumb and raise unittest.SkipTest."""
    import unittest

    print(log_line, file=sys.stderr, flush=True)
    raise unittest.SkipTest(skip_msg)


def windows_cross_app_factory_unsafe(leftover_open: int | None = None) -> bool:
    """True when leftover Draw/Impress factory would hang on Windows."""
    if leftover_open is None:
        leftover_open = _windows_leftover_open()
    return sys.platform == "win32" and int(leftover_open or 0) > _WINDOWS_CROSS_APP_LEFTOVER_MAX


def skip_windows_cross_app_factory(factory_url: str, leftover_open: int) -> None:
    """Skip leftover Draw/Impress factory when leftover Writer count exceeds the Windows limit."""
    if factory_url not in ("private:factory/sdraw", "private:factory/simpress"):
        return
    if not windows_cross_app_factory_unsafe(leftover_open):
        return
    app = factory_url.rsplit("/", 1)[-1]
    _raise_windows_skip(
        "windows leftover skip: leftover %s leftovers=%s" % (app, leftover_open),
        "Windows leftover Draw/Impress skip (%s, leftovers=%s)" % (app, leftover_open),
    )


def _set_windows_hidden_open_bitmap(on: bool) -> None:
    _STATE.hidden_open_bitmap = bool(on)


def _windows_hidden_open_bitmap() -> bool:
    return bool(_STATE.hidden_open_bitmap)


def windows_hidden_open_bitmap_err(err: str | None) -> bool:
    """True when a Windows Hidden sibling open failed with system bitmap."""
    if sys.platform != "win32" or not err:
        return False
    return "system bitmap" in str(err).lower()


def skip_windows_hidden_open_after_bitmap(reason: str) -> None:
    """Skip a later Hidden sibling open or Hidden ``_blank`` load, because the next Hidden open hangs 30s after a Windows system-bitmap failure."""
    if not _windows_hidden_open_bitmap():
        return
    _raise_windows_skip(
        "windows hidden skip: %s after system bitmap" % reason,
        "Windows Hidden skip after system bitmap (%s)" % reason,
    )


def windows_awt_top_dialog_unsafe() -> bool:
    """True when mapping a TOP AWT dialog would hang Windows headless VCL."""
    return sys.platform == "win32"


def skip_windows_awt_top_dialog(reason: str) -> None:
    """Skip TOP dialog ``createPeer`` / ``setVisible`` on Windows headless to avoid VCL hang."""
    if not windows_awt_top_dialog_unsafe():
        return
    _raise_windows_skip(
        "windows awt skip: %s" % reason,
        "Windows headless AWT TOP dialog skip (%s)" % reason,
    )


def note_windows_hidden_open_bitmap(err: str | None) -> None:
    """Record a Windows Hidden bitmap so later sibling opens skip."""
    if not windows_hidden_open_bitmap_err(err):
        return
    _set_windows_hidden_open_bitmap(True)
    print(
        "windows hidden bitmap: later Hidden opens skip",
        file=sys.stderr,
        flush=True,
    )


# Separate win32 factory target for the notebook Writer pool.
_WINDOWS_NOTEBOOK_HOST_TARGET = "_wa_notebook_host"


def set_windows_notebook_host(on: bool) -> None:
    """Use a separate pooled Writer for notebook UNO suites so form-run state does not leak."""
    _STATE.notebook_host = bool(on)


def _windows_notebook_host() -> bool:
    return bool(_STATE.notebook_host)


def windows_notebook_load_args() -> tuple[str, int]:
    """Return (target, FrameSearchFlag) for Hidden Jupyter loads; reuse ``_wa_notebook`` on Windows because consecutive Hidden ``_blank`` hangs."""
    if sys.platform != "win32":
        return "_blank", 0
    return _WINDOWS_NOTEBOOK_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS


def _reraise_native_open_failure(
    exc: BaseException, factory_url: str, pre_open: str = "no_probe"
) -> None:
    """Re-raise factory-open failures with the previous-test breadcrumb attached.

    URP ``DisposedException`` on ``loadComponentFromURL`` usually means the
    *previous* ``@with_native_doc`` close killed soffice/the bridge. Keep
    ``Binary URP bridge`` in the message so ``_is_uno_bridge_disposed`` still
    matches and the runner aborts remaining suites. ``pre_open`` is the cheap
    getServiceManager probe taken *before* load (alive vs already disposed).
    """
    from plugin.testing_runner import (
        _is_uno_bridge_disposed,
        _progress,
        format_lifecycle_breadcrumb,
    )

    crumb = format_lifecycle_breadcrumb()
    if _is_uno_bridge_disposed(exc):
        msg = (
            "create_native_doc loadComponentFromURL(%s) DisposedException / URP dead "
            "pre_open=%s (%s: %s) %s"
            % (factory_url, pre_open, type(exc).__name__, exc, crumb)
        )
        _progress("LIFECYCLE native_doc open FAIL %s" % msg)
        raise RuntimeError("Binary URP bridge disposed during call; %s" % msg) from exc
    raise


def _log_close_doc_failure(exc: BaseException) -> None:
    """Log (do not re-raise) a dispose during ``close_doc`` so the trail is named."""
    from plugin.testing_runner import (
        _is_uno_bridge_disposed,
        _progress,
        _soffice_pids,
        format_lifecycle_breadcrumb,
    )

    if not (_is_uno_bridge_disposed(exc) or type(exc).__name__ == "DisposedException"):
        return
    _progress(
        "LIFECYCLE close_doc dispose pids=%s %s"
        % (_soffice_pids(), format_lifecycle_breadcrumb())
    )


def _log_office_health_after_close(ctx, doc_type: str) -> None:
    """Harness-only: probe the desktop after close; log if the office is already dead.

    Draw/Impress never reuse a pooled doc, so each test closes. If that close
    (or a delayed crash) kills URP, the next factory open is the named victim.
    This probe prints at close time. It does not restart office or skip tests.
    """
    from plugin.testing_runner import (
        _is_uno_bridge_disposed,
        _progress,
        _soffice_pids,
        format_lifecycle_breadcrumb,
    )

    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        desktop.getComponents()
    except Exception as exc:
        if _is_uno_bridge_disposed(exc) or type(exc).__name__ == "DisposedException":
            _progress(
                "LIFECYCLE office dead after close doc_type=%s pids=%s %s"
                % (doc_type, _soffice_pids(), format_lifecycle_breadcrumb())
            )


def _default_native_doc_reuse(doc_type: str) -> bool:
    return doc_type in ("calc", "writer")


# Pre-close URP settle after gc.collect() (see close_doc). Draw soak amplifier
# was duplicate_slide + held SvxShape proxies; Writer forms/charts/shapes share
# the SfxItemPool path. Measured 0/80 on the killer; post-close wait did not help.
_CLOSE_DOC_URP_SETTLE_S = 0.05

# Post-close and POSIX pre-close URP settle durations for Draw/Impress teardown.
# Without this settle, the next factory load hangs; keep it out of close_doc so
# Writer/Calc closes are not taxed, and keep the longer budget on Windows.
_DRAW_FAMILY_POST_CLOSE_SETTLE_S = 0.75 if sys.platform == "win32" else 0.15
_DRAW_FAMILY_PRE_CLOSE_SETTLE_S = _DRAW_FAMILY_POST_CLOSE_SETTLE_S


def _draw_family_raw_close() -> bool:
    """True on Windows where Impress/Draw must close via bare ``close(True)`` because pre-close GC/sleep hangs."""
    return sys.platform == "win32"


def _draw_family_doc_label(doc) -> str:
    """Harness-only: impress / draw / unknown for close breadcrumbs."""
    try:
        # ``is True``: MagicMock.supportsService() is truthy and must not
        # look like Draw-family during unit tests (Windows pytest).
        if doc.supportsService("com.sun.star.presentation.PresentationDocument") is True:
            return "impress"
    except Exception:
        pass
    try:
        if doc.supportsService("com.sun.star.drawing.DrawingDocument") is True:
            return "draw"
    except Exception:
        pass
    return "unknown"


# plugin/draw/math_insert.py MATH_CLSID; closing a Draw doc holding this OLE can crash soffice on Windows.
_MATH_OLE_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"


def mark_windows_math_ole_doc(doc) -> None:
    """Record a Draw/Impress doc holding a Math OLE so Windows teardown skips closing it."""
    if sys.platform != "win32" or not doc:
        return
    try:
        uid = str(getattr(doc, "RuntimeUID", None) or "")
    except Exception:
        uid = ""
    if not uid:
        return
    _STATE.math_ole_uids.add(uid)


def _windows_math_ole_uids() -> set[str]:
    return set(_STATE.math_ole_uids)


def _clear_windows_math_ole_uids() -> None:
    """Unit-test reset."""
    _STATE.math_ole_uids.clear()


def _draw_doc_has_math_ole(doc) -> bool:
    """True when a Draw/Impress page still has a Math OLE2Shape."""
    if not doc:
        return False
    try:
        pages = doc.getDrawPages()
        n_pages = pages.getCount()
    except Exception:
        return False
    try:
        page_count = int(n_pages)
    except (TypeError, ValueError):
        return False
    for i in range(page_count):
        try:
            page = pages.getByIndex(i)
            n_shapes = int(page.getCount())
        except Exception:
            continue
        for j in range(n_shapes):
            try:
                shape = page.getByIndex(j)
                clsid = str(getattr(shape, "CLSID", "") or "")
            except Exception:
                continue
            if clsid == _MATH_OLE_CLSID:
                return True
    return False


def _windows_should_skip_math_ole_close(uid: str = "") -> bool:
    """True when Windows must not ``close_doc`` this marked Math OLE uid.

    Extra UNO on Math-OLE Draw docs during teardown can exit soffice early on
    Windows. Skip close only when ``mark_windows_math_ole_doc`` recorded the
    uid; do not walk the document. Unmarked Draw close stays GC + 50 ms +
    ``close(True)``. Harness-only; not a product fix.
    """
    if sys.platform != "win32" or not uid:
        return False
    return uid in _windows_math_ole_uids()


def _windows_should_skip_draw_family_close() -> bool:
    """True when native_doc must not close Impress/Draw on Windows.

    With a leftover paste Writer open, ``close_draw_family_doc`` on win32 can
    kill soffice during raw ``close(True)`` and abort the runner. Skip
    ``close_draw_family_doc`` on win32 only when ``_windows_leftover_open() > 0``.
    Drop the proxy; named ``_wa_simpress`` / ``_wa_sdraw`` targets reuse instead
    of stacking. Peer skip-close uses the same rule; suite-end recycle clears the
    leftover Draw-family doc. Math OLE Draw still uses ``close_doc`` with its
    own skip. Harness-only; not a product fix.
    """
    if sys.platform != "win32":
        return False
    return _windows_leftover_open() > 0


def close_draw_family_doc(doc):
    """Harness-only Impress/Draw close. Does **not** go through ``close_doc``.

    On Windows, pre-close GC/sleep in ``close_doc`` blocks the UI thread;
    Draw-family teardown uses a bare ``close(True)`` with no setModified /
    GC / sleep in front of it, then callers drop the proxy and
    ``settle_after_draw_family_close``. Leftover Impress with Writer still
    open poisons later Writer factory loads if closed through the heavy path.
    POSIX still marks unmodified, GC, pre-close settle, then ``close(True)``.
    Peer tests keep Writer open across the Windows raw close and re-activate
    it before closing Writer. Logs svc/uid and each step. Harness-only; not a
    product fix.
    """
    if not doc:
        return
    from plugin.testing_runner import _progress, _progress_verbose

    svc = _draw_family_doc_label(doc)
    uid = "?"
    try:
        uid = str(getattr(doc, "RuntimeUID", None) or "?")
    except Exception:
        uid = "?"
    _progress_verbose("close_draw_family: start svc=%s uid=%s" % (svc, uid))
    if _draw_family_raw_close():
        # Do not GC or sleep here — that is the hung close/dispose path.
        _progress_verbose("close_draw_family: raw close(True) start svc=%s uid=%s" % (svc, uid))
        try:
            if hasattr(doc, "close"):
                doc.close(True)
            elif hasattr(doc, "dispose"):
                doc.dispose()
        except Exception as exc:
            _log_close_doc_failure(exc)
            _progress(
                "close_draw_family: raw close failed svc=%s uid=%s err=%s"
                % (svc, uid, type(exc).__name__)
            )
        else:
            _progress_verbose("close_draw_family: raw close(True) done svc=%s uid=%s" % (svc, uid))
        return
    try:
        if hasattr(doc, "setModified"):
            doc.setModified(False)
            _progress_verbose("close_draw_family: setModified(False) ok svc=%s uid=%s" % (svc, uid))
    except Exception as exc:
        _progress_verbose(
            "close_draw_family: setModified skipped svc=%s uid=%s err=%s"
            % (svc, uid, type(exc).__name__)
        )
    import gc
    import time

    gc.collect()
    _progress_verbose(
        "close_draw_family: gc done; sleep %.2fs svc=%s uid=%s"
        % (_DRAW_FAMILY_PRE_CLOSE_SETTLE_S, svc, uid)
    )
    time.sleep(_DRAW_FAMILY_PRE_CLOSE_SETTLE_S)
    _progress_verbose("close_draw_family: close(True) start svc=%s uid=%s" % (svc, uid))
    try:
        if hasattr(doc, "close"):
            doc.close(True)
        elif hasattr(doc, "dispose"):
            doc.dispose()
    except Exception as exc:
        _log_close_doc_failure(exc)
        _progress(
            "close_draw_family: close failed svc=%s uid=%s err=%s"
            % (svc, uid, type(exc).__name__)
        )
    else:
        _progress_verbose("close_draw_family: close(True) done svc=%s uid=%s" % (svc, uid))


def settle_after_draw_family_close() -> None:
    """GC and sleep after closing Impress/Draw so URP teardown finishes before the next factory load.

    Call after ``close_draw_family_doc`` and after dropping the local reference,
    because the next factory load hangs if the proxy is still held or skipped.
    """
    import gc
    import time

    gc.collect()
    time.sleep(_DRAW_FAMILY_POST_CLOSE_SETTLE_S)


# offapi/com/sun/star/sheet/CellFlags.idl — VALUE|DATETIME|STRING|ANNOTATION|FORMULA|HARDATTR|STYLES|OBJECTS|EDITATTR|FORMATTED
_CALC_CLEAR_ALL = 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512


def _native_doc_alive(doc) -> bool:
    try:
        doc.getCurrentController()
        return True
    except Exception:
        return False


def _clear_named_container(container) -> None:
    if container is None or not hasattr(container, "getElementNames"):
        return
    for name in list(container.getElementNames()):
        try:
            container.removeByName(name)
        except Exception:
            pass


def _clear_undo(doc) -> None:
    try:
        mgr = doc.getUndoManager()
        if mgr is not None:
            mgr.clear()
    except Exception:
        pass


def _remove_all_calc_charts(doc) -> None:
    sheets = doc.getSheets()
    for i in range(sheets.getCount()):
        try:
            charts = sheets.getByIndex(i).getCharts()
            for name in list(charts.getElementNames()):
                try:
                    charts.removeByName(name)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        objs = doc.getEmbeddedObjects()
        for name in list(objs.getElementNames()):
            try:
                objs.removeByName(name)
            except Exception:
                pass
    except Exception:
        pass


def _probe_soffice_before_udprops(doc, _ctx=None) -> None:
    """Cheap RuntimeUID read before clearing udprops; avoids desktop.getComponents() after paste close."""
    _native_teardown_progress("udprops probe: RuntimeUID start")
    try:
        uid = getattr(doc, "RuntimeUID", None)
        _native_teardown_progress("udprops probe: RuntimeUID done uid=%s" % uid)
    except Exception as exc:
        _native_teardown_progress("udprops probe: RuntimeUID failed %r" % (exc,))


def _clear_writeragent_udprops(doc, ctx=None) -> None:
    # Write the scripts UDProp directly because doc.isReadonly() hung on Windows;
    # do not skip this write, because skipping relocated the hang to getDocumentProperties().
    from plugin.doc import udprops as udprops_mod

    # Keep suite noise low: the desktop/clipboard probe runs only when
    # with_native_doc armed teardown logs (test_insert_cell_html).
    if _LOG_NATIVE_DOC_TEARDOWN and ctx is not None:
        _probe_soffice_before_udprops(doc, ctx)
    try:
        from plugin.scripting.document_scripts import DOCUMENT_SCRIPTS_UDPROP
        from plugin.scripting.session_manager import PYTHON_WORKBOOK_SESSION_PROP

        _native_teardown_progress("udprops clear: DOCUMENT_SCRIPTS start")
        udprops_mod.set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, "")
        _native_teardown_progress("udprops clear: DOCUMENT_SCRIPTS done")
        udprops_mod.set_document_property(doc, PYTHON_WORKBOOK_SESSION_PROP, "")
        udprops_mod.set_document_property(doc, "WriterAgentSessionID", "")
        _native_teardown_progress("udprops clear: all done")
    except Exception:
        pass


def _reset_calc_doc(doc, ctx) -> None:  # ctx unused; same signature as writer reset
    _native_teardown_progress("native_doc: _reset_calc_doc start")
    _native_teardown_progress("native_doc: _reset_calc_doc remove_charts start")
    _remove_all_calc_charts(doc)
    _native_teardown_progress("native_doc: _reset_calc_doc remove_charts done")
    sheets = doc.getSheets()
    while sheets.getCount() > 1:
        name = sheets.getByIndex(sheets.getCount() - 1).Name
        sheets.removeByName(name)
    sheet = sheets.getByIndex(0)
    try:
        if sheet.Name != "Sheet1":
            sheet.setName("Sheet1")
    except Exception:
        pass
    try:
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents start")
        cursor = sheet.createCursor()
        cursor.gotoStartOfUsedArea(False)
        cursor.gotoEndOfUsedArea(True)
        try:
            cursor.merge(False)
        except Exception:
            pass
        cursor.clearContents(_CALC_CLEAR_ALL)
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents done")
    except Exception:
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents fallback start")
        sheet.getCellRangeByName("A1:AMJ1048576").clearContents(_CALC_CLEAR_ALL)
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents fallback done")
    _clear_named_container(getattr(doc, "NamedRanges", None))
    try:
        _clear_named_container(sheet.NamedRanges)
    except Exception:
        pass
    _clear_named_container(getattr(doc, "DatabaseRanges", None))
    try:
        import uno

        settings = doc.getNumberFormatSettings()
        nd = uno.createUnoStruct("com.sun.star.util.Date")
        nd.Year, nd.Month, nd.Day = 1899, 12, 30
        settings.setPropertyValue("NullDate", nd)
    except Exception:
        pass
    try:
        controller = doc.getCurrentController()
        controller.setActiveSheet(sheet)
        controller.select(sheet.getCellByPosition(0, 0))
        _native_teardown_progress("native_doc: _reset_calc_doc select done")
    except Exception:
        pass
    _clear_writeragent_udprops(doc, ctx)
    _clear_undo(doc)
    _native_teardown_progress("native_doc: _reset_calc_doc done")


def _reset_writer_style_families(doc) -> None:
    """Drop user HTML styles and restore built-in CharWeight (Standard can pick up bold)."""
    try:
        families = doc.getStyleFamilies()
    except Exception:
        return
    # Restore "Standard" paragraph style properties (the primary built-in style that picks up bold/char formatting)
    try:
        para_styles = families.getByName("ParagraphStyles")
        if para_styles.hasByName("Standard"):
            std = para_styles.getByName("Standard")
            for prop in ("CharWeight", "CharHeight", "CharPosture", "CharUnderline", "CharColor"):
                try:
                    std.setPropertyToDefault(prop)
                except Exception:
                    pass
    except Exception:
        pass

    global _BUILTIN_PARA_STYLES, _BUILTIN_CHAR_STYLES
    for family_name, builtin_set in (
        ("ParagraphStyles", _BUILTIN_PARA_STYLES),
        ("CharacterStyles", _BUILTIN_CHAR_STYLES),
    ):
        try:
            styles = families.getByName(family_name)
        except Exception:
            continue
        try:
            names = list(styles.getElementNames())
        except Exception:
            continue
        if not builtin_set:
            for name in names:
                try:
                    st = styles.getByName(name)
                    if hasattr(st, "isUserDefined") and not bool(st.isUserDefined()):
                        builtin_set.add(name)
                except Exception:
                    builtin_set.add(name)
        for name in names:
            if name not in builtin_set:
                try:
                    styles.removeByName(name)
                except Exception:
                    pass


def _reset_writer_page_regions(doc) -> None:
    """Clear leftover header/footer XText, restore FirstIsShared, and reset PageDescName to Standard."""
    try:
        styles = doc.getStyleFamilies().getByName("PageStyles")
    except Exception:
        styles = None
    if styles is not None:
        try:
            names = list(styles.getElementNames())
        except Exception:
            names = []
        for name in names:
            try:
                style = styles.getByName(name)
            except Exception:
                continue
            # Fast-path: default page styles have FirstIsShared=True and no headers/footers enabled.
            try:
                first_shared = style.getPropertyValue("FirstIsShared")
                header_on = style.getPropertyValue("HeaderIsOn")
                footer_on = style.getPropertyValue("FooterIsOn")
                if first_shared is True and not header_on and not footer_on:
                    continue
            except Exception:
                pass
            for flag in ("FirstIsShared", "HeaderIsShared", "FooterIsShared"):
                try:
                    style.setPropertyValue(flag, True)
                except Exception:
                    pass
            for text_prop in (
                "HeaderText",
                "HeaderTextFirst",
                "HeaderTextLeft",
                "FooterText",
                "FooterTextFirst",
                "FooterTextLeft",
            ):
                try:
                    text_obj = style.getPropertyValue(text_prop)
                except Exception:
                    continue
                if text_obj is None:
                    continue
                try:
                    text_obj.setString("")
                except Exception:
                    pass
                try:
                    enum = text_obj.createEnumeration()
                except Exception:
                    continue
                while True:
                    try:
                        if enum.hasMoreElements() is not True:
                            break
                        el = enum.nextElement()
                    except Exception:
                        break
                    try:
                        if el.supportsService("com.sun.star.text.TextTable") is True:
                            text_obj.removeTextContent(el)
                    except Exception:
                        continue
    try:
        body = doc.getText().createTextCursor()
        body.gotoStart(False)
        body.gotoEnd(True)
        body.setPropertyValue("PageDescName", "Standard")
    except Exception:
        pass


def _writer_pool_is_clean(doc) -> bool:
    """False if wipe left text, bold, or graphics — caller should factory-load."""
    try:
        if (doc.getText().getString() or "").strip():
            return False
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        if float(cursor.getPropertyValue("CharWeight") or 100) >= 135.0:
            return False
        if hasattr(doc, "getGraphicObjects") and doc.getGraphicObjects().getCount() > 0:
            return False
    except Exception:
        return False
    return True


def _reset_writer_doc(doc, ctx) -> None:
    try:
        doc.setPropertyValue("RecordChanges", False)
    except Exception:
        pass
    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
    except Exception:
        pass
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.setString("")
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        # Empty para keeps last run CharWeight/Heading; HTML insert at "end" with
        # apply_styles=False then paints body text with leftover bold (150).
        try:
            cursor.setPropertyValue("ParaStyleName", "Standard")
        except Exception:
            pass
        try:
            cursor.setPropertyValue("NumberingRules", None)
        except Exception:
            pass
        try:
            cursor.setPropertyValue("CharWeight", 100.0)
        except Exception:
            pass
        for prop in (
            "CharStyleName",
            "CharWeight",
            "CharHeight",
            "CharPosture",
            "CharUnderline",
            "CharColor",
            "CharBackColor",
            "CharEscapement",
            "CharFontName",
            "ParaAdjust",
        ):
            try:
                cursor.setPropertyToDefault(prop)
            except Exception:
                pass
        cursor.gotoStart(False)
        try:
            doc.getCurrentController().select(cursor)
        except Exception:
            pass
    except Exception:
        pass
    try:
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            para = enum.nextElement()
            try:
                para.setPropertyValue("ParaStyleName", "Standard")
            except Exception:
                pass
            try:
                para.setPropertyValue("CharWeight", 100.0)
            except Exception:
                pass
            for prop in ("CharStyleName", "CharWeight", "CharHeight", "CharPosture"):
                try:
                    para.setPropertyToDefault(prop)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:SelectAll", "", 0, ())
        helper.executeDispatch(frame, ".uno:ResetAttributes", "", 0, ())
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        doc.getCurrentController().select(cursor)
    except Exception:
        pass
    for getter in ("getTextTables", "getTextFrames", "getGraphicObjects", "getEmbeddedObjects", "getTextSections"):
        if not hasattr(doc, getter):
            continue
        try:
            container = getattr(doc, getter)()
            if hasattr(container, "hasElements") and not container.hasElements():
                continue
            for name in list(container.getElementNames()):
                try:
                    content = container.getByName(name)
                    doc.getText().removeTextContent(content)
                except Exception:
                    try:
                        container.getByName(name).dispose()
                    except Exception:
                        pass
        except Exception:
            pass
    _reset_writer_page_regions(doc)
    _reset_writer_style_families(doc)
    _clear_undo(doc)


def reset_native_doc(doc, doc_type: str, ctx) -> None:
    """Wipe a Writer or Calc document so the next native test can reuse it."""
    if doc_type == "calc":
        _reset_calc_doc(doc, ctx)
    elif doc_type == "writer":
        _reset_writer_doc(doc, ctx)
    else:
        raise ValueError("reset_native_doc only supports writer and calc")

def _exception_leaving_native_doc(exc: BaseException) -> BaseException:
    """Return the object native_doc's generator is allowed to raise.

    CPython contextlib._GeneratorContextManager.__exit__ assigns
    exc.__traceback__ when the generator re-raises the same object throw()
    injected (the ``exc is value`` branch, Lib/contextlib.py; CI 3.12
    lines 158 and 191). PyUNO exceptions implement __setattr__ as
    uno._uno_struct__setattr__, which cannot store a traceback and raises
    "Couldn't convert <traceback object> to a UNO type", hiding the real
    message (for example "Binary URP bridge disposed during call").
    A different object takes the ``exc is not value`` branch, which does
    not assign __traceback__ on the UNO exception. __cause__ is set on the
    new RuntimeError, not on the UNO object.
    """
    setter = getattr(getattr(type(exc), "__setattr__", None), "__name__", "")
    if setter != "_uno_struct__setattr__":
        return exc
    text = str(exc).strip() or str(getattr(exc, "Message", "") or "") or type(exc).__name__
    wrapped = RuntimeError(text)
    wrapped.__cause__ = exc
    wrapped.__suppress_context__ = True
    return wrapped


class TestingFactory:
    """Unified factory for creating test documents and contexts."""

    @staticmethod
    def create_doc(env="mock", doc_type="writer", content=None, **kwargs):
        """Create a mock document stub (or raise for native — use create_native_doc).

        - ``calc`` → :class:`CalcDocStub` (prefer ``data=`` 2D grid)
        - otherwise → :class:`WriterDocStub` (``content=`` paragraph list, ``items=`` style families)
        """
        if env == "native":
            raise NotImplementedError("Native doc creation requires a ctx. Use create_native_doc(ctx, ...)")

        if doc_type == "calc":
            calc_kwargs = dict(kwargs)
            if "data" not in calc_kwargs and content is not None and not isinstance(content, list):
                calc_kwargs["data"] = content
            return CalcDocStub(**calc_kwargs)

        elements = content if isinstance(content, list) else []
        return WriterDocStub(elements, doc_type=doc_type, **kwargs)

    @staticmethod
    def create_native_doc(ctx, doc_type="writer", hidden=True):
        """Creates a real hidden document in LibreOffice.

        On URP ``DisposedException``, logs the previous native test (and PIDs)
        then re-raises a ``RuntimeError`` that still matches
        ``_is_uno_bridge_disposed`` so the runner names the *previous* test,
        not only this factory open. See ``docs/framework/uno-test-lifecycle.md``.
        """
        from plugin.framework.uno_context import get_desktop
        import uno

        desktop = get_desktop(ctx)
        props = []
        if hidden:
            props.append(uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True))
        
        if doc_type.startswith("private:") or doc_type.startswith("file://"):
            factory_url = doc_type
        else:
            factory_url = {
                "writer": "private:factory/swriter",
                "calc": "private:factory/scalc",
                "draw": "private:factory/sdraw",
                "impress": "private:factory/simpress"
            }.get(doc_type, "private:factory/swriter")

        from plugin.testing_runner import probe_uno_bridge

        leftover_open = 0
        target, flags = "_blank", 0
        # On Windows, prepare the keeper frame and reuse stable CREATE|GLOBAL
        # target names when leftover Writers exist; skip unsafe cross-app or
        # post-bitmap Hidden _blank loads.
        if sys.platform == "win32" and factory_url.startswith("private:factory/"):
            leftover_open = prepare_windows_writer_factory(ctx)
            target, flags = _windows_factory_load_args(factory_url, leftover_open)
            from plugin.testing_runner import _progress_verbose as _progress

            _progress(
                "create_native_doc: windows factory leftover_open=%s url=%s target=%s flags=%s"
                % (leftover_open, factory_url, target, flags)
            )
            skip_windows_cross_app_factory(factory_url, leftover_open)
            if target == "_blank":
                skip_windows_hidden_open_after_bitmap(
                    "create_native_doc Hidden _blank"
                )

        # Distinguish "bridge already dead" (previous test) from "died during load".
        pre_open = probe_uno_bridge(ctx)
        if pre_open == "disposed":
            _reraise_native_open_failure(
                RuntimeError("Binary URP bridge already disposed before loadComponentFromURL"),
                factory_url,
                pre_open=pre_open,
            )
            raise
        try:
            if sys.platform == "win32" and leftover_open:
                from plugin.testing_runner import _progress_verbose as _progress

                _progress(
                    "create_native_doc: load start url=%s target=%s flags=%s"
                    % (factory_url, target, flags)
                )
            doc = desktop.loadComponentFromURL(factory_url, target, flags, tuple(props))
            if sys.platform == "win32" and leftover_open:
                from plugin.testing_runner import _progress_verbose as _progress

                uid = ""
                try:
                    uid = str(getattr(doc, "RuntimeUID", None) or "")
                except Exception:
                    uid = ""
                _progress(
                    "create_native_doc: load done url=%s target=%s uid=%s"
                    % (factory_url, target, uid or "-")
                )
        except Exception as exc:
            _reraise_native_open_failure(exc, factory_url, pre_open=pre_open)
            raise
        return doc

    @staticmethod
    def close_doc(doc):
        """Safely closes a document instance if available."""
        if not doc:
            return
        # Skip leftover-window Writer close before dropping the pool entry so reuse still finds it.
        uid = ""
        is_writer = False
        leftover_open = 0
        if sys.platform == "win32":
            try:
                uid = str(getattr(doc, "RuntimeUID", None) or "")
            except Exception:
                uid = ""
            # Check Math OLE mark from RuntimeUID only because supportsService/getDrawPages killed soffice.
            if _windows_should_skip_math_ole_close(uid):
                from plugin.testing_runner import _progress_verbose as _progress, _soffice_pids

                leftover_open = _windows_leftover_open()
                reactivate_harness_keeper()
                _progress(
                    "close_doc: skip math ole close (windows) uid=%s "
                    "leftovers=%s keeper=%s pids=%s"
                    % (
                        uid or "-",
                        leftover_open,
                        _STATE.keeper_uid or "-",
                        _soffice_pids(),
                    )
                )
                return
            try:
                is_writer = bool(
                    doc.supportsService("com.sun.star.text.TextDocument") is True
                )
            except Exception:
                is_writer = False
            leftover_open = _windows_leftover_open()
            if is_writer:
                from plugin.testing_runner import _progress_verbose as _progress

                _progress(
                    "close_doc: start uid=%s leftovers=%s" % (uid or "-", leftover_open)
                )
                if leftover_open > 0:
                    # Do not close Writers on Windows while leftover paste/notebook Writers remain open,
                    # because closing them hangs the next factory load.
                    reactivate_harness_keeper()
                    _progress(
                        "close_doc: skip writer close leftovers open=%s uid=%s"
                        % (leftover_open, uid or "-")
                    )
                    return
        for key, pooled in list(_NATIVE_DOC_POOL.items()):
            if pooled is doc:
                del _NATIVE_DOC_POOL[key]
        _NATIVE_DOC_POOL.mark_clean(doc, False)
        try:
            from plugin.calc.python.function import clear_python_addin_cache

            clear_python_addin_cache()
        except Exception:
            pass
        try:
            import gc
            import time

            # Release PyUNO sequences before Calc tears down the document.
            # Large getDataArray results held across close can abort soffice (glibc double-free).
            gc.collect()
            # Draw close can race URP shape release with SfxItemPool teardown if Python still
            # holds page/shape proxies. GC plus a short settle lets wrappers finish before
            # close (Writer ControlShape / charts share the same pool).
            time.sleep(_CLOSE_DOC_URP_SETTLE_S)
            if hasattr(doc, "close"):
                doc.close(True)
            elif hasattr(doc, "dispose"):
                doc.dispose()
            if sys.platform == "win32" and is_writer:
                from plugin.testing_runner import _progress_verbose as _progress

                # Reactivate keeper so the next factory does not target a leftover Writer.
                reactivate_harness_keeper()
                _progress("close_doc: done uid=%s" % (uid or "-"))
        except Exception as exc:
            # Harness-only: swallowing DisposedException here poisons the next factory open;
            # log the close failure trail instead.
            _log_close_doc_failure(exc)


    @staticmethod
    @contextlib.contextmanager
    def native_doc(ctx, doc_type="writer", hidden=True, reuse=None):
        """Yield a native LO document. Writer and Calc default to wipe-and-reuse pool (pass reuse=False for fresh doc)."""
        if reuse is None:
            reuse = _default_native_doc_reuse(doc_type)
        # Wipe-and-reuse the pooled Calc when leftover Writers make a new scalc factory unsafe.
        if doc_type == "calc" and not reuse and _windows_should_reuse_calc(ctx):
            from plugin.testing_runner import _progress_verbose as _progress

            reuse = True
            _progress("native_doc: leftover calc reuse")
        use_pool = bool(reuse) and doc_type in ("writer", "calc")
        doc = None
        pooled = False
        if use_pool:
            # Separate notebook pool on all platforms; see set_windows_notebook_host.
            pool_kind = "notebook_host" if (
                doc_type == "writer" and _windows_notebook_host()
            ) else doc_type
            key = (id(ctx), pool_kind, bool(hidden))
            candidate = _NATIVE_DOC_POOL.get(key)
            if candidate is not None and _native_doc_alive(candidate):
                try:
                    if not _NATIVE_DOC_POOL.is_clean(candidate):
                        reset_native_doc(candidate, doc_type, ctx)
                        _NATIVE_DOC_POOL.mark_clean(candidate, True)
                    if doc_type == "writer" and not _writer_pool_is_clean(candidate):
                        if sys.platform == "win32" and _windows_leftover_open() > 0:
                            from plugin.testing_runner import _progress_verbose as _progress

                            # Wipe again and keep the pooled Writer while leftovers remain.
                            _progress("native_doc: leftover writer pool dirty; keep")
                            reset_native_doc(candidate, doc_type, ctx)
                            _NATIVE_DOC_POOL.mark_clean(candidate, True)
                            doc = candidate
                            pooled = True
                        else:
                            TestingFactory.close_doc(candidate)
                            doc = None
                    else:
                        doc = candidate
                        pooled = True
                except Exception:
                    if sys.platform == "win32" and _windows_leftover_open() > 0:
                        from plugin.testing_runner import _progress_verbose as _progress

                        _progress("native_doc: leftover writer reset failed; keep")
                        doc = candidate
                        pooled = True
                    else:
                        TestingFactory.close_doc(candidate)
                        doc = None
            if doc is None:
                doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
                _NATIVE_DOC_POOL[key] = doc
                _NATIVE_DOC_POOL.mark_clean(doc, True)
                pooled = True
        else:
            doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
        if doc_type == "calc" and doc is not None:
            try:
                from plugin.scripting.session_manager import calc_workbook_base_session_id

                calc_workbook_base_session_id(doc)
            except Exception:
                pass
        body_exc: BaseException | None = None
        try:
            _NATIVE_DOC_POOL.mark_clean(doc, False)
            yield doc
        except BaseException as exc:
            body_exc = _exception_leaving_native_doc(exc)
        finally:
            teardown_exc: BaseException | None = None
            try:
                if pooled:
                    # Wipe before leaving the pool: tests without @with_native_doc still
                    # see this document as the desktop's current component (init scripts, charts).
                    _native_teardown_progress(
                        "native_doc: teardown reset start doc_type=%s" % doc_type
                    )
                    try:
                        reset_native_doc(doc, doc_type, ctx)
                        _NATIVE_DOC_POOL.mark_clean(doc, True)
                    except Exception:
                        _NATIVE_DOC_POOL.mark_clean(doc, False)
                        # Do not return from finally: that swallows a failed test
                        # body and warns on 3.12+ (SyntaxWarning: return in finally).
                        _native_teardown_progress(
                            "native_doc: teardown reset failed; close_doc"
                        )
                        TestingFactory.close_doc(doc)
                    else:
                        _native_teardown_progress("native_doc: teardown reset done")
                        try:
                            from plugin.calc.python.function import clear_python_addin_cache

                            clear_python_addin_cache()
                        except Exception:
                            pass
                else:
                    # Non-pooled Draw/Impress use close_draw_family_doc + settle (or skip
                    # close on Windows when leftovers remain); Math OLE Draw keeps close_doc.
                    uid = ""
                    try:
                        uid = str(getattr(doc, "RuntimeUID", None) or "")
                    except Exception:
                        uid = ""
                    if (
                        doc_type in ("impress", "draw")
                        and not _windows_should_skip_math_ole_close(uid)
                    ):
                        from plugin.testing_runner import _progress_verbose as _progress

                        leftover_open = _windows_leftover_open()
                        if _windows_should_skip_draw_family_close():
                            from plugin.testing_runner import current_office

                            # Skip closing while leftover Writers are open; suite-end recycle tears down.
                            _progress(
                                "native_doc: teardown skip impress/draw close leftover_open=%s"
                                % leftover_open
                            )
                            doc = None
                            current_office().request_recycle()
                            _log_office_health_after_close(ctx, doc_type)
                        else:
                            _progress(
                                "native_doc: teardown close_draw_family start doc_type=%s"
                                % doc_type
                            )
                            close_draw_family_doc(doc)
                            doc = None
                            settle_after_draw_family_close()
                            _log_office_health_after_close(ctx, doc_type)
                            _progress(
                                "native_doc: teardown close_draw_family done doc_type=%s"
                                % doc_type
                            )
                    else:
                        _native_teardown_progress("native_doc: teardown close_doc start")
                        TestingFactory.close_doc(doc)
                        # Harness probe (not a product fix): if close toasted URP, name
                        # it now instead of waiting for the next loadComponentFromURL.
                        _log_office_health_after_close(ctx, doc_type)
                        _native_teardown_progress("native_doc: teardown close_doc done")
            except BaseException as exc:
                teardown_exc = _exception_leaving_native_doc(exc)
            if teardown_exc is not None:
                if body_exc is None:
                    body_exc = teardown_exc
                else:
                    _log_close_doc_failure(teardown_exc)
                    _native_teardown_progress(
                        "native_doc: teardown failed after body error: %s" % teardown_exc
                    )
        if body_exc is not None:
            raise body_exc



    @staticmethod
    def create_context(doc=None, ctx=None, env="mock", doc_type="writer", services=None, **ctx_kwargs):
        """Create a ToolContext for mock or native tests.

        Mock: builds a stub doc via :meth:`create_doc` when ``doc`` is omitted.
        Native: requires an existing ``doc`` (compose with ``@with_native_doc`` /
        :meth:`native_doc`); does not open documents itself.

        Pass ``services=`` to use the live plugin registry (``get_services()``) instead
        of a fresh ``ServiceRegistry``. Extra ``ctx_kwargs`` go to ``ToolContext``
        (e.g. ``status_callback``, ``active_page_index``).
        """
        from plugin.framework.tool import ToolContext
        from plugin.framework.service import ServiceRegistry

        if env == "mock":
            if doc is None:
                doc = TestingFactory.create_doc(env="mock", doc_type=doc_type)
            if ctx is None:
                ctx = MockContext()
            if services is None:
                services = ServiceRegistry()
            return ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=services, caller="test", **ctx_kwargs)

        # Native env — caller owns document lifecycle (@with_native_doc).
        if doc is None:
            raise ValueError("create_context(env='native') requires doc= (use @with_native_doc)")
        if services is None:
            from plugin.doc.document_helpers import DocumentService
            from plugin.framework.event_bus import EventBus
            services = ServiceRegistry()
            services.register("document", DocumentService())
            services.register("events", EventBus())

        return ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=services, caller="test", **ctx_kwargs)

    @staticmethod
    def execute_tool(doc, ctx, name, args=None, *, doc_type="calc", services=None, **ctx_kwargs):
        """Run a registered tool against a live (or stub) document.

        Defaults to ``get_services()`` so native Calc/Draw suites share one path.
        ``KeyError`` / ``ValueError`` from the registry become
        ``{"status": "error", "error": ...}`` (same contract as the old per-file helpers).
        """
        from plugin.app import get_tools, get_services

        if services is None:
            services = get_services()
        tctx = TestingFactory.create_context(
            doc=doc,
            ctx=ctx,
            env="native",
            doc_type=doc_type,
            services=services,
            **ctx_kwargs,
        )
        try:
            return get_tools().execute(name, tctx, **(args or {}))
        except (KeyError, ValueError) as e:
            return {"status": "error", "error": str(e)}


def execute_calc_tool(doc, ctx, name, args):
    """Registered Calc tool. One copy of the old per-file ``_execute_calc_tool``.

    ``doc_type`` stays ``calc`` (also the ``TestingFactory.execute_tool`` default).
    Analysis suites that call GoalSeek/Solver directly do not use this: those
    tools are ToolBaseDummy and are not in the chat registry.
    """
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type="calc")


def native_writer_tool_context(doc, ctx):
    """Native Writer ToolContext via ``TestingFactory.create_context``.

    ``doc_type`` stays the create_context default (``writer``).
    """
    return TestingFactory.create_context(doc=doc, ctx=ctx, env="native")


def writer_services_tool_context(doc, ctx):
    """Writer ToolContext using live ``get_services()``.

    Page header/footer tools need the plugin registry. A partial bootstrap
    where ``get_services`` cannot be imported must still build a context
    (``services=None``) so the suite fails in the tool, not at import.
    """
    from plugin.framework.tool import ToolContext

    services = None
    try:
        from plugin.app import get_services

        services = get_services()
    except Exception:
        services = None
    return ToolContext(doc, ctx, "writer", services, "test")


def with_native_doc(doc_type="writer", hidden=True, reuse=None):
    """Decorator to inject a native LibreOffice document into a test function and guarantee teardown.

    Writer/Calc: wipe-and-reuse pooled document by default (faster than factory+close). Pass reuse=False
    when the test requires a virgin factory document.
    Draw/Impress never reuse. Factory-open DisposedException is annotated with
    the previous native test (docs/framework/uno-test-lifecycle.md).
    """
    def decorator(func):
        import functools
        import inspect

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Resolve ctx from args if present (native_test functions may receive ctx as first arg or kwargs)
            ctx = kwargs.get("ctx", None)
            if ctx is None and len(args) > 0:
                ctx = args[0]

            # Split body execution from wipe-and-reuse teardown in verbose logs.
            log_teardown = func.__name__ == "test_insert_cell_html"
            global _LOG_NATIVE_DOC_TEARDOWN
            prev_teardown_log = _LOG_NATIVE_DOC_TEARDOWN
            if log_teardown:
                _LOG_NATIVE_DOC_TEARDOWN = True
                from plugin.testing_runner import _progress_verbose as _progress

                _progress(
                    "with_native_doc: enter name=%s doc_type=%s" % (func.__name__, doc_type)
                )
            try:
                with TestingFactory.native_doc(ctx, doc_type=doc_type, hidden=hidden, reuse=reuse) as doc:
                    sig = inspect.signature(func)
                    call_kwargs = {}
                    # Inject by parameter name so ctx is never dropped when doc is added.
                    if "ctx" in sig.parameters:
                        call_kwargs["ctx"] = ctx
                    if "doc" in sig.parameters:
                        call_kwargs["doc"] = doc
                    if call_kwargs:
                        result = func(**call_kwargs)
                    elif len(sig.parameters) == 1:
                        result = func(doc)
                    else:
                        result = func(*args, **kwargs)
                    if log_teardown:
                        from plugin.testing_runner import _progress_verbose as _progress

                        _progress(
                            "with_native_doc: body returned name=%s; teardown start"
                            % func.__name__
                        )
                    return result
            finally:
                _LOG_NATIVE_DOC_TEARDOWN = prev_teardown_log
                if log_teardown:
                    from plugin.testing_runner import _progress_verbose as _progress

                    _progress("with_native_doc: teardown done name=%s" % func.__name__)
        return wrapper
    return decorator

def create_mock_client():
    """Creates a pre-configured MagicMock for an LlmClient."""
    mock_client = MagicMock()
    mock_client.config = MagicMock()
    mock_client.config.get.return_value = False
    return mock_client

def create_mock_http_response(
    status_code=200,
    json_data=None,
    *,
    reason=None,
    body=None,
    sse_lines=None,
    iter_side_effect=None,
    headers=None,
):
    """Mock ``http.client.HTTPResponse`` for pytest (no UNO, no live HTTP).

    * ``json_data`` / ``body`` feed sync ``response.read()``.
    * ``sse_lines`` feeds ``for line in response`` / ``iterate_sse`` (bytes or str).
    * ``iter_side_effect`` is raised after those lines (timeout / connection reset
      mid-stream). HTTP 4xx/5xx use ``status`` + ``reason`` + body. ``LlmClient``
      retries 429/503 up to three total attempts with backoff; other statuses raise immediately.
    """
    import http.client
    import json

    mock_resp = MagicMock()
    mock_resp.status = status_code
    mock_resp.reason = (
        reason if reason is not None else http.client.responses.get(status_code, "")
    )
    header_map = dict(headers or {})

    def _getheader(name, default=None):
        return header_map.get(name, header_map.get(str(name).lower(), default))

    mock_resp.getheader.side_effect = _getheader

    if body is None and json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
    mock_resp.read.return_value = b"" if body is None else body

    lines = []
    if sse_lines is not None:
        for line in sse_lines:
            if isinstance(line, str):
                line = line.encode("utf-8")
            if not line.endswith(b"\n"):
                line = line + b"\n"
            lines.append(line)

    if iter_side_effect is not None:
        def _iter():
            yield from lines
            raise iter_side_effect

        # return_value (not side_effect): ``for line in response`` matches
        # existing LlmClient tests that set ``__iter__.return_value = iter(...)``.
        mock_resp.__iter__.return_value = _iter()
    else:
        mock_resp.__iter__.return_value = iter(lines)
    return mock_resp


def agent_changes(model: Any) -> list[dict[str, Any]]:
    """Pending agent changes, one entry per logical change (grouped by token), in document
    order of first appearance: ``{"token", "old", "new"}``.

    Primarily a preview helper for tests.
    """
    from plugin.writer import review_scan as _review_scan

    grouped: dict[str, dict[str, Any]] = {}

    def on_item(rl: Any) -> bool:
        comment_raw = rl.getPropertyValue("RedlineComment")
        comment_str = _review_scan.normalize_redline_comment(comment_raw)
        token = comment_str if (comment_str and _review_scan.is_agent_token(comment_str)) else None
        if token is None:
            return True
        entry = grouped.setdefault(token, {"token": token, "old": "", "new": ""})
        start = rl.getPropertyValue("RedlineStart")
        end = rl.getPropertyValue("RedlineEnd")
        if start is None or end is None:
            return True
        try:
            t = start.getText()
            if t is None:
                return True
            span = t.createTextCursorByRange(start)
            span.gotoRange(end, True)
            text = span.getString()
            kind_raw = rl.getPropertyValue("RedlineType")
            kind = str(kind_raw) if kind_raw is not None else None
            if kind == "Delete":
                entry["old"] += text
            elif kind == "Insert":
                entry["new"] += text
        except Exception:
            pass
        return True

    _review_scan.scan_redlines(model, on_item)
    return list(grouped.values())


class FakeUndoManager:
    """In-memory XUndoManager fake for unit tests.

    ``titles`` stores undo titles newest-first, matching LibreOffice's
    ``getAllUndoActionTitles()``. Optional ``limit`` drops the oldest
    entry (tail) when a closed group is pushed onto a full stack.
    """

    def __init__(
        self,
        *,
        titles: list[str] | None = None,
        limit: int | None = None,
        locked: bool = False,
        undo_error: BaseException | None = None,
        drop_empty: bool = False,
    ) -> None:
        self.titles: list[str] = list(titles or [])
        self.limit = limit
        self.locked = locked
        self.undo_error = undo_error
        self.drop_empty = drop_empty
        self.entered: list[str] = []
        self.undone: int = 0
        self._ctx_stack: list[str] = []
        self._ctx_recorded: bool = False

    def isLocked(self) -> bool:
        return self.locked

    def isInContext(self) -> bool:
        return bool(self._ctx_stack)

    def isUndoEnabled(self) -> bool:
        return not self.locked

    def getAllUndoActionTitles(self) -> tuple[str, ...]:
        return tuple(self.titles)

    def record(self) -> None:
        """Mark that an undoable change occurred inside the active context."""
        if self._ctx_stack:
            self._ctx_recorded = True

    def enterUndoContext(self, title: str) -> None:
        if self.locked:
            return
        if not self._ctx_stack:
            self._ctx_recorded = False
        self.entered.append(title)
        self._ctx_stack.append(title)

    def leaveUndoContext(self) -> None:
        if self.locked or not self._ctx_stack:
            return
        title = self._ctx_stack.pop()
        if not self._ctx_stack:
            recorded = self._ctx_recorded
            self._ctx_recorded = False
            if self.drop_empty and not recorded:
                return
            self.titles.insert(0, title)
            if self.limit is not None and len(self.titles) > self.limit:
                del self.titles[self.limit :]

    def undo(self) -> None:
        if self.undo_error is not None:
            raise self.undo_error
        if self.titles:
            self.titles.pop(0)
            self.undone += 1
