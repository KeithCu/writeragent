# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc workbook initialization scripts (persistence + sandbox execution)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.document_scripts import (
    build_python_eval_init_kwargs,
    get_calc_init_script,
    init_script_hash,
    set_calc_init_script,
)
from plugin.scripting.venv.venv_sandbox import clear_all_sandbox_sessions, reset_sandbox_session, run_sandboxed_code
from tests.writer.test_document_helpers import _DocWithUserDefinedProperties, _UserDefinedProperties


@pytest.fixture(autouse=True)
def _clear_sessions():
    clear_all_sandbox_sessions()
    yield
    clear_all_sandbox_sessions()


def test_get_set_calc_init_script_roundtrip():
    from plugin.scripting.document_scripts import DOCUMENT_SCRIPTS_UDPROP, set_document_scripts
    import json
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    assert get_calc_init_script(doc) == ""
    assert set_calc_init_script(doc, "import numpy as np") is None
    stored = props.getPropertyValue(DOCUMENT_SCRIPTS_UDPROP)
    assert json.loads(stored)["scripts"]["INIT"] == "import numpy as np"
    assert get_calc_init_script(doc) == "import numpy as np"

    # Test "Init" fallback case
    set_document_scripts(doc, {"Init": "import pandas as pd"})
    assert get_calc_init_script(doc) == "import pandas as pd"
    assert set_calc_init_script(doc, "import os") is None
    stored = props.getPropertyValue(DOCUMENT_SCRIPTS_UDPROP)
    assert json.loads(stored)["scripts"]["Init"] == "import os"
    assert "INIT" not in json.loads(stored)["scripts"]


def test_init_script_runs_once_in_isolated_mode():
    """Init runs in :init session once; isolated cells see bindings without re-running expensive init."""
    from plugin.scripting.venv import venv_sandbox as vs

    init_sid = "calc:wb-isolated-test:init"
    init_code = "INIT_RUNS = 1\nHELPER = 41"
    h = init_script_hash(init_code)

    with patch.object(vs, "_run_on_executor", wraps=vs._run_on_executor) as mock_run:
        r1 = run_sandboxed_code(
            "result = HELPER + 1",
            session_id=None,
            init_script=init_code,
            init_session_id=init_sid,
            init_script_hash=h,
        )
        assert r1["status"] == "ok", r1.get("message")
        assert r1["result"] == 42

        r2 = run_sandboxed_code(
            "result = HELPER + 1",
            session_id=None,
            init_script=init_code,
            init_session_id=init_sid,
            init_script_hash=h,
        )
        assert r2["status"] == "ok"
        assert r2["result"] == 42

        # One init execution + two cell executions.
        assert mock_run.call_count == 3

    init_exec = vs._SESSION_EXECUTORS[init_sid]
    assert init_exec.state.get("INIT_RUNS") == 1


