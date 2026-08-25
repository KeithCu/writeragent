# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side venv sandbox boundary: import whitelist, subprocess spawn env, interpreter resolution."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import subprocess

from plugin.framework.deal_shim import DEAL_MAX_ARGV, DEAL_MAX_CMD_ARGS, DEAL_MAX_PATH, DEAL_MAX_TOKEN, deal, str_bounded

# --- Import whitelist (shared by venv_sandbox and import_policy) ---

# Dynamically sync/mirror allowed and dangerous modules from smolagents to avoid silent drift.
try:
    from plugin.contrib.smolagents.utils import BASE_BUILTIN_MODULES as _BASE_BUILTIN
    BASE_BUILTIN_MODULES: tuple[str, ...] = tuple(_BASE_BUILTIN)
except ImportError:
    BASE_BUILTIN_MODULES = (
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
    )

try:
    from plugin.contrib.smolagents.local_python_executor import DANGEROUS_MODULES as _DANGEROUS
    DANGEROUS_MODULES: tuple[str, ...] = tuple(_DANGEROUS)
except ImportError:
    DANGEROUS_MODULES = (
        "builtins",
        "io",
        "multiprocessing",
        "os",
        "pathlib",
        "pty",
        "shutil",
        "socket",
        "subprocess",
        "sys",
    )

# Curated by WriterAgent (see docs/enabling_numpy_in_libreoffice.md)—not "whatever is in the venv".
VENV_AUTHORIZED_IMPORTS: tuple[str, ...] = (
    "platform",
    "numpy",
    "numpy.*",
    "pandas",
    "pandas.*",
    "scipy",
    "scipy.*",
    "sklearn",
    "sklearn.*",
    "matplotlib",
    "matplotlib.*",
    "seaborn",
    "seaborn.*",
    "sympy",
    "sympy.*",
    "statsmodels",
    "statsmodels.*",
    "networkx",
    "networkx.*",
    "PIL",
    "PIL.*",
    "data_profiling",
    "data_profiling.*",
    "pandas_montecarlo",
    "pandas_montecarlo.*",
    "cv2",
    "json",
    "csv",
    "decimal",
    "fractions",
    "functools",
    "operator",
    "string",
    "textwrap",
    "enum",
    "dataclasses",
    "typing",
    "copy",
    "pprint",
    "webview",
    "rocher",
    "jedi",
    "PyQt6",
    "PyQt6.QtWebEngineWidgets",
    "qtpy",
    "writeragent",
    "writeragent.*",
    "plugin.scripting.writeragent_api",
    "plugin.scripting.writeragent_api.*",
    "plugin.scripting.payload_codec",
    "plugin.embeddings.venv.embeddings_index",
    "plugin.embeddings.venv.embeddings_sqlite",
    "plugin.embeddings.venv.embeddings_llama_index",
    "plugin.embeddings.venv.embeddings_ingest_graph",
    "plugin.embeddings.venv.embeddings_search_graph",
    "plugin.embeddings.venv.embeddings_zvec",
    "plugin.embeddings.venv.embeddings_hybrid_search",
    "plugin.scripting.analysis",
    "plugin.vision",
    "plugin.vision.venv.vision",
    "plugin.vision.vision_common",
    "plugin.vision.venv.vision_docling",
    "plugin.vision.venv.vision_paddle",
    "plugin.vision.venv.vision_html_export",
    "css_inline",
    "plugin.scripting.viz",
    "plugin.scripting.symbolic",
    "plugin.scripting.units",
    "plugin.scripting.text_analytics",  # trusted text analytics (spaCy) for Run Python Script + direct imports in user scripts
    "spacy",
    "spacy.*",
    "textdescriptives",
    "spacytextblob",
    "spacytextblob.*",
    "pint",
    "pint.*",
    "duckdb",
    "duckdb.*",
    "sentence_transformers",
    "sentence_transformers.*",
    "transformers",
    "transformers.*",
    "yfinance",
    "yfinance.*",
    "pandas_ta",
    "pandas_ta.*",
    "quantstats",
    "quantstats.*",
    "pypfopt",
    "pypfopt.*",
    "plugin.scripting.quant",
    "plugin.scripting.optimize",
    "plugin.scripting.forecast",
    "plugin.scripting.calc_functions",
    "plugin.scripting.calc_functions.*",
    "plugin.writer.locale.vale",
    "plugin.writer.locale.languagetool",
)


