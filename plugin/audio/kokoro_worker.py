#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Long-lived Kokoro subprocess. Loads the ONNX model once and writes wav paths.

The stdio loop is a local copy because ``compute_service`` is not shipped inside
the WriterAgent OXT, and that loop's restricted unpickler and signal handling do
not apply here. Child stdout is claimed for frames with ``claim_ipc_channel``, and
a failed frame read exits so the pool can respawn.

Jobs are ``{text, voice, speed, lang, model_path, voices_path, out_path}``.
The response carries the wav **path**, not the samples: a sentence of audio
can exceed the 16MiB pickle cap, and the parent already chose the temp file.

Endpoint TTS never starts this process. Piper stays a one-shot CLI in the
parent; only Kokoro pays the ONNX + voices load this worker exists to avoid.
"""

from __future__ import annotations

import io
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
    claim_ipc_channel,
    read_pickle_frame,
    write_pickle_frame,
)
from plugin.audio.kokoro_g2p import (  # noqa: E402
    KOKORO_PIP_INSTALL,
    KOKORO_VOICE_PREFIX_TO_LANG,
    kokoro_g2p_lang,
    misaki_frontend,
)

# Held for the life of the process. Replaced only when the model paths change.
_loaded_key: tuple[str, str] | None = None
_loaded: Any = None
_kokoro_cls: Any = None
_soundfile: Any = None
_import_failure: BaseException | None = None


def _load_venv_audio() -> tuple[Any, Any]:
    global _kokoro_cls, _soundfile, _import_failure
    if isinstance(_import_failure, ImportError):
        raise _import_failure
    if _kokoro_cls is not None and _soundfile is not None:
        return _kokoro_cls, _soundfile
    try:
        from kokoro_onnx import Kokoro  # type: ignore[import-not-found, ty:unresolved-import]
        import soundfile as sf  # type: ignore[import-not-found, ty:unresolved-import]

        _kokoro_cls = Kokoro
        _soundfile = sf
        return _kokoro_cls, _soundfile
    except ImportError as exc:
        _import_failure = exc
        raise


def run_kokoro_stdio_loop(
    handler: Any,
    *,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> int:
    """Pickle 5 request loop. First frame is ``{"status": "ready", "pid": ...}``."""
    stdin_bin = sys.stdin.buffer
    try:
        is_real_fd1 = hasattr(sys.stdout, "fileno") and sys.stdout.fileno() == 1
    except (io.UnsupportedOperation, AttributeError, OSError):
        is_real_fd1 = False
    stdout_bin = claim_ipc_channel()
    if is_real_fd1:
        sys.stdout = sys.stderr
    write_pickle_frame(stdout_bin, {"status": "ready", "pid": os.getpid()})
    while True:
        # A length-prefix or decode error has consumed stdin mid-frame;
        # writing an error and reading again would desync every later job.
        try:
            req = read_pickle_frame(stdin_bin, max_payload_bytes=max_payload_bytes)
        except Exception as exc:
            sys.stderr.write(
                f"Fatal: failed to read/decode IPC frame from stdin: {exc}\n"
            )
            break
        if req is None:
            break
        try:
            if not isinstance(req, dict):
                res: dict[str, Any] = {"status": "error", "error": "Request must be a dict"}
            else:
                res = handler(req)
                if not isinstance(res, dict):
                    res = {"status": "error", "error": "Handler must return a dict"}
        except Exception as exc:
            res = {"status": "error", "error": f"Unhandled error: {exc}"}
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


def _fallback_voice(kokoro: Any, requested: str) -> str:
    """Choose a fallback voice if the requested voice is missing.

    What was wrong: voices[0] on an NpzFile raises KeyError ('0 is not a file
    in the archive').
    How it happened: kokoro_onnx stores np.load of the voices pack (NpzFile);
    the tests used a list and a pack that still contained af_sky.
    Why this change: next(iter(...)) yields the archive keys, matching
    KOKORO_ONNX_SCRIPT which lists keys from iteration.
    """
    family = requested[:1].lower()
    if family in KOKORO_VOICE_PREFIX_TO_LANG:
        try:
            for name in _voice_names(kokoro):
                if str(name)[:1].lower() == family:
                    return str(name)
        except TypeError:
            pass
    if _has_voice(kokoro, "af_sky"):
        return "af_sky"
    voices = _voice_names(kokoro)
    try:
        return str(next(iter(voices)))
    except (StopIteration, TypeError):
        return "af_sky"


def _get_kokoro(model_path: str, voices_path: str) -> Any:
    """Construct ``Kokoro`` once per (model, voices) pair."""
    global _loaded, _loaded_key
    key = (model_path, voices_path)
    if _loaded is not None and _loaded_key == key:
        return _loaded
    kokoro_cls, _sf = _load_venv_audio()

    inst = kokoro_cls(model_path, voices_path)
    _loaded = inst
    _loaded_key = key
    return inst


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

    voice = str(req.get("voice") or "af_sky")
    raw_speed = req.get("speed")
    if raw_speed is None:
        speed = 1.0
    else:
        # What was wrong: float(req.get("speed") or 1.0) mapped a real 0.0 to 1.0.
        try:
            speed = float(raw_speed)
        except (TypeError, ValueError):
            speed = 1.0
    lang = str(req.get("lang") or "en-us")

    try:
        kokoro = _get_kokoro(model_path, voices_path)
        _cls, sf = _load_venv_audio()
    except ImportError as exc:
        return {
            "status": "error",
            "error": f"Kokoro dependency missing ({exc}). Install with: {KOKORO_PIP_INSTALL}",
        }
    except Exception as exc:
        return {"status": "error", "error": f"Kokoro load failed: {exc}"}

    warning = ""
    if not _has_voice(kokoro, voice):
        # Same substitution as the one-shot script in tts_service: a voices
        # file that lacks the requested id (English-only pack, custom path)
        # keeps speaking instead of failing the sentence.
        fallback = _fallback_voice(kokoro, voice)
        warning = f"Kokoro voice {voice!r} is not in {voices_path}; using {fallback}"
        voice = fallback

    # What was wrong: payload lang was the requested voice's language.
    # How it happened: voice fallback replaced the voice but kept lang.
    # Why this change: recompute lang from the final voice so phonemizing
    # does not pass the wrong language to the substitute voice.
    lang = kokoro_g2p_lang(text, voice)

    phonemes, g2p_warning = misaki_frontend(text, lang)
    if g2p_warning:
        warning = f"{warning} {g2p_warning}".strip() if warning else g2p_warning

    try:
        if phonemes:
            samples, rate = kokoro.create(
                phonemes, voice=voice, speed=speed, lang=lang, is_phonemes=True
            )
        else:
            samples, rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)

        # What was wrong: an empty samples array still wrote a header-only wav.
        # How it happened: parent treated any wav with size > 0 as success.
        # Why this change: reject empty samples before write so silence is not played.
        try:
            empty = samples is None or len(samples) == 0
        except TypeError:
            empty = True
        if empty:
            return {
                "status": "error",
                "error": "Kokoro returned no audio samples",
                "warning": warning,
            }
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
