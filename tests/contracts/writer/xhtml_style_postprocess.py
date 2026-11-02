"""Deal contracts for plugin.writer.xhtml_style_postprocess."""

from __future__ import annotations

import deal

from plugin.writer.xhtml_style_postprocess import (
    compact_lo_style_name as _compact_lo_style_name,
    decode_lo_css_class_suffix as _decode_lo_css_class_suffix,
    parse_style_block as _parse_style_block,
    xhtml_has_paragraph_autostyle as _xhtml_has_paragraph_autostyle,
)

decode_lo_css_class_suffix = deal.post(lambda result: isinstance(result, str))(
    _decode_lo_css_class_suffix
)

compact_lo_style_name = deal.post(lambda result: isinstance(result, str) and " " not in result)(
    _compact_lo_style_name
)

xhtml_has_paragraph_autostyle = deal.post(lambda result: isinstance(result, bool))(
    _xhtml_has_paragraph_autostyle
)

parse_style_block = deal.post(
    lambda result: isinstance(result, tuple)
    and len(result) == 2
    and isinstance(result[0], dict)
    and isinstance(result[1], dict)
)(_parse_style_block)

PYTEST = [
    "decode_lo_css_class_suffix",
    "compact_lo_style_name",
    "xhtml_has_paragraph_autostyle",
    "parse_style_block",
]
TARGETS: list[str] = []
