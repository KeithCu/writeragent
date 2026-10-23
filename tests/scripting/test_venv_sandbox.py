# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-provided sandbox bindings (e.g. selected image bytes)."""

from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast

import pytest

from plugin.scripting.ipc import read_pickle_frame, write_pickle_frame
from plugin.scripting.payload_codec import host_unpack_data
from plugin.scripting.venv.venv_sandbox import (
    clear_all_sandbox_sessions,
    reset_sandbox_session,
    run_sandboxed_code,
    serialize_result,
)


@pytest.fixture(autouse=True)
def _reset_abandoned_timeout() -> Any:
    # Tip: ensure abandoned_timeout does not leak across sandbox tests.
    try:
        import plugin.contrib.smolagents.local_python_executor as lpe

        lpe.abandoned_timeout = False
    except ImportError:
        pass
    try:
        yield
    finally:
        try:
            import plugin.contrib.smolagents.local_python_executor as lpe

            lpe.abandoned_timeout = False
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

    What was wrong: both are BaseException. The vendored executor only
    catches Exception, so they left run_sandboxed_code, the warm worker
    died after EXEC_STARTED, and the host dropped every shared session.
    """
    sid = f"calc:base-exception-{label}"
    try:
        first = run_sandboxed_code("result = 7", session_id=sid, timeout_sec=8)
        assert first["status"] == "ok", first
        assert first["result"] == 7
        blown = run_sandboxed_code(code, session_id=sid, timeout_sec=8)
        assert blown["status"] == "error", blown
        assert label in blown.get("traceback", "")
        assert blown.get("code") != "USER_STOPPED"
        again = run_sandboxed_code("result = result + 1", session_id=sid, timeout_sec=8)
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
    from plugin.scripting.ipc import UserStopped

    def boom() -> None:
        raise UserStopped("Stopped by user.")

    code = "try:\n    boom()\n    result = 'kept going'\nexcept Exception:\n    result = 'caught'\n"
    out = run_sandboxed_code(code, bindings={"boom": boom}, timeout_sec=5)
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

    isolated = run_sandboxed_code("result = probe()", bindings={"probe": probe}, timeout_sec=8)
    assert isolated["status"] == "ok", isolated
    assert seen["sid"] is None
    assert seen["active"] is True

    sid = "calc:file:///timeout-fallback-workbook"
    try:
        shared = run_sandboxed_code(
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
    out = run_sandboxed_code(code, bindings={"image": b"png-bytes"})
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

    def _fake_run_sql(sql, con=None, files=None, scoped_dir=None, **kwargs):
        del sql, con, files, kwargs
        seen.append(scoped_dir)
        return 1

    monkeypatch.setattr("plugin.scripting.venv.duckdb_sql.run_sql", _fake_run_sql)
    sid = "calc:scoped-dir-leak"
    host = str(tmp_path)
    try:
        first = run_sandboxed_code(
            "scoped_dir = '/tmp/not-the-host'\nresult = run_sql('select 1', files=['a.csv'])",
            bindings={"scoped_dir": host},
            session_id=sid,
            timeout_sec=10,
        )
        assert first["status"] == "ok", first
        assert seen == [host]

        second = run_sandboxed_code(
            "result = run_sql('select 1', files=['a.csv'])",
            session_id=sid,
            timeout_sec=10,
        )
        assert second["status"] == "ok", second
        assert seen == [host, None]

        leaked = run_sandboxed_code("result = scoped_dir", session_id=sid, timeout_sec=10)
        assert leaked["status"] == "error", leaked

        other = str(tmp_path / "other")
        rebound = run_sandboxed_code(
            "scoped_dir = '/tmp/still-not-host'\nresult = run_sql('select 1', files=['a.csv'])",
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

    from plugin.scripting.venv.venv_sandbox import optional_module

    name = "writeragent_test_partial_mod"
    mod = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, loader=None)
    setattr(spec, "_initializing", True)
    mod.__spec__ = spec
    monkeypatch.setitem(sys.modules, name, mod)
    assert optional_module(name) is None


def test_serialize_result_rejects_self_referential_list():
    items: list[object] = []
    items.append(items)
    with pytest.raises(ValueError, match="self-referential"):
        serialize_result(items)


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
    from plugin.scripting.venv.venv_sandbox import _coerce_host_pickle_tree

    shared = [1]
    assert _coerce_host_pickle_tree([shared, shared], None) == [[1], [1]]


def test_serialize_result_set_of_range_falls_back_to_list():
    """range coerces to a list, which cannot live inside a set.

    What was wrong: the set comprehension raised TypeError and a successful
    value became an error frame.
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
    res1 = run_sandboxed_code("class Foo:\n    pass", timeout_sec=8)
    assert res1["status"] == "ok", res1
    assert res1["result"] is None

    res2 = run_sandboxed_code("class Foo:\n    pass\nresult = Foo", timeout_sec=8)
    assert res2["status"] == "ok", res2
    assert res2["result"] is None


