# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve ppt-master data root from user venv site-packages or config."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_PPT_MASTER_MARKERS = ("templates", "references", "SKILL.md")
_SKILL_REL_PATHS = (
    "skills/ppt-master",
    "ppt-master/skills/ppt-master",
    "ppt_master",
)

PPT_MASTER_INSTALL_CMD = (
    "git clone https://github.com/hugohe3/ppt-master.git\n"
    "# then Settings → Python → PPT-Master data path → .../ppt-master/skills/ppt-master"
)


def _looks_like_data_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any((path / m).exists() for m in _PPT_MASTER_MARKERS)


def _search_tree(root: Path) -> Path | None:
    if _looks_like_data_root(root):
        return root
    for rel in _SKILL_REL_PATHS:
        cand = root / rel
        if _looks_like_data_root(cand):
            return cand
    return None


def _user_venv_site_package_roots(ctx: Any | None = None) -> list[Path]:
    try:
        from plugin.framework.config import get_config_str

        venv_dir = get_config_str("scripting.python_venv_path").strip()
    except Exception:
        venv_dir = ""
    if not venv_dir:
        return []
    lib = Path(venv_dir).expanduser() / "lib"
    if not lib.is_dir():
        return []
    return [p for p in sorted(lib.glob("python*/site-packages")) if p.is_dir()]


def _dev_clone_data_root() -> Path | None:
    """Repo-root ``ppt-master/skills/ppt-master`` for local dev (not shipped in OXT)."""
    repo = Path(__file__).resolve().parents[2]
    return _search_tree(repo / "ppt-master")


def find_data_root_in_site_packages(ctx: Any | None = None) -> Path | None:
    """Locate skills/ppt-master under user venv site-packages or LO sys.path."""
    search_roots: list[Path] = []
    search_roots.extend(_user_venv_site_package_roots(ctx))
    for entry in sys.path:
        if entry:
            search_roots.append(Path(entry))
    dev = _dev_clone_data_root()
    if dev is not None:
        search_roots.append(dev.parent)
    seen: set[Path] = set()
    for base in search_roots:
        base = base.resolve()
        if base in seen:
            continue
        seen.add(base)
        found = _search_tree(base)
        if found is not None:
            return found
    return dev


def _configured_data_root() -> Path | None:
    try:
        from plugin.framework.config import get_config_str

        cfg_path = get_config_str("scripting.ppt_master_data_path").strip()
        if cfg_path:
            p = Path(cfg_path).expanduser()
            if _looks_like_data_root(p):
                return p
    except Exception:
        pass
    return None


def resolve_data_root(ctx: Any | None = None) -> Path:
    """Config override → env → venv discovery → dev clone."""
    configured = _configured_data_root()
    if configured is not None:
        return configured

    env = os.environ.get("PPT_MASTER_DATA_ROOT", "").strip()
    if env:
        p = Path(env).expanduser()
        if _looks_like_data_root(p):
            return p

    found = find_data_root_in_site_packages(ctx)
    if found is not None:
        return found

    # Placeholder for diagnostics when nothing is installed.
    return Path("skills/ppt-master")


def apply_data_root_env(ctx: Any | None = None) -> Path:
    """Set PPT_MASTER_DATA_ROOT for upstream ppt-master scripts."""
    root = resolve_data_root(ctx)
    if _looks_like_data_root(root):
        os.environ["PPT_MASTER_DATA_ROOT"] = str(root)
    return root


def _status_for_root(root: Path) -> dict[str, Any]:
    ok = _looks_like_data_root(root)
    scripts_dir = root / "scripts" if ok else None
    has_scripts = bool(scripts_dir and scripts_dir.is_dir())
    has_svg_to_pptx = bool(scripts_dir and (scripts_dir / "svg_to_pptx").is_dir())
    return {
        "data_root": str(root),
        "has_templates": (root / "templates").is_dir() if ok else False,
        "has_references": (root / "references").is_dir() if ok else False,
        "has_skill_md": (root / "SKILL.md").is_file() if ok else False,
        "has_scripts": has_scripts,
        "has_svg_to_pptx": has_svg_to_pptx,
        "ok": ok and has_scripts,
    }


def data_root_status(ctx: Any | None = None) -> dict[str, Any]:
    return _status_for_root(resolve_data_root(ctx))


def data_root_status_for_path(raw_path: str | None) -> dict[str, Any]:
    """Probe a path from Settings (saved or typed, not yet applied)."""
    raw = str(raw_path or "").strip()
    if raw:
        return _status_for_root(Path(raw).expanduser())
    return data_root_status()


def format_data_root_probe_message(status: dict[str, Any]) -> str:
    """Human-readable summary for Settings → Python PPT-Master Test."""
    lines = [
        f"Data root: {status.get('data_root', '')}",
        f"SKILL.md: {'yes' if status.get('has_skill_md') else 'no'}",
        f"templates/: {'yes' if status.get('has_templates') else 'no'}",
        f"references/: {'yes' if status.get('has_references') else 'no'}",
        f"scripts/: {'yes' if status.get('has_scripts') else 'no'}",
        f"scripts/svg_to_pptx/: {'yes' if status.get('has_svg_to_pptx') else 'no'}",
    ]
    if not status.get("ok"):
        lines.append("")
        lines.append("Not ready. Clone upstream and set the path to skills/ppt-master:")
        lines.append(PPT_MASTER_INSTALL_CMD)
    return "\n".join(lines)


def probe_data_path_with_progress(
    raw_path: str | None,
    on_display,
    on_status=None,
) -> tuple[bool, str]:
    """Settings probe callback (same shape as venv probe)."""
    status = data_root_status_for_path(raw_path)
    msg = format_data_root_probe_message(status)
    on_display(msg)
    if on_status:
        on_status("PPT-Master data root OK" if status.get("ok") else "PPT-Master data root incomplete")
    return bool(status.get("ok")), msg
