"""Every error ``code`` the compute service sends, each next to its HTTP status.

``HTTP_STATUS`` is the one map: the server statuses a worker error payload
from it, and ``tests/compute_service/test_error_codes.py`` checks the README
status table against it. A code at 200 leaves the route default in charge
(200 on execute and vision, 500 on session reset).
"""

from __future__ import annotations

HTTP_STATUS: dict[str, str] = {}


def _code(name: str, status: str) -> str:
    HTTP_STATUS[name] = status
    return name


# 200: an evaluation result the sheet shows as text.
EXECUTION_TIMEOUT = _code("EXECUTION_TIMEOUT", "200 OK")

# 400: the client sent something the service rejects.
BAD_REQUEST_BODY = _code("BAD_REQUEST_BODY", "400 Bad Request")
INVALID_REQUEST = _code("INVALID_REQUEST", "400 Bad Request")
INVALID_SESSION_ID = _code("INVALID_SESSION_ID", "400 Bad Request")
CODE_TOO_LARGE = _code("CODE_TOO_LARGE", "400 Bad Request")
MISSING_IMAGE_SOURCE = _code("MISSING_IMAGE_SOURCE", "400 Bad Request")
INVALID_FILE_PATH = _code("INVALID_FILE_PATH", "400 Bad Request")
FILE_PATH_DENIED = _code("FILE_PATH_DENIED", "400 Bad Request")
FILE_NOT_FOUND = _code("FILE_NOT_FOUND", "400 Bad Request")
NOT_A_FILE = _code("NOT_A_FILE", "400 Bad Request")
FILE_READ_ERROR = _code("FILE_READ_ERROR", "400 Bad Request")

# 401: Bearer auth failed on a protected route.
UNAUTHORIZED = _code("UNAUTHORIZED", "401 Unauthorized")

# 403: keyless mode refuses a cross-origin or non-loopback caller.
CROSS_ORIGIN_REFUSED = _code("CROSS_ORIGIN_REFUSED", "403 Forbidden")

# 408: the HTTP layer could not read the full request body in time.
READ_TIMEOUT = _code("READ_TIMEOUT", "408 Request Timeout")

# 413: a request, result, or vision file over its size cap.
PAYLOAD_TOO_LARGE = _code("PAYLOAD_TOO_LARGE", "413 Payload Too Large")
RESULT_TOO_LARGE = _code("RESULT_TOO_LARGE", "413 Payload Too Large")
FILE_TOO_LARGE = _code("FILE_TOO_LARGE", "413 Payload Too Large")

# 500: the cell may have run, or the response itself is the fault; a retry could repeat it.
# A formula WORKER_EXECUTION_ERROR inside forwarded result_json stays 200.
WORKER_CRASHED = _code("WORKER_CRASHED", "500 Internal Server Error")
WORKER_IPC_ERROR = _code("WORKER_IPC_ERROR", "500 Internal Server Error")
EMPTY_RESPONSE = _code("EMPTY_RESPONSE", "500 Internal Server Error")
RESULT_DECODE_ERROR = _code("RESULT_DECODE_ERROR", "500 Internal Server Error")
WORKER_EXECUTION_ERROR = _code("WORKER_EXECUTION_ERROR", "500 Internal Server Error")
VISION_WORKER_ERROR = _code("VISION_WORKER_ERROR", "500 Internal Server Error")
INTERNAL_ERROR = _code("INTERNAL_ERROR", "500 Internal Server Error")

# 501: vision is off on this server.
VISION_SERVICE_DISABLED = _code("VISION_SERVICE_DISABLED", "501 Not Implemented")

# 503: the cell never ran; a proxy may retry.
WORKER_POOL_BUSY = _code("WORKER_POOL_BUSY", "503 Service Unavailable")
VISION_POOL_BUSY = _code("VISION_POOL_BUSY", "503 Service Unavailable")
SERVICE_SHUTDOWN = _code("SERVICE_SHUTDOWN", "503 Service Unavailable")
WORKER_SPAWN_FAILED = _code("WORKER_SPAWN_FAILED", "503 Service Unavailable")
WORKER_PIPE_BROKEN = _code("WORKER_PIPE_BROKEN", "503 Service Unavailable")
QUEUE_TIMEOUT = _code("QUEUE_TIMEOUT", "503 Service Unavailable")
VISION_UNAVAILABLE = _code("VISION_UNAVAILABLE", "503 Service Unavailable")
