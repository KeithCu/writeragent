# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.host_tool_call."""

from __future__ import annotations

import pickle
from unittest.mock import patch

import pytest

from plugin.scripting.host_tool_call import ROLE_WORKER, call_host_tool, set_process_role


def test_worker_call_sends_plain_python_for_numpy_args() -> None:
    np = pytest.importorskip("numpy")
    captured: list[tuple[str, dict]] = []

    def _fake_exchange(tool_name: str, args: dict) -> dict:
        captured.append((tool_name, args))
        return {"status": "ok"}

    set_process_role(ROLE_WORKER)
    try:
        with patch("plugin.framework.ipc.exchange_tool_call", side_effect=_fake_exchange):
            call_host_tool(
                "write_range",
                range=["A1"],
                values=np.array([[1, 2.5], [3, 4]]),
                extra=[np.float64(1.5), {"k": np.int64(2)}],
                obj=np.array([np.int64(3), "x"], dtype=object),
            )
    finally:
        set_process_role(None)

    assert len(captured) == 1
    tool_name, args = captured[0]
    assert tool_name == "write_range"
    assert args["values"] == [[1.0, 2.5], [3.0, 4.0]]
    assert type(args["values"][0]) is list
    assert type(args["values"][0][0]) is float
    assert args["extra"] == [1.5, {"k": 2}]
    assert type(args["extra"][0]) is float
    assert type(args["extra"][1]["k"]) is int
    assert args["obj"] == [3, "x"]
    assert type(args["obj"][0]) is int
    assert b"numpy" not in pickle.dumps(args, protocol=5)


def test_plain_args_when_optional_modules_missing() -> None:
    captured: list[tuple[str, dict]] = []

    def _fake_exchange(tool_name: str, args: dict) -> dict:
        captured.append((tool_name, args))
        return {"status": "ok"}

    set_process_role(ROLE_WORKER)
    try:
        with patch("plugin.scripting.venv.optional_import.optional_module", return_value=None), patch(
            "plugin.framework.ipc.exchange_tool_call", side_effect=_fake_exchange
        ):
            call_host_tool("read_range", range="A1:B2", count=3)
    finally:
        set_process_role(None)

    assert captured == [("read_range", {"range": "A1:B2", "count": 3})]


@pytest.mark.parametrize(
    ("arg_builder", "expected"),
    [
        (lambda _np, pd, _cr: pd.DataFrame({"a": [1, 2], "b": ["x", None]}), [["a", "b"], [1, "x"], [2, None]]),
        (lambda _np, pd, _cr: pd.DataFrame([[1, 2], [3, 4]]), [[1, 2], [3, 4]]),
        (
            lambda _np, pd, _cr: pd.DataFrame({"d": [pd.Timestamp("2026-01-15")]}),
            [["d"], ["2026-01-15T00:00:00"]],
        ),
        (lambda _np, pd, _cr: pd.Series([10, 20], name="s"), [["s"], [10], [20]]),
        (lambda _np, pd, _cr: pd.Series([10, 20]), [10, 20]),
        (lambda _np, _pd, cr: cr([[1, 2], [3, 4]]), [[1, 2], [3, 4]]),
        (lambda _np, _pd, cr: cr([[5]]), [[5]]),
        (lambda np, _pd, _cr: {np.str_("k"): 1}, {"k": 1}),
        (lambda np, _pd, _cr: {np.int64(7)}, [7]),
        (lambda _np, _pd, _cr: __import__("datetime").date(2026, 3, 4), "2026-03-04"),
        (lambda _np, _pd, _cr: __import__("decimal").Decimal("1.5"), 1.5),
        (lambda _np, _pd, _cr: range(3), [0, 1, 2]),
    ],
)
def test_worker_call_converts_rich_types_and_round_trips_safe_unpickler(arg_builder, expected) -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    from plugin.framework.ipc import unpack_pickle_frame
    from plugin.scripting.calc_range import CalcRange

    captured: list[tuple[str, dict]] = []

    def _fake_exchange(tool_name: str, args: dict) -> dict:
        captured.append((tool_name, args))
        return {"status": "ok"}

    val = arg_builder(np, pd, CalcRange)
    set_process_role(ROLE_WORKER)
    try:
        with patch("plugin.framework.ipc.exchange_tool_call", side_effect=_fake_exchange):
            call_host_tool("write_range", values=val)
    finally:
        set_process_role(None)

    assert len(captured) == 1
    _tool_name, args = captured[0]
    round_tripped = unpack_pickle_frame(pickle.dumps(args, protocol=5))
    assert round_tripped["values"] == expected


def test_worker_call_unsupported_object_raises_value_error_without_sending() -> None:
    class _Opaque:
        pass

    called = False

    def _fake_exchange(_tool_name: str, _args: dict) -> dict:
        nonlocal called
        called = True
        return {"status": "ok"}

    set_process_role(ROLE_WORKER)
    try:
        with patch("plugin.framework.ipc.exchange_tool_call", side_effect=_fake_exchange):
            with pytest.raises(ValueError, match=r"write_range.*values"):
                call_host_tool("write_range", values=_Opaque())
    finally:
        set_process_role(None)

    assert called is False
