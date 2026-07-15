"""LibrePy build-core: slim gettext catalogs from bundled sources only."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import polib
import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

LIBREPY_POT = _REPO / "build" / "generated" / "librepy.pot"
FULL_POT = _REPO / "locales" / "writeragent.pot"
LIBREPY_LOCALES = _REPO / "build" / "generated" / "locales"

pytestmark = pytest.mark.skipif(
    shutil.which("xgettext") is None or shutil.which("msgfmt") is None,
    reason="gettext tools (xgettext/msgfmt) required; install gettext (e.g. choco install gettext.install)",
)


def _ensure_manifest_core() -> None:
    manifest_script = _REPO / "scripts" / "generate_manifest.py"
    subprocess.run(
        [
            sys.executable,
            str(manifest_script),
            "--modules",
            "scripting",
            "vision",
            "--manifest-output",
            str(_REPO / "build" / "generated" / "_manifest_librepy.py"),
            "--skip-writeragent-extension",
            "--skip-addons",
        ],
        cwd=_REPO,
        check=True,
    )


def _ensure_librepy_locales() -> None:
    if not LIBREPY_POT.is_file():
        _ensure_manifest_core()
        subprocess.run(
            [sys.executable, str(_REPO / "scripts" / "build_librepy_locales.py")],
            cwd=_REPO,
            check=True,
        )


@pytest.fixture(scope="module")
def librepy_pot_msgids() -> set[str]:
    _ensure_librepy_locales()
    pot = polib.pofile(str(LIBREPY_POT))
    return {e.msgid for e in pot if e.msgid}


def test_librepy_pot_smaller_than_full_catalog(librepy_pot_msgids: set[str]) -> None:
    if not FULL_POT.is_file():
        pytest.skip("writeragent.pot not present")
    full = polib.pofile(str(FULL_POT))
    full_count = sum(1 for e in full if e.msgid)
    assert len(librepy_pot_msgids) < full_count


def test_librepy_pot_excludes_writeragent_only_string(librepy_pot_msgids: set[str]) -> None:
    assert "Accept" not in librepy_pot_msgids


def test_librepy_pot_includes_core_string(librepy_pot_msgids: set[str]) -> None:
    assert "Python Settings" in librepy_pot_msgids


def test_librepy_locales_compile_mo_files() -> None:
    _ensure_librepy_locales()
    mo_files = glob.glob(str(LIBREPY_LOCALES / "*" / "LC_MESSAGES" / "writeragent.mo"))
    assert mo_files, "expected filtered writeragent.mo under build/generated/locales/"
