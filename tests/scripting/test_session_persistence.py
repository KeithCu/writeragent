# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared-kernel session persistence for =PYTHON() (harness / sandbox level)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.venv_sandbox import clear_all_sandbox_sessions, reset_sandbox_session
from plugin.scripting.worker_harness import _execute_request, _handle_request
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture(autouse=True)
def _clear_sessions():
    clear_all_sandbox_sessions()
    yield
    clear_all_sandbox_sessions()


def test_shared_session_persists_variables():
    sid = "calc:test-wb-1"
    r1 = _execute_request("x = 41\nresult = x + 1", None, session_id=sid)
    assert r1["status"] == "ok"
    assert r1["result"] == 42
    r2 = _execute_request("result = x + 1", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 42


def test_isolated_default_fresh_namespace():
    r1 = _execute_request("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    r2 = _execute_request("result = x + 1", None)
    assert r2["status"] == "error"


def test_cross_session_isolation():
    _execute_request("x = 10", None, session_id="calc:a")
    r = _execute_request("result = x", None, session_id="calc:b")
    assert r["status"] == "error"


def test_reset_session_clears_namespace():
    sid = "calc:reset-me"
    _execute_request("x = 1", None, session_id=sid)
    assert reset_sandbox_session(sid)["status"] == "ok"
    r = _execute_request("result = x", None, session_id=sid)
    assert r["status"] == "error"


def test_reset_sandbox_session_idempotent():
    sid = "calc:twice"
    assert reset_sandbox_session(sid)["status"] == "ok"
    assert reset_sandbox_session(sid)["status"] == "ok"


def test_handle_request_reset_session_action():
    sid = "calc:via-action"
    _execute_request("x = 99", None, session_id=sid)
    res = _handle_request({"action": "reset_session", "session_id": sid})
    assert res["status"] == "ok"
    r = _execute_request("result = x", None, session_id=sid)
    assert r["status"] == "error"


def test_run_code_in_user_venv_forwards_session_id():
    from plugin.scripting.venv_worker import run_code_in_user_venv

    ctx = MagicMock()
    with patch("plugin.scripting.venv_worker._worker_manager_for_ctx") as mock_mgr:
        manager = MagicMock()
        mock_mgr.return_value = (manager, None)
        manager.execute.return_value = {"status": "ok", "result": 1}
        run_code_in_user_venv(ctx, "result = 1", session_id="calc:wb1")
        manager.execute.assert_called_once()
        assert manager.execute.call_args.kwargs.get("session_id") == "calc:wb1"
