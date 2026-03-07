import sys
import os
import json

# Mocking ctx for a standalone test
class MockCtx:
    def getServiceManager(self):
        return self
    def createInstanceWithContext(self, name, ctx):
        return self
    @property
    def UserConfig(self):
        return "file:///tmp"

def test_pricing():
    sys.path.append("/home/keithcu/Desktop/Python/writeragent")
    from plugin.framework.pricing import calculate_cost, fetch_openrouter_pricing, get_model_pricing
    
    ctx = MockCtx()
    print("Testing pricing fetch...")
    # This might fail in a restricted env without internet, but let's see
    try:
        fetch_openrouter_pricing(ctx, force=True)
    except Exception as e:
        print(f"Fetch failed (expected if no net): {e}")

    # Mock usage
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "cost": 0.00123
    }
    
    cost = calculate_cost(ctx, usage, "gpt-4o")
    print(f"Calculated cost (should be 0.00123): {cost}")
    
    usage_no_cost = {
        "prompt_tokens": 1000000,
        "completion_tokens": 1000000
    }
    cost_fallback = calculate_cost(ctx, usage_no_cost, "non-existent-model")
    print(f"Fallback cost for 2M tokens (should be $2.0): {cost_fallback}")

if __name__ == "__main__":
    test_pricing()
