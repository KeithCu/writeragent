# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for host-provided sandbox bindings (e.g. selected image bytes)."""

from __future__ import annotations

from plugin.scripting.venv.venv_sandbox import run_sandboxed_code


def test_run_sandboxed_code_injects_bindings():
    code = "result = image"
    out = run_sandboxed_code(code, bindings={"image": b"png-bytes"})
    assert out["status"] == "ok"
    assert out["result"] == b"png-bytes"
