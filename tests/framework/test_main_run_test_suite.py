"""Tests for in-LO menu test suite dispatch (``plugin.main._run_test_suite``)."""

import sys
import threading
import types
from unittest.mock import MagicMock, patch

# `plugin.main` imports UNO interface modules; headless pytest may have partial or conflicting stubs.
def _ensure_main_import_stubs() -> None:
    task = sys.modules.get("com.sun.star.task")
    if task is None:
        task = types.ModuleType("com.sun.star.task")
        sys.modules["com.sun.star.task"] = task
    task.XJobExecutor = type("XJobExecutor", (), {})
    task.XJob = type("XJob", (), {})

    frame = sys.modules.get("com.sun.star.frame")
    if frame is None:
        frame = types.ModuleType("com.sun.star.frame")
        sys.modules["com.sun.star.frame"] = frame
    if not hasattr(frame, "DispatchDescriptor"):
        frame.DispatchDescriptor = type("DispatchDescriptor", (), {})
    if not hasattr(frame, "XDispatch"):
        frame.XDispatch = type("XDispatch", (), {})
    if not hasattr(frame, "XDispatchProvider"):
        frame.XDispatchProvider = type("XDispatchProvider", (), {})

    lang = sys.modules.get("com.sun.star.lang")
    if lang is not None:
        if not hasattr(lang, "XInitialization"):
            lang.XInitialization = type("XInitialization", (), {})
        if not hasattr(lang, "XServiceInfo"):
            lang.XServiceInfo = type("XServiceInfo", (), {})


def _patch_unohelper_implementation_helper() -> None:
    class _FakeImplHelper:
        def addImplementation(self, *args, **kwargs) -> None:
            pass

    uh = sys.modules["unohelper"]
    uh.ImplementationHelper = lambda: _FakeImplHelper()


_ensure_main_import_stubs()
_patch_unohelper_implementation_helper()

def test_run_test_suite_invokes_run_module_suite_on_main_thread() -> None:
    """``run_module_suite`` must run on the UI thread so UNO tools pass ``execute_safe``."""
    import plugin.main as main_mod
    
    threads_seen: list[threading.Thread] = []

    def fake_run_module_suite(ctx, module, name, doc_model=None):
        threads_seen.append(threading.current_thread())
        return (0, 0, [])

    fake_ctx = MagicMock()
    with (
        patch("plugin.main._tests_bundled", return_value=True),
        patch("plugin.framework.uno_context.get_ctx", return_value=fake_ctx),
        patch.object(main_mod, "get_active_document", return_value=None),
        patch("plugin.chatbot.dialogs.msgbox"),
        patch("plugin.testing_runner.run_module_suite", side_effect=fake_run_module_suite),
    ):
        main_mod._run_test_suite(MagicMock(), lambda _m: True, "writer.format_tests")

    assert len(threads_seen) == 1
    assert threads_seen[0] is threading.main_thread()


def test_menu_text_translates_vision_ocr_and_debug(monkeypatch) -> None:
    """Menubar titles come from get_menu_text, not from a po msgstr alone.

    Vision OCR was missing from the static map (None → Addons.xcu en-US).
    Debug was a raw English literal, so the status event overwrote the catalog.
    """
    import plugin.main as main_mod

    monkeypatch.setattr(main_mod, "bootstrap", lambda: None)

    def fake_gettext(message: str) -> str:
        return {
            "Vision OCR Settings...": "Vision OCR 設定...",
            "Debug": "デバッグ",
        }.get(message, message)

    monkeypatch.setattr("plugin.framework.i18n._", fake_gettext)
    assert main_mod.get_menu_text("vision.open_settings") == "Vision OCR 設定..."
    assert main_mod.get_menu_text("main.NoOp") == "デバッグ"
    assert main_mod.get_menu_text("main.settings") == "Settings"
