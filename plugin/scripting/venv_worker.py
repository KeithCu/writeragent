# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side venv worker: path resolution, warm subprocess, and run_code_in_user_venv."""

from __future__ import annotations

import json
import logging
import os
import pickle
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, IO, Optional, Tuple

from plugin.framework.config import get_config_str
from plugin.framework.i18n import _
from plugin.framework.constants import WORKER_POOL_DEFAULT
from plugin.scripting.config_limits import (
    VISION_PROBE_TIMEOUT_SEC,
    WARM_WORKER_TIMEOUT_SEC,
    configured_python_exec_timeout,
    python_exec_timeout_default,
    resolve_python_exec_timeout,
)
from plugin.scripting.payload_codec import host_unpack_data

_BLOCKED_ENV_SUBSTR = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
# LibreOffice sets PYTHONHOME/PYTHONPATH to its bundled stdlib; letting these
# leak into a venv subprocess causes SRE module mismatch and import failures.
_BLOCKED_ENV_EXACT = {"PYTHONHOME", "PYTHONPATH"}

_NOT_SET = "__not_set__"
_cached_sandbox: str | None = _NOT_SET  # type: ignore[assignment]  # sentinel


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
    return out


def detect_sandbox() -> str | None:
    """Return ``'flatpak'``, ``'snap'``, or ``None``.

    The result is cached because sandbox status cannot change at runtime.
    """
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


_PIPE_BUF_TARGET = 1024 * 1024


def optimize_pipe(pipe_fd: int) -> None:
    """Raise venv-worker pipe capacity toward 1 MiB on Linux (default ~64 KiB).

    Large pickle IPC (split-grid / NumPy) can exceed the default pipe buffer;
    F_SETPIPE_SZ requests a larger kernel ring buffer so host and child block less.
    No-op on macOS/Windows (no supported API). Silently no-ops when caps deny resize.
    """
    if sys.platform != "linux":
        return
    import fcntl

    try:
        fcntl.fcntl(pipe_fd, fcntl.F_SETPIPE_SZ, _PIPE_BUF_TARGET)
    except OSError:
        pass


def optimize_popen_pipes(proc: subprocess.Popen[Any]) -> None:
    """Apply :func:`optimize_pipe` to stdin/stdout/stderr of a piped child process."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            optimize_pipe(stream.fileno())
        except (OSError, ValueError):
            pass


def wrap_command_for_sandbox(cmd: list[str]) -> list[str]:
    """Prepend ``flatpak-spawn --host`` when running inside a Flatpak sandbox.

    Snap confinement with ``classic``/``home`` plugs typically allows direct
    subprocess access, so Snap commands are returned unchanged.
    """
    sandbox = detect_sandbox()
    if sandbox == "flatpak":
        return ["flatpak-spawn", "--host"] + cmd
    return cmd


def _reset_cache() -> None:
    """Reset the cached detection result (for tests only)."""
    global _cached_sandbox
    _cached_sandbox = _NOT_SET  # type: ignore[assignment]


log = logging.getLogger(__name__)

_TIMEOUT_AFTER = " timed out after "


def _worker_error_message(exc: BaseException) -> str:
    """Build a short user-facing worker error without subprocess command paths."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Python worker failed: timed out after {exc.timeout} seconds"
    text = str(exc)
    if text.startswith("Command ") and _TIMEOUT_AFTER in text:
        return f"Python worker failed:{text[text.index(_TIMEOUT_AFTER):]}"
    return f"Python worker failed: {text}"


_HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_harness.py")
_instances: dict[str, PythonWorkerManager] = {}
_registry_lock = threading.Lock()


# --- Path resolution ---


def resolve_libreoffice_python() -> Optional[str]:
    """Return ``sys.executable`` if it names a real file (no other heuristics).

    Under PyUNO this is normally the office-bundled Python; on broken installs it
    may be wrong or missing — callers surface an error and the user can set a venv.
    """
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
        candidates.append(os.path.join(bin_dir, "python.exe"))
    else:
        for name in ("python", "python3"):
            candidates.append(os.path.join(bin_dir, name))
        if os.path.isdir(bin_dir):
            for entry in sorted(os.listdir(bin_dir)):
                if entry.startswith("python3."):
                    candidates.append(os.path.join(bin_dir, entry))
    return candidates


