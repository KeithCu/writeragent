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

# Vision stack (docs/images/recognition.md §7–§13): probed outside the AST sandbox because
# docling/paddleocr/paddle are not whitelisted for LLM-submitted venv scripts.
# Primary OCR: docling + rapidocr-paddle. Fallback: paddleocr + paddle.
# Optional: ultralytics (detection helpers), skimage (trusted helper preprocessing).
_VISION_PACKAGE_KEYS = ("docling", "rapidocr", "css_inline", "paddleocr", "paddle", "ultralytics", "skimage")
# Primary Docling OCR keys shown under Missing (OCR) when the stack is not ready.
_VISION_OCR_PRIMARY_KEYS = ("docling", "rapidocr", "css_inline")
# Always optional in Test output (never required Missing once OCR readiness is decided).
_VISION_OPTIONAL_KEYS = ("paddleocr", "paddle", "ultralytics", "skimage")
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
# Kokoro multilingual extras match plugin/audio/kokoro_g2p.py KOKORO_PIP_INSTALL.
# Fallback handles standalone LibrePy builds where plugin.audio is excluded.
try:
    from plugin.audio.kokoro_g2p import TTS_INSTALL_CMD
except ImportError:
    TTS_INSTALL_CMD = (
        "uv pip install kokoro-onnx soundfile piper-tts 'misaki[ja,zh]' phonemizer-fork espeakng-loader"
    )
# Separate from the TTS recipe so a missing Whisper package does not print
# the Kokoro/Piper install line (and the reverse).
_WHISPER_INSTALL_CMD = 'uv pip install faster-whisper "av<14"'


def _audio_linux_portaudio_hint() -> str:
    # What was wrong: every Linux user was told `sudo pacman -S portaudio`.
    # How: d22842c51 hard-coded the maintainer's package manager.
    # Why: Test runs on more than Arch, including Flatpak, and a wrong
    # copy-paste command is worse than a generic sentence. Call-time _()
    # because module-level _() runs at import, often before init_i18n(ctx).
    # Do not call venv/audio_recorder.portaudio_hint (apt-only, untranslated).
    return _(
        "On Linux also install the system PortAudio package from your distribution "
        "(sounddevice needs it; the package name differs)."
    )


