# WriterAgent - open config file in external editor
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch a system text editor on writeragent.json (cross-platform).

Invoked from settings UI (``dialog_views``), not chat-specific logic.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Any

from plugin.chatbot.dialogs import msgbox
from plugin.framework.config import atomic_write_text, get_config_file_path, init_config
from plugin.framework.i18n import _


def resolve_editor_argv(path: str) -> list[str] | None:
    """Return a subprocess argv to open *path* in an editor, or None if none found."""
    if sys.platform == "win32":
        return None  # use os.startfile
    if sys.platform == "darwin":
        return ["open", "-t", path]

    for name in ("kate", "gedit"):
        found = shutil.which(name)
        if found:
            return [found, path]
    for env_var in ("EDITOR", "VISUAL"):
        raw = (os.environ.get(env_var) or "").strip()
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        if parts:
            return parts + [path]
    return None


def _ensure_config_file(path: str) -> tuple[bool, str | None]:
    """Ensure *path* exists (minimal ``{}``). Returns (ok, error_message)."""
    if os.path.isfile(path):
        return True, None
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        atomic_write_text(path, "{}\n")
        return True, None
    except OSError as e:
        return False, str(e)


def _popen_detached(argv: list[str]) -> None:
    kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    subprocess.Popen(  # noqa: S603 — argv from resolve_editor_argv only
        argv,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def open_writeragent_json_in_editor(ctx: Any) -> None:
    """Open ``writeragent.json`` in an external editor (best-effort by OS)."""
    try:
        init_config(ctx)
        path = get_config_file_path()
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
            notepad = shutil.which("notepad.exe")
            if notepad:
                _popen_detached([notepad, path])
            else:
                msgbox(ctx, _("Error"), _("Could not open an editor. Path:\n{0}").format(path))
        return

    argv = resolve_editor_argv(path)
    if argv is None:
        msgbox(ctx, _("Error"), _("No editor found (install Kate or Gedit, or set EDITOR). Path:\n{0}").format(path))
        return
    try:
        _popen_detached(argv)
    except OSError as e:
        msgbox(ctx, _("Error"), _("Could not start editor: {0}").format(e))