# In-process LO embedded sandbox (execute_python_script) — stdlib-only extras beyond BASE_BUILTIN_MODULES.
CALC_AUTHORIZED_IMPORTS: tuple[str, ...] = (
    "math",
    "datetime",
    "random",
    "json",
    "re",
    "collections",
    "itertools",
    "statistics",
)

# --- Subprocess environment ---

_BLOCKED_ENV_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
# LibreOffice sets PYTHONHOME/PYTHONPATH to its bundled stdlib; letting these
# leak into a venv subprocess causes SRE module mismatch and import failures.
_BLOCKED_ENV_EXACT = {"PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH"}

_NOT_SET = "__not_set__"
_cached_sandbox: str | None = _NOT_SET  # type: ignore[assignment]  # sentinel

_PIPE_BUF_TARGET = 1024 * 1024


@deal.pre(
    lambda base: base is None
    or (
        isinstance(base, dict)
        and len(base) <= DEAL_MAX_ARGV
        and all(
            isinstance(k, str)
            and str_bounded(k, DEAL_MAX_TOKEN)
            and isinstance(v, str)
            and str_bounded(v, DEAL_MAX_ARGV)
            for k, v in base.items()
        )
    )
)
@deal.post(lambda result: isinstance(result, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in result.items()))
@deal.ensure(lambda base, result: all(k.upper() not in _BLOCKED_ENV_EXACT for k in result))
@deal.ensure(lambda base, result: all(not any(s in k.upper() for s in _BLOCKED_ENV_SUBSTR) for k in result))
@deal.ensure(
    lambda base, result: (not base)
    or (
        result.get("PYTHONIOENCODING") == "utf-8"
        and result.get("PYTHONUTF8") == "1"
        and result.get("PYTHONDONTWRITEBYTECODE") == "1"
    )
)
def scrub_subprocess_env(base: dict[str, str] | None) -> dict[str, str]:
    """Drop likely-secret vars and LO Python overrides from the environment passed to venv Python."""
    if not base:
        return {}
    out: dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if ku in _BLOCKED_ENV_EXACT:
            continue
        if any(s in ku for s in _BLOCKED_ENV_SUBSTR):
            continue
        out[k] = v
    out.setdefault("PYTHONIOENCODING", "utf-8")
    out.setdefault("PYTHONUTF8", "1")
    out.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        from plugin.framework.logging import _debug_log_path
        if _debug_log_path:
            out["WRITERAGENT_DEBUG_LOG_PATH"] = _debug_log_path
    except Exception:
        pass
    return out


def detect_sandbox() -> str | None:
    """Return ``'flatpak'``, ``'snap'``, or ``None``.

    The result is cached because sandbox status cannot change at runtime.
    """
    # crosshair: off
    global _cached_sandbox
    if _cached_sandbox is not _NOT_SET:
        return _cached_sandbox

    if os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID"):
        _cached_sandbox = "flatpak"
    elif os.environ.get("SNAP_NAME"):
        _cached_sandbox = "snap"
    else:
        _cached_sandbox = None
    return _cached_sandbox


def optimize_pipe(pipe_fd: int) -> None:
    """Raise venv-worker pipe capacity toward 1 MiB on Linux (default ~64 KiB).

    Large pickle IPC (split-grid / NumPy) can exceed the default pipe buffer;
    F_SETPIPE_SZ requests a larger kernel ring buffer so host and child block less.
    No-op on macOS/Windows (no supported API). Silently no-ops when caps deny resize.
    """
    # crosshair: off
    if sys.platform != "linux":
        return
    import fcntl

    try:
        fcntl.fcntl(pipe_fd, fcntl.F_SETPIPE_SZ, _PIPE_BUF_TARGET)
    except OSError:
        pass


