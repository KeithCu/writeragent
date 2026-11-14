# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for stripped-bundle helpers."""

from tests.harness.strip_bundle import _decorator_header


def test_decorator_header_ignores_body_comment() -> None:
    src = (
        "@deal.post(lambda result: True)\n"
        "def foo() -> None:\n"
        "    # @deal.pre mentioned in a comment must not count\n"
        "    return None\n"
    )
    header = _decorator_header(src)
    assert "@deal.post" in header
    assert "@deal.pre" not in header
