"""LibrePy OXT vendor subset — only packages used by core runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.build_oxt import sync_vendor_into_lib
from scripts.librepy_bundle_paths import LIBREPY_VENDOR_PACKAGES, iter_librepy_vendor_packages  # noqa: E402

_VENDOR = _REPO_ROOT / "vendor"

_WRITERAGENT_ONLY_VENDOR = frozenset({"snowballstemmer", "websockets", "defusedxml"})


@pytest.fixture(scope="module")
def vendor_dir() -> str:
    if not _VENDOR.is_dir():
        pytest.skip("vendor/ missing — run: make vendor")
    return str(_VENDOR)


def test_librepy_vendor_packages_match_filter(vendor_dir: str) -> None:
    names = iter_librepy_vendor_packages(vendor_dir)
    assert set(names) == LIBREPY_VENDOR_PACKAGES
    assert names == sorted(names)


def test_librepy_vendor_excludes_writeragent_only_packages(vendor_dir: str) -> None:
    on_disk = {
        entry
        for entry in os.listdir(vendor_dir)
        if not entry.endswith(".dist-info") and os.path.isdir(os.path.join(vendor_dir, entry))
    }
    for pkg in _WRITERAGENT_ONLY_VENDOR:
        if pkg in on_disk:
            assert pkg not in iter_librepy_vendor_packages(vendor_dir)


def test_librepy_vendor_includes_required_packages(vendor_dir: str) -> None:
    names = iter_librepy_vendor_packages(vendor_dir)
    assert "isodate" in names
    assert "json_repair" in names
    assert "latex2mathml" in names
    assert "mathml_to_latex" in names


def test_filtered_sync_vendor_into_lib_copies_metadata_and_omits_excluded(tmp_path: Path) -> None:
    temp_vendor = tmp_path / "vendor"
    temp_lib = tmp_path / "lib"
    for pkg in ("latex2mathml", "mathml_to_latex", "websockets", "snowballstemmer"):
        (temp_vendor / pkg).mkdir(parents=True)
        (temp_vendor / pkg / "__init__.py").write_text("#\n", encoding="utf-8")

    (temp_vendor / "latex2mathml-3.81.0.dist-info").mkdir(parents=True)
    (temp_vendor / "latex2mathml-3.81.0.dist-info" / "METADATA").write_text("Name: latex2mathml\nVersion: 3.81.0\n", encoding="utf-8")
    (temp_vendor / "latex2mathml-3.81.0.dist-info" / "RECORD").write_text("", encoding="utf-8")

    (temp_vendor / "mathml_to_latex-1.0.0.dist-info").mkdir(parents=True)
    (temp_vendor / "mathml_to_latex-1.0.0.dist-info" / "METADATA").write_text("Name: mathml-to-latex\nVersion: 1.0.0\n", encoding="utf-8")
    (temp_vendor / "mathml_to_latex-1.0.0.dist-info" / "RECORD").write_text("", encoding="utf-8")

    count = sync_vendor_into_lib(
        str(temp_vendor),
        str(temp_lib),
        prune_websockets=False,
        only=LIBREPY_VENDOR_PACKAGES,
    )
    assert count == 2
    assert (temp_lib / "latex2mathml" / "__init__.py").is_file()
    assert (temp_lib / "mathml_to_latex" / "__init__.py").is_file()
    assert (temp_lib / "latex2mathml-3.81.0.dist-info" / "METADATA").is_file()
    assert not (temp_lib / "latex2mathml-3.81.0.dist-info" / "RECORD").exists()
    assert (temp_lib / "mathml_to_latex-1.0.0.dist-info" / "METADATA").is_file()
    assert not (temp_lib / "mathml_to_latex-1.0.0.dist-info" / "RECORD").exists()
    assert not (temp_lib / "websockets").exists()
    assert not (temp_lib / "snowballstemmer").exists()


def test_librepy_vendor_imports_in_isolated_process(vendor_dir: str, tmp_path: Path) -> None:
    import subprocess

    lib_dir = tmp_path / "plugin" / "lib"
    sync_vendor_into_lib(
        vendor_dir,
        str(lib_dir),
        prune_websockets=False,
        only=LIBREPY_VENDOR_PACKAGES,
    )

    script = """
import sys, os
lib_dir = sys.argv[1]
sys.path = [lib_dir] + [p for p in sys.path if "site-packages" not in p and "writeragent" not in p]

import latex2mathml
import mathml_to_latex
import isodate
import json_repair

# Check the bundled copy loaded, not the venv one (no exact version pin: CI may install a newer patch).
for mod in (latex2mathml, mathml_to_latex, isodate, json_repair):
    assert os.path.realpath(mod.__file__).startswith(os.path.realpath(lib_dir)), mod.__file__
"""
    proc = subprocess.run(
        [sys.executable, "-c", script, str(lib_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr

