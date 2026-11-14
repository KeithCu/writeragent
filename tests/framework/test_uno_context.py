from plugin.framework.queue_executor import main

import builtins
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.uno_context import get_ctx, get_extension_url, set_fallback_ctx


def test_get_ctx_with_uno():
    mock_uno = MagicMock()
    mock_ctx = MagicMock()
    mock_uno.getComponentContext.return_value = mock_ctx
    # B9 regression: patch.dict RESTORES the previous sys.modules['uno'] (the session-wide mock installed by
    # tests/conftest.py). The old pop('uno') left the whole run without a 'uno' module, so any
    # later test that imports uno lazily hit the real uno.py -> "No module named 'pyuno'"
    # (this is what broke tests/mcp/test_long_running_concurrency.py in combined runs).
    #
    # bootstrap() pins whatever get_ctx() returned into _fallback_ctx. On an
    # xdist worker an earlier test has already done that, so get_ctx() returns
    # the session uno mock's getComponentContext() child instead of mock_ctx.
    # Both are named mock.getComponentContext(); only the ids differ. This
    # test is the unset-fallback path. Put the pin back for later tests.
    from plugin.framework import uno_context as uc

    saved = uc._fallback_ctx
    set_fallback_ctx(None)
    try:
        with patch.dict(sys.modules, {'uno': mock_uno}):
            assert (get_ctx() == mock_ctx)
            mock_uno.getComponentContext.assert_called_once()
        assert sys.modules.get("uno") is not mock_uno and "uno" in sys.modules
    finally:
        set_fallback_ctx(saved)


def test_get_ctx_fallback():
    mock_fallback = MagicMock()
    set_fallback_ctx(mock_fallback)
    orig_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if (name == 'uno'):
            raise ImportError('simulated missing uno')
        return orig_import(name, globals, locals, fromlist, level)
    try:
        with patch.object(builtins, '__import__', failing_import):
            assert (get_ctx() == mock_fallback)
    finally:
        set_fallback_ctx(None)

def test_get_ctx_fallback_uno_returns_none():
    mock_uno = MagicMock()
    mock_uno.getComponentContext.return_value = None
    mock_fallback = MagicMock()
    # patch.dict restores the session-wide uno mock afterwards (see test_get_ctx_with_uno).
    with patch.dict(sys.modules, {'uno': mock_uno}):
        try:
            set_fallback_ctx(mock_fallback)
            assert (get_ctx() == mock_fallback)
        finally:
            set_fallback_ctx(None)




