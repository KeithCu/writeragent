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
Synthesis runs in the venv via ``kokoro_worker.py`` (warm stdio loop, or
``--once``). This module still does not import misaki or kokoro-onnx
(LibreOffice's Python must not gain those deps).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import logging
import shutil
import subprocess
import threading
from typing import Any, Callable

from plugin.framework.errors import best_effort
from plugin.framework.i18n import _

log = logging.getLogger(__name__)

ESPEAK_PIP = ("misaki", "phonemizer-fork", "espeakng-loader")


KOKORO_PIP_INSTALL = "uv pip install kokoro-onnx soundfile 'misaki[ja,zh]' phonemizer-fork espeakng-loader"
TTS_INSTALL_CMD = "uv pip install kokoro-onnx soundfile piper-tts 'misaki[ja,zh]' phonemizer-fork espeakng-loader"

_PROBE_TIMEOUT_SEC = 90.0
_PIP_TIMEOUT_SEC = 300.0
_UNIDIC_TIMEOUT_SEC = 300.0

G2P_FAILED_MARK = "Misaki G2P failed"


def g2p_fallback_warning(lang: str, exc: BaseException | None = None) -> str:
    fallback = "Kokoro fell back to espeak-ng for %s; non-English text may be misread" % lang
    if exc is None:
        return fallback
    return ("Misaki G2P failed for %s: %s" % (lang, exc)) + " " + fallback


@dataclass(frozen=True)
class MisakiLangSpec:
    module: str
    attr: str
    packages: tuple[str, ...] = ESPEAK_PIP
    probe_text: str = "ok"
    # Empty means call the class with no args (JAG2P / ZHG2P). EspeakG2P gets language=.
    language: str = ""
    needs_unidic: bool = False


_ESPEAK_LANGS = ("es", "fr-fr", "it", "hi", "pt-br")

_LANG_SPECS: dict[str, MisakiLangSpec] = {
    "ja": MisakiLangSpec(
        module="misaki.ja",
        attr="JAG2P",
        packages=("misaki[ja]",),
        probe_text="\u3042",
        needs_unidic=True,
    ),
    "zh": MisakiLangSpec(
        module="misaki.zh",
        attr="ZHG2P",
        packages=("misaki[zh]",),
        probe_text="\u4f60",
    ),
    **{
        lang: MisakiLangSpec(
            module="misaki.espeak",
            attr="EspeakG2P",
            language=lang,
        )
        for lang in _ESPEAK_LANGS
    },
}

_g2p_cache: dict[str, Any] = {}


def _get_misaki_g2p(lang: str) -> Any:
    # JAG2P() reloads fugashi and the unidic dictionary, and EspeakG2P rebuilds
    # the espeak map; the warm worker exists so later sentences skip that.
    # The stdio loop is single-threaded, so the dict needs no lock.
    inst = _g2p_cache.get(lang)
    if inst is not None:
        return inst
    spec = _LANG_SPECS[lang]
    cls = getattr(import_module(spec.module), spec.attr)  # type: ignore[import-not-found, ty:unresolved-import]
    inst = cls(language=spec.language) if spec.language else cls()
    _g2p_cache[lang] = inst
    return inst


_probe_ok_keys: set[tuple[str, str]] = set()
_seen_unknown_prefixes: set[str] = set()
_probe_ok_lock = threading.Lock()


def clear_kokoro_misaki_probe_cache() -> None:
    with _probe_ok_lock:
        _probe_ok_keys.clear()
        _seen_unknown_prefixes.clear()


# Single source of truth for Kokoro voice prefixes and their G2P language codes
KOKORO_VOICE_PREFIX_TO_LANG: dict[str, str] = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "zh",
}


def _kokoro_lang_for_voice(voice: str) -> str:
    """Determine the Kokoro phonemizer language code from voice prefix."""
    clean = (voice or "").lower().strip()
    prefix = clean[:1]
    if prefix and prefix not in KOKORO_VOICE_PREFIX_TO_LANG:
        with _probe_ok_lock:
            if prefix not in _seen_unknown_prefixes:
                _seen_unknown_prefixes.add(prefix)
                log.warning("Kokoro voice %r has no G2P prefix; using en-us", voice)
        return "en-us"
    return KOKORO_VOICE_PREFIX_TO_LANG.get(prefix, "en-us")


def _text_is_latin_script(text: str) -> bool:
    """True when all alphabetic characters fall in basic Latin through Latin Extended-B."""
    return all(ord(char) < 0x0250 or not char.isalpha() for char in text)


