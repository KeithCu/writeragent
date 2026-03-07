# Minimal stub for WriterAgent's search_web path. The search_web tool only uses
# DuckDuckGoSearchTool and VisitWebpageTool; this module exists so that
# default_tools and agents can import without failure. PythonInterpreterTool
# is never invoked in that path.

from typing import Any

# Re-export same list as utils.py for PythonInterpreterTool.__init__
BASE_BUILTIN_MODULES = [
    "collections",
    "datetime",
    "itertools",
    "math",
    "queue",
    "random",
    "re",
    "stat",
    "statistics",
    "time",
    "unicodedata",
]

MAX_EXECUTION_TIME_SECONDS = 30

# PythonInterpreterTool uses this as static_tools; can be empty for stub.
BASE_PYTHON_TOOLS = {}


def evaluate_python_code(
    code: str,
    state: dict | None = None,
    static_tools: dict | None = None,
    authorized_imports: list | None = None,
    timeout_seconds: float = MAX_EXECUTION_TIME_SECONDS,
) -> tuple[Any, bool]:
    """Stub: not used in search_web path. Returns (string result, is_final_answer)."""
    if state is None:
        state = {}
    state.setdefault("_print_outputs", [])
    return ("(python_interpreter not available in this build)", False)


def fix_final_answer_code(code: str) -> str:
    """Stub: returns code unchanged. Used by CodeAgent, not ToolCallingAgent."""
    return code


class _ExecutionResult:
    """Minimal result object for executor(code) with .logs attribute."""

    def __init__(self):
        self.logs = []


class PythonExecutor:
    """Base type for Python executors. Stub only."""

    def __call__(self, code: str) -> _ExecutionResult:
        return _ExecutionResult()


class LocalPythonExecutor(PythonExecutor):
    """Stub for local Python execution. Not used when tools are only web search + visit."""

    def __init__(self, authorized_imports: list | None = None, **kwargs: Any) -> None:
        self.authorized_imports = authorized_imports or []
        self._kwargs = kwargs

    def __call__(self, code: str) -> _ExecutionResult:
        return _ExecutionResult()
