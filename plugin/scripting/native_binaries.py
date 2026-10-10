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

import hashlib
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

# master-branch raw.githubusercontent.com had no digest, so a swapped body was
# imported into the LibreOffice process. Pin the commit and check sha256 before
# the partial replaces a host-mapped native.
_CONTRIB_COMMIT = "440924ec58ec29f3112c3eab643d4bb285861f20"
_CONTRIB_BASE_URL = f"https://raw.githubusercontent.com/KeithCu/writeragent/{_CONTRIB_COMMIT}/contrib/"
# urlopen's default is no timeout, so a stalled native download blocked the caller.
_NATIVE_DOWNLOAD_TIMEOUT_SEC = 60
_MAX_VEC_PACK_BYTES = 1_048_576
# sha256 of contrib/vec_pack blobs at _CONTRIB_COMMIT. Audio zips are not pinned.
_VEC_PACK_SHA256 = {
    "__init__.py": "cc00e1b2a43a73974798085e64df9492f744dad2415dbfeefe5582cd67433fb7",
    "pack.cp311-win_amd64.pyd": "da48716e9dcbad3802d58d3a47da86a20ff941d845d413c0cbe5fb839fef8f17",
    "pack.cp311-win_arm64.pyd": "0d113b50bbd035ed7b30c549964bcef740b3359e629d96ef7ec034209f238d50",
    "pack.cp312-win_amd64.pyd": "62037eb470bd9a45da46c910eff808f5f775f91cce8406d4c87ed4b4fb2094c1",
    "pack.cp312-win_arm64.pyd": "0c58c2480aad0aebbbaadc7b9f08dfd65e715811385e83d55bb5cef2a1bfee2f",
    "pack.cp313-win_amd64.pyd": "6bc59ed46964e500424b73221b713c06e30f9b69a524dc9faff16514c5fa3c96",
    "pack.cp313-win_arm64.pyd": "101e41dad713a305078b52f2ea4c572bb56496b7b12e78185e20511dd455e9f8",
    "pack.cp314-win_amd64.pyd": "8fed7eb82dd45f1d42b5a631a118da0f5b201707f69825d29bdfeecf71a29109",
    "pack.cp314-win_arm64.pyd": "3bfe863147fd56893540eee5065edad713468a0a92afe2db76bd6e18e482317c",
    "pack.cpython-311-aarch64-linux-gnu.so": "6ae401976b22fdc0937b95adf7e87b679f0089cb61fe868e981d238ec63796ad",
    "pack.cpython-311-darwin.so": "317d6a011c7851b0f31b4cf5d0f66fee26e82f3df959acda6877b8663c448d14",
    "pack.cpython-311-x86_64-linux-gnu.so": "b0204142c0d7512385076bcf6266a0e88108947ebd304c7429ab6920237b04c7",
    "pack.cpython-312-aarch64-linux-gnu.so": "e833d78dcd245e1293245eb36ed323ac02a7363d418f5142366d66c4a84bf0a8",
    "pack.cpython-312-darwin.so": "992b62dec6a17779a8a429b26fad238c4d9d4662da061bcdea27f8b483b6cb86",
    "pack.cpython-312-x86_64-linux-gnu.so": "2599b127bd99276eb6768700c17c5cb5ea99b361f9c75c90fbf2908236351c06",
    "pack.cpython-313-aarch64-linux-gnu.so": "33f8119215e2e44c1209a0182774ba7d0266aa10542d268d129cdf68cbb4583f",
    "pack.cpython-313-darwin.so": "7232b4cb3e74e14eb273c168f909239406b4b48cd8e665870031b83db8105617",
    "pack.cpython-313-x86_64-linux-gnu.so": "fa57b98aafad56e5e79ff5e6015d5c9542e67ea1176de282fec416f8f5f2388d",
    "pack.cpython-314-aarch64-linux-gnu.so": "606f65dcd0ca720792a8cc32833f161c0eddfddcbf513aca3162850dd518ca39",
    "pack.cpython-314-darwin.so": "3d294d290a44cff8e557e9d0130abb9c62fd4ea1f10c347c84debdbdb22fc846",
    "pack.cpython-314-x86_64-linux-gnu.so": "5102dd1f5ccc05c0b821e048e580949ad9977e4d507a78e2634c6b2cb8e6196f",
}

