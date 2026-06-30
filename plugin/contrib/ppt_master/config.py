# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ppt-master data path resolution (templates, references, workflows).

UPSTREAM NOTE (WriterAgent addition — not in upstream):
  Templates, references, workflows, and SKILL.md come from the pip-installed skill tree
  (``PPT_MASTER_DATA_ROOT``), not from this contrib package. Only WriterAgent adapter
  modules live under ``plugin/contrib/ppt_master/``.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    """Return ppt-master skill data root (set via apply_data_root_env / PPT_MASTER_DATA_ROOT)."""
    override = os.environ.get("PPT_MASTER_DATA_ROOT", "").strip()
    if not override:
        raise RuntimeError("PPT_MASTER_DATA_ROOT is not set; call plugin.ppt_master.paths.apply_data_root_env first")
    return Path(override).expanduser()


def templates_dir() -> Path:
    return data_root() / "templates"


def references_dir() -> Path:
    return data_root() / "references"


def workflows_dir() -> Path:
    return data_root() / "workflows"


def skill_md_path() -> Path:
    return data_root() / "SKILL.md"


def scripts_dir() -> Path:
    return data_root() / "scripts"
