# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end check that soffice loads the linked worktree's extension."""

from __future__ import annotations

import unittest
from pathlib import Path

import uno

from plugin.testing_runner import current_office, native_test, worktree_root


@native_test
def test_soffice_loads_this_worktree(ctx) -> None:
    if current_office().options.use_user_profile:
        raise unittest.SkipTest("--user-profile uses the installed OXT by design")
    tree = worktree_root()
    if tree is None:
        raise unittest.SkipTest("not in a linked worktree")

    pip = ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
    pkg_url = pip.getPackageLocation("org.extension.writeragent")
    assert pkg_url, "org.extension.writeragent is not registered in soffice"

    pkg_plugin = Path(uno.fileUrlToSystemPath(pkg_url)) / "plugin"
    assert pkg_plugin.is_dir(), f"missing plugin dir in package: {pkg_plugin}"

    compared = 0
    for installed in sorted(pkg_plugin.rglob("*.py")):
        rel = installed.relative_to(pkg_plugin)
        if rel.parts[0] == "lib" or rel.name == "_manifest.py":
            continue
        source = tree / "plugin" / rel
        assert source.is_file(), f"installed {rel} has no worktree counterpart at {source}"
        assert installed.read_bytes() == source.read_bytes(), f"soffice loaded stale {rel}"
        compared += 1

    assert compared > 0, f"no plugin/*.py files compared under {pkg_plugin}"
