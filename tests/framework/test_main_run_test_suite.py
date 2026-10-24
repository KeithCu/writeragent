"""Tests for in-LO menu test suite dispatch (``plugin.framework.main_shared._run_test_suite``)."""

import sys
import threading
import types
from typing import Any
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

import plugin.framework.main_shared as shared_mod
import plugin.main as main_mod


def test_run_test_suite_invokes_run_module_suite_on_main_thread() -> None:
    """``run_module_suite`` must run on the UI thread so UNO tools pass ``execute_safe``."""
    threads_seen: list[threading.Thread] = []
    sentinel_module = object()
    modules_seen: list[Any] = []

    def fake_run_module_suite(ctx, module, name, doc_model=None):
        threads_seen.append(threading.current_thread())
        modules_seen.append(module)
        return (0, 0, [])

    fake_ctx = MagicMock()
    with (
        patch("plugin.framework.main_shared.tests_bundled", return_value=True),
        patch("plugin.framework.uno_context.get_ctx", return_value=fake_ctx),
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
        patch("plugin.chatbot.dialogs.msgbox"),
        patch("plugin.testing_runner.run_module_suite", side_effect=fake_run_module_suite),
        patch("importlib.import_module", return_value=sentinel_module) as mock_import,
    ):
        shared_mod._run_test_suite("plugin.tests.writer.test_format_uno", lambda _m: True, "writer.format_tests")

    assert len(threads_seen) == 1
    assert threads_seen[0] is threading.main_thread()
    assert modules_seen == [sentinel_module]
    mock_import.assert_called_once_with("plugin.tests.writer.test_format_uno")


def test_menu_text_translates_vision_ocr_and_debug(monkeypatch) -> None:
    """Menubar titles come from get_menu_text, not from a po msgstr alone.

    Vision OCR was missing from the static map (None → Addons.xcu en-US).
    Debug was a raw English literal, so the status event overwrote the catalog.
    """
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
    assert main_mod.get_menu_text("writer.insert_latex_dialog") == "Insert LaTeX Math..."


def test_run_test_suite_unavailable_does_not_print() -> None:
    """When tests are not bundled, _run_test_suite must not emit debug prints."""
    printed: list[str] = []
    with (
        patch("plugin.framework.main_shared.tests_bundled", return_value=False),
        patch("builtins.print", side_effect=lambda *args, **kw: printed.append(" ".join(map(str, args)))),
        patch("plugin.framework.main_shared._show_tests_unavailable"),
    ):
        shared_mod._run_test_suite("plugin.tests.writer.test_format_uno", lambda _m: True, "writer.format_tests")

    assert printed == []


def test_run_test_suite_unavailable_does_not_import() -> None:
    """When tests are not bundled, _run_test_suite must not import the test module."""
    with (
        patch("plugin.framework.main_shared.tests_bundled", return_value=False),
        patch("plugin.framework.main_shared._show_tests_unavailable") as mock_unavailable,
        patch("importlib.import_module", side_effect=ModuleNotFoundError("unbundled")) as mock_import,
    ):
        shared_mod._run_test_suite("plugin.tests.writer.test_format_uno", lambda _m: True, "writer.format_tests")

    mock_unavailable.assert_called_once_with("writer.format_tests")
    mock_import.assert_not_called()


def test_run_test_suite_logs_failure() -> None:
    """When run_module_suite raises, _run_test_suite logs the failure and shows a dialog."""
    fake_ctx = MagicMock()
    sentinel_module = object()
    with (
        patch("plugin.framework.main_shared.tests_bundled", return_value=True),
        patch("plugin.framework.uno_context.get_ctx", return_value=fake_ctx),
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
        patch("plugin.chatbot.dialogs.msgbox") as mock_msgbox,
        patch("plugin.testing_runner.run_module_suite", side_effect=RuntimeError("suite crash")),
        patch.object(shared_mod.log, "exception") as mock_log_exc,
        patch("importlib.import_module", return_value=sentinel_module),
    ):
        shared_mod._run_test_suite("plugin.tests.writer.test_format_uno", lambda _m: True, "writer.format_tests")
        mock_msgbox.assert_called_once()
        assert any("_run_test_suite failed" in str(call) for call in mock_log_exc.call_args_list)


def test_show_tests_unavailable_invokes_msgbox() -> None:
    """_show_tests_unavailable invokes msgbox with package info."""
    fake_ctx = MagicMock()
    with (
        patch("plugin.framework.uno_context.get_ctx", return_value=fake_ctx),
        patch("plugin.chatbot.dialogs.msgbox") as mock_msgbox,
    ):
        shared_mod._show_tests_unavailable("writer.format_tests")
        mock_msgbox.assert_called_once_with(
            fake_ctx, "writer.format_tests", "This WriterAgent build was packaged without the optional `plugin.tests` test modules."
        )


