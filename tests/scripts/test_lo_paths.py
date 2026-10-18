# WriterAgent — tests for scripts/lo_paths.py
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibreOffice program discovery per platform (fake trees) and profile URLs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts import lo_paths


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Linux with no real install visible: only what the test creates."""
    monkeypatch.setattr(lo_paths.sys, "platform", "linux")
    monkeypatch.setattr(lo_paths.shutil, "which", lambda _name: None)
    monkeypatch.setattr(lo_paths, "_LINUX_DIRS", (str(tmp_path / "distro" / "program"),))
    monkeypatch.setattr(lo_paths, "_LINUX_OPT_GLOB", "no-such-dir*/program")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(tmp_path / "no-python3"))
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_linux_resolves_soffice_symlink_on_path(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prog = linux / "lib" / "libreoffice" / "program"
    _touch(prog / "soffice")
    unopkg = _touch(prog / "unopkg")
    link = linux / "bin" / "soffice"
    link.parent.mkdir()
    link.symlink_to(prog / "soffice")
    monkeypatch.setattr(lo_paths.shutil, "which", lambda _name: str(link))
    assert lo_paths.program_dir() == prog
    assert lo_paths.find("unopkg") == unopkg


def test_linux_distro_dir_and_system_python(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prog = linux / "distro" / "program"
    soffice = _touch(prog / "soffice")
    python3 = _touch(linux / "python3")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(python3))
    assert lo_paths.find("soffice") == soffice
    # No program/python (distro build): fall back to the system interpreter.
    assert lo_paths.find("python") == python3
    # A TDF build's own program/python wins.
    own = _touch(prog / "python")
    assert lo_paths.find("python") == own


def test_linux_not_installed(linux: Path) -> None:
    assert lo_paths.program_dir() is None
    assert lo_paths.find("unopkg") is None
    assert lo_paths.main(["unopkg"]) == 1


def test_windows_registry_then_program_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "win32")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    prog = tmp_path / "PF" / "LibreOffice" / "program"
    _touch(prog / "soffice.exe")
    _touch(prog / "unopkg.exe")
    com = _touch(prog / "unopkg.com")
    python = _touch(prog / "python.exe")
    assert lo_paths.find("unopkg") == com
    assert lo_paths.find("python") == python

    reg = tmp_path / "Custom" / "program"
    reg_soffice = _touch(reg / "soffice.exe")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: reg)
    assert lo_paths.find("soffice") == reg_soffice


def test_macos_app_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    monkeypatch.setattr(lo_paths.Path, "home", lambda: tmp_path)
    contents = tmp_path / "Applications" / "LibreOffice.app" / "Contents"
    if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        pytest.skip("a real /Applications/LibreOffice.app takes precedence")
    _touch(contents / "MacOS" / "soffice")
    unopkg = _touch(contents / "MacOS" / "unopkg")
    python = _touch(contents / "Resources" / "python")
    assert lo_paths.find("unopkg") == unopkg
    assert lo_paths.find("python") == python


def test_profile_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spaced = tmp_path / "my profile"
    url = lo_paths.profile_url(spaced)
    assert url == spaced.resolve().as_uri()
    assert url.startswith("file:///")
    assert "%20" in url
    assert not url.endswith("/")
    monkeypatch.chdir(tmp_path)
    assert lo_paths.profile_url("rel") == (tmp_path / "rel").resolve().as_uri()
    with pytest.raises(ValueError, match="user/"):
        lo_paths.profile_url(tmp_path / "profile" / "user")


def test_main_usage() -> None:
    assert lo_paths.main([]) == 2
    assert lo_paths.main(["find"]) == 2
    assert lo_paths.main(["find", "invalid"]) == 2
    assert lo_paths.main(["check-lo-python", "--invalid"]) == 2
    assert lo_paths.main(["run-lo-python", "--invalid"]) == 2
    assert lo_paths.main(["bogus"]) == 2


def _fake_linux_pythons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, works: set[str]) -> list[str]:
    # Fake interpreters + a fake import probe, so this also runs on Windows/macOS.
    monkeypatch.setattr(lo_paths.sys, "platform", "linux")
    usr = tmp_path / "usr_bin_python3"
    usr.write_text("", encoding="utf-8")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(usr))
    on_path = {"python3": tmp_path / "bin" / "python3", "python": tmp_path / "bin" / "python"}
    monkeypatch.setattr(lo_paths.shutil, "which", lambda name: str(on_path[name]) if name in on_path else None)
    probed: list[str] = []

    def probe(interp: Path | str) -> bool:
        probed.append(Path(interp).name)
        return Path(interp).name in works

    monkeypatch.setattr(lo_paths, "_probe_uno", probe)
    return probed


def test_linux_usr_bin_python3_wins_and_path_not_consulted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probed = _fake_linux_pythons(monkeypatch, tmp_path, {"usr_bin_python3", "python3"})
    assert lo_paths.find_lo_python() == tmp_path / "usr_bin_python3"
    assert probed == ["usr_bin_python3"]


def test_linux_skips_usr_bin_on_failure_and_probes_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probed = _fake_linux_pythons(monkeypatch, tmp_path, {"python"})
    assert lo_paths.find_lo_python() == tmp_path / "bin" / "python"
    assert probed == ["usr_bin_python3", "python3", "python"]


def test_linux_returns_nothing_when_every_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "linux")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("PATH", "")

    assert lo_paths.find_lo_python() is None

    ret = lo_paths.main(["find", "lo-python"])
    assert ret == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "not found" in err


def test_darwin_prefers_resources_python_and_caskroom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    apps_dir = tmp_path / "Applications"
    cask_dir = tmp_path / "Caskroom"
    monkeypatch.setattr(lo_paths, "_DARWIN_APPLICATIONS_DIR", apps_dir)
    monkeypatch.setattr(lo_paths, "_DARWIN_CASKROOM_DIRS", (cask_dir,))
    monkeypatch.setattr(lo_paths.Path, "home", lambda: tmp_path / "home")

    app = apps_dir / "LibreOffice.app"
    res_py = _touch(app / "Contents" / "Resources" / "python")
    fw_py = _touch(
        app
        / "Contents"
        / "Frameworks"
        / "LibreOfficePython.framework"
        / "Versions"
        / "Current"
        / "bin"
        / "python3"
    )
    assert lo_paths.find_lo_python() == res_py

    res_py.unlink()
    fw_py.unlink()
    (app / "Contents" / "Frameworks" / "LibreOfficePython.framework" / "Versions" / "Current" / "bin").rmdir()
    (app / "Contents" / "Frameworks" / "LibreOfficePython.framework" / "Versions" / "Current").rmdir()
    (app / "Contents" / "Frameworks" / "LibreOfficePython.framework" / "Versions").rmdir()
    (app / "Contents" / "Frameworks" / "LibreOfficePython.framework").rmdir()
    (app / "Contents" / "Frameworks").rmdir()
    (app / "Contents" / "Resources").rmdir()
    (app / "Contents").rmdir()
    app.rmdir()
    apps_dir.rmdir()

    cask_app = cask_dir / "7.6.0" / "LibreOffice.app"
    cask_res_py = _touch(cask_app / "Contents" / "Resources" / "python")
    assert lo_paths.find_lo_python() == cask_res_py

    cask_res_py.unlink()
    fake_path = tmp_path / "path"
    fake_path.mkdir()
    _touch(fake_path / "python3")
    monkeypatch.setenv("PATH", str(fake_path))
    assert lo_paths.find_lo_python() is None


def test_darwin_framework_python_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    apps_dir = tmp_path / "Applications"
    monkeypatch.setattr(lo_paths, "_DARWIN_APPLICATIONS_DIR", apps_dir)
    monkeypatch.setattr(lo_paths, "_DARWIN_CASKROOM_DIRS", ())
    monkeypatch.setattr(lo_paths.Path, "home", lambda: tmp_path / "home")

    app = apps_dir / "LibreOffice.app"
    fw_py = _touch(
        app
        / "Contents"
        / "Frameworks"
        / "LibreOfficePython.framework"
        / "Versions"
        / "Current"
        / "bin"
        / "python3"
    )
    assert lo_paths.find_lo_python() == fw_py


def test_windows_bundled_python_wins_without_import_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "win32")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    prog = tmp_path / "PF" / "LibreOffice" / "program"
    _touch(prog / "soffice.exe")
    python = _touch(prog / "python.exe")
    assert lo_paths.find_lo_python() == python


def test_lo_python_env_darwin_and_non_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "linux")
    assert lo_paths.lo_python_env(tmp_path / "python") == {}

    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    app = tmp_path / "LibreOffice.app"
    contents = app / "Contents"
    interp = _touch(contents / "Resources" / "python")
    rc = _touch(contents / "Resources" / "fundamentalrc")

    env = lo_paths.lo_python_env(interp)
    assert env["UNO_PATH"] == str(contents / "MacOS")
    assert env["URE_BOOTSTRAP"] == f"vnd.sun.star.pathname:{rc}"
    assert env["PYTHONPATH"] == f"{contents / 'Resources'}{os.pathsep}{contents / 'Frameworks'}"

    rc.unlink()
    ini = _touch(contents / "Resources" / "fundamental.ini")
    env_ini = lo_paths.lo_python_env(interp)
    assert env_ini["URE_BOOTSTRAP"] == f"vnd.sun.star.pathname:{ini}"


def test_child_env_drops_venv_keys_leaves_home_intact(tmp_path: Path) -> None:
    base_env = {
        "VIRTUAL_ENV": "/path/to/venv",
        "__PYVENV_LAUNCHER__": "/path/to/bin/python",
        "PYTHONHOME": "/path/to/home",
        "HOME": "/home/user",
        "CUSTOM_KEY": "keep_me",
    }
    child = lo_paths.build_child_env(tmp_path / "python", base_env=base_env)
    assert "VIRTUAL_ENV" not in child
    assert "__PYVENV_LAUNCHER__" not in child
    assert "PYTHONHOME" not in child
    assert child["HOME"] == "/home/user"
    assert child["CUSTOM_KEY"] == "keep_me"
    assert child["PYTHONUNBUFFERED"] == "1"
    assert "WRITERAGENT_PROJECT_ROOT" in child


def test_child_env_lo_python_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    app = tmp_path / "LibreOffice.app"
    interp = _touch(app / "Contents" / "Resources" / "python")
    _touch(app / "Contents" / "Resources" / "fundamentalrc")

    base_env = {
        "LO_PYTHON_ENV": 'CUSTOM_UNO=custom_val URE_BOOTSTRAP="vnd.sun.star.pathname:/custom/rc"',
    }
    child = lo_paths.build_child_env(interp, base_env=base_env)
    assert child["CUSTOM_UNO"] == "custom_val"
    assert child["URE_BOOTSTRAP"] == "vnd.sun.star.pathname:/custom/rc"
    assert "UNO_PATH" not in child
    assert "LO_PYTHON_ENV" not in child

    base_empty = {"LO_PYTHON_ENV": " "}
    child_empty = lo_paths.build_child_env(interp, base_env=base_empty)
    assert "UNO_PATH" not in child_empty
    assert "URE_BOOTSTRAP" not in child_empty


def test_child_env_pythonpath_handling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "linux")
    child_linux = lo_paths.build_child_env(
        tmp_path / "python", base_env={"PYTHONPATH": "."}
    )
    assert child_linux["PYTHONPATH"] == "."

    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    app = tmp_path / "LibreOffice.app"
    interp = _touch(app / "Contents" / "Resources" / "python")
    _touch(app / "Contents" / "Resources" / "fundamentalrc")
    child_darwin = lo_paths.build_child_env(
        interp, base_env={"PYTHONPATH": "."}
    )
    assert (
        child_darwin["PYTHONPATH"]
        == f"{app / 'Contents' / 'Resources'}{os.pathsep}{app / 'Contents' / 'Frameworks'}"
    )


def test_check_lo_python_empty_and_failed_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lo_paths.check_lo_python(["--python", ""]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "LO_PYTHON is empty" in err
    assert "Contents/Resources/python" in err
    assert "LibreOfficePython.framework" in err
    assert "/usr/bin/python3" in err

    bad_py = tmp_path / "bad_python"
    bad_py.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    bad_py.chmod(0o755)
    assert lo_paths.check_lo_python(["--python", str(bad_py)]) == 1
    out, err = capsys.readouterr()
    assert f"LO_PYTHON={bad_py}" in out
    assert "cannot import officehelper and uno" in err
    assert "project .venv" in err


def test_run_lo_python_execs_with_env_and_set(tmp_path: Path) -> None:
    script = tmp_path / "recorder.py"
    script.write_text(
        "import os, sys\n"
        "out = sys.argv[1]\n"
        "with open(out, 'w') as f:\n"
        "    f.write(os.environ.get('TEST_VAR', '') + '\\n')\n"
        "    f.write(os.environ.get('PYTHONUNBUFFERED', '') + '\\n')\n"
        "    f.write(' '.join(sys.argv[2:]) + '\\n')\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    out_file = tmp_path / "out.txt"
    code = lo_paths.run_lo_python([
        "--python",
        sys.executable,
        "--set",
        "TEST_VAR=injected",
        "--",
        str(script),
        str(out_file),
        "arg1",
        "arg2",
    ])
    assert code == 7
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "injected"
    assert lines[1] == "1"
    assert lines[2] == "arg1 arg2"