def _first_executable_python(candidates: list[str]) -> str | None:
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_venv_python(venv_dir: str) -> Optional[str]:
    """Return the python executable for *venv_dir*.

    Accepts a venv root (``…/myvenv``), ``bin/`` / ``Scripts/`` directory, or a direct
    path to ``python`` / ``python3`` / ``python.exe``.
    """
    if not venv_dir or not venv_dir.strip():
        return None
    expanded = os.path.expanduser(os.path.expandvars(venv_dir.strip()))

    if os.path.isfile(expanded):
        base = os.path.basename(expanded)
        if base.startswith("python") or base == "python.exe":
            if os.access(expanded, os.X_OK):
                return expanded
        return None

    if not os.path.isdir(expanded):
        return None

    dir_name = os.path.basename(os.path.normpath(expanded))
    if dir_name in ("bin", "Scripts"):
        return _first_executable_python(_python_candidates_in_bin_dir(expanded))

    if os.name == "nt":
        bin_candidates = [os.path.join(expanded, "Scripts")]
    else:
        bin_candidates = [os.path.join(expanded, "bin")]
    candidates: list[str] = []
    for bin_dir in bin_candidates:
        if os.path.isdir(bin_dir):
            candidates.extend(_python_candidates_in_bin_dir(bin_dir))
    return _first_executable_python(candidates)


# NOTE for AI agents: The diagnostic script below runs in a sandboxed LocalPythonExecutor.
# Do NOT use dynamic execution primitives like __import__(), eval(), or exec(), as they are
# forbidden by the sandbox and will cause an InterpreterError. Use explicit try/except import blocks.
_DIAGNOSTIC_SCRIPT = """
import platform
res = {'v': platform.python_version(), 'arch': platform.machine(), 'p': {}}
sci = ['numpy', 'pandas', 'scipy', 'sklearn', 'matplotlib', 'sympy']
eda = ['data_profiling', 'statsmodels', 'pandas_montecarlo']
cas = ['sympy']
viz = ['matplotlib', 'seaborn']
ui = ['webview', 'jedi', 'PyQt6', 'PyQt6.QtWebEngineWidgets', 'qtpy']
quant = ['yfinance', 'pandas_ta', 'quantstats', 'pypfopt']
data_eng = ['pint']
res['sci'] = sci
res['eda'] = eda
res['cas'] = cas
res['viz'] = viz
res['ui'] = ui
res['quant'] = quant
res['data_eng'] = data_eng

# Check for Cython accelerator
try:
    from plugin.scripting.payload_codec import fast_flatten_grid_2d
    res['cython'] = 'optimized' if fast_flatten_grid_2d is not None else 'python'
except ImportError:
    res['cython'] = 'missing'

# Explicit try/except blocks for each package (forbidden to use __import__ loop in sandbox)
try:
    import numpy
    res['p']['numpy'] = 'present'
except ImportError:
    res['p']['numpy'] = None

try:
    import pandas
    res['p']['pandas'] = 'present'
except ImportError:
    res['p']['pandas'] = None

try:
    import scipy
    res['p']['scipy'] = 'present'
except ImportError:
    res['p']['scipy'] = None

try:
    import sklearn
    res['p']['sklearn'] = 'present'
except ImportError:
    res['p']['sklearn'] = None

try:
    import matplotlib
    res['p']['matplotlib'] = 'present'
except ImportError:
    res['p']['matplotlib'] = None

try:
    import sympy
    res['p']['sympy'] = 'present'
except ImportError:
    res['p']['sympy'] = None

try:
    import webview
    res['p']['webview'] = 'present'
except ImportError:
    res['p']['webview'] = None

try:
    import jedi
    res['p']['jedi'] = 'present'
except ImportError:
    res['p']['jedi'] = None

try:
    import PyQt6
    res['p']['PyQt6'] = 'present'
except ImportError:
    res['p']['PyQt6'] = None

try:
    import PyQt6.QtWebEngineWidgets
    res['p']['PyQt6.QtWebEngineWidgets'] = 'present'
except ImportError:
    res['p']['PyQt6.QtWebEngineWidgets'] = None

try:
    import qtpy
    res['p']['qtpy'] = 'present'
except ImportError:
    res['p']['qtpy'] = None

try:
    import data_profiling
    res['p']['data_profiling'] = 'present'
except ImportError:
    res['p']['data_profiling'] = None

try:
    import statsmodels
    res['p']['statsmodels'] = 'present'
except ImportError:
    res['p']['statsmodels'] = None

try:
    import pandas_montecarlo
    res['p']['pandas_montecarlo'] = 'present'
except ImportError:
    res['p']['pandas_montecarlo'] = None

try:
    import seaborn
    res['p']['seaborn'] = 'present'
except ImportError:
    res['p']['seaborn'] = None

try:
    import yfinance
    res['p']['yfinance'] = 'present'
except ImportError:
    res['p']['yfinance'] = None

try:
    import pandas_ta
    res['p']['pandas_ta'] = 'present'
except ImportError:
    res['p']['pandas_ta'] = None

try:
    import quantstats
    res['p']['quantstats'] = 'present'
except ImportError:
    res['p']['quantstats'] = None

try:
    import pypfopt
    res['p']['pypfopt'] = 'present'
except ImportError:
    res['p']['pypfopt'] = None

try:
    import pint
    res['p']['pint'] = 'present'
except ImportError:
    res['p']['pint'] = None

result = res
"""

