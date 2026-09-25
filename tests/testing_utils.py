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
#   HTTP mocks       — create_mock_http_response

import contextlib
import sys
import types
from unittest.mock import MagicMock

# GHA 34595675515: ``python -m plugin.testing_runner`` imported
# ``tests.testing_utils``; suites imported ``plugin.tests.testing_utils``
# (``plugin/tests/__init__.py`` points ``__path__`` at ``tests/``). Same
# file, second object — keeper stayed empty on the suite copy. Register
# both names on first load so the second import hits sys.modules.
def _register_testing_utils_aliases() -> None:
    this = sys.modules[__name__]
    # Parent packages must exist before the dotted names are injected
    # (importing tests.testing_utils first used to make ``import plugin.tests``
    # fail with ``cannot import name 'tests' from 'plugin'``).
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

# GHA 33703959362: hang after insert_cell_html execute-done, before TEST end.
# When True, stderr breadcrumbs name reset_native_doc / _reset_calc_doc steps.
# with_native_doc sets this only for test_insert_cell_html (keep suite noise low).
_LOG_NATIVE_DOC_TEARDOWN = False


def _native_teardown_progress(msg: str) -> None:
    if not _LOG_NATIVE_DOC_TEARDOWN:
        return
    from plugin.testing_runner import _progress_verbose as _progress

    _progress(msg)


# testing_runner keeper. insert_cell_html_rich leaves extra Writers open
# (close skipped after paste). Never close those leftovers — GHA
# 34556185752 hung 30s in leftover close(True). Reactivate this keeper
# so a later Writer factory is not against a leftover current component.
class _HarnessState:
    """Process-wide leftover / keeper flags. One object (both import names)."""

    __slots__ = (
        "keeper_uid",
        "keeper_doc",
        "leftover_open",
        "writer_pool_reused",
        "hidden_open_bitmap",
        "notebook_host",
        "factory_seq",
        "math_ole_uids",
    )

    def __init__(self) -> None:
        self.keeper_uid = ""
        self.keeper_doc = None
        self.leftover_open = 0
        self.writer_pool_reused = False
        self.hidden_open_bitmap = False
        self.notebook_host = False
        self.factory_seq = 0
        self.math_ole_uids: set[str] = set()


_STATE = _HarnessState()

# Tests still read/assign the old module names (tu._WINDOWS_LEFTOVER_OPEN).
_STATE_MODULE_ATTRS = {
    "_HARNESS_KEEPER_UID": "keeper_uid",
    "_HARNESS_KEEPER_DOC": "keeper_doc",
    "_WINDOWS_LEFTOVER_OPEN": "leftover_open",
    "_WINDOWS_WRITER_POOL_REUSED": "writer_pool_reused",
    "_WINDOWS_HIDDEN_OPEN_BITMAP": "hidden_open_bitmap",
    "_WINDOWS_NOTEBOOK_HOST": "notebook_host",
    "_WINDOWS_FACTORY_SEQ": "factory_seq",
    "_WINDOWS_MATH_OLE_UIDS": "math_ole_uids",
}


class _TestingUtilsModule(types.ModuleType):
    def __getattr__(self, name):
        field = _STATE_MODULE_ATTRS.get(name)
        if field is not None:
            return getattr(_STATE, field)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        field = _STATE_MODULE_ATTRS.get(name)
        if field is not None:
            setattr(_STATE, field, value)
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _TestingUtilsModule


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
    """Harness breadcrumb: writer / calc / impress / draw. Impress first.

    ``is True``: MagicMock.supportsService() is truthy and would classify
    every mock as Writer. GHA 34609539461: leftover=1 from a prior prepare
    test then skipped close on an untyped mock, so
    ``test_close_doc_logs_urp_dispose`` never saw the dispose breadcrumb.
    """
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
    """``setActiveFrame`` the keeper. Does not close leftover paste Writers.

    GHA 34556185752: leftover ``close(True)`` hung 30s (uid=27). GHA
    34554275072: first text_helpers Writer factory + ``close_doc``
    returned; the *next* factory hung. After a test Writer close,
    desktop current becomes a leftover paste Writer. Reactivate the
    keeper so the next ``swriter`` load is not against that leftover.
    """
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

    What was wrong: GHA 34554275072 / 34553944171 hung 30s on the
    *second* text_helpers Writer factory. Leftovers uid=26/27 were
    already open (``close skipped pasted=True``). The first factory +
    ``close_doc`` returned. GHA 34556185752 then hung 30s *inside*
    leftover ``close(True)`` — product and harness must not close those
    Writers after paste (33771766524).

    Why this: enum leftovers (read-only; safe before load) and
    ``setActiveFrame`` the keeper. Do not close leftovers. Returns how
    many non-keeper Writers are still open. Windows-only caller.

    GHA 34593327841: the next hang was ``private:factory/scalc`` in
    ``document_research_uno`` ``_create_nearby_test_env``, not swriter.
    Call this before every Windows ``private:factory/`` load.
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


# Last leftover count from prepare lives on _STATE. leftover_open.
# close_doc / native_doc reuse read this instead of enumerating again
# (getComponents after paste close can hang). leftover_open=0 still
# reuses Writer on win32 (GHA 35470191616).


def _set_windows_leftover_open(n: int) -> None:
    """Cache leftover Writer count (prepare writes; close_doc / native_doc read)."""
    _STATE.leftover_open = int(n or 0)


def _windows_leftover_open() -> int:
    return int(_STATE.leftover_open or 0)


def _set_windows_writer_pool_reused(on: bool) -> None:
    _STATE.writer_pool_reused = bool(on)


