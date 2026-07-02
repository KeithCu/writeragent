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
from plugin.scripting.config_limits import (
    SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
    VECTOR_SEARCH_PROBE_TIMEOUT_SEC,
    VISION_PROBE_TIMEOUT_SEC,
)
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
ui = ['webview', 'rocher', 'jedi', 'PyQt6', 'PyQt6.QtWebEngineWidgets', 'qtpy']
quant = ['yfinance', 'pandas_ta', 'quantstats', 'pypfopt']
data_eng = ['pint', 'duckdb']
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
    import rocher
    res['p']['rocher'] = 'present'
except ImportError:
    res['p']['rocher'] = None

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

try:
    import duckdb
    res['p']['duckdb'] = 'present'
except ImportError:
    res['p']['duckdb'] = None

result = res
"""

# Install hints use uv (recommended).
# Users point Settings → Python at a venv created with `uv venv` and populated via `uv pip`.
_QUANT_INSTALL_CMD = "uv pip install yfinance pandas-ta quantstats pyportfolioopt"

# Vision stack (docs/image-recognition.md §7–§13): probed outside the AST sandbox because
# docling/paddleocr/paddle are not whitelisted for LLM-submitted venv scripts.
# Primary OCR: docling + rapidocr-paddle. Fallback: paddleocr + paddle.
# Optional: ultralytics (detection helpers), skimage (trusted helper preprocessing).
_ANALYSIS_INSTALL_CMD = (
    "uv pip install numpy pandas scipy scikit-learn statsmodels ydata-profiling pandas-montecarlo"
)
_VISION_PACKAGE_KEYS = ("docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage")
_DOCLING_INSTALL_CMD = "uv pip install docling rapidocr-paddle numpy pillow css-inline"
_VISION_OCR_INSTALL_CMD = _DOCLING_INSTALL_CMD
_VISION_PADDLE_FALLBACK_CMD = "uv pip install paddleocr paddlepaddle numpy"
_VIZ_INSTALL_CMD = "uv pip install matplotlib seaborn"
_SYMBOLIC_INSTALL_CMD = "uv pip install sympy"
_AUDIO_PACKAGE_KEYS = ("sounddevice", "input_device")
_AUDIO_INSTALL_CMD = "uv pip install sounddevice"
_AUDIO_LINUX_PORTAUDIO_HINT = _("On Linux also install system PortAudio: sudo apt-get install libportaudio2")
_AUDIO_PROBE_SCRIPT = """
import json
out = {}
try:
    import sounddevice as sd
    out["sounddevice"] = "present"
    devices = sd.query_devices()
    has_input = any(d.get("max_input_channels", 0) > 0 for d in devices)
    out["input_device"] = "present" if has_input else None
except Exception:
    out["sounddevice"] = None
    out["input_device"] = None
print(json.dumps(out))
"""
_AUDIO_PROBE_TIMEOUT_HINT = _("Audio probe timed out (sounddevice import failed or hung).")
_AUDIO_PROBE_FAILED_HINT = _("Audio probe failed (see writeragent_debug.log).")
_TEXT_ANALYTICS_INSTALL_CMD = "uv pip install spacy textdescriptives transformers language-tool-python torch --index-url https://download.pytorch.org/whl/cpu && python -m spacy download xx_sent_ud_sm"
_NLP_PACKAGE_KEYS = ("spacy", "textdescriptives", "transformers", "language_tool_python")
_NLP_PROBE_SCRIPT = """
import json
out = {}
try:
    import spacy  # noqa: F401
    out["spacy"] = "present"
except Exception:
    out["spacy"] = None
try:
    import textdescriptives  # noqa: F401
    out["textdescriptives"] = "present"
except Exception:
    out["textdescriptives"] = None
try:
    import transformers  # noqa: F401
    out["transformers"] = "present"
except Exception:
    out["transformers"] = None
try:
    import language_tool_python  # noqa: F401
    res = language_tool_python.__file__
    out["language_tool_python"] = "present"
except Exception:
    out["language_tool_python"] = None
