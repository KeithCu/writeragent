# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-provided sandbox bindings (e.g. selected image bytes)."""

from __future__ import annotations

import io
import pathlib
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast
from unittest.mock import patch

import pytest

from plugin.framework.ipc import read_pickle_frame, write_pickle_frame
from plugin.scripting.payload_codec import host_unpack_data
from plugin.scripting.sandbox import build_sandbox_policy
from plugin.scripting.venv import venv_sandbox as vs
from plugin.scripting.venv.venv_sandbox import (
    reset_sandbox_session,
    serialize_result,
)
from tests.scripting.sandbox_support import sandbox_run


@pytest.fixture(autouse=True)
def _reset_abandoned_timeout() -> Any:
    # Tip: ensure abandoned_timeout does not leak across sandbox tests.
    try:
        import plugin.contrib.smolagents.local_python_executor as lpe

        lpe.abandoned_timeout = False
        lpe.discard_thread_pool_timeout()
    except ImportError:
        pass
    try:
        yield
    finally:
        try:
            import plugin.contrib.smolagents.local_python_executor as lpe

            lpe.abandoned_timeout = False
            lpe.discard_thread_pool_timeout()
        except ImportError:
            pass


@pytest.mark.parametrize(
    ("code", "label"),
    [
        ("raise SystemExit(3)", "SystemExit"),
        ("raise KeyboardInterrupt", "KeyboardInterrupt"),
    ],
)
def test_user_base_exception_does_not_drop_the_shared_session(code: str, label: str):
    """SystemExit and KeyboardInterrupt must stay in the cell.

    SystemExit and KeyboardInterrupt are BaseException, not Exception.
    The sandbox must treat them like other user aborts: return an error
    frame, keep the worker alive, and preserve the shared session.
    """
    sid = f"calc:base-exception-{label}"
    try:
        first = sandbox_run("result = 7", session_id=sid, timeout_sec=8)
        assert first["status"] == "ok", first
        assert first["result"] == 7
        blown = sandbox_run(code, session_id=sid, timeout_sec=8)
        assert blown["status"] == "error", blown
        assert label in blown.get("traceback", "")
        assert blown.get("code") != "USER_STOPPED"
        again = sandbox_run("result = result + 1", session_id=sid, timeout_sec=8)
        assert again["status"] == "ok", again
        assert again["result"] == 8
    finally:
        reset_sandbox_session(sid)


def test_zero_dim_temporal_ndarray_matches_scalar():
    """0-d datetime64/timedelta64 must keep the dtype, not arr.item()'s raw int."""
    np = pytest.importorskip("numpy")

    raw_dt = np.array("2020-01-02T03:04:05", dtype="datetime64[ns]")
    scalar_dt = np.datetime64("2020-01-02T03:04:05", "ns")
    wire_dt = serialize_result(raw_dt)
    assert wire_dt == serialize_result(scalar_dt)
    assert isinstance(wire_dt, str)
    assert "2020-01-02" in wire_dt
    assert wire_dt != 1577934245000000000

    raw_ns = np.array(90, dtype="timedelta64[ns]")
    wire_ns = serialize_result(raw_ns)
    assert wire_ns == serialize_result(np.timedelta64(90, "ns"))
    assert wire_ns != 90
    assert wire_ns == 0.0

    raw_day = np.array(1, dtype="timedelta64[D]")
    assert serialize_result(raw_day) == serialize_result(np.timedelta64(1, "D"))
    assert serialize_result(raw_day) == 1.0


def test_user_stopped_ends_the_cell_even_if_the_script_catches_exception():
    """Stop during a wa.* call must not be a RuntimeError the script can swallow."""
    from plugin.framework.ipc import UserStopped

    def boom() -> None:
        raise UserStopped("Stopped by user.")

    code = "try:\n    boom()\n    result = 'kept going'\nexcept Exception:\n    result = 'caught'\n"
    out = sandbox_run(code, bindings={"boom": boom}, timeout_sec=5)
    assert out["status"] == "error"
    assert out["code"] == "USER_STOPPED"
    assert "Stopped by user" in out["message"]
    assert out.get("result") != "caught"


def _refuse_sigalrm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force local_python_executor.timeout onto its ThreadPoolExecutor path."""
    import signal

    real_signal = signal.signal

    def _signal(signum, handler):
        if signum == signal.SIGALRM:
            raise ValueError("SIGALRM unavailable")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", _signal)


def test_timeout_fallback_keeps_sandbox_session_context(monkeypatch: pytest.MonkeyPatch):
    """Windows / non-main timeout thread must still see the workbook session."""
    from plugin.scripting.venv.venv_sandbox import current_sandbox_session_id, sandbox_execute_active

    _refuse_sigalrm(monkeypatch)
    seen: dict[str, object] = {}

    def probe() -> int:
        seen["sid"] = current_sandbox_session_id()
        seen["active"] = sandbox_execute_active()
        return 1

    isolated = sandbox_run("result = probe()", bindings={"probe": probe}, timeout_sec=8)
    assert isolated["status"] == "ok", isolated
    assert seen["sid"] is None
    assert seen["active"] is True

    sid = "calc:file:///timeout-fallback-workbook"
    try:
        shared = sandbox_run(
            "result = probe()",
            bindings={"probe": probe},
            session_id=sid,
            timeout_sec=8,
        )
        assert shared["status"] == "ok", shared
        assert seen["sid"] == sid
        assert seen["active"] is True
    finally:
        reset_sandbox_session(sid)


def test_run_sandboxed_code_injects_bindings():
    code = "result = image"
    out = sandbox_run(code, bindings={"image": b"png-bytes"})
    assert out["status"] == "ok"
    assert out["result"] == b"png-bytes"


def test_scalar_dates_and_numbers_round_trip_host_unpickle():
    cases = [
        (date(2026, 8, 13), "2026-08-13"),
        (datetime(2026, 8, 13, 14, 30), "2026-08-13T14:30:00"),
        (time(14, 30), "14:30:00"),
        (timedelta(days=1, hours=12), 1.5),
        (Decimal("1.25"), 1.25),
        (Fraction(1, 4), 0.25),
        (range(3), [0, 1, 2]),
        ([date(2026, 1, 1), date(2026, 1, 2)], ["2026-01-01", "2026-01-02"]),
    ]
    for value, expected in cases:
        wire = serialize_result(value)
        buf = io.BytesIO()
        write_pickle_frame(buf, {"status": "ok", "result": wire})
        buf.seek(0)
        unpacked = read_pickle_frame(buf, require_dict=True)
        assert unpacked is not None
        assert host_unpack_data(unpacked["result"]) == expected


def test_pandas_timestamp_round_trips_as_iso():
    pd = pytest.importorskip("pandas")
    wire = serialize_result(pd.Timestamp("2026-08-13 14:30"))
    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "result": wire})
    buf.seek(0)
    unpacked = read_pickle_frame(buf, require_dict=True)
    assert unpacked is not None
    assert host_unpack_data(unpacked["result"]) == "2026-08-13T14:30:00"


def test_serialize_nested_dataframe_and_figure():
    """Nested containers must take the custom serialize path, not child_pack."""
    pd = pytest.importorskip("pandas")
    matplotlib = pytest.importorskip("matplotlib")
    # pyplot's default backend is Qt here and aborts without a display.
    matplotlib.use("Agg", force=True)
    from matplotlib.figure import Figure

    from plugin.scripting.payload_codec import is_dataframe_payload

    df1 = pd.DataFrame({"a": [1]})
    df2 = pd.DataFrame({"b": [2]})
    sheets = serialize_result({"sheets": [df1, df2]})
    assert is_dataframe_payload(sheets["sheets"][0])
    assert is_dataframe_payload(sheets["sheets"][1])
    assert sheets["sheets"][0]["columns"] == ["a"]

    fig = Figure()
    fig.add_subplot(111).plot([1, 2])
    wrapped = serialize_result([{"stats": df1, "plot": fig}])
    assert is_dataframe_payload(wrapped[0]["stats"])
    assert wrapped[0]["plot"]["__wa_payload__"] == "image"


def test_cell_scoped_dir_does_not_leak_into_unbound_execute(tmp_path, monkeypatch):
    """A shared executor must not treat a previous cell's scoped_dir as the host folder."""
    seen: list[str | None] = []

    def _fake_query_folder_df(sql, files=None, scoped_dir=None, **kwargs):
        del sql, files, kwargs
        seen.append(scoped_dir)
        return 1

    monkeypatch.setattr("plugin.scripting.venv.duckdb_sql.query_folder_df", _fake_query_folder_df)
    sid = "calc:scoped-dir-leak"
    host = str(tmp_path)
    try:
        first = sandbox_run(
            "scoped_dir = '/tmp/not-the-host'\nresult = query_folder_df('select 1', files=['a.csv'])",
            bindings={"scoped_dir": host},
            session_id=sid,
            timeout_sec=10,
        )
        assert first["status"] == "ok", first
        assert seen == [host]

        second = sandbox_run(
            "result = query_folder_df('select 1', files=['a.csv'])",
            session_id=sid,
            timeout_sec=10,
        )
        assert second["status"] == "ok", second
        assert seen == [host, None]

        leaked = sandbox_run("result = scoped_dir", session_id=sid, timeout_sec=10)
        assert leaked["status"] == "error", leaked

        other = str(tmp_path / "other")
        rebound = sandbox_run(
            "scoped_dir = '/tmp/still-not-host'\nresult = query_folder_df('select 1', files=['a.csv'])",
            bindings={"scoped_dir": other},
            session_id=sid,
            timeout_sec=10,
        )
        assert rebound["status"] == "ok", rebound
        assert seen[-1] == other
    finally:
        reset_sandbox_session(sid)


