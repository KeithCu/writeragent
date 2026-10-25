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
    el.controller = None
    return el


def _root_window():
    root = MagicMock(name="root")
    root.getPosSize.return_value = SimpleNamespace(Width=220, Height=400)
    return root


def test_get_real_interface_create_goes_through_main_thread_hop():
    """getRealInterface must hop create so Dummy-N never calls get_extension_url."""
    from plugin.librepy.panel_factory import PythonToolPanel

    el = _element()
    panel = PythonToolPanel(_root_window(), el.xParentWindow)

    def fake_hop(fn, *args, **kwargs):
        el.toolpanel = panel
        return None

    with patch("plugin.librepy.panel_factory.run_on_main", side_effect=fake_hop), patch(
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
        "plugin.librepy.panel_factory.run_on_main",
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
    with patch("plugin.librepy.panel_factory.run_on_main") as hop:
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
        "plugin.librepy.panel_factory.run_on_main",
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
    from typing import Any
    from plugin.framework.errors import UnoObjectError

    el = _element()
    root = _root_window()
    root.getPosSize.return_value = MagicMock(Width=200, Height=400)
    controller = MagicMock(name="controller")

    class _FailingPanel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        @property
        def resize_listener(self) -> Any:
            return None

        @resize_listener.setter
        def resize_listener(self, val: Any) -> None:
            raise RuntimeError("assign resize_listener failed")

    with patch(
        "plugin.librepy.panel_factory.run_on_main",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.librepy.panel_factory._ensure_paths"), patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.librepy.panel_factory.PythonToolPanel", _FailingPanel
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", return_value=controller
    ):
        with pytest.raises(UnoObjectError):
            el.getRealInterface()

    controller.disposing.assert_called_once()
    assert el.toolpanel is None
    assert el.controller is None


def test_get_real_interface_second_create_panel_is_noop():
    el = _element()
    root = _root_window()
    root.getPosSize.return_value = MagicMock(Width=200, Height=400)
    controller = MagicMock(name="controller")

    with patch(
        "plugin.librepy.panel_factory.run_on_main",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.librepy.panel_factory._ensure_paths"), patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", return_value=controller
    ):
        panel1 = el.getRealInterface()
        assert panel1 is not None
        assert el.controller is controller

        # Second create call should be a no-op
        panel2 = el.getRealInterface()
        assert panel2 is panel1
        controller.disposing.assert_not_called()


def test_get_or_create_root_window_raises_when_ctx_is_none():
    from plugin.framework.errors import UnoObjectError

    el = _element()
    el.ctx = None
    with patch("plugin.framework.uno_context.get_ctx") as mock_get_ctx, pytest.raises(UnoObjectError):
        el._getOrCreatePanelRootWindow()
    mock_get_ctx.assert_not_called()


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
        "plugin.librepy.panel_factory.run_on_main",
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
        assert "no window" in str(raised.value.__cause__)
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


def test_window_on_disposing_nulls_toolpanel_and_rebuilds():
    el = _element()
    root = _root_window()
    root.getPosSize.return_value = MagicMock(Width=200, Height=400)
    controller1 = MagicMock(name="controller1")
    controller2 = MagicMock(name="controller2")

    with patch(
        "plugin.librepy.panel_factory.run_on_main",
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    ), patch("plugin.librepy.panel_factory._ensure_paths"), patch.object(
        el, "_getOrCreatePanelRootWindow", return_value=root
    ), patch(
        "plugin.librepy.python_sidebar.PythonSidebarController", side_effect=[controller1, controller2]
    ) as mock_ctrl_cls:
        panel1 = el.getRealInterface()
        assert el.toolpanel is not None
        assert el.controller is controller1

        # Simulate on_torn_down callback being invoked from window dispose
        on_torn_down = mock_ctrl_cls.call_args.kwargs["on_torn_down"]
        on_torn_down()
        assert el.toolpanel is None
        assert el.controller is None

        # Next call to getRealInterface builds a new panel
        panel2 = el.getRealInterface()
        assert el.toolpanel is not None
        assert el.controller is controller2
        assert panel2 is not panel1
