#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Long-lived Kokoro subprocess. Loads the ONNX model once and writes wav paths.

The stdio loop matches ``compute_service.worker_base.run_worker_stdio_loop``
(Pickle 5 length-prefix from ``plugin.scripting.ipc``). ``compute_service`` is
not shipped inside the WriterAgent OXT, so this child keeps its own copy of
that small loop and talks to ``plugin.audio.kokoro_pool`` with the same frames.

Jobs are ``{text, voice, speed, lang, model_path, voices_path, out_path}``.
The response carries the wav **path**, not the samples: a sentence of audio
can exceed the 16MiB pickle cap, and the parent already chose the temp file.

Endpoint TTS never starts this process. Piper stays a one-shot CLI in the
parent; only Kokoro pays the ONNX + voices load this worker exists to avoid.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Extension layout is ``<root>/plugin/audio/kokoro_worker.py``. The venv
# interpreter does not have the extension on sys.path (LibreOffice's
# PYTHONPATH is scrubbed before spawn). Insert the root that contains
# ``plugin`` so the child can import the shared IPC helpers.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, os.pardir, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from plugin.scripting.ipc import (  # noqa: E402
    DEFAULT_MAX_PAYLOAD_BYTES,
    read_pickle_frame,
    write_pickle_frame,
)

# Held for the life of the process. Replaced only when the model paths change.
_loaded_key: tuple[str, str] | None = None
_loaded: Any = None


def run_kokoro_stdio_loop(
    handler: Any,
    *,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> int:
    """Pickle 5 request loop. First frame is ``{"status": "ready", "pid": ...}``."""
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer
    write_pickle_frame(stdout_bin, {"status": "ready", "pid": os.getpid()})
    while True:
        try:
            req = read_pickle_frame(stdin_bin, max_payload_bytes=max_payload_bytes)
            if req is None:
                break
            if not isinstance(req, dict):
                res: dict[str, Any] = {"status": "error", "error": "Request must be a dict"}
            else:
                res = handler(req)
                if not isinstance(res, dict):
                    res = {"status": "error", "error": "Handler must return a dict"}
        except Exception as exc:
            res = {"status": "error", "error": f"Invalid IPC frame or unhandled error: {exc}"}
        try:
            write_pickle_frame(stdout_bin, res, max_payload_bytes=max_payload_bytes)
        except Exception:
            break
    return 0


def _voice_names(kokoro: Any) -> Any:
    return getattr(kokoro, "voices", ())


def _has_voice(kokoro: Any, name: str) -> bool:
    try:
        return name in _voice_names(kokoro)
    except TypeError:
        return False


def _fallback_voice(kokoro: Any) -> str:
    if _has_voice(kokoro, "af_bella"):
        return "af_bella"
    voices = _voice_names(kokoro)
    if isinstance(voices, dict) and voices:
        return str(next(iter(voices)))
    if voices:
        return str(voices[0])
    return "af_bella"


def _get_kokoro(model_path: str, voices_path: str) -> Any:
    """Construct ``Kokoro`` once per (model, voices) pair."""
    global _loaded, _loaded_key
    key = (model_path, voices_path)
    if _loaded is not None and _loaded_key == key:
        return _loaded
    # kokoro-onnx lives in the user venv, not LibreOffice's or CI's site-packages.
    from kokoro_onnx import Kokoro  # type: ignore[import-not-found, ty:unresolved-import]

    inst = Kokoro(model_path, voices_path)
    _loaded = inst
    _loaded_key = key
    return inst


def _misaki_frontend(text: str, lang: str) -> tuple[str, str]:
    """Return ``(phonemes, warning)``. Empty phonemes means espeak-ng.

    The warning strings match ``KOKORO_ONNX_SCRIPT`` so the host can show the
    same status line for the warm worker and the one-shot fallback. English
    returns ``("", "")`` and does not import misaki.
    """
    try:
        from plugin.audio.kokoro_g2p import kokoro_lang_uses_misaki, misaki_phonemes
    except Exception as exc:
        # English never needs Misaki. A broken import must not mark af_* as
        # a phonemizer failure. Other langs still speak via espeak-ng.
        if lang in ("en-us", "en-gb", ""):
            return "", ""
        failed = f"Misaki G2P failed for {lang}: {exc}"
        fallback = f"Kokoro fell back to espeak-ng for {lang}; non-English text may be misread"
        return "", f"{failed} {fallback}"
    if not kokoro_lang_uses_misaki(lang):
        return "", ""
    try:
        phonemes = misaki_phonemes(text, lang)
    except Exception as exc:
        failed = f"Misaki G2P failed for {lang}: {exc}"
        fallback = f"Kokoro fell back to espeak-ng for {lang}; non-English text may be misread"
        return "", f"{failed} {fallback}"
    if isinstance(phonemes, str) and phonemes.strip():
        return phonemes, ""
    return "", f"Kokoro fell back to espeak-ng for {lang}; non-English text may be misread"


def handle_kokoro_job(req: dict[str, Any]) -> dict[str, Any]:
    """Synthesize one utterance to ``out_path`` and return that path."""
    text = req.get("text")
    out_path = req.get("out_path")
    model_path = req.get("model_path")
    voices_path = req.get("voices_path")
    if not isinstance(text, str) or not text.strip():
        return {"status": "error", "error": "text must be a non-empty string"}
    if not isinstance(out_path, str) or not out_path:
        return {"status": "error", "error": "out_path must be a file path"}
    if not isinstance(model_path, str) or not model_path:
        return {"status": "error", "error": "model_path is required"}
    if not isinstance(voices_path, str) or not voices_path:
        return {"status": "error", "error": "voices_path is required"}

    voice = str(req.get("voice") or "af_bella")
    try:
        speed = float(req.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    lang = str(req.get("lang") or "en-us")

    try:
        kokoro = _get_kokoro(model_path, voices_path)
    except Exception as exc:
        return {"status": "error", "error": f"Kokoro load failed: {exc}"}

    warning = ""
    if not _has_voice(kokoro, voice):
        # Same substitution as the one-shot script in tts_service: a voices
        # file that lacks the requested id (English-only pack, custom path)
        # keeps speaking instead of failing the sentence.
        fallback = _fallback_voice(kokoro)
        warning = f"Kokoro voice {voice!r} is not in {voices_path}; using {fallback}"
        voice = fallback

    phonemes, g2p_warning = _misaki_frontend(text, lang)
    if g2p_warning:
        warning = f"{warning} {g2p_warning}".strip() if warning else g2p_warning

    try:
        # soundfile is the same venv-only dependency as kokoro-onnx.
        import soundfile as sf  # type: ignore[import-not-found, ty:unresolved-import]

        if phonemes:
            samples, rate = kokoro.create(
                phonemes, voice=voice, speed=speed, lang=lang, is_phonemes=True
            )
        else:
            samples, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        sf.write(out_path, samples, rate)
    except Exception as exc:
        return {"status": "error", "error": f"Kokoro synthesis failed: {exc}", "warning": warning}

    result: dict[str, Any] = {"status": "ok", "path": out_path}
    if warning:
        result["warning"] = warning
    return result


def main() -> int:
    return run_kokoro_stdio_loop(handle_kokoro_job)


if __name__ == "__main__":
    raise SystemExit(main())
