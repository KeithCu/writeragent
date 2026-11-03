# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Verification check that sandbox filesystem walkers stay off cover-all targets."""

from __future__ import annotations


def test_sandbox_filesystem_helpers_are_off_cover_all() -> None:
    """Filesystem walkers stay off cover-all targets."""
    from pathlib import Path

    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/scripting/sandbox.py"))
    offed = (
        "_is_usable_python_file",
        "_python_beside_soffice",
        "_bundled_lo_python_candidates",
        "_python_candidates_in_bin_dir",
        "_python_candidates_at_env_root",
        "_first_executable_python",
    )
    for name in offed:
        assert not any(f.endswith(f".{name}") for f in fqns), name
    # Basename validator stays on with ascii dual-profile.
    assert any(f.endswith("._is_acceptable_python_basename") for f in fqns)

