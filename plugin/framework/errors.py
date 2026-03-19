"""
Centralized exception hierarchy for WriterAgent.

All custom exceptions should inherit from WriterAgentException.
"""

class WriterAgentException(Exception):
    """Base exception for all WriterAgent errors."""
    def __init__(self, message, code="INTERNAL_ERROR", context=None):
        super().__init__(message)
        self.code = code
        self.context = context or {}

class ConfigError(WriterAgentException):
    """Configuration, Auth, or Settings issues."""
    def __init__(self, message, code="CONFIG_ERROR", context=None):
        super().__init__(message, code, context)

class NetworkError(WriterAgentException):
    """HTTP/Network related failures."""
    def __init__(self, message, code="NETWORK_ERROR", context=None):
        super().__init__(message, code, context)

class UnoObjectError(WriterAgentException):
    """LibreOffice UNO interface failures (stale docs, missing properties)."""
    def __init__(self, message, code="UNO_ERROR", context=None):
        super().__init__(message, code, context)

class ToolExecutionError(WriterAgentException):
    """Tool invocation and execution failures."""
    def __init__(self, message, code="TOOL_ERROR", context=None):
        super().__init__(message, code, context)

class AgentParsingError(WriterAgentException):
    """LLM output / JSON parsing failures."""
    def __init__(self, message, code="PARSE_ERROR", context=None):
        super().__init__(message, code, context)
