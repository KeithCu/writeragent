#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# LibreOffice 24.8 bundles Python 3.9, which evaluates annotations at import time without this future import.
from __future__ import annotations

import ast
import builtins
import difflib
import importlib.abc
import importlib.machinery
import inspect
import logging
import math
import operator
import re
import string as _stdlib_string
import _string
import threading
import functools
import time
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from importlib import import_module
from types import BuiltinFunctionType, FunctionType, ModuleType
from typing import Any

from .utils import BASE_BUILTIN_MODULES, truncate_content


logger = logging.getLogger(__name__)


class InterpreterError(ValueError):
    """
    An error raised when the interpreter cannot evaluate a Python expression, due to syntax error or unsupported
    operations.
    """

    pass


ERRORS = {
    name: getattr(builtins, name)
    for name in dir(builtins)
    if isinstance(getattr(builtins, name), type) and issubclass(getattr(builtins, name), BaseException)
}

DEFAULT_MAX_LEN_OUTPUT = 50000
MAX_OPERATIONS = 10000000
MAX_WHILE_ITERATIONS = 1000000
MAX_EXECUTION_TIME_SECONDS = 30
ALLOWED_DUNDER_METHODS = frozenset({"__init__", "__str__", "__repr__"})
# np.__version__ (and pd/sp/...) is a read-only string, not __class__/__globals__. Allow version only.
ALLOWED_DUNDER_ATTRIBUTES = frozenset({"__version__"})

_OPS_KEY = object()
_PRINT_OUTPUTS_KEY = object()
_ACTIVE_EXCEPTIONS_KEY = object()
_CLASS_CELL_KEY = object()
_WRAPPED_TOOLS_KEY = object()

# Set when thread-pool fallback timeout expires so the worker process exits after writing the response.
abandoned_timeout: bool = False


class _Ops:
    __slots__ = ("count", "deadline", "timeout_seconds")

    def __init__(
        self,
        count: int = 0,
        deadline: float | None = None,
        timeout_seconds: int | None = None,
    ):
        self.count = count
        self.deadline = deadline
        self.timeout_seconds = timeout_seconds


def print_outputs_from_state(state: dict[Any, Any] | None) -> str:
    """Return captured stdout from the execution state."""
    if not isinstance(state, dict):
        return ""
    container = state.get(_PRINT_OUTPUTS_KEY)
    if container is not None:
        return str(container)
    return ""


class PrintContainer:
    def __init__(self, max_length: int = DEFAULT_MAX_LEN_OUTPUT):
        self.max_length = max_length
        self._head = ""
        self._tail = ""
        self._truncated = False

    def append(self, text: str) -> None:
        half = self.max_length // 2
        if not self._truncated:
            if len(self._head) + len(text) <= self.max_length:
                self._head += text
                return
            self._truncated = True
            combined = self._head + text
            self._head = combined[:half]
            self._tail = combined[-half:]
        else:
            self._tail = (self._tail + text)[-half:]

    def __iadd__(self, other: Any) -> PrintContainer:
        """Implements the += operator"""
        self.append(str(other))
        return self

    @property
    def value(self) -> str:
        return str(self)

    @value.setter
    def value(self, val: str) -> None:
        self._head = val
        self._tail = ""
        self._truncated = False

    def __str__(self):
        """String representation"""
        if not self._truncated:
            return self._head
        marker = f"\n..._This content has been truncated to stay below {self.max_length} characters_...\n"
        return self._head + marker + self._tail

    def __repr__(self):
        """Representation for debugging"""
        return f"PrintContainer({str(self)!r})"


_PRINT_STACK = threading.local()


def _get_print_stack() -> list[PrintContainer]:
    stack = getattr(_PRINT_STACK, "stack", None)
    if stack is None:
        stack = []
        _PRINT_STACK.stack = stack
    return stack


def _sandboxed_print(*args: Any, **kwargs: Any) -> None:
    unknown = set(kwargs.keys()) - {"sep", "end", "file", "flush"}
    if unknown:
        raise TypeError(f"print() got an unexpected keyword argument '{next(iter(unknown))}'")
    if kwargs.get("file") is not None:
        raise InterpreterError("print() file= is unsupported in the sandbox")
    stack = _get_print_stack()
    if not stack:
        return None
    container = stack[-1]
    sep = kwargs.get("sep", " ")
    if sep is None:
        sep = " "
    end = kwargs.get("end", "\n")
    if end is None:
        end = "\n"
    container.append(sep.join(map(str, args)) + end)
    return None


def is_forbidden_dunder_attribute(name: str) -> bool:
    return (
        name.startswith("__")
        and name.endswith("__")
        and name not in ALLOWED_DUNDER_ATTRIBUTES
        and name not in ALLOWED_DUNDER_METHODS
    )


_GETATTR_SENTINEL = object()


class _SafeFormatter(_stdlib_string.Formatter):
    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        try:
            first, rest = _string.formatter_field_name_split(field_name)
        except ValueError:
            return super().get_field(field_name, args, kwargs)
        for is_attr, i in rest:
            if is_attr and is_forbidden_dunder_attribute(str(i)):
                raise InterpreterError(f"Forbidden access to dunder attribute: {i}")
        return super().get_field(field_name, args, kwargs)


_NUMPY_CALL_DENIED = ("numpy.ctypeslib", "numpy.f2py", "numpy.distutils", "numpy.testing")

_NUMPY_PICKLE_RULES: dict[str, tuple[bool, int | None, bool]] = {
    "save": (True, 2, True),
    "savez": (True, None, True),
    "savez_compressed": (True, None, True),
    "write_array": (True, 3, True),
    "load": (False, 2, False),
    "read_array": (False, 1, False),
}


