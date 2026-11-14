# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.menu_handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_reset_python_session_action_in_background() -> None:
    from plugin.menu_handlers import register_common_handlers

    ctx = MagicMock()
    registered = {}
    with (
        patch("plugin.framework.uno_context.get_ctx", return_value=ctx),
        patch("plugin.framework.worker_pool.run_in_background") as m_run,
        patch("plugin.scripting.session_manager.reset_workbook_python_session") as m_reset,
        patch(
            "plugin.menu_handlers.register_action_handler",
            side_effect=lambda mod, act, fn: registered.__setitem__((mod, act), fn),
        ),
    ):
        register_common_handlers()
        reset_func = registered[("scripting", "reset_python_session")]
        reset_func()

        m_run.assert_called_once()
        args, kwargs = m_run.call_args
        assert kwargs["name"] == "reset-python-session"
        assert callable(args[0])
        args[0]()
        m_reset.assert_called_once_with(ctx)