def test_unknown_result_type_stays_in_the_child():
    class Weird:
        pass

    with pytest.raises(ValueError, match="pickle boundary"):
        serialize_result(Weird())


def test_serialize_result_dict_key_collision_raises():
    """Stringifying keys must not drop a value when 1 and "1" share a wire key."""
    with pytest.raises(ValueError, match="collide"):
        serialize_result({1: "a", "1": "b"})
    assert serialize_result({1: "a"}) == {"1": "a"}


def test_serialize_result_custom_dict_key_collision_raises():
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="collide"):
        serialize_result({1: np.arange(3), "1": "b"})


def test_optional_module_skips_partially_initialized_module(monkeypatch: pytest.MonkeyPatch):
    import importlib.machinery
    import sys
    import types

    from plugin.scripting.venv import optional_import

    name = "writeragent_test_partial_mod"
    mod = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, loader=None)
    setattr(spec, "_initializing", True)
    mod.__spec__ = spec
    monkeypatch.setitem(sys.modules, name, mod)
    assert optional_import.optional_module(name) is None


def test_serialize_result_rejects_self_referential_list():
    items: list[object] = []
    items.append(items)
    with pytest.raises(ValueError, match="self-referential"):
        serialize_result(items)


def test_serialize_result_rejects_self_referential_list_with_dataframe():
    pd = pytest.importorskip("pandas")
    items: list[object] = [pd.DataFrame({"a": [1]})]
    items.append(items)
    with pytest.raises(ValueError, match="self-referential"):
        serialize_result(items)


def test_serialize_result_rejects_self_referential_dict_with_dataframe():
    pd = pytest.importorskip("pandas")
    d: dict[str, object] = {"df": pd.DataFrame({"a": [1]})}
    d["self"] = d
    with pytest.raises(ValueError, match="self-referential"):
        serialize_result(d)


def test_serialize_result_allows_shared_sublist_with_dataframe():
    from plugin.scripting.payload_codec import is_dataframe_payload

    pd = pytest.importorskip("pandas")
    shared = [pd.DataFrame({"a": [1]})]
    res = serialize_result([shared, shared])
    assert is_dataframe_payload(res[0][0])
    assert is_dataframe_payload(res[1][0])


def test_serialize_result_rejects_deep_nesting():
    obj: list[object] = []
    cursor = obj
    for unused in range(80):
        nxt: list[object] = []
        cursor.append(nxt)
        cursor = nxt
    with pytest.raises(ValueError, match="too deeply nested"):
        serialize_result(obj)


def test_serialize_result_allows_shared_sublist():
    from plugin.scripting.venv.to_wire import to_wire

    shared = [1]
    assert to_wire([shared, shared], target="pickle") == [[1], [1]]


def test_serialize_result_set_of_range_falls_back_to_list():
    """range coerces to a list, which cannot live inside a set.

    A set containing range must coerce to a list wire value. Otherwise the
    comprehension raises TypeError and a successful result becomes an error
    frame.
    """
    assert serialize_result({1}) == {1}
    assert serialize_result(frozenset({1})) == frozenset({1})
    assert serialize_result({range(3)}) == [[0, 1, 2]]
    assert serialize_result(frozenset({range(2)})) == [[0, 1]]


def test_clongdouble_scalar_names_the_type():
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match=r"clongdouble.*builtin complex"):
        serialize_result(np.clongdouble(1 + 2j))


def test_class_statement_result_is_none():
    res1 = sandbox_run("class Foo:\n    pass", timeout_sec=8)
    assert res1["status"] == "ok", res1
    assert res1["result"] is None

    res2 = sandbox_run("class Foo:\n    pass\nresult = Foo", timeout_sec=8)
    assert res2["status"] == "ok", res2
    assert res2["result"] is None


def test_instance_result_errors_and_keeps_stdout():
    res = sandbox_run(
        'print("hello world")\nclass Box:\n    pass\nbox = Box()\n',
        timeout_sec=8,
    )
    assert res["status"] == "error", res
    assert "cannot cross" in res.get("message", "")
    assert res.get("stdout") == "hello world\n"


def test_system_exit_keeps_stdout():
    res = sandbox_run('print("hi")\nraise SystemExit(1)', timeout_sec=8)
    assert res["status"] == "error", res
    assert res.get("stdout") == "hi\n"


def test_module_expression_still_cannot_cross():
    res = sandbox_run("import math\nresult = math", timeout_sec=8)
    assert res["status"] == "error", res
    assert "cannot cross" in res.get("message", "")


def test_timedelta64_without_pandas_is_fractional_days(monkeypatch: pytest.MonkeyPatch) -> None:
    np = pytest.importorskip("numpy")
    import datetime
    from plugin.scripting.venv import venv_sandbox as vs

    from plugin.scripting.venv import optional_import as oi

    real_opt = oi.optional_module

    def fake_opt(name: str) -> Any:
        if name == "pandas":
            return None
        return real_opt(name)

    monkeypatch.setattr(oi, "optional_module", fake_opt)

    assert vs.serialize_result(np.timedelta64(90, "ns")) == 0.0
    assert vs.serialize_result(np.array(90, dtype="timedelta64[ns]")) == 0.0
    assert vs.serialize_result(np.timedelta64(500, "ns")) == 0.0
    assert vs.serialize_result(np.timedelta64(1500, "ns")) == (
        datetime.timedelta(microseconds=1500 / 1000).total_seconds() / 86400.0
    )
    assert vs.serialize_result(np.timedelta64(-1500, "ns")) == (
        datetime.timedelta(microseconds=-1500 / 1000).total_seconds() / 86400.0
    )
    assert vs.serialize_result(np.timedelta64(1, "D")) == 1.0
    assert vs.serialize_result(np.timedelta64(1, "Y")) == 365.2425
    assert vs.serialize_result(np.timedelta64(1, "M")) == 30.436875
    assert vs.serialize_result(np.timedelta64(90)) == 0.0
    assert vs.serialize_result(np.timedelta64(2, "ps")) == 0.0
    assert vs.serialize_result(np.timedelta64("NaT", "ns")) is None
    with pytest.raises(ValueError, match="pickle boundary"):
        vs.serialize_result(np.timedelta64(10**18, "D"))


def test_capture_closes_figures_when_figure_lookup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from plugin.scripting.venv import optional_import as oi
    from plugin.scripting.venv import venv_sandbox as vs

    closed: list[Any] = []

    class FakePyplot:
        @staticmethod
        def get_fignums() -> list[int]:
            return [1]

        @staticmethod
        def figure(num: int) -> Any:
            raise RuntimeError("backend gone")

        @staticmethod
        def close(arg: Any) -> None:
            closed.append(arg)

    def fake_opt(name: str) -> Any:
        if name == "matplotlib.pyplot":
            return FakePyplot
        return None

    monkeypatch.setattr(oi, "optional_module", fake_opt)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", cast("Any", FakePyplot))

    with pytest.raises(RuntimeError, match="backend gone"):
        vs._capture_open_figures_payload()
    assert closed == ["all"]


def test_run_sandboxed_code_allows_future_and_collections_abc() -> None:
    res1 = sandbox_run("from __future__ import annotations\nresult = 1 + 1\n")
    assert res1["status"] == "ok"
    assert res1["result"] == 2

    res2 = sandbox_run("from collections.abc import Mapping\nresult = issubclass(dict, Mapping)\n")
    assert res2["status"] == "ok"
    assert res2["result"] is True


def test_unpickleable_init_result_closes_open_figures(fresh_sandbox_sessions) -> None:
    pytest.importorskip("matplotlib")
    import matplotlib.pyplot as plt

    init_script = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3])\n"
        "class Box:\n"
        "    pass\n"
        "Box()\n"
    )
    try:
        res = sandbox_run(
            "result = 5",
            session_id=None,
            init_script=init_script,
            init_session_id="isolated:fig-close:init",
            init_script_hash="h-fig-close",
        )
        assert res["status"] == "ok"
        assert res["result"] == 5
    finally:
        plt.close("all")