def test_resolve_package_extension_id_prefers_librepy():
    from plugin.framework.constants import EXTENSION_ID_LIBREPY
    from plugin.framework.uno_context import (
        get_extension_url,
        reset_package_extension_id_for_tests,
        resolve_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    pip = MagicMock()
    pip.getPackageLocation.side_effect = lambda eid: (
        "file:///tmp/LibrePy.oxt" if eid == EXTENSION_ID_LIBREPY else ""
    )
    with patch("plugin.framework.uno_context.get_package_info", return_value=pip):
        assert resolve_package_extension_id() == EXTENSION_ID_LIBREPY
        assert get_extension_url() == "file:///tmp/LibrePy.oxt"
    reset_package_extension_id_for_tests()


def test_resolve_package_extension_id_off_main_skips_package_info(monkeypatch):
    from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
    from plugin.framework.uno_context import (
        reset_package_extension_id_for_tests,
        resolve_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    monkeypatch.setattr(main, "is_current", lambda: False)
    with patch("plugin.framework.uno_context.get_package_info") as pip:
        assert resolve_package_extension_id() == EXTENSION_ID_WRITERAGENT
        pip.assert_not_called()
    reset_package_extension_id_for_tests()


def test_set_package_extension_id_override():
    from plugin.framework.constants import EXTENSION_ID_LIBREPY
    from plugin.framework.uno_context import (
        reset_package_extension_id_for_tests,
        resolve_package_extension_id,
        set_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_LIBREPY)
    assert resolve_package_extension_id() == EXTENSION_ID_LIBREPY
    reset_package_extension_id_for_tests()


def test_product_display_name_follows_extension_id():
    from plugin.framework.constants import EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT
    from plugin.framework.uno_context import (
        product_display_name,
        reset_package_extension_id_for_tests,
        set_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_LIBREPY)
    try:
        assert product_display_name() == "LibrePy"
    finally:
        reset_package_extension_id_for_tests()

    set_package_extension_id(EXTENSION_ID_WRITERAGENT)
    try:
        assert product_display_name() == "WriterAgent"
        with patch("plugin.framework.uno_context.is_libreharper", return_value=True):
            assert product_display_name() == "LibreHarper"
    finally:
        reset_package_extension_id_for_tests()


def test_get_desktop_returns_none_without_service_manager():
    from plugin.framework.uno_context import get_desktop, reset_desktop_create_is_unsafe_for_tests

    ctx = MagicMock()
    ctx.ServiceManager = None
    ctx.getServiceManager.return_value = None
    reset_desktop_create_is_unsafe_for_tests()
    try:
        with (
            patch.object(sys, "argv", ["soffice"]),
            patch("plugin.framework.uno_context._linux_process_tokens", return_value=("/usr/lib64/libreoffice/program/soffice.bin", "soffice.bin", [])),
        ):
            assert get_desktop(ctx) is None
    finally:
        reset_desktop_create_is_unsafe_for_tests()


def test_get_extension_path_rejects_non_file_url():
    from plugin.framework.uno_context import get_extension_path

    with patch("plugin.framework.uno_context.get_extension_url", return_value="vnd.sun.star.extension://org.writeragent"):
        assert get_extension_path() == ""


@pytest.mark.parametrize(
    ("argv", "proc"),
    [
        pytest.param(
            ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"],
            None,
            id="argv_uno_bin",
        ),
        pytest.param(
            [""],
            ("/usr/lib64/libreoffice/program/uno.bin", "uno.bin", ["/usr/lib64/libreoffice/program/uno.bin", "--quiet", "--singleaccept"]),
            id="proc_exe_uno_bin",
        ),
    ],
)
def test_get_desktop_skips_create_when_unsafe(argv: list[str], proc: tuple[str, str, list[str]] | None) -> None:
    """Register/enable uno.bin (via argv or /proc/self/exe) must not createInstance(Desktop) (#768)."""
    import contextlib
    from plugin.framework.uno_context import get_desktop, reset_desktop_create_is_unsafe_for_tests

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    reset_desktop_create_is_unsafe_for_tests()
    try:
        proc_patch = (
            patch("plugin.framework.uno_context._linux_process_tokens", return_value=proc)
            if proc is not None
            else contextlib.nullcontext()
        )
        with patch.object(sys, "argv", argv), proc_patch:
            assert get_desktop(ctx) is None
        smgr.createInstanceWithContext.assert_not_called()
    finally:
        reset_desktop_create_is_unsafe_for_tests()


def test_get_desktop_creates_on_soffice():
    from plugin.framework.uno_context import get_desktop, reset_desktop_create_is_unsafe_for_tests

    desktop = MagicMock()
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    reset_desktop_create_is_unsafe_for_tests()
    try:
        with (
            patch.object(sys, "argv", ["soffice"]),
            patch("plugin.framework.uno_context._linux_process_tokens", return_value=("/usr/lib64/libreoffice/program/soffice.bin", "soffice.bin", [])),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda obj: obj),
        ):
            assert get_desktop(ctx) is desktop
        smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.frame.Desktop", ctx)
    finally:
        reset_desktop_create_is_unsafe_for_tests()


def test_get_active_document_skips_desktop_create_on_no_vcl():
    from plugin.framework.uno_context import get_active_document, reset_desktop_create_is_unsafe_for_tests

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    reset_desktop_create_is_unsafe_for_tests()
    try:
        with patch.object(sys, "argv", ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"]):
            assert get_active_document(ctx) is None
        smgr.createInstanceWithContext.assert_not_called()
    finally:
        reset_desktop_create_is_unsafe_for_tests()


def test_get_active_document_reraises_disposed_desktop():
    """A disposed desktop is not "no document"."""
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import get_active_document

    class DisposedException(Exception):
        pass

    with (
        patch("plugin.framework.uno_context.get_desktop", side_effect=DisposedException("desktop gone")),
        pytest.raises(DocumentDisposedError),
    ):
        get_active_document(MagicMock())


def test_get_active_document_reraises_disposed():
    """A document that dies mid-call must not look like nothing is open."""
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import get_active_document

    class DisposedException(Exception):
        pass

    desktop = MagicMock()
    desktop.getCurrentComponent.side_effect = DisposedException("gone")
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        pytest.raises(DocumentDisposedError),
    ):
        get_active_document(MagicMock())


def test_get_active_document_none_when_component_missing():
    from plugin.framework.uno_context import get_active_document

    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        assert get_active_document(MagicMock()) is None


def test_get_active_document_none_on_other_uno_object_error():
    from plugin.framework.uno_context import get_active_document

    desktop = MagicMock()
    desktop.getCurrentComponent.side_effect = RuntimeError("not disposed")
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        assert get_active_document(MagicMock()) is None


class _NeverEq:
    """Two instances compare unequal so the helper must fall through ``is`` / ``==``."""

    def __eq__(self, other: object) -> bool:
        return False


def test_uno_same_identity():
    from plugin.framework.uno_context import uno_same

    obj = object()
    assert uno_same(obj, obj) is True
    assert uno_same(None, None) is True
    assert uno_same(None, object()) is False


def test_uno_same_eq_when_not_same_ref():
    from plugin.framework.uno_context import uno_same

    class AlwaysEq:
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    assert uno_same(AlwaysEq(), AlwaysEq()) is True


@pytest.mark.parametrize(
    "return_value, expected",
    [
        pytest.param(True, True, id="test_uno_same_issame_when_is_and_eq_fail"),
        pytest.param(False, False, id="test_uno_same_false_when_all_paths_differ"),
    ],
)
def test_uno_same_issame_when_is_and_eq_fail(return_value, expected):
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", return_value=return_value):
        assert uno_same(a, b) is expected

def test_uno_same_false_when_issame_missing():
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", None):
        assert uno_same(a, b) is False


def test_uno_same_mocked_issame_is_not_treated_as_true():
    """A session-wide MagicMock ``uno.isSame`` is truthy; must not collapse every pair to same."""
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", MagicMock()):
        assert uno_same(a, b) is False


def test_uno_same_proxy_eq_unwraps_target():
    """GUARD_ON proxy ``__eq__`` unwraps ``_target`` so proxy↔unwrapped is same (step 2)."""
    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_context import uno_same
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("GUARD_ON thread guard proxy stripped in release bundle")
    real = object()
    proxy = tg._UnoThreadGuardProxy(real)
    assert proxy is not real
    assert uno_same(proxy, real) is True
    assert uno_same(real, proxy) is True


def test_uno_same_issame_unwraps_proxy_first():
    """``uno.isSame`` must see the real PyUNO target, not the viral proxy wrapper."""
    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_context import uno_same
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("GUARD_ON thread guard proxy stripped in release bundle")
    real_a, real_b = object(), object()
    proxy_a = tg._UnoThreadGuardProxy(real_a)
    seen: list[tuple[object, object]] = []

    def _issame(left: object, right: object) -> bool:
        seen.append((left, right))
        return left is real_a and right is real_b

    with patch.object(sys.modules["uno"], "isSame", _issame):
        # ``==`` is False (distinct objects / proxy target ≠ other), so ladder hits isSame.
        assert uno_same(proxy_a, real_b) is True
    assert seen == [(real_a, real_b)]


def test_uno_same_off_thread_raises_on_thread_issame_still_works(monkeypatch):
    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_context import uno_same
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("release bundles stub main_thread_only")
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", return_value=True):
        assert uno_same(a, b) is True

    was = tg.GUARD_ON
    tg.GUARD_ON = True
    holder: dict[str, BaseException] = {}

    def _call() -> None:
        try:
            uno_same(object(), object())
        except BaseException as exc:
            holder["exc"] = exc

    try:
        worker = threading.Thread(target=_call, name="bg-uno-same")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        exc = holder.get("exc")
        assert isinstance(exc, RuntimeError)
        assert "UNO thread violation" in str(exc)
    finally:
        tg.GUARD_ON = was


def test_uno_boundaries_import_guard_uno_at_the_return():
    """Patching thread_guard.guard_uno must see boundary returns (not import-time _wrap_uno)."""
    from plugin.framework.doc_type import DocumentType
    from plugin.framework import uno_context as uc

    seen: list[object] = []

    def _guard(obj: object) -> object:
        seen.append(obj)
        return obj

    ctx = MagicMock(name="ctx")
    desktop = MagicMock(name="desktop")
    doc = MagicMock(name="doc")
    pip = MagicMock(name="pip")
    toolkit = MagicMock(name="tk")
    model = MagicMock(name="model")
    model.getURL.return_value = "file:///tmp/note.odt"
    smgr = MagicMock()
    smgr.createInstanceWithContext.side_effect = lambda name, _ctx: toolkit if "Toolkit" in name else desktop
    ctx.ServiceManager = smgr
    ctx.getValueByName.return_value = pip
    desktop.getCurrentComponent.return_value = doc
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = model
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop.getComponents.return_value = comps
    frame_model = MagicMock(name="frame_model")
    controller = MagicMock()
    controller.getModel.return_value = frame_model
    frame = MagicMock()
    frame.getController.return_value = controller

    saved = uc._fallback_ctx
    uc.reset_desktop_create_is_unsafe_for_tests()
    try:
        uc.set_fallback_ctx(ctx)
        with (
            patch("plugin.framework.thread_guard.guard_uno", side_effect=_guard),
            patch.object(sys, "argv", ["soffice"]),
            patch("plugin.framework.uno_context._linux_process_tokens", return_value=("soffice.bin", "soffice.bin", [])),
            patch("plugin.framework.doc_type.get_document_type", return_value=DocumentType.WRITER),
        ):
            assert uc.get_ctx() is ctx
            assert uc.get_desktop(ctx) is desktop
            assert uc.get_active_document(ctx) is doc
            assert uc.get_package_info(ctx) is pip
            assert uc.get_toolkit(ctx) is toolkit
            resolved, label = uc.resolve_document_by_url(ctx, "file:///tmp/note.odt")
            assert resolved is model
            assert label == "writer"
            assert uc.get_document_from_frame(frame) is frame_model
    finally:
        uc.set_fallback_ctx(saved)
        uc.reset_desktop_create_is_unsafe_for_tests()
    for obj in (ctx, desktop, doc, pip, toolkit, model, frame_model):
        assert obj in seen


def test_doc_identity_url_repairs_file_slash_without_changing_normalize():
    from plugin.framework.uno_context import _doc_identity_url, normalize_doc_url

    assert normalize_doc_url("file:/tmp/note.odt") == "file:/tmp/note.odt"
    assert _doc_identity_url("file:/tmp/note.odt") == _doc_identity_url("file:///tmp/note.odt")

def test_resolve_document_by_url_reraises_disposed_on_nextelement():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import resolve_document_by_url, set_fallback_ctx, reset_desktop_create_is_unsafe_for_tests
    from unittest.mock import MagicMock

    ctx = MagicMock()
    desktop = MagicMock()
    comps = MagicMock()
    enum = MagicMock()

    ctx.ServiceManager.createInstanceWithContext.return_value = desktop
    desktop.getComponents.return_value = comps
    comps.createEnumeration.return_value = enum
    enum.hasMoreElements.return_value = True

    # Simulate nextElement raising a disposed-like exception
    class DisposedException(Exception):
        pass

    # We construct a mock exception that has "disposed" in its string representation or is com.sun.star.lang.DisposedException
    exc = DisposedException("com.sun.star.lang.DisposedException: document is closed")
    # Actually, _reraise_document_disposed in errors.py looks for "disposed" or DisposedException type.
    enum.nextElement.side_effect = exc

    # Save ``uc._fallback_ctx`` before overriding it for this test and restore
    # it in ``finally``. Otherwise a teardown that calls ``set_fallback_ctx``
    # can leave the global fallback as ``None`` for later tests.
    import plugin.framework.uno_context as uc

    saved_ctx = uc._fallback_ctx
    set_fallback_ctx(ctx)
    reset_desktop_create_is_unsafe_for_tests()
    try:
        with pytest.raises(DocumentDisposedError):
            resolve_document_by_url(ctx, "file:///tmp/missing.odt")
    finally:
        set_fallback_ctx(saved_ctx)
        reset_desktop_create_is_unsafe_for_tests()

@pytest.mark.parametrize(
    ("exe", "comm", "cmd_arg", "argv", "expected"),
    [
        pytest.param("/usr/lib/libreoffice/program/soffice.bin", "soffice.bin", "/home/u/uno", [""], False, id="soffice_with_uno_arg"),
        pytest.param("/usr/lib/libreoffice/program/uno.bin", "uno.bin", "arg", [""], True, id="uno_bin_exe"),
        pytest.param("/usr/lib/libreoffice/program/soffice.bin", "soffice.bin", "--singleaccept", ["soffice", "--singleaccept"], False, id="soffice_singleaccept"),
    ],
)
def test_desktop_create_is_unsafe_now(exe: str, comm: str, cmd_arg: str, argv: list[str], expected: bool) -> None:
    from plugin.framework.uno_context import _desktop_create_is_unsafe_now
    proc = (exe, comm, [exe, cmd_arg])
    with (
        patch.object(sys, "argv", argv),
        patch("plugin.framework.uno_context._linux_process_tokens", return_value=proc),
    ):
        assert _desktop_create_is_unsafe_now() is expected


def test_focus_preserved_routes_through_restore_focus_callback():
    """BUG A fix 3: with a session callback, focus_preserved never calls setFocus itself."""
    from plugin.framework.uno_context import focus_preserved

    query = MagicMock()
    restore_focus = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit") as mock_tk:
        with focus_preserved(MagicMock(), query, restore_focus=restore_focus):
            pass

    restore_focus.assert_called_once_with()
    query.setFocus.assert_not_called()
    mock_tk.assert_not_called()


def test_focus_preserved_callback_runs_when_block_raises():
    from plugin.framework.uno_context import focus_preserved

    restore_focus = MagicMock()
    try:
        with focus_preserved(MagicMock(), restore_focus=restore_focus):
            raise ValueError("boom")
    except ValueError:
        pass
    restore_focus.assert_called_once_with()

def test_product_display_name_libreharper_auto_detect():
    from plugin.framework.constants import EXTENSION_ID_LIBREHARPER
    from plugin.framework.uno_context import (
        product_display_name,
        reset_package_extension_id_for_tests,
    )

    reset_package_extension_id_for_tests()
    pip = MagicMock()
    pip.getPackageLocation.side_effect = lambda eid: (
        "file:///tmp/LibreHarper.oxt" if eid == EXTENSION_ID_LIBREHARPER else ""
    )
    with (
        patch("plugin.framework.uno_context.get_package_info", return_value=pip),
        patch.dict("sys.modules", {"plugin._manifest": None}),
    ):
        assert product_display_name() == "LibreHarper"
    reset_package_extension_id_for_tests()

def test_get_package_info_returns_none_when_ctx_none():
    from plugin.framework.uno_context import get_package_info
    with patch("plugin.framework.uno_context.get_ctx", return_value=None):
        assert get_package_info() is None

def test_set_fallback_ctx_returns_previous():
    from plugin.framework import uno_context as uc

    saved = uc._fallback_ctx
    sentinel1 = object()
    sentinel2 = object()
    try:
        prev1 = uc.set_fallback_ctx(sentinel1)
        assert prev1 is saved
        assert uc._fallback_ctx is sentinel1
        prev2 = uc.set_fallback_ctx(sentinel2)
        assert prev2 is sentinel1
        assert uc._fallback_ctx is sentinel2
    finally:
        uc.set_fallback_ctx(saved)


def test_property_arg():
    from plugin.framework.uno_context import property_arg

    class _PV:
        def __init__(self, name: str, value: object):
            self.Name = name
            self.Value = value

    pv1 = _PV("Foo", "bar")
    pv2 = _PV("Num", 42)
    args = (pv1, pv2)

    assert property_arg(args, "Foo") == "bar"
    assert property_arg(args, "Num") == 42
    assert property_arg(args, "NonExistent") is None
    assert property_arg(None, "Foo") is None
    assert property_arg((), "Foo") is None


def test_get_extension_url_returns_empty_on_all_failures():
    # 1. No PIP
    with patch("plugin.framework.uno_context.get_package_info", return_value=None):
        assert get_extension_url(extension_id="test.id") == ""

    # 2. Empty location returned by PIP
    mock_pip = MagicMock()
    mock_pip.getPackageLocation.return_value = ""
    with patch("plugin.framework.uno_context.get_package_info", return_value=mock_pip):
        assert get_extension_url(extension_id="test.id") == ""

    # 3. Success path
    mock_pip.getPackageLocation.return_value = "file:///path/to/ext"
    with patch("plugin.framework.uno_context.get_package_info", return_value=mock_pip):
        assert get_extension_url(extension_id="test.id") == "file:///path/to/ext"


def test_iter_open_models_unwraps_models_and_frames():
    from plugin.framework.uno_context import iter_open_models

    desktop = MagicMock()
    comps = MagicMock()
    desktop.getComponents.return_value = comps

    # Direct model with getURL
    model1 = MagicMock()
    model1.getURL.return_value = "file:///tmp/doc1.odt"

    # Frame with getController().getModel()
    frame = MagicMock()
    del frame.getURL  # Ensure it doesn't look like a model
    controller = MagicMock()
    model2 = MagicMock()
    model2.getURL.return_value = "file:///tmp/doc2.odt"
    controller.getModel.return_value = model2
    frame.getController.return_value = controller

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [model1, frame]
    comps.createEnumeration.return_value = enum

    models = list(iter_open_models(desktop))
    assert models == [model1, model2]


def test_iter_open_models_stops_on_next_element_error():
    from plugin.framework.uno_context import iter_open_models

    desktop = MagicMock()
    comps = MagicMock()
    desktop.getComponents.return_value = comps

    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    enum.nextElement.side_effect = RuntimeError("Broken enumeration element")
    comps.createEnumeration.return_value = enum

    models = list(iter_open_models(desktop))
    assert models == []


def test_iter_open_models_stops_on_magicmock():
    from plugin.framework.uno_context import iter_open_models

    desktop = MagicMock()
    comps = MagicMock()
    desktop.getComponents.return_value = comps

    enum = MagicMock()
    comps.createEnumeration.return_value = enum

    models = list(iter_open_models(desktop))
    assert models == []
    enum.nextElement.assert_not_called()


def test_enum_has_more():
    from plugin.framework.uno_context import enum_has_more

    assert enum_has_more(MagicMock()) is False
    mock = MagicMock()
    mock.hasMoreElements.return_value = True
    assert enum_has_more(mock) is True
    mock.hasMoreElements.return_value = False
    assert enum_has_more(mock) is False


def test_iter_open_models_stops_at_enum_cap():
    from plugin.framework.uno_context import iter_open_models

    desktop = MagicMock()
    comps = MagicMock()
    desktop.getComponents.return_value = comps

    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    comps.createEnumeration.return_value = enum

    with patch("plugin.framework.uno_context._ENUM_CAP", 2):
        list(iter_open_models(desktop))
    assert enum.nextElement.call_count == 2


def test_iter_open_models_strict_false_probes_and_skips_broken():
    from plugin.framework.uno_context import iter_open_models

    desktop = MagicMock()
    comps = MagicMock()
    desktop.getComponents.return_value = comps

    good_model = MagicMock()
    good_model.getURL.return_value = "file:///tmp/good.odt"

    broken_model = MagicMock()
    broken_model.getURL.return_value = "file:///tmp/broken.odt"
    broken_model.getCurrentController.side_effect = RuntimeError("Broken controller")

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, True, False]
    enum.nextElement.side_effect = [broken_model, good_model]
    comps.createEnumeration.return_value = enum

    models = list(iter_open_models(desktop, strict=False))
    assert models == [good_model]


