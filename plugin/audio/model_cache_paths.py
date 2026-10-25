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
from pathlib import Path
import tempfile

from plugin.framework.errors import best_effort

log = logging.getLogger(__name__)

# Filenames from the kokoro-onnx ``model-files-v1.1`` release. The download
# URLs stay in ``tts_service``; this module only decides the on-disk path.
KOKORO_MODEL_FILENAME = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILENAME = "voices-v1.0.bin"

_KOKORO_CANONICAL = ("kokoro",)
_PIPER_CANONICAL = ("piper",)
# Same id as voice_catalog.PIPER_FALLBACK_VOICE without importing voice_catalog.
_PIPER_PLAIN_FALLBACK_ID = "en_US-lessac-medium"
_KOKORO_PROBE: tuple[tuple[str, ...], ...] = (_KOKORO_CANONICAL, ("kokoro-tts",), ("pipecat", "kokoro-onnx"))
_PIPER_PROBE: tuple[tuple[str, ...], ...] = (_PIPER_CANONICAL, ("pipecat", "piper"))

_KOKORO_ENV_FILES: dict[str, str] = {"KOKORO_MODEL_PATH": KOKORO_MODEL_FILENAME, "KOKORO_VOICES_PATH": KOKORO_VOICES_FILENAME}

_warned_overrides: set[tuple[str, str]] = set()


def _is_usable_file(path: Path) -> bool:
    """True when path exists, is a regular file, and has non-zero size."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _plain_piper_voice_id(voice_id: str) -> bool:
    """True when voice_id is a plain filename stem without path separators or traversal."""
    name = voice_id.strip()
    if not name or name in (".", ".."):
        return False
    if any(sep in name for sep in ("/", "\\", "\x00")):
        return False
    if Path(name).name != name:
        return False
    return True


def _warn_unusable_override(name: str, path: Path) -> None:
    key = (name, os.fspath(path))
    if key not in _warned_overrides:
        _warned_overrides.add(key)
        log.warning("%s=%s is not a usable file; Kokoro will not download over an override", name, path)


def shared_cache_root() -> Path:
    """Cache root shared with other local-model apps.

    ``XDG_CACHE_HOME`` when set to an absolute path (with ``~`` expanded);
    otherwise ``Path.home() / ".cache"``. Blank, whitespace-only, or relative values are ignored.
    """
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        expanded = Path(raw).expanduser()
        if expanded.is_absolute():
            return expanded
    return Path.home() / ".cache"


def kokoro_probe_dirs() -> tuple[Path, ...]:
    """Kokoro directories, first hit first."""
    return _under_cache(_KOKORO_PROBE)


def kokoro_download_dir() -> Path:
    """Canonical Kokoro dump: ``<cache>/kokoro``."""
    return shared_cache_root().joinpath(*_KOKORO_CANONICAL)


def piper_probe_dirs() -> tuple[Path, ...]:
    """Piper directories, first hit first."""
    return _under_cache(_PIPER_PROBE)


def piper_download_dir() -> Path:
    """Canonical Piper dump: ``<cache>/piper``."""
    return shared_cache_root().joinpath(*_PIPER_CANONICAL)


def resolve_kokoro_model_paths() -> tuple[Path, Path]:
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
    if model_path is not None and not _is_usable_file(model_path):
        _warn_unusable_override("KOKORO_MODEL_PATH", model_path)
    voices_path = _env_override("KOKORO_VOICES_PATH")
    if voices_path is not None and not _is_usable_file(voices_path):
        _warn_unusable_override("KOKORO_VOICES_PATH", voices_path)
    model_found: Path | None = None
    voices_found: Path | None = None

    if model_path is None or voices_path is None:
        for directory in kokoro_probe_dirs():
            if model_path is None:
                candidate = directory / KOKORO_MODEL_FILENAME
                if _is_usable_file(candidate):
                    model_path = candidate
                    model_found = directory
            if voices_path is None:
                candidate = directory / KOKORO_VOICES_FILENAME
                if _is_usable_file(candidate):
                    voices_path = candidate
                    voices_found = directory
            if model_path is not None and voices_path is not None:
                break

    if model_path is None:
        model_path = _kokoro_missing_target(voices_found) / KOKORO_MODEL_FILENAME
    if voices_path is None:
        voices_path = _kokoro_missing_target(model_found) / KOKORO_VOICES_FILENAME
    return model_path, voices_path


def kokoro_should_download(path: Path, env_var: str) -> bool:
    """Whether a missing Kokoro path should be fetched.

    An env override is downloaded only when os.fspath(path) equals both the
    override and kokoro_download_dir() / filename. Compare strings, do not resolve().
    A probed path is a download target.
    """
    if _is_usable_file(path):
        return False
    filename = _KOKORO_ENV_FILES.get(env_var)
    if filename is None:
        return True
    override = _env_override(env_var)
    if override is None:
        return True
    canonical = kokoro_download_dir() / filename
    return os.fspath(override) == os.fspath(path) and os.fspath(path) == os.fspath(canonical)


def resolve_piper_voice_paths(voice_id: str) -> tuple[Path, Path]:
    """Return ``({voice}.onnx, {voice}.onnx.json)``.

    A voice id that is not a single filename resolves as en_US-lessac-medium.
    Both files must sit in the same probe directory. A lone ``.onnx`` is not
    a hit — Piper needs the json beside it, which is what the downloader
    writes. Missing pairs point at ``<cache>/piper``, not Pipecat's tree.
    An absolute voice path that already exists is handled by the caller
    before this function runs.
    """
    if not _plain_piper_voice_id(voice_id):
        voice_id = _PIPER_PLAIN_FALLBACK_ID
    onnx_name = f"{voice_id}.onnx"
    json_name = f"{voice_id}.onnx.json"
    for directory in piper_probe_dirs():
        onnx_path = directory / onnx_name
        json_path = directory / json_name
        if _is_usable_file(onnx_path) and _is_usable_file(json_path):
            return onnx_path, json_path
    dest = piper_download_dir()
    return dest / onnx_name, dest / json_name


def _under_cache(relative: tuple[tuple[str, ...], ...]) -> tuple[Path, ...]:
    root = shared_cache_root()
    return tuple(root.joinpath(*parts) for parts in relative)


def _env_override(name: str) -> Path | None:
    """Set env path, or None when the variable is missing or blank.

    A blank or whitespace variable is unset. ``~`` is expanded; a missing file stays that expanded path (no probe).
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


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
    try:
        fd, probe_path = tempfile.mkstemp(prefix=".writeragent-write-probe-", dir=directory)
    except OSError:
        return False
    os.close(fd)
    best_effort(Path(probe_path).unlink, "Could not remove cache write probe")
    return True