def test_init_executor_omits_cell_bindings(fresh_sandbox_sessions) -> None:
    res = sandbox_run(
        "result = 1",
        data=None,
        bindings={"scoped_dir": "/tmp/wa-init-ns"},
        session_id="calc:init-namespace",
        init_script="HELPER = 1",
        init_session_id="calc:init-namespace:init",
        init_script_hash="ns",
    )
    assert res["status"] == "ok"
    init_exec = fresh_sandbox_sessions.executor("calc:init-namespace:init")
    assert init_exec is not None
    init_state = init_exec.state
    assert init_state.get("HELPER") == 1
    for name in (
        "data",
        "ranges",
        "xl",
        "scoped_dir",
        "session_duckdb",
        "run_sql",
        "invalidate_session_tables",
    ):
        assert name not in init_state


def test_shared_init_import_session_duckdb_shares_catalog(fresh_sandbox_sessions) -> None:
    pytest.importorskip("duckdb")
    from plugin.framework.uno_bootstrap import register_alias_importer

    register_alias_importer()
    init_script = (
        "from writeragent.scripting.duckdb_sql import session_duckdb\n"
        "session_duckdb().execute('CREATE TABLE init_marker AS SELECT 11 AS n')\n"
        "HELPER = 3\n"
    )
    sid = "calc:init-duck"
    init_sid = f"{sid}:init"
    r1 = sandbox_run(
        "result = HELPER",
        session_id=sid,
        init_script=init_script,
        init_session_id=init_sid,
        init_script_hash="h",
    )
    assert r1["status"] == "ok"
    assert r1["result"] == 3

    r2 = sandbox_run(
        "result = session_duckdb().execute('SELECT n FROM init_marker').fetchone()[0]",
        session_id=sid,
        init_script=init_script,
        init_session_id=init_sid,
        init_script_hash="h",
    )
    assert r2["status"] == "ok"
    assert r2["result"] == 11


def test_isolated_init_import_session_duckdb_does_not_share_catalog(fresh_sandbox_sessions) -> None:
    pytest.importorskip("duckdb")
    from plugin.framework.uno_bootstrap import register_alias_importer

    register_alias_importer()
    init_script = (
        "from writeragent.scripting.duckdb_sql import session_duckdb\n"
        "session_duckdb().execute('CREATE TABLE init_marker AS SELECT 11 AS n')\n"
        "HELPER = 3\n"
    )
    r1 = sandbox_run(
        "result = HELPER",
        session_id=None,
        init_script=init_script,
        init_session_id="isolated:init-duck:init",
        init_script_hash="h2",
    )
    assert r1["status"] == "ok"
    assert r1["result"] == 3

    r2 = sandbox_run(
        "result = session_duckdb().execute('SELECT n FROM init_marker').fetchone()[0]",
        session_id=None,
        init_script=init_script,
        init_session_id="isolated:init-duck:init",
        init_script_hash="h2",
    )
    assert r2["status"] == "error"
    assert "init_marker" in str(r2.get("message", ""))


def test_init_result_lambda_is_not_seeded(fresh_sandbox_sessions) -> None:
    from plugin.scripting.venv.venv_sandbox import CellNamespace, _new_executor

    init_script = "def helper():\n    return 4\nresult = lambda: 99\n"
    init_sid = "isolated:skip-result:init"
    r1 = sandbox_run(
        "result = helper()",
        session_id=None,
        init_script=init_script,
        init_session_id=init_sid,
        init_script_hash="s",
    )
    assert r1["status"] == "ok"
    assert r1["result"] == 4

    exe = _new_executor(build_sandbox_policy())
    CellNamespace(exe).seed(init_sid, copy=True)
    assert cast("Any", exe.state["helper"])() == 4
    assert "result" not in exe.state
    assert "result" not in exe.custom_tools
    assert "helper" in exe.custom_tools

    check_code = (
        "try:\n"
        "    result()\n"
        "    result = 'present'\n"
        "except (NameError, Exception):\n"
        "    result = 'absent'\n"
    )
    r2 = sandbox_run(
        check_code,
        session_id=None,
        init_script=init_script,
        init_session_id=init_sid,
        init_script_hash="s",
    )
    assert r2["status"] == "ok"
    assert r2["result"] == "absent"


def test_isolated_init_lru_evicts_oldest_and_refreshes_on_reuse(
    fresh_sandbox_sessions,
) -> None:
    store = fresh_sandbox_sessions
    for i in range(33):
        r = sandbox_run(
            "result = 1",
            session_id=None,
            init_script=f"MARKER = {i}",
            init_session_id=f"isolated:{i}:init",
            init_script_hash=f"h{i}",
            timeout_sec=8,
        )
        assert r["status"] == "ok"

    assert store.executor("isolated:0:init") is None
    assert store.executor("isolated:32:init") is not None
    assert sum(store.executor(f"isolated:{i}:init") is not None for i in range(33)) == 32

    # Re-run i == 1 with the same script and hash "h1" (early return, LRU refresh)
    with patch.object(vs, "_run_on_executor", wraps=vs._run_on_executor) as patched_run:
        r_refresh = sandbox_run(
            "result = 1",
            session_id=None,
            init_script="MARKER = 1",
            init_session_id="isolated:1:init",
            init_script_hash="h1",
            timeout_sec=8,
        )
        assert r_refresh["status"] == "ok"
        assert patched_run.call_count == 1

    # Then one more id isolated:33:init with MARKER = 33 and hash "h33"
    r_33 = sandbox_run(
        "result = 1",
        session_id=None,
        init_script="MARKER = 33",
        init_session_id="isolated:33:init",
        init_script_hash="h33",
        timeout_sec=8,
    )
    assert r_33["status"] == "ok"

    assert store.executor("isolated:1:init") is not None
    assert store.executor("isolated:2:init") is None
    assert sum(store.executor(f"isolated:{i}:init") is not None for i in range(34)) == 32


