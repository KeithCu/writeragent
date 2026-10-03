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
from plugin.scripting.venv.venv_sandbox import run_sandboxed_code, serialize_result


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


def test_unknown_result_type_stays_in_the_child():
    class Weird:
        pass

    with pytest.raises(ValueError, match="pickle boundary"):
        serialize_result(Weird())