def test_document_id_for_frame():
    from plugin.framework.uno_context import document_id_for_frame

    # None frame returns empty string
    assert document_id_for_frame(None) == ""

    frame = MagicMock(name="frame")

    # Off main thread returns empty string
    with patch.object(main, "is_current", return_value=False):
        assert document_id_for_frame(frame) == ""

    # Model is None returns empty string
    with (
        patch.object(main, "is_current", return_value=True),
        patch("plugin.framework.uno_context.get_document_from_frame", return_value=None),
    ):
        assert document_id_for_frame(frame) == ""

    # Model resolved returns its runtime uid
    model = MagicMock(name="model")
    with (
        patch.object(main, "is_current", return_value=True),
        patch("plugin.framework.uno_context.get_document_from_frame", return_value=model),
        patch("plugin.framework.uno_context.get_runtime_uid", return_value="uid-1234"),
    ):
        assert document_id_for_frame(frame) == "uid-1234"


def test_desktop_create_is_unsafe_for_uno_bin_helper() -> None:
    from plugin.framework.uno_context import (
        desktop_create_is_unsafe,
        reset_desktop_create_is_unsafe_for_tests,
    )

    reset_desktop_create_is_unsafe_for_tests()
    try:
        with patch.object(sys, "argv", ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"]):
            assert desktop_create_is_unsafe()
        # The first call cached uno.bin. The soffice patch is invisible until reset.
        reset_desktop_create_is_unsafe_for_tests()
        with (
            patch.object(sys, "argv", ["soffice"]),
            patch(
                "plugin.framework.uno_context._linux_process_tokens",
                return_value=("/usr/lib64/libreoffice/program/soffice.bin", "soffice.bin", ["/usr/lib64/libreoffice/program/soffice.bin"]),
            ),
        ):
            assert not desktop_create_is_unsafe()
    finally:
        reset_desktop_create_is_unsafe_for_tests()


def test_normalize_doc_url():
    from plugin.framework.uno_context import normalize_doc_url

    assert normalize_doc_url("file:///test/") == "file:///test"
    assert normalize_doc_url("file:///test") == "file:///test"
    assert normalize_doc_url("") == ""
    assert normalize_doc_url(None) == ""


def _resolve_model(url, uid):
    m = MagicMock()
    m.getURL.return_value = url
    m.RuntimeUID = uid
    return m


def _resolve_by_url(models, target, type_map=None):
    """Run resolve_document_by_url over *models* with UNO desktop/enumeration mocked."""
    from plugin.framework.doc_type import DocumentType
    from plugin.framework.uno_context import resolve_document_by_url

    type_map = type_map or {}
    desktop = MagicMock()

    def make_enum(*_a, **_k):
        pending = list(models)
        enum = MagicMock()
        enum.hasMoreElements.side_effect = lambda: len(pending) > 0
        enum.nextElement.side_effect = lambda: pending.pop(0)
        return enum

    desktop.getComponents.return_value.createEnumeration.side_effect = make_enum
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop), \
         patch("plugin.framework.doc_type.get_document_type",
               side_effect=lambda m: type_map.get(m, DocumentType.WRITER)):
        return resolve_document_by_url(MagicMock(), target)


