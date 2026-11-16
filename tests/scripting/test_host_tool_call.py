# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.host_tool_call."""

from __future__ import annotations

import pickle
import sys
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


def test_no_numpy_module_leaves_args_untouched() -> None:
    captured: list[tuple[str, dict]] = []

    def _fake_exchange(tool_name: str, args: dict) -> dict:
        captured.append((tool_name, args))
        return {"status": "ok"}

    set_process_role(ROLE_WORKER)
    try:
        with patch.dict(sys.modules, {"numpy": None}), patch(
            "plugin.framework.ipc.exchange_tool_call", side_effect=_fake_exchange
        ):
            call_host_tool("read_range", range="A1:B2", count=3)
    finally:
        set_process_role(None)

    assert captured == [("read_range", {"range": "A1:B2", "count": 3})]
