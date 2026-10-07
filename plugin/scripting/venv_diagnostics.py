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
import sys
from typing import Any, Callable, Optional, Tuple

from plugin.framework.i18n import _
from plugin.scripting.config_limits import (
    SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
    VECTOR_SEARCH_PROBE_TIMEOUT_SEC,
    VISION_PROBE_TIMEOUT_SEC,
)
from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.sandbox import resolve_libreoffice_python, resolve_venv_python, scrub_subprocess_env, wrap_command_for_sandbox

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

# Install hints use uv (recommended).
# Users point Settings → Python at a venv created with `uv venv` and populated via `uv pip`.
_QUANT_INSTALL_CMD = "uv pip install yfinance pandas-ta quantstats pyportfolioopt"

# Vision stack (docs/images/recognition.md §7–§13): probed outside the AST sandbox because
# docling/paddleocr/paddle are not whitelisted for LLM-submitted venv scripts.
# Primary OCR: docling + rapidocr-paddle. Fallback: paddleocr + paddle.
# Optional: ultralytics (detection helpers), skimage (trusted helper preprocessing).
_ANALYSIS_INSTALL_CMD = (
    "uv pip install numpy pandas scipy scikit-learn statsmodels fg-data-profiling pandas-montecarlo"
)
_VISION_PACKAGE_KEYS = ("docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage")
# Primary Docling OCR keys shown under Missing (OCR) when the stack is not ready.
_VISION_OCR_PRIMARY_KEYS = ("docling", "rapidocr", "css_inline")
# Always optional in Test output (never required Missing once OCR readiness is decided).
_VISION_OPTIONAL_KEYS = ("paddleocr", "paddle", "ultralytics", "skimage")
_DOCLING_INSTALL_CMD = "uv pip install docling rapidocr-paddle numpy pillow css-inline onnxruntime"
_VISION_PADDLE_FALLBACK_CMD = "uv pip install paddleocr paddlepaddle numpy"
_VIZ_INSTALL_CMD = "uv pip install matplotlib seaborn"
_SYMBOLIC_INSTALL_CMD = "uv pip install sympy"
_AUDIO_PACKAGE_KEYS = (
    "sounddevice",
    "input_device",
    "kokoro_onnx",
    "soundfile",
    "piper",
    "faster_whisper",
)
# sounddevice is required for Record. Local TTS and local Whisper are optional
# and each missing set gets its own install line.
_TTS_OPTIONAL_KEYS = ("kokoro_onnx", "soundfile", "piper")
_AUDIO_OPTIONAL_KEYS = _TTS_OPTIONAL_KEYS + ("faster_whisper",)
_AUDIO_INSTALL_CMD = "uv pip install sounddevice"
# Kokoro multilingual extras match plugin/audio/kokoro_g2p.py KOKORO_PIP_INSTALL.
# piper-tts stays in this hint; do not import plugin.audio from LibrePy diagnostics.
_TTS_INSTALL_CMD = (
    "uv pip install kokoro-onnx soundfile piper-tts 'misaki[ja,zh]' phonemizer-fork espeakng-loader"
)
# Separate from the TTS recipe so a missing Whisper package does not print
# the Kokoro/Piper install line (and the reverse).
_WHISPER_INSTALL_CMD = 'uv pip install faster-whisper "av<14"'


def _audio_linux_portaudio_hint() -> str:
    # Translate when shown. Module-level _() runs at import, often before
    # init_i18n(ctx), and would store the startup-locale string for the process.
    return _("On Linux also install system PortAudio: sudo pacman -S portaudio")


