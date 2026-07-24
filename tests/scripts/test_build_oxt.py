# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for OXT build path exclusions."""

from __future__ import annotations

from scripts.build_oxt import should_exclude


def test_python_logo_dev_sources_excluded_from_oxt():
    assert should_exclude("extension/assets/python_logo.svg") is True
    assert should_exclude("extension/assets/python_logo.NOTICE") is True
    assert should_exclude("extension/assets/python_32.png") is False