def optimize_popen_pipes(proc: subprocess.Popen[Any]) -> None:
    """Apply :func:`optimize_pipe` to stdin/stdout/stderr of a piped child process."""
    # crosshair: off
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            optimize_pipe(stream.fileno())
        except (OSError, ValueError):
            pass


# Hypothesis and venv-path tests pass Unicode argv/paths; ascii_bounded would reject them.
# Argv uses DEAL_MAX_ARGV (venv -c probes are longer than DEAL_MAX_PATH filesystem caps).
@deal.pre(
    lambda cmd: isinstance(cmd, list)
    and len(cmd) <= DEAL_MAX_CMD_ARGS
    and all(str_bounded(x, DEAL_MAX_ARGV) for x in cmd)
)
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))
@deal.ensure(lambda cmd, result: len(result) >= len(cmd) and (result[-len(cmd):] == cmd if cmd else True))
def wrap_command_for_sandbox(cmd: list[str]) -> list[str]:
    """Prepend ``flatpak-spawn --host`` when running inside a Flatpak sandbox.

    Snap confinement with ``classic``/``home`` plugs typically allows direct
    subprocess access, so Snap commands are returned unchanged.
    """
    sandbox = detect_sandbox()
    if sandbox == "flatpak":
        return ["flatpak-spawn", "--host"] + cmd
    return cmd


def _reset_cache() -> None:  # pyright: ignore[reportUnusedFunction]  # test helper to clear sandbox path cache
    """Reset the cached detection result (for tests only)."""
    global _cached_sandbox
    _cached_sandbox = _NOT_SET  # type: ignore[assignment]


# --- Interpreter resolution ---


def _strip_surrounding_quotes(path: str) -> str:
    """Strip one layer of matching quotes (Windows Explorer \"Copy as path\")."""
    s = path.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1].strip()
    return s


def _path_from_file_url(raw: str) -> str | None:
    """Convert a ``file://`` / ``file:/`` URL to a filesystem path (stdlib only)."""
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    text = raw.strip()
    if text.startswith("file:/") and not text.startswith("file://"):
        text = "file://" + text[len("file:") :]
    if not text.startswith("file://"):
        return None
    parsed = urlparse(text)
    if parsed.scheme != "file":
        return None
    # url2pathname expects the path component; unquote percent-encoding (e.g. José).
    path = url2pathname(unquote(parsed.path))
    # Windows: file:///C:/Users/... → /C:/Users/... before url2pathname; after, C:\Users\...
    if parsed.netloc and os.name == "nt" and not path.startswith("\\\\"):
        # UNC: file://server/share → netloc=server, path=/share
        path = f"\\\\{parsed.netloc}{path}"
    return path or None


def _normalize_venv_path_input(venv_dir: str) -> str:
    """Strip quotes, convert file URLs, then expand ``~`` and env vars."""
    cleaned = _strip_surrounding_quotes(venv_dir.strip())
    if cleaned.lower().startswith("file:"):
        from_url = _path_from_file_url(cleaned)
        if from_url:
            cleaned = from_url
    return os.path.expanduser(os.path.expandvars(cleaned))


def _is_acceptable_python_basename(base: str) -> bool:
    """True for python / python3 / python.exe; false for pythonw (no console I/O)."""
    lower = base.lower()
    if lower in ("pythonw", "pythonw.exe"):
        return False
    return lower.startswith("python")


def _is_usable_python_file(path: str) -> bool:
    """True when *path* is a usable console Python interpreter file."""
    if not os.path.isfile(path):
        return False
    if not _is_acceptable_python_basename(os.path.basename(path)):
        return False
    # Windows ignores the Unix execute bit; require isfile only there.
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def resolve_libreoffice_python() -> Optional[str]:
    """Return ``sys.executable`` if it names a real file (no other heuristics).

    Under PyUNO this is normally the office-bundled Python; on broken installs it
    may be wrong or missing — callers surface an error and the user can set a venv.
    """
    # crosshair: off
    exe = (getattr(sys, "executable", None) or "").strip()
    if not exe or not os.path.isfile(exe):
        return None
    if os.name != "nt" and not os.access(exe, os.X_OK):
        return None
    # Reject LibreOffice binaries (soffice, libreoffice, oosplash) that are not Python
    basename = os.path.basename(exe).lower()
    if not basename.startswith("python"):
        return None
    return exe