_QUANT_INSTALL_CMD = "pip install yfinance pandas-ta quantstats pyportfolioopt"

# Vision stack (docs/image-recognition.md §7–§13): probed outside the AST sandbox because
# docling/paddleocr/paddle are not whitelisted for LLM-submitted venv scripts.
# Primary OCR: docling + rapidocr-paddle. Fallback: paddleocr + paddle.
# Optional: ultralytics (detection helpers), skimage (trusted helper preprocessing).
_ANALYSIS_INSTALL_CMD = (
    "pip install numpy pandas scipy scikit-learn statsmodels ydata-profiling pandas-montecarlo"
)
_VISION_PACKAGE_KEYS = ("docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage")
_DOCLING_INSTALL_CMD = "pip install docling rapidocr-paddle numpy pillow css-inline"
_VISION_OCR_INSTALL_CMD = _DOCLING_INSTALL_CMD
_VISION_PADDLE_FALLBACK_CMD = "pip install paddleocr paddlepaddle numpy"
_VIZ_INSTALL_CMD = "pip install matplotlib seaborn"
_SYMBOLIC_INSTALL_CMD = "pip install sympy"
_VISION_PROBE_SCRIPT = """
import json
out = {}
try:
    import docling.document_converter  # noqa: F401
    out["docling"] = "present"
except Exception as exc:
    out["docling"] = None
    out["docling_import_error"] = str(exc)
try:
    import rapidocr
    out["rapidocr"] = "present"
except Exception:
    try:
        import rapidocr_onnxruntime
        out["rapidocr"] = "present"
    except Exception:
        out["rapidocr"] = None
try:
    import paddleocr
    out["paddleocr"] = "present"
except Exception:
    out["paddleocr"] = None
try:
    import paddle
    out["paddle"] = "present"
except Exception:
    out["paddle"] = None
try:
    import ultralytics
    out["ultralytics"] = "present"
except Exception:
    out["ultralytics"] = None
try:
    import skimage
    out["skimage"] = "present"
except Exception:
    out["skimage"] = None
try:
    import css_inline  # noqa: F401
    out["css_inline"] = "present"
except Exception:
    out["css_inline"] = None
print(json.dumps(out))
"""

_VISION_PROBE_TIMEOUT_HINT = _(
    "Vision probe timed out (Docling import can take 10–30s on first check)."
)
_VISION_PROBE_FAILED_HINT = _("Vision probe failed (see writeragent_debug.log).")


