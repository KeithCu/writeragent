# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side download + sys.path for Cython pack binaries (and audio natives).

LibrePy Settings → Python downloads only the vec_pack accelerator. WriterAgent
also uses this directory for microphone recording binaries. The on-disk folder
stays ``audio_binaries`` so existing installs keep working.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

_CONTRIB_BASE_URL = "https://raw.githubusercontent.com/KeithCu/writeragent/master/contrib/"
# urlopen's default is no timeout, so a stalled native download blocked the caller.
_NATIVE_DOWNLOAD_TIMEOUT_SEC = 60


def _cleanup_stale_native_backups(bin_dir: str) -> None:
    """Delete leftover ``*.old`` native backups from Windows rename-aside redownloads.

    On Windows a loaded ``.pyd`` cannot be replaced in place, so redownload renames
    the loaded image aside (``pack….pyd.old``) and drops the new file in place. Those
    aside files are only unlocked after the owning process exits, so we sweep them on
    the next startup once nothing maps them. POSIX never creates ``*.old`` backups
    (``os.replace`` keeps the old inode alive), so this is a no-op there.
    """
    if os.name != "nt":
        return
    try:
        for root, _dirs, files in os.walk(bin_dir):
            for name in files:
                if ".old" not in name:
                    continue
                stale = os.path.join(root, name)
                try:
                    os.remove(stale)
                except OSError:
                    # Still mapped by another live process; try again next startup.
                    pass
    except OSError as exc:
        log.debug("Failed to sweep stale native backups in %s: %s", bin_dir, exc)


def ensure_downloaded_audio_on_path() -> None:
    """Ensure host binaries (audio + writeragent_vec) in user config or in-tree contrib are on sys.path."""
    from plugin.framework.config import user_config_dir

    try:
        ucd = user_config_dir()
        if ucd:
            bin_dir = os.path.join(ucd, "audio_binaries")
            if os.path.isdir(bin_dir):
                _cleanup_stale_native_backups(bin_dir)
                if bin_dir not in sys.path:
                    sys.path.insert(0, bin_dir)
    except Exception as exc:
        log.debug("Failed to add user config audio path to sys.path: %s", exc)

    try:
        # Also check for in-tree repo contrib directory (e.g. standalone/venv checkout)
        mod_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.abspath(os.path.join(mod_dir, "..", ".."))
        contrib_dir = os.path.join(repo_root, "contrib")
        if os.path.isdir(contrib_dir):
            if contrib_dir not in sys.path:
                sys.path.insert(0, contrib_dir)
            vec_pack_dir = os.path.join(contrib_dir, "vec_pack")
            if os.path.isdir(vec_pack_dir) and vec_pack_dir not in sys.path:
                sys.path.insert(0, vec_pack_dir)
    except Exception as exc:
        log.debug("Failed to add in-tree contrib path to sys.path: %s", exc)



def _atomic_replace_native(partial_path: str, dest_path: str) -> None:
    """Move *partial_path* onto *dest_path* without ever overwriting a live mapping.

    POSIX: a plain ``os.replace`` is atomic and keeps any existing ``mmap`` valid on
    its old inode. Windows: a currently-loaded ``.pyd``/DLL cannot be deleted or
    replaced (sharing violation), but it *can* be renamed. So on failure, rename the
    loaded target aside (unique ``*.old`` name) and drop the new file in place; the
    running process keeps its old in-memory copy and the fresh binary is used on the
    next launch. ``_cleanup_stale_native_backups`` sweeps the aside files at startup.
    """
    try:
        os.replace(partial_path, dest_path)
        return
    except OSError:
        if os.name != "nt" or not os.path.exists(dest_path):
            raise
        aside = f"{dest_path}.old"
        counter = 0
        while os.path.exists(aside):
            counter += 1
            aside = f"{dest_path}.old.{counter}"
        os.replace(dest_path, aside)
        try:
            os.replace(partial_path, dest_path)
        except OSError:
            # Restore the original so we never leave dest missing.
            os.replace(aside, dest_path)
            raise


def _download_url_to_file(
    url: str,
    dest_path: str,
    on_status: Callable[[str], None],
) -> None:
    """Download *url* to *dest_path* via a sibling ``.partial`` file + ``os.replace``.

    Never truncate an existing destination in place: host natives (``pack*.so``,
    ``_cffi_backend*.so``) may already be mmap'd after a prior import; in-place
    ``open(..., "wb")`` rewrite can SIGBUS LibreOffice on the next call into
    the old mapping (redownload crash).
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    partial_path = dest_path + ".partial"
    try:
        with urllib.request.urlopen(req, timeout=_NATIVE_DOWNLOAD_TIMEOUT_SEC) as response:
            total_size = int(response.headers.get("content-length", 0))
            block_size = 8192
            downloaded = 0
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            with open(partial_path, "wb") as fh:
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    fh.write(buffer)
                    if total_size:
                        percent = int(downloaded * 100 / total_size)
                        on_status(f"Downloading {os.path.basename(dest_path)}: {percent}%")
        _atomic_replace_native(partial_path, dest_path)
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"HTTP Error {err.code}: {err.reason} for URL: {url}") from err
    except Exception as exc:
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc
    finally:
        if os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass


def run_vec_pack_download(
    on_display: Callable[[str], None],
    on_status: Callable[[str], None],
    *,
    include_header: bool = True,
) -> bool:
    """Download the platform-specific Cython pack binary from contrib/vec_pack on GitHub."""
    import platform
    import sysconfig

    from plugin.framework.config import user_config_dir

    ucd = user_config_dir()
    if not ucd:
        raise RuntimeError("User config directory not resolved.")

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not ext_suffix:
        raise RuntimeError("Failed to determine Python EXT_SUFFIX.")

    target_dir = os.path.join(ucd, "audio_binaries")
    os.makedirs(target_dir, exist_ok=True)
    base_url = _CONTRIB_BASE_URL

    if include_header:
        on_display(f"Target directory: {target_dir}\n")
        on_display(f"Platform: {platform.system()} ({platform.machine()})\n")
        on_display(f"Python: {platform.python_version()}\n\n")

    vec_init_url = f"{base_url}vec_pack/__init__.py"
    vec_init_dest = os.path.join(target_dir, "writeragent_vec", "__init__.py")
    on_display("Downloading writeragent_vec/__init__.py...\n")
    _download_url_to_file(vec_init_url, vec_init_dest, on_status)

    pack_name = f"pack{ext_suffix}"
    vec_bin_url = f"{base_url}vec_pack/{pack_name}"
    vec_bin_dest = os.path.join(target_dir, "writeragent_vec", pack_name)
    on_display(f"Downloading binary {pack_name}...\n")
    _download_url_to_file(vec_bin_url, vec_bin_dest, on_status)

    ensure_downloaded_audio_on_path()
    # Drop stale in-process module after replace so the next load binds the new inode.
    from plugin.scripting.payload_codec import invalidate_host_cython_accelerator

    invalidate_host_cython_accelerator()
    if include_header:
        on_display("\nCython accelerator binary installed successfully.\n")
    return True