def test_instance_result_errors_and_keeps_stdout():
    res = run_sandboxed_code(
        'print("hello world")\nclass Box:\n    pass\nbox = Box()\n',
        timeout_sec=8,
    )
    assert res["status"] == "error", res
    assert "cannot cross" in res.get("message", "")
    assert res.get("stdout") == "hello world\n"


def test_system_exit_keeps_stdout():
    res = run_sandboxed_code('print("hi")\nraise SystemExit(1)', timeout_sec=8)
    assert res["status"] == "error", res
    assert res.get("stdout") == "hi\n"


def test_module_expression_still_cannot_cross():
    res = run_sandboxed_code("import math\nresult = math", timeout_sec=8)
    assert res["status"] == "error", res
    assert "cannot cross" in res.get("message", "")


def test_timedelta64_without_pandas_is_fractional_days(monkeypatch: pytest.MonkeyPatch) -> None:
    np = pytest.importorskip("numpy")
    import datetime
    from plugin.scripting.venv import venv_sandbox as vs

    real_opt = vs.optional_module

    def fake_opt(name: str) -> Any:
        if name == "pandas":
            return None
        return real_opt(name)

    monkeypatch.setattr(vs, "optional_module", fake_opt)

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

    monkeypatch.setattr(vs, "optional_module", fake_opt)

    with pytest.raises(RuntimeError, match="backend gone"):
        vs._capture_open_figures_payload()
    assert closed == ["all"]


def test_run_sandboxed_code_allows_future_and_collections_abc() -> None:
    res1 = run_sandboxed_code("from __future__ import annotations\nresult = 1 + 1\n")
    assert res1["status"] == "ok"
    assert res1["result"] == 2

    res2 = run_sandboxed_code("from collections.abc import Mapping\nresult = issubclass(dict, Mapping)\n")
    assert res2["status"] == "ok"
    assert res2["result"] is True


