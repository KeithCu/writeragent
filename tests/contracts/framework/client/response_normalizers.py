# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.client.response_normalizers."""

from __future__ import annotations

from typing import Any

import deal

from plugin.framework.client import response_normalizers as _prod


def _extracted_images_well_formed(result: list[dict[str, Any]]) -> bool:
    return isinstance(result, list) and all(
        isinstance(x, dict)
        and (
            (isinstance(x.get("mime_type"), str) and isinstance(x.get("data"), str))
            or isinstance(x.get("url"), str)
        )
        for x in result
    )


def _string_content_has_no_data_uri(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, str):
        return True
    return _prod._DATA_URI_IMAGE_RE.search(content) is None


strip_leaked_chat_template_control_tokens = deal.ensure(
    lambda content, result: len(result) <= len(content or "")
)(
    deal.ensure(
        lambda content, result: _prod._CHAT_TEMPLATE_CONTROL_TOKEN_RE.search(result) is None
    )(deal.post(lambda result: isinstance(result, str))(_prod.strip_leaked_chat_template_control_tokens))
)

extract_and_strip_images_from_message = deal.ensure(
    lambda *args, result=None, **kwargs: _string_content_has_no_data_uri(args[0])
)(
    deal.post(lambda result: _extracted_images_well_formed(result))(
        deal.pre(lambda *args, **kwargs: bool(args) and isinstance(args[0], dict))(
            _prod.extract_and_strip_images_from_message
        )
    )
)

PYTEST = [
    "strip_leaked_chat_template_control_tokens",
    "extract_and_strip_images_from_message",
]
TARGETS: list[str] = []