def _apply_pickle_policy(name: str, args: list[Any] | tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    rule = _NUMPY_PICKLE_RULES.get(name)
    if rule is None:
        return kwargs
    _default_allow, pos_idx, force_false = rule
    allow = kwargs.get("allow_pickle")
    if allow is None and pos_idx is not None and len(args) > pos_idx:
        allow = args[pos_idx]
    if allow is True:
        raise InterpreterError(f"numpy.{name}(allow_pickle=True) is not allowed")
    if allow is None and force_false:
        kwargs = dict(kwargs)
        kwargs["allow_pickle"] = False
    return kwargs


_WRAPPED_CALLABLES_WEAK: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_WRAPPED_CALLABLES_BY_ID: dict[int, Any] = {}


def _get_cached_wrapped_callable(func: Any) -> Any:
    try:
        return _WRAPPED_CALLABLES_WEAK.get(func)
    except TypeError:
        return _WRAPPED_CALLABLES_BY_ID.get(id(func))


def _set_cached_wrapped_callable(func: Any, wrapped: Any) -> None:
    try:
        _WRAPPED_CALLABLES_WEAK[func] = wrapped
    except TypeError:
        _WRAPPED_CALLABLES_BY_ID[id(func)] = wrapped


def _maybe_wrap_callable(val: Any) -> Any:
    if not callable(val):
        return val
    module = getattr(val, "__module__", "") or ""
    name = getattr(val, "__name__", "") or ""
    needs_wrap = False
    if module.startswith("numpy"):
        if name == "load_library" or any(module.startswith(p) for p in _NUMPY_CALL_DENIED):
            needs_wrap = True
        elif name in _NUMPY_PICKLE_RULES:
            needs_wrap = True
    elif module.startswith("pandas") and name in ("read_pickle", "to_pickle"):
        needs_wrap = True

    if not needs_wrap:
        return val

    cached = _get_cached_wrapped_callable(val)
    if cached is not None:
        return cached

    @wraps(val)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        if name == "load_library" or any(module.startswith(p) for p in _NUMPY_CALL_DENIED):
            raise InterpreterError(f"Forbidden call to {module}.{name}")
        if module.startswith("pandas") and name in ("read_pickle", "to_pickle"):
            raise InterpreterError(f"Forbidden call to {module}.{name}")
        if name in _NUMPY_PICKLE_RULES:
            kwargs = _apply_pickle_policy(name, args, kwargs)
        return val(*args, **kwargs)

    _set_cached_wrapped_callable(val, _wrapped)
    return _wrapped


def safe_getattr(obj: Any, name: str, default: Any = _GETATTR_SENTINEL) -> Any:
    if is_forbidden_dunder_attribute(name):
        raise InterpreterError(f"Forbidden access to dunder attribute: {name}")

    if name in ("format", "format_map"):
        if isinstance(obj, str):
            if name == "format":
                def _str_format(*args: Any, **kwargs: Any) -> str:
                    return _SafeFormatter().vformat(obj, args, kwargs)
                return _str_format
            else:
                def _str_format_map(mapping: Any) -> str:
                    return _SafeFormatter().vformat(obj, (), mapping)
                return _str_format_map
        elif isinstance(obj, type) and issubclass(obj, str):
            if name == "format":
                def _type_str_format(self: Any, *args: Any, **kwargs: Any) -> str:
                    if not isinstance(self, str):
                        raise TypeError(f"descriptor 'format' requires a 'str' object but received a '{type(self).__name__}'")
                    return _SafeFormatter().vformat(self, args, kwargs)
                return _type_str_format
            else:
                def _type_str_format_map(self: Any, mapping: Any) -> str:
                    if not isinstance(self, str):
                        raise TypeError(f"descriptor 'format_map' requires a 'str' object but received a '{type(self).__name__}'")
                    return _SafeFormatter().vformat(self, (), mapping)
                return _type_str_format_map

    if default is _GETATTR_SENTINEL:
        val = getattr(obj, name)
    else:
        val = getattr(obj, name, default)

    return _maybe_wrap_callable(val)


def nodunder_getattr(obj, name, default=_GETATTR_SENTINEL):
    return safe_getattr(obj, name, default)


def _nodunder_hasattr(obj: Any, name: Any) -> bool:
    if isinstance(name, str) and is_forbidden_dunder_attribute(name):
        raise InterpreterError(f"Forbidden access to dunder attribute: {name}")
    return hasattr(obj, name)


def _reject_shared_object_mutation(obj: Any) -> None:
    """Block assignment onto a module or a type.

    ``import numpy`` returns the live module (wrapping it SIGILLs).
    ``setattr(np, "array", ...)`` or ``list.append = ...`` would change every
    later cell and trusted action in this process. Instance assignment stays.
    """
    import types

    if isinstance(obj, (types.ModuleType, type)):
        raise InterpreterError("Cannot assign attributes on a module or a type")


def _guarded_setattr(obj: Any, name: str, value: Any) -> None:
    # Previously, _guarded_setattr did not check is_forbidden_dunder_attribute,
    # allowing setattr(obj, "__class__", ...) to bypass the dunder attribute guard.
    if is_forbidden_dunder_attribute(name):
        raise InterpreterError(f"Forbidden access to dunder attribute: {name}")
    _reject_shared_object_mutation(obj)
    setattr(obj, name, value)


def _reject_shared_object_deletion(obj: Any) -> None:
    import types

    if isinstance(obj, (types.ModuleType, type)):
        raise InterpreterError("Cannot delete attributes on a module or a type")


def _guarded_delattr(obj: Any, name: str) -> None:
    if is_forbidden_dunder_attribute(name):
        raise InterpreterError(f"Forbidden access to dunder attribute: {name}")
    _reject_shared_object_deletion(obj)
    delattr(obj, name)


def _safe_attrgetter(*attrs: Any, authorized_imports: list[str] | None = None) -> Any:
    # C operator.attrgetter walks past factory checks in one step; walk segments in Python and check each hop.
    if not attrs:
        return operator.attrgetter()
    for attr in attrs:
        if not isinstance(attr, str):
            raise InterpreterError("attrgetter attribute name must be a string")
        for segment in attr.split("."):
            if is_forbidden_dunder_attribute(segment):
                raise InterpreterError(f"Forbidden access to dunder attribute: {segment}")

    def _resolve(obj: Any, attr: str) -> Any:
        cur = obj
        for segment in attr.split("."):
            cur = safe_getattr(cur, segment)
            _check_dangerous_value(cur, authorized_imports)
        return cur

    if len(attrs) == 1:
        return lambda obj: _resolve(obj, attrs[0])
    return lambda obj: tuple(_resolve(obj, attr) for attr in attrs)


def _safe_methodcaller(name: Any, *args: Any, **kwargs: Any) -> Any:
    # Use safe_getattr so methodcaller reaches the format wrapper and dunder checks.
    if not isinstance(name, str):
        raise InterpreterError("methodcaller method name must be a string")
    if is_forbidden_dunder_attribute(name):
        raise InterpreterError(f"Forbidden access to dunder attribute: {name}")

    def _caller(obj: Any) -> Any:
        func = safe_getattr(obj, name)
        return func(*args, **kwargs)

    return _caller



BASE_PYTHON_TOOLS = {
    "print": _sandboxed_print,
    "isinstance": isinstance,
    "range": range,
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
    "set": set,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "log": math.log,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "degrees": math.degrees,
    "radians": math.radians,
    "pow": pow,
    "sqrt": math.sqrt,
    "len": len,
    "sum": sum,
    "max": max,
    "min": min,
    "abs": abs,
    "enumerate": enumerate,
    "zip": zip,
    "reversed": reversed,
    "sorted": sorted,
    "all": all,
    "any": any,
    "map": map,
    "filter": filter,
    "ord": ord,
    "chr": chr,
    "next": next,
    "iter": iter,
    "divmod": divmod,
    "callable": callable,
    "getattr": nodunder_getattr,
    "hasattr": _nodunder_hasattr,
    "setattr": _guarded_setattr,
    "issubclass": issubclass,
    "type": type,
    "complex": complex,
}

_IMPORT_OVERRIDABLE_TOOLS = frozenset(
    k
    for k, v in BASE_PYTHON_TOOLS.items()
    if getattr(v, "__module__", None) == "math" or v is getattr(math, k, None)
)

# Canonical always-blocked dangerous modules (host escape, process spawn, native code, dynamic import, serialization, raw OS, internals)
DANGEROUS_MODULES = [
    # Process / native code
    "subprocess",
    "_posixsubprocess",
    "ctypes",
    "_ctypes",
    "cffi",
    "pty",
    "multiprocessing",
    "_multiprocessing",
    # Dynamic import / builtins side doors
    "builtins",
    "importlib",
    "_frozen_importlib",
    "_frozen_importlib_external",
    "zipimport",
    "runpy",
    # Code-running deserialization
    "pickle",
    "marshal",
    "shelve",
    "dill",
    "cloudpickle",
    # Raw OS modules (bypass the os proxy)
    "posix",
    "nt",
    # Plugin / alias internals
    "plugin",
    "writeragent.framework",
]

_DANGEROUS_CALLABLE_MODULES = (
    frozenset(m for m in DANGEROUS_MODULES if m not in ("builtins", "plugin", "writeragent.framework"))
    | {"os", "socket", "_socket", "shutil", "tempfile", "sqlite3"}
)
_DANGEROUS_BUILTIN_FUNCTIONS = frozenset({"compile", "eval", "exec", "globals", "locals", "__import__"})
_DANGEROUS_SYS_FUNCTIONS = frozenset({"_getframe", "_current_frames", "settrace", "setprofile"})
_PROCESS_CALLABLE_NAMES = frozenset({"create_subprocess_exec", "create_subprocess_shell", "ProcessPoolExecutor"})

SANDBOX_POLICY_CONTEXT: ContextVar[tuple[frozenset[str], frozenset[int]]] = ContextVar(
    "sandbox_policy_context", default=(frozenset(), frozenset())
)


def _check_dangerous_value(val: Any, authorized_imports: list[str] | None = None) -> None:
    if isinstance(val, ModuleType):
        relaxed_roots, _ = SANDBOX_POLICY_CONTEXT.get()
        extras_roots = {entry.split(".", 1)[0] for entry in authorized_imports} if authorized_imports else set()
        try:
            from plugin.scripting.sandbox import active_extra_imports

            for entry in active_extra_imports():
                extras_roots.add(entry.split(".", 1)[0])
        except (ImportError, ModuleNotFoundError):
            pass
        os_allowed = ("os" in relaxed_roots) or ("os" in extras_roots)
        if val.__name__ == "os" and not isinstance(val, _FileOsProxy) and not os_allowed:
            raise InterpreterError(f"Forbidden access to module: {val.__name__}")
        if not _is_module_authorized(val.__name__, authorized_imports):
            raise InterpreterError(f"Forbidden access to module: {val.__name__}")
    elif callable(val):
        mod = getattr(val, "__module__", None)
        fn_name = getattr(val, "__name__", "")
        if mod:
            root = mod.split(".")[0]
            relaxed_roots, file_os_ids = SANDBOX_POLICY_CONTEXT.get()
            extras_roots = {entry.split(".", 1)[0] for entry in authorized_imports} if authorized_imports else set()
            try:
                from plugin.scripting.sandbox import active_extra_imports

                for entry in active_extra_imports():
                    extras_roots.add(entry.split(".", 1)[0])
            except (ImportError, ModuleNotFoundError):
                pass
            allowed_file_ids = file_os_ids or _get_default_file_os_ids()
            if root in _DANGEROUS_CALLABLE_MODULES:
                # os file functions are posix callables, hence the id set.
                if root not in relaxed_roots and id(val) not in allowed_file_ids and root not in extras_roots:
                    raise InterpreterError(f"Forbidden access to function: {fn_name}")
            if root == "builtins" and fn_name in _DANGEROUS_BUILTIN_FUNCTIONS:
                if "builtins" not in extras_roots:
                    raise InterpreterError(f"Forbidden access to function: {fn_name}")
            if root == "sys" and fn_name in _DANGEROUS_SYS_FUNCTIONS:
                if "sys" not in extras_roots:
                    raise InterpreterError(f"Forbidden access to function: {fn_name}")

            os_allowed = (
                ("os" in relaxed_roots)
                or ("os" in extras_roots)
                or ("subprocess" in extras_roots)
                or ("multiprocessing" in extras_roots)
                or ("pty" in extras_roots)
            )
            if not os_allowed:
                if fn_name in _PROCESS_CALLABLE_NAMES or mod.startswith("concurrent.futures.process"):
                    raise InterpreterError(f"Forbidden access to function: {fn_name}")


_SAFE_RESULT_TYPES = frozenset({
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    range,
})


def check_safer_result(result: Any, authorized_imports: list[str] | None = None) -> None:
    """
    Checks if a result is safer according to authorized imports.

    Args:
        result (Any): The result to check.
        authorized_imports (list[str]): List of authorized imports.

    Raises:
        InterpreterError: If the result is not safe
    """
    if type(result) in _SAFE_RESULT_TYPES:
        return
    if result is operator.attrgetter or result is operator.methodcaller:
        raise InterpreterError(f"Forbidden access to operator dispatch type: {getattr(result, '__name__', '')}")
    if isinstance(result, (importlib.machinery.ModuleSpec, importlib.abc.Loader)):
        raise InterpreterError(f"Forbidden access to module loader/spec: {type(result).__name__}")
    elif isinstance(result, dict) and isinstance(result.get("__spec__"), importlib.machinery.ModuleSpec):
        raise InterpreterError(f"Forbidden access to module namespace: {result.get('__name__', '')}")
    _check_dangerous_value(result, authorized_imports)


def safer_eval(func: Callable):
    """
    Decorator to enhance the security of an evaluation function by checking its return value.

    Args:
        func (Callable): Evaluation function to be made safer.

    Returns:
        Callable: Safer evaluation function with return value check.
    """

    @wraps(func)
    def _check_return(
        expression,
        state,
        static_tools,
        custom_tools,
        authorized_imports=BASE_BUILTIN_MODULES,
    ):
        result = func(expression, state, static_tools, custom_tools, authorized_imports=authorized_imports)
        check_safer_result(result, authorized_imports)
        return result

    return _check_return


def safer_func(
    func: Callable,
    authorized_imports: list[str] = BASE_BUILTIN_MODULES,
):
    """
    Decorator to enhance the security of a function call by checking its return value.

    Args:
        func (Callable): Function to be made safer.
        authorized_imports (list[str]): List of authorized imports.

    Returns:
        Callable: Safer function with return value check.
    """
    # If the function is a type, return it directly without wrapping
    if isinstance(func, type):
        return func

    @wraps(func)
    def _check_return(*args, **kwargs):
        result = func(*args, **kwargs)
        check_safer_result(result, authorized_imports)
        return result

    return _check_return


# Control-flow signals (break/continue/return) and timeout previously subclassed
# Exception, causing user `except Exception` blocks in the sandbox to swallow
# returns, breaks, continues, and SIGALRM timeout errors.
# Subclassing BaseException ensures these signals bypass `except Exception` clauses.
class InterpreterSignal(BaseException):
    """Base class for AST control flow signals (break, continue, return)."""

    pass


class BreakException(InterpreterSignal):
    pass


class ContinueException(InterpreterSignal):
    pass


class ReturnException(InterpreterSignal):
    def __init__(self, value):
        self.value = value


class ExecutionTimeoutError(BaseException):
    """Exception raised when code execution exceeds the maximum allowed time."""

    pass



def timeout(timeout_seconds: Any):
    """Decorator to limit execution time via SIGALRM on main thread or ThreadPoolExecutor fallback.

    On Linux main thread, SIGALRM unwinds the stack cleanly. On Windows or when
    SIGALRM raises, fallback thread execution marks abandoned_timeout on timeout
    so worker processes terminate cleanly after writing their error response.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise TypeError("executor timeout_seconds must be a whole number of seconds >= 1")

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Executing matplotlib or other non-thread-safe compiled C-extensions
            # inside a ThreadPoolExecutor thread crashes (SIGILL). On the main
            # thread, signal.alarm runs the cell on that thread. Fall back to a
            # thread only when alarm cannot be installed.
            import signal
            import time

            start_time = time.monotonic()

            def sigalrm_handler(signum, frame):
                raise ExecutionTimeoutError(
                    f"Code execution exceeded the maximum execution time of {timeout_seconds} seconds"
                )

            try:
                old_handler = signal.signal(signal.SIGALRM, sigalrm_handler)
                previous_alarm = signal.alarm(timeout_seconds)
                # If an outer alarm was already active and sooner than timeout_seconds,
                # keep the sooner deadline so the inner call cannot extend the budget.
                if 0 < previous_alarm < timeout_seconds:
                    signal.alarm(previous_alarm)
            except (ValueError, AttributeError):
                # Fallback to ThreadPoolExecutor when SIGALRM cannot be installed
                # (e.g. not the main thread, or on Windows).
                # The _Ops deadline stops pure-Python loops, but a blocked C call still
                # runs until it returns; host IPC kill remains the stop for a hung worker.
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(func, *args, **kwargs)
                try:
                    return future.result(timeout=timeout_seconds)
                except FuturesTimeoutError:
                    global abandoned_timeout
                    abandoned_timeout = True
                    raise ExecutionTimeoutError(
                        f"Code execution exceeded the maximum execution time of {timeout_seconds} seconds"
                    )
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)

            try:
                return func(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - start_time
                signal.signal(signal.SIGALRM, old_handler)
                if previous_alarm > 0:
                    # Tip: restore the outer alarm with its remaining time under the previous handler; never cancel it.
                    remaining = previous_alarm - elapsed
                    if remaining > 0:
                        signal.setitimer(signal.ITIMER_REAL, remaining)
                    else:
                        signal.setitimer(signal.ITIMER_REAL, 1e-6)
                else:
                    signal.alarm(0)

        return wrapper

    return decorator


def check_import_authorized(import_to_check: str, authorized_imports: list[str]) -> bool:
    if not import_to_check:
        return False
    # Check whether an import is authorized against the sandbox blocklist and extras.
    # Delegate directly to import_authorized.
    from plugin.scripting.sandbox import import_authorized

    return import_authorized(import_to_check, authorized_imports)


def _is_module_authorized(name: str, authorized_imports: list[str] | None = None) -> bool:
    if check_import_authorized(name, authorized_imports or []):
        return True
    alias = (
        "writeragent"
        if name == "plugin.scripting.writeragent_api"
        else ("writeragent" + name[len("plugin"):])
        if name.startswith("plugin.")
        else None
    )
    return alias is not None and check_import_authorized(alias, authorized_imports or [])


def evaluate_attribute(
    expression: ast.Attribute,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    value = evaluate_ast(expression.value, state, static_tools, custom_tools, authorized_imports)
    return safe_getattr(value, expression.attr)


def evaluate_unaryop(
    expression: ast.UnaryOp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    operand = evaluate_ast(expression.operand, state, static_tools, custom_tools, authorized_imports)
    if isinstance(expression.op, ast.USub):
        return -operand
    elif isinstance(expression.op, ast.UAdd):
        return operand
    elif isinstance(expression.op, ast.Not):
        return not operand
    elif isinstance(expression.op, ast.Invert):
        return ~operand
    else:
        raise InterpreterError(f"Unary operation {expression.op.__class__.__name__} is not supported.")


_NO_KW_DEFAULT = object()


def _build_signature(
    ast_args: ast.arguments,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> inspect.Signature:
    # Evaluate positional and keyword defaults at definition time in the defining scope.
    # Storing them on the signature prevents call-time re-evaluation and supports shared mutable defaults.
    evaluated_defaults = [
        evaluate_ast(d, state, static_tools, custom_tools, authorized_imports)
        for d in ast_args.defaults
    ]
    pos_names = [a.arg for a in ast_args.posonlyargs] + [a.arg for a in ast_args.args]
    defaults_map = dict(zip(pos_names[-len(evaluated_defaults):], evaluated_defaults))

    evaluated_kw_defaults = [
        evaluate_ast(d, state, static_tools, custom_tools, authorized_imports) if d is not None else _NO_KW_DEFAULT
        for d in ast_args.kw_defaults
    ]

    params = []
    for a in ast_args.posonlyargs:
        default = defaults_map.get(a.arg, inspect.Parameter.empty)
        params.append(inspect.Parameter(a.arg, inspect.Parameter.POSITIONAL_ONLY, default=default))
    for a in ast_args.args:
        default = defaults_map.get(a.arg, inspect.Parameter.empty)
        params.append(inspect.Parameter(a.arg, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default))
    if ast_args.vararg:
        params.append(inspect.Parameter(ast_args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for a, d in zip(ast_args.kwonlyargs, evaluated_kw_defaults):
        default = inspect.Parameter.empty if d is _NO_KW_DEFAULT else d
        params.append(inspect.Parameter(a.arg, inspect.Parameter.KEYWORD_ONLY, default=default))
    if ast_args.kwarg:
        params.append(inspect.Parameter(ast_args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))

    return inspect.Signature(params)


def evaluate_lambda(
    lambda_expression: ast.Lambda,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Callable:
    sig = _build_signature(
        lambda_expression.args, state, static_tools, custom_tools, authorized_imports
    )

    def lambda_func(*values: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*values, **kwargs)
        bound.apply_defaults()
        new_state = state.copy()
        new_state.update(bound.arguments)
        return evaluate_ast(
            lambda_expression.body,
            new_state,
            static_tools,
            custom_tools,
            authorized_imports,
        )

    lambda_func.__signature__ = sig
    return lambda_func



def evaluate_while(
    while_loop: ast.While,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    iterations = 0
    broke = False
    while evaluate_ast(while_loop.test, state, static_tools, custom_tools, authorized_imports):
        for node in while_loop.body:
            try:
                evaluate_ast(node, state, static_tools, custom_tools, authorized_imports)
            except BreakException:
                broke = True
                break
            except ContinueException:
                break
        if broke:
            break
        iterations += 1
        if iterations > MAX_WHILE_ITERATIONS:
            ops = state.get(_OPS_KEY)
            if ops is None or ops.deadline is None:
                raise InterpreterError(f"Maximum number of {MAX_WHILE_ITERATIONS} iterations in While loop exceeded")
    if not broke and while_loop.orelse:
        for node in while_loop.orelse:
            evaluate_ast(node, state, static_tools, custom_tools, authorized_imports)
    return None


def create_function(
    func_def: ast.FunctionDef,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
    class_cell: list[type | None] | None = None,
) -> Callable:
    if func_def.decorator_list:
        raise InterpreterError("Decorators are not supported in the sandbox")

    source_code = ast.unparse(func_def)
    sig = _build_signature(func_def.args, state, static_tools, custom_tools, authorized_imports)

    def new_func(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        func_state = state.copy()
        func_state.update(bound.arguments)
        if class_cell is not None:
            func_state[_CLASS_CELL_KEY] = class_cell

        # Update function state with self
        if func_def.args.args and func_def.args.args[0].arg == "self":
            if args:
                func_state["self"] = args[0]

        result = None
        try:
            for stmt in func_def.body:
                result = evaluate_ast(stmt, func_state, static_tools, custom_tools, authorized_imports)
        except ReturnException as e:
            if func_def.name == "__init__" and e.value is not None:
                raise TypeError(f"__init__() should return None, not '{type(e.value).__name__}'")
            result = e.value

        if func_def.name == "__init__":
            return None

        return result

    # Store original AST, source code, and name
    new_func.__ast__ = func_def
    new_func.__source__ = source_code
    new_func.__name__ = func_def.name
    new_func.__signature__ = sig

    return new_func


def evaluate_function_def(
    func_def: ast.FunctionDef,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Callable:
    # Function definitions store the function only in state, not custom_tools.
    # This prevents top-level or nested functions from leaking into shared tools.
    fn = create_function(func_def, state, static_tools, custom_tools, authorized_imports)
    state[func_def.name] = fn
    return fn



def evaluate_class_def(
    class_def: ast.ClassDef,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> type:
    if class_def.decorator_list:
        raise InterpreterError("Decorators are not supported in the sandbox")

    class_name = class_def.name
    bases = [evaluate_ast(base, state, static_tools, custom_tools, authorized_imports) for base in class_def.bases]

    # Determine the metaclass to use
    # If any base class has a custom metaclass, use it
    # Evaluate keywords (e.g. metaclass=)
    metaclass = None
    for kw in class_def.keywords:
        if kw.arg == "metaclass":
            metaclass = evaluate_ast(kw.value, state, static_tools, custom_tools, authorized_imports)
        else:
            raise InterpreterError(f"Unsupported class keyword argument '{kw.arg}'")

    if metaclass is None:
        metaclass = type
        for base in bases:
            base_metaclass = type(base)
            if base_metaclass is not type:
                metaclass = base_metaclass
                break

    # Use __prepare__ if the metaclass provides it (e.g., Enum uses _EnumDict)
    if hasattr(metaclass, "__prepare__"):
        class_dict = metaclass.__prepare__(class_name, bases)
    else:
        class_dict = {}

    class_cell: list[type | None] = [None]
    eval_state = state.copy()
    eval_state.update(class_dict)

    for stmt in class_def.body:
        if isinstance(stmt, ast.FunctionDef):
            fn = create_function(
                stmt, state, static_tools, custom_tools, authorized_imports, class_cell=class_cell
            )
            class_dict[stmt.name] = fn
            eval_state[stmt.name] = fn
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value:
                value = evaluate_ast(stmt.value, eval_state, static_tools, custom_tools, authorized_imports)
            target = stmt.target
            # Handle target types for annotation
            if isinstance(target, ast.Name):
                # Simple variable annotation like "x: int"
                annotation = evaluate_ast(stmt.annotation, eval_state, static_tools, custom_tools, authorized_imports)
                class_dict.setdefault("__annotations__", {})[target.id] = annotation
                # Assign value if provided
                if stmt.value:
                    class_dict[target.id] = value
                    eval_state[target.id] = value
            elif isinstance(target, ast.Attribute):
                # Attribute annotation like "obj.attr: int"
                obj = evaluate_ast(target.value, eval_state, static_tools, custom_tools, authorized_imports)
                # If there's a value assignment, set the attribute
                if stmt.value:
                    _guarded_setattr(obj, target.attr, value)
            elif isinstance(target, ast.Subscript):
                # Subscript annotation like "dict[key]: int"
                container = evaluate_ast(target.value, eval_state, static_tools, custom_tools, authorized_imports)
                index = evaluate_ast(target.slice, eval_state, static_tools, custom_tools, authorized_imports)
                # If there's a value assignment, set the item
                if stmt.value:
                    container[index] = value
            else:
                raise InterpreterError(f"Unsupported AnnAssign target in class body: {type(target).__name__}")
        elif isinstance(stmt, ast.Assign):
            value = evaluate_ast(stmt.value, eval_state, static_tools, custom_tools, authorized_imports)
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    class_dict[target.id] = value
                    eval_state[target.id] = value
                elif isinstance(target, ast.Attribute):
                    obj = evaluate_ast(target.value, eval_state, static_tools, custom_tools, authorized_imports)
                    _guarded_setattr(obj, target.attr, value)
                elif isinstance(target, ast.Subscript):
                    container = evaluate_ast(target.value, eval_state, static_tools, custom_tools, authorized_imports)
                    index = evaluate_ast(target.slice, eval_state, static_tools, custom_tools, authorized_imports)
                    container[index] = value
                else:
                    raise InterpreterError(f"Unsupported Assign target in class body: {type(target).__name__}")
        elif isinstance(stmt, ast.Pass):
            pass
        elif (
            isinstance(stmt, ast.Expr)
            and stmt == class_def.body[0]
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            # Check if it is a docstring: first statement in class body which is a string literal expression
            class_dict["__doc__"] = stmt.value.value
        else:
            raise InterpreterError(f"Unsupported statement in class body: {stmt.__class__.__name__}")

    new_class = metaclass(class_name, tuple(bases), class_dict)
    class_cell[0] = new_class
    state[class_name] = new_class
    return new_class


def evaluate_annassign(
    annassign: ast.AnnAssign,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    # If there's a value to assign, evaluate it
    if annassign.value:
        value = evaluate_ast(annassign.value, state, static_tools, custom_tools, authorized_imports)
        # Set the value for the target
        set_value(annassign.target, value, state, static_tools, custom_tools, authorized_imports)
        return value
    # For declarations without values (x: int), just return None
    return None


AUGASSIGN_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.iadd,
    ast.Sub: operator.isub,
    ast.Mult: operator.imul,
    ast.Div: operator.itruediv,
    ast.FloorDiv: operator.ifloordiv,
    ast.Mod: operator.imod,
    ast.Pow: operator.ipow,
    ast.MatMult: operator.imatmul,
    ast.LShift: operator.ilshift,
    ast.RShift: operator.irshift,
    ast.BitAnd: operator.iand,
    ast.BitOr: operator.ior,
    ast.BitXor: operator.ixor,
}


BINOP_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.MatMult: operator.matmul,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
}


def evaluate_augassign(
    expression: ast.AugAssign,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    target = expression.target
    obj = None
    key = None

    if isinstance(target, ast.Name):
        current_value = evaluate_name(target, state, static_tools, custom_tools, authorized_imports)
    elif isinstance(target, ast.Subscript):
        obj = evaluate_ast(target.value, state, static_tools, custom_tools, authorized_imports)
        key = evaluate_ast(target.slice, state, static_tools, custom_tools, authorized_imports)
        current_value = obj[key]
    elif isinstance(target, ast.Attribute):
        obj = evaluate_ast(target.value, state, static_tools, custom_tools, authorized_imports)
        current_value = nodunder_getattr(obj, target.attr)
    else:
        raise InterpreterError(f"Cannot assign to {type(target).__name__}")

    value_to_add = evaluate_ast(expression.value, state, static_tools, custom_tools, authorized_imports)

    op_func = AUGASSIGN_OPERATORS.get(type(expression.op))
    if op_func is None:
        raise InterpreterError(f"Operation {type(expression.op).__name__} is not supported.")
    current_value = op_func(current_value, value_to_add)

    # Update target in-place without re-evaluating target.value or target.slice
    if isinstance(target, ast.Name):
        if target.id in static_tools:
            raise InterpreterError(f"Cannot assign to name '{target.id}': doing this would erase the existing tool!")
        state[target.id] = current_value
    elif isinstance(target, ast.Subscript):
        obj[key] = current_value
    elif isinstance(target, ast.Attribute):
        _guarded_setattr(obj, target.attr, current_value)

    return current_value


def evaluate_boolop(
    node: ast.BoolOp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    # Determine which value should trigger short-circuit based on operation type:
    # - 'and' returns the first falsy value encountered (or the last value if all are truthy)
    # - 'or' returns the first truthy value encountered (or the last value if all are falsy)
    is_short_circuit_value = (lambda x: not x) if isinstance(node.op, ast.And) else (lambda x: bool(x))
    for value in node.values:
        result = evaluate_ast(value, state, static_tools, custom_tools, authorized_imports)
        # Short-circuit: return immediately if the condition is met
        if is_short_circuit_value(result):
            return result
    # If no short-circuit occurred, return the last evaluated value
    return result


def evaluate_binop(
    binop: ast.BinOp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    # Recursively evaluate the left and right operands
    left_val = evaluate_ast(binop.left, state, static_tools, custom_tools, authorized_imports)
    right_val = evaluate_ast(binop.right, state, static_tools, custom_tools, authorized_imports)

    op_func = BINOP_OPERATORS.get(type(binop.op))
    if op_func is None:
        raise InterpreterError(f"Binary operation {type(binop.op).__name__} is not supported.")
    return op_func(left_val, right_val)


def evaluate_assign(
    assign: ast.Assign,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    result = evaluate_ast(assign.value, state, static_tools, custom_tools, authorized_imports)
    for target in assign.targets:
        set_value(target, result, state, static_tools, custom_tools, authorized_imports)
    return result


def set_value(
    target: ast.AST,
    value: Any,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    if isinstance(target, ast.Name):
        if target.id in static_tools:
            raise InterpreterError(f"Cannot assign to name '{target.id}': doing this would erase the existing tool!")
        state[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)):
        if not hasattr(value, "__iter__"):
            raise InterpreterError("Cannot unpack non-iterable value")
        seq = list(value)
        elts = target.elts
        starred_indices = [i for i, elem in enumerate(elts) if isinstance(elem, ast.Starred)]
        if len(starred_indices) > 1:
            raise InterpreterError("multiple starred expressions in assignment")
        elif len(starred_indices) == 1:
            star_idx = starred_indices[0]
            before = star_idx
            after = len(elts) - 1 - star_idx
            if len(seq) < before + after:
                raise InterpreterError("Cannot unpack tuple of wrong size")
            for i in range(before):
                set_value(elts[i], seq[i], state, static_tools, custom_tools, authorized_imports)
            mid_len = len(seq) - after
            starred_val = seq[before:mid_len]
            set_value(elts[star_idx].value, starred_val, state, static_tools, custom_tools, authorized_imports)
            for i in range(after):
                set_value(elts[star_idx + 1 + i], seq[mid_len + i], state, static_tools, custom_tools, authorized_imports)
        else:
            if len(elts) != len(seq):
                raise InterpreterError("Cannot unpack tuple of wrong size")
            for elem, item in zip(elts, seq):
                set_value(elem, item, state, static_tools, custom_tools, authorized_imports)
    elif isinstance(target, ast.Subscript):
        obj = evaluate_ast(target.value, state, static_tools, custom_tools, authorized_imports)
        key = evaluate_ast(target.slice, state, static_tools, custom_tools, authorized_imports)
        obj[key] = value
    elif isinstance(target, ast.Attribute):
        obj = evaluate_ast(target.value, state, static_tools, custom_tools, authorized_imports)
        _guarded_setattr(obj, target.attr, value)
    else:
        raise InterpreterError(f"Cannot assign to {type(target).__name__}")


def _reject_numpy_code_exec(func: Any, args: list[Any], kwargs: dict[str, Any]) -> None:
    """Block pickle and ctypes escapes on the live NumPy module.

    NumPy is an allowed module, so ``_check_dangerous_value`` does not see
    ``np.load`` or ``numpy.ctypeslib.load_library``. Those denies run before
    ``func(*args, **kwargs)``. Numeric ``np.load`` / ``np.save`` run with
    ``allow_pickle=False`` when omitted; explicit ``True`` is rejected.
    """
    module = getattr(func, "__module__", "") or ""
    name = getattr(func, "__name__", "") or ""
    if not module.startswith("numpy"):
        return
    # NumPy is returned raw so dir() will not import f2py and SIGILL; this call
    # check is what blocks the compiler entry points.
    _NUMPY_CALL_DENIED = ("numpy.ctypeslib", "numpy.f2py", "numpy.distutils", "numpy.testing")
    if name == "load_library" or any(module.startswith(prefix) for prefix in _NUMPY_CALL_DENIED):
        raise InterpreterError(f"Forbidden call to {module}.{name}")
    if name in _NUMPY_PICKLE_RULES:
        new_kw = _apply_pickle_policy(name, args, kwargs)
        kwargs.update(new_kw)


def _reject_pandas_pickle(func: Any, args: list[Any], kwargs: dict[str, Any]) -> None:
    """Block pandas pickle the same way NumPy ``allow_pickle=True`` is blocked.

    ``read_pickle`` unpickles outside the AST walker and can import ``os``.
    """
    del args, kwargs
    module = getattr(func, "__module__", "") or ""
    name = getattr(func, "__name__", "") or ""
    if module.startswith("pandas") and name in ("read_pickle", "to_pickle"):
        raise InterpreterError(f"Forbidden call to {module}.{name}")


def evaluate_call(
    call: ast.Call,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    func, func_name = None, None

    if isinstance(call.func, ast.Name):
        func_name = call.func.id
        if func_name in state:
            func = state[func_name]
        elif func_name in static_tools:
            func = static_tools[func_name]
        elif func_name in custom_tools:
            func = custom_tools[func_name]
        elif func_name in ERRORS:
            func = ERRORS[func_name]
        elif func_name == "super":
            func = "super"
        else:
            raise InterpreterError(
                f"Forbidden function evaluation: '{call.func.id}' is not among the explicitly allowed tools or defined/imported in the preceding code"
            )
    elif isinstance(call.func, ast.Attribute):
        obj = evaluate_ast(call.func.value, state, static_tools, custom_tools, authorized_imports)
        func_name = call.func.attr
        try:
            func = safe_getattr(obj, func_name)
        except AttributeError:
            raise InterpreterError(f"Object {obj} has no attribute {func_name}")
    else:
        func = evaluate_ast(call.func, state, static_tools, custom_tools, authorized_imports)
        if not callable(func):
            raise InterpreterError(f"This is not a correct function: {call.func}).")

    args = []
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            args.extend(evaluate_ast(arg.value, state, static_tools, custom_tools, authorized_imports))
        else:
            args.append(evaluate_ast(arg, state, static_tools, custom_tools, authorized_imports))

    kwargs = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            # **kwargs unpacking
            starred_dict = evaluate_ast(keyword.value, state, static_tools, custom_tools, authorized_imports)
            if not isinstance(starred_dict, dict):
                raise InterpreterError(f"Cannot unpack non-dict value in **kwargs: {type(starred_dict).__name__}")
            kwargs.update(starred_dict)
        else:
            # Normal keyword argument
            kwargs[keyword.arg] = evaluate_ast(keyword.value, state, static_tools, custom_tools, authorized_imports)

    if func == "super":
        if not args:
            cell = state.get(_CLASS_CELL_KEY)
            if cell is not None and cell[0] is not None and "self" in state:
                return super(cell[0], state["self"])
            else:
                raise InterpreterError("super() needs at least one argument")
        cls = args[0]
        if not isinstance(cls, type):
            raise InterpreterError("super() argument 1 must be type")
        if len(args) == 1:
            return super(cls)
        elif len(args) == 2:
            instance = args[1]
            return super(cls, instance)
        else:
            raise InterpreterError("super() takes at most 2 arguments")
    else:  # Assume it's a callable object
        if func is operator.attrgetter or func is operator.methodcaller:
            raise InterpreterError(f"Forbidden access to operator dispatch type: {getattr(func, '__name__', '')}")
        if inspect.isbuiltin(func) and (getattr(func, "__module__", None) == "builtins") and (func not in static_tools.values()):
            raise InterpreterError(
                f"Invoking a builtin function that has not been explicitly added as a tool is not allowed ({func_name})."
            )
        if (
            hasattr(func, "__name__")
            and func.__name__.startswith("__")
            and func.__name__.endswith("__")
            and (func.__name__ not in static_tools)
            and (func.__name__ not in ALLOWED_DUNDER_METHODS)
        ):
            raise InterpreterError(f"Forbidden call to dunder function: {func.__name__}")
        _reject_numpy_code_exec(func, args, kwargs)
        _reject_pandas_pickle(func, args, kwargs)
        _check_dangerous_value(func, authorized_imports)
        return func(*args, **kwargs)


def _close_match_hint(needle: str, candidates: Any, *, label: str = "names") -> str:
    """Error-message hint only — never substitute or resolve via fuzzy match."""
    str_candidates = [c for c in candidates if isinstance(c, str)]
    close_matches = difflib.get_close_matches(needle, str_candidates)
    if not close_matches:
        return ""
    return f". Maybe you meant one of these {label} instead: {close_matches}"


def evaluate_subscript(
    subscript: ast.Subscript,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    # Evaluate container before index to match Python evaluation order.
    # Previously, indexing caught KeyError/IndexError/TypeError and wrapped
    # them in InterpreterError, preventing user try/except from catching them.
    value = evaluate_ast(subscript.value, state, static_tools, custom_tools, authorized_imports)
    index = evaluate_ast(subscript.slice, state, static_tools, custom_tools, authorized_imports)
    return value[index]



def evaluate_name(
    name: ast.Name,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    if name.id in state:
        return state[name.id]
    wrapped_tools = state.get(_WRAPPED_TOOLS_KEY)
    if wrapped_tools is not None and name.id in wrapped_tools:
        return wrapped_tools[name.id]
    elif name.id in static_tools:
        return safer_func(static_tools[name.id], authorized_imports=authorized_imports)
    elif name.id in custom_tools:
        return custom_tools[name.id]
    elif name.id in ERRORS:
        return ERRORS[name.id]
    hint = _close_match_hint(name.id, list(state.keys()), label="variables")
    # What was wrong: a missing name raised InterpreterError, a ValueError.
    # How: evaluate_try matches handlers with isinstance. The stateful benchmark
    # does `except NameError` around `counter += 1`. InterpreterError is not a
    # NameError, so the handler never ran, counter was never stored, and every
    # later cell on that shared kernel failed the same way. Subscript
    # KeyError/IndexError were unwrapped for this reason in 7560ccae9.
    # Why: CPython raises NameError, so the handler runs and the assignment
    # sticks. An uncaught NameError is still wrapped as InterpreterError by
    # evaluate_python_code, so a bare missing name stays an executor error.
    raise NameError(f"name '{name.id}' is not defined{hint}")


COMPARE_OPERATORS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}


def evaluate_condition(
    condition: ast.Compare,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> bool | object:
    result: bool | object = True
    left = evaluate_ast(condition.left, state, static_tools, custom_tools, authorized_imports)
    for op_node, comparator in zip(condition.ops, condition.comparators):
        op_type = type(op_node)
        op_func = COMPARE_OPERATORS.get(op_type)
        if op_func is None:
            raise InterpreterError(f"Unsupported comparison operator: {op_type.__name__}")
        right = evaluate_ast(comparator, state, static_tools, custom_tools, authorized_imports)
        result = op_func(left, right)
        if not result:
            return result
        left = right
    return result


def evaluate_if(
    if_statement: ast.If,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    result = None
    test_result = evaluate_ast(if_statement.test, state, static_tools, custom_tools, authorized_imports)
    if test_result:
        for line in if_statement.body:
            line_result = evaluate_ast(line, state, static_tools, custom_tools, authorized_imports)
            if line_result is not None:
                result = line_result
    else:
        for line in if_statement.orelse:
            line_result = evaluate_ast(line, state, static_tools, custom_tools, authorized_imports)
            if line_result is not None:
                result = line_result
    return result


def evaluate_for(
    for_loop: ast.For,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Any:
    result = None
    iterator = evaluate_ast(for_loop.iter, state, static_tools, custom_tools, authorized_imports)
    broke = False
    for counter in iterator:
        set_value(
            for_loop.target,
            counter,
            state,
            static_tools,
            custom_tools,
            authorized_imports,
        )
        for node in for_loop.body:
            try:
                line_result = evaluate_ast(node, state, static_tools, custom_tools, authorized_imports)
                if line_result is not None:
                    result = line_result
            except BreakException:
                broke = True
                break
            except ContinueException:
                break
        if broke:
            break
    if not broke and for_loop.orelse:
        for node in for_loop.orelse:
            line_result = evaluate_ast(node, state, static_tools, custom_tools, authorized_imports)
            if line_result is not None:
                result = line_result
    return result


def _evaluate_comprehensions(
    comprehensions: list[ast.comprehension],
    evaluate_element: Callable[[dict[str, Any]], Any],
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Generator[Any, None, None]:
    """
    Recursively evaluate nested comprehensions and yields elements.

    Args:
        comprehensions (`list[ast.comprehension]`): Comprehensions to evaluate.
        evaluate_element (`Callable`): Function that evaluates the final element when comprehensions are exhausted.
        state (`dict[str, Any]`): Current evaluation state.
        static_tools (`dict[str, Callable]`): Static tools.
        custom_tools (`dict[str, Callable]`): Custom tools.
        authorized_imports (`list[str]`): Authorized imports.

    Yields:
        `Any`: Individual elements produced by the comprehension
    """
    # Base case: no more comprehensions
    if not comprehensions:
        yield evaluate_element(state)
        return
    # Evaluate first comprehension
    comprehension = comprehensions[0]
    iter_value = evaluate_ast(comprehension.iter, state, static_tools, custom_tools, authorized_imports)
    for value in iter_value:
        new_state = state.copy()
        set_value(comprehension.target, value, new_state, static_tools, custom_tools, authorized_imports)
        # Check all filter conditions
        if all(
            evaluate_ast(if_clause, new_state, static_tools, custom_tools, authorized_imports)
            for if_clause in comprehension.ifs
        ):
            # Recurse with remaining comprehensions
            yield from _evaluate_comprehensions(
                comprehensions[1:], evaluate_element, new_state, static_tools, custom_tools, authorized_imports
            )


def evaluate_listcomp(
    listcomp: ast.ListComp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> list[Any]:
    return list(
        _evaluate_comprehensions(
            listcomp.generators,
            lambda comp_state: evaluate_ast(listcomp.elt, comp_state, static_tools, custom_tools, authorized_imports),
            state,
            static_tools,
            custom_tools,
            authorized_imports,
        )
    )


def evaluate_setcomp(
    setcomp: ast.SetComp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> set[Any]:
    return set(
        _evaluate_comprehensions(
            setcomp.generators,
            lambda comp_state: evaluate_ast(setcomp.elt, comp_state, static_tools, custom_tools, authorized_imports),
            state,
            static_tools,
            custom_tools,
            authorized_imports,
        )
    )


def evaluate_dictcomp(
    dictcomp: ast.DictComp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> dict[Any, Any]:
    return dict(
        _evaluate_comprehensions(
            dictcomp.generators,
            lambda comp_state: (
                evaluate_ast(dictcomp.key, comp_state, static_tools, custom_tools, authorized_imports),
                evaluate_ast(dictcomp.value, comp_state, static_tools, custom_tools, authorized_imports),
            ),
            state,
            static_tools,
            custom_tools,
            authorized_imports,
        )
    )


def evaluate_try(
    try_node: ast.Try,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    try:
        for stmt in try_node.body:
            evaluate_ast(stmt, state, static_tools, custom_tools, authorized_imports)
    except Exception as e:
        matched = False
        for handler in try_node.handlers:
            if handler.type is None or isinstance(
                e,
                evaluate_ast(handler.type, state, static_tools, custom_tools, authorized_imports),
            ):
                matched = True
                if handler.name:
                    state[handler.name] = e
                stack: list[Exception] = state.setdefault(_ACTIVE_EXCEPTIONS_KEY, [])
                stack.append(e)
                try:
                    for stmt in handler.body:
                        evaluate_ast(stmt, state, static_tools, custom_tools, authorized_imports)
                finally:
                    stack.pop()
                    if handler.name:
                        state.pop(handler.name, None)
                break
        if not matched:
            raise e
    else:
        if try_node.orelse:
            for stmt in try_node.orelse:
                evaluate_ast(stmt, state, static_tools, custom_tools, authorized_imports)
    finally:
        if try_node.finalbody:
            for stmt in try_node.finalbody:
                evaluate_ast(stmt, state, static_tools, custom_tools, authorized_imports)


def evaluate_raise(
    raise_node: ast.Raise,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    if raise_node.exc is not None:
        exc = evaluate_ast(raise_node.exc, state, static_tools, custom_tools, authorized_imports)
    else:
        stack = state.get(_ACTIVE_EXCEPTIONS_KEY, [])
        if not stack:
            raise InterpreterError("Re-raise is not supported without an active exception")
        exc = stack[-1]

    if raise_node.cause is not None:
        cause = evaluate_ast(raise_node.cause, state, static_tools, custom_tools, authorized_imports)
    else:
        cause = None
    if exc is not None:
        if cause is not None:
            raise exc from cause
        else:
            raise exc
    else:
        raise InterpreterError("Re-raise is not supported without an active exception")


def evaluate_assert(
    assert_node: ast.Assert,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    test_result = evaluate_ast(assert_node.test, state, static_tools, custom_tools, authorized_imports)
    if not test_result:
        if assert_node.msg:
            msg = evaluate_ast(assert_node.msg, state, static_tools, custom_tools, authorized_imports)
            raise AssertionError(msg)
        else:
            # Include the failing condition in the assertion message
            test_code = ast.unparse(assert_node.test)
            raise AssertionError(f"Assertion failed: {test_code}")


def _unwind_context_managers(contexts: list[Any], exc: Exception) -> None:
    # Tip: innermost first; a manager that suppresses resets exc_info so outer managers see no exception.
    exc_info = (type(exc), exc, exc.__traceback__)
    for context in reversed(contexts):
        try:
            if context.__exit__(*exc_info):
                exc_info = (None, None, None)
        except Exception as exit_exc:
            exc_info = (type(exit_exc), exit_exc, exit_exc.__traceback__)
    if exc_info[1] is not None:
        raise exc_info[1].with_traceback(exc_info[2])


def evaluate_with(
    with_node: ast.With,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    # Context managers enter one by one; if a later __enter__ raises, previous
    # managers must be exited in reverse. Previously, optional_vars used raw .id
    # (crashing on tuple/list unpacks) and suppressing managers swallowed returns.
    contexts = []
    try:
        for item in with_node.items:
            context_expr = evaluate_ast(item.context_expr, state, static_tools, custom_tools, authorized_imports)
            enter_result = context_expr.__enter__()
            contexts.append(context_expr)
            if item.optional_vars:
                set_value(item.optional_vars, enter_result, state, static_tools, custom_tools, authorized_imports)
    except (
        InterpreterSignal,
        FinalAnswerException,
        ExecutionTimeoutError,
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ):
        for context in reversed(contexts):
            try:
                context.__exit__(None, None, None)
            except Exception:
                continue
        raise
    except Exception as e:
        _unwind_context_managers(contexts, e)
        return

    try:
        for stmt in with_node.body:
            evaluate_ast(stmt, state, static_tools, custom_tools, authorized_imports)
    except (
        InterpreterSignal,
        FinalAnswerException,
        ExecutionTimeoutError,
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ):
        # A suppressing manager must not cancel control-flow signals, alarms, or exit interrupts.
        for context in reversed(contexts):
            try:
                context.__exit__(None, None, None)
            except Exception:
                continue
        raise
    except Exception as e:
        _unwind_context_managers(contexts, e)
    else:
        for context in reversed(contexts):
            context.__exit__(None, None, None)



_REAL_MODULES: dict[int, ModuleType] = {}


class _DelegatingSafeModule(ModuleType):
    """ModuleType wrapper exposing safe overrides while delegating attributes without scanning dir()."""

    _authorized_imports: list[str] | None

    def __init__(self, real_module: ModuleType, authorized_imports: list[str] | None = None):
        super().__init__(real_module.__name__, getattr(real_module, "__doc__", None))
        _REAL_MODULES[id(self)] = real_module
        self._authorized_imports = authorized_imports
        if real_module.__name__ == "operator":
            self.attrgetter = lambda *attrs: _safe_attrgetter(
                *attrs, authorized_imports=object.__getattribute__(self, "_authorized_imports")
            )
            self.methodcaller = _safe_methodcaller
        elif real_module.__name__ == "string":
            self.Formatter = _SafeFormatter

    def __dir__(self) -> list[str]:
        real = _REAL_MODULES.get(id(self))
        names = set(super().__dir__())
        names.discard("_authorized_imports")
        if real is not None:
            names.update(dir(real))
        return sorted(names)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        real = _REAL_MODULES.get(id(self))
        if real is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        # Tip: delegate __all__ so star-imports can discover exported names.
        if name == "__all__":
            return getattr(real, name)
        if name.startswith("_"):
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return getattr(real, name)


_FILE_OS_NAMES_SET: set[str] = set()


class _FileOsProxy(ModuleType):
    """Proxy os module exposing only allowlisted file operations."""

    def __init__(self, real_os_module: ModuleType) -> None:
        super().__init__("os", getattr(real_os_module, "__doc__", None))
        from plugin.scripting.sandbox import FILE_OS_NAMES

        for name in FILE_OS_NAMES:
            if hasattr(real_os_module, name):
                val = getattr(real_os_module, name)
                _FILE_OS_NAMES_SET.add(name)
                setattr(self, name, val)

    def __dir__(self) -> list[str]:
        return sorted(_FILE_OS_NAMES_SET)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            raise AttributeError(f"module 'os' has no attribute {name!r}")
        return super().__getattribute__(name)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"module 'os' has no attribute {name!r}")


_FILE_OS_PROXY: _FileOsProxy | None = None
_FILE_OS_READ_IDS: frozenset[int] | None = None
_FILE_OS_WRITE_IDS: frozenset[int] | None = None


def _get_default_file_os_ids() -> frozenset[int]:
    global _FILE_OS_READ_IDS
    if _FILE_OS_READ_IDS is None:
        get_file_os_proxy(files=False)
    assert _FILE_OS_READ_IDS is not None
    return _FILE_OS_READ_IDS


def get_file_os_proxy(files: bool = True) -> tuple[_FileOsProxy, frozenset[int]]:
    global _FILE_OS_PROXY, _FILE_OS_READ_IDS, _FILE_OS_WRITE_IDS
    if _FILE_OS_PROXY is None:
        import os as _real_os
        from plugin.scripting.sandbox import FILE_OS_READ_NAMES, FILE_OS_WRITE_NAMES

        proxy = _FileOsProxy(_real_os)
        read_ids: set[int] = set()
        for name in FILE_OS_READ_NAMES:
            val = getattr(proxy, name, None)
            if callable(val):
                read_ids.add(id(val))
        write_ids: set[int] = set()
        for name in FILE_OS_WRITE_NAMES:
            val = getattr(proxy, name, None)
            if callable(val):
                write_ids.add(id(val))
        _FILE_OS_PROXY = proxy
        _FILE_OS_READ_IDS = frozenset(read_ids)
        _FILE_OS_WRITE_IDS = frozenset(write_ids)
    assert _FILE_OS_PROXY is not None
    assert _FILE_OS_READ_IDS is not None
    assert _FILE_OS_WRITE_IDS is not None
    ids = (_FILE_OS_READ_IDS | _FILE_OS_WRITE_IDS) if files else _FILE_OS_READ_IDS
    return _FILE_OS_PROXY, ids


_SAFE_MODULE_CACHE: dict[int, _DelegatingSafeModule] = {}


def get_safe_module(raw_module: Any, authorized_imports: list[str] | None = None) -> Any:
    """Return raw module, safe delegating wrapper for operator/string, or empty module for dangerous modules."""
    if not isinstance(raw_module, ModuleType):
        return raw_module

    name = getattr(raw_module, "__name__", "") or ""
    root = name.split(".", 1)[0]
    relaxed_roots, file_os_ids = SANDBOX_POLICY_CONTEXT.get()

    if name == "os" and root not in relaxed_roots:
        proxy, _ = get_file_os_proxy(files=bool(file_os_ids and file_os_ids != _get_default_file_os_ids()))
        return proxy

    if root in relaxed_roots:
        return raw_module

    if not _is_module_authorized(name, authorized_imports):
        return ModuleType(name)

    if name in ("operator", "string"):
        # Tip: cache one wrapper per raw module to avoid unbounded growth across executor instances.
        cache_key = id(raw_module)
        wrapper = _SAFE_MODULE_CACHE.get(cache_key)
        if wrapper is None:
            wrapper = _DelegatingSafeModule(raw_module, authorized_imports=authorized_imports)
            _SAFE_MODULE_CACHE[cache_key] = wrapper
        else:
            wrapper._authorized_imports = authorized_imports
        return wrapper

    return raw_module


def evaluate_import(
    expression: ast.Import | ast.ImportFrom,
    state: dict[str, Any],
    authorized_imports: list[str],
    static_tools: dict[str, Callable] | None = None,
) -> None:
    import sys

    tools = static_tools if static_tools is not None else {}

    if isinstance(expression, ast.Import):
        for alias in expression.names:
            if not check_import_authorized(alias.name, authorized_imports):
                # Bugfix: keep import-failure messages short; dumping authorized_imports exposes duckdb.
                raise InterpreterError(f"Import of {alias.name} is not allowed.")
            raw_leaf = import_module(alias.name)
            if alias.asname:
                target_name = alias.asname
                if target_name in tools and target_name not in _IMPORT_OVERRIDABLE_TOOLS:
                    raise InterpreterError(
                        f"Cannot assign to name '{target_name}': doing this would erase the existing tool!"
                    )
                state[target_name] = get_safe_module(raw_leaf, authorized_imports)
            else:
                top_name = alias.name.split(".", 1)[0]
                if top_name in tools and top_name not in _IMPORT_OVERRIDABLE_TOOLS:
                    raise InterpreterError(
                        f"Cannot assign to name '{top_name}': doing this would erase the existing tool!"
                    )
                state[top_name] = get_safe_module(sys.modules[top_name], authorized_imports)
        return None
    elif isinstance(expression, ast.ImportFrom):
        if expression.level != 0 or expression.module is None:
            raise InterpreterError("Relative imports are not allowed.")
        if check_import_authorized(expression.module, authorized_imports):
            raw_module = __import__(expression.module, fromlist=[alias.name for alias in expression.names])
            module = get_safe_module(raw_module, authorized_imports)
            if expression.names[0].name == "*":  # Handle "from module import *"
                if hasattr(module, "__all__"):  # If module has __all__, import only those names
                    for name in module.__all__:
                        if is_forbidden_dunder_attribute(name):
                            raise InterpreterError(f"Forbidden access to dunder attribute: {name}")
                        if name in tools:
                            # Silently skip names in static_tools (overridable or not)
                            continue
                        val = safe_getattr(module, name)
                        if isinstance(val, ModuleType):
                            val = get_safe_module(val, authorized_imports)
                        _check_dangerous_value(val, authorized_imports)
                        state[name] = val
                else:  # If no __all__, import all public names (those not starting with '_')
                    for name in dir(module):
                        if not name.startswith("_"):
                            if name in tools:
                                # Silently skip names in static_tools (overridable or not)
                                continue
                            val = safe_getattr(module, name)
                            if isinstance(val, ModuleType):
                                val = get_safe_module(val, authorized_imports)
                            _check_dangerous_value(val, authorized_imports)
                            state[name] = val
            else:  # regular from imports
                sentinel = object()
                for alias in expression.names:
                    target_name = alias.asname or alias.name
                    if target_name in tools and target_name not in _IMPORT_OVERRIDABLE_TOOLS:
                        raise InterpreterError(
                            f"Cannot assign to name '{target_name}': doing this would erase the existing tool!"
                        )
                    val = nodunder_getattr(module, alias.name, sentinel)
                    if val is sentinel:
                        raise InterpreterError(f"Module {expression.module} has no attribute {alias.name}")
                    if isinstance(val, ModuleType):
                        val = get_safe_module(val, authorized_imports)
                    _check_dangerous_value(val, authorized_imports)
                    state[target_name] = val
        else:
            # Bugfix: keep import-failure messages short; dumping authorized_imports exposes duckdb.
            raise InterpreterError(f"Import from {expression.module} is not allowed.")
        return None


def evaluate_generatorexp(
    genexp: ast.GeneratorExp,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> Generator[Any]:
    # Nested ``for`` clauses must use _evaluate_comprehensions (same as list/set/dict comp).
    # The old loop over genexp.generators at the same level broke inner targets like ``v``.
    return _evaluate_comprehensions(
        genexp.generators,
        lambda comp_state: evaluate_ast(genexp.elt, comp_state, static_tools, custom_tools, authorized_imports),
        state,
        static_tools,
        custom_tools,
        authorized_imports,
    )


def evaluate_delete(
    delete_node: ast.Delete,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str],
) -> None:
    """
    Evaluate a delete statement (del x, del x[y]).

    Args:
        delete_node: The AST Delete node to evaluate
        state: The current state dictionary
        static_tools: Dictionary of static tools
        custom_tools: Dictionary of custom tools
        authorized_imports: List of authorized imports
    """
    for target in delete_node.targets:
        if isinstance(target, ast.Name):
            # Handle simple variable deletion (del x)
            if target.id in state:
                del state[target.id]
            else:
                raise InterpreterError(f"Cannot delete name '{target.id}': name is not defined")
        elif isinstance(target, ast.Subscript):
            # Handle index/key deletion (del x[y]); propagate KeyError/IndexError/TypeError directly
            obj = evaluate_ast(target.value, state, static_tools, custom_tools, authorized_imports)
            index = evaluate_ast(target.slice, state, static_tools, custom_tools, authorized_imports)
            del obj[index]
        elif isinstance(target, ast.Attribute):
            obj = evaluate_ast(target.value, state, static_tools, custom_tools, authorized_imports)
            _guarded_delattr(obj, target.attr)
        else:
            raise InterpreterError(f"Deletion of {type(target).__name__} targets is not supported")


def _eval_constant(expression, state, static_tools, custom_tools, authorized_imports):
    return expression.value


def _eval_tuple(expression, state, static_tools, custom_tools, authorized_imports):
    result_tuple = []
    common_params = (state, static_tools, custom_tools, authorized_imports)
    for elt in expression.elts:
        if isinstance(elt, ast.Starred):
            result_tuple.extend(evaluate_ast(elt.value, *common_params))
        else:
            result_tuple.append(evaluate_ast(elt, *common_params))
    return tuple(result_tuple)


def _eval_starred(expression, state, static_tools, custom_tools, authorized_imports):
    return evaluate_ast(expression.value, state, static_tools, custom_tools, authorized_imports)


def _eval_break(expression, state, static_tools, custom_tools, authorized_imports):
    raise BreakException()


def _eval_continue(expression, state, static_tools, custom_tools, authorized_imports):
    raise ContinueException()


def _eval_dict(expression, state, static_tools, custom_tools, authorized_imports):
    result_dict = {}
    common_params = (state, static_tools, custom_tools, authorized_imports)
    for k, v in zip(expression.keys, expression.values):
        if k is None:
            val = evaluate_ast(v, *common_params)
            if not isinstance(val, dict):
                raise InterpreterError(f"Cannot unpack non-dict value in dict literal: {type(val).__name__}")
            result_dict.update(val)
        else:
            key = evaluate_ast(k, *common_params)
            val = evaluate_ast(v, *common_params)
            result_dict[key] = val
    return result_dict


def _eval_expr(expression, state, static_tools, custom_tools, authorized_imports):
    return evaluate_ast(expression.value, state, static_tools, custom_tools, authorized_imports)


def _eval_formatted_value(expression, state, static_tools, custom_tools, authorized_imports):
    common_params = (state, static_tools, custom_tools, authorized_imports)
    value = evaluate_ast(expression.value, *common_params)
    if expression.conversion == 115:
        value = str(value)
    elif expression.conversion == 114:
        value = repr(value)
    elif expression.conversion == 97:
        value = ascii(value)
    format_spec = ""
    if expression.format_spec:
        format_spec = evaluate_ast(expression.format_spec, *common_params)
    return format(value, format_spec)


def _eval_joined_str(expression, state, static_tools, custom_tools, authorized_imports):
    common_params = (state, static_tools, custom_tools, authorized_imports)
    return "".join([str(evaluate_ast(v, *common_params)) for v in expression.values])


def _eval_list(expression, state, static_tools, custom_tools, authorized_imports):
    result_list = []
    common_params = (state, static_tools, custom_tools, authorized_imports)
    for elt in expression.elts:
        if isinstance(elt, ast.Starred):
            result_list.extend(evaluate_ast(elt.value, *common_params))
        else:
            result_list.append(evaluate_ast(elt, *common_params))
    return result_list


def _eval_if_exp(expression, state, static_tools, custom_tools, authorized_imports):
    common_params = (state, static_tools, custom_tools, authorized_imports)
    test_val = evaluate_ast(expression.test, *common_params)
    if test_val:
        return evaluate_ast(expression.body, *common_params)
    else:
        return evaluate_ast(expression.orelse, *common_params)


def _eval_slice(expression, state, static_tools, custom_tools, authorized_imports):
    common_params = (state, static_tools, custom_tools, authorized_imports)
    return slice(
        evaluate_ast(expression.lower, *common_params) if expression.lower is not None else None,
        evaluate_ast(expression.upper, *common_params) if expression.upper is not None else None,
        evaluate_ast(expression.step, *common_params) if expression.step is not None else None,
    )


def _eval_import(expression, state, static_tools, custom_tools, authorized_imports):
    return evaluate_import(expression, state, authorized_imports, static_tools=static_tools)


def _eval_set(expression, state, static_tools, custom_tools, authorized_imports):
    result_set = set()
    common_params = (state, static_tools, custom_tools, authorized_imports)
    for elt in expression.elts:
        if isinstance(elt, ast.Starred):
            result_set.update(evaluate_ast(elt.value, *common_params))
        else:
            result_set.add(evaluate_ast(elt, *common_params))
    return result_set


def _eval_return(expression, state, static_tools, custom_tools, authorized_imports):
    common_params = (state, static_tools, custom_tools, authorized_imports)
    raise ReturnException(evaluate_ast(expression.value, *common_params) if expression.value else None)


def _eval_pass(expression, state, static_tools, custom_tools, authorized_imports):
    return None


_HANDLERS: dict[type[ast.AST], Callable[..., Any]] = {
    ast.Assign: evaluate_assign,
    ast.AnnAssign: evaluate_annassign,
    ast.AugAssign: evaluate_augassign,
    ast.Call: evaluate_call,
    ast.Constant: _eval_constant,
    ast.Tuple: _eval_tuple,
    ast.GeneratorExp: evaluate_generatorexp,
    ast.ListComp: evaluate_listcomp,
    ast.DictComp: evaluate_dictcomp,
    ast.SetComp: evaluate_setcomp,
    ast.UnaryOp: evaluate_unaryop,
    ast.Starred: _eval_starred,
    ast.BoolOp: evaluate_boolop,
    ast.Break: _eval_break,
    ast.Continue: _eval_continue,
    ast.BinOp: evaluate_binop,
    ast.Compare: evaluate_condition,
    ast.Lambda: evaluate_lambda,
    ast.FunctionDef: evaluate_function_def,
    ast.Dict: _eval_dict,
    ast.Expr: _eval_expr,
    ast.For: evaluate_for,
    ast.FormattedValue: _eval_formatted_value,
    ast.If: evaluate_if,
    ast.JoinedStr: _eval_joined_str,
    ast.List: _eval_list,
    ast.Name: evaluate_name,
    ast.Subscript: evaluate_subscript,
    ast.IfExp: _eval_if_exp,
    ast.Attribute: evaluate_attribute,
    ast.Slice: _eval_slice,
    ast.While: evaluate_while,
    ast.Import: _eval_import,
    ast.ImportFrom: _eval_import,
    ast.ClassDef: evaluate_class_def,
    ast.Try: evaluate_try,
    ast.Raise: evaluate_raise,
    ast.Assert: evaluate_assert,
    ast.With: evaluate_with,
    ast.Set: _eval_set,
    ast.Return: _eval_return,
    ast.Pass: _eval_pass,
    ast.Delete: evaluate_delete,
}


@safer_eval
def evaluate_ast(
    expression: ast.AST,
    state: dict[str, Any],
    static_tools: dict[str, Callable],
    custom_tools: dict[str, Callable],
    authorized_imports: list[str] = BASE_BUILTIN_MODULES,
):
    """
    Evaluate an abstract syntax tree using the content of the variables stored in a state and only evaluating a given
    set of functions.

    This function will recurse through the nodes of the tree provided.

    Args:
        expression (`ast.AST`):
            The code to evaluate, as an abstract syntax tree.
        state (`Dict[str, Any]`):
            A dictionary mapping variable names to values. The `state` is updated if need be when the evaluation
            encounters assignments.
        static_tools (`Dict[str, Callable]`):
            Functions that may be called during the evaluation. Trying to change one of these static_tools will raise an error.
        custom_tools (`Dict[str, Callable]`):
            Functions that may be called during the evaluation. These custom_tools can be overwritten.
        authorized_imports (`List[str]`):
            The list of modules the code may import; default is the small safe set;
            a bare "*" entry is ignored; package wildcards use the "name.*" form.
    """
    ops = state.get(_OPS_KEY)
    if ops is None:
        ops = _Ops()
        state[_OPS_KEY] = ops
    if ops.count >= MAX_OPERATIONS:
        raise InterpreterError(
            f"Reached the max number of operations of {MAX_OPERATIONS}. Maybe there is an infinite loop somewhere in the code, or you're just asking too many calculations."
        )
    if (ops.count & 255) == 0 and ops.deadline is not None and time.monotonic() >= ops.deadline:
        sec = ops.timeout_seconds
        msg = (
            f"Code execution exceeded the maximum execution time of {sec} seconds"
            if sec is not None
            else "Code execution exceeded the maximum execution time"
        )
        raise ExecutionTimeoutError(msg)
    ops.count += 1
    handler = _HANDLERS.get(type(expression))
    if handler is None:
        raise InterpreterError(f"{expression.__class__.__name__} is not supported.")
    return handler(expression, state, static_tools, custom_tools, authorized_imports)


class FinalAnswerException(BaseException):
    """Exception raised when final_answer is called.

    Inherits from BaseException instead of Exception to prevent being caught
    by generic `except Exception` clauses in agent-generated code.
    """

    def __init__(self, value):
        self.value = value


def evaluate_python_code(
    code: str,
    static_tools: dict[str, Callable] | None = None,
    custom_tools: dict[str, Callable] | None = None,
    state: dict[str, Any] | None = None,
    authorized_imports: list[str] = BASE_BUILTIN_MODULES,
    max_print_outputs_length: int = DEFAULT_MAX_LEN_OUTPUT,
    timeout_seconds: int | None = MAX_EXECUTION_TIME_SECONDS,
):
    """
    Evaluate a python expression using the content of the variables stored in a state and only evaluating a given set
    of functions.

    This function will recurse through the nodes of the tree provided.

    Args:
        code (`str`):
            The code to evaluate.
        static_tools (`Dict[str, Callable]`):
            The functions that may be called during the evaluation. These can also be agents in a multiagent setting.
            These tools cannot be overwritten in the code: any assignment to their name will raise an error.
        custom_tools (`Dict[str, Callable]`):
            The functions that may be called during the evaluation.
            These tools can be overwritten in the code: any assignment to their name will overwrite them.
        state (`Dict[str, Any]`):
            A dictionary mapping variable names to values. The `state` should contain the initial inputs but will be
            updated by this function to contain all variables as they are evaluated.
            The print outputs are captured in state[_PRINT_OUTPUTS_KEY] (use print_outputs_from_state).
        timeout_seconds (`int`, *optional*, defaults to `MAX_EXECUTION_TIME_SECONDS`):
            Maximum time in seconds allowed for code execution. Set to `None` to disable timeout.
    """
    # WriterAgent (2026-05): hot cache for ast.parse + static sandbox policy on unchanged code.
    # Fresh executor state every call; does not cache execution results (see sandbox_cache.py).
    from plugin.scripting.sandbox_cache import get_hot_entry

    hot = get_hot_entry(code, authorized_imports)
    if hot.error is not None:
        raise InterpreterError(hot.error)
    if hot.module is None:
        raise InterpreterError("Code parsing failed.")
    expression = hot.module

    if state is None:
        state = {}
    static_tools = static_tools.copy() if static_tools is not None else {}
    custom_tools = custom_tools if custom_tools is not None else {}
    state[_OPS_KEY] = _Ops()
    print_container = PrintContainer(max_length=max_print_outputs_length)
    state[_PRINT_OUTPUTS_KEY] = print_container
    state[_ACTIVE_EXCEPTIONS_KEY] = []

    static_tools["print"] = _sandboxed_print

    if "final_answer" in static_tools:
        previous_final_answer = static_tools["final_answer"]

        def final_answer(*args, **kwargs):  # Allow arbitrary arguments to be passed
            raise FinalAnswerException(previous_final_answer(*args, **kwargs))

        static_tools["final_answer"] = final_answer

    state[_WRAPPED_TOOLS_KEY] = {
        k: safer_func(v, authorized_imports=authorized_imports)
        for k, v in static_tools.items()
    }

    # Define the actual execution logic
    def _execute_code():
        ops = state.get(_OPS_KEY)
        if ops is not None:
            ops.timeout_seconds = timeout_seconds
            ops.deadline = (time.monotonic() + timeout_seconds) if timeout_seconds is not None else None
        result = None
        stack = _get_print_stack()
        stack.append(print_container)
        try:
            try:
                for node in expression.body:
                    result = evaluate_ast(node, state, static_tools, custom_tools, authorized_imports)
                print_container.value = truncate_content(
                    str(print_container), max_length=max_print_outputs_length
                )
                is_final_answer = False
                return result, is_final_answer
            except FinalAnswerException as e:
                print_container.value = truncate_content(
                    str(print_container), max_length=max_print_outputs_length
                )
                is_final_answer = True
                return e.value, is_final_answer
            except InterpreterSignal as e:
                print_container.value = truncate_content(
                    str(print_container), max_length=max_print_outputs_length
                )
                if isinstance(e, ReturnException):
                    sig_name = "return"
                elif isinstance(e, BreakException):
                    sig_name = "break"
                elif isinstance(e, ContinueException):
                    sig_name = "continue"
                else:
                    sig_name = type(e).__name__.replace("Exception", "").lower()
                raise InterpreterError(f"'{sig_name}' outside function")
            except Exception as e:
                from plugin.framework.errors import is_disposed_exception

                # lp() used to catch DisposedException, wrap it as CALC_READ_ERROR,
                # and this handler then wrapped that as InterpreterError, so a closed
                # document became PYTHON_EXECUTION_ERROR. Re-raising here lets
                # execute_with_return see the disposal.
                if is_disposed_exception(e):
                    raise
                print_container.value = truncate_content(
                    str(print_container), max_length=max_print_outputs_length
                )
                # Bugfix: What was wrong: evaluate_name was changed to raise NameError so
                # scripts could catch missing names via `except NameError`. However, uncaught
                # NameErrors were formatted with `due to: NameError:`, which Calc add-in worker
                # exception classification mapped to #NAME? (525) instead of the historical
                # executor failure #VALUE! (519).
                # How it happened: evaluate_python_code interpolated type(e).__name__.
                # Why this change fixes it: formatting uncaught NameError with `due to: InterpreterError:`
                # preserves the pre-NameError worker failure formatting and Calc #VALUE! (519) error
                # while allowing `except NameError` inside user scripts.
                exc_type_name = "InterpreterError" if isinstance(e, NameError) else type(e).__name__
                raise InterpreterError(
                    f"Code execution failed at line '{ast.get_source_segment(code, node)}' due to: {exc_type_name}: {e}"
                )
        finally:
            if stack and stack[-1] is print_container:
                stack.pop()

    # Apply timeout if specified
    if timeout_seconds is not None:
        _execute_code = timeout(timeout_seconds)(_execute_code)

    return _execute_code()


@dataclass
class CodeOutput:
    output: Any
    logs: str
    is_final_answer: bool


class PythonExecutor(ABC):
    @abstractmethod
    def send_tools(self, tools: dict[str, Any]) -> None: ...

    @abstractmethod
    def send_variables(self, variables: dict[str, Any]) -> None: ...

    @abstractmethod
    def __call__(self, code_action: str) -> CodeOutput: ...


class LocalPythonExecutor(PythonExecutor):
    """
    Executor of Python code in a local environment.

    This executor evaluates Python code with restricted access to imports and built-in functions,
    making it suitable for running untrusted code. It maintains state between executions,
    allows for custom tools and functions to be made available to the code, and captures
    print outputs separately from return values.

    Args:
        additional_authorized_imports (`list[str]`):
            Additional authorized imports for the executor.
        max_print_outputs_length (`int`, defaults to `DEFAULT_MAX_LEN_OUTPUT=50_000`):
            Maximum length of the print outputs.
        additional_functions (`dict[str, Callable]`, *optional*):
            Additional Python functions to be added to the executor.
        timeout_seconds (`int`, *optional*, defaults to `MAX_EXECUTION_TIME_SECONDS`):
            Maximum time in seconds allowed for code execution. Set to `None` to disable timeout.
    """

    def __init__(
        self,
        additional_authorized_imports: list[str],
        max_print_outputs_length: int | None = None,
        additional_functions: dict[str, Callable] | None = None,
        timeout_seconds: int | None = MAX_EXECUTION_TIME_SECONDS,
    ):
        self.custom_tools = {}
        self.state = {"__name__": "__main__"}
        self.logs: str = ""
        self.max_print_outputs_length: int = (
            DEFAULT_MAX_LEN_OUTPUT if max_print_outputs_length is None else max_print_outputs_length
        )
        self.additional_authorized_imports = additional_authorized_imports
        self.authorized_imports = list(set(BASE_BUILTIN_MODULES) | set(self.additional_authorized_imports))
        # WriterAgent vendored change (2026-05): upstream HuggingFace smolagents calls
        # _check_authorized_imports_are_installed() here, which runs find_spec() on every
        # authorized top-level module at executor construction ("Non-installed authorized
        # modules: …"). We disabled that pre-flight probe. Import policy is default-allow plus
        # extras that override a short blocklist: evaluate_import → check_import_authorized →
        # import_module at the line that imports. If an authorized package is missing, the user
        # gets a normal ModuleNotFoundError wrapped as InterpreterError when code actually imports it.
        self.additional_functions = additional_functions or {}
        self.static_tools = {**BASE_PYTHON_TOOLS.copy(), **self.additional_functions}
        self.timeout_seconds = timeout_seconds
        self._sandbox_policy_digest: str = ""

    def __call__(self, code_action: str) -> CodeOutput:
        output, is_final_answer = evaluate_python_code(
            code_action,
            static_tools=self.static_tools,
            custom_tools=self.custom_tools,
            state=self.state,
            authorized_imports=self.authorized_imports,
            max_print_outputs_length=self.max_print_outputs_length,
            timeout_seconds=self.timeout_seconds,
        )
        logs = print_outputs_from_state(self.state)
        self.logs = logs
        return CodeOutput(output=output, logs=logs, is_final_answer=is_final_answer)

    def send_variables(self, variables: dict[str, Any]):
        self.state.update(variables)

    def send_tools(self, tools: dict[str, Any]):
        # Combine agent tools, base Python tools, and additional Python functions
        self.static_tools = {**tools, **BASE_PYTHON_TOOLS.copy(), **self.additional_functions}


__all__ = ["evaluate_python_code", "LocalPythonExecutor", "print_outputs_from_state"]
