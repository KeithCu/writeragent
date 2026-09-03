# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import sys
from pathlib import Path

import plugin.testing_runner as tr


def _soffice_name() -> str:
    """Windows LO ships ``soffice.exe`` next to officehelper; POSIX is ``soffice``."""
    return "soffice.exe" if sys.platform.startswith("win") else "soffice"


def _touch_soffice_next_to_helper(tmp_path: Path, *, name: str | None = None) -> Path:
    binary = tmp_path / (name or _soffice_name())
    binary.write_text("", encoding="utf-8")
    return binary


def test_soffice_bootstrap_command_seeds_throwaway(monkeypatch, tmp_path: Path) -> None:
    seeded: list[Path] = []
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "_seed_throwaway_profile_with_user_oxt", seeded.append)
    _touch_soffice_next_to_helper(tmp_path)
    helper = type("Helper", (), {"__file__": str(tmp_path / "officehelper.py")})()
    cmd = tr._soffice_bootstrap_command(helper)
    assert cmd is not None
    assert "--headless" in cmd
    assert "-env:UserInstallation=" in cmd
    assert len(seeded) == 1
    assert seeded[0].name.startswith("writeragent-lo-test-profile-")


def test_soffice_bootstrap_command_github_actions_requires_user_oxt(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(tr, "_user_writeragent_uno_packages", lambda: None)
    monkeypatch.setattr(tr, "_libreoffice_user_profile_dir", lambda: tmp_path)
    _touch_soffice_next_to_helper(tmp_path)
    helper = type("Helper", (), {"__file__": str(tmp_path / "officehelper.py")})()
    try:
        tr._soffice_bootstrap_command(helper)
    except RuntimeError as exc:
        assert "register-built-oxt" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when GitHub Actions has no user OXT")


def test_soffice_bootstrap_command_seeds_throwaway_win32_exe(
    monkeypatch, tmp_path: Path
) -> None:
    """GHA 33749075233: Windows lookup is soffice.exe; a bare ``soffice`` is ignored."""
    seeded: list[Path] = []
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "_seed_throwaway_profile_with_user_oxt", seeded.append)
    monkeypatch.setattr(tr.sys, "platform", "win32")
    _touch_soffice_next_to_helper(tmp_path, name="soffice.exe")
    helper = type("Helper", (), {"__file__": str(tmp_path / "officehelper.py")})()
    cmd = tr._soffice_bootstrap_command(helper)
    assert cmd is not None
    assert "soffice.exe" in cmd
    assert "--headless" in cmd
    assert len(seeded) == 1


def test_soffice_bootstrap_command_uses_resolve_when_not_beside_helper(
    monkeypatch, tmp_path: Path
) -> None:
    """macOS: officehelper is in Contents/Resources, soffice in Contents/MacOS."""
    seeded: list[Path] = []
    found = tmp_path / "MacOS" / "soffice"
    found.parent.mkdir()
    found.write_text("", encoding="utf-8")
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "_seed_throwaway_profile_with_user_oxt", seeded.append)
    monkeypatch.setattr(tr, "_resolve_soffice_bin", lambda _helper: found)
    helper = type("Helper", (), {"__file__": str(tmp_path / "Resources" / "officehelper.py")})()
    cmd = tr._soffice_bootstrap_command(helper)
    assert cmd is not None
    assert str(found) in cmd
    assert "--headless" in cmd
    assert "-env:UserInstallation=" in cmd
    assert len(seeded) == 1


def test_on_github_actions_reads_env(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert tr.on_github_actions() is False
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert tr.on_github_actions() is True
    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    assert tr.on_github_actions() is False


def test_writeragent_oxt_in_uno_packages(tmp_path: Path) -> None:
    empty = tmp_path / "uno_packages"
    empty.mkdir()
    assert tr._writeragent_oxt_in_uno_packages(empty) is False
    packed = tmp_path / "packed" / "cache" / "uno_packages" / "lu1.tmp_" / "WriterAgent.oxt"
    packed.mkdir(parents=True)
    (packed / "plugin").mkdir()
    assert tr._writeragent_oxt_in_uno_packages(tmp_path / "packed") is True


def test_seed_throwaway_copies_user_uno_packages(monkeypatch, tmp_path: Path) -> None:
    src = tmp_path / "user-profile" / "user" / "uno_packages"
    oxt = src / "cache" / "uno_packages" / "lu9.tmp_" / "WriterAgent.oxt"
    oxt.mkdir(parents=True)
    (oxt / "addin.py").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(tr, "_libreoffice_user_profile_dir", lambda: tmp_path / "user-profile")
    dest_root = tmp_path / "throwaway"
    tr._seed_throwaway_profile_with_user_oxt(dest_root)
    copied = dest_root / "user" / "uno_packages" / "cache" / "uno_packages" / "lu9.tmp_" / "WriterAgent.oxt" / "addin.py"
    assert copied.read_text(encoding="utf-8") == "ok"
    seeded_cfg = dest_root / "user" / "config" / "writeragent.json"
    seeded_text = seeded_cfg.read_text(encoding="utf-8")
    assert '"scripting.python_session_mode": "shared"' in seeded_text
    # Checkout .venv seed made leftover Shared Isolated (33751116865 / 33752809831).
    assert "python_venv_path" not in seeded_text


def test_seed_throwaway_missing_oxt_raises_on_github_actions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tr, "_libreoffice_user_profile_dir", lambda: tmp_path / "missing")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    try:
        tr._seed_throwaway_profile_with_user_oxt(tmp_path / "throwaway")
    except RuntimeError as exc:
        assert "register-built-oxt" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on GitHub Actions without user OXT")


