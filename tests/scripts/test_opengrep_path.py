from __future__ import annotations

from pathlib import Path

from scripts.opengrep_path import resolve_opengrep, shell_path


def _touch(path: Path, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def test_resolve_honors_opengrep_env_on_windows(tmp_path: Path) -> None:
    exe = _touch(tmp_path / "custom" / "opengrep.exe")

    resolved = resolve_opengrep(
        repo_root=tmp_path / "repo",
        env={"OS": "Windows_NT", "OPENGREP": str(exe), "PATH": ""},
        platform_name="win32",
    )

    assert resolved == exe


def test_resolve_windows_user_profile_install(tmp_path: Path) -> None:
    exe = _touch(tmp_path / "home" / ".opengrep" / "cli" / "latest" / "opengrep.exe")

    resolved = resolve_opengrep(
        repo_root=tmp_path / "repo",
        env={"OS": "Windows_NT", "USERPROFILE": str(tmp_path / "home"), "PATH": ""},
        platform_name="win32",
    )

    assert resolved == exe
    assert "\\" not in shell_path(resolved)


def test_resolve_windows_repo_local_exe(tmp_path: Path) -> None:
    exe = _touch(tmp_path / "repo" / "bin" / "opengrep.exe")

    resolved = resolve_opengrep(
        repo_root=tmp_path / "repo",
        env={"OS": "Windows_NT", "PATH": ""},
        platform_name="win32",
    )

    assert resolved == exe


def test_resolve_posix_home_install(tmp_path: Path) -> None:
    exe = _touch(tmp_path / "home" / ".opengrep" / "cli" / "latest" / "opengrep", executable=True)

    resolved = resolve_opengrep(
        repo_root=tmp_path / "repo",
        env={"HOME": str(tmp_path / "home"), "PATH": ""},
        platform_name="linux",
    )

    assert resolved == exe
