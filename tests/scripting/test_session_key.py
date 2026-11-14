# WriterAgent - LibreOffice Extension
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SessionKey and session key helpers."""

from __future__ import annotations

import pytest

from plugin.scripting.session_key import (
    SessionKey,
    SessionKind,
    is_reserved_caller_id,
    is_unsaved_document_key,
    new_unsaved_document_key,
)
from plugin.scripting.sandbox import build_sandbox_policy
from plugin.scripting.venv.duckdb_sql import persistable_duckdb_session_id
from plugin.scripting.venv.venv_sandbox import SessionStore


@pytest.mark.parametrize(
    "text",
    [
        "calc:file:///C:/Users/a.ods",
        "calc:file:///tmp/foo:init",
        "rps:file:///tmp/foo:init",
        "notebook:doc:init",
        "calc:wb:init",
        "calc:",
        "calc::init",
        ":init",
        "ppt_master:active",
        "doc:uuid:extra",
        "documentary:x",
        "online-wb",
        "online-wb:init",
        "writeragent:sql",
        "sess_xyz",
        "other:foo",
        "  calc:wb:init  ",
        "isolated:abc",
    ],
)
def test_parse_wire_round_trip(text: str) -> None:
    assert SessionKey.parse(text).wire() == text


def test_calc_windows_url_rest_first_colon_only() -> None:
    k = SessionKey.parse("calc:file:///C:/Users/a.ods")
    assert k.kind is SessionKind.CALC
    assert k.rest == "file:///C:/Users/a.ods"
    assert k.document_lookup_key() == "file:///C:/Users/a.ods"


def test_calc_init_companion_and_duckdb() -> None:
    k = SessionKey.parse("calc:file:///tmp/foo:init")
    assert k.init is True
    assert k.document_lookup_key() == "file:///tmp/foo"
    assert k.with_init_suffix().wire() == "calc:file:///tmp/foo:init"
    assert persistable_duckdb_session_id("calc:file:///tmp/foo:init") == "calc:file:///tmp/foo"


def test_rps_init_lookup_strips_init_flag() -> None:
    k = SessionKey.parse("rps:file:///tmp/foo:init")
    assert k.init is True
    assert k.document_lookup_key() == "file:///tmp/foo"
    assert persistable_duckdb_session_id("rps:file:///tmp/foo:init") == "rps:file:///tmp/foo"


def test_notebook_and_calc_init_duckdb_and_lookup() -> None:
    assert persistable_duckdb_session_id("notebook:doc:init") == "notebook:doc"
    assert persistable_duckdb_session_id("calc:wb:init") == "calc:wb"
    assert SessionKey.parse("calc:wb:init").document_lookup_key() == "wb"
    assert SessionKey.parse("notebook:doc:init").document_lookup_key() == "doc"


def test_stacked_init_suffix_round_trip() -> None:
    text = "calc:file:///tmp/foo:init:init"
    assert SessionKey.parse(text).wire() == text


def test_kind_only_init_wire_parses_other_with_init_flag() -> None:
    """``calc:init`` has no document key; init strip yields OTHER, not CALC(rest=\"init\")."""
    k = SessionKey.parse("calc:init")
    assert k.kind is SessionKind.OTHER
    assert k.rest == "calc"
    assert k.init is True
    assert k.wire() == "calc:init"
    assert is_reserved_caller_id("calc:init") is True


def test_with_init_suffix_idempotent() -> None:
    k = SessionKey.parse("calc:wb:init")
    assert k.with_init_suffix().wire() == "calc:wb:init"


def test_empty_rest_calc_and_init_lookup_and_duckdb_none() -> None:
    assert SessionKey.parse("calc:").document_lookup_key() is None
    assert SessionKey.parse("calc::init").document_lookup_key() is None
    assert SessionKey.parse(":init").document_lookup_key() is None

    assert persistable_duckdb_session_id("calc:") is None
    assert persistable_duckdb_session_id("calc::init") is None
    assert persistable_duckdb_session_id(":init") is None

    # :init pops no sandbox cell
    store = SessionStore()
    exe, created = store.get_or_create("", policy=build_sandbox_policy())
    assert created is True
    assert store.reset(":init")["status"] == "ok"
    assert store.executor("") is exe


def test_ppt_master_active_lookup_not_persistable_default_wire() -> None:
    k = SessionKey.parse("ppt_master:active")
    assert k.document_lookup_key() == "active"
    assert persistable_duckdb_session_id("ppt_master:active") is None
    assert SessionKey.of(SessionKind.PPT_MASTER, "default").wire() == "ppt_master:default"


def test_doc_pin_key_whole_string() -> None:
    k = SessionKey.parse("doc:uuid:extra")
    assert k.kind is SessionKind.DOC
    assert k.rest == "uuid:extra"
    assert k.wire() == "doc:uuid:extra"
    assert k.document_lookup_key() is None


@pytest.mark.parametrize(
    "raw",
    [
        "documentary:x",
        "online-wb",
        "online-wb:init",
        "writeragent:sql",
        "sess_xyz",
        "other:foo",
    ],
)
def test_other_kinds_round_trip(raw: str) -> None:
    k = SessionKey.parse(raw)
    assert k.kind is SessionKind.OTHER
    assert k.wire() == raw
    assert k.document_lookup_key() is None


def test_whitespace_padded_calc_wb_init() -> None:
    # DuckDB strips outer whitespace
    assert persistable_duckdb_session_id("  calc:wb:init  ") == "calc:wb"
    # Document lookup does not strip
    k = SessionKey.parse("  calc:wb:init  ")
    assert k.kind is SessionKind.OTHER
    assert k.document_lookup_key() is None


@pytest.mark.parametrize(
    "sid",
    [
        "sess:init",
        "isolated:abc",
        "foo:init",
        "isolated:",
        "calc",
        "embeddings",
        "doc",
        "isolated",
    ],
)
def test_reserved_caller_ids(sid: str) -> None:
    assert is_reserved_caller_id(sid) is True


def test_validate_session_id_rejects_padded_init_suffix() -> None:
    import os
    import sys

    try:
        from compute_service.json_forward import validate_session_id, ExecuteRequestError
    except ModuleNotFoundError:
        # Stripped release-verify tree has plugin/ + tests/ only; checkout is in env.
        root = os.environ.get("WRITERAGENT_PROJECT_ROOT", "").strip()
        if root and root not in sys.path:
            sys.path.insert(0, root)
        try:
            from compute_service.json_forward import validate_session_id, ExecuteRequestError
        except ModuleNotFoundError:
            pytest.skip("compute_service not in stripped release tree")

    with pytest.raises(ExecuteRequestError) as exc_info:
        validate_session_id("foo:init ")
    assert exc_info.value.code == "INVALID_SESSION_ID"


@pytest.mark.parametrize(
    "sid",
    [
        "my_session",
        "ISOLATED:abc",
        "calc:wb",
    ],
)
def test_not_reserved_caller_ids(sid: str) -> None:
    assert is_reserved_caller_id(sid) is False


def test_isolated_with_init_suffix() -> None:
    assert SessionKey.of(SessionKind.ISOLATED, "ab").with_init_suffix().wire() == "isolated:ab:init"
    assert SessionKey.parse("isolated:ab:init").init is True


def test_other_raises_and_hash_equality() -> None:
    with pytest.raises(ValueError, match="OTHER is parse-only"):
        SessionKey.of(SessionKind.OTHER, "x")

    k1 = SessionKey.parse("calc:wb")
    k2 = SessionKey.parse("calc:wb")
    assert hash(k1) == hash(k2)
    assert k1 == k2
    assert len({k1, k2}) == 1


def test_unsaved_document_key_helpers() -> None:
    key = new_unsaved_document_key()
    assert is_unsaved_document_key(key)
    assert is_unsaved_document_key("unsaved:123")
    assert not is_unsaved_document_key("calc:123")
