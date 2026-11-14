# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for openrouter_model_id and ast_stmt_edit."""

from __future__ import annotations

import datetime as dt
from hypothesis import given
from hypothesis import strategies as st

from plugin.framework.deal_shim import DEAL_MAX_TOKEN
from plugin.framework.openrouter_model_id import (
    openrouter_model_ids_equivalent,
)
from plugin.framework.constants import (
    now_aware,
)


@given(id_a=st.text(max_size=DEAL_MAX_TOKEN), id_b=st.text(max_size=DEAL_MAX_TOKEN))
def test_openrouter_model_ids_equivalent_symmetric(id_a: str, id_b: str) -> None:
    assert openrouter_model_ids_equivalent(id_a, id_a) is True
    eq1 = openrouter_model_ids_equivalent(id_a, id_b)
    eq2 = openrouter_model_ids_equivalent(id_b, id_a)
    assert eq1 == eq2


def test_timezone_utilities_contracts() -> None:
    now = now_aware()
    assert isinstance(now, dt.datetime)
    assert now.tzinfo is not None
