# WriterAgent - open config file in external editor
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch a system text editor on writeragent.json (cross-platform)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from plugin.framework.config import _config_path
from plugin.framework.dialogs import msgbox
from plugin.framework.i18n import _


def _which(exe: str) -> str | None:
    return shutil.which(exe)


def resolve_editor_argv(path: str) -> list[str] | None:
    """Return a subprocess argv to open *path* in an editor, or None if none found."""
    p = path
    if sys.platform == "win32":
        return None  # use os.startfile
    if sys.platform == "darwin":
        return ["open", "-t", p]

    kate = _which("kate")
    if kate:
        return [kate, p]
    gedit = _which("gedit")
    if gedit:
        return [gedit, p]
    for env_var in ("EDITOR", "VISUAL"):
        raw = (os.environ.get(env_var) or "").strip()
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        if parts:
            return parts + [p]
    return None


def _ensure_config_file(path: str) -> tuple[bool, str | None]:
    """Ensure *path* exists (minimal ``{}``). Returns (ok, error_message)."""
    if os.path.isfile(path):
        return True, None
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        return True, None
    except OSError as e:
        return False, str(e)


def _popen_detached(argv: list[str]) -> None:
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, close_fds=True, **kwargs)  # noqa: S603 — argv from resolve_editor_argv only


def open_writeragent_json_in_editor(ctx) -> None:
    """Open ``writeragent.json`` in an external editor (best-effort by OS)."""
    try:
        path = _config_path(ctx)
    except Exception as e:
        msgbox(ctx, _("Error"), str(e))
        return

    ok, err = _ensure_config_file(path)
    if not ok:
        msgbox(ctx, _("Error"), _("Could not create config file: {0}").format(err or path))
        return

    if sys.platform == "win32":
        try:
            os.startfile(path)  # noqa: S606
        except OSError:
            notepad = _which("notepad.exe")
            if notepad:
                subprocess.Popen([notepad, path], close_fds=True)  # noqa: S603
            else:
                msgbox(
                    ctx,
                    _("Error"),
                    _("Could not open an editor. Path:\n{0}").format(path),
                )
        return

    argv = resolve_editor_argv(path)
    if argv is None:
        msgbox(
            ctx,
            _("Error"),
            _("No editor found (install Kate or Gedit, or set EDITOR). Path:\n{0}").format(path),
        )
        return
    try:
        _popen_detached(argv)
    except OSError as e:
        msgbox(ctx, _("Error"), _("Could not start editor: {0}").format(e))
