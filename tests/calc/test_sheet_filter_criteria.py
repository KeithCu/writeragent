# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Sheet-filter criterion ingest must return UnoObjectError, not a deal assertion."""

from plugin.framework.errors import UnoObjectError
from plugin.calc.sheet_filter_criteria import parse_sheet_filter_criterion


def test_odd_operator_is_uno_error_not_precontract() -> None:
    import pytest

    raw = {"field": 0, "operator": "égal" * 40, "connection": "AND", "extra": 1}
    with pytest.raises(UnoObjectError):
        parse_sheet_filter_criterion(raw, False)
