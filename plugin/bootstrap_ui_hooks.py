# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Framework UI hook registration shared by WriterAgent and LibrePy bootstraps."""

from __future__ import annotations

from typing import Any


def register_framework_ui_hooks() -> None:
    """Thread-violation and action-error UI — both WriterAgent and LibrePy."""
    from plugin.framework.main_shared import set_action_error_ui
    from plugin.framework.thread_guard import set_thread_violation_ui

    def _thread_violation_ui(uno_ctx: Any, title: str, full_msg: str) -> None:
        from plugin.chatbot.dialogs import msgbox_with_report

        msgbox_with_report(
            uno_ctx,
            title,
            full_msg,
            reportable=True,
            report_title="UNO thread violation",
            report_extra=full_msg,
        )

    set_thread_violation_ui(_thread_violation_ui)

    def _action_error_ui(uno_ctx: Any, detail: str, report_title: str, exc: BaseException) -> None:
        from plugin.chatbot.dialogs import msgbox_with_report
        from plugin.framework.i18n import _
        from plugin.framework.uno_context import get_ctx

        msgbox_with_report(
            uno_ctx or get_ctx(),
            _("Error"),
            detail,
            reportable=True,
            report_title=report_title,
            report_extra=str(exc),
        )

    set_action_error_ui(_action_error_ui)

    from plugin.scripting.sandbox import install_alias_authorizer

    install_alias_authorizer()
