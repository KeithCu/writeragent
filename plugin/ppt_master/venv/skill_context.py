# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Load ppt-master SKILL.md and routing files for venv sub-agent system prompt."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_DEFAULT_SKILL_CAP = 80_000

_EXTRA_FILES = (
    "workflows/routing.md",
    "workflows/index.md",
    "references/artifact-ownership.md",
)


def _read_capped(path: Path, cap: int) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n\n[truncated at {cap} chars — use read_ppt_master_workflow_file for more]\n"


def resolve_data_root_from_env() -> Path:
    raw = os.environ.get("PPT_MASTER_DATA_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("skills/ppt-master")


def load_skill_context(*, cap: int = _DEFAULT_SKILL_CAP) -> dict[str, Any]:
    """Build system-prompt block from upstream skill tree (Level-2 load)."""
    root = resolve_data_root_from_env()
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        return {
            "ok": False,
            "data_root": str(root),
            "block": (
                f"PPT-Master data root missing or incomplete: {root}\n"
                "Configure Settings → Python → PPT-Master data path and clone upstream ppt-master."
            ),
        }

    parts: list[str] = [f"[PPT-MASTER SKILL from {root}]\n", _read_capped(skill_path, cap)]
    remaining = max(0, cap - sum(len(p) for p in parts))
    for rel in _EXTRA_FILES:
        if remaining <= 0:
            break
        chunk = _read_capped(root / rel, min(remaining, 12_000))
        if chunk:
            parts.append(f"\n\n--- {rel} ---\n{chunk}")
            remaining -= len(chunk)

    bridge = """
[WRITERAGENT LO BRIDGE]
- You run inside LibreOffice via a user venv worker with filesystem + script access.
- Use run_ppt_master_script for upstream commands under scripts/.
- Use read/write_project_file for project artifacts (SVG, design_spec, etc.).
- Call export_presentation_project / validate_ppt_master_project for the active Impress/Draw document (host UNO).
- reply_to_user continues the session; ppt_master_finished ends it (HTML messages).
"""
    parts.append(bridge)
    return {"ok": True, "data_root": str(root), "block": "".join(parts)}
