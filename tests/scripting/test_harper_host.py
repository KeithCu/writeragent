# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-side Harper entry (status UI pump must not abort lint)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.scripting import harper_host  # noqa: E402


def test_pump_grammar_status_ui_posts_not_blocking_execute() -> None:
    ctx = MagicMock()
    with (
        patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post,
        patch("plugin.framework.queue_executor.execute_on_main_thread") as mock_execute,
    ):
        harper_host._pump_grammar_status_ui(ctx)

    mock_post.assert_called_once()
    mock_execute.assert_not_called()


def test_pump_grammar_status_ui_swallows_post_errors() -> None:
    ctx = MagicMock()
    with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=RuntimeError("no AsyncCallback")):
        harper_host._pump_grammar_status_ui(ctx)  # must not raise


def test_run_harper_check_continues_when_pump_post_times_out() -> None:
    """Regression: status UI pump must not abort Harper when main-thread post fails."""
    ctx = MagicMock()

    def _fake_in_process(text, config_dir, *, bcp47="en-US", heartbeat_fn=None):
        if heartbeat_fn is not None:
            heartbeat_fn({"message": "Downloading harper-ls…"})
        return {"errors": [{"n_error_start": 0, "n_error_length": 4}]}

    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status"),
        patch(
            "plugin.framework.queue_executor.post_to_main_thread",
            side_effect=TimeoutError("Main-thread execution of _pump timed out after 2.0s"),
        ),
        patch("plugin.scripting.venv.harper.run_harper_check", side_effect=_fake_in_process) as mock_run,
    ):
        result = harper_host.run_harper_check(ctx, "They is here.", "/tmp/cfg", bcp47="en-US")

    assert result == {"errors": [{"n_error_start": 0, "n_error_length": 4}]}
    mock_run.assert_called_once()
