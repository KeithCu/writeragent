# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor IPC protocol and session identity."""

from __future__ import annotations

from collections import UserDict
import io
from pathlib import Path
import pickle
import re
import struct

import pytest

from plugin.scripting.editor_ipc import (
    _TARGET_KEYS,
    PARENT_TO_CHILD_TYPES,
    normalize_target,
    read_message,
    stamp_session,
    target_from_load,
    target_identity_key,
    write_message,
)
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcPayloadSizeError,
    IpcTruncatedFrame,
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
    with pytest.raises(IpcTruncatedFrame):
        read_message(buf)


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


def test_write_message_reports_payload_byte_count():
    buf = io.BytesIO()
    msg = {"code": "x" * (DEFAULT_MAX_PAYLOAD_BYTES + 1)}
    with pytest.raises(ValueError, match=r"Pickle frame exceeds maximum payload size: \d+") as exc_info:
        write_message(buf, msg)
    assert isinstance(exc_info.value.__cause__, IpcPayloadSizeError)


def test_stamp_session_sets_id_mode_and_target_when_mode_is_set():
    msg = stamp_session({"type": "save", "code": "x"}, session_id="abc", mode="calc_cell", target={"cell_address": "A1"})
    assert msg["session_id"] == "abc"
    assert msg["mode"] == "calc_cell"
    assert msg["target"] == {"cell_address": "A1"}


def test_stamp_session_omits_empty_mode():
    msg = stamp_session({"type": "dirty"}, session_id="abc", mode="")
    assert msg["session_id"] == "abc"
    assert msg["target"] == {}
    assert "mode" not in msg


def test_stamp_session_keeps_message_mode_when_argument_empty():
    msg = stamp_session({"type": "dirty", "mode": "latex"}, session_id="abc", mode="")
    assert msg["mode"] == "latex"


def test_stamp_session_drops_empty_target_keys():
    msg = stamp_session({"type": "dirty"}, session_id="x", mode="run_script", target={"script_name": "foo", "doc_url": ""})
    assert msg["target"] == {"script_name": "foo"}


def test_stamp_session_copies_mapping_target():
    msg = stamp_session({"type": "dirty", "target": UserDict({"cell_address": "C3"})}, session_id="abc", mode="calc_cell")
    assert msg["target"] == {"cell_address": "C3"}


def test_normalize_target_drops_non_strings():
    class UnoObject:
        def __str__(self) -> str:
            return "uno:Object"

    # str() of a UNO object or other non-str must not become the identity.
    raw = {
        "cell_address": " A1 ",
        "script_name": "  ",
        "script_origin": None,
        "doc_url": 12,
        "resource": UnoObject(),
    }
    assert normalize_target(raw) == {"cell_address": "A1"}


def test_normalize_target_accepts_non_dict_mapping():
    assert normalize_target(UserDict({"cell_address": "A1", "nope": "x"})) == {"cell_address": "A1"}


def test_target_from_load_aliases_cell_and_script():
    t = target_from_load({"cell_address": "Sheet1.A1", "selected_script_name": "demo", "mode": "calc_cell"})
    assert t["cell_address"] == "Sheet1.A1"
    assert t["script_name"] == "demo"


def test_target_from_load_does_not_stringify_aliases():
    assert target_from_load({"cell_address": object(), "script_name": " demo "}) == {"script_name": "demo"}
    assert target_from_load({"target": {"cell_address": object()}, "cell_address": "B2"}) == {"cell_address": "B2"}


def test_stamp_session_accepts_long_text():
    """Script source is longer than the old deal caps."""
    from plugin.framework.deal_shim import DEAL_MAX_TOKEN

    code = "x" * (DEAL_MAX_TOKEN + 50)
    msg = stamp_session({"type": "save", "code": code}, session_id="abc", mode="run_script")
    assert msg["code"] == code


def test_target_identity_key_same_cell_matches():
    a = target_from_load({"mode": "calc_cell", "cell_address": "A1", "doc_url": "file:///x"})
    b = target_from_load({"mode": "calc_cell", "cell_address": "A1", "doc_url": "file:///x"})
    assert target_identity_key("calc_cell", a) == target_identity_key("calc_cell", b)
    c = target_from_load({"mode": "calc_cell", "cell_address": "B1", "doc_url": "file:///x"})
    assert target_identity_key("calc_cell", a) != target_identity_key("calc_cell", c)


def test_target_identity_key_distinguishes_script_origin():
    user_script = {"script_name": "helper.py", "script_origin": "user"}
    doc_script = {"script_name": "helper.py", "script_origin": "document"}
    assert target_identity_key("run_script", user_script) != target_identity_key("run_script", doc_script)


def test_target_identity_key_is_mode_plus_target_keys_in_order():
    target = {key: key.upper() for key in _TARGET_KEYS}
    assert target_identity_key("m", target) == ("m", *(key.upper() for key in _TARGET_KEYS))
    assert target_identity_key("m", {}) == ("m", *("" for _key in _TARGET_KEYS))


def test_target_from_load_selected_script_name_beats_script_name():
    assert (
        target_from_load({"selected_script_name": "from-selected", "script_name": "from-script"})["script_name"]
        == "from-selected"
    )


def test_target_from_load_nested_target_beats_aliases():
    assert (
        target_from_load({"target": {"script_name": "nested"}, "selected_script_name": "alias", "script_name": "top"})["script_name"]
        == "nested"
    )


def test_parent_to_child_types_match_js_handlers():
    repo_root = Path(__file__).resolve().parents[2]
    editor_js = repo_root / "plugin/contrib/scripting/assets/editor/editor_shell.js"
    scripts_manager_js = repo_root / "plugin/contrib/scripting/assets/editor/scripts_manager.js"
    backend_monaco_js = repo_root / "plugin/contrib/scripting/assets/editor/backend_monaco.js"

    # The module header is not CI; a handler added or renamed in those two files fails here.
    # On this tree the set is load, request_save, saved, error, theme, scripts_list.
    pattern = re.compile(r"""msg\.type\s*[!=]==\s*["']([A-Za-z0-9_]+)["']""")
    found: set[str] = set()
    for js_path in (editor_js, scripts_manager_js, backend_monaco_js):
        found.update(pattern.findall(js_path.read_text(encoding="utf-8")))

    assert found == set(PARENT_TO_CHILD_TYPES)
