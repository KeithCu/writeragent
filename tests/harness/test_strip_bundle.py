# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for stripped-bundle helpers."""

from plugin.framework.client.stream_normalizer import (
    _merge_reasoning_details,
    _streaming_replay,
)
from tests.harness.strip_bundle import (
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


def test_stripped_plugin_module_has_no_asserts_in_release() -> None:
    """When running in a release build, plugin modules must have assert statements stripped and compile."""
    if not is_release_build():
        return
    import ast
    import inspect
    import plugin.chatbot.history_db as history_db

    source = inspect.getsource(history_db)
    tree = ast.parse(source)
    asserts = [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert not asserts, f"Found {len(asserts)} assert statement(s) in {history_db.__name__}"
    compile(source, history_db.__file__, "exec")


def test_asserts_stripped_matches_release_build() -> None:
    from plugin.framework.deal_shim import asserts_enabled
    from tests.harness.strip_bundle import asserts_stripped

    if is_release_build() or not __debug__:
        assert asserts_stripped() is True
    else:
        assert asserts_enabled() is True
        assert asserts_stripped() is False