def _windows_writer_pool_reused() -> bool:
    return bool(_STATE.writer_pool_reused)


def _windows_should_reuse_writer(ctx) -> bool:
    """True when a later Windows Writer factory would hang.

    GHA 34601787293 / 34602219973: unique ``_wa_factory_N`` loaded three
    leftover Calc factories and the first leftover swriter (uid=34).
    ``close_doc`` of that Writer returned; the next unique swriter hung
    30s. Reuse the first leftover Writer instead of close + factory.

    GHA 34652644656 (paste suites deferred): leftover_open=0,
    ``document_research_uno`` 3/3 and slash OK, then first text_helpers
    Hidden ``_blank`` + ``close_doc`` uid=29 returned; the next Hidden
    ``_blank`` hung 30s. Consecutive Hidden ``_blank`` swriter is unsafe
    even without paste leftovers (34597506651 with leftovers). Reuse
    the first Windows Writer.

    GHA 34661915875 (master ``aa6bf058``, tip of #737): leftover
    ``_wa_simpress`` at leftover_open=15 hung 30s. Notebook / importer
    suites isolated on ``_wa_notebook_host`` *and* disabled leftover
    Writer reuse, so each leftover factory stacked a Writer
    (leftover_open 1→15) because close is skipped. Reuse the leftover
    ``_wa_notebook_host`` pool — still not leftover HTML-paste
    ``_wa_factory`` (GHA 34643210006). Wipe leftover notebook host
    between leftover notebook tests; leftover form listeners stay
    per-document.
    """
    if ctx is None or sys.platform != "win32":
        return False
    return True


def _windows_should_reuse_calc(ctx) -> bool:
    """True when a leftover scalc factory would hang.

    GHA 34643210006: ``test_calc_reuse_false_still_empty`` loaded leftover
    ``target=_wa_scalc`` at leftover_open=5 and hung 30s (office alive).
    Unique leftover ``_wa_factory_N`` already failed then hung
    (34633295036). Cached leftover count only — do not enumerate
    (getComponents after paste close can hang). Do not close leftover
    paste Writers (34556185752).
    """
    if ctx is None or sys.platform != "win32":
        return False
    return _windows_leftover_open() > 0


# Same CREATE|GLOBAL as insert_cell_html_rich (8|55=63). Named target with
# flags 0 can search instead of creating. Do not reuse "_blank" / "_default"
# while leftover paste Writers are open — see _windows_factory_load_args.
_WINDOWS_FACTORY_SEARCH_FLAGS = 8 | 55
# Leftover Hidden swriter only. rich_html reuses one CREATE|GLOBAL name
# (_wa_calc_html). Unique _wa_factory_N stacked empty frames after close
# and the second leftover swriter hung (GHA 34602219973, _wa_factory_5).
_WINDOWS_FACTORY_TARGET = "_wa_factory"
# Leftover Calc uses one CREATE|GLOBAL name. The pooled @with_native_doc
# Calc is opened at leftover_open=0 as Hidden ``_blank`` — ``_wa_scalc``
# does not replace it. Unique ``_wa_factory_N`` stacked after a failed
# leftover scalc load (GHA 34633295036). Leftover Draw / leftover
# Impress use the same stable-name rule (GHA 34657826349).
_WINDOWS_CALC_FACTORY_TARGET = "_wa_scalc"
_WINDOWS_DRAW_FACTORY_TARGET = "_wa_sdraw"
_WINDOWS_IMPRESS_FACTORY_TARGET = "_wa_simpress"
# Unknown leftover factory URLs only (not sdraw / simpress). Unique
# leftover Draw/Impress names hung after notebook leftovers
# (GHA 34657826349, leftover_open=15, target=_wa_factory_10).
_WINDOWS_FACTORY_TARGETS = {
    "private:factory/swriter": _WINDOWS_FACTORY_TARGET,
    "private:factory/scalc": _WINDOWS_CALC_FACTORY_TARGET,
    "private:factory/sdraw": _WINDOWS_DRAW_FACTORY_TARGET,
    "private:factory/simpress": _WINDOWS_IMPRESS_FACTORY_TARGET,
}