def _probe_vision_packages(
    python_exe: str,
    timeout: float = VISION_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check vision stack in the real venv interpreter (not the sandboxed warm worker)."""
    try:
        proc = subprocess.run(
            [python_exe, "-c", _VISION_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            env=scrub_subprocess_env(dict(os.environ)),
        )
    except subprocess.TimeoutExpired:
        return {}, _VISION_PROBE_TIMEOUT_HINT
    except OSError as exc:
        log.warning("Vision package probe could not run: %s", exc)
        return {}, _VISION_PROBE_FAILED_HINT
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        log.warning("Vision package probe exit %s: %s", proc.returncode, stderr)
        return {}, _VISION_PROBE_FAILED_HINT
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("Vision package probe returned invalid JSON: %r", (proc.stdout or "")[:200])
        return {}, _VISION_PROBE_FAILED_HINT
    if not isinstance(parsed, dict):
        return {}, _VISION_PROBE_FAILED_HINT
    return parsed, None


_SELF_CHECK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Scientific Libraries", ("numpy", "pandas", "scipy", "sklearn", "matplotlib", "sympy")),
    ("Data Analysis / EDA Libraries", ("data_profiling", "statsmodels", "pandas_montecarlo")),
    ("UI / Monaco Libraries", ("webview", "jedi", "PyQt6", "PyQt6.QtWebEngineWidgets", "qtpy")),
    ("Visualization Libraries", ("matplotlib", "seaborn")),
    ("Computer Algebra", ("sympy",)),
    ("Quantitative Finance Libraries", ("yfinance", "pandas_ta", "quantstats", "pypfopt")),
    ("Data Engineering Libraries", ("pint",)),
)

_ALLOWED_PROBE_MODULES = frozenset(pkg for _title, pkgs in _SELF_CHECK_GROUPS for pkg in pkgs)

_VERSION_PROBE_SCRIPT = """
import platform
result = {'v': platform.python_version(), 'arch': platform.machine()}
"""


def _package_probe_script(module: str) -> str:
    """Return a sandbox-safe one-import probe script for a whitelisted *module*."""
    if module not in _ALLOWED_PROBE_MODULES:
        raise ValueError(f"unsupported probe module: {module}")
    if module == "PyQt6.QtWebEngineWidgets":
        import_stmt = "import PyQt6.QtWebEngineWidgets"
    else:
        import_stmt = f"import {module}"
    return f"""
try:
    {import_stmt}
    result = 'present'
except ImportError:
    result = None
"""


def _format_group_lines(title: str, keys: tuple[str, ...] | list[str], packages: dict[str, Any]) -> list[str]:
    found: list[str] = []
    missing: list[str] = []
    for key in keys:
        if packages.get(key) == "present":
            found.append(key)
        else:
            missing.append(key)
    lines = [f"\n{title}:"]
    if found:
        lines.append(f"  Present: {', '.join(found)}")
    if missing:
        lines.append(f"  Missing: {', '.join(missing)}")
    return lines


def _self_check_group_specs(data: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        ("Scientific Libraries", tuple(data.get("sci", ()))),
        ("Data Analysis / EDA Libraries", tuple(data.get("eda", ()))),
        ("UI / Monaco Libraries", tuple(data.get("ui", ()))),
        (_("Visualization Libraries"), tuple(data.get("viz", ()))),
        (_("Computer Algebra"), tuple(data.get("cas", ()))),
        (_("Quantitative Finance Libraries"), tuple(data.get("quant", ()))),
        (_("Data Engineering Libraries"), tuple(data.get("data_eng", ()))),
        (_("Vision Libraries"), tuple(data.get("vision", ()))),
    ]


def _build_probe_display(
    data: dict[str, Any],
    *,
    completed_groups: int,
    partial_group_keys: tuple[str, ...] | None = None,
    partial_group_title: str | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    include_vision: bool = False,
) -> str:
    """Rebuild the Settings → Python Test body in the legacy grouped Present/Missing format."""
    version = data.get("v", "unknown")
    arch = data.get("arch", "")
    packages = data.get("p", {})
    header = f"Python {version} ({arch})" if arch else f"Python {version}"
    msg_lines = [f"{header} responds OK."]
    if extra_lines_after_header:
        msg_lines.extend(extra_lines_after_header)

    specs = _self_check_group_specs(data)
    for idx, (title, keys) in enumerate(specs):
        if not keys:
            continue
        if idx < completed_groups:
            msg_lines.extend(_format_group_lines(title, keys, packages))
        elif idx == completed_groups and partial_group_keys and partial_group_title:
            msg_lines.extend(_format_group_lines(partial_group_title, partial_group_keys, packages))
        elif include_vision and title == _("Vision Libraries"):
            msg_lines.extend(_format_group_lines(title, keys, packages))
            vision_failure = data.get("vision_probe_failure")
            if vision_failure:
                msg_lines.append(f"  {vision_failure}")

    return "\n".join(msg_lines)


def _format_self_check_success(data: dict[str, Any]) -> str:
    data = dict(data)
    data.setdefault("vision", list(_VISION_PACKAGE_KEYS))
    return _build_probe_display(data, completed_groups=len(_SELF_CHECK_GROUPS), include_vision=True)


def run_venv_self_check_with_progress(
    python_exe: str,
    on_display: Callable[[str], None],
    timeout: float = 10.0,
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
) -> Tuple[bool, str]:
    """Like :func:`run_venv_self_check` but refreshes the legacy grouped view through *on_display*."""
    timeout_sec = max(1, int(timeout))
    per_pkg_timeout = max(3, min(30, timeout_sec))

    def _status(text: str) -> None:
        if on_status is not None:
            on_status(text)

    def _refresh(
        data: dict[str, Any],
        *,
        completed_groups: int = 0,
        partial_group_keys: tuple[str, ...] | None = None,
        partial_group_title: str | None = None,
        include_vision: bool = False,
    ) -> None:
        on_display(
            _build_probe_display(
                data,
                completed_groups=completed_groups,
                partial_group_keys=partial_group_keys,
                partial_group_title=partial_group_title,
                extra_lines_after_header=extra_lines_after_header,
                include_vision=include_vision,
            )
        )

    _status(_("Starting Python worker..."))
    try:
        manager = PythonWorkerManager.get(python_exe, scrub_subprocess_env(dict(os.environ)))
    except OSError as e:
        return False, f"Could not run Python: {e}"

    _status(_("Reading Python version..."))
    try:
        response = manager.execute(_VERSION_PROBE_SCRIPT, timeout_sec=per_pkg_timeout)
    except OSError as e:
        return False, f"Could not run Python: {e}"

    if response.get("status") != "ok":
        msg = str(response.get("message", "Unknown error"))
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return False, "Timed out waiting for Python (check venv and try again)."
        return False, msg

    version_data = response.get("result")
    if not isinstance(version_data, dict):
        return False, f"Unexpected output from test run: {version_data!r}"

    data: dict[str, Any] = {
        "v": version_data.get("v", "unknown"),
        "arch": version_data.get("arch", ""),
        "p": {},
        "sci": list(_SELF_CHECK_GROUPS[0][1]),
        "eda": list(_SELF_CHECK_GROUPS[1][1]),
        "ui": list(_SELF_CHECK_GROUPS[2][1]),
        "viz": list(_SELF_CHECK_GROUPS[3][1]),
        "cas": list(_SELF_CHECK_GROUPS[4][1]),
        "quant": list(_SELF_CHECK_GROUPS[5][1]),
        "data_eng": list(_SELF_CHECK_GROUPS[6][1]),
    }
    _refresh(data)

    for group_index, (group_title, packages) in enumerate(_SELF_CHECK_GROUPS):
        checked: list[str] = []
        for pkg in packages:
            _status(f"{group_title}: {pkg}")
            try:
                pkg_resp = manager.execute(_package_probe_script(pkg), timeout_sec=per_pkg_timeout)
            except OSError as e:
                return False, f"Could not run Python: {e}"
            if pkg_resp.get("status") != "ok":
                msg = str(pkg_resp.get("message", "Unknown error"))
                return False, msg
            present = pkg_resp.get("result") == "present"
            data["p"][pkg] = "present" if present else None
            checked.append(pkg)
            _refresh(
                data,
                completed_groups=group_index,
                partial_group_keys=tuple(checked),
                partial_group_title=group_title,
            )
        _refresh(data, completed_groups=group_index + 1)

    _status(_("Vision Libraries: loading (first run may take a while)..."))
    vision_probes, vision_failure = _probe_vision_packages(
        python_exe,
        timeout=float(VISION_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and vision_probes:
        packages.update(vision_probes)
    data["vision"] = list(_VISION_PACKAGE_KEYS)
    if vision_failure:
        data["vision_probe_failure"] = vision_failure
    _refresh(data, completed_groups=len(_SELF_CHECK_GROUPS), include_vision=True)

    try:
        final_msg = _build_probe_display(
            data,
            completed_groups=len(_SELF_CHECK_GROUPS),
            include_vision=True,
            extra_lines_after_header=extra_lines_after_header,
        )
        on_display(final_msg)
        return True, final_msg
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def run_venv_self_check(python_exe: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Run a diagnostic script via the warm worker; return (success, user-facing message)."""
    timeout_sec = max(1, int(timeout))
    try:
        manager = PythonWorkerManager.get(python_exe, scrub_subprocess_env(dict(os.environ)))
        response = manager.execute(_DIAGNOSTIC_SCRIPT, timeout_sec=timeout_sec)
    except OSError as e:
        return False, f"Could not run Python: {e}"

    if response.get("status") != "ok":
        msg = str(response.get("message", "Unknown error"))
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return False, "Timed out waiting for Python (check venv and try again)."
        return False, msg

    data = response.get("result")
    if not isinstance(data, dict):
        return False, f"Unexpected output from test run: {data!r}"

    vision_probes, vision_failure = _probe_vision_packages(
        python_exe,
        timeout=float(VISION_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and vision_probes:
        packages.update(vision_probes)
    data["vision"] = list(_VISION_PACKAGE_KEYS)
    if vision_failure:
        data["vision_probe_failure"] = vision_failure

    try:
        return True, _format_self_check_success(data)
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def probe_venv_path(venv_dir: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Resolve *venv_dir* and run a self-check; single entry for UI and tests."""
    if not venv_dir or not str(venv_dir).strip():
        exe = resolve_libreoffice_python()
        if not exe:
            return False, "No process interpreter: sys.executable is missing, not a file, or not executable. Set a venv path in Settings → Python, or fix the LibreOffice install."
        ok, msg = run_venv_self_check(exe, timeout=timeout)
        if ok:
            return True, f"LibreOffice process Python ({exe}) responds OK."
        return ok, msg
    expanded = os.path.expanduser(os.path.expandvars(str(venv_dir).strip()))
    exe = resolve_venv_python(str(venv_dir).strip())
    if not exe:
        if os.path.isfile(expanded):
            return False, f"Not a Python executable: {expanded}"
        if os.path.isdir(expanded):
            return False, (
                "No python found. Use the venv root (folder containing bin/), "
                "the bin/ folder, or the full path to bin/python."
            )
        return False, f"Path not found: {expanded}"
    return run_venv_self_check(exe, timeout=timeout)


def probe_venv_path_with_progress(
    venv_dir: str,
    on_display: Callable[[str], None],
    timeout: float = 10.0,
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
) -> Tuple[bool, str]:
    """Resolve *venv_dir* and run a self-check, refreshing the legacy grouped view."""
    def _status(text: str) -> None:
        if on_status is not None:
            on_status(text)

    if not venv_dir or not str(venv_dir).strip():
        _status(_("Using LibreOffice process Python..."))
        exe = resolve_libreoffice_python()
        if not exe:
            msg = "No process interpreter: sys.executable is missing, not a file, or not executable. Set a venv path in Settings → Python, or fix the LibreOffice install."
            on_display(msg)
            return False, msg
        ok, msg = run_venv_self_check_with_progress(
            exe,
            on_display,
            timeout=timeout,
            on_status=on_status,
            extra_lines_after_header=extra_lines_after_header,
        )
        if ok:
            return True, f"LibreOffice process Python ({exe}) responds OK."
        return ok, msg
    expanded = os.path.expanduser(os.path.expandvars(str(venv_dir).strip()))
    _status(_("Resolving venv Python..."))
    exe = resolve_venv_python(str(venv_dir).strip())
    if not exe:
        if os.path.isfile(expanded):
            msg = f"Not a Python executable: {expanded}"
        elif os.path.isdir(expanded):
            msg = (
                "No python found. Use the venv root (folder containing bin/), "
                "the bin/ folder, or the full path to bin/python."
            )
        else:
            msg = f"Path not found: {expanded}"
        on_display(msg)
        return False, msg
    _status(f"{_('Using')} {exe}")
    return run_venv_self_check_with_progress(
        exe,
        on_display,
        timeout=timeout,
        on_status=on_status,
        extra_lines_after_header=extra_lines_after_header,
    )


# --- Warm worker ---


def _worker_registry_key(exe: str, pool: str) -> str:
    return f"{pool}:{exe}"


class PythonWorkerManager:
    """One warm child process per (pool, Python executable path) pair."""

    def __init__(self, exe: str, env: dict[str, str]) -> None:
        self.exe = exe
        self.env = dict(env)
        self._proc: subprocess.Popen[Any] | None = None
        self._io_lock = threading.Lock()
        self._primed = False

    @classmethod
    def get(cls, exe: str, env: dict[str, str], *, pool: str = WORKER_POOL_DEFAULT) -> PythonWorkerManager:
        """Return the singleton worker for *pool* + *exe* (caller should pass a scrubbed env dict)."""
        key = _worker_registry_key(exe, pool)
        with _registry_lock:
            mgr = _instances.get(key)
            if mgr is None:
                mgr = cls(exe, dict(env))
                _instances[key] = mgr
            return mgr

    @classmethod
    def shutdown_all(cls) -> None:
        """Terminate all workers (tests / extension teardown)."""
        with _registry_lock:
            for mgr in list(_instances.values()):
                mgr._terminate_worker()
            _instances.clear()

    def _is_worker_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _ensure_warmed_unlocked(self) -> dict[str, Any] | None:
        """Spawn worker and prime auto-imports. Returns error dict or None."""
        if self._primed and self._is_worker_alive():
            return None
        prime = self._execute_ipc_unlocked("result = None", timeout_sec=WARM_WORKER_TIMEOUT_SEC)
        if prime.get("status") != "ok":
            return prime
        self._primed = True
        return None

    def _ensure_warmed(self) -> dict[str, Any] | None:
        with self._io_lock:
            return self._ensure_warmed_unlocked()

    def warm(self) -> None:
        """Spawn the worker and trigger auto-imports (numpy etc.) so the next real execute is instant."""
        self._ensure_warmed()

    def _build_request(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"id": str(uuid.uuid4())}
        if action:
            request["action"] = action
            if session_id:
                request["session_id"] = session_id
        else:
            request["code"] = code if code is not None else ""
            if data is not None:
                request["data"] = data
            if session_id:
                request["session_id"] = session_id
            if init_script:
                request["init_script"] = init_script
            if init_session_id:
                request["init_session_id"] = init_session_id
            if init_script_hash:
                request["init_script_hash"] = init_script_hash
        return request

    def _execute_ipc_unlocked(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        timeout_sec: int,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
    ) -> dict[str, Any]:
        request = self._build_request(
            code,
            data=data,
            session_id=session_id,
            action=action,
            init_script=init_script,
            init_session_id=init_session_id,
            init_script_hash=init_script_hash,
        )
        payload = pickle.dumps(request, protocol=5)
        header = struct.pack("!I", len(payload))

        for attempt in range(2):
            try:
                self._ensure_running()
                assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
                stdin = self._proc.stdin
                stdout = self._proc.stdout
                stdin.write(header)
                stdin.write(payload)
                stdin.flush()

                response_bytes = self._read_response_bytes(stdout, timeout_sec)
                if not response_bytes:
                    stderr_out = self._drain_stderr()
                    raise RuntimeError(f"Worker closed stdout without a response{stderr_out}")
                # Trusted IPC: bytes from our own worker_harness child over a private pipe.
                response = pickle.loads(response_bytes)  # nosec B301
                return self._normalize_response(response)
            except (BrokenPipeError, pickle.UnpicklingError, RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                self._terminate_worker()
                if attempt == 1:
                    return {"status": "error", "message": _worker_error_message(e)}
        return {"status": "error", "message": "Python worker failed"}

    def execute(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        timeout_sec: int | None = None,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
    ) -> dict[str, Any]:
        """Run *code* in the warm worker, or handle *action* (e.g. reset_session).

        Without *session_id*, each execute uses a fresh namespace in the child. With
        *session_id*, the child reuses one LocalPythonExecutor per id.

        Cold start: spawn + auto-imports run first under :data:`WARM_WORKER_TIMEOUT_SEC`
        and are not charged against *timeout_sec*.
        """
        if timeout_sec is None:
            timeout_sec = python_exec_timeout_default()

        with self._io_lock:
            warm_err = self._ensure_warmed_unlocked()
            if warm_err is not None:
                return warm_err
            return self._execute_ipc_unlocked(
                code,
                data=data,
                timeout_sec=timeout_sec,
                session_id=session_id,
                action=action,
                init_script=init_script,
                init_session_id=init_session_id,
                init_script_hash=init_script_hash,
            )

    def _normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") == "ok":
            result = response.get("result")
            if result is not None:
                result = host_unpack_data(result, as_nested_list=True)
            return {
                "status": "ok",
                "result": result,
                "stdout": (response.get("stdout") or "").strip(),
                "stderr": "",
            }
        msg = response.get("message") or response.get("error") or "Unknown worker error"
        tb = response.get("traceback")
        if tb and isinstance(tb, str):
            msg = f"{msg}\n{tb.strip()}"
        out: dict[str, Any] = {
            "status": "error",
            "message": str(msg),
            "stdout": (response.get("stdout") or "").strip(),
        }
        return out

    def _ensure_running(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._terminate_worker()
        popen_kw: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self.env,
            "text": False,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            popen_kw["preexec_fn"] = os.setsid
        self._proc = subprocess.Popen(wrap_command_for_sandbox([self.exe, _HARNESS_PATH]), **popen_kw)
        optimize_popen_pipes(self._proc)
        log.debug("Started Python worker pid=%s exe=%s", self._proc.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        assert self._proc is not None
        # Windows select.select() only supports sockets, not pipes (raises
        # WinError 10038).  Use a thread-based blocking read there instead.
        if sys.platform == "win32":
            return self._read_response_bytes_threaded(stdout, timeout_sec)
        return self._read_response_bytes_select(stdout, timeout_sec)

    def _read_response_bytes_select(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        """POSIX path: use select() to poll the pipe with a timeout."""
        assert self._proc is not None
        end = time.time() + timeout_sec

        def _read_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                if time.time() >= end:
                    raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
                remaining = end - time.time()
                ready, _, _ = select.select([stdout], [], [], min(1.0, remaining))
                if ready:
                    chunk = stdout.read(n - len(buf))
                    if not chunk:
                        return bytes()
                    buf.extend(chunk)
                if self._proc is not None and self._proc.poll() is not None and not ready:
                    break
            return bytes(buf)

        header = _read_exact(4)
        if len(header) < 4:
            return b""

        size = struct.unpack("!I", header)[0]
        payload = _read_exact(size)
        if len(payload) < size:
            return b""

        return payload

    def _read_response_bytes_threaded(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        """Windows path: blocking read in a daemon thread with join-timeout."""
        result: list[bytes] = [b""]
        error: list[BaseException | None] = [None]

        def _reader() -> None:
            try:
                header = stdout.read(4)
                if not header or len(header) < 4:
                    return
                size = struct.unpack("!I", header)[0]
                payload = stdout.read(size)
                if len(payload) < size:
                    return
                result[0] = payload
            except Exception as exc:
                error[0] = exc

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
        if error[0] is not None:
            raise error[0]
        return result[0]

    def _drain_stderr(self) -> str:
        """Read any pending stderr from the crashed worker for diagnostics."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            # Worker already exited (stdout closed); wait briefly then read stderr.
            self._proc.wait(timeout=2)
        except Exception:
            pass
        try:
            stderr_bytes = self._proc.stderr.read()
        except Exception:
            return ""
        if not stderr_bytes:
            return ""
        text = stderr_bytes.decode("utf-8", errors="replace").strip()
        return f"\nWorker stderr:\n{text}"

    def _terminate_worker(self) -> None:
        proc = self._proc
        self._proc = None
        self._primed = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if sys.platform == "win32":
                    proc.kill()
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        proc.kill()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


# --- Public entrypoints ---


def _resolve_worker_python(uno_ctx: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Return (exe, error_response) for the configured venv / LO interpreter."""
    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return None, {
                "status": "error",
                "message": f"No python executable found under configured venv: {venv_dir!r}",
            }
        log.debug("run_venv_code: using venv interpreter under %s", venv_dir)
        return exe, None
    exe = resolve_libreoffice_python()
    if not exe:
        return None, {
            "status": "error",
            "message": (
                "Could not resolve a Python interpreter (sys.executable missing, not a file, or not executable). "
                "Set scripting.python_venv_path in Settings → Python for a dedicated venv, or fix the LibreOffice install."
            ),
        }
    log.debug("run_venv_code: using process interpreter %s (no venv path set)", exe)
    return exe, None


def _worker_manager_for_ctx(
    uno_ctx: Any,
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[PythonWorkerManager | None, dict[str, Any] | None]:
    exe, err = _resolve_worker_python(uno_ctx)
    if err is not None:
        return None, err
    assert exe is not None
    child_env = scrub_subprocess_env(dict(os.environ))
    return PythonWorkerManager.get(exe, child_env, pool=pool), None


def run_code_in_user_venv(
    uno_ctx: Any,
    code: str,
    *,
    data: Any = None,
    timeout_sec: int | None = None,
    session_id: str | None = None,
    init_script: str | None = None,
    init_session_id: str | None = None,
    init_script_hash: str | None = None,
    active_domain: str | None = None,
    python_tool_domain: str | None = None,
    worker_pool: str = WORKER_POOL_DEFAULT,
) -> Dict[str, Any]:
    """Execute *code* via :class:`PythonWorkerManager` (warm process).

    Without *session_id*, each call uses an isolated namespace in the child. With
    *session_id*, the child reuses one namespace per workbook (shared kernel).

    *worker_pool* selects which warm child to use (e.g. embeddings vs Calc/chat default).

    *active_domain* / *python_tool_domain* are reserved for future venv→LO tool RPC (not wired yet).
    """
    del active_domain, python_tool_domain  # deferred — see docs/enabling_numpy_in_libreoffice.md §7
    if not (code or "").strip():
        return {"status": "error", "message": "No code provided."}

    manager, err = _worker_manager_for_ctx(uno_ctx, pool=worker_pool)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    return manager.execute(
        code,
        data=data,
        timeout_sec=timeout_sec,
        session_id=session_id,
        init_script=init_script,
        init_session_id=init_session_id,
        init_script_hash=init_script_hash,
    )


def reset_python_session(uno_ctx: Any, session_id: str, *, timeout_sec: int | None = None) -> Dict[str, Any]:
    """Drop the shared-kernel executor for *session_id* in the warm worker."""
    if not (session_id or "").strip():
        return {"status": "error", "message": "No session_id provided."}

    manager, err = _worker_manager_for_ctx(uno_ctx)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    return manager.execute(
        None,
        timeout_sec=timeout_sec,
        session_id=session_id,
        action="reset_session",
    )


def warm_venv_worker(uno_ctx: Any) -> None:
    """Pre-warm the venv subprocess (spawn + trigger auto-imports). Safe to call from a background thread."""
    venv_dir = get_config_str(uno_ctx, "scripting.python_venv_path").strip()
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
    else:
        exe = resolve_libreoffice_python()
    if not exe:
        return
    child_env = scrub_subprocess_env(dict(os.environ))
    manager = PythonWorkerManager.get(exe, child_env)
    manager.warm()
