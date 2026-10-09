# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared-kernel session persistence for =PYTHON() (harness / sandbox level)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.venv.venv_sandbox import clear_all_sandbox_sessions, reset_sandbox_session, run_sandboxed_code
from plugin.scripting.venv.worker_harness import _handle_request


@pytest.fixture(autouse=True)
def _clear_sessions():
    clear_all_sandbox_sessions()
    yield
    clear_all_sandbox_sessions()


def test_shared_session_persists_variables():
    sid = "calc:test-wb-1"
    r1 = run_sandboxed_code("x = 41\nresult = x + 1", None, session_id=sid)
    assert r1["status"] == "ok"
    assert r1["result"] == 42
    r2 = run_sandboxed_code("result = x + 1", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 42


def test_shared_kernel_persists_across_simulated_recalc():
    """Shared kernel keeps globals across separate execute calls (no reset between recalcs)."""
    sid = "calc:recalc-sim"
    r1 = run_sandboxed_code("counter = 0\ncounter += 1\nresult = counter", None, session_id=sid)
    assert r1["status"] == "ok"
    assert r1["result"] == 1
    # Second invocation simulates another recalc pass without reset_python_session.
    r2 = run_sandboxed_code("counter += 1\nresult = counter", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 2
    r3 = run_sandboxed_code("result = counter", None, session_id=sid)
    assert r3["status"] == "ok"
    assert r3["result"] == 2


def test_isolated_default_fresh_namespace():
    r1 = run_sandboxed_code("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    r2 = run_sandboxed_code("result = x + 1", None)
    assert r2["status"] == "error"


def test_cross_session_isolation():
    run_sandboxed_code("x = 10", None, session_id="calc:a")
    r = run_sandboxed_code("result = x", None, session_id="calc:b")
    assert r["status"] == "error"


def test_reset_session_clears_namespace():
    sid = "calc:reset-me"
    run_sandboxed_code("x = 1", None, session_id=sid)
    assert reset_sandbox_session(sid)["status"] == "ok"
    r = run_sandboxed_code("result = x", None, session_id=sid)
    assert r["status"] == "error"


def test_reset_sandbox_session_idempotent():
    sid = "calc:twice"
    assert reset_sandbox_session(sid)["status"] == "ok"
    assert reset_sandbox_session(sid)["status"] == "ok"


def test_handle_request_reset_session_action():
    sid = "calc:via-action"
    run_sandboxed_code("x = 99", None, session_id=sid)
    res = _handle_request({"action": "reset_session", "session_id": sid})
    assert res["status"] == "ok"
    r = run_sandboxed_code("result = x", None, session_id=sid)
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


def test_shared_session_result_does_not_hijack_subsequent_last_expression_cells():
    """Issue #388: result = ... in one cell must not hijack later cells relying on last-expression."""
    sid = "calc:test-issue-388"
    # Cell 1: B1
    r1 = run_sandboxed_code("x = 10", None, session_id=sid)
    assert r1["status"] == "ok"
    assert r1["result"] == 10

    # Cell 2: D1
    r2 = run_sandboxed_code("x + 1", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] == 11

    # Cell 3: D8 (KPI cell assigning explicit result)
    r3 = run_sandboxed_code("result = 3900.5", None, session_id=sid)
    assert r3["status"] == "ok"
    assert r3["result"] == 3900.5

    # Re-evaluate Cell 1 (B1): must still return 10, not 3900.5
    r4 = run_sandboxed_code("x = 10", None, session_id=sid)
    assert r4["status"] == "ok"
    assert r4["result"] == 10

    # Re-evaluate Cell 2 (D1): must still return 11, not 3900.5
    r5 = run_sandboxed_code("x + 1", None, session_id=sid)
    assert r5["status"] == "ok"
    assert r5["result"] == 11

    # Later cell may still *use* result as a shared-kernel variable.
    r6 = run_sandboxed_code("result * 2", None, session_id=sid)
    assert r6["status"] == "ok"
    assert r6["result"] == 7801.0


def test_shared_session_failed_cell_does_not_poison_result():
    """A cell failing execution must not leave leftover result in state for next cell."""
    sid = "calc:test-error-path"
    # Cell assigning result
    r1 = run_sandboxed_code("result = 500", None, session_id=sid)
    assert r1["result"] == 500

    # Failed cell
    r2 = run_sandboxed_code("1 / 0", None, session_id=sid)
    assert r2["status"] == "error"

    # Next cell relying on last-expression: must not see 500
    r3 = run_sandboxed_code("y = 77", None, session_id=sid)
    assert r3["status"] == "ok"
    assert r3["result"] == 77

    # Failed assignment in this cell must not stick; last successful result remains usable.
    r4 = run_sandboxed_code("result = 1 / 0", None, session_id=sid)
    assert r4["status"] == "error"
    r5 = run_sandboxed_code("result * 2", None, session_id=sid)
    assert r5["status"] == "ok"
    assert r5["result"] == 1000


def test_shared_session_data_and_ranges_isolation():
    """data and ranges must be reset when a cell does not pass data arguments."""
    sid = "calc:test-data-isolation"
    # Cell 1 passes data
    r1 = run_sandboxed_code("result = len(ranges)", [[1.0, 2.0], [3.0, 4.0]], session_id=sid)
    assert r1["status"] == "ok"
    assert r1["result"] == 1

    # Cell 2 passes no data: data must be None and ranges must be empty list
    r2 = run_sandboxed_code("data is None and ranges == []", None, session_id=sid)
    assert r2["status"] == "ok"
    assert r2["result"] is True


def test_rebind_result_to_same_singleton_survives_trailing_statement():
    """A trailing statement must not hide ``result = <singleton>``.

    What was wrong: egress treated ``current is not prior_result`` as a rebind.
    Small ints, None, True, and interned strings are singletons, and
    ``result += [2]`` stores the same list. ``pass`` then returned None.
    """
    sid = "calc:test-result-singleton"
    assert run_sandboxed_code("result = 5", None, session_id=sid)["result"] == 5
    again = run_sandboxed_code("result = 5\npass", None, session_id=sid)
    assert again["status"] == "ok", again
    assert again["result"] == 5
    trailed = run_sandboxed_code("x = 1\nresult = 5\npass", None, session_id=sid)
    assert trailed["result"] == 5
    assert run_sandboxed_code("result = None\npass", None, session_id=sid)["result"] is None
    assert run_sandboxed_code("result = True\npass", None, session_id=sid)["result"] is True
    assert run_sandboxed_code("result = 'a'\npass", None, session_id=sid)["result"] == "a"
    assert run_sandboxed_code("result = [1]", None, session_id=sid)["result"] == [1]
    extended = run_sandboxed_code("result += [2]\npass", None, session_id=sid)
    assert extended["status"] == "ok", extended
    assert extended["result"] == [1, 2]
    # A store inside a function does not write the module ``result``.
    nested = run_sandboxed_code("def f():\n    result = 9\npass", None, session_id=sid)
    assert nested["status"] == "ok", nested
    assert nested["result"] is None
    comp = run_sandboxed_code("[result for result in [9]]\npass", None, session_id=sid)
    assert comp["status"] == "ok", comp
    assert comp["result"] is None
    # Last-expression cells still must not see the leftover list.
    later = run_sandboxed_code("1 + 1", None, session_id=sid)
    assert later["result"] == 2


def test_module_binds_name_ignores_nested_scopes():
    from plugin.scripting.venv.venv_sandbox import _module_binds_name

    assert _module_binds_name("result = 5\npass", "result")
    assert _module_binds_name("result += [2]", "result")
    assert _module_binds_name("for result in [5]:\n    pass", "result")
    assert not _module_binds_name("def f():\n    result = 9\npass", "result")
    assert not _module_binds_name("class C:\n    result = 5", "result")
    assert not _module_binds_name("result: int\npass", "result")
    assert _module_binds_name("result: int = 5", "result")
    assert not _module_binds_name("[result for result in [9]]", "result")
    assert not _module_binds_name("[(result := x) for x in [1]]", "result")
    # The iter runs on the module state. The executor rejects := today;
    # the visitor still has to see it.
    assert _module_binds_name("[x for x in (result := 5)]", "result")


def test_shared_session_multiple_explicit_result_assignments():
    """Explicit result assignments in sequence each return their own value."""
    sid = "calc:test-multi-result"
    r1 = run_sandboxed_code("result = 42", None, session_id=sid)
    assert r1["result"] == 42
    r2 = run_sandboxed_code("result = 99", None, session_id=sid)
    assert r2["result"] == 99
    r3 = run_sandboxed_code("z = 123", None, session_id=sid)
    assert r3["result"] == 123

