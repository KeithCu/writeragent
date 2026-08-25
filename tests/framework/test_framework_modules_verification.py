# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis / CrossHair verification for openrouter_model_id and ast_stmt_edit."""

from __future__ import annotations

import datetime as dt
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.deal_shim import DEAL_MAX_TOKEN
from plugin.framework.openrouter_model_id import (
    _split_suffix,
    resolve_openrouter_catalog_id,
    openrouter_model_ids_equivalent,
)
from plugin.framework.ast_stmt_edit import (
    remove_expr_statements,
)
from plugin.framework.default_models import (
    resolve_model_id,
    get_provider_defaults,
    DEFAULT_MODELS,
)
from plugin.framework.constants import (
    get_local_timezone,
    now_aware,
)


@given(model_id=st.text(max_size=DEAL_MAX_TOKEN))
@settings(max_examples=100)
def test_openrouter_model_id_contracts(model_id: str) -> None:
    base, suff = _split_suffix(model_id)
    assert isinstance(base, str)
    assert suff is None or isinstance(suff, str)
    
    cid = resolve_openrouter_catalog_id(model_id)
    assert isinstance(cid, str)
    
    # Reflexive equivalence
    assert openrouter_model_ids_equivalent(model_id, model_id) is True


@given(id_a=st.text(max_size=DEAL_MAX_TOKEN), id_b=st.text(max_size=DEAL_MAX_TOKEN))
def test_openrouter_model_ids_equivalent_symmetric(id_a: str, id_b: str) -> None:
    eq1 = openrouter_model_ids_equivalent(id_a, id_b)
    eq2 = openrouter_model_ids_equivalent(id_b, id_a)
    assert eq1 == eq2


@given(src=st.sampled_from([
    "x = 1\ngrammar_obs('test')\ny = 2",
    "if True:\n    grammar_obs('clean')\n",
    "def foo():\n    xl('data')\n    return 42",
]))
def test_remove_expr_statements_contracts(src: str) -> None:
    def _match(node):
        import ast
        return isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) in ("grammar_obs", "xl")

    res_src, count = remove_expr_statements(src, _match)
    assert isinstance(res_src, str)
    assert isinstance(count, int)
    assert count >= 0
    # Output must be valid Python syntax
    import ast
    ast.parse(res_src)


@given(provider=st.sampled_from(["openrouter", "google", "together", "ollama", "unknown", ""]))
def test_default_models_contracts(provider: str) -> None:
    defaults = get_provider_defaults(provider)
    assert isinstance(defaults, dict)
    for model in DEFAULT_MODELS:
        mid = resolve_model_id(model, provider)
        assert mid is None or isinstance(mid, str)


def test_timezone_utilities_contracts() -> None:
    tz = get_local_timezone()
    assert tz is None or isinstance(tz, dt.tzinfo)
    
    now = now_aware()
    assert isinstance(now, dt.datetime)
    assert now.tzinfo is not None
