# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Misaki G2P for local Kokoro voices that are not English.

kokoro-onnx phonemizes with espeak-ng unless ``create(..., is_phonemes=True)``.
That is the wrong frontend for Kokoro v1.0:

- Japanese: espeak-ng has no kanji lexicon. ``espeak-ng -v ja -x "非常に強力"``
  prints the English words "chinese letter", which is what a ``jf_*`` voice
  then speaks.
- French, Spanish, Italian, Hindi, and Brazilian Portuguese: Kokoro was
  trained on ``misaki.espeak.EspeakG2P``, which remaps espeak phones into
  Kokoro's alphabet (``misaki/espeak.py`` ``EspeakG2P.e2m``). kokoro-onnx's
  tokenizer does not, so ``ff_siwis`` and ``ef_dora`` sound wrong.
- Chinese: Kokoro-82M uses Misaki's legacy ``ZHG2P()`` (``version=None``).
  ``version="1.1"`` belongs to the separate v1.1-zh model, which we do not
  download.

English (``en-us`` / ``en-gb``) stays on kokoro-onnx's espeak tokenizer.
``examples/english.py`` uses ``misaki[en]``, but that extra installs torch
and would change English audio that already ships.

Patterns: hexgrad/kokoro ``pipeline.py`` ``KPipeline.__init__`` and
kokoro-onnx ``examples/{japanese,french,spanish,chinese}.py``.
Synthesis still runs in the configured venv via ``python -c``; this module
does not import misaki (LibreOffice's Python must not gain those deps).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import logging
import shutil
import subprocess
from typing import Any, Callable

from plugin.framework.errors import best_effort
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

KOKORO_RUNTIME = ("kokoro-onnx", "soundfile")
PIPER_PACKAGE = "piper-tts"
MISAKI_LANG_EXTRAS = ("ja", "zh")
G2P_SHARED = ("phonemizer-fork", "espeakng-loader")
ESPEAK_PIP = ("misaki",) + G2P_SHARED


def misaki_lang_extra() -> str:
    return f"misaki[{','.join(MISAKI_LANG_EXTRAS)}]"


def uv_install(*packages: str) -> str:
    quoted = [f"'{p}'" if "[" in p else p for p in packages]
    return "uv pip install " + " ".join(quoted)


KOKORO_PIP_INSTALL = uv_install(*KOKORO_RUNTIME, misaki_lang_extra(), *G2P_SHARED)
TTS_INSTALL_CMD = uv_install(*KOKORO_RUNTIME, PIPER_PACKAGE, misaki_lang_extra(), *G2P_SHARED)

_PROBE_TIMEOUT_SEC = 90.0
_PIP_TIMEOUT_SEC = 300.0
_UNIDIC_TIMEOUT_SEC = 300.0

G2P_FAILED_FMT = "Misaki G2P failed for %s: %s"
G2P_FALLBACK_FMT = "Kokoro fell back to espeak-ng for %s; non-English text may be misread"
G2P_FAILED_MARK = "Misaki G2P failed"


def g2p_fallback_warning(lang: str, exc: BaseException | None = None) -> str:
    fallback = G2P_FALLBACK_FMT % lang
    if exc is None:
        return fallback
    return (G2P_FAILED_FMT % (lang, exc)) + " " + fallback


@dataclass(frozen=True)
class MisakiLangSpec:
    lang: str
    module: str
    attr: str
    kwargs: tuple[tuple[str, str], ...]
    packages: tuple[str, ...]
    probe_text: str
    needs_unidic: bool


_ESPEAK_LANGS = ("es", "fr-fr", "it", "hi", "pt-br")

_LANG_SPECS: dict[str, MisakiLangSpec] = {
    "ja": MisakiLangSpec(
        lang="ja",
        module="misaki.ja",
        attr="JAG2P",
        kwargs=(),
        packages=("misaki[ja]",),
        probe_text="\u3042",
        needs_unidic=True,
    ),
    "zh": MisakiLangSpec(
        lang="zh",
        module="misaki.zh",
        attr="ZHG2P",
        kwargs=(),
        packages=("misaki[zh]",),
        probe_text="\u4f60",
        needs_unidic=False,
    ),
    **{
        lang: MisakiLangSpec(
            lang=lang,
            module="misaki.espeak",
            attr="EspeakG2P",
            kwargs=(("language", lang),),
            packages=ESPEAK_PIP,
            probe_text="ok",
            needs_unidic=False,
        )
        for lang in _ESPEAK_LANGS
    },
}


def _misaki_snippet(spec: MisakiLangSpec) -> str:
    kwargs_str = ", ".join(f"{k}={v!r}" for k, v in spec.kwargs)
    return f"from {spec.module} import {spec.attr}\nphonemes, _tokens = {spec.attr}({kwargs_str})(text)"


_MISAKI_G2P_SNIPPETS: dict[str, str] = {
    spec.lang: _misaki_snippet(spec)
    for spec in _LANG_SPECS.values()
}

_g2p_cache: dict[str, Any] = {}


def _get_misaki_g2p(lang: str) -> Any:
    # JAG2P() reloads fugashi and the unidic dictionary, and EspeakG2P rebuilds
    # the espeak map; the warm worker exists so later sentences skip that.
    # The stdio loop is single-threaded, so the dict needs no lock.
    inst = _g2p_cache.get(lang)
    if inst is not None:
        return inst
    spec = _LANG_SPECS.get(lang)
    if spec is None:
        raise RuntimeError("no Misaki G2P for %s" % lang)
    cls = getattr(import_module(spec.module), spec.attr)  # type: ignore[import-not-found, ty:unresolved-import]
    inst = cls(**dict(spec.kwargs))
    _g2p_cache[lang] = inst
    return inst


def kokoro_lang_uses_misaki(lang: str) -> bool:
    """True when this Kokoro lang code must be phonemized with Misaki."""
    return lang in _LANG_SPECS


def misaki_phonemes(text: str, lang: str) -> str:
    """Phonemes for ``lang``. Empty when this voice stays on espeak-ng.

    Only the venv Kokoro worker may call this: the import of misaki happens
    here, and LibreOffice's Python must not take that dependency. A successful
    construct is reused for that ``lang`` until the process exits. English is
    not cached and does not import misaki.
    """
    if lang not in _LANG_SPECS:
        return ""
    g2p = _get_misaki_g2p(lang)
    phonemes, _tokens = g2p(text)
    if not isinstance(phonemes, str):
        return ""
    return phonemes


def kokoro_misaki_packages(lang: str) -> tuple[str, ...]:
    """Pip requirements for ``lang``, or empty when English keeps espeak-ng."""
    spec = _LANG_SPECS.get(lang)
    if spec is not None:
        return spec.packages
    return ()


def kokoro_misaki_install_hint(lang: str) -> str:
    """Human install line for one language, e.g. ``uv pip install 'misaki[ja]'``."""
    packages = kokoro_misaki_packages(lang)
    if not packages:
        return KOKORO_PIP_INSTALL
    return uv_install(*packages)


def kokoro_misaki_probe_code(lang: str) -> str:
    """Venv snippet that constructs and runs the G2P. Empty for English.

    Constructing ``JAG2P`` is not enough: the ``unidic`` pip package does not
    ship the dictionary, and ``ZHG2P()`` does not import jieba until called.
    A one-character call fails when the extra is only half-installed.
    """
    spec = _LANG_SPECS.get(lang)
    if spec is None:
        return ""
    kwargs_str = ", ".join(f"{k}={v!r}" for k, v in spec.kwargs)
    return f"from {spec.module} import {spec.attr}\n{spec.attr}({kwargs_str})({spec.probe_text!r})\n"


def _misaki_branches() -> str:
    """If/elif body that fills ``phonemes`` for each non-English lang."""
    lines: list[str] = []
    for index, (lang, snippet) in enumerate(_MISAKI_G2P_SNIPPETS.items()):
        keyword = "if" if index == 0 else "elif"
        lines.append("        %s lang == %r:" % (keyword, lang))
        for line in snippet.splitlines():
            lines.append("            " + line)
    lines.append("        else:")
    lines.append('            raise RuntimeError("no Misaki G2P for %s" % lang)')
    return "\n".join(lines)


def _build_kokoro_onnx_script() -> str:
    """``python -c`` source. English skips Misaki; other langs set is_phonemes.

    The language tuple is spliced in with a marker. The script itself contains
    ``%`` format strings for the venv process; interpolating those here would
    raise at import time.
    """
    lang_tuple = repr(tuple(_LANG_SPECS.keys()))
    script = (
        "import sys\n"
        "from kokoro_onnx import Kokoro\n"
        "import soundfile as sf\n"
        "\n"
        "text = sys.stdin.read() if sys.argv[1] == '-' else sys.argv[1]\n"
        "requested = sys.argv[2]\n"
        "speed = float(sys.argv[3])\n"
        "out_path = sys.argv[4]\n"
        "model_path = sys.argv[5]\n"
        "voices_path = sys.argv[6]\n"
        "lang = sys.argv[7]\n"
        "\n"
        "kokoro = Kokoro(model_path, voices_path)\n"
        "names = list(kokoro.voices)\n"
        "if requested in names:\n"
        "    voice = requested\n"
        "else:\n"
        "    voice = 'af_sky' if 'af_sky' in names else names[0]\n"
        "    sys.stderr.write(\n"
        "        'Kokoro voice %r is not in %s; using %s\\n'\n"
        "        % (requested, voices_path, voice)\n"
        "    )\n"
        "\n"
        "phonemes = ''\n"
        "if lang in __KOKORO_MISAKI_LANGS__:\n"
        "    try:\n"
        "__MISAKI_BRANCHES__\n"
        "    except Exception as exc:\n"
        "        sys.stderr.write('" + G2P_FAILED_FMT + "\\n' % (lang, exc))\n"
        "        phonemes = ''\n"
        "\n"
        "if isinstance(phonemes, str) and phonemes.strip():\n"
        "    samples, rate = kokoro.create(\n"
        "        phonemes, voice=voice, speed=speed, lang=lang, is_phonemes=True\n"
        "    )\n"
        "else:\n"
        "    if lang in __KOKORO_MISAKI_LANGS__:\n"
        "        sys.stderr.write(\n"
        "            '" + G2P_FALLBACK_FMT + "\\n'\n"
        "            % lang\n"
        "        )\n"
        "    samples, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)\n"
        "sf.write(out_path, samples, rate)\n"
    )
    return (
        script.replace("__KOKORO_MISAKI_LANGS__", lang_tuple)
        .replace("__MISAKI_BRANCHES__", _misaki_branches())
    )


# Built once so speak and tests share the exact script the venv runs.
KOKORO_ONNX_SCRIPT = _build_kokoro_onnx_script()


def _emit(on_status: Callable[[str], None] | None, message: str) -> None:
    if on_status is None:
        log.info("%s", message)
        return
    on_status(message)


def _cancelled(cancelled: Callable[[], bool] | None) -> bool:
    if cancelled is None:
        return False
    return bool(best_effort(cancelled, "Kokoro phonemizer cancel check failed", False))


RunCmd = Callable[[list[str], float], subprocess.CompletedProcess[str] | None]


def _run_cmd(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Kokoro phonemizer command failed (%s): %s", cmd[0], exc)
        return None


def _failure_detail(completed: subprocess.CompletedProcess[str] | None) -> str:
    if completed is None:
        return ""
    return (completed.stderr or completed.stdout or "").strip()


def _run_checked(
    cmd: list[str],
    timeout: float,
    *,
    what: str,
    level: int,
    run_cmd: RunCmd | None,
    log_when_empty: bool,
) -> bool:
    runner = run_cmd or _run_cmd
    completed = runner(cmd, timeout)
    if completed is None or completed.returncode != 0:
        detail = _failure_detail(completed)
        if detail or log_when_empty:
            log.log(level, "%s: %s", what, detail[-2000:])
        return False
    return True


def _venv_probe_ok(
    py_exe: str,
    lang: str,
    run_cmd: RunCmd | None = None,
) -> bool:
    code = kokoro_misaki_probe_code(lang)
    if not code:
        return True
    return _run_checked(
        [py_exe, "-c", code],
        _PROBE_TIMEOUT_SEC,
        what=f"Kokoro phonemizer probe ({lang}) failed",
        level=logging.INFO,
        run_cmd=run_cmd,
        log_when_empty=False,
    )


def _pip_install(
    py_exe: str,
    packages: tuple[str, ...],
    run_cmd: RunCmd | None = None,
) -> bool:
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", py_exe, *packages]
    else:
        cmd = [py_exe, "-m", "pip", "install", *packages]
    log.info("Installing Kokoro phonemizer: %s", " ".join(cmd))
    return _run_checked(
        cmd,
        _PIP_TIMEOUT_SEC,
        what="Kokoro phonemizer install failed",
        level=logging.WARNING,
        run_cmd=run_cmd,
        log_when_empty=True,
    )


def _download_unidic(
    py_exe: str,
    run_cmd: RunCmd | None = None,
) -> bool:
    """``misaki[ja]`` depends on the unidic package, which does not include the dictionary."""
    return _run_checked(
        [py_exe, "-m", "unidic", "download"],
        _UNIDIC_TIMEOUT_SEC,
        what="Japanese phonemizer dictionary download failed",
        level=logging.WARNING,
        run_cmd=run_cmd,
        log_when_empty=True,
    )


def ensure_kokoro_misaki(
    py_exe: str,
    lang: str,
    on_status: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    run_cmd: RunCmd | None = None,
) -> bool | None:
    """Install the Misaki extra for ``lang`` when the venv cannot import it.

    Returns True when the G2P probe passes (including English, which needs
    nothing). Returns False when install failed; the caller still speaks and
    the venv script falls back to espeak-ng. Returns None when speech was
    cancelled so the caller does not start synthesis.
    """
    if _cancelled(cancelled):
        return None
    packages = kokoro_misaki_packages(lang)
    if not packages:
        return True
    if _venv_probe_ok(py_exe, lang, run_cmd=run_cmd):
        return True
    if _cancelled(cancelled):
        return None

    _emit(on_status, _("Installing Kokoro phonemizer…"))
    if not _pip_install(py_exe, packages, run_cmd=run_cmd):
        _emit(
            on_status,
            _("Couldn't install the Kokoro phonemizer; this language may be misread. Install with: {0}").format(
                kokoro_misaki_install_hint(lang)
            ),
        )
        return False
    if _cancelled(cancelled):
        return None
    if _venv_probe_ok(py_exe, lang, run_cmd=run_cmd):
        return True

    # fugashi's default dictionary is not in the wheel. Without this download,
    # JAG2P() raises and Japanese falls back to espeak ("chinese letter").
    spec = _LANG_SPECS.get(lang)
    if spec is not None and spec.needs_unidic:
        _emit(on_status, _("Downloading Japanese phonemizer dictionary…"))
        if _download_unidic(py_exe, run_cmd=run_cmd) and _venv_probe_ok(py_exe, lang, run_cmd=run_cmd):
            return True

    _emit(
        on_status,
        _("Couldn't install the Kokoro phonemizer; this language may be misread. Install with: {0}").format(
            kokoro_misaki_install_hint(lang)
        ),
    )
    return False