def test_seed_worker_python_path_never_seeds_checkout_venv(
    monkeypatch, tmp_path: Path
) -> None:
    """Checkout .venv seed made leftover Shared Isolated (33751116865 / 33752809831)."""
    fake_root = tmp_path / "repo"
    venv_py = fake_root / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    monkeypatch.setattr(tr, "__file__", str(fake_root / "plugin" / "testing_runner.py"))
    for platform in ("darwin", "win32", "linux"):
        monkeypatch.setattr(tr.sys, "platform", platform)
        assert tr._seed_worker_python_path() is None


def test_seed_throwaway_missing_oxt_is_noop_locally(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tr, "_libreoffice_user_profile_dir", lambda: tmp_path / "missing")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    tr._seed_throwaway_profile_with_user_oxt(tmp_path / "throwaway")
    assert not (tmp_path / "throwaway" / "user" / "uno_packages").exists()


def test_parse_cli_user_profile_sets_flags(monkeypatch) -> None:
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "show_window", False)
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    rest = tr._parse_cli_args(["--user-profile", "tests/chatbot/test_mock_llm_sidebar_uno.py"])
    assert tr.use_user_profile is True
    assert tr.show_window is True
    assert rest == ["tests/chatbot/test_mock_llm_sidebar_uno.py"]
    assert os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1"


def test_main_prints_officehelper_importerror_detail(monkeypatch, capsys) -> None:
    """CI 33708366478 swallowed the real ImportError (often nested ``import uno``)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "officehelper":
            raise ImportError("simulated missing uno")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(tr, "_ensure_libreoffice_python_path", lambda: None)
    assert tr.main() == 1
    out = capsys.readouterr().out
    assert "officehelper module is not available" in out
    assert "simulated missing uno" in out


def test_ensure_libreoffice_python_path_includes_macos_resources() -> None:
    """officehelper.py is in Contents/Resources, not the framework python3 dir."""
    src = Path(tr.__file__).read_text(encoding="utf-8")
    fn = src.split("def _ensure_libreoffice_python_path", 1)[1].split("\ndef ", 1)[0]
    assert "Contents/Resources" in fn
    assert "Caskroom/libreoffice" in fn
    assert "Contents/Frameworks" in fn


def test_soffice_strip_env_names() -> None:
    assert "PYTHONPATH" in tr._SOFFICE_STRIP_ENV
    assert "PYTHONHOME" in tr._SOFFICE_STRIP_ENV
    assert "VIRTUAL_ENV" in tr._SOFFICE_STRIP_ENV
    assert "__PYVENV_LAUNCHER__" in tr._SOFFICE_STRIP_ENV


def test_pop_soffice_env_restores(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/venv/bin/python")
    saved, stripped = tr._pop_soffice_env()
    assert "PYTHONPATH" in stripped
    assert "__PYVENV_LAUNCHER__" in stripped
    assert "PYTHONPATH" not in os.environ
    os.environ.update(saved)
    assert os.environ.get("PYTHONPATH") == "/checkout"


def test_is_uno_bridge_disposed() -> None:
    assert tr._is_uno_bridge_disposed(RuntimeError("Binary URP bridge disposed during call"))
    assert tr._is_uno_bridge_disposed(RuntimeError("Binary URP bridge already disposed"))
    assert not tr._is_uno_bridge_disposed(RuntimeError("no desktop"))


def test_run_module_suite_stops_after_urp_dispose() -> None:
    ran: list[str] = []

    def test_first(ctx=None):
        ran.append("first")
        raise RuntimeError("Binary URP bridge disposed during call")

    def test_second(ctx=None):
        ran.append("second")

    test_first._is_test = True
    test_second._is_test = True

    class _Mod:
        pass

    module = _Mod()
    module.test_first = test_first
    module.test_second = test_second

    tr._urp_bridge_dead = False
    passed, failed, suite_log = tr.run_module_suite(object(), module, "fake.urp")
    assert ran == ["first"]
    assert passed == 0
    assert failed == 1
    assert tr._urp_bridge_dead is True
    assert any("ABORT" in line or "disposed" in line.lower() for line in suite_log)
    tr._urp_bridge_dead = False


def test_user_profile_child_env_disables_uno_thread_guard(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    env = tr._child_env_without_runner_python(uno_thread_guard=False)
    assert "PYTHONPATH" not in env
    assert env.get("WRITERAGENT_UNO_THREAD_GUARD") == "0"


def test_parse_cli_default_is_headless_suite(monkeypatch) -> None:
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "show_window", False)
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    rest = tr._parse_cli_args(["--visible", "test_charts_uno"])
    assert tr.use_user_profile is False
    assert tr.show_window is True
    assert rest == ["test_charts_uno"]


def test_user_profile_soffice_argv_skips_nodefault_and_restore() -> None:
    from pathlib import Path

    argv = tr._user_profile_soffice_argv(Path("/usr/bin/soffice"), "socket,host=127.0.0.1,port=9;urp;")
    assert "--norestore" in argv
    assert "--writer" in argv
    assert "--nologo" in argv
    assert "--nodefault" not in argv
    assert any(a.startswith("--accept=socket") for a in argv)


def test_libreoffice_user_lock_path_is_under_profile() -> None:
    lock = tr._libreoffice_user_lock_path()
    assert lock.name == ".lock"
    assert "libreoffice" in str(lock).lower() or "LibreOffice" in str(lock)


def test_run_module_suite_prints_call_and_returned(capsys) -> None:
    """GHA 33703959362: execute-done then silence; call vs returned names the side."""

    def test_ok(ctx=None):
        return None

    test_ok._is_test = True

    class _Mod:
        pass

    module = _Mod()
    module.test_ok = test_ok
    passed, failed, _suite_log = tr.run_module_suite(object(), module, "fake.ok")
    assert passed == 1
    assert failed == 0
    err = capsys.readouterr().err
    assert "TEST call fake.ok.test_ok" in err
    assert "TEST returned fake.ok.test_ok" in err
    assert "TEST end fake.ok.test_ok OK" in err


def test_run_module_suite_arms_hang_dump_only_for_insert_cell_html(monkeypatch) -> None:
    """GHA 33703959362: arm at TEST start, disarm on TEST end (OK or FAIL)."""
    events: list[object] = []
    monkeypatch.setattr(
        tr, "_arm_insert_cell_html_hang_dump", lambda label: events.append(("arm", label))
    )
    monkeypatch.setattr(
        tr,
        "_disarm_insert_cell_html_hang_dump",
        lambda label: events.append(("disarm", label)),
    )

    def test_insert_cell_html(ctx=None):
        events.append("body")

    def test_other(ctx=None):
        events.append("other")

    test_insert_cell_html._is_test = True
    test_other._is_test = True

    class _Mod:
        pass

    module = _Mod()
    module.test_insert_cell_html = test_insert_cell_html
    module.test_other = test_other
    passed, failed, _suite_log = tr.run_module_suite(object(), module, "fake.html")
    assert passed == 2
    assert failed == 0
    assert events == [
        ("arm", "fake.html.test_insert_cell_html"),
        "body",
        ("disarm", "fake.html.test_insert_cell_html"),
        "other",
    ]


def test_run_module_suite_disarms_hang_dump_on_fail(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(tr, "_arm_insert_cell_html_hang_dump", lambda label: events.append("arm"))
    monkeypatch.setattr(
        tr, "_disarm_insert_cell_html_hang_dump", lambda label: events.append("disarm")
    )

    def test_insert_cell_html(ctx=None):
        raise AssertionError("boom")

    test_insert_cell_html._is_test = True

    class _Mod:
        pass

    module = _Mod()
    module.test_insert_cell_html = test_insert_cell_html
    passed, failed, _suite_log = tr.run_module_suite(object(), module, "fake.html")
    assert passed == 0
    assert failed == 1
    assert events == ["arm", "disarm"]


def test_arm_insert_cell_html_hang_dump_uses_ci_debug(monkeypatch) -> None:
    seen: list[tuple[object, ...]] = []

    def fake_arm(timeout, *, label=""):
        seen.append((timeout, label))

    import tests.ci_debug as ci_debug_mod

    monkeypatch.setattr(ci_debug_mod, "arm_stderr_hang_dump", fake_arm)
    monkeypatch.setattr(ci_debug_mod, "STDERR_HANG_DUMP_SECONDS", 90)
    tr._arm_insert_cell_html_hang_dump("calc.test_rich_html_uno.test_insert_cell_html")
    assert seen == [(90, "calc.test_rich_html_uno.test_insert_cell_html")]


def test_run_module_suite_prints_fail_reason(capsys) -> None:
    """GHA 33699746211 hung after three FAILs; suite_log JSON never printed."""

    def test_boom(ctx=None):
        raise AssertionError("A1 did not become 2.0")

    test_boom._is_test = True

    class _Mod:
        pass

    module = _Mod()
    module.test_boom = test_boom
    passed, failed, suite_log = tr.run_module_suite(object(), module, "fake.boom")
    assert passed == 0
    assert failed == 1
    assert any("A1 did not become 2.0" in line for line in suite_log)
    err = capsys.readouterr().err
    assert "TEST call fake.boom.test_boom" in err
    assert "TEST returned fake.boom.test_boom" not in err
    assert "TEST end fake.boom.test_boom FAIL AssertionError: A1 did not become 2.0" in err


def test_soffice_pids_win32_parses_tasklist(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(tr.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            '"soffice.exe","99","Console","1","10 K"\r\n'
            '"soffice.bin","100","Console","1","12 K"\r\n'
            '"notepad.exe","1","Console","1","1 K"\r\n'
        ),
    )
    assert tr._soffice_pids() == "99,100"


def test_fail_reason_flattens_and_caps() -> None:
    assert tr._fail_reason(AssertionError("x\ny")) == "AssertionError: x y"
    long_exc = AssertionError("n" * 500)
    out = tr._fail_reason(long_exc)
    assert out.startswith("AssertionError: ")
    assert out.endswith("...")
    assert len(out) == 400


def test_soffice_bin_running_uses_pids_helper(monkeypatch) -> None:
    monkeypatch.setattr(tr, "_soffice_pids", lambda: "18456")
    assert tr._soffice_bin_running() is True
    monkeypatch.setattr(tr, "_soffice_pids", lambda: "-")
    assert tr._soffice_bin_running() is False


def test_clear_stale_user_profile_ipc_globs_os_tempdir(monkeypatch, tmp_path) -> None:
    import glob
    import tempfile

    monkeypatch.setattr(tr, "_soffice_bin_running", lambda: False)
    monkeypatch.setattr(tr, "_libreoffice_user_lock_path", lambda: tmp_path / "missing.lock")
    seen: list[str] = []

    def fake_glob(pattern: str) -> list[str]:
        seen.append(pattern)
        return []

    monkeypatch.setattr(glob, "glob", fake_glob)
    tr._clear_stale_user_profile_ipc()
    # Production skips the POSIX OSL_PIPE glob when os.getuid is missing
    # (Windows). The test must not call getuid in the assertion.
    if hasattr(os, "getuid"):
        assert seen == [
            os.path.join(tempfile.gettempdir(), "OSL_PIPE_%s_*" % os.getuid())
        ]
    else:
        assert seen == []


def test_leftover_shared_diag_includes_cells_and_config(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from tests.calc.python import test_geometric_recalc_uno as geo_uno

    cfg = tmp_path / "writeragent.json"
    cfg.write_text('{"scripting.python_session_mode": "shared"}\n', encoding="utf-8")
    cell = SimpleNamespace(
        getValue=lambda: 0.0,
        getError=lambda: 0,
        getString=lambda: "x_geo_live is not defined",
        getFormula=lambda: '=PY("x_geo_live";A1)',
    )
    blob = geo_uno._leftover_shared_diag(None, cell, cell)
    assert "x_geo_live" in blob
    assert "leftover diag A1" in blob
    assert "leftover diag A3" in blob


def test_geometric_leftover_525_fails_on_github_actions(monkeypatch) -> None:
    from types import SimpleNamespace

    from tests.calc.python import test_geometric_recalc_uno as geo_uno

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cell = SimpleNamespace(
        getError=lambda: 525,
        getValue=lambda: 0.0,
        getFormula=lambda: '=py("x_geo_live = 41")',
    )
    try:
        geo_uno._skip_if_py_unregistered(
            cell, test_name="test_geometric_shared_kernel_a3_reads_a1_f9_stable"
        )
    except AssertionError as exc:
        assert "GitHub Actions" in str(exc)
        assert "525" in str(exc)
    else:
        raise AssertionError("525 on GitHub Actions must not skip")


def test_geometric_leftover_525_skips_locally(monkeypatch) -> None:
    from types import SimpleNamespace

    from tests.calc.python import test_geometric_recalc_uno as geo_uno

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cell = SimpleNamespace(
        getError=lambda: 525,
        getValue=lambda: 0.0,
        getFormula=lambda: '=py("x_geo_live = 41")',
    )
    assert (
        geo_uno._skip_if_py_unregistered(
            cell, test_name="test_geometric_shared_kernel_a3_reads_a1_f9_stable"
        )
        is True
    )