def test_match_by_url_regression():
    a = _resolve_model("file:///docs/a.odt", "uid-a")
    b = _resolve_model("file:///docs/b.ods", "uid-b")
    doc, doc_type = _resolve_by_url([a, b], "file:///docs/a.odt")
    assert doc is a
    assert doc_type == "writer"


def test_match_by_url_doc_type_calc():
    from plugin.framework.doc_type import DocumentType

    b = _resolve_model("file:///docs/b.ods", "uid-b")
    doc, doc_type = _resolve_by_url([b], "file:///docs/b.ods", type_map={b: DocumentType.CALC})
    assert doc is b
    assert doc_type == "calc"


def test_match_via_getController_getModel_branch():
    # An enumerated element that exposes getController().getModel() (a frame) instead of getURL
    # directly -- the desktop-enumeration path the URL/uid mocks otherwise skip.
    inner = _resolve_model("file:///docs/c.odt", "uid-c")
    frame = MagicMock(spec=["getController"])  # no getURL attr -> forces the getController branch
    frame.getController.return_value.getModel.return_value = inner
    doc, doc_type = _resolve_by_url([frame], "file:///docs/c.odt")
    assert doc is inner
    assert doc_type == "writer"


def test_match_by_uid_when_target_is_not_a_url():
    a = _resolve_model("file:///docs/a.odt", "uid-a")
    b = _resolve_model("file:///docs/b.ods", "uid-b")
    doc, _ = _resolve_by_url([a, b], "uid-b")
    assert doc is b