def _windows_factory_load_args(factory_url: str, leftover_open: int) -> tuple[str, int]:
    """Target + FrameSearchFlag for a Windows factory load.

    GHA 34597506651: keeper sync worked (``keeper=1``, reactivated).
    First text_helpers ``_blank`` swriter + leftovers returned (uid=34,
    close_doc OK). The *next* ``_blank`` swriter hung 30s after the same
    leftover log + keeper reactivate.

    GHA 34599838644: same leftovers + ``keeper=1``, but Calc ``_blank``
    failed in ~1s (PyUNO traceback conversion on
    ``loadComponentFromURL(scalc)``) and the next Calc ``_blank`` hung
    30s — ``document_research_uno`` never finished, so the swriter-only
    named target was never reached. ``setActiveFrame`` is not enough
    for Hidden ``_blank`` while leftover ``_wa_calc_html`` frames exist
    (rich_html.py: not ``_blank`` / ``_default``).

    GHA 34602219973 (``ec40ed29``): unique ``_wa_factory_N`` loaded
    leftover Calc (``document_research_uno`` passed=3, targets
    ``_wa_factory_1/2/3``) and the first leftover Hidden swriter
    (``doc.test_text_helpers_uno.test_get_string_without_tracked_deletions_paragraph_bold_run_no_newline``,
    ``target=_wa_factory_4``, uid=34, ``close_doc`` OK). The *next*
    leftover Hidden swriter
    (``…_multi_para_joins_with_newline``, ``target=_wa_factory_5``)
    hung 30s in ``loadComponentFromURL`` — no RuntimeException. Unique
    CREATE stacks empty named frames after harness Writer close; it
    does not fix consecutive leftover Hidden swriter (same hang family
    as 34597506651). Reuse one CREATE|GLOBAL name for leftover
    ``swriter`` so CREATE replaces/reuses instead of stacking.

    GHA 34633295036 (master ``bdb421bb``, post #724): same leftover
    paste Writers (uids ``27``/``26``, ``frames=['-','-']``,
    ``keeper=1``) then unique leftover ``scalc`` ``target=_wa_factory_1``
    failed in ~766ms (PyUNO traceback wrap on
    ``loadComponentFromURL``). The next unique ``_wa_factory_2`` hung
    30s — same stacking family as leftover swriter unique names.
    Pooled Calc is still the leftover_open=0 ``_blank`` workbook.
    Leftover ``scalc`` now reuses one CREATE|GLOBAL name ``_wa_scalc``.
    Do not close leftover paste Writers (34556185752).

    GHA 34657826349 / 34657808315 (master ``0bf7d223``, tip of #734):
    leftover Draw/Impress unique ``_wa_factory_1``–``_wa_factory_9``
    succeeded at leftover_open=1. Notebook / importer suites then
    skipped Writer close (``leftover_open`` 1→15). Stable leftover
    ``swriter`` ``target=_wa_factory`` at leftover_open=15 returned.
    Unique leftover ``simpress`` ``target=_wa_factory_10`` hung 30s
    in ``loadComponentFromURL`` — same stacking family as leftover
    swriter ``_wa_factory_5`` and leftover scalc ``_wa_factory_2``.
    Leftover ``sdraw`` / ``simpress`` now reuse one CREATE|GLOBAL
    name each (``_wa_sdraw`` / ``_wa_simpress``).
    """
    if leftover_open <= 0 or not factory_url.startswith("private:factory/"):
        return "_blank", 0
    # One stable name, like rich_html._wa_calc_html. CREATE|GLOBAL finds
    # the empty frame left by the previous leftover-mode Writer close.
    if factory_url == "private:factory/swriter" and _windows_notebook_host():
        return _WINDOWS_NOTEBOOK_HOST_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS
    target = _WINDOWS_FACTORY_TARGETS.get(factory_url)
    if target is not None:
        return target, _WINDOWS_FACTORY_SEARCH_FLAGS
    _STATE.factory_seq += 1
    return "_wa_factory_%s" % _STATE.factory_seq, _WINDOWS_FACTORY_SEARCH_FLAGS


# Stable CREATE|GLOBAL name for Hidden .ipynb loads. Do not use "_blank"
# after leftover Writers — consecutive Hidden _blank hung detect
# (GHA 34619751330) the same way as leftover swriter (34597506651).
_WINDOWS_NOTEBOOK_TARGET = "_wa_notebook"


def note_windows_html_paste_leftover() -> None:
    """Mark leftover_open after ``insert_cell_html_rich`` close skipped.

    What was wrong: GHA 34649699848 / 34648929578 leftover paste Writers
    (uids 26/27) existed, but the cached leftover count stayed 0 because
    pooled Calc reuse never called ``prepare_windows_writer_factory``.
    Later slash ``createPeer`` and Hidden ``Budget_read.ods`` then hung
    or bitmap-failed without the skip path seeing leftovers.

    How: the paste UNO tests call this after a successful paste. Do not
    enum ``getComponents`` (can hang after paste close, 33771766524).
    Cached count only. Do not close leftover paste Writers (34556185752).
    """
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


# GHA 34661915875 (master aa6bf058, #737 live): leftover _wa_factory
# swriter at leftover_open=15 returned; leftover _wa_simpress hung 30s
# in loadComponentFromURL. Leftover Draw/Impress unique names succeeded
# at leftover_open=1 (34657826349). High leftover Writer count wedges
# a new app factory — skip leftover Draw/Impress, do not close leftovers.
# GHA 34672065355 (master #742+#743): leftover _wa_simpress hung 30s at
# leftover_open=3 (uids 40/39/29 Writer leftovers from notebook/importer).
# Old max=4 only skipped leftover_open>4, so leftover_open=3 still loaded.
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
    """Skip leftover Draw/Impress factory when leftover Writer count is high.

    GHA 34661915875: leftover ``_wa_simpress`` at leftover_open=15 hung
    30s (office alive). #737's stable name is live — hang is not unique
    ``_wa_factory_N`` stacking. Leftover swriter at leftover_open=15
    returned. GHA 34672065355: leftover ``_wa_simpress`` at
    leftover_open=3 hung 30s — skip leftover_open>2. Do not close
    leftover paste / notebook Writers (34556185752 / 34646877587).
    Leftover Writer / leftover Calc still load. Cached leftover count
    only.
    """
    if factory_url not in ("private:factory/sdraw", "private:factory/simpress"):
        return
    if not windows_cross_app_factory_unsafe(leftover_open):
        return
    app = factory_url.rsplit("/", 1)[-1]
    _raise_windows_skip(
        "windows leftover skip: leftover %s leftovers=%s" % (app, leftover_open),
        "Windows leftover Draw/Impress skip (%s, leftovers=%s)" % (app, leftover_open),
    )


