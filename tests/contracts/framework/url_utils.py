# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.framework.url_utils."""

from __future__ import annotations

import deal

from plugin.framework import url_utils as _prod

get_api_version_suffix = deal.post(
    lambda result: isinstance(result, str) and result.startswith("/")
)(_prod.get_api_version_suffix)

normalize_endpoint_url = deal.ensure(
    lambda url, is_openwebui=False, result="": bool(isinstance(url, str) and url.strip())
    or result == ""
)(deal.post(lambda result: isinstance(result, str))(_prod.normalize_endpoint_url))

get_url_hostname = deal.post(lambda result: isinstance(result, str))(_prod.get_url_hostname)
get_url_domain = deal.post(lambda result: isinstance(result, str))(_prod.get_url_domain)
get_url_path = deal.post(lambda result: isinstance(result, str))(_prod.get_url_path)
get_url_query_dict = deal.post(lambda result: isinstance(result, dict))(_prod.get_url_query_dict)
get_url_path_and_query = deal.post(lambda result: isinstance(result, str))(
    _prod.get_url_path_and_query
)
is_pdf_url = deal.post(lambda result: isinstance(result, bool))(_prod.is_pdf_url)

PYTEST = [
    "get_api_version_suffix",
    "normalize_endpoint_url",
    "get_url_hostname",
    "get_url_domain",
    "get_url_path",
    "get_url_query_dict",
    "get_url_path_and_query",
    "is_pdf_url",
]
TARGETS: list[str] = []
