# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Venv self-check diagnostics and Settings → Python Test probing."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Callable, Optional, Tuple

from plugin.framework.i18n import _
from plugin.scripting.config_limits import EMBEDDINGS_PROBE_TIMEOUT_SEC, VISION_PROBE_TIMEOUT_SEC
from plugin.scripting.sandbox import resolve_libreoffice_python, resolve_venv_python, scrub_subprocess_env

log = logging.getLogger(__name__)

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

# Embeddings stack (docs/embeddings.md): probed outside the AST sandbox because
# sqlite_vec/langgraph/langchain_* are not whitelisted for LLM-submitted venv scripts.
from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

_EMBEDDINGS_INSTALL_CMD = EMBEDDINGS_VENV_PIP_INSTALL
_EMBEDDINGS_PACKAGE_KEYS = (
    "envwrap",
    "sentence_transformers",
    "sqlite_vec",
    "langgraph",
    "langchain_core",
    "langchain_text_splitters",
    "odfpy",
    "pandas",
    "openpyxl",
    "xlrd",
    "python_docx",
)
_EMBEDDINGS_PROBE_SCRIPT = """
import json
out = {}
try:
    import envwrap  # noqa: F401
    out["envwrap"] = "present"
except Exception:
    out["envwrap"] = None
try:
    import sentence_transformers  # noqa: F401
    out["sentence_transformers"] = "present"
except Exception as exc:
    out["sentence_transformers"] = None
    out["sentence_transformers_import_error"] = str(exc)
try:
    import sqlite_vec  # noqa: F401
    out["sqlite_vec"] = "present"
except Exception:
    out["sqlite_vec"] = None
try:
    import langgraph  # noqa: F401
    out["langgraph"] = "present"
except Exception:
    out["langgraph"] = None
try:
    import langchain_core  # noqa: F401
    out["langchain_core"] = "present"
except Exception:
    out["langchain_core"] = None
try:
    import langchain_text_splitters  # noqa: F401
    out["langchain_text_splitters"] = "present"
except Exception:
    out["langchain_text_splitters"] = None
try:
    import odf  # noqa: F401
    out["odfpy"] = "present"
except Exception:
    out["odfpy"] = None
try:
    import pandas  # noqa: F401
    out["pandas"] = "present"
except Exception:
    out["pandas"] = None
try:
    import openpyxl  # noqa: F401
    out["openpyxl"] = "present"
except Exception:
    out["openpyxl"] = None
try:
    import xlrd  # noqa: F401
    out["xlrd"] = "present"
except Exception:
    out["xlrd"] = None
try:
    import docx  # noqa: F401
    out["python_docx"] = "present"
except Exception:
    out["python_docx"] = None
print(json.dumps(out))
"""

_EMBEDDINGS_PROBE_TIMEOUT_HINT = _(
    "Embeddings probe timed out (sentence-transformers import can take 10–30s on first check)."
)
_EMBEDDINGS_PROBE_FAILED_HINT = _("Embeddings probe failed (see writeragent_debug.log).")