def skip_windows_leftover_hidden_load(reason: str) -> None:
    """Skip leftover-poisoned Hidden loads on Windows.

    GHA 34646877587: first leftover Hidden ``_wa_notebook`` + close
    returned; the next Hidden ``_wa_notebook`` hung 30s. Unique leftover
    names (``_wa_notebook_2``, ``_wa_factory_N``) are the same stacking
    family as leftover ``_wa_scalc`` / ``_wa_factory_5``. Import-filter
    detect uses this; do **not** skip ``document_research_uno`` Hidden
    ``Budget_read.ods`` (34643210006 was 3/3). GHA 34675151298: leftover
    writer reuse then ``convert_mathml_to_starmath`` Hidden ``_blank``
    ``.mml`` hung 30s — latex / MathML UNO uses this + a reason string
    (XDL is patched; not AWT TOP). GHA 34678020608: that skip fired;
    next suite ``test_convert_mathml_to_starmath_fraction`` hung the
    same load. GHA 34679494812: leftover_open=3 (uids 40/39/29) then
    leftover swriter ``create_native_doc`` uid=41 returned;
    ``test_document_scripts_survive_save_reopen`` hung 30s in
    attach / storeAsURL / raw close / Hidden ``_blank`` reopen.
    GHA 34681661844: first ``html_to_plain_text`` Hidden ``_default``
    swriter returned; the next hung 30s. GHA 34683742049: those apply
    skips fired; next ``test_write_compact_heading1_resolves_to_spaced_uno``
    hung the same leftover Hidden ``_default`` load. Apply and MathML
    share this helper; do not add alias wrappers. Not AWT TOP (see
    ``skip_windows_awt_top_dialog``). Cached leftover count only — do
    not enum. Do not close leftover paste Writers (34556185752).
    """
    if not windows_leftover_hidden_load_unsafe():
        return
    leftovers = _windows_leftover_open()
    _raise_windows_skip(
        "windows leftover skip: %s leftovers=%s" % (reason, leftovers),
        "Windows leftover Hidden/AWT skip (%s, leftovers=%s)" % (reason, leftovers),
    )


def windows_leftover_hidden_load_unsafe() -> bool:
    """True when a leftover Hidden load or AWT ``createPeer`` would hang."""
    return sys.platform == "win32" and _windows_leftover_open() > 0


def windows_pooled_writer_reuse() -> bool:
    """True when this test's Writer came from the Windows reuse pool.

    GHA 35470191616 (master ``c4fdbee``, #809): leftover_open=0 after
    impress recycle still printed ``native_doc: leftover writer reuse``.
    ``skip_windows_leftover_hidden_load`` only fires leftover_open>0, so
    the cross-paragraph color test ran and failed a bare AssertionError.
    Sibling ``test_same_length_replacement_preserves_colors`` passed on
    the same reuse path. Not a product ``replace_preserving_format``
    change. Factory-fresh Windows Writer still runs this class of test.
    """
    return sys.platform == "win32" and _windows_writer_pool_reused()


def skip_windows_pooled_writer_reuse(reason: str) -> None:
    """Skip when Windows yielded a pooled Writer, including leftover_open=0.

    Do not use ``skip_windows_leftover_hidden_load`` alone for this —
    that helper only fires leftover_open>0 (GHA 35470191616). Cached
    pool-reuse flag only; do not enum. Do not factory-load a second
    Hidden ``_blank`` (34652644656 hung 30s at leftover_open=0).
    """
    if not windows_pooled_writer_reuse():
        return
    leftovers = _windows_leftover_open()
    _raise_windows_skip(
        "windows pool skip: %s leftovers=%s" % (reason, leftovers),
        "Windows pooled Writer reuse skip (%s, leftovers=%s)" % (reason, leftovers),
    )


# GHA 34655847157 (master f88b8749, leftovers=0, paste deferred): first
# Hidden Budget_read.ods raised ``Could not create system bitmap!`` in
# ~20ms; the next sibling Hidden open hung 30s. 34652644656 was 3/3 on
# the same copy path. After a bitmap, do not Hidden-open again.


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
    """Skip a later Hidden sibling open after a fast Windows bitmap fail.

    GHA 34655847157: leftover_open=0, slash OK, list_nearby OK, then
    Hidden ``Budget_read.ods`` bitmap-failed; the next
    ``open_document_for_read`` hung 30s. Attempt the first Hidden-open
    (34652644656 was 3/3). After bitmap, skip — do not hang.

    GHA 34670295632 (PR #742): #734 skipped later document_research
    Hidden siblings. Next suite ``text_helpers`` leftover writer reuse
    then Hidden ``_blank`` (leftover_open=0) hung 30s. ``create_native_doc``
    also skips Hidden ``_blank`` after bitmap. Named leftover factories
    still load.
    """
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
    """Skip TOP dialog ``createPeer`` / ``setVisible`` on Windows headless.

    GHA 34671277292 (master ``dc3be8d6``): after full calc UNO suites
    (all green, leftover_open not required),
    ``test_slash_popup_listbox_filter_and_keys`` printed ``TEST call``
    then hung 30s. Main thread was ``dlg.setVisible(True)`` after
    ``createPeer(toolkit, None)``; office stayed alive (kill-libreoffice
    later killed the same soffice PIDs). Worker threads were
    ``worker_pool._loop`` / ``_drain_soffice_stderr``.

    ``skip_windows_leftover_hidden_load`` does not cover this — that
    helper is leftover_open>0. Overlay ``createWindow`` TOP + later
    ``setVisible`` is the same VCL map. ENABLE_SLASH stays parked.
    Linux UNO still maps the overlay. Do not map a TOP dialog on win32
    CI.
    """
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


