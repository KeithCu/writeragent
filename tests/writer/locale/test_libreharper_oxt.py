# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Harper proofreader locale and behavior."""

from __future__ import annotations

import ast
import os
from types import SimpleNamespace

from plugin.writer.locale.harper_proofreader import normalize_harper_locale_to_bcp47


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def test_libreharper_preserves_supported_english_dialects() -> None:
    for country, expected in (("US", "en-US"), ("GB", "en-GB"), ("AU", "en-AU"), ("CA", "en-CA"), ("IN", "en-IN")):
        locale = SimpleNamespace(Language="en", Country=country, Variant="")
        assert normalize_harper_locale_to_bcp47(locale) == expected

    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="en-AU", Country="", Variant="")) == "en-AU"
    assert normalize_harper_locale_to_bcp47(SimpleNamespace(Language="de", Country="DE", Variant="")) is None


def test_grammar_work_queue_has_no_top_level_framework_client_package_import() -> None:
    """Harper OXT must not pull LLM/embeddings via ``from plugin.framework.client import …``."""
    path = os.path.join(_repo_root(), "plugin", "writer", "locale", "grammar_work_queue.py")
    with open(path, encoding="utf-8") as f:
        module = ast.parse(f.read(), filename=path)
    top_level = [
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "plugin.framework.client" not in top_level
    assert "plugin.framework.client.request_controls" not in top_level
    assert "plugin.framework.client.llm_client" not in top_level
    assert "plugin.framework.client.model_fetcher" not in top_level


def test_libreharper_config_always_uses_harper_and_ignores_json() -> None:
    from unittest.mock import patch
    from plugin.writer.locale.grammar_persistence import grammar_checker_identity
    from plugin.writer.locale.grammar_worker import get_grammar_provider, is_grammar_enabled
    from plugin.framework.constants import EXTENSION_ID_LIBREHARPER
    from plugin.framework.uno_context import reset_package_extension_id_for_tests, set_package_extension_id

    try:
        set_package_extension_id(EXTENSION_ID_LIBREHARPER)
        with patch("plugin.framework.config.get_config", return_value="off"):
            assert is_grammar_enabled() is True
            assert get_grammar_provider() == "harper"
            assert grammar_checker_identity() == "harper"

        with patch("plugin.framework.config.get_config", return_value="llm"):
            assert is_grammar_enabled() is True
            assert get_grammar_provider() == "harper"
            assert grammar_checker_identity() == "harper"
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_identity_and_locale_check() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale import ai_grammar_proofreader
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        # __init__ calls maybe_start_harper_async when user_config_dir is set.
        # The autouse config fixture supplies a real temp dir, so without this
        # patch a unit test downloads/starts harper-ls and Windows xdist hangs.
        with (
            patch("plugin.chatbot.extension_update_check.schedule_extension_update_check_once"),
            patch("plugin.writer.locale.harper.maybe_start_harper_async"),
        ):
            proofreader = HarperProofreader(ctx)
        assert proofreader._checker_identity == "harper"
        assert proofreader._provider == "harper"

        locale_en = SimpleNamespace(Language="en", Country="US", Variant="")
        assert proofreader._check_enabled_and_locale("doc1", "Some text.", locale_en, 0, 10) == "en-US"

        locale_de = SimpleNamespace(Language="de", Country="DE", Variant="")
        assert proofreader._check_enabled_and_locale("doc1", "Some text.", locale_de, 0, 10) is None

        ai_grammar_proofreader._GRAMMAR_DISABLED_NOTICE_EMITTED = True
        with (
            patch("plugin.writer.locale.grammar_worker.is_grammar_enabled", return_value=False),
            patch("plugin.writer.locale.ai_grammar_proofreader.grammar_obs") as mock_obs,
        ):
            assert proofreader._check_enabled_and_locale("doc1", "Some text.", locale_en, 0, 10) == "en-US"
            assert ai_grammar_proofreader._GRAMMAR_DISABLED_NOTICE_EMITTED is True
            entry_calls = [c for c in mock_obs.call_args_list if c.args and c.args[0] == "do_proofreading_entry"]
            assert len(entry_calls) == 1
            assert entry_calls[0].kwargs.get("locale_raw") == "en"
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_initialization_starts_harper_warmup() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        with (
            patch("plugin.framework.config.init_config"),
            patch("plugin.framework.config.user_config_dir", return_value="/tmp/lo-user"),
            patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_start,
        ):
            HarperProofreader(ctx)
            mock_start.assert_called_once_with(ctx, user_config_dir="/tmp/lo-user")
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_skips_register_side_effects_on_no_vcl() -> None:
    """uno.bin register/enable must not warmup harper-ls or schedule update check (#768)."""
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        with (
            patch("plugin.framework.uno_context.desktop_create_is_unsafe", return_value=True),
            patch("plugin.chatbot.extension_update_check.schedule_extension_update_check_once") as mock_update,
            patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_start,
        ):
            HarperProofreader(ctx)
        mock_update.assert_not_called()
        mock_start.assert_not_called()
        ctx.ServiceManager.createInstanceWithContext.assert_not_called()
    finally:
        reset_package_extension_id_for_tests()


def test_harper_proofreader_skips_warmup_without_config_dir() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.writer.locale.harper_proofreader import HarperProofreader
    from plugin.framework.uno_context import reset_package_extension_id_for_tests

    try:
        ctx = MagicMock()
        with (
            patch("plugin.framework.config.init_config"),
            patch("plugin.framework.config.user_config_dir", return_value=""),
            patch("plugin.writer.locale.harper.maybe_start_harper_async") as mock_start,
        ):
            HarperProofreader(ctx)
            mock_start.assert_not_called()
    finally:
        reset_package_extension_id_for_tests()


def test_unrecognized_english_locale_is_logged() -> None:
    from unittest.mock import patch

    from plugin.writer.locale import harper_proofreader as hp

    hp._HARPER_LOCALE_MISS_LOGGED.clear()
    unrecognized = SimpleNamespace(Language="en-NZ", Country="", Variant="")
    with patch.object(hp.log, "warning") as mock_warn:
        assert hp.normalize_harper_locale_to_bcp47(unrecognized) is None
        assert hp.normalize_harper_locale_to_bcp47(unrecognized) is None
    assert mock_warn.call_count == 1
    assert "unrecognized English" in mock_warn.call_args[0][0]

    supported = SimpleNamespace(Language="en", Country="GB", Variant="")
    with patch.object(hp.log, "warning") as mock_warn_ok:
        assert hp.normalize_harper_locale_to_bcp47(supported) == "en-GB"
    mock_warn_ok.assert_not_called()

    german = SimpleNamespace(Language="de", Country="DE", Variant="")
    with patch.object(hp.log, "warning") as mock_warn_de:
        assert hp.normalize_harper_locale_to_bcp47(german) is None
    mock_warn_de.assert_not_called()


def test_locale_construction_failure_registers_degraded_and_retries() -> None:
    from unittest.mock import MagicMock, patch

    from plugin.framework.uno_context import reset_package_extension_id_for_tests
    from plugin.writer.locale.harper_proofreader import HarperProofreader

    calls = {"n": 0}

    def flaky() -> tuple[object, ...]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("uno struct failed")
        return ("locale-obj",)

    try:
        with (
            patch("plugin.chatbot.extension_update_check.schedule_extension_update_check_once"),
            patch("plugin.writer.locale.harper_proofreader._harper_locale_tuple", side_effect=flaky),
            patch("plugin.writer.locale.harper.maybe_start_harper_async"),
            patch("plugin.writer.locale.harper_proofreader.log") as mock_log,
        ):
            proofreader = HarperProofreader(MagicMock())
            assert proofreader._locales == ()
            assert proofreader._locales_degraded is True
            mock_log.error.assert_called()
            assert "degraded" in mock_log.error.call_args[0][0]

            locale = SimpleNamespace(Language="en", Country="US", Variant="")
            with patch.object(proofreader, "_normalize_locale", return_value="en-US"):
                assert proofreader.hasLocale(locale) is True
            assert proofreader._locales_degraded is False
            assert proofreader.getLocales() == ("locale-obj",)
            assert calls["n"] == 2
    finally:
        reset_package_extension_id_for_tests()


def test_locale_retry_failure_is_logged_once() -> None:
    from unittest.mock import MagicMock, patch

    from plugin.framework.uno_context import reset_package_extension_id_for_tests
    from plugin.writer.locale.harper_proofreader import HarperProofreader

    def boom() -> tuple[object, ...]:
        raise RuntimeError("still broken")

    try:
        with (
            patch("plugin.chatbot.extension_update_check.schedule_extension_update_check_once"),
            patch("plugin.writer.locale.harper_proofreader._harper_locale_tuple", side_effect=boom),
            patch("plugin.writer.locale.harper.maybe_start_harper_async"),
            patch("plugin.writer.locale.harper_proofreader.log") as mock_log,
        ):
            proofreader = HarperProofreader(MagicMock())
            locale = SimpleNamespace(Language="en", Country="US", Variant="")
            assert proofreader.hasLocale(locale) is False
            assert proofreader.hasLocale(locale) is False
            assert proofreader.getLocales() == ()
        assert mock_log.warning.call_count == 1
        assert "retry failed" in mock_log.warning.call_args[0][0]
    finally:
        reset_package_extension_id_for_tests()