def test_match_unsaved_doc_by_uid():
    # An unsaved doc (no URL) is reachable only via its RuntimeUID.
    saved = _resolve_model("file:///docs/a.odt", "uid-a")
    untitled = _resolve_model("", "uid-untitled")
    doc, _ = _resolve_by_url([saved, untitled], "uid-untitled")
    assert doc is untitled


def test_no_match_returns_none():
    a = _resolve_model("file:///docs/a.odt", "uid-a")
    assert _resolve_by_url([a], "file:///docs/nope.odt") == (None, None)
    assert _resolve_by_url([a], "uid-does-not-exist") == (None, None)


def test_disposed_element_does_not_hide_a_later_match():
    class DisposedException(Exception):
        pass

    bad = MagicMock()
    bad.getURL.side_effect = DisposedException("gone")
    good = _resolve_model("file:///docs/a.odt", "uid-a")
    doc, doc_type = _resolve_by_url([bad, good], "file:///docs/a.odt")
    assert doc is good
    assert doc_type == "writer"


def test_stuck_next_element_does_not_spin():
    """A nextElement that raises without advancing must not spin the walk."""
    from plugin.framework.uno_context import resolve_document_by_url

    class StuckEnumException(Exception):
        pass

    calls = {"next": 0, "more": 0}

    def has_more():
        calls["more"] += 1
        # Bound the mock. Production must stop; this only keeps a regression
        # from hanging the suite if the walk continues forever.
        if calls["more"] > 8:
            raise AssertionError("resolve_document_by_url did not stop")
        return True

    def next_elem():
        calls["next"] += 1
        raise StuckEnumException("stuck frame")

    desktop = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = has_more
    enum.nextElement.side_effect = next_elem
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        assert resolve_document_by_url(MagicMock(), "file:///docs/a.odt") == (None, None)
    assert calls["next"] == 1


