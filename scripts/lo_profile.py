#!/usr/bin/env python3
# WriterAgent — seed, register, or verify LibreOffice extensions
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Seed, register, or verify LibreOffice extensions.

    python scripts/lo_profile.py seed --oxt build/WriterAgent.oxt --profile DIR
    python scripts/lo_profile.py register --oxt build/WriterAgent.oxt --profile DIR [--remove ID] [--log PATH]
    python scripts/lo_profile.py verify --id EXTENSION_ID
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.lo_kill import kill, office_pids  # noqa: E402
from scripts.lo_paths import find, profile_url  # noqa: E402

# unopkg imports the extension's Python components in-process; keep the caller's venv out.
_SCRUB = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
_CONFIGMGR_INI = "user/uno_packages/cache/registry/com.sun.star.comp.deployment.configuration.PackageRegistryBackend/configmgr.ini"


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    return env


def _capture(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    # Capture stdout and stderr; unopkg hangs or leaves stale locks if piped into broken pipes.
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _run(cmd: list[str], env: dict[str, str] | None = None) -> str:
    code, out = _capture(cmd, env=env)
    if code != 0:
        raise RuntimeError("%s failed (exit %d):\n%s" % (" ".join(cmd), code, out))
    return out


def verify(profile: Path, oxt_name: str, listing: str) -> None:
    """Raise unless *profile* itself holds a registered *oxt_name* with CalcAddIns.xcu and no stale .lock."""
    if not list((profile / "user" / "uno_packages" / "cache" / "uno_packages").glob("*.tmp_/" + oxt_name)):
        raise RuntimeError("%s is not installed in %s (unopkg wrote to another profile?)" % (oxt_name, profile))
    if oxt_name not in listing or "is registered: yes" not in listing or "is registered: no" in listing:
        raise RuntimeError("unopkg list does not show %s fully registered:\n%s" % (oxt_name, listing))
    ini = profile / _CONFIGMGR_INI
    if not ini.is_file() or "CalcAddIns.xcu" not in ini.read_text(encoding="utf-8"):
        raise RuntimeError("CalcAddIns.xcu is not in %s" % ini)
    if (profile / ".lock").exists():
        raise RuntimeError("stale %s left behind by unopkg" % (profile / ".lock"))


def seed(oxt: Path, profile: Path) -> None:
    url = profile_url(profile)
    busy = office_pids(url)
    if busy:
        raise RuntimeError("LibreOffice PID(s) %s are using %s; unopkg would install into the default profile instead" % (busy, url))
    if profile.exists() and any(profile.iterdir()):
        raise RuntimeError("%s is not empty; seed needs a fresh profile dir" % profile)
    unopkg = find("unopkg")
    if unopkg is None:
        raise RuntimeError("LibreOffice unopkg not found")
    with tempfile.TemporaryDirectory(prefix="lo-profile-home-") as home:
        env = _env(Path(home))
        _run([str(unopkg), "add", "-f", "-env:UserInstallation=" + url, str(oxt.resolve())], env)
        verify(profile, oxt.name, _run([str(unopkg), "list", "-env:UserInstallation=" + url], env))


def register(oxt: Path, profile: Path, remove_ids: list[str], logs: list[Path]) -> None:
    unopkg = find("unopkg")
    if unopkg is None:
        raise RuntimeError("LibreOffice unopkg not found")
    kill(office_pids(None))
    for lock in (profile / ".lock", profile / "user" / ".lock"):
        lock.unlink(missing_ok=True)
    for remove_id in remove_ids:
        _capture([str(unopkg), "remove", remove_id])
    (profile / "user" / "extensions" / "tmp" / "extensions.pmap").unlink(missing_ok=True)
    tmp_dir = profile / "user" / "extensions" / "tmp" / "extensions"
    if tmp_dir.is_dir():
        for item in tmp_dir.glob("*.tmp_"):
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
    _run([str(unopkg), "add", str(oxt.resolve())])
    for log_path in logs:
        log_path.unlink(missing_ok=True)


def verify_registered(ext_id: str) -> None:
    unopkg = find("unopkg")
    if unopkg is None:
        raise RuntimeError("LibreOffice unopkg not found")
    listing = _run([str(unopkg), "list"])
    if ext_id not in listing:
        raise RuntimeError("%s is not registered in LibreOffice" % ext_id)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Seed, register, or verify LibreOffice extensions.")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_cmd = sub.add_parser("seed", help="unopkg add into a fresh profile and verify it")
    seed_cmd.add_argument("--oxt", required=True, type=Path)
    seed_cmd.add_argument("--profile", required=True, type=Path)

    reg_cmd = sub.add_parser("register", help="register extension in LibreOffice profile")
    reg_cmd.add_argument("--oxt", required=True, type=Path)
    reg_cmd.add_argument("--profile", required=True, type=Path)
    reg_cmd.add_argument("--remove", action="append", default=[], dest="remove")
    reg_cmd.add_argument("--log", action="append", default=[], type=Path, dest="log")

    ver_cmd = sub.add_parser("verify", help="verify extension is registered in default profile")
    ver_cmd.add_argument("--id", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "seed":
            seed(args.oxt, args.profile)
            print("Seeded %s into %s" % (args.oxt.name, profile_url(args.profile)))
        elif args.command == "register":
            register(args.oxt, args.profile, args.remove, args.log)
        elif args.command == "verify":
            verify_registered(args.id)
    except (RuntimeError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
