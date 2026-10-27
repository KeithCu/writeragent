# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-provided sandbox bindings (e.g. selected image bytes)."""

from __future__ import annotations

import io
import pathlib
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
    import sys
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
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", cast("Any", FakePyplot))

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
    assert err_frame["exiting"] is True


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


def test_sandbox_policy_process_spawning_blocked_by_default():
    """Verify that starting another process is blocked across all mechanisms when python_allow_os is OFF.

    With default policy (os=False), subprocess module import is blocked, os.system is missing from
    the restricted os proxy, and asyncio subprocess callables and ProcessPoolExecutor are blocked
    at the callable gate.
    """
    # 1. subprocess.run: blocked at import
    res = run_sandboxed_code("import subprocess\nsubprocess.run(['echo'])", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()

    # 2. os.system: not present on _FileOsProxy
    res = run_sandboxed_code("import os\nresult = os.system('true')", sandbox_policy=None)
    assert res["status"] == "error"

    # 3. asyncio subprocess: create_subprocess_exec and create_subprocess_shell blocked
    res = run_sandboxed_code(
        "import asyncio\nproc = asyncio.run(asyncio.create_subprocess_exec('echo', 'hi'))",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: create_subprocess_exec" in res.get("message", "")

    res = run_sandboxed_code(
        "import asyncio\nproc = asyncio.run(asyncio.create_subprocess_shell('echo hi'))",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: create_subprocess_shell" in res.get("message", "")

    # 4. ProcessPoolExecutor: blocked via from import and direct attribute access
    res = run_sandboxed_code(
        "from concurrent.futures import ProcessPoolExecutor\np = ProcessPoolExecutor(max_workers=1)",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: ProcessPoolExecutor" in res.get("message", "")

    res = run_sandboxed_code(
        "import concurrent.futures\np = concurrent.futures.ProcessPoolExecutor(max_workers=1)",
        sandbox_policy=None,
    )
    assert res["status"] == "error"
    assert "Forbidden access to function: ProcessPoolExecutor" in res.get("message", "")

    # 5. multiprocessing: blocked at import (bare, from, submodule)
    res = run_sandboxed_code("import multiprocessing", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()

    res = run_sandboxed_code("from multiprocessing import Process, Pool", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()

    res = run_sandboxed_code("import multiprocessing.pool", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()

    # 6. pty: blocked at import
    res = run_sandboxed_code("import pty", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()

    res = run_sandboxed_code("from pty import openpty", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()


def test_sandbox_policy_process_spawning_allowed_with_os_flag():
    """Verify that starting another process works across all mechanisms when python_allow_os is ON."""
    policy = {"os": True}

    # 1. subprocess.run: works
    code_sub = "import subprocess\nres = subprocess.run(['echo', 'hello'], capture_output=True, text=True)\nresult = res.stdout.strip()"
    res = run_sandboxed_code(code_sub, sandbox_policy=policy)
    assert res["status"] == "ok"
    assert res["result"] == "hello"

    # 2. os.system: works
    res = run_sandboxed_code("import os\nresult = os.system('true')", sandbox_policy=policy)
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
    res = run_sandboxed_code(code_aio_exec, sandbox_policy=policy)
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
    res = run_sandboxed_code(code_aio_shell, sandbox_policy=policy)
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
    res = run_sandboxed_code(code_pool, sandbox_policy=policy)
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
    res = run_sandboxed_code(code_mp_pool, sandbox_policy=policy)
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
    res = run_sandboxed_code(code_mp_proc, sandbox_policy=policy)
    assert res["status"] == "ok", res
    assert res["result"] is True

    # 7. pty: works
    code_pty = """
import os
import pty
master, slave = pty.openpty()
os.close(master)
os.close(slave)
result = (master > 0 and slave > 0)
"""
    res = run_sandboxed_code(code_pty, sandbox_policy=policy)
    assert res["status"] == "ok", res
    assert res["result"] is True


def test_sandbox_policy_os_harmless_reads_allowed_by_default():
    """Verify that harmless os reads (os.name, os.sep, os.linesep, os.path.join/basename) work by default."""
    import os
    code = """
import os
result = (os.name, os.sep, os.linesep, os.path.join('a', 'b'), os.path.basename('/x/y'))
"""
    res = run_sandboxed_code(code, sandbox_policy=None)
    assert res["status"] == "ok"
    assert res["result"] == [os.name, os.sep, os.linesep, os.path.join('a', 'b'), 'y']


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





def test_extra_modules_override_blocklist() -> None:
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    res = run_sandboxed_code(
        "import socket\nresult = callable(socket.socket)",
        sandbox_policy={"extra_modules": "socket"},
    )
    assert res.get("status") == "ok", res
    code = "import subprocess, sys\nresult = subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True).returncode"
    res = run_sandboxed_code(code, sandbox_policy={"extra_modules": "subprocess, sys"})
    assert res.get("status") == "ok", res
    assert res.get("result") == 0


@pytest.mark.parametrize(
    "mod",
    ["importlib", "builtins", "pickle", "subprocess", "pty", "multiprocessing", "marshal"],
)
def test_sandbox_runtime_default_blocks_host_escape(mod: str) -> None:
    res = run_sandboxed_code(f"import {mod}", sandbox_policy=None)
    assert res["status"] == "error"
    assert "not allowed" in res.get("message", "").lower()


def test_sandbox_runtime_blocks_internal_plugin_and_writeragent_framework() -> None:
    for mod in ("plugin.framework.config", "writeragent.framework.config"):
        res = run_sandboxed_code(f"import {mod}", sandbox_policy=None)
        assert res["status"] == "error"
        assert "not allowed" in res.get("message", "").lower()


def test_sandbox_runtime_reexported_os_proxy() -> None:
    # default: read-only _FileOsProxy (no system, remove forbidden)
    res_def = run_sandboxed_code(
        "from random import _os\nresult = (hasattr(_os, 'system'), hasattr(_os, 'remove'))",
        sandbox_policy=None,
    )
    assert res_def["status"] == "ok"
    assert res_def["result"] == [False, True]

    res_remove_def = run_sandboxed_code(
        "from random import _os\n_os.remove('nonexistent_file_xyz')",
        sandbox_policy=None,
    )
    assert res_remove_def["status"] == "error"
    assert "Forbidden access to function: remove" in res_remove_def.get("message", "")

    # files=True: _FileOsProxy with writes (remove present, no system)
    res_files = run_sandboxed_code(
        "from random import _os\nresult = (hasattr(_os, 'system'), callable(_os.remove))",
        sandbox_policy={"files": True},
    )
    assert res_files["status"] == "ok"
    assert res_files["result"] == [False, True]

    # os=True: raw os module (callable(_os.system) is True)
    res_os = run_sandboxed_code(
        "from random import _os\nresult = callable(_os.system)",
        sandbox_policy={"os": True},
    )
    assert res_os["status"] == "ok"
    assert res_os["result"] is True


def test_sandbox_runtime_requests_network_gate() -> None:
    res_def = run_sandboxed_code("import requests", sandbox_policy=None)
    assert res_def["status"] == "error"
    assert "not allowed" in res_def.get("message", "").lower()

    res_net = run_sandboxed_code(
        "import requests\nresult = bool(hasattr(requests, 'get'))",
        sandbox_policy={"network": True},
    )
    assert res_net["status"] == "ok"
    assert res_net["result"] is True


def test_sandbox_runtime_pathlib_allowed_default_and_files() -> None:
    res_def = run_sandboxed_code(
        "import pathlib\nresult = pathlib.Path('.').exists()",
        sandbox_policy=None,
    )
    assert res_def["status"] == "ok"
    assert res_def["result"] is True

    res_files = run_sandboxed_code(
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
    res = run_sandboxed_code(f"import {mod}\nresult = True", sandbox_policy=None)
    assert res["status"] == "ok"
    assert res["result"] is True


@pytest.mark.parametrize("mod", ["shutil", "tempfile", "sqlite3"])
def test_sandbox_runtime_file_blocked_modules(mod: str) -> None:
    res_def = run_sandboxed_code(f"import {mod}", sandbox_policy=None)
    assert res_def["status"] == "error"
    assert "not allowed" in res_def.get("message", "").lower()

    res_files = run_sandboxed_code(f"import {mod}\nresult = True", sandbox_policy={"files": True})
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


def _file_fixture(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, dict[str, str]]:
    p, src, d = tmp_path / "out", tmp_path / "src.txt", tmp_path / "empty"
    target = tmp_path / "target.txt"
    target.write_text("fixture")
    target.chmod(0o644)
    src.symlink_to(target)
    d.mkdir()
    return p, src, d, {"p": repr(str(p)), "src": repr(str(src)), "d": repr(str(d))}


@pytest.mark.parametrize("code", _FILE_WRITES)
def test_sandbox_runtime_file_writes_blocked_unless_files(code: str, tmp_path: pathlib.Path) -> None:
    p, src, d, names = _file_fixture(tmp_path)
    res = run_sandboxed_code(code.format(**names), sandbox_policy=None)
    assert res["status"] == "error"
    assert "Forbidden access to function" in res.get("message", "")
    assert not p.exists() and not p.is_symlink()
    assert src.read_text() == "fixture" and src.stat().st_mode & 0o777 == 0o644
    assert d.is_dir()

    res_files = run_sandboxed_code(code.format(**names) + "\nresult = True", sandbox_policy={"files": True})
    assert res_files["status"] == "ok", res_files


def test_sandbox_runtime_file_writes_allowed_with_os_policy(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "os.txt"
    res = run_sandboxed_code(f"from pathlib import Path\nPath({str(p)!r}).write_text('ok')", sandbox_policy={"os": True})
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
    res = run_sandboxed_code(code.format(**names), sandbox_policy=None)
    assert res["status"] == "error"
    if "open({src}).read()" in code:
        assert (
            "name 'open' is not defined" in res.get("message", "")
            or "Forbidden function evaluation: 'open'" in res.get("message", "")
        )
    else:
        assert "Forbidden access to function" in res.get("message", "")

    res_files = run_sandboxed_code(code.format(**names), sandbox_policy={"files": True})
    assert res_files["status"] == "ok", res_files
    assert res_files["result"] == "fixture"


def test_sandbox_runtime_file_reads_allowed_with_os_policy(tmp_path: pathlib.Path) -> None:
    _p, src, _d, _names = _file_fixture(tmp_path)
    res = run_sandboxed_code(f"from pathlib import Path\nresult = Path({str(src)!r}).read_text()", sandbox_policy={"os": True})
    assert res["status"] == "ok"
    assert res["result"] == "fixture"


def test_sandbox_runtime_safe_in_memory_allowed_without_file_policy(tmp_path: pathlib.Path) -> None:
    _p, src, _d, _names = _file_fixture(tmp_path)

    res_io = run_sandboxed_code(
        "import io\ns = io.StringIO('fixture').read()\nb = io.BytesIO(b'fixture').read()\nresult = s == 'fixture' and b == b'fixture'",
        sandbox_policy=None,
    )
    assert res_io["status"] == "ok" and res_io["result"] is True

    res_gz = run_sandboxed_code(
        "import gzip\nresult = gzip.decompress(gzip.compress(b'fixture')).decode()",
        sandbox_policy=None,
    )
    assert res_gz["status"] == "ok" and res_gz["result"] == "fixture"

    res_zip = run_sandboxed_code(
        "import io, zipfile\nbuf = io.BytesIO()\nwith zipfile.ZipFile(buf, 'w') as zf:\n    zf.writestr('x', 'fixture')\nwith zipfile.ZipFile(buf, 'r') as zf:\n    result = zf.read('x').decode()",
        sandbox_policy=None,
    )
    assert res_zip["status"] == "ok" and res_zip["result"] == "fixture"

    res_gz_buf = run_sandboxed_code(
        "import io, gzip\nbuf = io.BytesIO()\nwith gzip.open(buf, 'wb') as f:\n    f.write(b'fixture')\nwith gzip.open(io.BytesIO(buf.getvalue()), 'rb') as f:\n    result = f.read().decode()",
        sandbox_policy=None,
    )
    assert res_gz_buf["status"] == "ok" and res_gz_buf["result"] == "fixture"

    res_exists = run_sandboxed_code(
        f"import os\nfrom pathlib import Path\nresult = os.path.exists({repr(str(src))}) and os.path.isfile({repr(str(src))}) and Path({repr(str(src))}).exists()",
        sandbox_policy=None,
    )
    assert res_exists["status"] == "ok" and res_exists["result"] is True

    res_glob = run_sandboxed_code("import glob\nresult = glob.escape('[*]')", sandbox_policy=None)
    assert res_glob["status"] == "ok" and res_glob["result"] in (r"\[\*\]", "[[][*]]")


def test_sandbox_runtime_extra_modules_allows_importlib() -> None:
    res = run_sandboxed_code(
        "import importlib\nresult = bool(hasattr(importlib, 'import_module'))",
        sandbox_policy={"extra_modules": "importlib"},
    )
    assert res["status"] == "ok"
    assert res["result"] is True


def test_inject_auto_imports_ignores_nested_scope_bindings() -> None:
    from plugin.scripting.config_limits import python_exec_timeout_default
    from plugin.scripting.venv.venv_sandbox import (
        _new_executor,
        inject_auto_imports,
        optional_module,
    )

    executor = _new_executor(python_exec_timeout_default())
    inject_auto_imports(executor, "def f():\n    dt = 1\nresult = dt")
    assert "dt" in executor.state
    res = executor("def g():\n    dt = 1\nresult = dt.date(2020, 1, 1).isoformat()")
    assert res.output == "2020-01-01"

    executor_class = _new_executor(python_exec_timeout_default())
    inject_auto_imports(executor_class, "class C:\n    dt = 1\nresult = 1")
    assert "dt" in executor_class.state

    executor2 = _new_executor(python_exec_timeout_default())
    inject_auto_imports(executor2, "def f():\n    import numpy as np\nresult = 1")
    if optional_module("numpy") is not None:
        assert "np" in executor2.state


def test_apply_auto_imports_skips_shadowed_alias() -> None:
    from plugin.scripting.venv.venv_sandbox import apply_auto_imports

    code1, _ = apply_auto_imports("dt = 123\nresult = dt")
    assert "import datetime as dt" not in code1

    code2, _ = apply_auto_imports("def f():\n    import datetime as dt\nresult = 1")
    assert "import datetime as dt" in code2


def test_apply_auto_imports_parses_source_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.scripting.venv.venv_sandbox as vs

    real = vs.ast.parse
    count = 0

    def counting_parse(*args: Any, **kwargs: Any) -> vs.ast.AST:
        nonlocal count
        count += 1
        return real(*args, **kwargs)

    vs.apply_auto_imports("result = 1")
    monkeypatch.setattr(vs.ast, "parse", counting_parse)
    count = 0
    vs.apply_auto_imports("result = 1")
    assert count == 1

    code, lines = vs.apply_auto_imports("def ")
    assert lines > 0
    assert "import " in code


def test_run_sandboxed_code_rejects_non_numeric_timeout() -> None:
    res = run_sandboxed_code("result = 1", timeout_sec=cast("Any", "soon"))
    assert res["status"] == "error"
    assert "timeout" in res["message"].lower()


def test_run_sandboxed_code_rejects_non_dict_sandbox_policy() -> None:
    res = run_sandboxed_code("result = 1", sandbox_policy=cast("Any", [("extra_modules", "x")]))
    assert res["status"] == "error"


def test_run_sandboxed_code_rejects_non_string_extra_modules() -> None:
    res = run_sandboxed_code("result = 1", sandbox_policy={"extra_modules": [1]})
    assert res["status"] == "error"