def test_unpickleable_init_result_closes_open_figures() -> None:
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
        res = run_sandboxed_code(
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
        clear_all_sandbox_sessions()


def test_init_executor_omits_cell_bindings() -> None:
    from plugin.scripting.venv import venv_sandbox as vs

    try:
        res = run_sandboxed_code(
            "result = 1",
            data=None,
            bindings={"scoped_dir": "/tmp/wa-init-ns"},
            session_id="calc:init-namespace",
            init_script="HELPER = 1",
            init_session_id="calc:init-namespace:init",
            init_script_hash="ns",
        )
        assert res["status"] == "ok"
        init_state = vs._SESSION_EXECUTORS["calc:init-namespace:init"].state
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
    finally:
        clear_all_sandbox_sessions()


def test_shared_init_import_session_duckdb_shares_catalog() -> None:
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
    try:
        r1 = run_sandboxed_code(
            "result = HELPER",
            session_id=sid,
            init_script=init_script,
            init_session_id=init_sid,
            init_script_hash="h",
        )
        assert r1["status"] == "ok"
        assert r1["result"] == 3

        r2 = run_sandboxed_code(
            "result = session_duckdb().execute('SELECT n FROM init_marker').fetchone()[0]",
            session_id=sid,
            init_script=init_script,
            init_session_id=init_sid,
            init_script_hash="h",
        )
        assert r2["status"] == "ok"
        assert r2["result"] == 11
    finally:
        clear_all_sandbox_sessions()


def test_isolated_init_import_session_duckdb_does_not_share_catalog() -> None:
    pytest.importorskip("duckdb")
    from plugin.framework.uno_bootstrap import register_alias_importer

    register_alias_importer()
    init_script = (
        "from writeragent.scripting.duckdb_sql import session_duckdb\n"
        "session_duckdb().execute('CREATE TABLE init_marker AS SELECT 11 AS n')\n"
        "HELPER = 3\n"
    )
    try:
        r1 = run_sandboxed_code(
            "result = HELPER",
            session_id=None,
            init_script=init_script,
            init_session_id="isolated:init-duck:init",
            init_script_hash="h2",
        )
        assert r1["status"] == "ok"
        assert r1["result"] == 3

        r2 = run_sandboxed_code(
            "result = session_duckdb().execute('SELECT n FROM init_marker').fetchone()[0]",
            session_id=None,
            init_script=init_script,
            init_session_id="isolated:init-duck:init",
            init_script_hash="h2",
        )
        assert r2["status"] == "error"
        assert "init_marker" in str(r2.get("message", ""))
    finally:
        clear_all_sandbox_sessions()


def test_init_result_lambda_is_not_seeded() -> None:
    from plugin.scripting.venv.venv_sandbox import _new_executor, _seed_executor_from_init

    init_script = "def helper():\n    return 4\nresult = lambda: 99\n"
    init_sid = "isolated:skip-result:init"
    try:
        r1 = run_sandboxed_code(
            "result = helper()",
            session_id=None,
            init_script=init_script,
            init_session_id=init_sid,
            init_script_hash="s",
        )
        assert r1["status"] == "ok"
        assert r1["result"] == 4

        exe = _new_executor(5)
        _seed_executor_from_init(exe, init_sid, copy_values=True)
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
        r2 = run_sandboxed_code(
            check_code,
            session_id=None,
            init_script=init_script,
            init_session_id=init_sid,
            init_script_hash="s",
        )
        assert r2["status"] == "ok"
        assert r2["result"] == "absent"
    finally:
        clear_all_sandbox_sessions()


def test_send_cell_owned_rejects_unknown_name() -> None:
    from plugin.scripting.venv.venv_sandbox import (
        _INIT_STATE_SKIP_KEYS,
        _inject_data,
        _inject_excel_xl,
        _inject_session_duckdb,
        _new_executor,
        _send_cell_owned,
    )

    try:
        exe = _new_executor(5)
        with pytest.raises(RuntimeError, match="not_a_cell_name"):
            _send_cell_owned(exe, {"not_a_cell_name": 1})
        assert "not_a_cell_name" not in exe.state

        _send_cell_owned(exe, {"data": None, "ranges": []})
        assert exe.state.get("data") is None
        assert exe.state.get("ranges") == []

        _inject_data(exe, None)
        _inject_excel_xl(exe, ())
        _inject_session_duckdb(exe)

        for name in ("data", "ranges", "xl"):
            assert name in _INIT_STATE_SKIP_KEYS
            assert name in exe.state
    finally:
        clear_all_sandbox_sessions()


def test_isolated_init_lru_evicts_oldest_and_refreshes_on_reuse() -> None:
    from plugin.scripting.venv import venv_sandbox as vs

    try:
        for i in range(33):
            r = run_sandboxed_code(
                "result = 1",
                session_id=None,
                init_script=f"MARKER = {i}",
                init_session_id=f"isolated:{i}:init",
                init_script_hash=f"h{i}",
                timeout_sec=8,
            )
            assert r["status"] == "ok"

        assert "isolated:0:init" not in vs._SESSION_EXECUTORS
        assert "isolated:0:init" not in vs._INIT_SCRIPT_HASH
        assert "isolated:0:init" not in vs._ISOLATED_INIT_LRU

        assert "isolated:32:init" in vs._SESSION_EXECUTORS
        assert "isolated:32:init" in vs._INIT_SCRIPT_HASH
        assert "isolated:32:init" in vs._ISOLATED_INIT_LRU

        assert len(vs._ISOLATED_INIT_LRU) == 32

        # Re-run i == 1 with the same script and hash "h1" (early return, LRU refresh)
        r_refresh = run_sandboxed_code(
            "result = 1",
            session_id=None,
            init_script="MARKER = 1",
            init_session_id="isolated:1:init",
            init_script_hash="h1",
            timeout_sec=8,
        )
        assert r_refresh["status"] == "ok"

        # Then one more id isolated:33:init with MARKER = 33 and hash "h33"
        r_33 = run_sandboxed_code(
            "result = 1",
            session_id=None,
            init_script="MARKER = 33",
            init_session_id="isolated:33:init",
            init_script_hash="h33",
            timeout_sec=8,
        )
        assert r_33["status"] == "ok"

        assert "isolated:1:init" in vs._SESSION_EXECUTORS
        assert "isolated:1:init" in vs._ISOLATED_INIT_LRU

        assert "isolated:2:init" not in vs._SESSION_EXECUTORS
        assert "isolated:2:init" not in vs._INIT_SCRIPT_HASH
        assert "isolated:2:init" not in vs._ISOLATED_INIT_LRU

        assert len(vs._ISOLATED_INIT_LRU) == 32
    finally:
        clear_all_sandbox_sessions()


def test_isolated_init_lru_pops_non_isolated_id() -> None:
    from plugin.scripting.venv import venv_sandbox as vs

    try:
        clear_all_sandbox_sessions()
        vs._SESSION_EXECUTORS["weird:1"] = vs._new_executor(5)
        vs._SESSION_EXECUTORS["weird:1:init"] = vs._new_executor(5)
        vs._INIT_SCRIPT_HASH["weird:1:init"] = "h"
        vs._CELL_SESSION_INIT_DIGEST["weird:1"] = "h"

        vs._record_isolated_init_access_unlocked("weird:1:init")
        for i in range(32):
            vs._record_isolated_init_access_unlocked(f"isolated:lru{i}:init")

        assert "weird:1:init" not in vs._SESSION_EXECUTORS
        assert "weird:1" not in vs._SESSION_EXECUTORS
        assert "weird:1:init" not in vs._INIT_SCRIPT_HASH
        assert "weird:1:init" not in vs._ISOLATED_INIT_LRU
        assert "weird:1" not in vs._CELL_SESSION_INIT_DIGEST
        assert len(vs._ISOLATED_INIT_LRU) == 32
    finally:
        clear_all_sandbox_sessions()


def test_harness_calls_exit_on_abandoned_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import plugin.contrib.smolagents.local_python_executor as lpe
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    exit_calls: list[int] = []
    monkeypatch.setattr(harness.os, "_exit", lambda code: exit_calls.append(code))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
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

    assert exit_calls == [1]
    stdout.seek(0)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {"type": "exec_started", "id": "req-timeout"}
    err_frame = read_pickle_frame(stdout, require_dict=True)
    assert err_frame is not None
    assert err_frame["status"] == "error"
    assert "exceeded" in err_frame["message"]


def test_stdio_loop_calls_exit_on_abandoned_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import plugin.contrib.smolagents.local_python_executor as lpe
    from plugin.scripting.ipc import pack_pickle_frame, read_pickle_frame
    # compute_service/ is not in the OXT bundle that release_verify tests.
    ws = pytest.importorskip("compute_service.worker_stdio")

    exit_calls: list[int] = []
    monkeypatch.setattr(ws.os, "_exit", lambda code: exit_calls.append(code))
    monkeypatch.setattr(ws, "set_pdeathsig", lambda: True)

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


def test_sandbox_policy_socket_default_errors():
    res = run_sandboxed_code("import socket", sandbox_policy=None)
    assert res["status"] == "error"
    assert "Import of socket is not allowed" in res["message"]


def test_sandbox_policy_network_binds_socket():
    res = run_sandboxed_code(
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
    res = run_sandboxed_code(code, sandbox_policy={"files": True})
    assert res["status"] == "ok"
    assert res["result"] == "sandboxed-hello"
    import os
    assert not os.path.exists(target)

    res_sub = run_sandboxed_code("import subprocess\nsubprocess.run(['echo'])", sandbox_policy={"files": True})
    assert res_sub["status"] == "error"


def test_sandbox_policy_os_subprocess():
    import sys
    code = f"""
import subprocess
res = subprocess.run([{sys.executable!r}, "-c", "print(1)"], capture_output=True, text=True)
result = res.stdout.strip()
"""
    res = run_sandboxed_code(code, sandbox_policy={"os": True})
    assert res["status"] == "ok"
    assert res["result"] == "1"


def test_sandbox_policy_shared_session_rebuild():
    sid = "calc:test_policy_rebuild"
    try:
        r1 = run_sandboxed_code(
            "import socket\nx = 1\nresult = x",
            session_id=sid,
            sandbox_policy={"network": True},
        )
        assert r1["status"] == "ok"
        assert r1["result"] == 1

        r2 = run_sandboxed_code(
            "result = x",
            session_id=sid,
            sandbox_policy=None,
        )
        assert r2["status"] == "error"
        assert "not defined" in r2["message"] or "NameError" in r2["message"] or "InterpreterError" in r2["message"]

        r3 = run_sandboxed_code("import socket", session_id=sid, sandbox_policy=None)
        assert r3["status"] == "error"
        assert "Import of socket is not allowed" in r3["message"]
    finally:
        from plugin.scripting.venv.venv_sandbox import reset_sandbox_session
        reset_sandbox_session(sid)


def test_sandbox_policy_timeout_wins_over_socket_and_subprocess():
    import sys
    code_socket = """
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(10)
s.connect(('192.0.2.1', 80))
"""
    r_sock = run_sandboxed_code(code_socket, timeout_sec=1, sandbox_policy={"network": True})
    assert r_sock["status"] == "error"
    assert any(term in r_sock["message"].lower() for term in ("timeout", "timed out", "exceeded", "maximum execution time")) or r_sock.get("code") == "TIMEOUT"

    code_sub = f"""
import subprocess
subprocess.run([{sys.executable!r}, "-c", "import time; time.sleep(10)"])
"""
    r_sub = run_sandboxed_code(code_sub, timeout_sec=1, sandbox_policy={"os": True})
    assert r_sub["status"] == "error"
    assert any(term in r_sub["message"].lower() for term in ("timeout", "timed out", "exceeded", "maximum execution time")) or r_sub.get("code") == "TIMEOUT"



