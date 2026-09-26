# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where local Kokoro and Piper weights already live, and where new ones go.

Other apps drop the same filenames under ``~/.cache``. WriterAgent reads those
copies in place (no migrate/copy) and downloads into the shared canonical
directory so the next app can reuse the file. ``XDG_CACHE_HOME`` replaces
``~/.cache`` when it is set. On Windows the unset root is
``%USERPROFILE%\\.cache`` — the layout Hugging Face already uses — not
``%LOCALAPPDATA%\\WriterAgent`` or ``~/Library/Caches``.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Filenames from the kokoro-onnx ``model-files-v1.1`` release. The download
# URLs stay in ``tts_service``; this module only decides the on-disk path.
KOKORO_MODEL_FILENAME = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILENAME = "voices-v1.0.bin"

# First existing file wins. Downloads target the first entry unless a later
# directory already holds one of the Kokoro pair and can take the sibling.
# Pipecat's tree is a probe hit, not the dump location: a fresh download in
# ``pipecat/`` would be invisible to apps that only look in ``kokoro/``.
_KOKORO_PROBE: tuple[tuple[str, ...], ...] = (("kokoro",), ("kokoro-tts",), ("pipecat", "kokoro-onnx"))
_PIPER_PROBE: tuple[tuple[str, ...], ...] = (("piper",), ("pipecat", "piper"))

_KOKORO_ENV_FILES: dict[str, str] = {"KOKORO_MODEL_PATH": KOKORO_MODEL_FILENAME, "KOKORO_VOICES_PATH": KOKORO_VOICES_FILENAME}


def shared_cache_root() -> Path:
    """Cache root shared with other local-model apps.

    ``XDG_CACHE_HOME`` when set and non-blank; otherwise ``Path.home() / ".cache"``.
    """
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache"


def kokoro_probe_dirs() -> tuple[Path, ...]:
    """Kokoro directories, first hit first."""
    return _under_cache(_KOKORO_PROBE)


def kokoro_download_dir() -> Path:
    """Canonical Kokoro dump: ``<cache>/kokoro``."""
    return kokoro_probe_dirs()[0]


def piper_probe_dirs() -> tuple[Path, ...]:
    """Piper directories, first hit first."""
    return _under_cache(_PIPER_PROBE)


def piper_download_dir() -> Path:
    """Canonical Piper dump: ``<cache>/piper``."""
    return piper_probe_dirs()[0]


def resolve_kokoro_model_paths(model_filename: str = KOKORO_MODEL_FILENAME, voices_filename: str = KOKORO_VOICES_FILENAME) -> tuple[Path, Path]:
    """Return the Kokoro model and voices paths to use or to download into.

    A non-empty ``KOKORO_MODEL_PATH`` / ``KOKORO_VOICES_PATH`` locks that file:
    the path is used even when it is missing, and a probed copy is not
    substituted. That matches the old ``env or default`` assignment, which
    never searched another directory after an override was set.

    Otherwise each filename is taken from the first probe directory that has
    it. When one file is present and the other is not, the missing path is
    placed in that same directory if we can create a file there; if not, it
    points at ``<cache>/kokoro`` and the two paths may differ.
    """
    model_path = _env_override("KOKORO_MODEL_PATH")
    voices_path = _env_override("KOKORO_VOICES_PATH")
    model_found: Path | None = None
    voices_found: Path | None = None

    if model_path is None or voices_path is None:
        for directory in kokoro_probe_dirs():
            if model_path is None:
                candidate = directory / model_filename
                if candidate.is_file():
                    model_path = candidate
                    model_found = directory
            if voices_path is None:
                candidate = directory / voices_filename
                if candidate.is_file():
                    voices_path = candidate
                    voices_found = directory
            if model_path is not None and voices_path is not None:
                break

    if model_path is None:
        model_path = _kokoro_missing_target(voices_found) / model_filename
    if voices_path is None:
        voices_path = _kokoro_missing_target(model_found) / voices_filename
    return model_path, voices_path


def kokoro_should_download(path: Path, env_var: str) -> bool:
    """Whether a missing Kokoro path should be fetched.

    An explicit env path is downloaded only when it is the canonical cache
    file. The previous resolver used ``path == default and not exists`` for
    the same reason: ``KOKORO_MODEL_PATH=/tmp/missing.onnx`` was returned
    unchanged and did not trigger a download or a search of ``~/.cache``.
    A path chosen by probing (including a sibling beside a file we already
    found) is a download target.
    """
    if path.is_file():
        return False
    filename = _KOKORO_ENV_FILES.get(env_var)
    if filename is None:
        return True
    override = _env_override(env_var)
    if override is None:
        return True
    canonical = kokoro_download_dir() / filename
    # Exact string match, not ``resolve()``: the old code compared the env
    # string to the expanded default and did not fold ``..`` or symlinks.
    return os.fspath(override) == os.fspath(path) and os.fspath(path) == os.fspath(canonical)


def resolve_piper_voice_paths(voice_id: str) -> tuple[Path, Path]:
    """Return ``({voice}.onnx, {voice}.onnx.json)``.

    Both files must sit in the same probe directory. A lone ``.onnx`` is not
    a hit — Piper needs the json beside it, which is what the downloader
    writes. Missing pairs point at ``<cache>/piper``, not Pipecat's tree.
    An absolute voice path that already exists is handled by the caller
    before this function runs.
    """
    onnx_name = f"{voice_id}.onnx"
    json_name = f"{voice_id}.onnx.json"
    for directory in piper_probe_dirs():
        onnx_path = directory / onnx_name
        json_path = directory / json_name
        if onnx_path.is_file() and json_path.is_file():
            return onnx_path, json_path
    dest = piper_download_dir()
    return dest / onnx_name, dest / json_name


def _under_cache(relative: tuple[tuple[str, ...], ...]) -> tuple[Path, ...]:
    root = shared_cache_root()
    return tuple(root.joinpath(*parts) for parts in relative)


def _env_override(name: str) -> Path | None:
    """Set env path, or None when the variable is missing or blank.

    A blank value is treated like unset (``os.environ.get(name) or default``).
    The path is not expanded: an override of ``~/models/x`` used to be taken
    literally, and a missing file must stay that literal path.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    return Path(raw)


def _kokoro_missing_target(sibling_dir: Path | None) -> Path:
    """Directory a missing Kokoro file should be downloaded into."""
    if sibling_dir is not None and _directory_is_writable(sibling_dir):
        return sibling_dir
    return kokoro_download_dir()


def _directory_is_writable(directory: Path) -> bool:
    """True when a missing sibling file can be created in ``directory``.

    ``os.access`` is the wrong check here: it reports a mode-0555 directory
    as writable for root, and it reports success for mounts that still reject
    the create. A throwaway file is the same operation the download will do.
    """
    if not directory.is_dir():
        return False
    # Unique per call so two resolves in one process do not share the probe name.
    probe = directory / f".writeragent-write-probe-{uuid.uuid4().hex}"
    try:
        fd = os.open(os.fspath(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        probe.unlink()
    except OSError:
        log.debug("Could not remove cache write probe %s", probe, exc_info=True)
    return True
