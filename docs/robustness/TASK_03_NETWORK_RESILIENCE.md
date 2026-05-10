# Task 3: Network Resilience & Retry Logic

## Objective
Implement robust network error handling with retry logic for transient failures.

## Scope
- `plugin/framework/client/llm_client.py` - LLM client
- `plugin/mcp/mcp_protocol.py` - MCP server protocol
- Create reusable retry decorator

## Critical Issues to Fix

### 1. LLM Client Network Errors
**Current:** Limited error handling for network issues

**Fix:**
- Add retry logic with exponential backoff
- Handle specific network exceptions
- Add retry counters and limits

### 2. MCP Protocol Errors
**Current:** Broad exception handling in protocol operations

**Fix:**
- Specific exception handling for protocol errors
- Timeout handling
- Graceful degradation

## Deliverables
1. **retry_decorator.py** - Reusable retry logic
2. **client.py** - Updated with network resilience
3. **mcp_protocol.py** - Updated with protocol error handling
4. **test_network_resilience.py** - Unit tests

## Implementation Steps

### 1. Create Retry Decorator
Create `plugin/framework/retry_decorator.py`:

```python
import time
import random
from functools import wraps
from typing import Callable, TypeVar, Any
from plugin.framework.errors import NetworkError
from plugin.framework.logging import debug_log

T = TypeVar('T')

def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
    retry_exceptions: tuple = (ConnectionError, TimeoutError, OSError),
    logger=None
) -> Callable:
    """Retry decorator with exponential backoff.
    
    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Base delay in seconds
        max_delay: Maximum delay in seconds
        retry_exceptions: Tuple of exceptions to retry on
        logger: Optional logger for retry logging
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            attempt = 1
            last_exception = None
            
            while attempt <= max_attempts:
                try:
                    return func(*args, **kwargs)
                except retry_exceptions as e:
                    last_exception = e
                    if logger:
                        logger.warning(
                            f"Attempt {attempt} failed: {str(e)}. "
                            f"Retrying in {base_delay:.2f}s..."
                        )
                    
                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay = delay * random.uniform(0.5, 1.5)  # Add jitter
                    time.sleep(delay)
                    attempt += 1
            
            # If all attempts failed, raise the last exception
            if last_exception:
                raise NetworkError(
                    f"Operation failed after {max_attempts} attempts",
                    code="NETWORK_RETRY_FAILED",
                    details={
                        "attempts": max_attempts,
                        "last_error": str(last_exception),
                        "type": type(last_exception).__name__
                    }
                ) from last_exception
            
            # Shouldn't reach here
            raise NetworkError("Unexpected retry failure")
        
        return wrapper
    
    return decorator
```

### 2. Update LLM Client
In `plugin/framework/client/llm_client.py`:

```python
from plugin.framework.retry_decorator import retry_with_backoff
from plugin.framework.errors import NetworkError

class LlmClient:
    @retry_with_backoff(
        max_attempts=3,
        base_delay=0.2,
        max_delay=3.0,
        retry_exceptions=(ConnectionError, TimeoutError, OSError, http.client.HTTPException),
        logger=log
    )
    def _make_request(self, method, endpoint, headers, data=None, stream=False):
        # Existing implementation
        pass
```

### 3. Update MCP Protocol
In `plugin/mcp/mcp_protocol.py`:

```python
from plugin.framework.retry_decorator import retry_with_backoff
from plugin.framework.errors import NetworkError, WriterAgentException

class MCPProtocol:
    @retry_with_backoff(
        max_attempts=2,
        base_delay=0.1,
        max_delay=1.0,
        retry_exceptions=(WriterAgentException, TimeoutError),
        logger=log
    )
    def execute_tool(self, tool_name, args):
        # Existing implementation
        pass
```

### 4. Add Network-Specific Error Handling
```python
def handle_network_error(e, context="network_operation"):
    """Handle network errors with appropriate logging and user messaging."""
    error_payload = format_error_payload(e)
    
    if isinstance(e, NetworkError):
        log.error(f"Network error [{context}]: {e.message}", 
                 extra={"network_error": error_payload})
        agent_log("network_error", {
            "context": context,
            "error": error_payload,
            "timestamp": time.time()
        })
    else:
        # Wrap non-network exceptions
        wrapped = NetworkError(
            f"Network-related error in {context}",
            code="NETWORK_WRAPPED_ERROR",
            details={
                "original_error": str(e),
                "type": type(e).__name__,
                "context": context
            }
        )
        log.error(f"Wrapped network error [{context}]: {wrapped.message}")
        error_payload = format_error_payload(wrapped)
    
    return error_payload
```

## Testing
Create comprehensive tests for:
- Retry logic with different exception types
- Exponential backoff timing
- Network error scenarios
- MCP protocol failures

## Success Criteria
- ✅ Retry decorator implemented and tested
- ✅ LLM client has network resilience
- ✅ MCP protocol handles errors gracefully
- ✅ Network errors logged with context
- ✅ Comprehensive test coverage
