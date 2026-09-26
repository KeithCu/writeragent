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

import logging
import shutil
import subprocess
from typing import Callable

from plugin.framework.i18n import _

log = logging.getLogger(__name__)

# Spoken install line when kokoro-onnx itself is missing, and the multilingual
# extras a non-English voice needs. piper-tts is intentionally absent here;
# venv_diagnostics._TTS_INSTALL_CMD adds it for the Test dialog.
KOKORO_PIP_INSTALL = (
    "uv pip install kokoro-onnx soundfile 'misaki[ja,zh]' phonemizer-fork espeakng-loader"
)

# es / fr-fr / it / hi / pt-br have no Misaki language extra. EspeakG2P lives
# in the base package and needs phonemizer-fork (set_data_path) plus
# espeakng-loader. Do not install misaki[en]: that pulls torch.
_ESPEAK_MISAKI_LANGS = frozenset({"es", "fr-fr", "it", "hi", "pt-br"})
_ESPEAK_PIP = ("misaki", "phonemizer-fork", "espeakng-loader")

# Snippets run inside the venv script. They must stay valid on the user's
# venv Python (no host-only syntax). ``phonemes, _tokens`` avoids a bare ``_``.
def _espeak_g2p_snippet(lang: str) -> str:
    return (
        "from misaki.espeak import EspeakG2P\n"
        "phonemes, _tokens = EspeakG2P(language=%r)(text)" % lang
    )


# version=None on Chinese: legacy pinyin frontend for Kokoro-82M / kokoro-v1.0.onnx.
# version="1.1" is only for the separate v1.1-zh checkpoint.
_MISAKI_G2P_SNIPPETS: dict[str, str] = {
    "ja": "from misaki.ja import JAG2P\nphonemes, _tokens = JAG2P()(text)",
    "zh": "from misaki.zh import ZHG2P\nphonemes, _tokens = ZHG2P()(text)",
    **{lang: _espeak_g2p_snippet(lang) for lang in ("es", "fr-fr", "it", "hi", "pt-br")},
}

_PROBE_TIMEOUT_SEC = 90.0
_PIP_TIMEOUT_SEC = 300.0
_UNIDIC_TIMEOUT_SEC = 300.0


def kokoro_lang_uses_misaki(lang: str) -> bool:
    """True when this Kokoro lang code must be phonemized with Misaki."""
    return lang in _MISAKI_G2P_SNIPPETS


def misaki_phonemes(text: str, lang: str) -> str:
    """Phonemes for ``lang``. Empty when this voice stays on espeak-ng.

    Runs the same snippets as ``KOKORO_ONNX_SCRIPT``. Only the venv Kokoro
    worker may call this: the import of misaki happens here, and LibreOffice's
    Python must not take that dependency. The host installs via
    ``ensure_kokoro_misaki`` and otherwise uses the generated ``python -c``
    script.
    """
    snippet = _MISAKI_G2P_SNIPPETS.get(lang)
    if not snippet:
        return ""
    namespace: dict[str, object] = {"text": text}
    # Fixed Misaki snippets from this module, not caller text.
    exec(snippet, namespace, namespace)  # nosec B102  # noqa: S102
    phonemes = namespace.get("phonemes", "")
    if not isinstance(phonemes, str):
        return ""
    return phonemes


def kokoro_misaki_packages(lang: str) -> tuple[str, ...]:
    """Pip requirements for ``lang``, or empty when English keeps espeak-ng."""
    if lang == "ja":
        return ("misaki[ja]",)
    if lang == "zh":
        return ("misaki[zh]",)
    if lang in _ESPEAK_MISAKI_LANGS:
        return _ESPEAK_PIP
    return ()


def kokoro_misaki_install_hint(lang: str) -> str:
    """Human install line for one language, e.g. ``uv pip install 'misaki[ja]'``."""
    packages = kokoro_misaki_packages(lang)
    if not packages:
        return KOKORO_PIP_INSTALL
    quoted = ["'%s'" % pkg if "[" in pkg else pkg for pkg in packages]
    return "uv pip install " + " ".join(quoted)


def kokoro_misaki_probe_code(lang: str) -> str:
    """Venv snippet that constructs and runs the G2P. Empty for English.

    Constructing ``JAG2P`` is not enough: the ``unidic`` pip package does not
    ship the dictionary, and ``ZHG2P()`` does not import jieba until called.
    A one-character call fails when the extra is only half-installed.
    """
    if lang == "ja":
        return "from misaki.ja import JAG2P\nJAG2P()('\\u3042')\n"
    if lang == "zh":
        return "from misaki.zh import ZHG2P\nZHG2P()('\\u4f60')\n"
    if lang in _ESPEAK_MISAKI_LANGS:
        return (
            "from misaki.espeak import EspeakG2P\n"
            "EspeakG2P(language=%r)('ok')\n" % lang
        )
    return ""


