# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor IPC protocol and failure formatting."""

from __future__ import annotations

import io
import pickle
import struct

import pytest

from plugin.scripting.editor_ipc import (
    exception_traceback,
    failure_detail,
    failure_message,
    read_message,
    stamp_session,
    target_from_load,
    target_identity_key,
    write_message,
)


def test_roundtrip_simple():
    buf = io.BytesIO()
    write_message(buf, {"type": "ready", "version": 1})
    buf.seek(0)
    msg = read_message(buf)
    assert msg == {"type": "ready", "version": 1}


def test_roundtrip_unicode():
    buf = io.BytesIO()
    write_message(buf, {"type": "load", "code": "print('日本語')"})
    buf.seek(0)
    msg = read_message(buf)
    assert msg is not None
    assert msg["code"] == "print('日本語')"


def test_eof_returns_none():
    buf = io.BytesIO()
    assert read_message(buf) is None


def test_truncated_payload_returns_none():
    buf = io.BytesIO()
    buf.write(struct.pack("!I", 100))
    buf.write(b"short")
    buf.seek(0)
    assert read_message(buf) is None


def test_invalid_size_raises():
    buf = io.BytesIO()
    buf.write(struct.pack("!I", 32 * 1024 * 1024))
    buf.seek(0)
    with pytest.raises(ValueError, match="Invalid editor message size"):
        read_message(buf)


def test_invalid_pickle_raises():
    buf = io.BytesIO()
    buf.write(struct.pack("!I", 8))
    buf.write(b"notpickl")
    buf.seek(0)
    with pytest.raises(ValueError, match="Invalid editor message pickle"):
        read_message(buf)


def test_non_dict_pickle_raises():
    buf = io.BytesIO()
    payload = pickle.dumps(["not", "a", "dict"], protocol=5)
    buf.write(struct.pack("!I", len(payload)))
    buf.write(payload)
    buf.seek(0)
    with pytest.raises(ValueError, match="Editor message must be a dict"):
        read_message(buf)


def test_exception_traceback_includes_frame():
    try:
        raise ValueError("probe failure")
    except ValueError as e:
        tb = exception_traceback(e)
    assert "ValueError: probe failure" in tb
    assert "test_exception_traceback_includes_frame" in tb


def test_failure_message_combines_summary_and_detail():
    msg = failure_message("Summary", detail="stderr line")
    assert msg.startswith("Summary\n\n")
    assert "stderr line" in msg


def test_failure_message_accepts_none_detail():
    msg = failure_message("Summary", detail=None)
    assert msg == "Summary"


def test_failure_message_empty_summary():
    assert failure_message("", detail="", exc=None) == ""


def test_failure_detail_rejects_exc_when_deal_present():
    """exc is None in the pre so CrossHair cannot format_exception a symbolic error."""
    import deal
    from tests.strip_bundle import deal_pre_present

    if not deal_pre_present(failure_detail):
        pytest.skip("@deal.pre stripped in release bundle")
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        with pytest.raises(deal.PreContractError):
            failure_detail(detail="stderr line", exc=e)
        with pytest.raises(deal.PreContractError):
            failure_message("Summary", detail=None, exc=e)
        tb = exception_traceback(e)
        assert "RuntimeError: boom" in tb
    assert failure_detail(detail="stderr line", exc=None) == "stderr line"


def test_stamp_session_always_sends_id_mode_and_target():
    msg = stamp_session({"type": "save", "code": "x"}, session_id="abc", mode="calc_cell", target={"cell_address": "A1"})
    assert msg["session_id"] == "abc"
    assert msg["mode"] == "calc_cell"
    assert msg["target"] == {"cell_address": "A1"}


def test_stamp_session_drops_empty_target_keys():
    msg = stamp_session({"type": "dirty"}, session_id="x", mode="run_script", target={"script_name": "foo", "doc_url": ""})
    assert msg["target"] == {"script_name": "foo"}


def test_target_from_load_aliases_cell_and_script():
    t = target_from_load({"cell_address": "Sheet1.A1", "selected_script_name": "demo", "mode": "calc_cell"})
    assert t["cell_address"] == "Sheet1.A1"
    assert t["script_name"] == "demo"


def test_target_identity_key_same_cell_matches():
    a = target_from_load({"mode": "calc_cell", "cell_address": "A1", "doc_url": "file:///x"})
    b = target_from_load({"mode": "calc_cell", "cell_address": "A1", "doc_url": "file:///x"})
    assert target_identity_key("calc_cell", a) == target_identity_key("calc_cell", b)
    c = target_from_load({"mode": "calc_cell", "cell_address": "B1", "doc_url": "file:///x"})
    assert target_identity_key("calc_cell", a) != target_identity_key("calc_cell", c)
