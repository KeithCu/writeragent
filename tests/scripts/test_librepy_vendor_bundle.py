"""LibrePy OXT vendor subset — only packages used by core runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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


def test_librepy_vendor_includes_json_repair_and_latex2mathml(vendor_dir: str) -> None:
    names = iter_librepy_vendor_packages(vendor_dir)
    assert "json_repair" in names
    assert "latex2mathml" in names
