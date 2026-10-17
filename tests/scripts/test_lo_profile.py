# WriterAgent — tests for scripts/lo_profile.py
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""``lo_profile.py seed``: refusal and verification rules, plus real unopkg runs."""

from __future__ import annotations

import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from scripts import lo_kill, lo_paths, lo_profile

needs_lo = pytest.mark.skipif(lo_paths.find("unopkg") is None or lo_paths.find("soffice") is None, reason="LibreOffice not installed")

_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="http://openoffice.org/2001/manifest">
 <manifest:file-entry manifest:media-type="application/vnd.sun.star.uno-component;type=Python" manifest:full-path="seedtest.py"/>
 <manifest:file-entry manifest:media-type="application/vnd.sun.star.configuration-data" manifest:full-path="CalcAddIns.xcu"/>
</manifest:manifest>
"""
_DESCRIPTION = """<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006">
 <identifier value="org.extension.writeragent.seedtest"/>
 <version value="0.0.1"/>
</description>
"""
_CALC_ADDINS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:component-data xmlns:oor="http://openoffice.org/2001/registry" oor:name="CalcAddIns" oor:package="org.openoffice.Office">
 <node oor:name="AddInInfo"><node oor:name="org.extension.writeragent.SeedTest" oor:op="replace"><node oor:name="AddInFunctions"/></node></node>
</oor:component-data>
"""
_COMPONENT = """import unohelper


class SeedTest(unohelper.Base):
    def __init__(self, ctx):
        self.ctx = ctx


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(SeedTest, "org.extension.writeragent.SeedTest", ("org.extension.writeragent.SeedTest",))
"""


def _oxt(tmp_path: Path) -> Path:
    """Tiny extension with one Python component and a CalcAddIns.xcu, like WriterAgent's."""
    oxt = tmp_path / "SeedTest.oxt"
    with zipfile.ZipFile(oxt, "w") as zf:
        zf.writestr("META-INF/manifest.xml", _MANIFEST)
        zf.writestr("description.xml", _DESCRIPTION)
        zf.writestr("CalcAddIns.xcu", _CALC_ADDINS)
        zf.writestr("seedtest.py", _COMPONENT)
    return oxt


def _fake_install(profile: Path, ini_text: str = "DATA=$UNO_USER_PACKAGES_CACHE/x/CalcAddIns.xcu\n") -> None:
    (profile / "user/uno_packages/cache/uno_packages/lu1.tmp_/SeedTest.oxt").mkdir(parents=True)
    ini = profile / lo_profile._CONFIGMGR_INI
    ini.parent.mkdir(parents=True)
    ini.write_text(ini_text, encoding="utf-8")


LISTING = "Identifier: x\n  URL: .../SeedTest.oxt\n  is registered: yes\n"


def test_refuses_profile_an_office_is_using(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_profile, "office_pids", lambda _url: [4242])
    with pytest.raises(RuntimeError, match="4242"):
        lo_profile.seed(tmp_path / "x.oxt", tmp_path / "profile")


def test_refuses_non_empty_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_profile, "office_pids", lambda _url: [])
    (tmp_path / "profile" / "user").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="not empty"):
        lo_profile.seed(tmp_path / "x.oxt", tmp_path / "profile")


def test_verify(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    with pytest.raises(RuntimeError, match="not installed"):
        lo_profile.verify(profile, "SeedTest.oxt", LISTING)
    _fake_install(profile)
    lo_profile.verify(profile, "SeedTest.oxt", LISTING)
    with pytest.raises(RuntimeError, match="fully registered"):
        lo_profile.verify(profile, "SeedTest.oxt", LISTING + "  is registered: no\n")
    (profile / ".lock").write_text("")
    with pytest.raises(RuntimeError, match="stale"):
        lo_profile.verify(profile, "SeedTest.oxt", LISTING)


def test_verify_requires_calc_addins(tmp_path: Path) -> None:
    _fake_install(tmp_path, ini_text="DATA=\n")
    with pytest.raises(RuntimeError, match="CalcAddIns"):
        lo_profile.verify(tmp_path, "SeedTest.oxt", LISTING)


def test_env_scrubs_python_and_sets_scratch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHONPATH", ".")
    monkeypatch.setenv("VIRTUAL_ENV", "/venv")
    env = lo_profile._env(tmp_path)
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["HOME"] == str(tmp_path)


@needs_lo
def test_seed_real_unopkg(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    assert lo_profile.main(["seed", "--oxt", str(_oxt(tmp_path)), "--profile", str(profile)]) == 0
    assert list(profile.glob("user/uno_packages/cache/uno_packages/*.tmp_/SeedTest.oxt"))


@needs_lo
def test_seed_refuses_while_an_office_runs_on_the_profile(tmp_path: Path) -> None:
    """Regression: with an office on the profile, unopkg silently installs into HOME's profile."""
    soffice = lo_paths.find("soffice")
    assert soffice is not None
    profile = tmp_path / "profile"
    url = lo_paths.profile_url(profile)
    home = tmp_path / "office-home"
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    proc = subprocess.Popen([str(soffice), "--headless", "--norestore", "--nologo", "-env:UserInstallation=" + url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not lo_kill.office_pids(url) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert lo_kill.office_pids(url)
        assert lo_profile.main(["seed", "--oxt", str(_oxt(tmp_path)), "--profile", str(profile)]) == 1
        assert not list(profile.glob("user/uno_packages/cache/uno_packages/*.tmp_"))
        assert not list(home.glob("**/SeedTest.oxt"))
    finally:
        lo_kill.kill(lo_kill.office_pids(url))
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_main_usage() -> None:
    with pytest.raises(SystemExit):
        lo_profile.main([])
