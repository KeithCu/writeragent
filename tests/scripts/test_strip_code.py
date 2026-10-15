# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for strip_code.py release stubs and stripping routines."""

from __future__ import annotations

import inspect

import plugin.framework.thread_guard as live_tg
from scripts.strip_code import THREAD_GUARD_RELEASE_STUB


def test_thread_guard_release_stub_public_surface() -> None:
    stub_ns: dict[str, object] = {}
    exec(THREAD_GUARD_RELEASE_STUB, stub_ns)

    assert stub_ns["__all__"] == live_tg.__all__

    for fn_name in ("_wrap_uno", "_unwrap_uno", "on_main_thread", "sync_host_dispatch"):
        stub_fn = stub_ns[fn_name]
        live_fn = getattr(live_tg, fn_name)
        stub_params = [(p.name, p.kind) for p in inspect.signature(stub_fn).parameters.values()]  # type: ignore[arg-type]
        live_params = [(p.name, p.kind) for p in inspect.signature(live_fn).parameters.values()]
        assert stub_params == live_params, f"Signature mismatch for {fn_name}: {stub_params} != {live_params}"