print(json.dumps(out))
"""

_NLP_PROBE_TIMEOUT_HINT = _(
    "Text/NLP probe timed out (spaCy or transformers cold import can take 10–30s on first check)."
)
_NLP_PROBE_FAILED_HINT = _("Text/NLP probe failed (see writeragent_debug.log).")
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

# Vector Search stack (docs/embeddings.md): probed outside the AST sandbox because
# sqlite_vec/langgraph/langchain_* are not whitelisted for LLM-submitted venv scripts.
from plugin.embeddings.venv.embeddings_index import EMBEDDINGS_VENV_PIP_INSTALL

_VECTOR_SEARCH_INSTALL_CMD = EMBEDDINGS_VENV_PIP_INSTALL
_VECTOR_SEARCH_PACKAGE_KEYS = (
    "envwrap",
    "sentence_transformers",
    "sqlite_vec",
    "zvec",
    "langgraph",
    "langchain_core",
    "langchain_text_splitters",
    "icu4py",
    "odfpy",
    "pandas",
    "openpyxl",
    "xlrd",
    "python_docx",
    "langdetect",
)
_VECTOR_SEARCH_PROBE_SCRIPT = """
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
    import zvec  # noqa: F401
    out["zvec"] = "present"
except Exception:
    out["zvec"] = None
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
    import icu4py  # noqa: F401
    out["icu4py"] = "present"
except Exception:
    out["icu4py"] = None
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
try:
    import langdetect  # noqa: F401
    out["langdetect"] = "present"
except Exception:
    out["langdetect"] = None