def test_harness_calls_exit_on_abandoned_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import plugin.contrib.smolagents.local_python_executor as lpe
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    exit_calls: list[int] = []
    monkeypatch.setattr(harness.os, "_exit", lambda code: exit_calls.append(code))
    monkeypatch.setattr(harness, "die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    req_data = pack_pickle_frame({"id": "req-timeout", "action": "execute", "code": "result = 1"})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness, "claim_ipc_stdin", lambda: stdin)
    monkeypatch.setattr(harness, "claim_ipc_channel", lambda: stdout)

    def _simulate_timeout(*args, **kwargs):
        lpe.abandoned_timeout = True
        return {"status": "error", "message": "Code execution exceeded the maximum execution time"}

    monkeypatch.setattr(harness, "_handle_request", _simulate_timeout)

    try:
        harness.main()
    finally:
        lpe.abandoned_timeout = False
        from plugin.scripting.host_tool_call import set_process_role

        # main() sets the process role to worker; put it back so later tests run as host.
        set_process_role(None)

    assert exit_calls == [1]
    stdout.seek(0)
    ready = read_pickle_frame(stdout, require_dict=True)
    assert ready is not None and ready.get("status") == "ready"
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {"type": "exec_started", "id": "req-timeout"}
    err_frame = read_pickle_frame(stdout, require_dict=True)
    assert err_frame is not None
    assert err_frame["status"] == "error"
    assert "exceeded" in err_frame["message"]


def test_stdio_loop_calls_exit_on_abandoned_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import plugin.contrib.smolagents.local_python_executor as lpe
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame
    # compute_service/ is not in the OXT bundle that release_verify tests.
    ws = pytest.importorskip("compute_service.worker_stdio")

    exit_calls: list[int] = []
    monkeypatch.setattr(ws.os, "_exit", lambda code: exit_calls.append(code))
    monkeypatch.setattr("plugin.framework.child_runtime.die_with_parent", lambda: False)

    req_data = pack_pickle_frame({"id": "req-stdio-timeout", "code": "x = 1"})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()

    class _FakeStdin:
        buffer = stdin

    monkeypatch.setattr(ws.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(ws, "claim_ipc_channel", lambda: stdout)

    def _handler(req):
        lpe.abandoned_timeout = True
        return {"id": req.get("id"), "status": "error", "error": "timeout"}

    try:
        ws.run_worker_stdio_loop(_handler)
    finally:
        lpe.abandoned_timeout = False

    assert exit_calls == [1]
    stdout.seek(0)
    ready = read_pickle_frame(stdout, require_dict=True)
    assert ready == {"status": "ready", "pid": ws.os.getpid()}
    err_frame = read_pickle_frame(stdout, require_dict=True)
    assert err_frame is not None
    assert err_frame["status"] == "error"
    assert err_frame["exiting"] is True


def test_sandbox_policy_socket_default_errors():
    res = sandbox_run("import socket", sandbox_policy=None)
    assert res["status"] == "error"
    assert "Import of socket is not allowed" in res["message"]


def test_sandbox_policy_network_binds_socket():
    res = sandbox_run(
        "import socket\nresult = bool(hasattr(socket, 'socket'))",
        sandbox_policy={"network": True},
    )
    assert res["status"] == "ok"
    assert res["result"] is True


def test_sandbox_policy_files_rw_and_remove(tmp_path):
    target = str(tmp_path / "sandbox_test_file.txt")
    code = f"""
import os
with open({target!r}, "w") as f:
    f.write("sandboxed-hello")
with open({target!r}, "r") as f:
    content = f.read()
os.remove({target!r})
result = content
"""
    res = sandbox_run(code, sandbox_policy={"files": True})
    assert res["status"] == "ok"
    assert res["result"] == "sandboxed-hello"
    import os
    assert not os.path.exists(target)

    res_sub = sandbox_run("import subprocess\nsubprocess.run(['echo'])", sandbox_policy={"files": True})
    assert res_sub["status"] == "error"


def test_sandbox_policy_process_spawning_blocked_by_default():
    """Verify asyncio subprocess callables and ProcessPoolExecutor are blocked when python_allow_os is OFF."""
    # asyncio subprocess: create_subprocess_exec and create_subprocess_shell blocked
    res = sandbox_run(
        "import asyncio\nproc = asyncio.run(asyncio.create_subprocess_exec('echo', 'hi'))",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: create_subprocess_exec" in res.get("message", "")

    res = sandbox_run(
        "import asyncio\nproc = asyncio.run(asyncio.create_subprocess_shell('echo hi'))",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: create_subprocess_shell" in res.get("message", "")

    # ProcessPoolExecutor: blocked via from import and direct attribute access
    res = sandbox_run(
        "from concurrent.futures import ProcessPoolExecutor\np = ProcessPoolExecutor(max_workers=1)",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: ProcessPoolExecutor" in res.get("message", "")

    res = sandbox_run(
        "import concurrent.futures\np = concurrent.futures.ProcessPoolExecutor(max_workers=1)",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: ProcessPoolExecutor" in res.get("message", "")


def test_sandbox_policy_process_spawning_allowed_with_os_flag():
    """Verify that starting another process works across all mechanisms when python_allow_os is ON."""
    policy = {"os": True}

    # 1. subprocess.run: works
    code_sub = "import subprocess\nres = subprocess.run(['echo', 'hello'], capture_output=True, text=True)\nresult = res.stdout.strip()"
    res = sandbox_run(code_sub, sandbox_policy=policy)
    assert res["status"] == "ok"
    assert res["result"] == "hello"

    # 2. os.system: works
    res = sandbox_run("import os\nresult = os.system('true')", sandbox_policy=policy)
    assert res["status"] == "ok"
    assert res["result"] == 0

    # 3. asyncio subprocess: create_subprocess_exec and create_subprocess_shell work
    code_aio_exec = """
import asyncio
loop = asyncio.new_event_loop()
proc = loop.run_until_complete(asyncio.create_subprocess_exec('echo', 'hi'))
loop.run_until_complete(proc.wait())
loop.close()
result = proc.pid > 0
"""
    res = sandbox_run(code_aio_exec, sandbox_policy=policy)
    assert res["status"] == "ok"
    assert res["result"] is True

    code_aio_shell = """
import asyncio
loop = asyncio.new_event_loop()
proc = loop.run_until_complete(asyncio.create_subprocess_shell('echo shell_hi'))
loop.run_until_complete(proc.wait())
loop.close()
result = proc.pid > 0
"""
    res = sandbox_run(code_aio_shell, sandbox_policy=policy)
    assert res["status"] == "ok"
    assert res["result"] is True

    # 4. ProcessPoolExecutor: works
    code_pool = """
from concurrent.futures import ProcessPoolExecutor
import math
with ProcessPoolExecutor(max_workers=1) as executor:
    fut = executor.submit(math.sqrt, 25.0)
    result = fut.result()
"""
    res = sandbox_run(code_pool, sandbox_policy=policy)
    assert res["status"] == "ok"
    assert res["result"] == 5.0

    # 5. multiprocessing.Pool: works
    code_mp_pool = """
from multiprocessing import Pool
import math

with Pool(1) as pool:
    result = pool.apply(math.sqrt, [49.0])
    pool.close()
    pool.join()
"""
    res = sandbox_run(code_mp_pool, sandbox_policy=policy)
    assert res["status"] == "ok", res
    assert res["result"] == 7.0

    # 6. multiprocessing.Process: works
    code_mp_proc = """
from multiprocessing import Process
import time

p = Process(target=time.sleep, args=(0.01,))
p.start()
p.join()
result = (p.exitcode == 0)
"""
    res = sandbox_run(code_mp_proc, sandbox_policy=policy)
    assert res["status"] == "ok", res
    assert res["result"] is True

    if sys.platform != "win32":
        # 7. pty: works (termios is Unix-only)
        code_pty = """
import os
import pty
master, slave = pty.openpty()
os.close(master)
os.close(slave)
result = (master > 0 and slave > 0)
"""
        res = sandbox_run(code_pty, sandbox_policy=policy)
        assert res["status"] == "ok", res
        assert res["result"] is True


def test_sandbox_policy_os_harmless_reads_allowed_by_default():
    """Verify that harmless os reads (os.name, os.sep, os.linesep, os.path.join/basename) work by default."""
    import os
    code = """
import os
result = (os.name, os.sep, os.linesep, os.path.join('a', 'b'), os.path.basename('/x/y'))
"""
    res = sandbox_run(code, sandbox_policy=None)
    assert res["status"] == "ok"
    assert res["result"] == [os.name, os.sep, os.linesep, os.path.join('a', 'b'), 'y']


def test_sandbox_policy_shared_session_rebuild():
    sid = "calc:test_policy_rebuild"
    try:
        r1 = sandbox_run(
            "import socket\nx = 1\nresult = x",
            session_id=sid,
            sandbox_policy={"network": True},
        )
        assert r1["status"] == "ok"
        assert r1["result"] == 1

        r2 = sandbox_run(
            "result = x",
            session_id=sid,
            sandbox_policy=None,
        )
        assert r2["status"] == "error"
        assert "not defined" in r2["message"] or "NameError" in r2["message"] or "InterpreterError" in r2["message"]

        r3 = sandbox_run("import socket", session_id=sid, sandbox_policy=None)
        assert r3["status"] == "error"
        assert "Import of socket is not allowed" in r3["message"]
    finally:
        from plugin.scripting.venv.venv_sandbox import reset_sandbox_session
        reset_sandbox_session(sid)


def test_sandbox_policy_timeout_wins_over_subprocess():
    import sys

    code_sub = f"""
import subprocess
subprocess.run([{sys.executable!r}, "-c", "import time; time.sleep(10)"])
"""
    r_sub = sandbox_run(code_sub, timeout_sec=1, sandbox_policy={"os": True})
    assert r_sub["status"] == "error"
    assert any(term in r_sub["message"].lower() for term in ("timeout", "timed out", "exceeded", "maximum execution time")) or r_sub.get("code") == "TIMEOUT"





def test_extra_modules_override_blocklist() -> None:
    from tests.scripting.sandbox_support import sandbox_run

    res = sandbox_run(
        "import socket\nresult = callable(socket.socket)",
        sandbox_policy={"extra_modules": "socket"},
    )
    assert res.get("status") == "ok", res
    code = "import subprocess, sys\nresult = subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True).returncode"
    res = sandbox_run(code, sandbox_policy={"extra_modules": "subprocess, sys"})
    assert res.get("status") == "ok", res
    assert res.get("result") == 0


@pytest.mark.parametrize(
    "mod",
    ["importlib", "builtins", "pickle", "subprocess", "pty", "multiprocessing", "marshal"],
)
def test_sandbox_runtime_default_blocks_host_escape(mod: str) -> None:
    res = sandbox_run(f"import {mod}", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()


def test_sandbox_runtime_blocks_internal_plugin_and_writeragent_framework() -> None:
    for mod in ("plugin.framework.config", "writeragent.framework.config"):
        res = sandbox_run(f"import {mod}", sandbox_policy=None)
        assert res["status"] == "error"
        assert "not allowed" in res.get("message", "").lower()


def test_sandbox_runtime_reexported_os_proxy() -> None:
    # default: read-only _FileOsProxy (no system, remove forbidden)
    res_def = sandbox_run(
        "from random import _os\nresult = (hasattr(_os, 'system'), hasattr(_os, 'remove'))",
        sandbox_policy=None,
    )
    assert res_def["status"] == "ok"
    assert res_def["result"] == [False, True]

    res_remove_def = sandbox_run(
        "from random import _os\n_os.remove('nonexistent_file_xyz')",
        sandbox_policy=None,
    )
    assert res_remove_def["status"] == "error"
    assert "Forbidden access to function: remove" in res_remove_def.get("message", "")

    # files=True: _FileOsProxy with writes (remove present, no system)
    res_files = sandbox_run(
        "from random import _os\nresult = (hasattr(_os, 'system'), callable(_os.remove))",
        sandbox_policy={"files": True},
    )
    assert res_files["status"] == "ok"
    assert res_files["result"] == [False, True]

    # os=True: raw os module (callable(_os.system) is True)
    res_os = sandbox_run(
        "from random import _os\nresult = callable(_os.system)",
        sandbox_policy={"os": True},
    )
    assert res_os["status"] == "ok"
    assert res_os["result"] is True


def test_sandbox_runtime_requests_network_gate() -> None:
    res_def = sandbox_run("import requests", sandbox_policy=None)
    assert res_def["status"] == "error"
    assert "not allowed" in res_def.get("message", "").lower()

    res_net = sandbox_run(
        "import requests\nresult = bool(hasattr(requests, 'get'))",
        sandbox_policy={"network": True},
    )
    assert res_net["status"] == "ok"
    assert res_net["result"] is True


def test_sandbox_runtime_pathlib_allowed_default_and_files() -> None:
    res_def = sandbox_run(
        "import pathlib\nresult = pathlib.Path('.').exists()",
        sandbox_policy=None,
    )
    assert res_def["status"] == "ok"
    assert res_def["result"] is True

    res_files = sandbox_run(
        "import pathlib\nresult = pathlib.Path('.').exists()",
        sandbox_policy={"files": True},
    )
    assert res_files["status"] == "ok"
    assert res_files["result"] is True


@pytest.mark.parametrize(
    "mod",
    [
        "io",
        "logging",
        "glob",
        "zipfile",
        "asyncio",
        "inspect",
        "sys",
        "signal",
        "concurrent.futures",
    ],
)
def test_sandbox_runtime_safe_stdlib_allowed_by_default(mod: str) -> None:
    res = sandbox_run(f"import {mod}\nresult = True", sandbox_policy=None)
    assert res["status"] == "ok"
    assert res["result"] is True


@pytest.mark.parametrize("mod", ["shutil", "tempfile", "sqlite3"])
def test_sandbox_runtime_file_blocked_modules(mod: str) -> None:
    res_def = sandbox_run(f"import {mod}", sandbox_policy=None)
    assert res_def["status"] == "error"
    assert "not allowed" in res_def.get("message", "").lower()

    res_files = sandbox_run(f"import {mod}\nresult = True", sandbox_policy={"files": True})
    assert res_files["status"] == "ok"
    assert res_files["result"] is True


# {p} is a fresh target path, {src} an existing file holding "fixture", {d} an existing empty dir.
_FILE_WRITES = [
    "import io\nio.open({p}, 'w').write('x')",
    "import io\nio.open({p}, mode='a').write('x')",
    "import io\nio.open({p}, 'x').write('x')",
    "import io\nio.open({src}, 'r+').write('x')",
    "import io\nio.open({p}, 'wb').write(b'x')",
    "import io\nio.FileIO({p}, 'w').write(b'x')",
    "import io\nraw = io.FileIO({src}, 'r')\nio.FileIO.__init__(raw, {p}, 'w')",
    "import codecs\ncodecs.open({p}, 'w').write('x')",
    "import _pyio\n_pyio.open({p}, 'w').write('x')",
    "import _pyio\n_pyio.FileIO({p}, 'w').write(b'x')",
    "from pathlib import Path\nPath({p}).write_text('x')",
    "from pathlib import Path\nPath({p}).write_bytes(b'x')",
    "from pathlib import Path\nPath({p}).open('w').write('x')",
    "from pathlib import Path\nPath({p}).open(mode='a').write('x')",
    "from pathlib import Path\nPath({p}).touch()",
    "from pathlib import Path\nPath({p}).mkdir()",
    "from pathlib import Path\nPath({src}).unlink()",
    "from pathlib import Path\nPath({src}).rename({p})",
    "from pathlib import Path\nPath({src}).replace({p})",
    "from pathlib import Path\nPath({d}).rmdir()",
    "from pathlib import Path\nPath({src}).chmod(0o777)",
    "from pathlib import Path\nPath({p}).symlink_to({src})",
    "from pathlib import Path\nPath({p}).hardlink_to({src})",
    "import operator\nfrom pathlib import Path\noperator.methodcaller('write_text', 'x')(Path({p}))",
    "import io\nlist(map(io.open, [{p}], ['w']))",
]


def _assert_src_fixture_unchanged(src: pathlib.Path) -> None:
    assert src.read_text() == "fixture"
    if sys.platform != "win32":
        assert src.stat().st_mode & 0o777 == 0o644


def _file_fixture(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, dict[str, str]]:
    p, src, d = tmp_path / "out", tmp_path / "src.txt", tmp_path / "empty"
    target = tmp_path / "target.txt"
    target.write_text("fixture")
    target.chmod(0o644)
    try:
        src.symlink_to(target)
    except OSError:
        src.write_text("fixture")
    d.mkdir()
    return p, src, d, {"p": repr(str(p)), "src": repr(str(src)), "d": repr(str(d))}


@pytest.mark.parametrize("code", _FILE_WRITES)
def test_sandbox_runtime_file_writes_blocked_unless_files(code: str, tmp_path: pathlib.Path) -> None:
    p, src, d, names = _file_fixture(tmp_path)
    res = sandbox_run(code.format(**names), sandbox_policy=None)
    assert res["status"] == "error"
    assert "Forbidden access to function" in res.get("message", "")
    assert not p.exists() and not p.is_symlink()
    _assert_src_fixture_unchanged(src)
    assert d.is_dir()

    res_files = sandbox_run(code.format(**names) + "\nresult = True", sandbox_policy={"files": True})
    assert res_files["status"] == "ok", res_files


def test_sandbox_runtime_file_writes_allowed_with_os_policy(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "os.txt"
    res = sandbox_run(f"from pathlib import Path\nPath({str(p)!r}).write_text('ok')", sandbox_policy={"os": True})
    assert res["status"] == "ok"
    assert p.read_text() == "ok"


_FILE_READS = [
    "result = open({src}).read()",
    "import io\nresult = io.open({src}, 'r').read()",
    "import io\nresult = io.open({src}, 'rb').read().decode()",
    "import io\nresult = io.FileIO({src}, 'r').read().decode()",
    "import io\nresult = io.FileIO({src}, 'rb').read().decode()",
    "import io\nraw = io.FileIO({src}, 'r')\nio.FileIO.__init__(raw, {src}, 'r')\nresult = raw.read().decode()",
    "import codecs\nresult = codecs.open({src}, 'r').read()",
    "import codecs\nresult = codecs.open({src}, 'rb').read().decode()",
    "import _pyio\nresult = _pyio.open({src}, 'r').read()",
    "import _pyio\nresult = _pyio.open({src}, 'rb').read().decode()",
    "from pathlib import Path\nresult = Path({src}).read_text()",
    "from pathlib import Path\nresult = Path({src}).read_bytes().decode()",
    "from pathlib import Path\nresult = Path({src}).open().read()",
    "from pathlib import Path\nresult = Path({src}).open('rb').read().decode()",
    "from pathlib import Path\nlist(Path({d}).iterdir())\nresult = 'fixture'",
    "from pathlib import Path\nlist(Path({d}).glob('*'))\nresult = 'fixture'",
    "from pathlib import Path\nlist(Path({d}).rglob('*'))\nresult = 'fixture'",
    "import operator\nfrom pathlib import Path\nresult = operator.methodcaller('read_text')(Path({src}))",
    "import io\nresult = list(map(io.open, [{src}]))[0].read()",
    "import os\nos.listdir({d})\nresult = 'fixture'",
    "from os import listdir\nlistdir({d})\nresult = 'fixture'",
    "import os\nlist(os.scandir({d}))\nresult = 'fixture'",
    "import os\nlist(os.walk({d}))\nresult = 'fixture'",
    "import os\nos.stat({src})\nresult = 'fixture'",
    "import os\nos.readlink({src})\nresult = 'fixture'",
    "import glob\nglob.glob({src})\nresult = 'fixture'",
    "import fileinput\nfi = fileinput.input([{src}])\nlist(fi)\nfi.close()\nresult = 'fixture'",
    "import fileinput\nfi = fileinput.FileInput([{src}])\nlist(fi)\nfi.close()\nresult = 'fixture'",
    "import linecache\nresult = linecache.getline({src}, 1).strip()",
    "import gzip\ngzip.open({src}).close()\nresult = 'fixture'",
    "import bz2\nbz2.open({src}).close()\nresult = 'fixture'",
    "import lzma\nlzma.open({src}).close()\nresult = 'fixture'",
    "import zipfile\ntry:\n    zipfile.ZipFile({src})\nexcept zipfile.BadZipFile:\n    pass\nresult = 'fixture'",
    "import zipfile\nzipfile.is_zipfile({src})\nresult = 'fixture'",
    "import tarfile\ntry:\n    tarfile.open({src})\nexcept tarfile.ReadError:\n    pass\nresult = 'fixture'",
    "import logging\nh = logging.FileHandler({src})\nh.close()\nresult = 'fixture'",
    "import logging\nlogging.basicConfig(filename={src})\nresult = 'fixture'",
]


@pytest.mark.parametrize("code", _FILE_READS)
def test_sandbox_runtime_file_reads_gated(code: str, tmp_path: pathlib.Path) -> None:
    _p, _src, _d, names = _file_fixture(tmp_path)
    if "readlink" in code and not _src.is_symlink():
        pytest.skip("symlink fixture unavailable on this host")
    res = sandbox_run(code.format(**names), sandbox_policy=None)
    assert res["status"] == "error"
    if "open({src}).read()" in code:
        assert (
            "name 'open' is not defined" in res.get("message", "")
            or "Forbidden function evaluation: 'open'" in res.get("message", "")
        )
    else:
        assert "Forbidden access to function" in res.get("message", "")

    res_files = sandbox_run(code.format(**names), sandbox_policy={"files": True})
    assert res_files["status"] == "ok", res_files
    assert res_files["result"] == "fixture"


def test_sandbox_runtime_file_reads_allowed_with_os_policy(tmp_path: pathlib.Path) -> None:
    _p, src, _d, _names = _file_fixture(tmp_path)
    res = sandbox_run(f"from pathlib import Path\nresult = Path({str(src)!r}).read_text()", sandbox_policy={"os": True})
    assert res["status"] == "ok"
    assert res["result"] == "fixture"


def test_sandbox_runtime_safe_in_memory_allowed_without_file_policy(tmp_path: pathlib.Path) -> None:
    _p, src, _d, _names = _file_fixture(tmp_path)

    res_io = sandbox_run(
        "import io\ns = io.StringIO('fixture').read()\nb = io.BytesIO(b'fixture').read()\nresult = s == 'fixture' and b == b'fixture'",
        sandbox_policy=None,
    )
    assert res_io["status"] == "ok" and res_io["result"] is True

    res_gz = sandbox_run(
        "import gzip\nresult = gzip.decompress(gzip.compress(b'fixture')).decode()",
        sandbox_policy=None,
    )
    assert res_gz["status"] == "ok" and res_gz["result"] == "fixture"

    res_zip = sandbox_run(
        "import io, zipfile\nbuf = io.BytesIO()\nwith zipfile.ZipFile(buf, 'w') as zf:\n    zf.writestr('x', 'fixture')\nwith zipfile.ZipFile(buf, 'r') as zf:\n    result = zf.read('x').decode()",
        sandbox_policy=None,
    )
    assert res_zip["status"] == "ok" and res_zip["result"] == "fixture"

    res_gz_buf = sandbox_run(
        "import io, gzip\nbuf = io.BytesIO()\nwith gzip.open(buf, 'wb') as f:\n    f.write(b'fixture')\nwith gzip.open(io.BytesIO(buf.getvalue()), 'rb') as f:\n    result = f.read().decode()",
        sandbox_policy=None,
    )
    assert res_gz_buf["status"] == "ok" and res_gz_buf["result"] == "fixture"

    res_exists = sandbox_run(
        f"import os\nfrom pathlib import Path\nresult = os.path.exists({repr(str(src))}) and os.path.isfile({repr(str(src))}) and Path({repr(str(src))}).exists()",
        sandbox_policy=None,
    )
    assert res_exists["status"] == "ok" and res_exists["result"] is True

    res_glob = sandbox_run("import glob\nresult = glob.escape('[*]')", sandbox_policy=None)
    assert res_glob["status"] == "ok" and res_glob["result"] in (r"\[\*\]", "[[][*]]")


def test_sandbox_runtime_extra_modules_allows_importlib() -> None:
    res = sandbox_run(
        "import importlib\nresult = bool(hasattr(importlib, 'import_module'))",
        sandbox_policy={"extra_modules": "importlib"},
    )
    assert res["status"] == "ok"
    assert res["result"] is True


def test_inject_auto_imports_ignores_nested_scope_bindings() -> None:
    from plugin.scripting.venv import optional_import
    from plugin.scripting.venv.venv_sandbox import (
        _new_executor,
        inject_auto_imports,
    )

    executor = _new_executor(build_sandbox_policy())
    inject_auto_imports(executor, "def f():\n    dt = 1\nresult = dt")
    assert "dt" in executor.state
    res = executor("def g():\n    dt = 1\nresult = dt.date(2020, 1, 1).isoformat()")
    assert res.output == "2020-01-01"

    executor_class = _new_executor(build_sandbox_policy())
    inject_auto_imports(executor_class, "class C:\n    dt = 1\nresult = 1")
    assert "dt" in executor_class.state

    executor2 = _new_executor(build_sandbox_policy())
    inject_auto_imports(executor2, "def f():\n    import numpy as np\nresult = 1")
    if optional_import.optional_module("numpy") is not None:
        assert "np" in executor2.state


def test_apply_auto_imports_skips_shadowed_alias() -> None:
    from plugin.scripting.venv.venv_sandbox import apply_auto_imports

    code1, _ = apply_auto_imports("dt = 123\nresult = dt")
    assert "import datetime as dt" not in code1

    code2, _ = apply_auto_imports("def f():\n    import datetime as dt\nresult = 1")
    assert "import datetime as dt" in code2


def test_apply_auto_imports_unparseable_source_still_prepends() -> None:
    from plugin.scripting.venv.venv_sandbox import apply_auto_imports

    # Partial code from the editor must still get imports for completion.
    code, lines = apply_auto_imports("def ")
    assert lines > 0
    assert "import " in code


def test_run_sandboxed_code_rejects_non_dict_sandbox_policy() -> None:
    res = sandbox_run("result = 1", sandbox_policy=cast("Any", [("extra_modules", "x")]))
    assert res["status"] == "error"


def test_run_sandboxed_code_rejects_non_string_extra_modules() -> None:
    res = sandbox_run("result = 1", sandbox_policy={"extra_modules": [1]})
    assert res["status"] == "error"


def test_get_or_create_rebuilds_on_policy_mismatch(fresh_sandbox_sessions) -> None:
    store = fresh_sandbox_sessions
    sid = "calc:get-or-create-policy-mismatch"
    policy_net = build_sandbox_policy(network=True)
    policy_default = build_sandbox_policy()
    exec1, created1 = store.get_or_create(sid, policy=policy_net)
    assert created1 is True
    exec1.send_variables({"MARKER": 42})
    exec2, created2 = store.get_or_create(
        sid, policy=policy_default
    )
    assert created2 is True
    assert exec2 is not exec1
    assert exec2.state.get("MARKER") is None
    exec3, created3 = store.get_or_create(
        sid, policy=policy_default
    )
    assert created3 is False
    assert exec3 is exec2


def test_shared_policy_change_reruns_init_before_seed(fresh_sandbox_sessions) -> None:
    store = fresh_sandbox_sessions
    sid = "calc:policy-init-rerun"
    init_sid = f"{sid}:init"
    init_hash = "h-policy-fixed"
    first = sandbox_run(
        "cell_only = 1\nresult = HELPER",
        session_id=sid,
        init_script="HELPER = 1",
        init_session_id=init_sid,
        init_script_hash=init_hash,
        sandbox_policy={"network": True},
    )
    assert first["status"] == "ok", first
    assert first["result"] == 1
    cell_exec = store.executor(sid)
    assert cell_exec is not None
    assert cell_exec.state.get("cell_only") == 1

    second = sandbox_run(
        "result = HELPER",
        session_id=sid,
        init_script="HELPER = 2",
        init_session_id=init_sid,
        init_script_hash=init_hash,
        sandbox_policy=None,
    )
    assert second["status"] == "ok", second
    assert second["result"] == 2
    cell_exec_after = store.executor(sid)
    assert cell_exec_after is not None
    assert cell_exec_after.state.get("cell_only") is None


def test_shared_policy_change_drops_cell_before_first_init(fresh_sandbox_sessions) -> None:
    sid = "calc:policy-cell-drop"
    r1 = sandbox_run(
        "cell_only = 1\nresult = 1",
        session_id=sid,
        sandbox_policy={"network": True},
    )
    assert r1["status"] == "ok"
    r2 = sandbox_run(
        "result = HELPER",
        session_id=sid,
        init_script="HELPER = 5",
        init_session_id=sid + ":init",
        init_script_hash="h",
    )
    assert r2["status"] == "ok"
    assert r2["result"] == 5


def test_arm_alarm_is_the_alarm_writer() -> None:
    import time as time_mod
    from plugin.scripting.venv.venv_sandbox import _arm_alarm, _new_executor

    exe = _new_executor(build_sandbox_policy())
    assert _arm_alarm(exe, time_mod.monotonic() + 2.2, 7) is None
    assert exe.timeout_seconds == 3
    assert _arm_alarm(exe, time_mod.monotonic() + 0.1, 7) is None
    assert exe.timeout_seconds == 1
    res = _arm_alarm(exe, time_mod.monotonic() - 1, 7)
    assert res == {
        "status": "error",
        "code": "EXECUTION_TIMEOUT",
        "message": "Code execution exceeded the maximum execution time of 7 seconds",
    }
    assert exe.timeout_seconds == 1


def test_run_sandboxed_code_arms_the_cell_from_the_request_budget(fresh_sandbox_sessions) -> None:
    sid = "calc:budget-arm-test"
    res = sandbox_run(
        "result = 123",
        session_id=sid,
        timeout_sec=3,
        init_script="import time\ntime.sleep(1)",
        init_session_id=sid + ":init",
        init_script_hash="h",
    )
    assert res["status"] == "ok"
    assert res["result"] == 123
    exe = fresh_sandbox_sessions.executor(sid)
    assert exe is not None
    assert exe.timeout_seconds <= 2


def test_run_sandboxed_code_timeout_carries_execution_timeout_code(fresh_sandbox_sessions) -> None:
    sid = "calc:timeout-code-test"
    res = sandbox_run("import time\ntime.sleep(5)\nresult = 1", session_id=sid, timeout_sec=1)
    assert res["status"] == "error"
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert "exceeded the maximum execution time" in res.get("message", "")

    follow = sandbox_run("result = 7", session_id=sid, timeout_sec=5)
    assert follow["status"] == "ok"
    assert follow["result"] == 7


@pytest.mark.parametrize(
    "code",
    [
        "import writeragent as wa\nresult = dir(wa)",
        "import writeragent as wa\nresult = dir(wa.shape)",
        "import writeragent as wa\nresult = wa.__doc__",
        "import writeragent as wa\nresult = wa.shape.upsert.__doc__",
        "import writeragent as wa\nresult = getattr(wa, '__doc__')",
    ],
)
def test_blocked_introspection_suggests_wa_help(fresh_sandbox_sessions, code: str) -> None:
    """dir() and __doc__ remain blocked in the sandbox and point the model to wa.help(name)."""
    res = sandbox_run(code, session_id="calc:help-hint-test", timeout_sec=5)
    assert res["status"] == "error", res
    assert "wa.help(name)" in res["message"]


def test_sandbox_wa_help_executes_and_returns_signature(fresh_sandbox_sessions) -> None:
    """Inside the sandbox, import writeragent as wa; wa.help('shape.upsert') returns the API signature."""
    res = sandbox_run(
        "import writeragent as wa\nresult = wa.help('shape.upsert')",
        session_id="calc:wa-help-exec-test",
        timeout_sec=5,
    )
    assert res["status"] == "ok", res
    assert isinstance(res["result"], str)
    assert res["result"].startswith("wa.shape.upsert(")
    assert "Args:" in res["result"]


def test_ensure_mpl_agg_does_not_retry_after_use_failure(monkeypatch):
    from plugin.scripting.venv import optional_import as oi

    calls = {"n": 0}

    class _Mpl:
        def use(self, backend: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("backend busy")

    monkeypatch.setattr(vs, "_MPL_AGG_SET", False)
    monkeypatch.setattr(vs, "_MPL_AGG_FAILED", False)
    monkeypatch.setattr(oi, "optional_module", lambda name: _Mpl() if name == "matplotlib" else None)
    vs._ensure_mpl_agg()
    assert vs._MPL_AGG_SET is False
    assert vs._MPL_AGG_FAILED is True
    vs._ensure_mpl_agg()
    assert calls["n"] == 1


def test_serialize_result_dataframe_numeric():
    """DataFrame with numeric values should serialize to dataframe envelope with split_grid inner when large."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import PAYLOAD_DATAFRAME, is_dataframe_payload, is_split_grid

    df = pd.DataFrame({"a": np.arange(200), "b": np.arange(200) * 0.5})
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["__wa_payload__"] == PAYLOAD_DATAFRAME
    assert res["columns"] == ["a", "b"]
    inner = res["data"]
    # Above BINARY_MIN_CELLS -> split_grid
    assert is_split_grid(inner)
    assert inner["shape"][0] == 200


def test_serialize_result_dataframe_mixed_and_roundtrip():
    """Mixed DF should keep strings via the inner grid/strings path; host unpack yields lists."""
    pd = pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame({"num": [1, 2, 3], "txt": ["x", "y", None]})
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["columns"] == ["num", "txt"]
    unpacked = host_unpack_data(res)
    assert unpacked["columns"] == ["num", "txt"]
    data = unpacked["data"]
    assert data[0] == [1, "x"]
    assert data[2] == [3, None]


def test_run_sandboxed_dataframe_produces_envelope():
    """End-to-end: code returning a DF yields the envelope in the worker response."""
    pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload

    code = "import pandas as pd\ndf = pd.DataFrame({'x': [10, 20], 'y': ['p', 'q']})\nresult = df"
    res = sandbox_run(code, timeout_sec=30)
    assert res["status"] == "ok"
    assert is_dataframe_payload(res["result"])
    assert res["result"]["columns"] == ["x", "y"]


def test_serialize_result_small_numeric_dataframe_spills_body():
    """A numeric DataFrame under BINARY_MIN_CELLS spills header and body, not the header alone."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    from plugin.calc.python.addin_values import to_calc_compatible
    from plugin.scripting.calc_range import result_to_calc_grid
    from plugin.scripting.payload_codec import is_split_grid

    df = pd.DataFrame({"a": np.arange(10), "b": np.arange(10) + 0.5})
    res = serialize_result(df)
    assert not is_split_grid(res["data"])
    unpacked = host_unpack_data(res)
    grid = result_to_calc_grid(unpacked)
    assert grid[0] == ["a", "b"]
    assert len(grid) == 11
    coerced = to_calc_compatible(grid)
    assert coerced[1] == (0, pytest.approx(0.5))

    named = pd.Series(np.arange(5, dtype=float), name="x")
    named_grid = result_to_calc_grid(host_unpack_data(serialize_result(named)))
    assert named_grid[0] == ["x"]
    assert len(named_grid) == 6
    assert named_grid[1] == [pytest.approx(0.0)]


def test_serialize_result_small_object_dataframe_is_host_pickleable():
    """Object/extension frames under BINARY_MIN_CELLS must not leak unpickleable cells.

    Small object DataFrames must not leave host-unpickleable cells in the nested
    list path. Extension types belong in split_grid flatten like frames at
    BINARY_MIN_CELLS and above.
    """
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    nullable = serialize_result(pd.DataFrame({"a": pd.array([True, None], dtype="boolean")}))
    assert nullable["data"] == [[True], [None]]
    assert type(nullable["data"][0][0]) is bool

    ints = serialize_result(pd.DataFrame({"a": [np.int64(1), np.int64(2)]}))
    assert ints["data"] == [[1], [2]]
    assert type(ints["data"][0][0]) is int

    exact = serialize_result(pd.DataFrame({"d": [Decimal("1.5")], "f": [Fraction(1, 4)]}))
    assert exact["data"] == [[1.5, 0.25]]
    assert type(exact["data"][0][0]) is float
    assert type(exact["data"][0][1]) is float

    series = serialize_result(pd.Series([np.int64(3), np.int64(4)]))
    assert series == [3, 4]
    assert type(series[0]) is int


def test_serialize_result_empty_dataframe_and_series():
    """0-row DataFrame / named empty Series → header-only envelope. Not a codec gap."""
    pd = pytest.importorskip("pandas")
    from plugin.calc.python.addin_values import to_calc_compatible
    from plugin.scripting.calc_range import result_to_calc_grid
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame(columns=["A", "B"])
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["columns"] == ["A", "B"]
    assert res["data"] == []
    assert result_to_calc_grid(res) == [["A", "B"]]

    named = pd.Series([], dtype=float, name="x")
    named_res = serialize_result(named)
    assert is_dataframe_payload(named_res)
    assert named_res["columns"] == ["x"]
    assert result_to_calc_grid(named_res) == [["x"]]

    unnamed = pd.Series([], dtype=float)
    assert to_calc_compatible(serialize_result(unnamed)) == ()


def test_serialize_result_dataframe_datetime_is_iso_not_unix_epoch():
    """datetime64 columns must not take split_grid astype(float64) (Unix epoch days)."""
    pd = pytest.importorskip("pandas")
    from plugin.calc.python.addin_values import to_calc_compatible
    from plugin.scripting.calc_range import result_to_calc_grid

    df = pd.DataFrame({"d": pd.to_datetime(["2026-06-25", "2026-06-26"])})
    unpacked = host_unpack_data(serialize_result(df))
    coerced = to_calc_compatible(result_to_calc_grid(unpacked))
    assert coerced[0] == ("d",)
    assert coerced[1][0] == "2026-06-25T00:00:00"
    assert coerced[2][0] == "2026-06-26T00:00:00"
    assert coerced[1][0] != 20629.0


def test_serialize_result_datetime64_ndarray_is_iso_not_unix_epoch():
    np = pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    from plugin.calc.python.addin_values import to_calc_compatible

    arr = np.array([np.datetime64("2026-06-25"), np.datetime64("2026-06-26")])
    out = to_calc_compatible(serialize_result(arr))
    assert out[0] == "2026-06-25T00:00:00"
    assert out[1] == "2026-06-26T00:00:00"


def test_serialize_result_timedelta64_ndarray_is_fractional_days():
    """timedelta64 must be fractional days on both sides of BINARY_MIN_CELLS."""
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import BINARY_MIN_CELLS, is_split_grid

    small = np.array([np.timedelta64(0, "D"), np.timedelta64(1, "D")])
    assert host_unpack_data(serialize_result(small)) == [0.0, 1.0]
    assert serialize_result(np.timedelta64(1, "D")) == 1.0

    large = np.arange(BINARY_MIN_CELLS).astype("timedelta64[D]")
    wire = serialize_result(large)
    assert is_split_grid(wire)
    back = host_unpack_data(wire)
    assert back[0] == 0.0
    assert back[1] == 1.0
    assert "day" not in str(back[1])

    cube = np.arange(8).astype("timedelta64[D]").reshape(2, 2, 2)
    assert host_unpack_data(serialize_result(cube)) == [
        [[0.0, 1.0], [2.0, 3.0]],
        [[4.0, 5.0], [6.0, 7.0]],
    ]

    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "result": serialize_result(small)})
    buf.seek(0)
    unpacked = read_pickle_frame(buf, require_dict=True)
    assert host_unpack_data(unpacked["result"]) == [0.0, 1.0]


def test_serialize_result_small_object_ndarray_is_host_pickleable():
    """An object ndarray under the threshold must coerce, not raise."""
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import BINARY_MIN_CELLS, is_split_grid

    small = np.array([np.int64(1), np.int64(2)], dtype=object)
    assert serialize_result(small) == [1, 2]
    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "result": serialize_result(small)})
    buf.seek(0)
    unpacked = read_pickle_frame(buf, require_dict=True)
    assert host_unpack_data(unpacked["result"]) == [1, 2]

    large = np.array([np.int64(i) for i in range(BINARY_MIN_CELLS)], dtype=object)
    wire = serialize_result(large)
    assert is_split_grid(wire)
    back = host_unpack_data(wire)
    assert back[0] == 0
    assert back[-1] == BINARY_MIN_CELLS - 1


def test_serialize_result_blank_multiindex_levels():
    pd = pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame(
        [[1, 2]],
        columns=pd.MultiIndex.from_tuples([("Region", ""), ("A", " x ")]),
    )
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["columns"] == ["Region", "A / x"]

    s = pd.Series([1], name=("", None))
    res2 = serialize_result(s)
    assert is_dataframe_payload(res2)
    assert res2["columns"] == ["value"]
    unpacked = host_unpack_data(res2)
    assert unpacked["columns"] == ["value"]


def test_serialize_result_flat_label_collisions_are_deduped():
    pd = pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame([[1, 2]], columns=[("A", ""), "A"])
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["columns"] == ["A", "A_1"]

    df2 = pd.DataFrame([[1, 2]], columns=[("", None), "value"])
    res2 = serialize_result(df2)
    assert res2["columns"] == ["value", "value_1"]


def test_serialize_result_multiindex_columns_and_categorical():
    """MultiIndex columns flatten to 'A / x'; row index is dropped; categoricals are labels."""
    pd = pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_tuples([("A", "x"), ("A", "y")]))
    res = serialize_result(df)
    assert is_dataframe_payload(res)
    assert res["columns"] == ["A / x", "A / y"]

    cat = pd.DataFrame({"c": pd.Categorical(["red", "blue", "red"])})
    unpacked = host_unpack_data(serialize_result(cat))
    assert unpacked["data"][0][0] == "red"
    assert unpacked["data"][1][0] == "blue"


def test_serialize_pil_image():
    """A PIL Image should be serialized to an image payload with format png."""
    Image = pytest.importorskip("PIL.Image")

    img = Image.new("RGB", (100, 100), color="red")
    res = serialize_result(img)
    assert res["__wa_payload__"] == "image"
    assert res["format"] == "png"
    assert isinstance(res["data"], bytes)


def test_serialize_dict_of_dataframes():
    """A dictionary containing DataFrames should serialize its DataFrame values correctly."""
    pd = pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload

    df = pd.DataFrame({"x": [1, 2]})
    res = serialize_result({"df_key": df})
    assert isinstance(res, dict)
    assert is_dataframe_payload(res["df_key"])


def test_serialize_container_with_ndarray():
    np = pytest.importorskip("numpy")

    arr = np.array([1, 2, 3])
    res = serialize_result([arr, {"k": (arr,)}])
    assert isinstance(res, list)
    assert host_unpack_data(res[0]) == [1, 2, 3]
    assert host_unpack_data(res[1]["k"][0]) == [1, 2, 3]


def test_serialize_set_and_frozenset_coercion():
    np = pytest.importorskip("numpy")

    res = serialize_result({np.int64(42), np.int64(43)})
    assert res == {42, 43}

    res_frozen = serialize_result([frozenset([np.int64(7)])])
    assert res_frozen == [frozenset([7])]


def test_serialize_numpy_scalar():
    np = pytest.importorskip("numpy")
    assert serialize_result(np.int64(7)) == 7


def test_execute_request_fresh_namespace():
    r1 = sandbox_run("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    assert r1["result"] == 42
    r2 = sandbox_run("result = x + 1", None)
    assert r2["status"] == "error"


def test_execute_request_injects_data():
    r = sandbox_run("result = float(np.sum(data))", [[1, 2, 3, 4]])
    assert r["status"] == "ok"
    assert r["result"] == 10.0


def test_execute_request_1x1_data_arithmetic_dag():
    r1 = sandbox_run("result = data + 3", [[2]])
    assert r1["status"] == "ok"
    assert r1["result"] == 5
    r2 = sandbox_run("result = data * 4", [[5.0]])
    assert r2["status"] == "ok"
    assert r2["result"] == 20.0


def test_execute_request_1x1_data_serialize_unwraps_to_scalar():
    r = sandbox_run("result = data", [[2]])
    assert r["status"] == "ok"
    assert r["result"] == 2
    assert not isinstance(r["result"], list)


def test_execute_request_injects_ranges_single_range():
    r = sandbox_run("result = (len(ranges), data is ranges[0], hasattr(data, 'to_pandas'))", [[1, 2, 3]])
    assert r["status"] == "ok"
    assert r["result"] == [1, True, True]


def test_execute_request_injects_ranges_multi_polymorphic_data():
    from plugin.calc.calc_addin_data import pack_calc_multi_data_for_wire

    wire = pack_calc_multi_data_for_wire([[[1.0, 2.0, 3.0]], [[4.0, 5.0]]], force="never")
    r = sandbox_run("result = (len(ranges), data is ranges, data[1].values[0][0])", wire)
    assert r["status"] == "ok"
    assert r["result"] == [2, True, 4.0]


def test_execute_request_does_not_inject_inputs():
    r = sandbox_run("result = inputs", [[1]])
    assert r["status"] == "error"
    assert "inputs" in r.get("message", "").lower() and "not defined" in r.get("message", "").lower()


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("result = math.sqrt(16)", 4.0, id="test_automatic_imports_math"),
        pytest.param("import math as my_math\nresult = my_math.sqrt(16)", 4.0, id="test_automatic_imports_already_imported"),
        pytest.param("import math\nresult = math.sqrt(25)", 5.0, id="test_automatic_imports_explicit"),
    ],
)
def test_automatic_imports_math(value, expected):
    r = sandbox_run(value, None)
    assert r["status"] == "ok"
    assert r["result"] == expected


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param("numpy", "result = float(np.sum([1, 2, 3]))", 6.0, id="test_automatic_imports_numpy"),
        pytest.param("sympy", "result = str(sp.Symbol('x'))", "x", id="test_automatic_imports_sympy"),
    ],
)
def test_automatic_imports_numpy(value, value_2, expected):
    pytest.importorskip(value)
    r = sandbox_run(value_2, None)
    assert r["status"] == "ok"
    assert r["result"] == expected

