# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for GitHub bug-report URL building and browser launch."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from plugin.chatbot import bug_report as br
from plugin.framework.constants import EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT


def test_build_github_issue_url_encodes_title_and_body():
    url = br.build_github_issue_url(title="Test bug", extra_body="extra line", ctx=None)
    assert url.startswith(br.BUG_REPORT_ISSUES_URL)
    assert "title=Test+bug" in url or "title=Test%20bug" in url
    assert "body=" in url
    assert "extra+line" in url or "extra%20line" in url


def test_build_github_issue_url_truncates_long_body():
    long_extra = "x" * 10000
    url = br.build_github_issue_url(extra_body=long_extra, ctx=None)
    assert len(url) <= br._MAX_URL_CHARS
    assert "truncated" in url


def test_compose_issue_body_keeps_environment_header_when_traceback_is_long():
    header = "### Environment\n- WriterAgent: 0.8.33\n- OS: Linux\n- Chat model: test-model"
    rest = "### Debug log\n" + ("instructions " * 50)
    extra = ("frame\n" * 4000) + "ValueError: sidebar send failed uniquely"
    body = br._compose_issue_body(header, rest, extra)
    assert body.startswith(header)
    assert "ValueError: sidebar send failed uniquely" in body
    assert len(br._issue_url(body, "")) <= br._MAX_URL_CHARS


def test_build_github_issue_url_keeps_exception_tail():
    last = "ValueError: sidebar send failed uniquely"
    traceback_body = ("frame line\n" * 2000) + last
    url = br.build_github_issue_url(extra_body=traceback_body, ctx=None)
    assert len(url) <= br._MAX_URL_CHARS
    assert "truncated" in url
    assert "ValueError%3A+sidebar+send+failed+uniquely" in url or "ValueError%3A%20sidebar%20send%20failed%20uniquely" in url


def test_build_github_issue_url_caps_encoded_multibyte_body():
    url = br.build_github_issue_url(extra_body=("é" * 5000) + "\nValueError: boom", ctx=None)
    assert len(url) <= br._MAX_URL_CHARS
    assert "ValueError" in url


def test_compose_issue_body_truncates_oversized_header():
    header = "H" * 20000
    rest = "### Description"
    extra = "trace"
    body = br._compose_issue_body(header, rest, extra)
    assert len(br._issue_url(body, "")) <= br._MAX_URL_CHARS
    assert body.startswith("H")
    assert body.endswith("[truncated]")


def test_collect_environment_block_includes_endpoint_and_model():
    with patch("plugin.framework.uno_context.resolve_package_extension_id", return_value=EXTENSION_ID_WRITERAGENT):
        with patch("plugin.framework.client.model_fetcher.get_text_model", return_value="test-model"):
            with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.example.com"):
                with patch("plugin.framework.config.user_config_dir", return_value="/tmp/lo-user"):
                    with patch("plugin.framework.logging.get_debug_log_path", return_value=None):
                        with patch("plugin.version.EXTENSION_VERSION", "0.8.33"):
                            header, rest = br._issue_sections(ctx=None)
                            block = f"{header}\n\n{rest}"
    assert "Endpoint: https://api.example.com" in block
    assert "Chat model: test-model" in block
    assert "WriterAgent: 0.8.33" in block
    # Production uses os.path.join; Windows CI failed on a hardcoded POSIX slash.
    expected_log = os.path.join("/tmp/lo-user", "writeragent_debug.log")
    assert f"WriterAgent debug log: `{expected_log}`" in block


def test_collect_environment_block_librepy_uses_venv_path():
    with patch("plugin.framework.uno_context.resolve_package_extension_id", return_value=EXTENSION_ID_LIBREPY):
        with patch("plugin.framework.config.get_config", return_value="/home/user/venv"):
            with patch("plugin.framework.config.user_config_dir", return_value="/tmp/lo-user"):
                with patch("plugin.framework.logging.get_debug_log_path", return_value=None):
                    with patch("plugin.version.EXTENSION_VERSION", "0.8.33"):
                        header, rest = br._issue_sections(ctx=MagicMock())
                        block = f"{header}\n\n{rest}"
    assert "LibrePy: 0.8.33" in block
    assert "Python venv path: /home/user/venv" in block
    assert "Endpoint:" not in block
    assert "Chat model:" not in block
    expected_log = os.path.join("/tmp/lo-user", "writeragent_debug.log")
    assert f"LibrePy debug log: `{expected_log}`" in block


def test_collect_environment_block_prefers_debug_log_path():
    with patch("plugin.framework.uno_context.resolve_package_extension_id", return_value=EXTENSION_ID_WRITERAGENT):
        with patch("plugin.framework.client.model_fetcher.get_text_model", return_value="test-model"):
            with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.example.com"):
                with patch("plugin.framework.config.user_config_dir", return_value="/tmp/lo-user"):
                    with patch("plugin.framework.logging.get_debug_log_path", return_value="/custom/path/writeragent_debug.log"):
                        with patch("plugin.version.EXTENSION_VERSION", "0.8.33"):
                            header, rest = br._issue_sections(ctx=None)
                            block = f"{header}\n\n{rest}"
    assert "WriterAgent debug log: `/custom/path/writeragent_debug.log`" in block
    assert "/tmp/lo-user" not in block


