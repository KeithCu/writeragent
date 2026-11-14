# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for stripped-bundle helpers."""

import deal

from tests.harness.strip_bundle import _decorator_header, deal_pre_present


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


def test_deal_pre_present_checks_decorator_header_only() -> None:
    """Body comments mentioning ``@deal.pre`` must not count as an active decorator."""

    def without_pre() -> None:
        # @deal.pre mentioned in a body comment must not count
        return None

    @deal.pre(lambda x: x > 0)
    def with_pre(x: int) -> int:
        return x

    assert not deal_pre_present(without_pre)
    assert deal_pre_present(with_pre)
