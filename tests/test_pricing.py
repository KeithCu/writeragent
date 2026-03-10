import pytest
import os

from plugin.framework.pricing import calculate_cost

def test_calculate_cost_from_usage_cost_string():
    # If the usage dict provides a cost string directly, use that.
    ctx = None
    usage = {"cost": "0.50"}
    assert calculate_cost(ctx, usage, "model1") == 0.5

def test_calculate_cost_fallback():
    # Since get_model_pricing interacts with config/cache we avoid testing it directly without mocks,
    # but we can test the fallback calculation logic.
    ctx = None
    usage = {"prompt_tokens": 1000000, "completion_tokens": 0}

    # We monkeypatch the internal get_model_pricing to None specifically for this test,
    # instead of touching sys.modules
    import plugin.framework.pricing as pricing
    old_get = pricing.get_model_pricing
    try:
        pricing.get_model_pricing = lambda c, m: None
        # With get_model_pricing returning None, this will hit the fallback multiplier (0.000001 per token)
        assert calculate_cost(ctx, usage, "model_no_exist") == 1.0
    finally:
        pricing.get_model_pricing = old_get

def test_calculate_cost_empty():
    assert calculate_cost(None, None, "model1") == 0.0
