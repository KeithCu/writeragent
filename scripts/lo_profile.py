#!/usr/bin/env python3
# WriterAgent — seed a throwaway LibreOffice profile with an extension
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install an .oxt into a fresh throwaway LibreOffice profile and verify it landed there.

    python scripts/lo_profile.py seed --oxt build/WriterAgent.oxt --profile DIR

Why the checks: when an office is already running on the profile, unopkg starts a
helper soffice *without* ``-env:UserInstallation`` and installs into the default
(HOME / %APPDATA%) profile instead, still exiting 0. So seed refuses a busy or used
profile, runs unopkg with a scratch HOME as a backstop (Windows ignores HOME), and
then checks the profile itself rather than trusting the exit code.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.lo_kill import office_pids  # noqa: E402
from scripts.lo_paths import find, profile_url  # noqa: E402

# unopkg imports the extension's Python components in-process; keep the caller's venv out.
_SCRUB = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
_CONFIGMGR_INI = "user/uno_packages/cache/registry/com.sun.star.comp.deployment.configuration.PackageRegistryBackend/configmgr.ini"


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    return env


def _run(cmd: list[str], env: dict[str, str]) -> str:
    # Capture everything: piping unopkg into e.g. head kills it with SIGPIPE and leaves a stale .lock.
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300, check=False)
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("%s failed (exit %d):\n%s" % (" ".join(cmd), proc.returncode, out))
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Seed a fresh LibreOffice profile with an extension.")
    sub = parser.add_subparsers(dest="command", required=True)
    seed_cmd = sub.add_parser("seed", help="unopkg add into a fresh profile and verify it")
    seed_cmd.add_argument("--oxt", required=True, type=Path)
    seed_cmd.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        seed(args.oxt, args.profile)
    except (RuntimeError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("Seeded %s into %s" % (args.oxt.name, profile_url(args.profile)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
