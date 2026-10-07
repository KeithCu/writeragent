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


def test_numpy_load_allow_pickle_and_ctypeslib_rejected():
    pytest.importorskip("numpy")
    loaded = run_sandboxed_code(
        "import numpy as np\nresult = np.load('/no/such.npy', allow_pickle=True)"
    )
    assert loaded["status"] == "error"
    assert "allow_pickle" in loaded.get("message", "")
    ctypes_call = run_sandboxed_code(
        "import numpy as np\nresult = np.ctypeslib.load_library('nope', '.')"
    )
    assert ctypes_call["status"] == "error"
    assert "Forbidden call" in ctypes_call.get("message", "")


def test_pandas_pickle_and_module_setattr_rejected():
    pytest.importorskip("pandas")
    pickled = run_sandboxed_code("import pandas as pd\nresult = pd.read_pickle('/no/such.pkl')")
    assert pickled["status"] == "error"
    assert "Forbidden call" in pickled.get("message", "")
    mutated = run_sandboxed_code("import numpy as np\nsetattr(np, 'array', None)\nresult = 1")
    assert mutated["status"] == "error"
    assert "module or a type" in mutated.get("message", "")
    instance_ok = run_sandboxed_code("class Box:\n    pass\nbox = Box()\nbox.n = 3\nresult = box.n")
    assert instance_ok["status"] == "ok"
    assert instance_ok["result"] == 3


def test_shared_kernel_keeps_cell_override_of_init_name():
    """Re-seeding every cell used to state.update init names over cell rebinds."""
    init_sid = "calc:wb-override:init"
    cell_sid = "calc:wb-override"
    init_code = "FACTOR = 10"
    h = init_script_hash(init_code)
    first = run_sandboxed_code(
        "FACTOR = 99\nresult = FACTOR",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert first["status"] == "ok", first.get("message")
    assert first["result"] == 99
    second = run_sandboxed_code(
        "result = FACTOR",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert second["status"] == "ok", second.get("message")
    assert second["result"] == 99


def test_isolated_cell_reseeds_init_name_every_run():
    init_sid = "calc:wb-iso-override:init"
    init_code = "FACTOR = 10"
    h = init_script_hash(init_code)
    run_sandboxed_code(
        "FACTOR = 99\nresult = FACTOR",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    second = run_sandboxed_code(
        "result = FACTOR",
        session_id=None,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert second["status"] == "ok", second.get("message")
    assert second["result"] == 10


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


def test_isolated_init_mutation_does_not_leak():
    """A cell that mutates an init list must not change the next isolated cell."""
    init_sid = "calc:wb-iso-mut:init"
    init_code = "items = []"
    h = init_script_hash(init_code)
    kwargs = {"session_id": None, "init_script": init_code, "init_session_id": init_sid, "init_script_hash": h}
    first = run_sandboxed_code("items.append(1)\nresult = len(items)", **kwargs)
    assert first["status"] == "ok", first.get("message")
    assert first["result"] == 1
    second = run_sandboxed_code("items.append(1)\nresult = len(items)", **kwargs)
    assert second["status"] == "ok", second.get("message")
    assert second["result"] == 1


def test_reset_non_calc_session_drops_init_companion():
    from plugin.scripting.venv import venv_sandbox as vs

    init_sid = "online-wb:init"
    run_sandboxed_code(
        "result = MAGIC",
        session_id="online-wb",
        init_script="MAGIC = 1",
        init_session_id=init_sid,
        init_script_hash="h",
    )
    assert init_sid in vs._SESSION_EXECUTORS
    assert reset_sandbox_session("online-wb")["status"] == "ok"
    assert init_sid not in vs._SESSION_EXECUTORS
    assert "online-wb" not in vs._SESSION_EXECUTORS


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


def test_reset_clears_from_init_session_id():
    from plugin.scripting.venv import venv_sandbox as vs

    init_sid = "calc:wb-reset-init:init"
    cell_sid = "calc:wb-reset-init"
    init_code = "INIT_RUNS = 1\nMAGIC = 7"
    h = init_script_hash(init_code)
    run_sandboxed_code(
        "result = MAGIC",
        session_id=cell_sid,
        init_script=init_code,
        init_session_id=init_sid,
        init_script_hash=h,
    )
    assert init_sid in vs._SESSION_EXECUTORS
    assert cell_sid in vs._SESSION_EXECUTORS
    assert reset_sandbox_session(init_sid)["status"] == "ok"
    assert init_sid not in vs._SESSION_EXECUTORS
    assert cell_sid not in vs._SESSION_EXECUTORS
    assert init_sid not in vs._INIT_SCRIPT_HASH
    assert cell_sid not in vs._CELL_SESSION_INIT_DIGEST


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
    from plugin.scripting.session_manager import workbook_session_id

    # No caller doc: None without a desktop lookup or deadlock (#402)
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


def test_numpy_compiler_and_testing_prefixes_rejected():
    from plugin.contrib.smolagents.local_python_executor import (
        InterpreterError,
        _reject_numpy_code_exec,
    )

    def _fake(module: str, name: str):
        def fn(*_a, **_k):
            return None
        fn.__module__ = module
        fn.__name__ = name
        return fn

    for module, name in (
        ("numpy.f2py", "run_main"),
        ("numpy.f2py.f2py2e", "run_compile"),
        ("numpy.distutils", "core"),
        ("numpy.distutils.core", "setup"),
        ("numpy.testing", "assert_equal"),
    ):
        with pytest.raises(InterpreterError, match="Forbidden call"):
            _reject_numpy_code_exec(_fake(module, name), [], {})

    _reject_numpy_code_exec(_fake("numpy.linalg", "norm"), [], {})
    _reject_numpy_code_exec(_fake("numpy", "array"), [1], {})