def kokoro_g2p_lang(text: str, voice: str = "") -> str:
    """Phonemizer code for one Kokoro utterance. The voice id is not changed."""
    if not voice:
        return "en-us"
    target = _kokoro_lang_for_voice(voice)
    if target in ("ja", "zh", "hi") and _text_is_latin_script(text):
        return "en-us"
    return target


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


def misaki_frontend(text: str, lang: str) -> tuple[str, str]:
    """Return ``(phonemes, warning)``. Empty phonemes means espeak-ng.

    The warning strings are what both the warm worker and ``--once`` put in the
    ``warning`` field. English returns ``("", "")`` and does not import misaki.
    """
    if not kokoro_lang_uses_misaki(lang):
        return "", ""
    try:
        phonemes = misaki_phonemes(text, lang)
    except Exception as exc:
        return "", g2p_fallback_warning(lang, exc)
    if isinstance(phonemes, str) and phonemes.strip():
        return phonemes, ""
    return "", g2p_fallback_warning(lang)


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
    quoted = " ".join(f"'{p}'" if "[" in p else p for p in packages)
    return "uv pip install " + quoted


def kokoro_misaki_probe_code(lang: str) -> str:
    """Venv snippet that constructs and runs the G2P. Empty for English.

    Constructing ``JAG2P`` is not enough: the ``unidic`` pip package does not
    ship the dictionary, and ``ZHG2P()`` does not import jieba until called.
    A one-character call fails when the extra is only half-installed.
    """
    spec = _LANG_SPECS.get(lang)
    if spec is None:
        return ""
    kwargs_str = f"language={spec.language!r}" if spec.language else ""
    return (
        f"from {spec.module} import {spec.attr}\n"
        f"phonemes, _tokens = {spec.attr}({kwargs_str})({spec.probe_text!r})\n"
        "raise SystemExit(0 if isinstance(phonemes, str) and phonemes.strip() else 1)\n"
    )


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


def _run_checked(
    cmd: list[str],
    timeout: float,
    *,
    what: str,
    level: int,
    run_cmd: RunCmd,
    log_when_empty: bool,
) -> bool:
    completed = run_cmd(cmd, timeout)
    if completed is None or completed.returncode != 0:
        detail = "" if completed is None else (completed.stderr or completed.stdout or "").strip()
        if detail or log_when_empty:
            log.log(level, "%s: %s", what, detail[-2000:])
        return False
    return True


def _venv_probe_ok(
    py_exe: str,
    lang: str,
    run_cmd: RunCmd,
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
    run_cmd: RunCmd,
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
    run_cmd: RunCmd,
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
    *,
    run_cmd: RunCmd,
) -> bool | None:
    """Install the Misaki extra for ``lang`` when the venv cannot import it.

    Returns True when the G2P probe passes (including English, which needs
    nothing). Returns False when install failed; the caller still speaks and
    the venv script falls back to espeak-ng. Returns None when speech was
    cancelled so the caller does not start synthesis.
    """
    if _cancelled(cancelled):
        return None
    with _probe_ok_lock:
        if (py_exe, lang) in _probe_ok_keys:
            return True
    spec = _LANG_SPECS.get(lang)
    if spec is None:
        return True

    def remember() -> bool:
        with _probe_ok_lock:
            _probe_ok_keys.add((py_exe, lang))
        return True

    def fail() -> bool:
        _emit(
            on_status,
            _("Couldn't install the Kokoro phonemizer; this language may be misread. Install with: {0}").format(
                kokoro_misaki_install_hint(lang)
            ),
        )
        return False

    if _venv_probe_ok(py_exe, lang, run_cmd=run_cmd):
        return remember()
    if _cancelled(cancelled):
        return None

    _emit(on_status, _("Installing Kokoro phonemizer…"))
    if not _pip_install(py_exe, spec.packages, run_cmd=run_cmd):
        return fail()
    if _cancelled(cancelled):
        return None
    if _venv_probe_ok(py_exe, lang, run_cmd=run_cmd):
        return remember()

    # fugashi's default dictionary is not in the wheel. Without this download,
    # JAG2P() raises and Japanese falls back to espeak ("chinese letter").
    if spec.needs_unidic:
        _emit(on_status, _("Downloading Japanese phonemizer dictionary…"))
        if _download_unidic(py_exe, run_cmd=run_cmd) and _venv_probe_ok(py_exe, lang, run_cmd=run_cmd):
            return remember()
    return fail()