_MAX_AUDIO_BYTES = 1_048_576
_AUDIO_SHA256: dict[str, str] = {
    "audio_source.zip": "ab8a80786511afdd68d52e94b439e0484908a05c188ec2686419c3d5ad358110",
    "libportaudio.dylib": "03190134f5d59c999dd13bc66213444f289af88c29f8a8003d77b018700d8ed9",
    "libportaudio64bit.dll": "ec080194f01e4095c7fb43dbd7ed05af922c5b34295056a9ff56782741d65481",
    "libportaudioarm64.dll": "e39e95e95c0cb262c70ca4e676e440cf34be5643fe5a31cdfe23e8ab6f1422cb",
    "_cffi_backend.cp311-win_amd64.pyd": "87243aab8aa82f05b21565471b08ffbf8ab4a9a8282e55d7579b487d9e6c8daa",
    "_cffi_backend.cp311-win_arm64.pyd": "ff3705700706a664308843f62bf7c1f1c1581d26487cd354c16ac085d748ae18",
    "_cffi_backend.cp312-win_amd64.pyd": "c24340a5484db93df7d2654149196ae4fd2d7560c73ef4ae12ccc51ae8a7fb70",
    "_cffi_backend.cp312-win_arm64.pyd": "12cdd4100dfd0ecdb1cc073a5142a267cb630ac1b76ebacf7350300de04f4067",
    "_cffi_backend.cp313-win_amd64.pyd": "3215e22f0264be58239bd671cf4bbfca403b3802260cbceb414f6b72ebe45cfe",
    "_cffi_backend.cp313-win_arm64.pyd": "a4afd30998a4d876ce1bff0890a34225cc8ee316c7e5bbd27a7ddd79795fa028",
    "_cffi_backend.cp314-win_amd64.pyd": "e5137002ad41c6d9563fd7a72fcc7310e75d93f158f8e4e7139ac47944451010",
    "_cffi_backend.cp314-win_arm64.pyd": "9169c80f0556a677db53c8ac3d68f877e456bac2a1f3a89d17bca12164215506",
    "_cffi_backend.cpython-311-aarch64-linux-gnu.so": "7f6d9c0fd109014f5501a4ae207a8aad7a6ea23bbf817431347cc47a71c565b1",
    "_cffi_backend.cpython-311-darwin.so": "6df24e0e996a136c2b28b861a8b38b9a55872754116c4442e7a8bf676c48b94f",
    "_cffi_backend.cpython-311-x86_64-linux-gnu.so": "4fd21c28b220ba95596d630f4c23d7361beb0252bee3133693d7f3ddff682fc9",
    "_cffi_backend.cpython-312-aarch64-linux-gnu.so": "dd1595e11919a2d2f16f2690b3c59d573c5afca7b47d1953381743b7e8923978",
    "_cffi_backend.cpython-312-darwin.so": "b85e100c08e86b188f99debb3326499742f908b4fac377dde3a5366d800fc16c",
    "_cffi_backend.cpython-312-x86_64-linux-gnu.so": "5b35d92ce7fd74f288a11e755713d731734951789a2ea09da6c7c76e7080cc7c",
    "_cffi_backend.cpython-313-aarch64-linux-gnu.so": "9a380b033c3027c86b153d446ecc27c9174516d6d7f970f06ddcfb886adc95b0",
    "_cffi_backend.cpython-313-darwin.so": "0bc68363fddb89f0e25e5c735f5debf2d7b2e64fa817f2ad4ff1a1f0c15377a9",
    "_cffi_backend.cpython-313-x86_64-linux-gnu.so": "c0da5c6c9d146b36cc27394e98fac6125f77816d76f8ffb430ad82cdeb39b04e",
    "_cffi_backend.cpython-314-aarch64-linux-gnu.so": "7115a9e8c0cbe545b40a9ee8153198db30a0d0e2790cc2ed4d3c975515a9caf1",
    "_cffi_backend.cpython-314-darwin.so": "4ffda54cc470c1cbfdc975f7808553794a34c2ad3408c215f90aac2cf77a4893",
    "_cffi_backend.cpython-314-x86_64-linux-gnu.so": "db7fc2f6b397158cd867038949ca3524403aa0821e6778e657f41188188d5fd9",
}

_swept = False


def _cleanup_stale_native_backups(bin_dir: str) -> None:
    """Delete leftover ``*.old`` native backups from Windows rename-aside redownloads.

    On Windows a loaded ``.pyd`` cannot be replaced in place, so redownload renames
    the loaded image aside (``pack….pyd.old``) and drops the new file in place. Those
    aside files are only unlocked after the owning process exits, so we sweep them on
    the next startup once nothing maps them. POSIX never creates ``*.old`` backups
    (``os.replace`` keeps the old inode alive), so this is a no-op there.
    """
    global _swept
    if os.name != "nt" or _swept:
        return
    _swept = True
    try:
        import re

        for root, _dirs, files in os.walk(bin_dir):
            for name in files:
                # '.old' in name also matches foo.older.py. Match only the
                # .old and .old.<num> backup suffixes.
                if not re.search(r"\.old(\.\d+)?$", name):
                    continue
                stale = os.path.join(root, name)
                try:
                    os.remove(stale)
                except OSError:
                    # Still mapped by another live process; try again next startup.
                    pass
    except OSError as exc:
        log.debug("Failed to sweep stale native backups in %s: %s", bin_dir, exc)


