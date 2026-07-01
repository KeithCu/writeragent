# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve WriterAgent's bundled ppt-master SKILL.md fork."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_BUNDLED_SKILL = Path(__file__).resolve().parent / "skill" / "SKILL.md"


def bundled_skill_md_path() -> Path:
    return _BUNDLED_SKILL


def resolve_writeragent_skill_md(ctx: Any | None = None) -> Path:
    """WriterAgent fork under contrib; fallback to data-root SKILL.md if fork missing."""
    if _BUNDLED_SKILL.is_file():
        return _BUNDLED_SKILL
    from plugin.ppt_master.paths import resolve_data_root

    return resolve_data_root(ctx) / "SKILL.md"