# Hardware / non-PyPI probe keys must not appear in the copy-paste install footer.
_NON_PIP_PROBE_KEYS = frozenset({"input_device"})
# Probe import/key name → PyPI package name for the global install footer.
_PROBE_KEY_TO_PIP: dict[str, str] = {
    "sklearn": "scikit-learn",
    "data_profiling": "fg-data-profiling",
    "pandas_montecarlo": "pandas-montecarlo",
    "pandas_ta": "pandas-ta",
    "pypfopt": "pyportfolioopt",
    "PyQt6.QtWebEngineWidgets": "PyQt6-WebEngine",
    "rapidocr": "rapidocr-paddle",
    "css_inline": "css-inline",
    "language_tool_python": "language-tool-python",
    "python_docx": "python-docx",
    "sentence_transformers": "sentence-transformers",
    "sqlite_vec": "sqlite-vec",
    "langchain_core": "langchain-core",
    "langchain_text_splitters": "langchain-text-splitters",
    "paddle": "paddlepaddle",
    "kokoro_onnx": "kokoro-onnx",
    "piper": "piper-tts",
    "faster_whisper": "faster-whisper",
}
_AUDIO_PROBE_SCRIPT = """
import json
out = {}
try:
    import sounddevice as sd
    out["sounddevice"] = "present"
except Exception:
    sd = None
    out["sounddevice"] = None
    out["input_device"] = None
else:
    # PortAudio / device enumeration can fail when the package itself imported.
    # Do not report that as "sounddevice is not installed".
    try:
        devices = sd.query_devices()
        has_input = any(d.get("max_input_channels", 0) > 0 for d in devices)
        out["input_device"] = "present" if has_input else None
    except Exception:
        out["input_device"] = None

try:
    import kokoro_onnx
    out["kokoro_onnx"] = "present"
except Exception:
    out["kokoro_onnx"] = None

try:
    import soundfile
    out["soundfile"] = "present"
except Exception:
    out["soundfile"] = None

try:
    import piper
    out["piper"] = "present"
except Exception:
    out["piper"] = None

try:
    import faster_whisper
    out["faster_whisper"] = "present"
except Exception:
    out["faster_whisper"] = None

print(json.dumps(out))
"""
def _audio_probe_timeout_hint() -> str:
    return _("Audio probe timed out (sounddevice import failed or hung).")


def _audio_probe_failed_hint() -> str:
    return _("Audio probe failed (see writeragent_debug.log).")


_TEXT_ANALYTICS_INSTALL_CMD = "uv pip install spacy textdescriptives transformers language-tool-python torch --index-url https://download.pytorch.org/whl/cpu && python -m spacy download xx_sent_ud_sm"
_NLP_PACKAGE_KEYS = ("spacy", "textdescriptives", "transformers", "language_tool_python")
_NLP_OPTIONAL_KEYS = ("language_tool_python",)
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

def _nlp_probe_timeout_hint() -> str:
    return _(
        "Text/NLP probe timed out (spaCy or transformers cold import can take 10–30s on first check)."
    )


def _nlp_probe_failed_hint() -> str:
    return _("Text/NLP probe failed (see writeragent_debug.log).")


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


def _vision_probe_timeout_hint() -> str:
    return _(
        "Vision probe timed out (Docling import can take 10–30s on first check)."
    )


def _vision_probe_failed_hint() -> str:
    return _("Vision probe failed (see writeragent_debug.log).")

# Vector Search stack: probed outside the AST sandbox (WriterAgent embeddings only).
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

def _vector_search_probe_timeout_hint() -> str:
    return _(
        "Vector Search probe timed out (sentence-transformers import can take 10–30s on first check)."
    )


def _vector_search_probe_failed_hint() -> str:
    return _("Vector Search probe failed (see writeragent_debug.log).")


