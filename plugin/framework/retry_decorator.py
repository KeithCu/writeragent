import time
import random
from functools import wraps
from typing import Callable, TypeVar, Any

from plugin.framework.errors import NetworkError

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
                    if attempt == max_attempts:
                        break

                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay = delay * random.uniform(0.5, 1.5)  # Add jitter

                    if logger:
                        logger.warning(
                            f"Attempt {attempt} failed: {str(e)}. "
                            f"Retrying in {delay:.2f}s..."
                        )

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
