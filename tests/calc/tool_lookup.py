# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Look up registered Calc tools by name in unit tests."""

from __future__ import annotations

from typing import Any

from plugin.app import get_tools


def calc_tool(name: str) -> Any:
    """The registered tool instance called *name*, or None when no tool has that name."""
    return get_tools().get(name)
