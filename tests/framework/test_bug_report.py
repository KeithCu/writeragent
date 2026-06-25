# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for GitHub bug-report URL building and browser launch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.framework import bug_report as br
from plugin.framework.errors import ConfigError


def test_build_github_issue_url_encodes_title_and_body():
    url = br.build_github_issue_url(title="Test bug", extra_body="extra line", ctx=None)
    assert url.startswith(br.BUG_REPORT_ISSUES_URL)
    assert "title=Test+bug" in url or "title=Test%20bug" in url
    assert "body=" in url
    assert "extra+line" in url or "extra%20line" in url


def test_build_github_issue_url_truncates_long_body():
    long_extra = "x" * 10000
    url = br.build_github_issue_url(extra_body=long_extra, ctx=None)
    assert len(url) < 12000
    assert "truncated" in url


def test_collect_environment_block_includes_endpoint_and_model():
    with patch("plugin.framework.client.model_fetcher.get_text_model", return_value="test-model"):
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.example.com"):
            with patch("plugin.version.EXTENSION_VERSION", "0.8.33"):
                block = br.collect_environment_block(ctx=None)
    assert "Endpoint: https://api.example.com" in block
    assert "Chat model: test-model" in block
    assert "WriterAgent: 0.8.33" in block


def test_should_offer_bug_report_denies_config_errors():
    assert br.should_offer_bug_report(code="CONFIG_ERROR") is False
    assert br.should_offer_bug_report(exc=ConfigError("bad key")) is False
    assert br.should_offer_bug_report(code="INTERNAL_ERROR") is True


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


@patch("plugin.framework.bug_report.open_url_in_browser", return_value=True)
@patch("plugin.framework.bug_report.build_github_issue_url", return_value="https://github.com/issue")
def test_open_bug_report_in_browser(mock_build, mock_open):
    ctx = MagicMock()
    assert br.open_bug_report_in_browser(ctx, title="t", extra_body="e") is True
    mock_build.assert_called_once_with(title="t", extra_body="e", ctx=ctx)
    mock_open.assert_called_once_with(ctx, "https://github.com/issue")


@patch("plugin.chatbot.dialogs.load_writeragent_dialog", return_value=None)
@patch("plugin.chatbot.dialogs.msgbox")
def test_msgbox_with_report_falls_back_without_dialog(mock_msgbox, mock_load):
    from plugin.chatbot.dialogs import msgbox_with_report

    ctx = MagicMock()
    msgbox_with_report(ctx, "T", "M", reportable=True)
    mock_msgbox.assert_called_once()


@patch("plugin.framework.bug_report.open_bug_report_in_browser")
@patch("plugin.chatbot.dialogs.load_writeragent_dialog")
@patch("plugin.chatbot.dialogs.copy_to_clipboard", return_value=True)
def test_msgbox_with_report_report_button(mock_copy, mock_load, mock_open_report):
    from plugin.chatbot.dialogs import msgbox_with_report

    dlg = MagicMock()
    mock_load.return_value = dlg
    report_btn = MagicMock()
    ok_btn = MagicMock()
    msg_ctrl = MagicMock()

    def get_control(name):
        return {"ReportBtn": report_btn, "OKBtn": ok_btn, "Msg": msg_ctrl, "CopyBtn": MagicMock()}.get(name)

    dlg.getControl.side_effect = get_control

    listeners = {}

    def add_listener(btn, listener):
        listeners[btn] = listener

    report_btn.addActionListener.side_effect = lambda lst: listeners.setdefault("report", lst)
    ok_btn.addActionListener.side_effect = lambda lst: listeners.setdefault("ok", lst)

    ctx = MagicMock()
    msgbox_with_report(ctx, "Err", "details", reportable=True, report_title="Err", report_extra="trace")

    listeners["report"].on_action_performed(MagicMock())
    mock_open_report.assert_called_once_with(ctx, title="Err", extra_body="details\n\ntrace")


@patch("plugin.framework.bug_report.build_github_issue_url", return_value="https://github.com/issue")
@patch("plugin.chatbot.dialogs.load_writeragent_dialog")
@patch("plugin.chatbot.dialogs.copy_to_clipboard", return_value=True)
def test_msgbox_with_report_includes_message_when_no_report_extra(mock_copy, mock_load, mock_build_url):
    from plugin.chatbot.dialogs import msgbox_with_report

    dlg = MagicMock()
    mock_load.return_value = dlg
    dlg.getControl.side_effect = lambda name: MagicMock()

    ctx = MagicMock()
    msgbox_with_report(ctx, "Err", "visible error text", reportable=True, report_title="Err")

    mock_build_url.assert_called_once_with(title="Err", extra_body="visible error text", ctx=ctx)


@patch("plugin.framework.bug_report.build_github_issue_url", return_value="https://github.com/issue")
@patch("plugin.chatbot.dialogs.load_writeragent_dialog")
@patch("plugin.chatbot.dialogs.copy_to_clipboard", return_value=True)
def test_msgbox_with_report_dedupes_identical_message_and_extra(mock_copy, mock_load, mock_build_url):
    from plugin.chatbot.dialogs import msgbox_with_report

    dlg = MagicMock()
    mock_load.return_value = dlg
    dlg.getControl.side_effect = lambda name: MagicMock()

    ctx = MagicMock()
    msgbox_with_report(ctx, "T", "same text", reportable=True, report_extra="same text")

    mock_build_url.assert_called_once_with(title="T", extra_body="same text", ctx=ctx)