# Notebook-runner host while leftover HTML-paste Writers stay open.
# Not leftover ``_wa_factory`` (reuses paste leftovers) and not
# import-filter ``_wa_notebook`` (GHA 34643210006 leftover listeners).
_WINDOWS_NOTEBOOK_HOST_TARGET = "_wa_notebook_host"


def set_windows_notebook_host(on: bool) -> None:
    """Leftover factory uses ``_wa_notebook_host`` (not leftover ``_wa_factory``)."""
    _STATE.notebook_host = bool(on)


def _windows_notebook_host() -> bool:
    return bool(_STATE.notebook_host)


def windows_notebook_load_args() -> tuple[str, int]:
    """Target + FrameSearchFlag for a Hidden Jupyter ``loadComponentFromURL``.

    GHA 34619751330 (``5377a87e``, ``test_draw_uno`` already deferred):
    leftover Math Draw had **not** run. ``test_import_filter_uno_load_component``
    Hidden ``_blank`` + FilterName returned, then raw ``close(True)``.
    ``test_import_filter_uno_detect_without_filtername`` Hidden ``_blank``
    hung 30s (soffice still ``2444,5124``). Same consecutive leftover
    Hidden ``_blank`` family as 34597506651. POSIX keeps ``_blank``.

    GHA 34646877587: first Hidden ``_wa_notebook`` load+close notebook
    leftover uid=41 returned; the next Hidden ``_wa_notebook`` hung 30s
    (paste leftovers still open=3). Unique leftover ``_wa_notebook_2``
    is the same stacking family as leftover ``_wa_factory_N``. Do not
    close leftover notebook docs (same skip as paste Writers). The
    first leftover Hidden .ipynb load may run; the detect reload
    ``skip_windows_leftover_hidden_load``s instead of a second load.
    """
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

# GHA 34419828920 (master bf6c2ea2): peer test_peer_impress_rejected_on_resolved_model
# raw-closed Impress via doc.close(True), then the *next* test hung 30s in
# private:factory/swriter load (test_peer_message_uno.py:109). Office stayed
# alive (kill-libreoffice.ps1 still found soffice PIDs). close_doc's 50 ms is
# pre-close (release proxies before teardown). This is post-close: drop the
# closed Impress/Draw proxy, GC, then let URP finish before the next factory
# load. Do not put this inside close_doc — that would tax every Writer/Calc
# close. Windows needs longer; POSIX is a short drain. Not a product fix.
#
# GHA 34518091151 (#710 path): the hang moved *into* close_doc at Impress
# ``doc.close(True)`` (faulthandler 30s; office still alive). close_doc GCs
# leftover ``resolve_document_by_url`` wrappers then close()s after only
# 50 ms — too tight on Windows Draw-family. Pre-close settle uses the same
# Windows-longer budget; still not inside close_doc. POSIX only: Windows
# Draw-family close is a bare ``close(True)`` (see ``_draw_family_raw_close``).
_DRAW_FAMILY_POST_CLOSE_SETTLE_S = 0.75 if sys.platform == "win32" else 0.15
_DRAW_FAMILY_PRE_CLOSE_SETTLE_S = _DRAW_FAMILY_POST_CLOSE_SETTLE_S


def _draw_family_raw_close() -> bool:
    """True when Impress must be closed with a bare ``close(True)``.

    What the breadcrumbs showed:
    - GHA 34419828920 / #710: raw ``doc.close(True)`` (no pre-close GC)
      *returned* on Windows; the *next* Writer factory load then hung
      until ``settle_after_draw_family_close`` was added.
    - GHA 34518091151: ``close_doc`` (GC + 50 ms + ``close``) hung 30s
      *inside* Impress close.
    - GHA 34532953982 / 34535868114: GC + pre-close settle then
      ``close(True)`` / ``dispose()`` hung the same way.
    - GHA 34537826720: skipping close/dispose left Impress alive; the
      next ``private:factory/swriter`` load hung 30s.

    Skip is not a fix. Pre-close GC/sleep before close is the hung path.
    Raw close + post-close settle is the only sequence that both returned
    and (with #710's settle) was meant to unwedge the next Writer load.
    """
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


# plugin/draw/math_insert.py MATH_CLSID. close_doc of a Draw that still
# holds this OLE killed soffice (GHA 34607010446, exit 0).
_MATH_OLE_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"


