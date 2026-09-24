# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for stripped-bundle helpers."""

from plugin.framework.client.stream_normalizer import (
    _merge_reasoning_details,
    _streaming_replay,
)
from tests.strip_bundle import (
    _decorator_header,
    deal_pre_present,
    is_release_build,
    skip_if_release_build,
)


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


def test_deal_pre_present_uses_function_source_not_module_comment() -> None:
    """``_streaming_replay`` comments mention ``@deal.pre``; that must not count."""
    assert not deal_pre_present(_streaming_replay)
    # Checkout still has the real decorator; make release strip removes it.
    assert deal_pre_present(_merge_reasoning_details) is not is_release_build()


def test_skip_if_release_build_is_noop_in_checkout() -> None:
    """Checkout still has ``scripts/``; release pytest must skip, not fail, those tests."""
    if is_release_build():
        return
    skip_if_release_build("must not skip unstripped checkout")