def test_init_visible_in_shared_kernel():
    init_sid = "calc:wb-shared:init"
    cell_sid = "calc:wb-shared"
    init_code = "BASE = 10"
    h = init_script_hash(init_code)
    run_sandboxed_code(
        "result = BASE + 1",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    r = run_sandboxed_code(
        "result = BASE + 5",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r["status"] == "ok"
    assert r["result"] == 15


def test_isolated_cells_do_not_share_cell_assignments():
    init_sid = "calc:wb-iso-vars:init"
    init_code = "BASE = 0"
    h = init_script_hash(init_code)
    run_sandboxed_code(
        "x = 7\nresult = x",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    r = run_sandboxed_code(
        "result = x",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r["status"] == "error"


def test_reset_clears_init_session():
    from plugin.scripting.venv import venv_sandbox as vs

    init_sid = "calc:wb-reset:init"
    cell_sid = "calc:wb-reset"
    init_code = "INIT_RUNS = 1\nMAGIC = 3"
    h = init_script_hash(init_code)
    run_sandboxed_code(
        "result = MAGIC",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert init_sid in vs._SESSION_EXECUTORS
    assert reset_sandbox_session(cell_sid)["status"] == "ok"
    assert init_sid not in vs._SESSION_EXECUTORS
    run_sandboxed_code(
        "result = MAGIC",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert vs._SESSION_EXECUTORS[init_sid].state.get("INIT_RUNS") == 1


def test_build_python_eval_init_kwargs():
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    set_calc_init_script(doc, "import pandas as pd")
    with patch("plugin.scripting.session_manager._workbook_session_key", return_value="doc-1"):
        kw = build_python_eval_init_kwargs(doc)
    assert kw["init_script"] == "import pandas as pd"
    assert kw["init_session_id"] == "calc:doc-1:init"
    assert kw["init_script_hash"]


def test_run_code_forwards_init_kwargs():
    from plugin.scripting.venv_worker import run_code_in_user_venv

    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    set_calc_init_script(doc, "x = 1")
    with (
        patch("plugin.scripting.venv_worker._worker_manager_for_ctx") as mock_mgr,
        patch("plugin.scripting.session_manager._workbook_session_key", return_value="k"),
    ):
        manager = MagicMock()
        mock_mgr.return_value = (manager, None)
        manager.execute.return_value = {"status": "ok", "result": 1}
        run_code_in_user_venv(ctx, "result = 1", init_script="x = 1", init_session_id="calc:k:init", init_script_hash="abc")
        assert manager.execute.call_args.kwargs.get("init_script") == "x = 1"


def test_workbook_session_id_recursion_off_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting.session_manager import clear_active_calc_session, workbook_session_id

    # Reset any cached session from prior test cases
    clear_active_calc_session()

    # Off-main threads must return None without blocking or deadlocking (#402)
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr("plugin.scripting.session_manager.python_session_mode", lambda ctx: "shared")

    doc = MagicMock()
    monkeypatch.setattr("plugin.scripting.session_manager._calc_document", lambda ctx: doc)
    monkeypatch.setattr("plugin.scripting.session_manager.calc_workbook_base_session_id", lambda d: "test-wb-1")

    ctx = MagicMock()
    res = workbook_session_id(ctx)
    assert res is None


def test_init_helper_function_in_shared_kernel():
    """Functions defined via `def` in INIT scripts must be callable in shared kernel cells."""
    init_sid = "calc:wb-func-shared:init"
    cell_sid = "calc:wb-func-shared"
    init_code = "def double(x):\n    return x * 2\nFACTOR = 10"
    h = init_script_hash(init_code)

    r1 = run_sandboxed_code(
        "result = double(21)",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r1["status"] == "ok", r1.get("message")
    assert r1["result"] == 42

    r2 = run_sandboxed_code(
        "result = 3 * FACTOR",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r2["status"] == "ok", r2.get("message")
    assert r2["result"] == 30


def test_init_helper_function_in_isolated_mode():
    """Functions defined via `def` in INIT scripts must be callable in isolated cells."""
    init_sid = "calc:wb-func-iso:init"
    init_code = "def double(x):\n    return x * 2\nFACTOR = 10"
    h = init_script_hash(init_code)

    r1 = run_sandboxed_code(
        "result = double(21)",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r1["status"] == "ok", r1.get("message")
    assert r1["result"] == 42


def test_reset_re_seeds_init_helper_function():
    """Resetting the session drops cell overrides but re-seeds init helper functions."""
    init_sid = "calc:wb-func-reset:init"
    cell_sid = "calc:wb-func-reset"
    init_code = "def double(x):\n    return x * 2\nFACTOR = 10"
    h = init_script_hash(init_code)

    r1 = run_sandboxed_code(
        "result = double(21)",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r1["status"] == "ok", r1.get("message")
    assert r1["result"] == 42

    reset_sandbox_session(cell_sid)

    r2 = run_sandboxed_code(
        "result = double(3)",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r2["status"] == "ok", r2.get("message")
    assert r2["result"] == 6


def test_init_helper_chained_and_imports():
    """Init helpers calling other helpers and utilizing imports work properly."""
    init_sid = "calc:wb-func-chain:init"
    cell_sid = "calc:wb-func-chain"
    init_code = "import math\n\ndef add(a, b):\n    return a + b\n\ndef circle_area(r):\n    return math.pi * r * r\n\ndef double_add(x):\n    return add(x, x)"
    h = init_script_hash(init_code)

    r1 = run_sandboxed_code(
        "result = double_add(7)",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r1["status"] == "ok", r1.get("message")
    assert r1["result"] == 14

    r2 = run_sandboxed_code(
        "result = round(circle_area(2), 2)",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert r2["status"] == "ok", r2.get("message")
    assert r2["result"] == 12.57