print(json.dumps(out))
"""

_VECTOR_SEARCH_PROBE_TIMEOUT_HINT = _(
    "Vector Search probe timed out (sentence-transformers import can take 10–30s on first check)."
)
_VECTOR_SEARCH_PROBE_FAILED_HINT = _("Vector Search probe failed (see writeragent_debug.log).")


def _probe_nlp_packages(
    python_exe: str,
    timeout: float = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check Text/NLP stack in the real venv interpreter (not the sandboxed warm worker)."""
    try:
        proc = subprocess.run(
            [python_exe, "-c", _NLP_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            env=scrub_subprocess_env(dict(os.environ)),
        )
    except subprocess.TimeoutExpired:
        return {}, _NLP_PROBE_TIMEOUT_HINT
    except OSError as exc:
        log.warning("Text/NLP package probe could not run: %s", exc)
        return {}, _NLP_PROBE_FAILED_HINT
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        log.warning("Text/NLP package probe exit %s: %s", proc.returncode, stderr)
        return {}, _NLP_PROBE_FAILED_HINT
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("Text/NLP package probe returned invalid JSON: %r", (proc.stdout or "")[:200])
        return {}, _NLP_PROBE_FAILED_HINT
    if not isinstance(parsed, dict):
        return {}, _NLP_PROBE_FAILED_HINT
    return parsed, None


def _probe_vector_search_packages(
    python_exe: str,
    timeout: float = VECTOR_SEARCH_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check embeddings stack in the real venv interpreter (not the sandboxed warm worker)."""
    try:
        proc = subprocess.run(
            [python_exe, "-c", _VECTOR_SEARCH_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
            env=scrub_subprocess_env(dict(os.environ)),
        )
    except subprocess.TimeoutExpired:
        return {}, _VECTOR_SEARCH_PROBE_TIMEOUT_HINT
    except OSError as exc:
        log.warning("Vector Search package probe could not run: %s", exc)
        return {}, _VECTOR_SEARCH_PROBE_FAILED_HINT
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        log.warning("Vector Search package probe exit %s: %s", proc.returncode, stderr)
        return {}, _VECTOR_SEARCH_PROBE_FAILED_HINT
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("Vector Search package probe returned invalid JSON: %r", (proc.stdout or "")[:200])
        return {}, _VECTOR_SEARCH_PROBE_FAILED_HINT
    if not isinstance(parsed, dict):
        return {}, _VECTOR_SEARCH_PROBE_FAILED_HINT
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


def _probe_audio_packages(
    python_exe: str,
    timeout: float = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
) -> tuple[dict[str, str | None], str | None]:
    try:
        proc = subprocess.run(
            [python_exe, "-c", _AUDIO_PROBE_SCRIPT],
            capture_output=True,
            timeout=timeout,
            env=scrub_subprocess_env(dict(os.environ)),
            text=True,
        )
    except subprocess.TimeoutExpired:
        return {}, _AUDIO_PROBE_TIMEOUT_HINT
    except OSError as exc:
        log.warning("Audio package probe could not run: %s", exc)
        return {}, _AUDIO_PROBE_FAILED_HINT
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        log.warning("Audio package probe exit %s: %s", proc.returncode, stderr)
        return {}, _AUDIO_PROBE_FAILED_HINT
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("Audio package probe returned invalid JSON: %r", (proc.stdout or "")[:200])
        return {}, _AUDIO_PROBE_FAILED_HINT
    if not isinstance(parsed, dict):
        return {}, _AUDIO_PROBE_FAILED_HINT
    return parsed, None


_SANDBOX_SELF_CHECK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Scientific Libraries", ("numpy", "pandas", "scipy", "sklearn", "matplotlib", "sympy")),
    ("Data Analysis / EDA Libraries", ("data_profiling", "statsmodels", "pandas_montecarlo")),
    ("UI / Monaco Libraries", ("webview", "rocher", "jedi", "PyQt6", "PyQt6.QtWebEngineWidgets", "qtpy")),
    ("Visualization Libraries", ("matplotlib", "seaborn")),
    ("Computer Algebra", ("sympy",)),
    ("Quantitative Finance Libraries", ("yfinance", "pandas_ta", "quantstats", "pypfopt")),
    ("Data Engineering Libraries", ("pint", "duckdb")),
)

# Display order includes NLP (probed via subprocess, not the sandbox worker loop).
_SELF_CHECK_SANDBOX_GROUP_COUNT = len(_SANDBOX_SELF_CHECK_GROUPS)
_SELF_CHECK_DISPLAY_GROUP_COUNT = _SELF_CHECK_SANDBOX_GROUP_COUNT + 1  # + Text / NLP

_ALLOWED_PROBE_MODULES = frozenset(pkg for _title, pkgs in _SANDBOX_SELF_CHECK_GROUPS for pkg in pkgs)

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
        (_("Audio Recording"), tuple(data.get("audio", ()))),
        (_("Scientific Libraries"), tuple(data.get("sci", ()))),
        (_("Data Analysis / EDA Libraries"), tuple(data.get("eda", ()))),
        (_("UI / Monaco Libraries"), tuple(data.get("ui", ()))),
        (_("Visualization Libraries"), tuple(data.get("viz", ()))),
        (_("Computer Algebra"), tuple(data.get("cas", ()))),
        (_("Quantitative Finance Libraries"), tuple(data.get("quant", ()))),
        (_("Data Engineering Libraries"), tuple(data.get("data_eng", ()))),
        (_("Text / NLP Libraries"), tuple(data.get("nlp", ()))),
        (_("Vision Libraries"), tuple(data.get("vision", ()))),
        (_("Vector Search Libraries"), tuple(data.get("vector_search", ()))),
    ]


def _build_probe_display(
    data: dict[str, Any],
    *,
    completed_groups: int,
    partial_group_keys: tuple[str, ...] | None = None,
    partial_group_title: str | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    include_vector_search: bool = False,
    include_vision: bool = False,
    include_audio: bool = False,
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
    sandbox_titles = [
        _("Scientific Libraries"),
        _("Data Analysis / EDA Libraries"),
        _("UI / Monaco Libraries"),
        _("Visualization Libraries"),
        _("Computer Algebra"),
        _("Quantitative Finance Libraries"),
        _("Data Engineering Libraries"),
    ]
    for idx, (title, keys) in enumerate(specs):
        if not keys:
            continue
        if title in sandbox_titles:
            s_idx = sandbox_titles.index(title)
            if s_idx < completed_groups:
                msg_lines.extend(_format_group_lines(title, keys, packages))
            elif s_idx == completed_groups and partial_group_keys and partial_group_title == title:
                msg_lines.extend(_format_group_lines(title, partial_group_keys, packages))
        elif title == _("Text / NLP Libraries"):
            if completed_groups >= _SELF_CHECK_DISPLAY_GROUP_COUNT:
                msg_lines.extend(_format_group_lines(title, keys, packages))
                nlp_failure = data.get("nlp_probe_failure")
                if nlp_failure:
                    msg_lines.append(f"  {nlp_failure}")
        elif title == _("Vision Libraries"):
            if include_vision:
                msg_lines.extend(_format_group_lines(title, keys, packages))
                vision_failure = data.get("vision_probe_failure")
                if vision_failure:
                    msg_lines.append(f"  {vision_failure}")
        elif title == _("Vector Search Libraries"):
            if include_vector_search:
                msg_lines.extend(_format_group_lines(title, keys, packages))
                vector_search_failure = data.get("vector_search_probe_failure")
                if vector_search_failure:
                    msg_lines.append(f"  {vector_search_failure}")
        elif title == _("Audio Recording"):
            if include_audio:
                msg_lines.extend(_format_group_lines(title, keys, packages))
                audio_failure = data.get("audio_probe_failure")
                if audio_failure:
                    msg_lines.append(f"  {audio_failure}")
                elif packages.get("sounddevice") == "present" and packages.get("input_device") != "present":
                    msg_lines.append(f"  {_('No microphone input devices detected.')}")

    probe_warnings = data.get("probe_warnings")
    if isinstance(probe_warnings, list):
        for warning in probe_warnings:
            if warning:
                msg_lines.append(f"\nWarning: {warning}")

    return "\n".join(msg_lines)


def _format_self_check_success(data: dict[str, Any]) -> str:
    data = dict(data)
    data.setdefault("vector_search", list(_VECTOR_SEARCH_PACKAGE_KEYS))
    data.setdefault("vision", list(_VISION_PACKAGE_KEYS))
    data.setdefault("audio", list(_AUDIO_PACKAGE_KEYS))
    data.setdefault("nlp", list(_NLP_PACKAGE_KEYS))
    data.setdefault("data_eng", list(_SANDBOX_SELF_CHECK_GROUPS[6][1]))
    return _build_probe_display(
        data,
        completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
        include_vector_search=True,
        include_vision=True,
        include_audio=True,
    )


def run_venv_self_check_with_progress(
    python_exe: str,
    on_display: Callable[[str], None],
    timeout: float | None = None,
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
) -> Tuple[bool, str]:
    """Like :func:`run_venv_self_check` but refreshes the legacy grouped view through *on_display*."""
    from plugin.scripting.venv_worker import PythonWorkerManager

    del timeout  # Per-import probes use SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC, not scripting.python_exec_timeout.
    per_pkg_timeout = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC

    def _status(text: str) -> None:
        if on_status is not None:
            on_status(text)

    def _refresh(
        data: dict[str, Any],
        *,
        completed_groups: int = 0,
        partial_group_keys: tuple[str, ...] | None = None,
        partial_group_title: str | None = None,
        include_vector_search: bool = False,
        include_vision: bool = False,
        include_audio: bool = False,
    ) -> None:
        on_display(
            _build_probe_display(
                data,
                completed_groups=completed_groups,
                partial_group_keys=partial_group_keys,
                partial_group_title=partial_group_title,
                extra_lines_after_header=extra_lines_after_header,
                include_vector_search=include_vector_search,
                include_vision=include_vision,
                include_audio=include_audio,
            )
        )

    def _record_probe_warning(data: dict[str, Any], pkg: str, message: str) -> None:
        warnings = data.setdefault("probe_warnings", [])
        if isinstance(warnings, list):
            warnings.append(f"{pkg}: {message}")

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
        "sci": list(_SANDBOX_SELF_CHECK_GROUPS[0][1]),
        "eda": list(_SANDBOX_SELF_CHECK_GROUPS[1][1]),
        "ui": list(_SANDBOX_SELF_CHECK_GROUPS[2][1]),
        "viz": list(_SANDBOX_SELF_CHECK_GROUPS[3][1]),
        "cas": list(_SANDBOX_SELF_CHECK_GROUPS[4][1]),
        "quant": list(_SANDBOX_SELF_CHECK_GROUPS[5][1]),
        "data_eng": list(_SANDBOX_SELF_CHECK_GROUPS[6][1]),
        "nlp": list(_NLP_PACKAGE_KEYS),
    }

    _status(_("Audio Recording: checking sounddevice..."))
    audio_probes, audio_failure = _probe_audio_packages(
        python_exe,
        timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and audio_probes:
        packages.update(audio_probes)
    data["audio"] = list(_AUDIO_PACKAGE_KEYS)
    if audio_failure:
        data["audio_probe_failure"] = audio_failure
    _refresh(data, include_audio=True)

    for group_index, (group_title, packages) in enumerate(_SANDBOX_SELF_CHECK_GROUPS):
        checked: list[str] = []
        for pkg in packages:
            _status(f"{group_title}: {pkg}")
            try:
                pkg_resp = manager.execute(_package_probe_script(pkg), timeout_sec=per_pkg_timeout)
            except OSError as e:
                return False, f"Could not run Python: {e}"
            if pkg_resp.get("status") != "ok":
                msg = str(pkg_resp.get("message", "Unknown error"))
                log.warning("Package probe failed for %s: %s", pkg, msg)
                _record_probe_warning(data, pkg, msg)
                data["p"][pkg] = None
                checked.append(pkg)
                _refresh(
                    data,
                    completed_groups=group_index,
                    partial_group_keys=tuple(checked),
                    partial_group_title=group_title,
                    include_audio=True,
                )
                continue
            present = pkg_resp.get("result") == "present"
            data["p"][pkg] = "present" if present else None
            checked.append(pkg)
            _refresh(
                data,
                completed_groups=group_index,
                partial_group_keys=tuple(checked),
                partial_group_title=group_title,
                include_audio=True,
            )
        _refresh(data, completed_groups=group_index + 1, include_audio=True)

    _status(_("Text / NLP Libraries: loading (first run may take a while)..."))
    nlp_probes, nlp_failure = _probe_nlp_packages(
        python_exe,
        timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and nlp_probes:
        packages.update(nlp_probes)
    if nlp_failure:
        data["nlp_probe_failure"] = nlp_failure
    _refresh(data, completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT, include_audio=True)

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
    _refresh(
        data,
        completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
        include_vision=True,
        include_audio=True,
    )

    _status(_("Vector Search Libraries: loading (first run may take a while)..."))
    vector_search_probes, vector_search_failure = _probe_vector_search_packages(
        python_exe,
        timeout=float(VECTOR_SEARCH_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and vector_search_probes:
        packages.update(vector_search_probes)
    data["vector_search"] = list(_VECTOR_SEARCH_PACKAGE_KEYS)
    if vector_search_failure:
        data["vector_search_probe_failure"] = vector_search_failure
    _refresh(
        data,
        completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
        include_vector_search=True,
        include_vision=True,
        include_audio=True,
    )

    try:
        final_msg = _build_probe_display(
            data,
            completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
            include_vector_search=True,
            include_vision=True,
            include_audio=True,
            extra_lines_after_header=extra_lines_after_header,
        )
        on_display(final_msg)
        return True, final_msg
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def run_venv_self_check(python_exe: str, timeout: float | None = None) -> Tuple[bool, str]:
    """Run a diagnostic script via the warm worker; return (success, user-facing message)."""
    from plugin.scripting.venv_worker import PythonWorkerManager

    timeout_sec = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC if timeout is None else max(1, int(timeout))
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

    audio_probes, audio_failure = _probe_audio_packages(
        python_exe,
        timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and audio_probes:
        packages.update(audio_probes)
    data["audio"] = list(_AUDIO_PACKAGE_KEYS)
    if audio_failure:
        data["audio_probe_failure"] = audio_failure

    nlp_probes, nlp_failure = _probe_nlp_packages(
        python_exe,
        timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and nlp_probes:
        packages.update(nlp_probes)
    data["nlp"] = list(_NLP_PACKAGE_KEYS)
    if nlp_failure:
        data["nlp_probe_failure"] = nlp_failure

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

    vector_search_probes, vector_search_failure = _probe_vector_search_packages(
        python_exe,
        timeout=float(VECTOR_SEARCH_PROBE_TIMEOUT_SEC),
    )
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and vector_search_probes:
        packages.update(vector_search_probes)
    data["vector_search"] = list(_VECTOR_SEARCH_PACKAGE_KEYS)
    if vector_search_failure:
        data["vector_search_probe_failure"] = vector_search_failure

    try:
        return True, _format_self_check_success(data)
    except Exception as e:
        return False, f"Failed to parse diagnostic output: {e}\nRaw output: {data!r}"


def probe_venv_path(venv_dir: str, timeout: float | None = None) -> Tuple[bool, str]:
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
    timeout: float | None = None,
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