def _misaki_branches() -> str:
    """If/elif body that fills ``phonemes`` for each non-English lang."""
    lines: list[str] = []
    for index, lang in enumerate(_MISAKI_G2P_SNIPPETS):
        keyword = "if" if index == 0 else "elif"
        lines.append("        %s lang == %r:" % (keyword, lang))
        for line in _MISAKI_G2P_SNIPPETS[lang].splitlines():
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
    lang_tuple = "(" + ", ".join("%r" % lang for lang in _MISAKI_G2P_SNIPPETS) + ",)"
    # voices-v1.0.bin is an npz. Integer indexing raises; membership and
    # list() use the voice-id keys.
    script = (
        "import sys\n"
        "from kokoro_onnx import Kokoro\n"
        "import soundfile as sf\n"
        "\n"
        "text = sys.argv[1]\n"
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
        "    voice = 'af_bella' if 'af_bella' in names else names[0]\n"
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
        "        sys.stderr.write('Misaki G2P failed for %s: %s\\n' % (lang, exc))\n"
        "        phonemes = ''\n"
        "\n"
        "if isinstance(phonemes, str) and phonemes.strip():\n"
        "    samples, rate = kokoro.create(\n"
        "        phonemes, voice=voice, speed=speed, lang=lang, is_phonemes=True\n"
        "    )\n"
        "else:\n"
        "    if lang in __KOKORO_MISAKI_LANGS__:\n"
        "        sys.stderr.write(\n"
        "            'Kokoro fell back to espeak-ng for %s; non-English text may be misread\\n'\n"
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
    try:
        return bool(cancelled())
    except Exception:
        log.debug("Kokoro phonemizer cancel check failed", exc_info=True)
        return False


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


def _venv_probe_ok(py_exe: str, lang: str) -> bool:
    code = kokoro_misaki_probe_code(lang)
    if not code:
        return True
    completed = _run_cmd([py_exe, "-c", code], _PROBE_TIMEOUT_SEC)
    if completed is None or completed.returncode != 0:
        detail = ""
        if completed is not None:
            detail = (completed.stderr or completed.stdout or "").strip()
        if detail:
            log.info("Kokoro phonemizer probe (%s) failed: %s", lang, detail[-2000:])
        return False
    return True


def _pip_install(py_exe: str, packages: tuple[str, ...]) -> bool:
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", py_exe, *packages]
    else:
        cmd = [py_exe, "-m", "pip", "install", *packages]
    log.info("Installing Kokoro phonemizer: %s", " ".join(cmd))
    completed = _run_cmd(cmd, _PIP_TIMEOUT_SEC)
    if completed is None or completed.returncode != 0:
        detail = ""
        if completed is not None:
            detail = (completed.stderr or completed.stdout or "").strip()
        log.warning("Kokoro phonemizer install failed: %s", detail[-2000:])
        return False
    return True


def _download_unidic(py_exe: str) -> bool:
    """``misaki[ja]`` depends on the unidic package, which does not include the dictionary."""
    completed = _run_cmd([py_exe, "-m", "unidic", "download"], _UNIDIC_TIMEOUT_SEC)
    if completed is None or completed.returncode != 0:
        detail = ""
        if completed is not None:
            detail = (completed.stderr or completed.stdout or "").strip()
        log.warning("Japanese phonemizer dictionary download failed: %s", detail[-2000:])
        return False
    return True


def ensure_kokoro_misaki(
    py_exe: str,
    lang: str,
    on_status: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
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
    if _venv_probe_ok(py_exe, lang):
        return True
    if _cancelled(cancelled):
        return None

    _emit(on_status, _("Installing Kokoro phonemizer…"))
    if not _pip_install(py_exe, packages):
        _emit(
            on_status,
            _("Couldn't install the Kokoro phonemizer; this language may be misread. Install with: {0}").format(
                kokoro_misaki_install_hint(lang)
            ),
        )
        return False
    if _cancelled(cancelled):
        return None
    if _venv_probe_ok(py_exe, lang):
        return True

    # fugashi's default dictionary is not in the wheel. Without this download,
    # JAG2P() raises and Japanese falls back to espeak ("chinese letter").
    if lang == "ja":
        _emit(on_status, _("Downloading Japanese phonemizer dictionary…"))
        if _download_unidic(py_exe) and _venv_probe_ok(py_exe, lang):
            return True

    _emit(
        on_status,
        _("Couldn't install the Kokoro phonemizer; this language may be misread. Install with: {0}").format(
            kokoro_misaki_install_hint(lang)
        ),
    )
    return False