def _probe_embeddings_packages(
    python_exe: str,
    timeout: float = EMBEDDINGS_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check embeddings stack in the real venv interpreter (not the sandboxed warm worker)."""
    try:
        proc = subprocess.run(
            [python_exe, "-c", _EMBEDDINGS_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            env=scrub_subprocess_env(dict(os.environ)),
        )
    except subprocess.TimeoutExpired:
        return {}, _EMBEDDINGS_PROBE_TIMEOUT_HINT
    except OSError as exc:
        log.warning("Embeddings package probe could not run: %s", exc)
        return {}, _EMBEDDINGS_PROBE_FAILED_HINT
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        log.warning("Embeddings package probe exit %s: %s", proc.returncode, stderr)
        return {}, _EMBEDDINGS_PROBE_FAILED_HINT
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("Embeddings package probe returned invalid JSON: %r", (proc.stdout or "")[:200])
        return {}, _EMBEDDINGS_PROBE_FAILED_HINT
    if not isinstance(parsed, dict):
        return {}, _EMBEDDINGS_PROBE_FAILED_HINT
    return parsed, None


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
    lines: list[str] = []
    if found:
        lines.append(f"\n{title}: {', '.join(found)}")
    else:
        lines.append(f"\n{title}:")
    if missing:
        lines.append(f"Missing: {', '.join(missing)}")
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
        (_("Embeddings Libraries"), tuple(data.get("embeddings", ()))),
    ]


def _build_probe_display(
    data: dict[str, Any],
    *,
    completed_groups: int,
    partial_group_keys: tuple[str, ...] | None = None,
    partial_group_title: str | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    include_embeddings: bool = False,
    include_vision: bool = False,
) -> str:
    """Rebuild the Settings → Python Test body in the legacy grouped Present/Missing format."""
    version = data.get("v", "unknown")
    arch = data.get("arch", "")
    packages = data.get("p", {})
    header = f"Python {version} ({arch})" if arch else f"Python {version}"
    first_line = f"{header} responds OK."
    if extra_lines_after_header:
        extras = " ".join(line.strip() for line in extra_lines_after_header if line and line.strip())
        if extras:
            first_line = f"{first_line} {extras}"
    msg_lines = [first_line]

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
        elif include_embeddings and title == _("Embeddings Libraries"):
            msg_lines.extend(_format_group_lines(title, keys, packages))
            embeddings_failure = data.get("embeddings_probe_failure")
            if embeddings_failure:
                msg_lines.append(f"  {embeddings_failure}")

    return "\n".join(msg_lines)


def _format_self_check_success(data: dict[str, Any]) -> str:
    data = dict(data)
    data.setdefault("embeddings", list(_EMBEDDINGS_PACKAGE_KEYS))
    data.setdefault("vision", list(_VISION_PACKAGE_KEYS))
    return _build_probe_display(
        data,
        completed_groups=len(_SELF_CHECK_GROUPS),
        include_embeddings=True,
        include_vision=True,
    )


def run_venv_self_check_with_progress(
    python_exe: str,
    on_display: Callable[[str], None],
    timeout: float = 10.0,
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
) -> Tuple[bool, str]:
    """Like :func:`run_venv_self_check` but refreshes the legacy grouped view through *on_display*."""
    from plugin.scripting.venv_worker import PythonWorkerManager

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
        include_embeddings: bool = False,
        include_vision: bool = False,
    ) -> None:
        on_display(
            _build_probe_display(
                data,
                completed_groups=completed_groups,
                partial_group_keys=partial_group_keys,
                partial_group_title=partial_group_title,
                extra_lines_after_header=extra_lines_after_header,
                include_embeddings=include_embeddings,
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

    _status(_("Embeddings Libraries: loading (first run may take a while)..."))
    embeddings_probes, embeddings_failure = _probe_embeddings_packages(
        python_exe,
        timeout=float(EMBEDDINGS_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and embeddings_probes:
        packages.update(embeddings_probes)
    data["embeddings"] = list(_EMBEDDINGS_PACKAGE_KEYS)
    if embeddings_failure:
        data["embeddings_probe_failure"] = embeddings_failure
    _refresh(
        data,
        completed_groups=len(_SELF_CHECK_GROUPS),
        include_embeddings=True,
        include_vision=True,
    )

    try:
        final_msg = _build_probe_display(
            data,
            completed_groups=len(_SELF_CHECK_GROUPS),
            include_embeddings=True,
            include_vision=True,
            extra_lines_after_header=extra_lines_after_header,
        )
        on_display(final_msg)
        return True, final_msg
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def run_venv_self_check(python_exe: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Run a diagnostic script via the warm worker; return (success, user-facing message)."""
    from plugin.scripting.venv_worker import PythonWorkerManager

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

    embeddings_probes, embeddings_failure = _probe_embeddings_packages(
        python_exe,
        timeout=float(EMBEDDINGS_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and embeddings_probes:
        packages.update(embeddings_probes)
    data["embeddings"] = list(_EMBEDDINGS_PACKAGE_KEYS)
    if embeddings_failure:
        data["embeddings_probe_failure"] = embeddings_failure

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
