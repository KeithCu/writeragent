# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.tool_schema."""

from __future__ import annotations

import deal

from plugin.framework import tool_schema as _prod

_make_optional_scalar_nullable = deal.ensure(
    lambda prop_schema, result: not isinstance(prop_schema, dict) or isinstance(result, dict)
)(_prod._make_optional_scalar_nullable)

_normalize_schema_for_strict_providers = deal.ensure(
    lambda params, result: not isinstance(result, dict) or result.get("required") != []
)(
    deal.ensure(
        lambda params, result: (not isinstance(params, dict) or not params) or isinstance(result, dict)
    )(_prod._normalize_schema_for_strict_providers)
)

to_openai_schema = deal.ensure(
    lambda tool, doc_type=None, result=None, **kwargs: result is not None
    and isinstance(result, dict)
    and result.get("function", {}).get("name") == tool.name
)(
    deal.post(
        lambda result: isinstance(result, dict)
        and result.get("type") == "function"
        and isinstance(result.get("function"), dict)
    )(
        deal.pre(lambda tool, **kwargs: getattr(tool, "name", None) is not None)(
            _prod.to_openai_schema
        )
    )
)

to_mcp_schema = deal.ensure(
    lambda tool, doc_type=None, result=None, **kwargs: result is not None
    and isinstance(result, dict)
    and result.get("name") == tool.name
)(
    deal.post(
        lambda result: isinstance(result, dict)
        and "inputSchema" in result
        and result.get("name") is not None
    )(
        deal.pre(lambda tool, **kwargs: getattr(tool, "name", None) is not None)(
            _prod.to_mcp_schema
        )
    )
)

PYTEST = [
    "_make_optional_scalar_nullable",
    "_normalize_schema_for_strict_providers",
    "to_openai_schema",
    "to_mcp_schema",
]
TARGETS: list[str] = []