def native_bin_dir() -> str:
    """Single source of truth for the on-disk native binary directory path."""
    from plugin.framework.config import user_config_dir

    ucd = user_config_dir()
    if not ucd:
        raise RuntimeError("User config directory not resolved.")
    return os.path.join(ucd, "audio_binaries")


def ensure_native_binaries_on_path() -> None:
    """Ensure host binaries (audio + writeragent_vec) in user config or in-tree contrib are on sys.path."""
    try:
        bin_dir = native_bin_dir()
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
    *,
    max_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    """Download *url* to *dest_path* via a sibling ``.partial`` file + ``os.replace``.

    Never truncate an existing destination in place: host natives (``pack*.so``,
    ``_cffi_backend*.so``) may already be mmap'd after a prior import; in-place
    ``open(..., "wb")`` rewrite can SIGBUS LibreOffice on the next call into
    the old mapping (redownload crash).

    *max_bytes* aborts once the body grows past the cap. *expected_sha256*, when
    set, is checked on the partial before it replaces *dest_path*; a mismatch
    leaves the previous file in place. Both default off so audio zip downloads
    keep working without pins.
    """
    import tempfile
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    # A fixed dest_path + '.partial' lets concurrent downloads (settings probe
    # and the background worker) clobber each other. mkstemp gives each
    # download its own partial file.
    fd, partial_path = tempfile.mkstemp(dir=dest_dir, suffix=".partial")
    try:
        with urllib.request.urlopen(req, timeout=_NATIVE_DOWNLOAD_TIMEOUT_SEC) as response:
            total_size = int(response.headers.get("content-length", 0))
            block_size = 65536
            downloaded = 0
            digest = hashlib.sha256() if expected_sha256 is not None else None
            with os.fdopen(fd, "wb") as fh:
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    fh.write(buffer)
                    if digest is not None:
                        digest.update(buffer)
                    if max_bytes is not None and downloaded > max_bytes:
                        raise RuntimeError(
                            f"Download of {os.path.basename(dest_path)} exceeded {max_bytes} bytes"
                        )
                    if total_size:
                        percent = int(downloaded * 100 / total_size)
                        on_status(f"Downloading {os.path.basename(dest_path)}: {percent}%")
            if total_size and downloaded != total_size:
                raise RuntimeError(
                    f"Download of {os.path.basename(dest_path)} stopped at {downloaded} of {total_size} bytes"
                )
        if digest is not None and expected_sha256 is not None:
            actual = digest.hexdigest()
            if actual != expected_sha256.lower():
                raise RuntimeError(
                    f"sha256 mismatch for {os.path.basename(dest_path)}: "
                    f"expected {expected_sha256}, got {actual}"
                )
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
    bind_host: bool = True,
) -> bool:
    """Download the platform-specific Cython pack binary from contrib/vec_pack on GitHub.

    ``bind_host`` (default) calls ``ensure_native_binaries_on_path`` and
    ``invalidate_host_cython_accelerator`` on the caller before returning.
    LibrePy Settings passes False: that probe runs off the VCL thread, and the
    listener binds on the main thread instead.
    """
    import platform
    import sysconfig

    target_dir = native_bin_dir()
    os.makedirs(target_dir, exist_ok=True)

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not ext_suffix:
        raise RuntimeError("Failed to determine Python EXT_SUFFIX.")

    base_url = _CONTRIB_BASE_URL

    if include_header:
        on_display(f"Target directory: {target_dir}\n")
        on_display(f"Platform: {platform.system()} ({platform.machine()})\n")
        on_display(f"Python: {platform.python_version()}\n\n")

    vec_init_url = f"{base_url}vec_pack/__init__.py"
    vec_init_dest = os.path.join(target_dir, "writeragent_vec", "__init__.py")
    pack_name = f"pack{ext_suffix}"
    if pack_name not in _VEC_PACK_SHA256:
        raise RuntimeError(f"No pinned sha256 for {pack_name}")
    on_display("Downloading writeragent_vec/__init__.py...\n")
    _download_url_to_file(
        vec_init_url,
        vec_init_dest,
        on_status,
        max_bytes=_MAX_VEC_PACK_BYTES,
        expected_sha256=_VEC_PACK_SHA256["__init__.py"],
    )

    vec_bin_url = f"{base_url}vec_pack/{pack_name}"
    vec_bin_dest = os.path.join(target_dir, "writeragent_vec", pack_name)
    on_display(f"Downloading binary {pack_name}...\n")
    _download_url_to_file(
        vec_bin_url,
        vec_bin_dest,
        on_status,
        max_bytes=_MAX_VEC_PACK_BYTES,
        expected_sha256=_VEC_PACK_SHA256[pack_name],
    )

    if bind_host:
        ensure_native_binaries_on_path()
        # Drop stale in-process module after replace so the next load binds the new inode.
        from plugin.scripting.payload_codec import invalidate_host_cython_accelerator

        invalidate_host_cython_accelerator()
    if include_header:
        on_display("\nCython accelerator binary installed successfully.\n")
    return True