def test_issue_sections_redacts_endpoint_userinfo_and_query():
    with patch("plugin.framework.uno_context.resolve_package_extension_id", return_value=EXTENSION_ID_WRITERAGENT):
        with patch("plugin.framework.client.model_fetcher.get_text_model", return_value="test-model"):
            with patch("plugin.framework.config.get_current_endpoint", return_value="https://user:pass@api.example.com/v1?api_key=sekret99"):
                with patch("plugin.version.EXTENSION_VERSION", "0.8.33"):
                    header, _rest = br._issue_sections(ctx=None)
    assert "sekret99" not in header
    assert "pass@" not in header
    assert "https://api.example.com/v1" in header


def test_build_github_issue_url_redacts_configured_api_key():
    with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.example.com"):
        with patch("plugin.framework.config.get_api_key_for_endpoint", return_value="sk-secret-1234"):
            url = br.build_github_issue_url(extra_body="failed with key sk-secret-1234", ctx=None)
    assert "sk-secret-1234" not in url
    assert "redacted" in url


def test_format_lo_version_prefers_about_with_suffix():
    assert br._format_lo_version({"about": "24.8.4.2", "suffix": "beta1"}) == "24.8.4.2 beta1"
    assert br._format_lo_version({"about": "24.8.4.2"}) == "24.8.4.2"


def test_format_lo_version_falls_back_to_name_and_version():
    assert br._format_lo_version({"name": "LibreOffice", "version": "24.8"}) == "LibreOffice 24.8"
    assert br._format_lo_version({"version": "24.8"}) == "24.8"
    assert br._format_lo_version({}) == "unknown"


def test_get_lo_product_info_reads_valid_properties_only():
    ctx = MagicMock()
    smgr = ctx.getServiceManager.return_value
    config_provider = smgr.createInstanceWithContext.return_value
    ca = config_provider.createInstanceWithArguments.return_value
    prop_info = MagicMock()
    prop_info.hasPropertyByName.side_effect = lambda name: name in {
        "ooName",
        "ooSetupVersionAboutBox",
        "ooSetupVersion",
    }
    ca.getPropertySetInfo.return_value = prop_info
    ca.getPropertyValue.side_effect = lambda name: {
        "ooName": "LibreOffice",
        "ooSetupVersionAboutBox": "24.8.4.2",
        "ooSetupVersion": "24.8",
    }[name]

    with patch.dict("sys.modules", {"uno": MagicMock(createUnoStruct=MagicMock())}):
        product = br._get_lo_product_info(ctx)

    assert product == {
        "name": "LibreOffice",
        "about": "24.8.4.2",
        "version": "24.8",
    }
    queried = {call.args[0] for call in ca.getPropertyValue.call_args_list}
    assert "ooSetupBuild" not in queried
    assert "ooSetupVersionAboutBoxSuffix" not in queried


def test_get_lo_product_info_skips_unknown_properties_without_get_property_value():
    ctx = MagicMock()
    smgr = ctx.getServiceManager.return_value
    config_provider = smgr.createInstanceWithContext.return_value
    ca = config_provider.createInstanceWithArguments.return_value
    prop_info = MagicMock()
    prop_info.hasPropertyByName.return_value = False
    ca.getPropertySetInfo.return_value = prop_info

    with patch.dict("sys.modules", {"uno": MagicMock(createUnoStruct=MagicMock())}):
        assert br._get_lo_product_info(ctx) == {}

    ca.getPropertyValue.assert_not_called()


def test_open_url_in_browser_uses_system_shell_execute_first():
    flags = MagicMock()
    flags.URIS_ONLY = 99
    sys_mod = MagicMock()
    sys_mod.SystemShellExecuteFlags = flags

    ctx = MagicMock()
    shell = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = shell

    with patch.dict("sys.modules", {"com.sun.star.system": sys_mod}):
        assert br.open_url_in_browser(ctx, "https://example.com/issue") is True
    shell.execute.assert_called_once_with("https://example.com/issue", "", 99)


def test_open_url_in_browser_falls_back_to_webbrowser():
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.side_effect = RuntimeError("no uno")

    with patch("webbrowser.open", return_value=True) as wb_open:
        with patch.object(br, "log"):
            assert br.open_url_in_browser(ctx, "https://example.com/issue") is True
    wb_open.assert_called_once_with("https://example.com/issue")


def test_open_url_in_browser_returns_false_when_both_fail():
    ctx = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.side_effect = RuntimeError("no uno")

    with patch("webbrowser.open", return_value=False):
        with patch.object(br, "log"):
            assert br.open_url_in_browser(ctx, "https://example.com/issue") is False