def _python_candidates_in_bin_dir(bin_dir: str) -> list[str]:
    """Return candidate interpreter paths under a venv ``bin/`` or ``Scripts/`` directory."""
    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend(
            [
                os.path.join(bin_dir, "python.exe"),
                os.path.join(bin_dir, "python"),
                os.path.join(bin_dir, "python3"),
            ]
        )
    else:
        for name in ("python", "python3"):
            candidates.append(os.path.join(bin_dir, name))
    if os.path.isdir(bin_dir):
        for entry in sorted(os.listdir(bin_dir)):
            if entry.startswith("python3."):
                candidates.append(os.path.join(bin_dir, entry))
    return candidates


def _python_candidates_at_env_root(env_dir: str) -> list[str]:
    """Return interpreter candidates at the env root (conda / pyenv-win layout)."""
    if os.name == "nt":
        return [
            os.path.join(env_dir, "python.exe"),
            os.path.join(env_dir, "python"),
            os.path.join(env_dir, "python3"),
        ]
    return [
        os.path.join(env_dir, "python"),
        os.path.join(env_dir, "python3"),
    ]


def _first_executable_python(candidates: list[str]) -> str | None:
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_usable_python_file(candidate):
            return candidate
    return None


def resolve_venv_python(venv_dir: str) -> Optional[str]:
    """Return the python executable for *venv_dir*.

    Accepts a venv root (``…/myvenv``), ``bin/`` / ``Scripts/`` directory, or a direct
    path to ``python`` / ``python3`` / ``python.exe``. Also accepts conda/pyenv-win
    layouts with ``python.exe`` at the env root. Strips surrounding quotes and
    converts ``file://`` URLs from pasted paths.
    """
    # crosshair: off
    if not venv_dir or not venv_dir.strip():
        return None
    expanded = _normalize_venv_path_input(venv_dir)

    if os.path.isfile(expanded):
        if _is_usable_python_file(expanded):
            return expanded
        return None

    if not os.path.isdir(expanded):
        return None

    dir_name = os.path.basename(os.path.normpath(expanded))
    if dir_name in ("bin", "Scripts"):
        return _first_executable_python(_python_candidates_in_bin_dir(expanded))

    if os.name == "nt":
        bin_candidates = [os.path.join(expanded, "Scripts"), os.path.join(expanded, "bin")]
    else:
        bin_candidates = [os.path.join(expanded, "bin"), os.path.join(expanded, "Scripts")]
    candidates: list[str] = []
    for bin_dir in bin_candidates:
        if os.path.isdir(bin_dir):
            candidates.extend(_python_candidates_in_bin_dir(bin_dir))
    # Prefer bin/Scripts; fall back to env-root python.exe (conda / pyenv-win).
    candidates.extend(_python_candidates_at_env_root(expanded))
    return _first_executable_python(candidates)


@deal.pre(lambda target_path, root_dir: str_bounded(target_path, DEAL_MAX_PATH) and str_bounded(root_dir, DEAL_MAX_PATH))
@deal.post(lambda result: isinstance(result, bool))
@deal.ensure(
    lambda target_path, root_dir, result: (
        not result
        or os.path.commonpath(
            [os.path.abspath(os.path.join(os.path.abspath(root_dir), target_path)), os.path.abspath(root_dir)]
        )
        == os.path.abspath(root_dir)
    )
)
def is_safe_workspace_path(target_path: str, root_dir: str) -> bool:
    """Return True if *target_path* resolves strictly inside *root_dir* (prevents path traversal)."""
    if not target_path or not root_dir:
        return False
    try:
        abs_root = os.path.abspath(root_dir)
        abs_target = os.path.abspath(os.path.join(abs_root, target_path))
        return os.path.commonpath([abs_target, abs_root]) == abs_root
    except Exception:
        return False