def mark_windows_math_ole_doc(doc) -> None:
    """Harness-only: this Draw/Impress still holds a Math OLE.

    GHA 34607010446: ``test_insert_math_draw`` body returned, then
    ``close_doc`` ``dispose`` of that Draw killed soffice (exit 0).
    Nine earlier Draw ``close_doc`` calls in the same suite survived.
    Remember the uid so teardown can skip the close. The runner then
    defers ``test_draw_uno`` until just before the peer suite so this
    leftover is not ``close(True)``'d (34607010446). Notebook detect
    hang is leftover Hidden ``_blank`` (34619751330), not this Draw.
    Not a product fix.
    """
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

    What was wrong: GHA 34607010446 (master ``3720c175``) closed a Draw
    after ``insert_math`` and soffice exited 0. GHA 34612145495 then
    died on the *first* Draw forms ``close(True)`` after this helper
    walked pages/shapes and ``_native_doc_svc`` probed the model.
    Master closed ordinary Draw docs without that extra UNO.

    How: skip only when ``mark_windows_math_ole_doc`` recorded the uid.
    Do not walk the document. Unmarked Draw close stays GC + 50 ms +
    ``close(True)``. Not a product fix.
    """
    if sys.platform != "win32" or not uid:
        return False
    return uid in _windows_math_ole_uids()


def _windows_should_skip_draw_family_close() -> bool:
    """True when native_doc must not close Impress/Draw on Windows.

    What was wrong: GHA 35450779692 (post-#803 SHA ``cd9da961``)
    leftover Writer reuse (``leftover_open=1``,
    ``html_paste_writer: leftovers open=1 uids=['27']``) then
    ``draw.test_designs_uno`` loaded ``private:factory/simpress``
    (``target=_wa_simpress`` flags=63, uid=30). Body returned
    (~300ms). ``native_doc`` teardown already used
    ``close_draw_family_doc`` (bare ``close(True)`` on win32). During
    that raw close (~3s) soffice exited 0; pids disappeared;
    ``LIFECYCLE close_doc dispose`` / ``LIFECYCLE office dead after
    close doc_type=impress``; raw close raised ``DisposedException``;
    runner aborted remaining suites. Same abort as pre-#803
    35413789298 / 35407952704 — GC/sleep avoidance is not enough
    when a leftover Writer is open.

    How: skip ``close_draw_family_doc`` on win32 only when
    ``_windows_leftover_open() > 0``. Drop the proxy; leftover
    ``CREATE|GLOBAL`` ``_wa_simpress`` / ``_wa_sdraw`` reuse replaces
    instead of stacking (34657826349). Peer skip-close is the same
    family (``test_peer_message_uno``); suite-end recycle kills the
    leftover Draw-family doc. Math OLE Draw still uses ``close_doc``
    so that skip stays (34607010446). Not a product fix.
    """
    if sys.platform != "win32":
        return False
    return _windows_leftover_open() > 0


def close_draw_family_doc(doc):
    """Harness-only Impress/Draw close. Does **not** go through ``close_doc``.

    What was wrong: GHA 34518091151 hung 30s in ``TestingFactory.close_doc``
    at ``doc.close(True)`` while tearing down Impress in
    ``test_peer_impress_rejected_on_resolved_model`` (Writer still open;
    both factory loads had succeeded). #710's post-close settle never ran.
    Skipping close/dispose (34537826720) unblocked that teardown, then the
    *next* ``private:factory/swriter`` load hung 30s — leftover Impress
    poisons later Writer factory loads (same family as #710).

    How: ``close_doc`` GCs leftover peer-resolve wrappers then close()s
    after 50 ms. On Windows, GC + sleep *immediately before* ``close`` /
    ``dispose`` blocks the UI thread (34532953982 / 34535868114). A bare
    ``close(True)`` with no pre-close GC is the only Impress close that
    has returned on Windows (#710 / 34419828920).

    Why this: Windows calls ``close(True)`` with no setModified / GC /
    sleep in front of it, then callers drop the proxy and
    ``settle_after_draw_family_close``. POSIX still marks unmodified, GC,
    pre-close settle, then ``close(True)``. Peer tests keep Writer open
    across the Windows raw close (the #710 returning order) and
    re-activate it before closing Writer. Logs svc/uid and each step.
    Not a product fix.
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
    """Harness-only: GC + sleep after closing Impress/Draw before the next factory load.

    Call after ``close_draw_family_doc`` *and* after dropping the local
    reference. The next ``loadComponentFromURL`` is the hang site if this
    settle is skipped *or* if Impress was never actually closed
    (GHA 34537826720 skip-teardown; see
    ``tests/chatbot/test_peer_message_uno.py``).
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
    """Name whether the Calc model is already wedged before the udprop write.

    GHA 33763078357: sheet-level reset returned, then getDocumentProperties blocked.
    GHA 33771766524 / 33772063173: #572's desktop.getComponents probe became
    the Windows hang site after close+paste (deterministic). Product fix is in
    rich_html.py (reuse Writer, do not enumerate after close). Test harness
    keeps only a cheap RuntimeUID attribute read here.
    """
    _native_teardown_progress("udprops probe: RuntimeUID start")
    try:
        uid = getattr(doc, "RuntimeUID", None)
        _native_teardown_progress("udprops probe: RuntimeUID done uid=%s" % uid)
    except Exception as exc:
        _native_teardown_progress("udprops probe: RuntimeUID failed %r" % (exc,))


def _clear_writeragent_udprops(doc, ctx=None) -> None:
    # GHA 33707990007: after insert_cell_html, Windows hung in
    # set_document_scripts → is_document_readonly_for_scripts → doc.isReadonly()
    # during calc native-doc wipe (remove_charts + clearContents had already
    # returned). A harness wipe is never a user-readonly save; write the
    # scripts UDProp the same way as the session ids so we never call
    # isReadonly(). Empty string is missing to get_document_scripts.
    # GHA 33763078357: that workaround relocated the hang to
    # getDocumentProperties() — do not skip this write; probe first, then
    # trace each UNO step inside set_document_property.
    from plugin.doc import udprops as udprops_mod

    prev_trace = udprops_mod._TRACE_UDPROPS
    # Keep suite noise low: desktop/clipboard probe + per-UNO-step stderr
    # only when with_native_doc armed teardown logs (test_insert_cell_html).
    if _LOG_NATIVE_DOC_TEARDOWN:
        if ctx is not None:
            _probe_soffice_before_udprops(doc, ctx)
        udprops_mod._TRACE_UDPROPS = True
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
    finally:
        udprops_mod._TRACE_UDPROPS = prev_trace


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
    """Clear leftover header/footer XText and restore shared-page defaults.

    Body wipe does not touch page-style regions. Windows Writer reuse
    (leftover_open=0) then left ``header_first`` / ``FirstIsShared=False``
    from a prior letterhead test, so ``page_set_style_properties``
    correctly refused disable (GHA 35466498641). Also reset
    ``PageDescName`` so findFirst searches the Standard header we write.
    """
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
        # GHA 34593327841 / 34599838644 / 34602219973 / 34633295036:
        # leftover paste Writers as desktop current hung the next factory
        # (30s). #720 only prepared swriter. Reactivate the keeper.
        # Leftover swriter reuses one CREATE|GLOBAL name (_wa_factory).
        # Leftover Calc reuses _wa_scalc (unique _wa_factory_N failed
        # then hung, 34633295036). Leftover Draw / leftover Impress
        # reuse _wa_sdraw / _wa_simpress (unique _wa_factory_10 hung
        # at leftover_open=15, 34657826349).
        if sys.platform == "win32" and factory_url.startswith("private:factory/"):
            leftover_open = prepare_windows_writer_factory(ctx)
            target, flags = _windows_factory_load_args(factory_url, leftover_open)
            from plugin.testing_runner import _progress_verbose as _progress

            _progress(
                "create_native_doc: windows factory leftover_open=%s url=%s target=%s flags=%s"
                % (leftover_open, factory_url, target, flags)
            )
            # GHA 34661915875 / 34672065355: leftover _wa_simpress hung
            # 30s at leftover_open=15 and leftover_open=3. Stable name
            # is live. Skip leftover Draw/Impress when leftover Writer
            # count is >2 — leftover swriter at leftover_open=15
            # returned.
            skip_windows_cross_app_factory(factory_url, leftover_open)
            # GHA 34670295632 (PR #742): #734 skipped later
            # document_research Hidden siblings after Budget_read
            # bitmap. Next suite text_helpers leftover writer reuse
            # then Hidden _blank (leftover_open=0) hung 30s. After
            # bitmap, Hidden _blank is unsafe. Named leftover
            # factories still load.
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
        # Skip leftover-window Writer close *before* dropping the pool entry
        # so reuse can still find the doc (34602219973).
        uid = ""
        is_writer = False
        leftover_open = 0
        if sys.platform == "win32":
            try:
                uid = str(getattr(doc, "RuntimeUID", None) or "")
            except Exception:
                uid = ""
            # GHA 34612145495: _native_doc_svc + page walk before the first
            # Draw forms close(True) killed soffice (exit 0). Master
            # 34607010446 closed that same forms Draw. Check the Math
            # mark from RuntimeUID only — no supportsService / getDrawPages.
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
                    # GHA 34602219973: close uid=34 returned; next unique
                    # _wa_factory_5 swriter hung 30s. Do not close a
                    # harness Writer while paste leftovers remain (same
                    # ban as leftover paste close, 34556185752).
                    # GHA 34646877587: close notebook leftover uid=41
                    # returned; next Hidden ``_wa_notebook`` hung 30s.
                    # Per-doc listener counts + ``_wa_notebook_host``
                    # isolate notebook_runner. Do not close leftover
                    # notebook docs or leftover paste Writers.
                    reactivate_harness_keeper()
                    _progress(
                        "close_doc: skip writer close leftovers open=%s uid=%s"
                        % (leftover_open, uid or "-")
                    )
                    return
        for key, pooled in list(_NATIVE_DOC_POOL.items()):
            if pooled is doc:
                del _NATIVE_DOC_POOL[key]
        if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
            _NATIVE_DOC_POOL.mark_clean(doc, False)
        try:
            from plugin.scripting.session_manager import clear_active_calc_session

            clear_active_calc_session()
        except Exception:
            pass
        try:
            import gc
            import time

            # Release PyUNO sequences before Calc tears down the document.
            # Large getDataArray results held across close can abort soffice (glibc double-free).
            gc.collect()
            # What was wrong: Draw close raced URP ~SvxShape / SdrRectObj with
            # SfxItemPool::unregisterNameOrIndex (SalAbort after the test returned).
            # How: Python still held page/shape proxies; close tore the model down
            # while cppu_threadpool released the wrappers. Why this: GC then a short
            # settle lets ~SvxShape finish before close. Unscoped: Writer
            # ControlShape / charts use the same pool. Post-close wait did not help.
            time.sleep(_CLOSE_DOC_URP_SETTLE_S)
            if hasattr(doc, "close"):
                doc.close(True)
            elif hasattr(doc, "dispose"):
                doc.dispose()
            if sys.platform == "win32" and is_writer:
                from plugin.testing_runner import _progress_verbose as _progress

                # Test Writer close returned (34554275072). Leftover close
                # hangs (34556185752). Reactivate keeper so the next factory
                # is not against a leftover paste Writer as current.
                reactivate_harness_keeper()
                _progress("close_doc: done uid=%s" % (uid or "-"))
        except Exception as exc:
            # Harness-only: close used to swallow DisposedException, so the
            # *next* factory open became the named victim. Log the trail here.
            _log_close_doc_failure(exc)


    @staticmethod
    @contextlib.contextmanager
    def native_doc(ctx, doc_type="writer", hidden=True, reuse=None):
        """Yield a native LO document. Writer and Calc default to wipe-and-reuse pool (pass reuse=False for fresh doc)."""
        if reuse is None:
            reuse = _default_native_doc_reuse(doc_type)
            if doc_type == "writer" and _windows_should_reuse_writer(ctx):
                from plugin.testing_runner import _progress_verbose as _progress

                reuse = True
                if _windows_notebook_host():
                    _progress("native_doc: leftover notebook host reuse")
                else:
                    _progress("native_doc: leftover writer reuse")
        # reuse=False still calls create_native_doc. Leftover _wa_scalc
        # hung 30s (34643210006). Wipe-and-reuse the pooled Calc instead.
        if doc_type == "calc" and not reuse and _windows_should_reuse_calc(ctx):
            from plugin.testing_runner import _progress_verbose as _progress

            reuse = True
            _progress("native_doc: leftover calc reuse")
        use_pool = bool(reuse) and doc_type in ("writer", "calc")
        doc = None
        pooled = False
        writer_reused = False
        if use_pool:
            # Leftover notebook host must not reuse leftover _wa_factory
            # (GHA 34643210006 leftover HTML-paste leftover). Same leftover
            # Writer type, separate leftover pool.
            pool_kind = "notebook_host" if (
                doc_type == "writer" and _windows_notebook_host()
            ) else doc_type
            key = (id(ctx), pool_kind, bool(hidden))
            candidate = _NATIVE_DOC_POOL.get(key)
            if candidate is not None and _native_doc_alive(candidate):
                try:
                    if hasattr(_NATIVE_DOC_POOL, "is_clean") and not _NATIVE_DOC_POOL.is_clean(candidate):
                        reset_native_doc(candidate, doc_type, ctx)
                        if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
                            _NATIVE_DOC_POOL.mark_clean(candidate, True)
                    if doc_type == "writer" and not _writer_pool_is_clean(candidate):
                        if sys.platform == "win32" and _windows_leftover_open() > 0:
                            from plugin.testing_runner import _progress_verbose as _progress

                            # close + factory hangs (34602219973). Wipe again
                            # and keep the leftover-window Writer.
                            _progress("native_doc: leftover writer pool dirty; keep")
                            reset_native_doc(candidate, doc_type, ctx)
                            if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
                                _NATIVE_DOC_POOL.mark_clean(candidate, True)
                            doc = candidate
                            pooled = True
                            writer_reused = True
                        else:
                            TestingFactory.close_doc(candidate)
                            doc = None
                    else:
                        doc = candidate
                        pooled = True
                        writer_reused = doc_type == "writer"
                except Exception:
                    if sys.platform == "win32" and _windows_leftover_open() > 0:
                        from plugin.testing_runner import _progress_verbose as _progress

                        _progress("native_doc: leftover writer reset failed; keep")
                        doc = candidate
                        pooled = True
                        writer_reused = doc_type == "writer"
                    else:
                        TestingFactory.close_doc(candidate)
                        doc = None
            if doc is None:
                doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
                _NATIVE_DOC_POOL[key] = doc
                if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
                    _NATIVE_DOC_POOL.mark_clean(doc, True)
                pooled = True
                writer_reused = False
        else:
            doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
            writer_reused = False
        if doc_type == "writer":
            # GHA 35470191616: leftover_open=0 still reuses. Tests that
            # fail only on the pooled Writer (cross-para colors) read
            # this flag — not leftover_open.
            _set_windows_writer_pool_reused(writer_reused)
        if doc_type == "calc" and doc is not None:
            try:
                from plugin.scripting.session_manager import calc_workbook_base_session_id

                calc_workbook_base_session_id(doc)
            except Exception:
                pass
        try:
            if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
                _NATIVE_DOC_POOL.mark_clean(doc, False)
            yield doc
        finally:
            if pooled:
                # Wipe before leaving the pool: tests without @with_native_doc still
                # see this document as the desktop's current component (init scripts, charts).
                _native_teardown_progress(
                    "native_doc: teardown reset start doc_type=%s" % doc_type
                )
                try:
                    reset_native_doc(doc, doc_type, ctx)
                    if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
                        _NATIVE_DOC_POOL.mark_clean(doc, True)
                except Exception:
                    if hasattr(_NATIVE_DOC_POOL, "mark_clean"):
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
                        from plugin.scripting.session_manager import clear_active_calc_session

                        clear_active_calc_session()
                    except Exception:
                        pass
            else:
                # GHA 35413789298 (master 609490ec) / 29079e14: first
                # designs_uno Impress @with_native_doc teardown called
                # close_doc (gc.collect + 50 ms + close(True)) after
                # leftover Writer reuse (leftover_open=1). soffice exited
                # 0; URP disposed; runner aborted remaining suites.
                # How: close_doc's pre-close GC/sleep is the Windows
                # Impress/Draw killer (34518091151 / 34532953982 /
                # 34535868114). Peer tests already use
                # close_draw_family_doc (bare close(True) on win32) +
                # settle_after_draw_family_close.
                # GHA 35450779692 (post-#803 cd9da961): that raw close
                # still killed soffice with leftover_open=1. Skip the
                # Draw-family close on win32 while leftovers remain
                # (peer skip-close family). Math OLE Draw still uses
                # close_doc so the existing skip stays (34607010446).
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
                        from plugin.testing_runner import (
                            request_office_recycle_after_suite,
                        )

                        # Do not close(True) while leftover Writer is
                        # open (35450779692). Drop the proxy; leftover
                        # CREATE|GLOBAL _wa_simpress / _wa_sdraw reuse
                        # replaces instead of stacking. Suite-end
                        # recycle kills leftovers (34537826720).
                        _progress(
                            "native_doc: teardown skip impress/draw close leftover_open=%s"
                            % leftover_open
                        )
                        doc = None
                        request_office_recycle_after_suite()
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
        from plugin.main import get_tools, get_services

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

            # GHA 33703959362: no TEST end after execute-done. Enter/exit here
            # splits a body hang from wipe-and-reuse teardown after return.
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