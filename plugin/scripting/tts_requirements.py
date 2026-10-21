# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Single source for Kokoro and Piper TTS pip requirement strings.

No imports: safe for LibrePy bundle inclusion and diagnostics without importing plugin.audio.
"""

from __future__ import annotations

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
