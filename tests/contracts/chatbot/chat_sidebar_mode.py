"""Contracts sidecar for plugin.chatbot.chat_sidebar_mode."""

from __future__ import annotations

import deal

from plugin.chatbot import chat_sidebar_mode as _prod
from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags, _SPEC_BY_KEY
from tests.contracts.bounds import DEAL_MAX_TOKEN, str_bounded

sidebar_mode_flags_for_doc_type = deal.pre(
    lambda doc_type_label: str_bounded(doc_type_label, DEAL_MAX_TOKEN)
)(
    deal.post(lambda result: isinstance(result, SidebarModeFlags))(
        _prod.sidebar_mode_flags_for_doc_type
    )
)

get_mode_labels = deal.post(
    lambda result: isinstance(result, tuple) and len(result) >= 4
)(_prod.get_mode_labels)

mode_from_label = deal.pre(
    lambda label, flags=SidebarModeFlags(): str_bounded(label, DEAL_MAX_TOKEN)
)(
    deal.post(lambda result: result in _SPEC_BY_KEY)(_prod.mode_from_label)
)

PYTEST = [
    "sidebar_mode_flags_for_doc_type",
    "get_mode_labels",
    "mode_from_label",
]

TARGETS = [
    f"{__name__}.sidebar_mode_flags_for_doc_type",
    f"{__name__}.get_mode_labels",
]
