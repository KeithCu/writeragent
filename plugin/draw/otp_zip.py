# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Capped ZIP member reader and extractor for Impress .otp templates."""

from __future__ import annotations

import logging
import os
import zipfile
import zlib

log = logging.getLogger("writeragent.draw.otp_zip")

# Caps so a hostile/corrupt user .otp cannot inflate listing or cloning.
MAX_MEMBER_BYTES = 2 * 1024 * 1024
# ZipExtFile.read(n) passes n through as zlib max_length, then slices to
# the header file_size. One read(limit) still builds that whole buffer
# before the slice, so a forged file_size of 64 still costs ~limit bytes.
# Chunks keep each inflate request small; the loop enforces the real cap.
READ_CHUNK = 64 * 1024


def norm_zip_name(name: str) -> str:
    """Normalize ZIP member path for comparison (slashes, leading ./ and lowercase)."""
    return name.replace("\\", "/").lstrip("./").lower()


def _find_member(zf: zipfile.ZipFile, name: str) -> str | None:
    """Find member name in zipfile matching case-insensitively or with normalized slashes."""
    names = zf.namelist()
    if name in names:
        return name
    target = norm_zip_name(name)
    for n in names:
        if norm_zip_name(n) == target:
            return n
    return None


def read_member(zf: zipfile.ZipFile, name: str, limit: int = MAX_MEMBER_BYTES) -> bytes | None:
    """Return one member's bytes, or None if missing, corrupt, or over *limit*.

    ``ZipInfo.file_size`` is not a read bound. That value is the
    uncompressed size in the central directory; the local file
    header stores the same field and both are forgeable.
    ``ZipExtFile.read`` with no size inflates via
    ``decompress(..., MAX_N)`` (~1 GiB) and only then slices to
    ``file_size``. A 64KB ``.otp`` whose declared size is 64 bytes
    still expands by ~100MB before a post-read cap can see the real
    length. ``list_designs`` → ``derive_otp_look`` does this on the
    main thread for every discovered template, including
    user-writable dirs (thumbnail, ``styles.xml``, and
    ``Pictures/*.svg``). A declared size above *limit* is skipped,
    but a small declared size is not the read bound. Chunks pass
    ``READ_CHUNK`` as zlib's ``max_length``, and the loop drops
    the member once more than *limit* bytes have been produced.
    """
    actual_name = _find_member(zf, name)
    if actual_name is None:
        return None
    try:
        info = zf.getinfo(actual_name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    buf = bytearray()
    try:
        with zf.open(info, "r") as src:
            while len(buf) <= limit:
                piece = src.read(READ_CHUNK)
                if not piece:
                    break
                if len(buf) + len(piece) > limit:
                    return None
                buf.extend(piece)
    except (zipfile.BadZipFile, EOFError, zlib.error):
        return None
    return bytes(buf)


def extract_member(
    zf: zipfile.ZipFile,
    name: str,
    dest_dir: str,
    prefix: str = "",
    limit: int = MAX_MEMBER_BYTES,
) -> str | None:
    """Extract a ZIP member under the member limit into dest_dir."""
    actual_name = _find_member(zf, name)
    if actual_name is None:
        return None
    # Refuse empty or directory traversal basenames.
    base = os.path.basename(actual_name.replace("\\", "/"))
    if not base or base in (".", ".."):
        return None
    data = read_member(zf, actual_name, limit)
    if data is None:
        return None
    filename = prefix + base
    out = os.path.join(dest_dir, filename)
    try:
        with open(out, "wb") as dst:
            dst.write(data)
        return out
    except Exception:
        log.debug("extract member failed name=%s", name, exc_info=True)
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
        return None
