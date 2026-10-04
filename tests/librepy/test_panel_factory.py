# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy sidebar factory lifecycle: main-thread hop, dispose, failed create."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _element():
    from plugin.librepy.panel_factory import PythonPanelElement

    el = object.__new__(PythonPanelElement)
    el.ctx = MagicMock(name="element-ctx")
    el.xFrame = MagicMock(name="frame")
    el.xParentWindow = MagicMock(name="parent")
    el.xParentWindow.getPosSize.return_value = SimpleNamespace(Width=220, Height=400)
    el.ResourceURL = "private:resource/toolpanel/PythonPanel"
    el.toolpanel = None
    el.m_panelRootWindow = None
    el.controller = None
    return el


def _root_window():
    root = MagicMock(name="root")
    root.getPosSize.return_value = SimpleNamespace(Width=220, Height=400)
    return root


def test_run_on_main_thread_inline_on_vcl():
    from plugin.librepy.panel_factory import _run_on_main_thread

    with patch("plugin.framework.thread_guard.on_main_thread", return_value=True), patch(
        "plugin.framework.queue_executor.execute_on_main_thread"
    ) as exe:
        assert _run_on_main_thread(lambda: 42) == 42
    exe.assert_not_called()


def test_run_on_main_thread_marshals_off_vcl():
    from plugin.librepy.panel_factory import _run_on_main_thread

    with patch("plugin.framework.thread_guard.on_main_thread", return_value=False), patch(
        "plugin.framework.queue_executor.execute_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ) as exe:
        assert _run_on_main_thread(lambda: 42) == 42
    exe.assert_called_once()


def test_get_real_interface_create_goes_through_main_thread_hop():
    """getRealInterface must hop create so Dummy-N never calls get_extension_url."""
    from plugin.librepy.panel_factory import PythonToolPanel

    el = _element()
    panel = PythonToolPanel(_root_window(), el.xParentWindow, el.ctx)

    def fake_hop(fn, *args, **kwargs):
        el.toolpanel = panel
        return None

    with patch("plugin.librepy.panel_factory._run_on_main_thread", side_effect=fake_hop), patch(
        "plugin.librepy.panel_factory.get_extension_url"
    ) as url:
        result = el.getRealInterface()
    url.assert_not_called()
    assert result is panel


def test_get_real_interface_create_runs_on_hop():
    from plugin.librepy.panel_factory import PythonToolPanel

    el = _element()
    root = _root_window()
    controller = MagicMock(name="controller")
    controller.resize_listener = MagicMock(name="resize")

    with patch(
        "plugin.librepy.panel_factory._run_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ) as hop, patch("plugin.librepy.panel_factory._ensure_paths") as ensure, patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", return_value=controller
    ):
        result = el.getRealInterface()

    hop.assert_called_once()
    ensure.assert_called_once_with(el.ctx)
    assert isinstance(result, PythonToolPanel)
    assert result is el.toolpanel
    assert result.Window is root
    assert result.resize_listener is controller.resize_listener
    assert el.controller is controller


def test_get_real_interface_skips_hop_when_panel_exists():
    el = _element()
    existing = MagicMock(name="existing")
    el.toolpanel = existing
    with patch("plugin.librepy.panel_factory._run_on_main_thread") as hop:
        result = el.getRealInterface()
    hop.assert_not_called()
    assert result is existing


def test_get_real_interface_clears_toolpanel_and_retries():
    from plugin.framework.errors import UnoObjectError

    el = _element()
    root = _root_window()
    calls = {"n": 0}

    def factory(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("wire failed")
        ctrl = MagicMock(name="controller")
        ctrl.resize_listener = MagicMock(name="resize")
        return ctrl

    with patch(
        "plugin.librepy.panel_factory._run_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.librepy.panel_factory._ensure_paths"), patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", side_effect=factory
    ):
        with pytest.raises(UnoObjectError):
            el.getRealInterface()
        assert el.toolpanel is None
        assert el.controller is None
        result = el.getRealInterface()

    assert calls["n"] == 2
    assert result is el.toolpanel
    assert el.controller is not None


def test_get_real_interface_disposes_controller_when_create_fails_late():
    from plugin.framework.errors import UnoObjectError

    el = _element()
    root = _root_window()
    root.getPosSize.side_effect = RuntimeError("pos")
    controller = MagicMock(name="controller")

    with patch(
        "plugin.librepy.panel_factory._run_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.librepy.panel_factory._ensure_paths"), patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", return_value=controller
    ):
        with pytest.raises(UnoObjectError):
            el.getRealInterface()

    controller.disposing.assert_called_once()
    assert el.toolpanel is None
    assert el.controller is None


def test_null_create_container_window_raises_and_retries():
    from plugin.framework.errors import UnoObjectError

    el = _element()
    window = _root_window()
    provider = MagicMock(name="provider")
    provider.createContainerWindow.side_effect = [None, window]
    el.ctx.getServiceManager.return_value.createInstanceWithContext.return_value = provider
    controller = MagicMock(name="controller")
    controller.resize_listener = None

    with patch(
        "plugin.librepy.panel_factory._run_on_main_thread",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.librepy.panel_factory._ensure_paths"), patch(
        "plugin.librepy.panel_factory.get_extension_url", return_value="file:///ext"
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", return_value=controller
    ):
        with pytest.raises(UnoObjectError) as raised:
            el.getRealInterface()
        assert el.toolpanel is None
        assert raised.value.__cause__ is not None
        assert "no window" in raised.value.__cause__.message
        result = el.getRealInterface()

    assert result is el.toolpanel
    assert result.Window is window
    assert provider.createContainerWindow.call_count == 2


def test_element_disposing_runs_controller_and_clears_it():
    el = _element()
    controller = MagicMock(name="controller")
    el.controller = controller
    el.disposing(None)
    controller.disposing.assert_called_once()
    assert el.controller is None

    el.controller = controller
    controller.disposing.side_effect = RuntimeError("already dead")
    el.disposing(None)
    assert el.controller is None
