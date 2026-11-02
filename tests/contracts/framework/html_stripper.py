# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.html_stripper."""

from __future__ import annotations

import deal

from plugin.framework import html_stripper as _prod
from tests.contracts.bounds import DEAL_MAX_HTML_CHUNK, str_bounded

escape_prose_angle_brackets = deal.post(lambda result: isinstance(result, str))(
    _prod.escape_prose_angle_brackets
)

strip_html_tags = deal.post(lambda result: isinstance(result, str))(_prod.strip_html_tags)

html_to_search_text = deal.post(lambda result: isinstance(result, str))(_prod.html_to_search_text)


class StreamingHTMLStripper(_prod.StreamingHTMLStripper):
    _feed_chunk = deal.post(lambda result: isinstance(result, str))(
        deal.pre(lambda self, chunk: str_bounded(chunk, DEAL_MAX_HTML_CHUNK))(
            _prod.StreamingHTMLStripper._feed_chunk
        )
    )
    feed = deal.post(lambda result: isinstance(result, str))(_prod.StreamingHTMLStripper.feed)
    finalize = deal.post(lambda result: isinstance(result, str))(_prod.StreamingHTMLStripper.finalize)


PYTEST = [
    "escape_prose_angle_brackets",
    "strip_html_tags",
    "html_to_search_text",
    "StreamingHTMLStripper",
]
TARGETS: list[str] = []
