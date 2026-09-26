#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot faster-whisper transcription for the Settings → Python venv.

The host (LibreOffice's Python) must not import faster-whisper. It spawns this
file with the venv interpreter::

    python whisper_transcribe.py --wav /tmp/rec.wav --model base

Stdout is one JSON object. ``WhisperModel`` is constructed without
``download_root``, so Systran weights follow Hugging Face's own cache on
first use: ``~/.cache/huggingface`` (Windows: ``%USERPROFILE%\\.cache\\huggingface``),
or ``HF_HOME`` / ``HF_HUB_CACHE`` when those are already set. WriterAgent does
not invent a private Hugging Face root.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def transcribe_wav(wav_path: str, model_name: str) -> str:
    """Transcribe ``wav_path`` with faster-whisper on CPU.

    ``int8`` is the CPU default in the faster-whisper README
    (SYSTRAN/faster-whisper ``README.md``, "Usage"). ``device="cpu"`` keeps
    this child off CUDA even when the venv has a GPU build.
    """
    # The package lives in the user venv. LibreOffice's Python never imports it.
    from faster_whisper import WhisperModel  # type: ignore[import-not-found, ty:unresolved-import]

    # download_root stays unset. faster-whisper then uses the Hugging Face
    # cache (HF_HUB_CACHE, else HF_HOME, else ~/.cache/huggingface). Passing a
    # path would ignore a user HF_HOME or pin weights to a WriterAgent-only tree.
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(wav_path)
    parts: list[str] = []
    for segment in segments:
        text = getattr(segment, "text", "")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe a WAV with faster-whisper")
    parser.add_argument("--wav", required=True)
    parser.add_argument("--model", default="base")
    args = parser.parse_args(argv)
    try:
        text = transcribe_wav(args.wav, args.model)
    except ImportError as exc:
        _emit({"status": "error", "message": "faster-whisper is not installed (%s)" % exc})
        return 1
    except Exception as exc:
        _emit({"status": "error", "message": str(exc)})
        return 1
    _emit({"status": "ok", "text": text})
    return 0


if __name__ == "__main__":
    sys.exit(main())