def test_resolve_reraises_when_desktop_is_disposed():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import resolve_document_by_url

    class DisposedException(Exception):
        pass

    with patch("plugin.framework.uno_context.get_desktop", side_effect=DisposedException("dead")):
        with pytest.raises(DocumentDisposedError):
            resolve_document_by_url(MagicMock(), "file:///tmp/note.odt")


def test_resolve_does_not_spin_on_a_magicmock_enumeration():
    """MagicMock.hasMoreElements() is truthy forever. The loop must stop."""
    from plugin.framework.uno_context import resolve_document_by_url

    desktop = MagicMock()
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        assert resolve_document_by_url(MagicMock(), "file:///tmp/note.odt") == (None, None)


def test_resolve_returns_none_when_desktop_is_missing():
    from plugin.framework.uno_context import resolve_document_by_url

    with patch("plugin.framework.uno_context.get_desktop", return_value=None):
        assert resolve_document_by_url(MagicMock(), "file:///tmp/note.odt") == (None, None)


def test_empty_inputs_never_match():
    # Empty target short-circuits; an empty uid/url must never match anything.
    from plugin.framework.uno_context import resolve_document_by_url

    assert resolve_document_by_url(MagicMock(), "") == (None, None)
    blank = _resolve_model("", "")
    assert _resolve_by_url([blank], "anything") == (None, None)