def _run_json_probe(
    python_exe: str,
    script: str,
    timeout: float,
    *,
    timeout_hint: str,
    fail_hint: str,
    log_label: str,
    floor_timeout: bool = True,
) -> tuple[dict[str, Any], str | None]:
    """Run a one-shot ``python -c`` JSON probe outside the warm worker."""
    limit = max(1.0, timeout) if floor_timeout else timeout
    try:
        proc = subprocess.run(
            wrap_command_for_sandbox([python_exe, "-c", script]),
            capture_output=True,
            text=True,
            timeout=limit,
            env=scrub_subprocess_env(dict(os.environ)),
            **get_subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return {}, timeout_hint
    except OSError as exc:
        log.warning("%s package probe could not run: %s", log_label, exc)
        return {}, fail_hint
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        log.warning("%s package probe exit %s: %s", log_label, proc.returncode, stderr)
        return {}, fail_hint
    try:
        parsed = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("%s package probe returned invalid JSON: %r", log_label, (proc.stdout or "")[:200])
        return {}, fail_hint
    if not isinstance(parsed, dict):
        return {}, fail_hint
    return parsed, None


def _probe_nlp_packages(
    python_exe: str,
    timeout: float = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check Text/NLP stack in the real venv interpreter (not the sandboxed warm worker)."""
    return _run_json_probe(
        python_exe,
        _NLP_PROBE_SCRIPT,
        timeout,
        timeout_hint=_nlp_probe_timeout_hint(),
        fail_hint=_nlp_probe_failed_hint(),
        log_label="Text/NLP",
    )


def _probe_vector_search_packages(
    python_exe: str,
    timeout: float = VECTOR_SEARCH_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check embeddings stack in the real venv interpreter (not the sandboxed warm worker)."""
    return _run_json_probe(
        python_exe,
        _VECTOR_SEARCH_PROBE_SCRIPT,
        timeout,
        timeout_hint=_vector_search_probe_timeout_hint(),
        fail_hint=_vector_search_probe_failed_hint(),
        log_label="Vector Search",
    )


def probe_vision_packages(
    python_exe: str,
    timeout: float = VISION_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check vision stack in the real venv interpreter (not the sandboxed warm worker)."""
    return _run_json_probe(
        python_exe,
        _VISION_PROBE_SCRIPT,
        timeout,
        timeout_hint=_vision_probe_timeout_hint(),
        fail_hint=_vision_probe_failed_hint(),
        log_label="Vision",
    )


def _probe_audio_packages(
    python_exe: str,
    timeout: float = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
) -> tuple[dict[str, str | None], str | None]:
    # Audio keeps the caller timeout as-is (no 1s floor). The other probes floor it.
    return _run_json_probe(
        python_exe,
        _AUDIO_PROBE_SCRIPT,
        timeout,
        timeout_hint=_audio_probe_timeout_hint(),
        fail_hint=_audio_probe_failed_hint(),
        log_label="Audio",
        floor_timeout=False,
    )


# Editor GUI stacks. Probed in a one-shot subprocess so Qt/WebEngine never
# land in the warm worker that later runs =PY() and Run Python Script.
_UI_PACKAGE_KEYS = ("webview", "rocher", "jedi", "PyQt6", "PyQt6.QtWebEngineWidgets", "qtpy")
_UI_PROBE_SCRIPT = """
import json
out = {}
pairs = (
    ("webview", "webview"),
    ("rocher", "rocher"),
    ("jedi", "jedi"),
    ("PyQt6", "PyQt6"),
    ("PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineWidgets"),
    ("qtpy", "qtpy"),
)
for key, mod in pairs:
    try:
        __import__(mod)
        out[key] = "present"
    except Exception:
        out[key] = None
print(json.dumps(out))
"""


def _ui_probe_failed_hint() -> str:
    return _("UI / Monaco probe failed (see writeragent_debug.log).")


def _probe_ui_packages(
    python_exe: str,
    timeout: float = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check editor GUI packages outside the sandboxed warm worker."""
    return _run_json_probe(
        python_exe,
        _UI_PROBE_SCRIPT,
        timeout,
        timeout_hint=_("UI / Monaco probe timed out."),
        fail_hint=_ui_probe_failed_hint(),
        log_label="UI",
    )


def _duckdb_probe_failed_hint() -> str:
    return _("DuckDB probe failed (see writeragent_debug.log).")


# Not inside _DIAGNOSTIC_SCRIPT: a sandbox import raises InterpreterError,
# which is not ImportError, so the worker probe fails instead of reporting missing.
_DUCKDB_PROBE_SCRIPT = """
import json
try:
    import duckdb
    out = {"duckdb": "present"}
except Exception as exc:
    out = {"duckdb": None, "duckdb_import_error": str(exc)[:200]}
print(json.dumps(out))
"""


def _probe_duckdb(
    python_exe: str,
    timeout: float = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC,
) -> Tuple[dict[str, Any], Optional[str]]:
    """Import-check duckdb outside the sandboxed warm worker."""
    return _run_json_probe(
        python_exe,
        _DUCKDB_PROBE_SCRIPT,
        timeout,
        timeout_hint=_("DuckDB probe timed out."),
        fail_hint=_duckdb_probe_failed_hint(),
        log_label="DuckDB",
    )


def _merge_duckdb_probe(python_exe: str, data: dict[str, Any], timeout: float) -> None:
    """Set data['p']['duckdb'] from the subprocess probe. Never fails the self-check.

    Appends duckdb to data['data_eng'] when the sandbox group no longer lists it,
    so the Settings install line still renders.
    """
    probes, failure = _probe_duckdb(python_exe, timeout=timeout)
    packages = data.get("p")
    if not isinstance(packages, dict):
        packages = {}
        data["p"] = packages
    if failure:
        packages["duckdb"] = None
    else:
        value = probes.get("duckdb")
        packages["duckdb"] = value if value == "present" else None
    data_eng = data.get("data_eng")
    if not isinstance(data_eng, list):
        data_eng = list(data_eng) if isinstance(data_eng, tuple) else []
        data["data_eng"] = data_eng
    if "duckdb" not in data_eng:
        data_eng.append("duckdb")


_SANDBOX_SELF_CHECK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Scientific Libraries", ("numpy", "pandas", "scipy", "sklearn", "matplotlib", "sympy")),
    ("Data Analysis / EDA Libraries", ("data_profiling", "statsmodels", "pandas_montecarlo")),
    ("UI / Monaco Libraries", _UI_PACKAGE_KEYS),
    ("Visualization Libraries", ("matplotlib", "seaborn")),
    ("Computer Algebra", ("sympy",)),
    ("Quantitative Finance Libraries", ("yfinance", "pandas_ta", "quantstats", "pypfopt")),
    ("Data Engineering Libraries", ("pint",)),
)

# Display order includes NLP (probed via subprocess, not the sandbox worker loop).
_SELF_CHECK_SANDBOX_GROUP_COUNT = len(_SANDBOX_SELF_CHECK_GROUPS)
_SELF_CHECK_DISPLAY_GROUP_COUNT = _SELF_CHECK_SANDBOX_GROUP_COUNT + 1  # + Text / NLP

_ALLOWED_PROBE_MODULES = frozenset(
    pkg for _title, pkgs in _SANDBOX_SELF_CHECK_GROUPS for pkg in pkgs if pkg not in _UI_PACKAGE_KEYS
)

_VERSION_PROBE_SCRIPT = """
import platform
result = {'v': platform.python_version(), 'arch': platform.machine()}
"""


def _package_probe_script(module: str) -> str:
    """Return a sandbox-safe one-import probe script for a whitelisted *module*."""
    if module not in _ALLOWED_PROBE_MODULES:
        raise ValueError(f"unsupported probe module: {module}")
    import_stmt = f"import {module}"
    return f"""
try:
    {import_stmt}
    result = 'present'
except ImportError:
    result = None
"""


def _ocr_backend_ready(probe: dict[str, Any]) -> bool:
    """True when Docling or PaddleOCR+Paddle is present (css-inline checked separately)."""
    if probe.get("docling") == "present":
        return True
    return probe.get("paddleocr") == "present" and probe.get("paddle") == "present"


def vision_ocr_stack_ready(probe: dict[str, Any]) -> bool:
    """True when an OCR backend and css-inline are present (Settings Test / diagnostics)."""
    return _ocr_backend_ready(probe) and probe.get("css_inline") == "present"


def _probe_key_to_pip(key: str) -> str | None:
    """Map a probe key to a PyPI name, or None when the key is not installable via pip."""
    if key in _NON_PIP_PROBE_KEYS:
        return None
    return _PROBE_KEY_TO_PIP.get(key, key)


def _missing_keys(keys: tuple[str, ...] | list[str], packages: dict[str, Any]) -> list[str]:
    return [key for key in keys if packages.get(key) != "present"]


def _format_group_lines(
    title: str,
    keys: tuple[str, ...] | list[str],
    packages: dict[str, Any],
    optional_keys: tuple[str, ...] | list[str] = (),
) -> list[str]:
    found: list[str] = []
    missing: list[str] = []
    optional_missing: list[str] = []
    for key in keys:
        if packages.get(key) == "present":
            found.append(key)
        elif key in optional_keys:
            optional_missing.append(key)
        else:
            missing.append(key)
    lines: list[str] = []
    if found:
        lines.append(f"\n{title}: {', '.join(found)}")
    else:
        lines.append(f"\n{title}:")
    if missing:
        lines.append(f"Missing: {', '.join(missing)}")
    if optional_missing:
        lines.append(f"Optional (not installed): {', '.join(optional_missing)}")
    return lines


def _format_vision_group_lines(title: str, keys: tuple[str, ...] | list[str], packages: dict[str, Any]) -> list[str]:
    """Present/OCR-ready/Optional formatting — paddle/ultralytics/skimage are never required Missing."""
    present = [key for key in keys if packages.get(key) == "present"]
    lines: list[str] = []
    if present:
        lines.append(f"\n{title}: {', '.join(present)}")
    else:
        lines.append(f"\n{title}:")

    if vision_ocr_stack_ready(packages):
        engine = "Docling" if packages.get("docling") == "present" else "Paddle"
        lines.append(f"OCR: ready ({engine})")
        optional_missing: list[str] = []
        for key in _VISION_OPTIONAL_KEYS:
            if packages.get(key) != "present":
                optional_missing.append(key)
        # Paddle-only readiness: Docling primary packages are optional, not Missing.
        if packages.get("docling") != "present":
            for key in ("docling", "rapidocr"):
                if packages.get(key) != "present" and key not in optional_missing:
                    optional_missing.append(key)
        if optional_missing:
            lines.append(f"Optional (not installed): {', '.join(optional_missing)}")
    else:
        lines.append("OCR: not ready")
        primary_missing = [key for key in _VISION_OCR_PRIMARY_KEYS if packages.get(key) != "present"]
        if primary_missing:
            lines.append(f"Missing (OCR): {', '.join(primary_missing)}")
        optional_missing = [key for key in _VISION_OPTIONAL_KEYS if packages.get(key) != "present"]
        if optional_missing:
            lines.append(f"Optional (not installed): {', '.join(optional_missing)}")
    return lines


def _self_check_group_specs(data: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (_("Audio Recording & Speech"), tuple(data.get("audio", ()))),
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


def _collect_missing_probe_keys_for_display(
    data: dict[str, Any],
    *,
    completed_groups: int,
    partial_group_keys: tuple[str, ...] | None,
    partial_group_title: str | None,
    include_vector_search: bool,
    include_vision: bool,
    include_audio: bool,
) -> list[str]:
    """Probe keys that appear under Missing in the rendered Test body (stable group order)."""
    packages = data.get("p", {})
    if not isinstance(packages, dict):
        packages = {}
    missing: list[str] = []
    seen: set[str] = set()

    def _add(keys: list[str]) -> None:
        for key in keys:
            if key in _NON_PIP_PROBE_KEYS or key in seen:
                continue
            seen.add(key)
            missing.append(key)

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
    for title, keys in specs:
        if not keys:
            continue
        if title in sandbox_titles:
            s_idx = sandbox_titles.index(title)
            if s_idx < completed_groups:
                _add(_missing_keys(keys, packages))
            elif s_idx == completed_groups and partial_group_keys and partial_group_title == title:
                _add(_missing_keys(partial_group_keys, packages))
        elif title == _("Text / NLP Libraries"):
            if completed_groups >= _SELF_CHECK_DISPLAY_GROUP_COUNT:
                _add(_missing_keys([k for k in keys if k not in _NLP_OPTIONAL_KEYS], packages))
        elif title == _("Vision Libraries"):
            if include_vision:
                if not vision_ocr_stack_ready(packages):
                    _add([key for key in _VISION_OCR_PRIMARY_KEYS if packages.get(key) != "present"])
        elif title == _("Vector Search Libraries"):
            if include_vector_search:
                _add(_missing_keys(keys, packages))
        elif title in (_("Audio Recording"), _("Audio Recording & Speech")):
            if include_audio:
                _add(_missing_keys([k for k in keys if k not in _AUDIO_OPTIONAL_KEYS], packages))
    return missing


def _format_install_footer(missing_probe_keys: list[str]) -> list[str]:
    """Copy-paste uv then pip lines for remaining Missing packages; empty when nothing to install."""
    pip_names: list[str] = []
    seen_pip: set[str] = set()
    for key in missing_probe_keys:
        pip_name = _probe_key_to_pip(key)
        if not pip_name or pip_name in seen_pip:
            continue
        seen_pip.add(pip_name)
        pip_names.append(pip_name)
    if not pip_names:
        return []

    # Docling's RapidOCR path imports onnxruntime; it is not a separate probe key.
    if any(key in missing_probe_keys for key in ("docling", "rapidocr")) and "onnxruntime" not in seen_pip:
        pip_names.append("onnxruntime")

    pkg_args = " ".join(pip_names)
    lines = [
        "",
        _("To install remaining packages:"),
        f"uv pip install {pkg_args}",
        f"pip install {pkg_args}",
    ]
    # NLP often needs the PyTorch CPU index + a spaCy model after the main packages.
    nlp_need_extras = any(
        key in missing_probe_keys for key in ("spacy", "textdescriptives", "transformers")
    )
    if nlp_need_extras:
        lines.append("uv pip install torch --index-url https://download.pytorch.org/whl/cpu")
        lines.append("pip install torch --index-url https://download.pytorch.org/whl/cpu")
        if "spacy" in missing_probe_keys:
            lines.append("python -m spacy download xx_sent_ud_sm")
    return lines


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
    include_install_footer: bool = False,
) -> str:
    """Rebuild the Settings → Python Test body in the legacy grouped Present/Missing format."""
    version = data.get("v", "unknown")
    arch = data.get("arch", "")
    packages = data.get("p", {})
    if not isinstance(packages, dict):
        packages = {}
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
    for _idx, (title, keys) in enumerate(specs):
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
                msg_lines.extend(_format_group_lines(title, keys, packages, optional_keys=_NLP_OPTIONAL_KEYS))
                nlp_failure = data.get("nlp_probe_failure")
                if nlp_failure:
                    msg_lines.append(f"  {nlp_failure}")
        elif title == _("Vision Libraries"):
            if include_vision:
                msg_lines.extend(_format_vision_group_lines(title, keys, packages))
                vision_failure = data.get("vision_probe_failure")
                if vision_failure:
                    msg_lines.append(f"  {vision_failure}")
        elif title == _("Vector Search Libraries"):
            if include_vector_search:
                msg_lines.extend(_format_group_lines(title, keys, packages))
                vector_search_failure = data.get("vector_search_probe_failure")
                if vector_search_failure:
                    msg_lines.append(f"  {vector_search_failure}")
        elif title in (_("Audio Recording"), _("Audio Recording & Speech")):
            if include_audio:
                msg_lines.extend(_format_group_lines(title, keys, packages, optional_keys=_AUDIO_OPTIONAL_KEYS))
                audio_failure = data.get("audio_probe_failure")
                if audio_failure:
                    msg_lines.append(f"  {audio_failure}")
                elif sys.platform.startswith("linux") and packages.get("sounddevice") != "present":
                    msg_lines.append(f"  {_audio_linux_portaudio_hint()}")
                elif packages.get("sounddevice") == "present" and packages.get("input_device") != "present":
                    msg_lines.append(f"  {_('No microphone input devices detected.')}")
                tts_missing = [k for k in _TTS_OPTIONAL_KEYS if packages.get(k) != "present"]
                if tts_missing and include_install_footer:
                    msg_lines.append(f"  {_('Local TTS engines (optional):')} {_TTS_INSTALL_CMD}")
                if (
                    include_install_footer
                    and "faster_whisper" in keys
                    and packages.get("faster_whisper") != "present"
                ):
                    msg_lines.append(f"  {_('Local Whisper (optional):')} {_WHISPER_INSTALL_CMD}")

    probe_warnings = data.get("probe_warnings")
    if isinstance(probe_warnings, list):
        for warning in probe_warnings:
            if warning:
                msg_lines.append(f"\nWarning: {warning}")

    # Progressive _refresh must not show a partial install recipe; only the final display does.
    if include_install_footer:
        missing_keys = _collect_missing_probe_keys_for_display(
            data,
            completed_groups=completed_groups,
            partial_group_keys=partial_group_keys,
            partial_group_title=partial_group_title,
            include_vector_search=include_vector_search,
            include_vision=include_vision,
            include_audio=include_audio,
        )
        msg_lines.extend(_format_install_footer(missing_keys))

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
        include_install_footer=True,
    )


def _attach_probe_group(
    data: dict[str, Any],
    probes: dict[str, Any] | None,
    failure: str | None,
    *,
    group: str,
    keys: tuple[str, ...] | list[str],
    failure_key: str,
) -> None:
    """Merge one out-of-worker probe into *data*. Shared by both self-check entry points."""
    packages = data.setdefault("p", {})
    if isinstance(packages, dict) and probes:
        packages.update(probes)
    data[group] = list(keys)
    if failure:
        data[failure_key] = failure


def _attach_external_probes(
    python_exe: str,
    data: dict[str, Any],
    *,
    include_audio: bool = True,
    include_ui: bool = True,
    include_nlp: bool = True,
    include_vision: bool = True,
    include_vector_search: bool = True,
) -> None:
    """Audio, UI, NLP, vision, and vector probes. Not the warm =PY() worker."""
    if include_audio:
        probes, failure = _probe_audio_packages(
            python_exe,
            timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
        )
        _attach_probe_group(
            data, probes, failure, group="audio", keys=_AUDIO_PACKAGE_KEYS, failure_key="audio_probe_failure"
        )
    if include_ui:
        probes, failure = _probe_ui_packages(
            python_exe,
            timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
        )
        _attach_probe_group(
            data, probes, failure, group="ui", keys=list(_SANDBOX_SELF_CHECK_GROUPS[2][1]), failure_key="ui_probe_failure"
        )
    if include_nlp:
        probes, failure = _probe_nlp_packages(
            python_exe,
            timeout=float(SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC),
        )
        _attach_probe_group(
            data, probes, failure, group="nlp", keys=_NLP_PACKAGE_KEYS, failure_key="nlp_probe_failure"
        )
    if include_vision:
        probes, failure = probe_vision_packages(
            python_exe,
            timeout=float(VISION_PROBE_TIMEOUT_SEC),
        )
        _attach_probe_group(
            data, probes, failure, group="vision", keys=_VISION_PACKAGE_KEYS, failure_key="vision_probe_failure"
        )
    if include_vector_search:
        probes, failure = _probe_vector_search_packages(
            python_exe,
            timeout=float(VECTOR_SEARCH_PROBE_TIMEOUT_SEC),
        )
        _attach_probe_group(
            data,
            probes,
            failure,
            group="vector_search",
            keys=_VECTOR_SEARCH_PACKAGE_KEYS,
            failure_key="vector_search_probe_failure",
        )


def run_venv_self_check_with_progress(
    python_exe: str,
    on_display: Callable[[str], None],
    timeout: float | None = None,
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    *,
    include_vector_search: bool = True,
    include_audio: bool = True,
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
        from plugin.framework.constants import WORKER_POOL_DIAGNOSTICS

        manager = PythonWorkerManager.get(
            python_exe,
            scrub_subprocess_env(dict(os.environ)),
            pool=WORKER_POOL_DIAGNOSTICS,
        )
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

    if include_audio:
        _status(_("Audio & Speech: checking sounddevice, TTS, and local Whisper..."))
        _attach_external_probes(
            python_exe,
            data,
            include_ui=False,
            include_nlp=False,
            include_vision=False,
            include_vector_search=False,
        )
        _refresh(data, include_audio=True)

    for group_index, (group_title, packages) in enumerate(_SANDBOX_SELF_CHECK_GROUPS):
        if group_title == "UI / Monaco Libraries":
            # Qt/WebEngine in the warm worker would stay loaded for later =PY() cells.
            _status(_("UI / Monaco Libraries: checking outside the script worker..."))
            ui_probes, ui_failure = _probe_ui_packages(python_exe, timeout=float(per_pkg_timeout))
            packages_map = data.setdefault("p", {})
            if isinstance(packages_map, dict) and ui_probes:
                packages_map.update(ui_probes)
            if ui_failure:
                data["ui_probe_failure"] = ui_failure
                for pkg in packages:
                    if isinstance(packages_map, dict):
                        packages_map.setdefault(pkg, None)
            _refresh(data, completed_groups=group_index + 1, include_audio=include_audio)
            continue
        checked: list[str] = []
        packages_raw = data.get("p")
        packages_seen: dict[Any, Any] = packages_raw if isinstance(packages_raw, dict) else {}
        for pkg in packages:
            _status(f"{group_title}: {pkg}")
            # sympy and matplotlib are listed in more than one group.
            if pkg in packages_seen:
                checked.append(pkg)
                _refresh(
                    data,
                    completed_groups=group_index,
                    partial_group_keys=tuple(checked),
                    partial_group_title=group_title,
                    include_audio=include_audio,
                )
                continue
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
                    include_audio=include_audio,
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
                include_audio=include_audio,
            )
        if group_title == "Data Engineering Libraries":
            _merge_duckdb_probe(python_exe, data, timeout=float(per_pkg_timeout))
        _refresh(data, completed_groups=group_index + 1, include_audio=include_audio)

    _status(_("Text / NLP Libraries: loading (first run may take a while)..."))
    _attach_external_probes(
        python_exe,
        data,
        include_audio=False,
        include_ui=False,
        include_vision=False,
        include_vector_search=False,
    )
    _refresh(data, completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT, include_audio=include_audio)

    _status(_("Vision Libraries: loading (first run may take a while)..."))
    _attach_external_probes(
        python_exe,
        data,
        include_audio=False,
        include_ui=False,
        include_nlp=False,
        include_vector_search=False,
    )
    _refresh(
        data,
        completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
        include_vision=True,
        include_audio=include_audio,
    )

    if include_vector_search:
        _status(_("Vector Search Libraries: loading (first run may take a while)..."))
        _attach_external_probes(
            python_exe,
            data,
            include_audio=False,
            include_ui=False,
            include_nlp=False,
            include_vision=False,
        )
        _refresh(
            data,
            completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
            include_vector_search=True,
            include_vision=True,
            include_audio=include_audio,
        )

    try:
        final_msg = _build_probe_display(
            data,
            completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
            include_vector_search=include_vector_search,
            include_vision=True,
            include_audio=include_audio,
            include_install_footer=True,
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
        from plugin.framework.constants import WORKER_POOL_DIAGNOSTICS

        manager = PythonWorkerManager.get(
            python_exe,
            scrub_subprocess_env(dict(os.environ)),
            pool=WORKER_POOL_DIAGNOSTICS,
        )
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

    _merge_duckdb_probe(python_exe, data, timeout=float(timeout_sec))
    _attach_external_probes(python_exe, data)

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
                "No python found. Use the venv root (folder containing bin/ or Scripts/), "
                "the bin/ or Scripts/ folder, env-root python.exe (conda/pyenv-win), "
                "or the full path to the interpreter."
            )
        return False, f"Path not found: {expanded}"
    return run_venv_self_check(exe, timeout=timeout)


def probe_venv_path_with_progress(
    venv_dir: str,
    on_display: Callable[[str], None],
    timeout: float | None = None,
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    *,
    include_vector_search: bool = True,
    include_audio: bool = True,
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
            include_vector_search=include_vector_search,
            include_audio=include_audio,
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
                "No python found. Use the venv root (folder containing bin/ or Scripts/), "
                "the bin/ or Scripts/ folder, env-root python.exe (conda/pyenv-win), "
                "or the full path to the interpreter."
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
        include_vector_search=include_vector_search,
        include_audio=include_audio,
    )
