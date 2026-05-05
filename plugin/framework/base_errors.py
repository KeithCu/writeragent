# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Base exceptions for WriterAgent to break circular dependencies."""

from typing import Any
from plugin.framework.i18n import _


class WriterAgentException(Exception):
    """Base exception for all WriterAgent errors.

    Backwards compatibility: some older code paths use `context=` while
    newer code uses `details=` for the JSON error payload.
    """

    def __init__(self, message, code="INTERNAL_ERROR", context=None, details=None):
        # Accept both `context` and `details` (alias).
        if details is None and context is not None:
            details = context

        super().__init__(message)
        self.message = _(str(message))
        self.code = code
        self.details = details or {}
        # Keep legacy attribute name too (some callers reference `.context`).
        self.context = self.details


class ConfigError(WriterAgentException):
    """Configuration, Auth, or Settings issues."""

    def __init__(self, message, code="CONFIG_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


class NetworkError(WriterAgentException):
    """HTTP/Network related failures."""

    def __init__(self, message, code="NETWORK_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


def format_error_payload(e: Exception) -> dict[str, Any]:
    """Format an exception into the standard JSON error payload schema."""
    if isinstance(e, WriterAgentException):
        payload: dict[str, Any] = {
            "status": "error",
            "code": e.code,
            "message": e.message,
        }
        if e.details:
            payload["details"] = e.details
        return payload

    # For unexpected exceptions
    return {
        "status": "error",
        "code": "INTERNAL_ERROR",
        "message": str(e),
        "details": {
            "type": type(e).__name__,
        },
    }