def run_audio_download(on_display: Callable[[str], None], on_status: Callable[[str], None]) -> bool:
    """Download the pure-Python audio source zip and platform-specific compiled binaries from GitHub."""
    import platform
    import sysconfig
    import zipfile

    target_dir = native_bin_dir()
    os.makedirs(target_dir, exist_ok=True)

    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not ext_suffix:
        raise RuntimeError("Failed to determine Python EXT_SUFFIX.")

    cffi_name = f"_cffi_backend{ext_suffix}"
    if cffi_name not in _AUDIO_SHA256:
        raise RuntimeError(f"No pinned sha256 for {cffi_name}")

    portaudio_name = None
    if platform.system() == "Darwin":
        portaudio_name = "libportaudio.dylib"
    elif platform.system() == "Windows":
        is_arm = platform.machine().lower() in ("arm64", "aarch64")
        platform_suffix = "arm64" if is_arm else "64bit"
        portaudio_name = f"libportaudio{platform_suffix}.dll"

    if portaudio_name and portaudio_name not in _AUDIO_SHA256:
        raise RuntimeError(f"No pinned sha256 for {portaudio_name}")

    base_url = _CONTRIB_BASE_URL

    on_display(f"Target directory: {target_dir}\n")
    on_display(f"Platform: {platform.system()} ({platform.machine()})\n")
    on_display(f"Python: {platform.python_version()}\n\n")

    # Download pure Python source zip
    zip_url = f"{base_url}audio_source.zip"
    zip_dest = os.path.join(target_dir, "audio_source.zip")
    on_display("Downloading pure Python audio libraries (audio_source.zip)...\n")
    _download_url_to_file(
        zip_url,
        zip_dest,
        on_status,
        max_bytes=_MAX_AUDIO_BYTES,
        expected_sha256=_AUDIO_SHA256["audio_source.zip"],
    )

    # Extract audio_source.zip
    on_status("Extracting audio_source.zip...")
    on_display("Extracting audio_source.zip...\n")
    try:
        with zipfile.ZipFile(zip_dest, "r") as zf:
            zf.extractall(target_dir)
    except Exception as exc:
        raise RuntimeError(f"Failed to extract audio_source.zip: {exc}") from exc
    finally:
        if os.path.exists(zip_dest):
            try:
                os.remove(zip_dest)
            except Exception:
                pass

    # Download CFFI binary
    cffi_url = f"{base_url}audio/{cffi_name}"
    cffi_dest = os.path.join(target_dir, cffi_name)
    on_display(f"Downloading binary {cffi_name}...\n")
    _download_url_to_file(
        cffi_url,
        cffi_dest,
        on_status,
        max_bytes=_MAX_AUDIO_BYTES,
        expected_sha256=_AUDIO_SHA256[cffi_name],
    )

    # Download PortAudio binary if needed
    if portaudio_name:
        pa_url = f"{base_url}audio/_sounddevice_data/portaudio-binaries/{portaudio_name}"
        pa_dest = os.path.join(target_dir, "_sounddevice_data", "portaudio-binaries", portaudio_name)
        on_display(f"Downloading binary {portaudio_name}...\n")
        _download_url_to_file(
            pa_url,
            pa_dest,
            on_status,
            max_bytes=_MAX_AUDIO_BYTES,
            expected_sha256=_AUDIO_SHA256[portaudio_name],
        )

    # Create _sounddevice_data/__init__.py placeholder
    init_dest = os.path.join(target_dir, "_sounddevice_data", "__init__.py")
    os.makedirs(os.path.dirname(init_dest), exist_ok=True)
    with open(init_dest, "w") as f:
        f.write("# Placeholder\n")

    run_vec_pack_download(on_display, on_status, include_header=False)
    on_display("\nAll downloaded files installed successfully!\n")
    return True