def test_resolve_reraises_when_enumeration_is_disposed():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import resolve_document_by_url

    class DisposedException(Exception):
        pass

    desktop = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = DisposedException("enum dead")
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with pytest.raises(DocumentDisposedError):
            resolve_document_by_url(MagicMock(), "file:///tmp/note.odt")


def test_get_runtime_uid_present_missing_and_error():
    from plugin.framework.uno_context import get_runtime_uid

    present = type("M", (), {"RuntimeUID": "abc"})()
    assert get_runtime_uid(present) == "abc"

    missing = MagicMock(spec=[])  # no RuntimeUID attribute at all
    assert get_runtime_uid(missing) == ""

    class Boom:
        @property
        def RuntimeUID(self):  # e.g. a disposed UNO object
            raise RuntimeError("disposed")

    assert get_runtime_uid(Boom()) == ""


def test_resolve_reraises_when_next_element_is_disposed():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import resolve_document_by_url

    class DisposedException(Exception):
        pass

    calls = {"next": 0, "more": 0}

    def has_more():
        calls["more"] += 1
        return True

    def next_elem():
        calls["next"] += 1
        raise DisposedException("com.sun.star.lang.DisposedException: frame dead")

    desktop = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = has_more
    enum.nextElement.side_effect = next_elem
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        with pytest.raises(DocumentDisposedError):
            resolve_document_by_url(MagicMock(), "file:///docs/a.odt")
    assert calls["next"] == 1

