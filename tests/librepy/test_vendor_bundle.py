"""LibrePy OXT vendor subset — only packages used by core runtime."""

from __future__ import annotations

import os

import pytest

from scripts.librepy_bundle_paths import LIBREPY_VENDOR_PACKAGES, iter_librepy_vendor_packages

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_VENDOR = os.path.join(_REPO_ROOT, "vendor")

_WRITERAGENT_ONLY_VENDOR = frozenset({"snowballstemmer", "websockets", "defusedxml"})


@pytest.fixture(scope="module")
def vendor_dir() -> str:
    if not os.path.isdir(_VENDOR):
        pytest.skip("vendor/ missing — run: make vendor")
    return _VENDOR


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
