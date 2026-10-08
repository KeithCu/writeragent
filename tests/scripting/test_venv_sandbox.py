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

import pytest

from plugin.scripting.ipc import read_pickle_frame, write_pickle_frame
from plugin.scripting.payload_codec import host_unpack_data
from plugin.scripting.venv.venv_sandbox import reset_sandbox_session, run_sandboxed_code, serialize_result


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
        assert host_unpack_data(unpacked["result"]) == expected


def test_pandas_timestamp_round_trips_as_iso():
    pd = pytest.importorskip("pandas")
    wire = serialize_result(pd.Timestamp("2026-08-13 14:30"))
    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "result": wire})
    buf.seek(0)
    unpacked = read_pickle_frame(buf, require_dict=True)
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


def test_clongdouble_scalar_names_the_type():
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match=r"clongdouble.*builtin complex"):
        serialize_result(np.clongdouble(1 + 2j))
