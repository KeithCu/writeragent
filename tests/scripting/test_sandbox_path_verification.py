# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / verification test suite for sandbox.is_safe_workspace_path."""

from __future__ import annotations

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import deal
from plugin.framework.deal_shim import DEAL_MAX_PATH
from plugin.scripting.sandbox import is_safe_workspace_path
from tests.strip_bundle import deal_pre_present
from tests.vhs_budget import vhs_max_examples


def test_basic_safe_paths() -> None:
    root = "/home/user/workspace"
    assert is_safe_workspace_path("file.txt", root) is True
    assert is_safe_workspace_path("subdir/file.txt", root) is True
    assert is_safe_workspace_path(".", root) is True


def test_basic_unsafe_traversal_paths() -> None:
    root = "/home/user/workspace"
    assert is_safe_workspace_path("../file.txt", root) is False
    assert is_safe_workspace_path("../../etc/passwd", root) is False
    assert is_safe_workspace_path("subdir/../../etc/passwd", root) is False
    assert is_safe_workspace_path("/etc/passwd", root) is False


def test_empty_inputs_return_false() -> None:
    assert is_safe_workspace_path("", "/home/user") is False
    assert is_safe_workspace_path("file.txt", "") is False


@given(
    rel_path=st.text(max_size=50),
    root_dir=st.text(min_size=1, max_size=50),
)
@settings(max_examples=vhs_max_examples(80, 800), deadline=None)
def test_hypothesis_path_containment_invariants(rel_path: str, root_dir: str) -> None:
    result = is_safe_workspace_path(rel_path, root_dir)
    assert isinstance(result, bool)
    if result:
        # If True, target must be inside root_dir
        abs_root = os.path.abspath(root_dir)
        abs_target = os.path.abspath(os.path.join(abs_root, rel_path))
        assert os.path.commonpath([abs_target, abs_root]) == abs_root


def test_safe_workspace_path_overflow_pre_fails_closed() -> None:
    if not deal_pre_present(is_safe_workspace_path):
        pytest.skip("@deal.pre stripped in release bundle")
    too_long = "a" * (DEAL_MAX_PATH + 1)
    with pytest.raises(deal.PreContractError):
        is_safe_workspace_path(too_long, "/home/user")
    assert is_safe_workspace_path("José.txt", "/home/user/workspace") is True
