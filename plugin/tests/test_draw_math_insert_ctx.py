# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace

from plugin.modules.draw.math_insert import _uno_ctx_from_tool_ctx


def test_uno_ctx_from_tool_ctx_uses_inner_ctx() -> None:
    inner = object()
    assert _uno_ctx_from_tool_ctx(SimpleNamespace(ctx=inner)) is inner


def test_uno_ctx_from_tool_ctx_passes_bare_object() -> None:
    bare = object()
    assert _uno_ctx_from_tool_ctx(bare) is bare
