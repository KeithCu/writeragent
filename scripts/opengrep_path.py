#!/usr/bin/env python3
"""Resolve the Opengrep executable across local and upstream install layouts."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _is_windows(platform_name: str | None = None, env: dict[str, str] | None = None) -> bool:
    env = env or os.environ
    platform_name = platform_name or sys.platform
    return platform_name.startswith("win") or env.get("OS") == "Windows_NT"


def _executable_names(is_windows: bool) -> tuple[str, ...]:
    return ("opengrep.exe", "opengrep") if is_windows else ("opengrep",)


def _usable_file(path: Path, *, is_windows: bool) -> bool:
    if not path.is_file():
        return False
    return is_windows or os.access(path, os.X_OK)


def _from_env(env: dict[str, str], *, is_windows: bool) -> Path | None:
    value = env.get("OPENGREP", "").strip()
    if not value:
        return None
    path = Path(value)
    return path if _usable_file(path, is_windows=is_windows) else None


def _from_path(env: dict[str, str], *, is_windows: bool) -> Path | None:
    search_path = env.get("PATH")
    for name in _executable_names(is_windows):
        found = shutil.which(name, path=search_path)
        if found:
            return Path(found)
    return None


def _home_candidates(env: dict[str, str], *, is_windows: bool) -> list[Path]:
    homes = []
    for key in ("USERPROFILE", "HOME"):
        value = env.get(key)
        if value:
            homes.append(Path(value))

    candidates: list[Path] = []
    seen: set[Path] = set()
    for home in homes:
        for name in _executable_names(is_windows):
            candidate = home / ".opengrep" / "cli" / "latest" / name
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def resolve_opengrep(
    *,
    repo_root: str | Path | None = None,
    env: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> Path | None:
    """Return the best Opengrep executable path, if one is installed."""
    env = env or os.environ
    is_windows = _is_windows(platform_name, env)
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]

    for candidate in (
        _from_env(env, is_windows=is_windows),
        _from_path(env, is_windows=is_windows),
    ):
        if candidate is not None:
            return candidate

    for name in _executable_names(is_windows):
        candidate = root / "bin" / name
        if _usable_file(candidate, is_windows=is_windows):
            return candidate

    for candidate in _home_candidates(env, is_windows=is_windows):
        if _usable_file(candidate, is_windows=is_windows):
            return candidate

    return None


def shell_path(path: Path) -> str:
    """Emit a path form that Git Bash and POSIX shells both accept."""
    return path.resolve().as_posix()


def main() -> int:
    path = resolve_opengrep()
    if path is None:
        return 1
    print(shell_path(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