# Hardware / non-PyPI probe keys must not appear in the copy-paste install footer.
_NON_PIP_PROBE_KEYS = frozenset({"input_device"})
# Probe import/key name → PyPI package name for the global install footer.
_PROBE_KEY_TO_PIP: dict[str, str] = {
    "sklearn": "scikit-learn",
    # What was wrong: a missing editor stack put `webview` on the uv/pip footer.
    # How: _probe_key_to_pip falls back to the import key, and this map had no
    # webview entry. The module is webview; the PyPI name is pywebview
    # (editor_host.probe_webview_import, `uv pip install pywebview rocher`).
    # Why: the footer is copy-paste. `pip install webview` does not install it.
    "webview": "pywebview",
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

# Child stdout marker. _parse_probe_stdout searches for this prefix
# anywhere on a line. A C extension can write a banner with no trailing
# newline, so the probe print is glued to that banner and the line does
# not start with the prefix. Parse from the prefix to the end of the
# line. The six probe scripts below are built by _import_probe_script,
# which emits print("WA_PROBE_JSON " + json.dumps(out)) (concatenation,
# trailing space on the prefix unchanged).
_PROBE_JSON_PREFIX = "WA_PROBE_JSON "


def _import_try_block(key: str, module: str, *, record_error: bool = False) -> str:
    """One child try/except for one import. Source is plain Python (no f-string, walrus, or match)."""
    lines = [
        "try:",
        f"    import {module}",
        f'    out["{key}"] = "present"',
        "except Exception as exc:" if record_error else "except Exception:",
        f'    out["{key}"] = None',
    ]
    if record_error:
        lines.append(f'    out["{key}_import_error"] = str(exc)[:200]')
    return "\n".join(lines) + "\n"


def _import_probe_script(*chunks: str) -> str:
    """Wrap probe chunks with json header and the WA_PROBE_JSON print.

    Each chunk is either one _import_try_block or a hand-written snippet that
    already has its own try/except. The print expression is concatenation, not
    an f-string, so the prefix stays identical to _PROBE_JSON_PREFIX.
    """
    body = "".join(chunks)
    if body and not body.endswith("\n"):
        body += "\n"
    return (
        "import json\n"
        "out = {}\n"
        + body
        + 'print("WA_PROBE_JSON " + json.dumps(out))\n'
    )


_SOUNDDEVICE_PROBE_BODY = """\
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
"""

_AUDIO_PROBE_SCRIPT = _import_probe_script(
    _SOUNDDEVICE_PROBE_BODY,
    _import_try_block("kokoro_onnx", "kokoro_onnx"),
    _import_try_block("soundfile", "soundfile"),
    _import_try_block("piper", "piper"),
    _import_try_block("faster_whisper", "faster_whisper"),
)
def _audio_probe_timeout_hint() -> str:
    return _("Audio probe timed out (sounddevice import failed or hung).")


def _audio_probe_failed_hint() -> str:
    return _("Audio probe failed (see writeragent_debug.log).")


_NLP_PACKAGE_KEYS = ("spacy", "textdescriptives", "transformers", "language_tool_python")
_NLP_OPTIONAL_KEYS = ("language_tool_python",)
_NLP_PROBE_SCRIPT = _import_probe_script(
    *(_import_try_block(name, name) for name in _NLP_PACKAGE_KEYS)
)

def _nlp_probe_timeout_hint() -> str:
    return _(
        "Text/NLP probe timed out (spaCy or transformers cold import can take 10–30s on first check)."
    )


def _nlp_probe_failed_hint() -> str:
    return _("Text/NLP probe failed (see writeragent_debug.log).")


_RAPIDOCR_PROBE_BODY = """\
try:
    import rapidocr
    out["rapidocr"] = "present"
except Exception:
    try:
        import rapidocr_onnxruntime
        out["rapidocr"] = "present"
    except Exception:
        out["rapidocr"] = None
"""

_VISION_PROBE_SCRIPT = _import_probe_script(
    _import_try_block("docling", "docling.document_converter", record_error=True),
    _RAPIDOCR_PROBE_BODY,
    _import_try_block("paddleocr", "paddleocr"),
    _import_try_block("paddle", "paddle"),
    _import_try_block("ultralytics", "ultralytics"),
    _import_try_block("skimage", "skimage"),
    _import_try_block("css_inline", "css_inline"),
)


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
_VECTOR_SEARCH_PROBE_SCRIPT = _import_probe_script(
    _import_try_block("envwrap", "envwrap"),
    _import_try_block("sentence_transformers", "sentence_transformers", record_error=True),
    _import_try_block("sqlite_vec", "sqlite_vec"),
    _import_try_block("zvec", "zvec"),
    _import_try_block("langgraph", "langgraph"),
    _import_try_block("langchain_core", "langchain_core"),
    _import_try_block("langchain_text_splitters", "langchain_text_splitters"),
    _import_try_block("icu4py", "icu4py"),
    _import_try_block("odfpy", "odf"),
    _import_try_block("pandas", "pandas"),
    _import_try_block("openpyxl", "openpyxl"),
    _import_try_block("xlrd", "xlrd"),
    _import_try_block("python_docx", "docx"),
    _import_try_block("langdetect", "langdetect"),
)

def _vector_search_probe_timeout_hint() -> str:
    return _(
        "Vector Search probe timed out (sentence-transformers import can take 10–30s on first check)."
    )


def _vector_search_probe_failed_hint() -> str:
    return _("Vector Search probe failed (see writeragent_debug.log).")


def _parse_probe_stdout(stdout: str) -> dict[str, Any] | None:
    """Return the last ``WA_PROBE_JSON `` object on stdout, or None.

    What was wrong: json.loads of the whole stdout failed when paddle, onnx,
    or docling printed a banner before the probe dict, and the group was
    reported as a failed probe.
    How: probe scripts print one prefixed line. Other lines are ignored.
    The last prefixed dict wins, so a later atexit line that is not the
    marker does not hide the payload.
    Why: a bare JSON line from the library is not a probe result. No marker
    means failure, including empty stdout. Do not fall back to {}.

    What was wrong: startswith(prefix) dropped the payload when the
    banner and the probe print were the same line.
    How: paddle/onnx/docling can write stdout without a newline, then
    the probe print continues that line.
    Why: find(prefix) and json.loads of the remainder still ignores
    unrelated lines, and a non-dict or invalid slice does not win.
    """
    found: dict[str, Any] | None = None
    prefix = _PROBE_JSON_PREFIX
    for line in stdout.splitlines():
        idx = line.find(prefix)
        if idx < 0:
            continue
        payload = line[idx + len(prefix):].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found


def _run_json_probe(
    python_exe: str,
    script: str,
    timeout: float,
    *,
    timeout_hint: str,
    fail_hint: str,
    log_label: str,
) -> tuple[dict[str, Any], str | None]:
    """Run a one-shot ``python -c`` JSON probe outside the warm worker."""
    # subprocess.run rejects timeout <= 0. Callers pass the 30s constants.
    # The floor keeps a zero or negative value on TimeoutExpired.
    limit = max(1.0, float(timeout))
    scrubbed_env = scrub_subprocess_env(dict(os.environ))
    # Child stdout is UTF-8: scrub_subprocess_env sets PYTHONIOENCODING=utf-8
    # and PYTHONUTF8=1. text=True decodes with locale.getpreferredencoding(False).
    # A cp1252 LibreOffice parent then raised UnicodeDecodeError on a
    # non-ASCII banner and aborted the self-check (only TimeoutExpired and
    # OSError were caught). errors="replace" keeps one bad byte from failing
    # the probe. env is passed twice on purpose: wrap_command_for_sandbox
    # turns it into flatpak-spawn --clear-env/--env, and subprocess.run(env=)
    # is what a non-Flatpak spawn uses. flatpak-spawn does not forward Popen env.
    try:
        proc = subprocess.run(
            wrap_command_for_sandbox([python_exe, "-c", script], env=scrubbed_env),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            env=scrubbed_env,
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
    parsed = _parse_probe_stdout(proc.stdout or "")
    if parsed is None:
        log.warning("%s package probe returned invalid JSON: %r", log_label, (proc.stdout or "")[:200])
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
    return _run_json_probe(
        python_exe,
        _AUDIO_PROBE_SCRIPT,
        timeout,
        timeout_hint=_audio_probe_timeout_hint(),
        fail_hint=_audio_probe_failed_hint(),
        log_label="Audio",
    )


# Editor GUI stacks. Probed in a one-shot subprocess so Qt/WebEngine never
# land in the warm worker that later runs =PY() and Run Python Script.
_UI_PACKAGE_KEYS = ("webview", "rocher", "jedi", "PyQt6", "PyQt6.QtWebEngineWidgets", "qtpy")
_UI_PROBE_SCRIPT = _import_probe_script(
    *(_import_try_block(name, name) for name in _UI_PACKAGE_KEYS)
)


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


# Not a _package_probe_script target: duckdb is off the venv AST allowlist.
# A sandbox import raises InterpreterError, which is not ImportError, so the
# worker probe would fail the package instead of reporting it missing.
_DUCKDB_PROBE_SCRIPT = _import_probe_script(
    _import_try_block("duckdb", "duckdb", record_error=True),
)


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


# NOTE for AI agents: _VERSION_PROBE_SCRIPT and _package_probe_script run in a
# sandboxed LocalPythonExecutor. Do NOT use dynamic execution primitives like
# __import__(), eval(), or exec(). The sandbox rejects them with
# InterpreterError. Use an explicit import statement inside try/except ImportError.
# The one-shot subprocess scripts above this note use an explicit import
# inside their own try/except; do not switch them to __import__ or to one
# try around every import.
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


def _docling_ocr_ready(probe: dict[str, Any]) -> bool:
    return probe.get("docling") == "present" and probe.get("rapidocr") == "present"


def _paddle_ocr_ready(probe: dict[str, Any]) -> bool:
    return probe.get("paddleocr") == "present" and probe.get("paddle") == "present"


def _ocr_backend_ready(probe: dict[str, Any]) -> bool:
    """True when Docling+RapidOCR or PaddleOCR+Paddle imported.

    css-inline is not part of this check; vision_ocr_stack_ready adds it.
    What was wrong: docling alone returned True, so docling + css-inline
    without rapidocr was "OCR: ready (Docling)" and rapidocr was omitted
    from Missing and from Optional.
    How: the default ocr_backend is rapidocr
    (plugin/vision/vision_common.py DEFAULT_OCR_BACKEND). _resolve_ocr_options
    raises OcrBackendError when that backend cannot be built.
    Why: the Docling pair now requires rapidocr. PaddleOCR+Paddle is unchanged.
    """
    return _docling_ocr_ready(probe) or _paddle_ocr_ready(probe)


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
        engine = "Docling" if _docling_ocr_ready(packages) else "Paddle"
        lines.append(f"OCR: ready ({engine})")
        optional_missing: list[str] = []
        for key in _VISION_OPTIONAL_KEYS:
            if packages.get(key) != "present":
                optional_missing.append(key)
        # Paddle-only readiness: Docling primary packages are optional, not Missing.
        if not _docling_ocr_ready(packages):
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


def _sandbox_display_titles() -> list[str]:
    """Translated sandbox titles, in `_SANDBOX_SELF_CHECK_GROUPS` order.

    Call-time _(): module-level _() would freeze the catalog from import,
    before init_i18n(ctx). completed_groups is an index into this list.
    """
    return [_(title) for title, _pkgs in _SANDBOX_SELF_CHECK_GROUPS]


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
    sandbox_titles = _sandbox_display_titles()
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
        elif title == _("Audio Recording & Speech"):
            if include_audio:
                _add(_missing_keys([k for k in keys if k not in _AUDIO_OPTIONAL_KEYS], packages))
    return missing


# Install hints use uv (recommended).
# Users point Settings → Python at a venv created with `uv venv` and populated via `uv pip`.
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

    # Docling's RapidOCR path imports onnxruntime; it is not a probe key,
    # so seen_pip cannot already contain it.
    if any(key in missing_probe_keys for key in ("docling", "rapidocr")):
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
    sandbox_titles = _sandbox_display_titles()
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
        elif title == _("Audio Recording & Speech"):
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
                    msg_lines.append(f"  {_('Local TTS engines (optional):')} {TTS_INSTALL_CMD}")
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


def _format_self_check_success(data: dict[str, Any]) -> str:  # pyright: ignore[reportUnusedFunction]
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
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    *,
    include_vector_search: bool = True,
    include_audio: bool = True,
) -> tuple[bool, str]:
    """Probe *python_exe* and refresh the grouped Present/Missing view through *on_display*.

    The version line and allowlisted scientific packages run in the diagnostics
    worker, one import at a time. UI, duckdb, NLP, vision, vector search, and
    audio run as one-shot subprocesses so Qt, paddle, and sounddevice never stay
    loaded in the =PY() worker. Returns (True, final_text) when the interpreter
    answered, including when packages are missing. Returns (False, message) when
    the interpreter cannot be started or the version probe fails.
    """
    from plugin.scripting.venv_worker import PythonWorkerManager

    # scripting.python_exec_timeout is the user-script budget. These probes
    # always use SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC, VISION_PROBE_TIMEOUT_SEC,
    # and VECTOR_SEARCH_PROBE_TIMEOUT_SEC. There is no caller timeout argument.
    per_pkg_timeout = SELF_CHECK_IMPORT_PROBE_TIMEOUT_SEC

    shown: list[str] = []

    def _show(text: str) -> None:
        shown.clear()
        shown.append(text)
        on_display(text)

    def _fail(msg: str) -> tuple[bool, str]:
        # What was wrong: (False, msg) never reached the LogArea.
        # How: run_modal_probe binds the message to _msg and only finish()es,
        # and these returns never called on_display. Path checks in
        # probe_venv_path_with_progress do call on_display; these do not.
        # Why: show msg when the log is still empty, and append it under the
        # last body when a group was already drawn. Package IPC failures are
        # not this path: execute() returns a status dict, and the
        # status != "ok" branch records a warning and continues.
        # The returned message stays msg.
        if shown:
            body = shown[-1] + "\n\n" + msg
        else:
            body = msg
        _show(body)
        return False, msg

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
        _show(
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
    from plugin.framework.constants import WORKER_POOL_DIAGNOSTICS

    manager = PythonWorkerManager.get(
        python_exe,
        scrub_subprocess_env(dict(os.environ)),
        pool=WORKER_POOL_DIAGNOSTICS,
    )

    _status(_("Reading Python version..."))
    response = manager.execute(_VERSION_PROBE_SCRIPT, timeout_sec=per_pkg_timeout)

    if response.get("status") != "ok":
        msg = str(response.get("message", "Unknown error"))
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return _fail("Timed out waiting for Python (check venv and try again).")
        return _fail(msg)

    version_data = response.get("result")
    if not isinstance(version_data, dict):
        return _fail(f"Unexpected output from test run: {version_data!r}")

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
                    partial_group_title=_(group_title),
                    include_audio=include_audio,
                )
                continue
            pkg_resp = manager.execute(_package_probe_script(pkg), timeout_sec=per_pkg_timeout)
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
                    partial_group_title=_(group_title),
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
                partial_group_title=_(group_title),
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

    final_msg = _build_probe_display(
        data,
        completed_groups=_SELF_CHECK_DISPLAY_GROUP_COUNT,
        include_vector_search=include_vector_search,
        include_vision=True,
        include_audio=include_audio,
        include_install_footer=True,
        extra_lines_after_header=extra_lines_after_header,
    )
    _show(final_msg)
    return True, final_msg


def probe_venv_path_with_progress(
    venv_dir: str,
    on_display: Callable[[str], None],
    on_status: Callable[[str], None] | None = None,
    extra_lines_after_header: tuple[str, ...] | None = None,
    *,
    include_vector_search: bool = True,
    include_audio: bool = True,
) -> tuple[bool, str]:
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
        on_status=on_status,
        extra_lines_after_header=extra_lines_after_header,
        include_vector_search=include_vector_search,
        include_audio=include_audio,
    )
